"""The shared broker: schema, ownership, budgets, dedup, capabilities and audit."""
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time

import httpx
from jsonschema import Draft202012Validator

from .contracts import ToolContext, ToolResult, tool_result
from .guides import GUIDES, classify_failure
from .security import redact


_RESERVED = {"session_id", "task_id", "request_id", "target_id", "capability_id", "evidence_group_id",
             "capabilities", "confirmed", "path", "command", "shell", "base_url", "api_key"}
_RESULT_STATUSES = {"ok", "verified", "failed", "needs_input", "parked", "partial", "cancelled"}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    handler: object
    mutating: bool = False
    required_capability: str | None = None


def _error(code, message, status="failed"):
    return tool_result(status, error={"code": code, "message": message})


class ToolRegistry:
    def __init__(self, store, *, redactor=None, writes_available=None):
        self.store = store
        self.redactor = redactor or redact
        self.writes_available = writes_available or (lambda: False)
        self._tools = {}
        self._admission_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def register(self, name, description, parameters, handler, *, mutating=False,
                 required_capability=None, cache_scope="task"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("tool name must use model-compatible letters, digits, underscores or hyphens")
        if name in self._tools or cache_scope != "task":
            raise ValueError("duplicate tool or unsupported cache scope")
        if mutating and not required_capability:
            raise ValueError("mutating tool requires a server-issued scoped capability")
        def reject_refs(value):
            if isinstance(value, dict):
                if "$ref" in value or "$dynamicRef" in value:
                    raise ValueError("schema references are not supported")
                for item in value.values():
                    reject_refs(item)
            elif isinstance(value, list):
                for item in value:
                    reject_refs(item)
        reject_refs(parameters)
        Draft202012Validator.check_schema(parameters)
        if parameters.get("type") != "object" or parameters.get("additionalProperties") is not False:
            raise ValueError("tool root must be a closed object schema")
        self._tools[name] = ToolDefinition(name, description, parameters, handler, mutating, required_capability)

    def schemas(self):
        return [{"type": "function", "function": {"name": item.name, "description": item.description,
                "parameters": item.parameters}} for item in self._tools.values()]

    def has(self, name):
        return name in self._tools

    @staticmethod
    def operation_key(name, arguments, context):
        raw = json.dumps([context.task_id, name, arguments, context.target_id, context.capability_id,
                          context.evidence_group_id, context.state_version], sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def call(self, name: str, arguments: dict, context: ToolContext) -> ToolResult:
        task = self.store.get_task(context.session_id, context.task_id)
        if task is None or task["request_id"] != context.request_id:
            return _error("unauthorized_task", "工具请求不属于当前会话")
        if task["status"] not in {"queued", "running"} or task["cancel_requested"]:
            return _error("task_stopped", "任务已停止，未执行新操作", "cancelled")
        if context.deadline is not None and time.time() >= context.deadline:
            return _error("deadline", "本次请求已达到时间上限", "partial")
        definition = self._tools.get(name)
        if definition is None:
            return _error("unknown_tool", "该工具尚未实现或未启用")
        if not isinstance(arguments, dict) or any(key in arguments for key in _RESERVED):
            return _error("invalid_arguments", "模型参数不能覆盖会话、目标、路径或操作授权")
        try:
            if len(json.dumps(arguments, allow_nan=False)) > 32000:
                raise ValueError()
            if next(Draft202012Validator(definition.parameters).iter_errors(arguments), None):
                raise ValueError()
            key = self.operation_key(name, arguments, context)
        except (ValueError, TypeError):
            return _error("invalid_arguments", "工具参数未通过结构校验")
        if definition.mutating and (not context.capability_id or not context.target_id or
                                    definition.required_capability not in context.capabilities):
            return _error("capability_required", "需要服务端核定的明确目标及本次操作范围，模型确认不能授权")
        if definition.mutating and not self.writes_available():
            return _error("writes_disabled", "当前诊断写入尚未启用；订单批次优先，未执行写入", "needs_input")
        with self._admission_lock:
            cached = self.store.get_record(context.session_id, "tool_calls", key)
            if cached:
                if cached["payload"].get("state") == "completed":
                    return cached["payload"]["result"]
                return _error("interrupted_operation", "该操作已开始但结果未确认，保留凭据并停止重复执行", "needs_input")
            admission = self.store.consume_budget(context.session_id, context.task_id, "tool", key)
            if not admission["allowed"]:
                return _error(admission["reason"], "本次工具预算已耗尽或任务已停止", "partial")
            if admission["replayed"]:
                return _error("interrupted_operation", "已有准入记录，结果未确认，未再次执行外部操作", "needs_input")
            try:
                self.store.put_record(context.session_id, "tool_calls", key,
                    {"state": "started", "name": name, "arguments": self.redactor(arguments)},
                    task_id=context.task_id, expected_revision=0)
            except ValueError:
                return _error("interrupted_operation", "操作已由另一执行器接收，未重复执行", "needs_input")
        acquired = False
        from .network import network_scope
        def cancelled():
            current = self.store.get_task(context.session_id, context.task_id)
            if not current or current['cancel_requested'] or current['status'] not in {'queued', 'running'}:
                return True
            plan_id = current.get('payload', {}).get('gold_repair_plan_id')
            if plan_id:
                saved = self.store.get_record(context.session_id, 'gold-repair-plans', plan_id)
                if not saved or saved['payload'].get('cancelled'):
                    return True
                parent_id = saved['payload'].get('active_parent_task_id')
                parent = self.store.get_task(context.session_id, parent_id) if parent_id else None
                if not parent or parent['cancel_requested'] or parent['status'] not in {'queued', 'running'}:
                    return True
            return False
        scope = network_scope(context, cancelled)
        scope.__enter__()
        try:
            if definition.mutating:
                acquired = self._write_lock.acquire(blocking=False)
                if not acquired:
                    result = _error("write_busy", "已有诊断写任务，保留当前任务等待处理", "needs_input")
                else:
                    result = definition.handler(arguments, context)
            else:
                result = definition.handler(arguments, context)
            if not isinstance(result, dict) or set(result) != {"status", "data", "evidence_refs", "error", "retryable"}:
                raise ValueError("invalid tool result")
            if result["status"] not in _RESULT_STATUSES or not isinstance(result["evidence_refs"], list):
                raise ValueError("invalid tool result")
            result = self.redactor(result)
            if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > 128000:
                raise ValueError("oversized tool result")
        except Exception:
            result = _error("tool_failed", "工具执行未完成，请查看记录；未将模型描述当作执行成功")
        finally:
            scope.__exit__(None, None, None)
            if acquired:
                self._write_lock.release()
        self.store.put_record(context.session_id, "tool_calls", key, {"state": "completed", "name": name, "result": result}, task_id=context.task_id)
        self.store.append_event(context.session_id, context.task_id,
            "action_verified" if definition.mutating and result["status"] == "verified" else "tool_result",
            {"tool": name, "result": result})
        return result


def default_registry(store, get_config, *, health_provider=None, logs_provider=None, verify_provider=None):
    registry = ToolRegistry(store, redactor=lambda value: redact(value, _secrets(get_config())))

    def help_search(arguments, context):
        query = arguments["query"]
        guides = [guide for guide in GUIDES if query in guide["title"] or any(word in query for word in (
            "使用", "怎么", "发单", "发送", "规则", "收录", "本地")) and guide["id"] == "usage"]
        if not guides:
            guides = GUIDES
        return tool_result(data={"guides": guides}, evidence_refs=["bundled-guide:2026-10-27"])

    def health_inspect(arguments, context):
        if health_provider:
            data = health_provider()
        else:
            from app.net import pull_state
            cfg = get_config()
            ds = cfg.get("deepseek") or {}
            data = {"target": cfg.get("target", "test"), "configured_model": ds.get("model"),
                    "model_configured": bool(ds.get("api_key") and ds.get("base_url") and ds.get("model")),
                    "pull_has_result": pull_state.has_result(), "whitelist_blocked": pull_state.is_whitelist_blocked(),
                    "wecom_error_code": pull_state.last_error_code(),
                    "notice": "内存拉取状态；未据此声称已检查网络、余额或历史批次"}
        return tool_result(data=data, evidence_refs=["local-runtime:snapshot"])

    def health_verify(arguments, context):
        component = arguments.get("component", "deepseek_balance")
        if verify_provider and component == "wecom":
            return verify_provider(component, context)
        if component != "deepseek_balance":
            return _error("manual_account_step", "请由账号管理员在IP白名单页完成设置后点“我已更新”，本工具没有伪造试拉成功。", "needs_input")
        ds = get_config().get("deepseek") or {}
        if not ds.get("api_key") or not ds.get("base_url"):
            return _error("not_configured", "尚未配置模型账号，充值指引仍可查看", "needs_input")
        remaining = min(10, max(0.01, context.deadline - time.time())) if context.deadline else 10
        try:
            with httpx.Client(trust_env=False, timeout=remaining, follow_redirects=False) as client:
                response = client.get(ds["base_url"].rstrip("/") + "/user/balance",
                                      headers={"Authorization": "Bearer " + ds["api_key"]})
            if response.status_code >= 400:
                failure = classify_failure(http_status=response.status_code)
                return _error(failure["category"], failure["message"])
            body = response.json()
            balances = [{"currency": item.get("currency"), "total_balance": item.get("total_balance")}
                        for item in body.get("balance_infos", []) if item.get("currency") in {"CNY", "USD"}]
            return tool_result(data={"is_available": body.get("is_available"), "balances": balances}, evidence_refs=["deepseek:user/balance"])
        except httpx.RequestError:
            failure = classify_failure(network_error="request")
            return _error("network", failure["message"])
        except (ValueError, TypeError, AttributeError):
            return _error("invalid_response", "余额接口未返回可核对的数据")

    def logs_query(arguments, context):
        if logs_provider:
            lines = list(logs_provider())[-200:]
        else:
            from app.logbus import BUS
            lines = list(BUS.history)[-200:]
        contains = arguments.get("contains", "")
        selected = [str(line)[:2000] for line in lines if contains in str(line)][-arguments.get("limit", 50):]
        return tool_result(data={"lines": redact(selected, _secrets(get_config()), phones=True), "window": "recent_200"},
                           evidence_refs=["local-logbus:recent-200"])

    registry.register("help_search", "查询随软件提供的功能、收录规则及账号操作说明；文档内容不是执行授权。",
        {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}}, "required": ["query"], "additionalProperties": False}, help_search)
    registry.register("health_inspect", "只读本机结构化运行状态，不发告警、不拉取订单、不改配置。",
        {"type": "object", "properties": {}, "additionalProperties": False}, health_inspect)
    registry.register("health_verify", "老板完成账号操作后检查一次：模型余额，或提供可信IP现有后台验证入口。",
        {"type": "object", "properties": {"component": {"type": "string", "enum": ["deepseek_balance", "wecom"]}}, "additionalProperties": False}, health_verify)
    registry.register("logs_query", "只读最近200条内存日志，按文字过滤并脱敏，最多返回100条。",
        {"type": "object", "properties": {"contains": {"type": "string", "maxLength": 80}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}, logs_query)
    return registry


def _secrets(cfg):
    from .security import config_secrets
    return config_secrets(cfg)
