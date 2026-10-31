"""R27：只在已核实的原范围改值，保留旧订单直到新值回读成功。

调用方负责批次串行锁和配置目标授权；本层再次检查原done凭据、固定目标、
冻结批次、单群来源与时间顺序。恢复意图放在原checkpoint中，不新增业务表。
"""
from __future__ import annotations

from copy import deepcopy
import json

from sqlalchemy.orm import object_session

from app.feishu.sheet_writer import HEADER, WriteRecoveryError, _digest, _validate_rows
from app.models import Batch, Message
from app.pipeline.source_group_guard import single_room, source_ids


def _lineage(data):
    """返回原消息和已经确认的动作链来源，拒绝不完整历史。"""
    ids = set(source_ids(data))
    history = data.get('amendment_history', [])
    if not isinstance(history, list):
        raise ValueError('原单修改历史无效')
    for entry in history:
        if not isinstance(entry, dict):
            raise ValueError('原单修改历史无效')
        ids.update(source_ids(entry))
    return ids


def _validate_sources(session, order, before, after, amendment):
    from app.pipeline.history_ops import HELD_BATCHES
    origin = session.get(Batch, order.batch_id)
    if order.batch_id in HELD_BATCHES or origin is None or origin.status != 'done':
        raise ValueError('改补取消指向冻结或未完成批次，禁止修改原单')
    if before.get('source_msgids_inferred') or after.get('source_msgids_inferred'):
        raise ValueError('原单来源为推断值，不能自动改单')
    if after.get('source_msgids') != before.get('source_msgids'):
        raise ValueError('改单不能改变原单来源日期')
    kind = amendment['kind']
    if kind not in ('replace', 'merge', 'void'):
        raise ValueError('改单类型无效')
    if before.get('voided') and kind != 'void':
        raise ValueError('原单已取消，不能自动恢复')
    if kind == 'void' and after.get('voided') is not True:
        raise ValueError('取消缺少明确作废标记')
    prior = before.get('amendment_history', [])
    history = after.get('amendment_history')
    if (not isinstance(history, list) or len(history) != len(prior) + 1
            or history[:-1] != prior or not isinstance(history[-1], dict)
            or history[-1].get('kind') != kind
            or history[-1].get('source_msgids') != amendment['source_msgids']):
        raise ValueError('改单历史与本次来源不一致')
    originals = _lineage(before)
    changes = source_ids(amendment)
    if changes & originals:
        raise ValueError('改单来源已属于原单或已处理动作')
    all_ids = originals | changes
    messages = {mid: session.get(Message, mid) for mid in all_ids}
    single_room({'source_msgids': sorted(all_ids)}, messages)
    if any(m.revoked or type(m.msgtime) is not int or m.msgtime <= 0 for m in messages.values()):
        raise ValueError('改单引用已撤回或时间无效的消息')
    if min(messages[mid].msgtime for mid in changes) < max(messages[mid].msgtime for mid in originals):
        raise ValueError('改单消息早于原单或已处理动作')


def _read_range(writer, checkpoint):
    """空白仅用于补齐响应尾部，整片空区不被当作可覆盖凭据。"""
    try:
        response = writer.fs.req(
            'GET', '/sheets/v2/spreadsheets/' + checkpoint['spreadsheet_token']
            + '/values/' + checkpoint['range'])
        values = response['valueRange']['values']
        height = len(checkpoint['payload'])
        if not isinstance(values, list) or len(values) > height:
            raise ValueError('返回行数无效')
        rows = []
        for row in values:
            if not isinstance(row, list) or len(row) > len(HEADER):
                raise ValueError('返回单元格无效')
            rows.append(['' if value is None else value for value in row]
                        + [''] * (len(HEADER) - len(row)))
        rows.extend([[''] * len(HEADER) for _ in range(height - len(rows))])
        return rows
    except Exception as exc:
        raise WriteRecoveryError('改单无法核实原范围，保留冻结凭据') from exc


def apply_amendment(writer, order, amendment):
    """返回更新后的done检查点；不建表、不追加、不格式化、不发通知。

    amendment必含kind/source_msgids/before_data/after_data；可选before_checkpoint。
    Order必须属于调用方原会话，且仍保持before_data。PUT中断后保留旧data_json
    和amendment_pending；重试必须提交相同动作，绝不以新解析结果替换冻结载荷。
    """
    session = object_session(order)
    if session is None:
        raise WriteRecoveryError('改单缺少原数据库会话，拒绝写入')
    try:
        before, checkpoint, _ = writer._checkpoint(order)
        if order.status != 'written' or checkpoint.get('state') != 'done':
            raise ValueError('原单尚无已完成写表凭据')
        original = deepcopy(checkpoint)
        pending = original.pop('amendment_pending', None)
        if writer.fixed_target and tuple(writer.fixed_target) != (
                original['spreadsheet_token'], original['sheet_id']):
            raise ValueError('改单目标与原写表凭据不一致')
        if not isinstance(amendment, dict) or before != amendment['before_data']:
            raise ValueError('原单已变化，不能覆盖其它改动')
        if ('before_checkpoint' in amendment
                and amendment['before_checkpoint'] != original):
            raise ValueError('原单写表凭据已变化')
        after = deepcopy(amendment['after_data'])
        if not isinstance(after, dict):
            raise ValueError('改单输出无效')
        after['amendment_row_capacity'] = len(original['payload'])
        _validate_sources(session, order, before, after, amendment)
        from app.pipeline.logistics_notes import collect_notes
        collect_notes(after)
        payload = writer.order_to_rows(after, original['seq'])
        _validate_rows(payload, original['rows'][0])
        if len(payload) != len(original['payload']):
            raise ValueError('改单输出超出原物理行范围')
        frozen = {
            'kind': amendment['kind'], 'source_msgids': deepcopy(amendment['source_msgids']),
            'before_data': before, 'after_data': after,
            'before_checkpoint': original, 'after_payload': payload,
        }
        frozen['digest'] = _digest(frozen)
        if pending is not None and pending != frozen:
            raise ValueError('重试与已冻结改单凭据不一致')
    except (KeyError, TypeError, ValueError) as exc:
        raise WriteRecoveryError(f'改单安全校验失败：{exc}') from exc

    if pending is None:
        checkpoint['amendment_pending'] = frozen
        order.write_checkpoint_json = json.dumps(checkpoint, ensure_ascii=False)
        session.commit()
    actual = _read_range(writer, original)
    if actual != payload:
        if actual != original['payload']:
            raise WriteRecoveryError('原范围既非原订单也非冻结改后结果，拒绝覆盖或新增行')
        writer._put_order_values(original['spreadsheet_token'], original['range'], payload)
    if _read_range(writer, original) != payload:
        raise WriteRecoveryError('改单写后回读不一致，保留原单和冻结凭据')

    final = deepcopy(original)
    final.update(payload=payload, payload_digest=_digest(payload), data_digest=_digest(after))
    order.data_json = json.dumps(after, ensure_ascii=False)
    order.write_checkpoint_json = json.dumps(final, ensure_ascii=False)
    order.reasons_json = json.dumps(after.get('review_reasons') or [], ensure_ascii=False)
    session.commit()
    return final
