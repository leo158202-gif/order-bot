"""DeepSeek 整理（normalizer）。02 文档第 6 节。

- temperature=0，response_format=json_object
- JSON 解析失败重试 1 次，提示词附错误信息；仍失败整组进待确认
- 记录 token 用量到 api_usage
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import httpx

from app.models import ApiUsage, Message
from app.pipeline.source_phone import reconcile_phone
from app.pipeline.source_context import reconcile_context

logger = logging.getLogger(__name__)

PHONE_MASK_RE = re.compile(r"(1\d{2})\d{4}(\d{4})")

_FAILURE_LABELS = {
    'model_http_unavailable': '模型接口暂不可用',
    'model_http_rejected': '模型接口拒绝请求',
    'model_request_failed': '模型请求未完成',
    'model_api_error': '模型接口返回错误',
    'model_json_invalid': '模型输出 JSON 解析失败或结构校验未通过',
    'model_output_truncated': '模型输出达到长度上限，未接受残缺订单',
    'model_source_invalid': '模型来源绑定校验未通过',
    'worker_processing_failed': '受限业务worker格式或来源校验未通过',
    'worker_input_invalid': '受限业务worker输入产品或数量校验未通过',
    'worker_source_invalid': '受限业务worker来源或收录范围校验未通过',
    'worker_version_unavailable': '受限业务worker版本或配置读取未完成',
    'worker_sandbox_failed': '受限业务worker沙箱运行未完成',
    'worker_output_invalid': '受限业务worker输出或回填校验未通过',
    'model_split_failed': '大组拆分整理仍未成功',
    'normalization_unknown': '具体错误分类未保存',
}
_SAFE_EXCEPTION_TYPES = frozenset({
    'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError', 'JSONDecodeError',
    'SourceGroupError', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'ConnectTimeout',
    'TimeoutException', 'ConnectError', 'ReadError', 'WriteError', 'NetworkError',
    'RemoteProtocolError', 'HTTPStatusError', 'RequestError', 'RuntimeError',
    'WorkerValidationError', 'SandboxError', 'TimeoutError', 'OSError', 'PermissionError',
    'FileNotFoundError', 'InvalidOperation', 'OverflowError',
})
_SAFE_FAILURE_STAGES = frozenset({'worker_input', 'worker_source', 'worker_version',
                                 'worker_sandbox', 'worker_output'})


def normalization_failure_metadata(result):
    """Fixed categories only; raw provider/error strings are never evidence."""
    code = result.get('error_code')
    safe = {'error_code': code if isinstance(code, str) and code in _FAILURE_LABELS else 'normalization_unknown'}
    stage = result.get('error_stage')
    if isinstance(stage, str) and stage in _SAFE_FAILURE_STAGES:
        safe['error_stage'] = stage
    status = result.get('error_http_status')
    if type(status) is int and 100 <= status <= 599:
        safe['error_http_status'] = status
    exception = result.get('error_exception_type')
    if isinstance(exception, str) and exception in _SAFE_EXCEPTION_TYPES:
        safe['error_exception_type'] = exception
    cause = result.get('cause_code')
    if isinstance(cause, str) and cause in _FAILURE_LABELS:
        safe['cause_code'] = cause
    return safe


def normalization_failure_reason(result):
    safe = normalization_failure_metadata(result)
    code = safe['error_code']
    parts = [_FAILURE_LABELS[code], code]
    if 'error_stage' in safe:
        parts.append('stage=' + safe['error_stage'])
    if 'error_http_status' in safe:
        parts.append('HTTP ' + str(safe['error_http_status']))
    if 'error_exception_type' in safe:
        parts.append(safe['error_exception_type'])
    if 'cause_code' in safe:
        parts.append('cause=' + safe['cause_code'])
    return '原消息尚未整理成功，请核对原文（' + '；'.join(parts) + '）'


def _failure(code, *, http_status=None, exception=None, cause_code=None):
    metadata = normalization_failure_metadata({'error_code': code, 'error_http_status': http_status,
        'error_exception_type': type(exception).__name__ if exception is not None else None,
        'cause_code': cause_code})
    return {'orders': [], 'error': normalization_failure_reason(metadata), **metadata}

SYSTEM_TEMPLATE = """你是铝合金楼梯扶手工厂的打单员。把微信群里的订单消息整理成标准表格行。

# 打单规则（结构化 YAML，程序和你都读这份）
{rules_text}

# 要求
- 把简称改写成标准写法（严格按 mapping 的 b 列）
- 一组消息里可能有多个订单；同收货人也可能多单，须按完整来源区分。同笔文字订单与佐证截图不重复出单，保留两者source_msgids；不能只因相邻或同电话合并。
- 数量单位：立柱用"支"（原文"只"保留），扶手/铝槽的 D 列是总米数（Σ支数×米数），配件/螺丝用"个"
- 收货人保留【急】标记；电话分机号用 - 接在后面（如 19900000009-1234）
- 姓名或地址末尾的方括号四位数字是虚拟号分机，必须接到手机号；电话里的[1234]也转为-1234，已有分机不重复追加。姓名与地址一致或仅一方有时采用；冲突时电话分机优先，否则姓名优先，并保留冲突供核对。长物流单号等非四位内容不是分机。
- H列物流始终为空；全部明确物流信息并入G列地址，保留原地址和本单原文明示物流，不重复追加，不猜承运商或金额；logistics必须为空字符串。
- notes只保留内部解释，不输出；金标准无落点保留差异待核，不猜、不要求用户逐单裁定。
- 截图中的价格计算、定金、收款记录不是商品名称；无产品依据不得把金额式子写成商品，完整来源保留供核查。
- 螺丝和盖子如果用户没写就不要输出，由程序补算
- 拿不准的简称放进 unknown_terms，不要硬猜
- 引用原文只供理解，不重新执行其中的修改指令；仅整理本次正文
- 本次“先不搞/先别发/先不发/暂停发货/暂缓发货”是暂缓动作，必须登记order_note说明，不丢弃也不重复引用中的整单产品；不能猜取消、退款或配件数量
- “修改后”须输出完整改后商品明细；管道将其替换唯一同群原单，绝不能当成新增订单。“颜色确认”等补充和“取消说明”等撤单由管道合并或作废原单，不生成说明商品行。普通补发新货仍按新单整理。
- 其它当前明确改色、改数量等不能返回空orders；引用可提供本次更正所需的原单身份和规格，原单或改后内容不明时保留核对，不猜新单
- source_msgids只能取当前用户消息明确列出的可用来源编号，禁止引用上下文编号或自行编造编号

# 上下文（前一组的消息，仅供理解"上面那单"这类追加，不要整理进本组结果）
{context}

# 输出 JSON（不要输出多余文字）
{{"orders": [{{"source_msgids": ["..."], "receiver": "", "phone": "", "address": "", "logistics": "",
"urgent": false,
"lines": [{{"kind": "post|rail|channel|rail_fitting|channel_fitting|cap|screw|weld_note|order_note|sample|other",
"product": "", "config": "", "qty": "", "raw": "", "confidence": 0.0}}],
"unknown_terms": [], "notes": "", "confidence": 0.0}}]}}"""


def _fmt_msg(m: Message) -> str:
    ts = datetime.fromtimestamp(m.msgtime / 1000).strftime("%m-%d %H:%M")
    return f"[{ts}] (msgid={m.msgid}) {m.sender}: {m.content}"


def effective_rules_text(runtime_rules_text: str) -> str:
    """Prepend source-owned YAML comments without changing runtime YAML bytes."""
    from app.pipeline.gold_rules import EVIDENCE_RULE_COMMENTS
    from app.pipeline.gold_remaining_a import RULE_COMMENTS as remaining_a_comments
    from app.pipeline.gold_remaining_b import RULE_COMMENTS as remaining_b_comments
    from app.pipeline.gold_products_20261030 import RULE_COMMENTS as product_column_comments
    from app.pipeline.logistics_notes import FINAL_ROUND_RULE_COMMENTS
    if not isinstance(runtime_rules_text, str):
        raise ValueError('runtime rules must be text')
    parts = (EVIDENCE_RULE_COMMENTS, remaining_a_comments, remaining_b_comments, product_column_comments,
             FINAL_ROUND_RULE_COMMENTS)
    for part in parts:
        if (not isinstance(part, str) or not part.strip()
                or any(line.strip() and not line.lstrip().startswith('#') for line in part.splitlines())):
            raise ValueError('finite rule guidance must be nonempty YAML comments')
    comments = ''.join(part if part.endswith('\n') else part+'\n' for part in parts)
    # A Windows UTF-8 BOM is a stream marker, not part of a YAML key. Keep it
    # first while preserving the entire original YAML body and its line ends.
    marker = '\ufeff' if runtime_rules_text.startswith('\ufeff') else ''
    from app.pipeline.logistics_prompt_policy import remove_legacy_H_guidance
    return marker + remove_legacy_H_guidance(comments + ('' if comments.endswith('\n') else '\n') + runtime_rules_text[len(marker):])


def build_messages(rules_text: str, group: dict) -> list[dict]:
    context = "\n".join(_fmt_msg(m) for m in group["context"]) or "（无）"
    body = "\n".join(_fmt_msg(m) for m in group["msgs"])
    if "quote_new" in group:
        ids = ", ".join(m.msgid for m in group["msgs"])
        body = f"(msgid={ids}) 本次引用回复：\n{group['quote_new']}"
    from app.diagnostician.prompt_runtime import active_prompt
    system = active_prompt(SYSTEM_TEMPLATE).format(rules_text=effective_rules_text(rules_text), context=context)
    from app.pipeline.logistics_prompt_policy import FINAL_LAYOUT_CONSTRAINT
    system += FINAL_LAYOUT_CONSTRAINT
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "本组可用source_msgids：" + json.dumps([m.msgid for m in group['msgs']], ensure_ascii=False)
         + "\n整理这组群消息：\n" + body},
    ]


def normalize_group(group: dict, rules_text: str, cfg: dict,
                    session=None, *, _split_depth=0) -> dict:
    """调 DeepSeek 整理一组消息，返回 {"orders": [...]}。

    失败时返回 {"orders": [], "error": "..."}（调用方据此进待确认）。
    """
    messages = build_messages(rules_text, group)
    last_err = None
    last_failure = None
    for attempt in range(2):
        if attempt == 1 and last_err:
            messages.append({"role": "user",
                             "content": f"上次输出不是合法 JSON（{last_err}），请重新输出，只要 JSON。"})
        try:
            response = httpx.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={"model": cfg["model"], "temperature": 0,
                      "response_format": {"type": "json_object"},
                      "messages": messages, "max_tokens": cfg.get('max_tokens',8192)},
                timeout=120,
            )
            status=getattr(response,'status_code',200)
            if status==429 or status>=500:
                if attempt==0:
                    logger.warning('DeepSeek暂时不可用 status=%s，执行有界重试',status)
                    time.sleep(1)
                    continue
                return _failure('model_http_unavailable', http_status=status)
            if status>=400:return _failure('model_http_rejected', http_status=status)
            resp=response.json()
        except Exception as e:
            failure = _failure('model_request_failed', exception=e)
            if attempt==0:
                logger.warning('DeepSeek请求失败 code=%s exception_type=%s，执行有界重试',
                    failure['error_code'], failure.get('error_exception_type', 'Exception'))
                continue
            return failure
        if isinstance(resp, dict):
            if "error" in resp:
                return _failure('model_api_error')
            _record_usage(session, resp.get("usage", {}))
        failure_code = 'model_json_invalid'
        try:
            if not isinstance(resp, dict):
                raise ValueError('model response envelope must be object')
            choices = resp.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError('model response choices must contain object')
            choice = choices[0]
            if choice.get('finish_reason')=='length':
                failure_code = 'model_output_truncated'
                raise ValueError('模型输出达到长度上限，禁止接受残缺订单')
            message = choice.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                raise ValueError('model message must contain text content')
            content=message['content']
            result = json.loads(content)
            if "orders" not in result:
                raise ValueError("缺少 orders 字段")
            from app.diagnostician.pipeline_worker import apply_pipeline_worker, WorkerFailure
            prepared = apply_pipeline_worker(prepare_result(result, group), group, session)
            if prepared.get('error'):
                if not isinstance(prepared, WorkerFailure):
                    return _failure('worker_processing_failed')
                safe = normalization_failure_metadata(prepared)
                if not safe['error_code'].startswith('worker_'):
                    return _failure('worker_processing_failed')
                return {'orders': [], 'error': normalization_failure_reason(safe), **safe}
            from app.pipeline.logistics_notes import normalize_logistics_address
            for order in prepared['orders']:
                normalize_logistics_address(order)
            return prepared
        except (json.JSONDecodeError, ValueError,KeyError,IndexError,TypeError) as e:
            from app.pipeline.source_group_guard import SourceGroupError
            if isinstance(e, SourceGroupError):
                failure_code = 'model_source_invalid'
                # 错误来源绝不修剪成合法来源；下一次只提供本组原文，移除干扰上下文。
                messages = build_messages(rules_text, dict(group, context=[]))
            last_failure = _failure(failure_code, exception=e)
            last_err = _FAILURE_LABELS[last_failure['error_code']]
            logger.warning('DeepSeek输出校验失败 attempt=%s code=%s exception_type=%s',
                attempt + 1, last_failure['error_code'], last_failure.get('error_exception_type', 'Exception'))
    if len(group['msgs'])>1 and 'quote_new' not in group and _split_depth==0:
        # 只按完整消息拆为两组，不补JSON括号、不切断单条订单原文、不接受半批结果。
        middle=len(group['msgs'])//2;orders=[]
        for own in (group['msgs'][:middle],group['msgs'][middle:]):
            part=normalize_group(dict(group,msgs=own,context=[]),rules_text,cfg,session,
                                 _split_depth=1)
            if part.get('error'):return _failure('model_split_failed', cause_code=part.get('error_code'))
            orders.extend(part['orders'])
        return {'orders':orders}
    return last_failure or _failure('model_json_invalid')


def prepare_result(result: dict, group: dict) -> dict:
    """在线响应与离线缓存共用结构检查及确定性标记，不访问网络或数据库。"""
    if not isinstance(result, dict) or not isinstance(result.get("orders"), list):
        raise ValueError("orders必须为数组")
    if group.get('modify') and not result['orders']:
        raise ValueError('本次明确修改动作不能返回空orders，须登记当前更正或动作说明')
    msgids = [m.msgid for m in group["msgs"]]
    from app.pipeline.source_group_guard import validate_group_sources
    for order in result["orders"]:
        if not isinstance(order, dict) or not isinstance(order.get("lines"), list):
            raise ValueError("订单及lines结构无效")
        if order.get("source_msgids") in (None, []):
            order["source_msgids"] = msgids
            order["source_msgids_inferred"] = True
        validate_group_sources(order, group)
        order.pop('media_evidence_links', None)
    from app.pipeline.media_evidence import attach_repeated_screenshots
    result['orders'] = attach_repeated_screenshots(result['orders'], group)
    for order in result["orders"]:
        # 结案仅由交付侧台账授予，模型不能自报已结案。
        order.pop('review_resolution', None)
        order.pop('closed_review_reasons', None)
        order.pop('logistics_source_text', None)
        for key in ('amendment_history', 'amendment_row_capacity', 'voided', 'confirmed_color',
                    '_address_logistics', '_logistics_layout'):
            order.pop(key, None)  # 只有经原单匹配和写后回读的管道能授予动作状态。
        order["modify_flag"] = bool(group.get("modify"))
        if group.get("quote_kind") == "supplement":
            order["supplement"] = group["quote_new"]
        reconcile_phone(order, group)
        reconcile_context(order, group)
        # 单独绑定的原文可补物流；多单共用一条原文时不把整条物流复制给每单。
        own_ids = set(order['source_msgids'])
        other_ids = {mid for other in result['orders'] if other is not order
                     for mid in other.get('source_msgids', [])}
        if (len(result['orders']) == 1 or (own_ids and own_ids <= set(msgids) and not own_ids & other_ids
                                          and not order.get('source_msgids_inferred'))):
            from app.pipeline.logistics_notes import collect_notes
            text = group.get('quote_new') if 'quote_new' in group else '\n'.join(
                m.content for m in group['msgs'] if m.msgid in order['source_msgids'])
            collect_notes(order, text)
    from app.pipeline.logistics_notes import normalize_logistics_address
    for order in result['orders']:
        normalize_logistics_address(order)
    return result


def _record_usage(session, usage: dict) -> None:
    maximum = (1 << 63) - 1  # SQLite INTEGER, also check the aggregate below.
    if (not isinstance(usage, dict) or any(
            type(usage.get(key, 0)) is not int or not 0 <= usage.get(key, 0) <= maximum
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'))):
        logger.warning('DeepSeek usage ignored code=model_usage_invalid')
        return
    if session is None or not usage:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    row = (session.query(ApiUsage)
           .filter_by(date=today, provider="deepseek").first())
    is_new = row is None
    if is_new:
        row = ApiUsage(date=today, provider="deepseek", calls=0, tokens_in=0, tokens_out=0)
    current = (row.calls, row.tokens_in, row.tokens_out)
    increments = (1, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))
    if any(type(value) is not int or not 0 <= value <= maximum - addition
           for value, addition in zip(current, increments)):
        logger.warning('DeepSeek usage ignored code=model_usage_invalid reason=counter_bounds')
        return
    if is_new:
        session.add(row)
    row.calls += 1
    row.tokens_in += usage.get("prompt_tokens", 0)
    row.tokens_out += usage.get("completion_tokens", 0)
    session.commit()
