"""批次写完发飞书群消息。04 文档第 8 节格式。"""
from __future__ import annotations

import json
import logging

from app.feishu.client import Feishu

logger = logging.getLogger(__name__)


def _complete_sheet_url(sheet_url: str) -> str:
    """完整月表链接才保留；残缺/空白链接视为无链接（空批禁空白链接铁律）。"""
    url = (sheet_url or "").strip()
    if not url:
        return ""
    # 历史坏例：https://feishu.cn/sheets/?sheet= 或缺 token/sheet
    if "/sheets/?sheet=" in url or url.rstrip("/").endswith("/sheets"):
        return ""
    marker = "/sheets/"
    pos = url.find(marker)
    if pos < 0:
        return ""
    rest = url[pos + len(marker):]
    token, _, query = rest.partition("?")
    if not token.strip():
        return ""
    # sheet= 必须有非空值
    sheet_val = ""
    for part in query.split("&"):
        if part.startswith("sheet="):
            sheet_val = part[6:]
            break
    if not sheet_val.strip():
        return ""
    return url


def build_message(batch_kind: str, window_label: str, order_count: int,
                  pending_count: int, sheet_url: str, sheet_title: str,
                  row_from: int, row_to: int, resend: bool = False,
                  incomplete: bool = False, review_reasons=None,
                  added_count: int | None = None, corrected_count: int = 0) -> str:
    """仅输出四个数量与表格链接；保留旧调用参数以兼容冻结批次。"""
    if added_count is None:
        added_count = order_count - corrected_count
    if any(type(n) is not int or n < 0 for n in
           (order_count, pending_count, added_count, corrected_count)):
        raise ValueError('通知计数必须是非负整数')
    # R33: failed messages have no order row, but still count as pending review.
    if added_count + corrected_count != order_count:
        raise ValueError('通知计数不一致')
    sheet_url = _complete_sheet_url(sheet_url)
    if order_count == 0:
        # 空批：从未写过当日表→固定文案无链；已有当日完整链接→保留链接
        if not sheet_url:
            sheet_url = '本批无订单，未写入表格'
    elif not sheet_url:
        raise ValueError('有订单的通知缺少有效表格链接')
    lines = [f'订单数：{order_count}', f'待核数：{pending_count}',
             f'新增：{added_count}', f'修正：{corrected_count}', f'表格：{sheet_url}']
    return "\n".join(lines)


def summary_only(text: str, *, order_count: int | None = None,
                 pending_count: int | None = None, corrected_count: int | None = None) -> str:
    """Rebuild frozen counts/link without forwarding appended legacy details.

    Unknown formats require inspection. Batch manifests override R28 text counts
    that excluded failed messages; journals already freeze their own counts.
    """
    lines = text.splitlines()
    labels = ('订单数：', '待核数：', '新增：', '修正：', '表格：')
    if len(lines) < 5 or any(not line.startswith(label) for line, label in zip(lines, labels)):
        raise ValueError('冻结通知缺少五项汇总，拒绝转发明细')
    counts = []
    for line, label in zip(lines[:4], labels):
        value = line[len(label):]
        if not value.isascii() or not value.isdigit():
            raise ValueError('冻结通知计数不完整，拒绝转发明细')
        counts.append(int(value))
    count = counts[0] if order_count is None else order_count
    pending = counts[1] if pending_count is None else pending_count
    corrected = counts[3] if corrected_count is None else corrected_count
    return build_message('', '', count, pending, lines[4][len(labels[4]):], '', 0, 0,
                         added_count=counts[2] if order_count is None else count - corrected,
                         corrected_count=corrected)


def send_text(fs: Feishu, chat_id: str, text: str) -> None:
    fs.req("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"},
           json={"receive_id": chat_id, "msg_type": "text",
                 "content": json.dumps({"text": text}, ensure_ascii=False)})
    logger.info("已发飞书群消息（%d 字）", len(text))
