"""Session-scoped facade and single diagnostic worker for web/MCP callers."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
import uuid

from .guides import GUIDES
from .model_client import ModelClient
from .runner import DiagnosticRunner
from .security import config_secrets, redact
from .tools import default_registry


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_ACTIVE = {"queued", "running"}


class DiagnosticianService:
    def __init__(self, store, *, get_config, upload_root, model_client=None, registry=None,
                 auto_start=True, busy_provider=None, health_provider=None, logs_provider=None,
                 verify_provider=None, context_factory=None):
        self.store, self.get_config = store, get_config
        self.upload_root = Path(upload_root).resolve()
        self.registry = registry or default_registry(store, get_config, health_provider=health_provider,
                                                     logs_provider=logs_provider, verify_provider=verify_provider)
        self.model_client = model_client or ModelClient(get_config)
        self.runner = DiagnosticRunner(store, self.registry, self.model_client, get_config, context_factory=context_factory)
        self.busy_provider = busy_provider or (lambda: False)
        self.auto_start = auto_start
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.RLock()
        self._thread = None
        self.capabilities = {"chat": True, "upload": True, "corrections": False, "code_execution": False}

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.recover_pending()
            self._thread = threading.Thread(target=self._worker, name="diagnostician", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _enqueue(self, session_id, task_id):
        with self._lock:
            if task_id not in self._queued:
                self._queued.add(task_id)
                self._queue.put((session_id, task_id))

    def _worker(self):
        while not self._stop.is_set():
            try:
                session_id, task_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                task = self.store.get_task(session_id, task_id)
                if task and task["status"] == "queued" and self.busy_provider():
                    self._queue.put((session_id, task_id))
                    self._stop.wait(0.2)
                    continue
                self.run_task(session_id, task_id)
                current = self.store.get_task(session_id, task_id)
                if current and current["status"] == "queued":
                    self._queue.put((session_id, task_id))
                    self._stop.wait(0.2)
            except Exception:
                current = self.store.get_task(session_id, task_id)
                if current and current['status'] in _ACTIVE:
                    self.store.update_task(session_id, task_id, status='failed', phase='failed',
                        result={'content': '执行中断，已保留任务凭据，未自动重复外部动作。'}, error='worker_interrupted')
            finally:
                current = self.store.get_task(session_id, task_id)
                if not current or current["status"] != "queued":
                    with self._lock:
                        self._queued.discard(task_id)
                self._queue.task_done()

    def recover_pending(self):
        """Never replay a running external operation after a crash."""
        for task in self.store.list_pending_tasks():
            if task['kind'] == 'gold_repair_child':
                continue  # Frozen repair plans own child recovery, never the model.
            if task["status"] == "running":
                text = "上次诊断中断，已保留阶段及操作凭据；未自动重复模型或外部动作。"
                self.store.append_message(task["session_id"], task["conversation_id"], "assistant", text,
                                          request_id="interrupted:" + task["id"])
                self.store.update_task(task["session_id"], task["id"], status="partial", phase="partial",
                                       result={"content": text}, error="interrupted_operation")
            elif self.auto_start:
                self._enqueue(task["session_id"], task["id"])

    def _conversation(self, session_id):
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 512:
            raise ValueError("会话标识无效")
        with self._lock:
            saved = self.store.get_record(session_id, "session", "conversation")
            if saved:
                return self.store.ensure_conversation(session_id, saved["payload"]["id"])
            conversation = self.store.ensure_conversation(session_id)
            try:
                self.store.put_record(session_id, "session", "conversation", {"id": conversation["id"]}, expected_revision=0)
            except ValueError:
                saved = self.store.get_record(session_id, "session", "conversation")
                return self.store.ensure_conversation(session_id, saved["payload"]["id"])
            return conversation

    def session_snapshot(self, session_id):
        conversation = self._conversation(session_id)
        tasks = self.store.list_tasks(session_id, conversation["id"])
        mapping = {row["request_id"]: row["id"] for row in tasks}
        messages = []
        for message in self.store.list_messages(session_id, conversation["id"]):
            identity = message.get("request_id") or ""
            task_id = identity.split(":", 1)[1] if identity.startswith(("assistant:", "interrupted:")) else mapping.get(identity)
            messages.append({"id": message["id"], "role": message["role"], "content": self._redact(message["content"]),
                             "created_at": message["created_at"], "task_id": task_id})
        return {"session_id": session_id, "messages": messages,
                "tasks": [self.get_task(session_id, task["id"], include_messages=False) for task in tasks],
                "capabilities": dict(self.capabilities),
                "guides": GUIDES}

    def submit_chat(self, session_id, request_id, message, attachment_ids=None):
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 6000:
            raise ValueError("请输入1到6000字的问题")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 512:
            raise ValueError("请求标识无效")
        attachment_ids = attachment_ids or []
        if not isinstance(attachment_ids, list) or len(attachment_ids) > 10 or any(not isinstance(identity, str) for identity in attachment_ids):
            raise ValueError("每次最多附带10个已上传文件")
        conversation = self._conversation(session_id)
        clean = self._redact(message.strip())
        with self._lock:
            previous = self.store.get_record(session_id, "chat_requests", request_id)
            if previous:
                if previous["payload"]["message"] != clean or previous["payload"]["provided_attachment_ids"] != attachment_ids:
                    raise ValueError("task request_id already has different immutable input")
                return self.get_task(session_id, previous["payload"]["task_id"])
            saved_selection = self.store.get_record(session_id, "session", "selected_attachments")
            selected_ids = attachment_ids or (saved_selection["payload"]["ids"] if saved_selection else [])
            attachments = [{"attachment_id": item["attachment_id"], "filename": item["filename"]}
                           for item in (self.attachment_for(session_id, identity) for identity in selected_ids)]
            task = self.store.create_task(session_id, conversation["id"], request_id,
                payload={"message": clean, "attachments": attachments, "attachment_ids": selected_ids})
            self.store.put_record(session_id, "chat_requests", request_id,
                {"task_id": task["id"], "message": clean, "provided_attachment_ids": attachment_ids},
                task_id=task["id"], expected_revision=0)
            if selected_ids:
                self.store.put_record(session_id, "session", "selected_attachments", {"ids": selected_ids})
            self.store.append_message(session_id, conversation["id"], "user", clean, request_id=request_id)
            if self.auto_start and task["status"] == "queued":
                if not self._thread or not self._thread.is_alive():
                    self.start()
                self._enqueue(session_id, task["id"])
        return self.get_task(session_id, task["id"])

    def run_task(self, session_id, task_id):
        """Worker entry, also usable by a headless local host. Never HTTP callable."""
        task = self.store.get_task(session_id, task_id)
        if not task or task['kind'] == 'gold_repair_child' or task['status'] != 'queued':
            return
        if not self.busy_provider():
            from app.maintenance import activity, MaintenanceDenied
            try:
                with activity("web"):
                    repairs = getattr(self, 'repair_queue', None)
                    if repairs and task['kind'] == 'gold_repair_step':
                        repairs.run(task)
                    elif task['kind'] == 'repair_learning' and getattr(self, 'learning_queue', None):
                        self.learning_queue.run(task)
                    else:
                        gate = getattr(self, 'comparison_gate', None)
                        message = task['payload'].get('message', '')
                        from .contracts import ToolContext
                        preflight = ToolContext(session_id, task_id, task['request_id'], deadline=time.time() + 120)
                        only_compare = bool(re.search(r'对照|核对|匹配|复核', message)) and not re.search(r'修正|修改|更新|纠正|代码|脚本|提示词|规则|以.+为准', message)
                        if gate and only_compare and gate(session_id, task['payload'].get('attachment_ids', []), context=preflight):
                            content = '这份相同证据已达到核查停点，未再次调用模型或外部接口。未匹配项继续保留备注；新增金标或已证来源变化时只核受影响项。'
                            self.store.append_message(session_id, task['conversation_id'], 'assistant', content,
                                                      request_id='assistant:' + task_id)
                            self.store.update_task(session_id, task_id, status='parked', phase='parked', result={'content': content})
                        else:
                            self.runner.run(session_id, task_id)
                        if repairs:
                            repairs.after_chat(self.store.get_task(session_id, task_id))
            except MaintenanceDenied:
                return

    def get_task(self, session_id, task_id, *, include_messages=True):
        task = self.store.get_task(session_id, task_id)
        if task is None:
            raise KeyError("任务不属于当前会话或不存在")
        events = []
        for event in self.store.list_events(session_id, task_id):
            payload = self._redact(event["payload"])
            events.append({"id": event["id"], "kind": event["kind"],
                           "text": self._event_text(event["kind"], payload), "data": payload,
                           "created_at": event["created_at"]})
        snapshot = {"task_id": task_id, "status": task["status"], "phase": self.store.task_progress(session_id, task_id)["phase"],
                    "events": events, "result": self._redact(task["result"]),
                    "error": {"code": self._redact(task["error"]), "message": "任务未全部完成，详情见结果"} if task["error"] else None,
                    "can_cancel": task["status"] in _ACTIVE}
        if include_messages:
            rows = self.store.list_messages(session_id, task["conversation_id"])
            snapshot["messages"] = [{"id": row["id"], "role": row["role"], "content": self._redact(row["content"]),
                                     "created_at": row["created_at"], "task_id": task_id if row.get("request_id") in {task["request_id"], "assistant:" + task_id, "interrupted:" + task_id} else None} for row in rows]
        return snapshot

    @staticmethod
    def _event_text(kind, payload):
        if kind == "tool_result":
            result = payload.get("result") or {}
            return payload.get("tool", "工具") + "：" + ((result.get("error") or {}).get("message") or result.get("status", "结果已记录"))
        return {"task.created": "任务已保存", "task.updated": "任务阶段已更新", "task.cancelled": "任务已停止",
                "budget.admitted": "本次调用已计入预算", "action_verified": "工具回读已确认操作结果",
                "record.updated": "恢复凭据已保存", "model_error": "模型请求未完成"}.get(kind, "事件已保存")

    def cancel_task(self, session_id, task_id, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 512:
            raise ValueError("请求标识无效")
        if self.store.get_task(session_id, task_id) is None:
            raise KeyError("任务不属于当前会话或不存在")
        self.store.cancel_task(session_id, task_id)
        return self.get_task(session_id, task_id)

    def _redact(self, value):
        return redact(value, config_secrets(self.get_config()))

    def save_upload(self, session_id, request_id, filename, content):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 512:
            raise ValueError("请求标识无效")
        if not isinstance(content, bytes) or not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("请上传不超过20MiB的非空文件")
        if not isinstance(filename, str) or len(filename) > 200 or any(ord(char) < 32 for char in filename) or "/" in filename or "\\" in filename:
            raise ValueError("文件名不正确")
        extension = Path(filename).suffix.lower()
        if extension not in {".xlsx", ".csv"}:
            raise ValueError("仅支持.xlsx或.csv；旧.xls请另存为.xlsx")
        if extension == ".xlsx" and not content.startswith(b"PK\x03\x04"):
            raise ValueError("文件内容不是.xlsx容器")
        if extension == ".csv" and (b"\x00" in content or content.startswith(b"PK")):
            raise ValueError("文件内容不是文本CSV")
        digest = hashlib.sha256(content).hexdigest()
        conversation = self._conversation(session_id)
        with self._lock:
            existing = self.store.get_record(session_id, "upload_requests", request_id)
            if existing:
                if existing["payload"]["sha256"] != digest or existing["payload"]["filename"] != filename:
                    raise ValueError("上传请求标识已绑定不同内容")
                return existing["payload"]["response"]
            task = self.store.create_task(session_id, conversation["id"], "upload:" + request_id,
                kind="upload", payload={"filename": filename, "sha256": digest})
            self.upload_root.mkdir(parents=True, exist_ok=True)
            if self.upload_root.is_symlink() or getattr(self.upload_root.stat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("上传目录不能是重解析链接")
            attachment_id = hashlib.sha256((session_id + ":" + request_id).encode()).hexdigest()
            destination = self.upload_root / (attachment_id + extension)
            metadata = {"attachment_id": attachment_id, "path": str(destination), "sha256": digest,
                        "filename": filename, "size": len(content)}
            saved_attachment = self.store.get_record(session_id, "attachments", attachment_id)
            if saved_attachment:
                recovered = self.attachment_for(session_id, attachment_id)
                if recovered["sha256"] != digest or recovered["filename"] != filename:
                    raise ValueError("已保存附件与本次请求不一致")
            else:
                temporary = self.upload_root / (uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                try:
                    self.store.put_record(session_id, "attachments", attachment_id, metadata,
                                          task_id=task["id"], expected_revision=0)
                except ValueError:
                    recovered = self.attachment_for(session_id, attachment_id)
                    if recovered["sha256"] != digest or recovered["filename"] != filename:
                        raise
            self.store.put_record(session_id, "session", "selected_attachments", {"ids": [attachment_id]})
            text = "文件已安全保存，尚未解析或改表。请在聊天中说明要对照的日期与范围。"
            self.store.update_task(session_id, task["id"], status="needs_input", phase="needs_input", result={"content": text, "attachment_id": attachment_id})
            self.store.append_message(session_id, conversation["id"], "assistant", text,
                                      request_id="assistant:" + task["id"])
            response = {"attachment_id": attachment_id, "task_id": task["id"], "status": "needs_input", "filename": filename,
                        "sha256": digest, "result": {"content": text}}
            self.store.put_record(session_id, "upload_requests", request_id,
                {"sha256": digest, "filename": filename, "response": response}, task_id=task["id"], expected_revision=0)
            return response

    def attachment_for(self, session_id, attachment_id):
        saved = self.store.get_record(session_id, "attachments", attachment_id)
        if not saved:
            raise PermissionError("附件不属于当前会话")
        value = saved["payload"]
        path = Path(value["path"])
        if path.is_symlink() or path.resolve().parent != self.upload_root or not path.is_file():
            raise ValueError("附件路径或文件状态不正确")
        content = path.read_bytes()
        if len(content) > MAX_UPLOAD_BYTES or hashlib.sha256(content).hexdigest() != value["sha256"]:
            raise ValueError("附件内容已变更，停止读取")
        return {**value, "path": path.resolve()}
