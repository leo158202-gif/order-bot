"""飞书电子表格写入。02 文档第 10 节，格式 04 文档。

- 每月一个表格（26年10月订单），每天一个工作表（10.20），映射缓存在 DB
- 每批追加在已有内容之后，序号从 1 开始
- 断点续写：记录每单已写的行，续跑跳过
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import time
from copy import deepcopy
from datetime import datetime

from sqlalchemy.orm import object_session

from app.db import get_session
from app.models import Order, State
from app.feishu.client import Feishu

logger = logging.getLogger(__name__)

HEADER = ["序号", "产品", "配置", "数量", "收货人", "电话", "地址", "物流", "待核"]
# 04 文档：列宽像素（Excel 字符宽 ×7.5）
COL_WIDTHS = {"B": 270, "C": 218, "E": 158, "G": 525, "H": 100}
HEADER_STYLE = {"backColor": "#4472C4", "foreColor": "#FFFFFF",
                "bold": True, "hAlign": 1, "vAlign": 1, "wordWrap": True}
DATA_STYLE = {"hAlign": 1, "vAlign": 1}


def sheet_url(ss_token: str, sheet_id: str, tenant_domain: str = "") -> str:
    """没有完整写表目标时返回空串，不生成不可用/残缺链接；空批仅在查到已有当日表时才附完整链接。"""
    if not ss_token or not ss_token.strip() or not sheet_id or not sheet_id.strip():
        return ""
    domain = tenant_domain or "feishu.cn"
    return f"https://{domain}/sheets/{ss_token}?sheet={sheet_id}"


def sheet_title_for(dt: datetime) -> str:
    """10.20（月.日，不补零）"""
    return f"{dt.month}.{dt.day}"


def spreadsheet_title_for(dt: datetime) -> str:
    """26年10月订单（两位年份）"""
    return f"{dt.year % 100}年{dt.month}月订单"


def _col_index(col: str) -> int:
    return ord(col) - 65


class WriteRecoveryError(RuntimeError):
    """原写入无法安全核实，必须保留断点并停止。"""


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_rows(rows: list, start_row: int) -> None:
    """订单写值前本地校验，不把空区/非法值发送给远端。"""
    if type(start_row) is not int or start_row < 2 or not rows:
        raise WriteRecoveryError("订单写入范围无效：起始行须为至少2的整数且订单非空")
    for row in rows:
        if len(row) != len(HEADER) or any(
            value is not None and (not isinstance(value, (str, int, float, bool))
                                   or isinstance(value, float) and not math.isfinite(value))
            for value in row
        ):
            raise WriteRecoveryError("订单单元格类型或列数无效")


class SheetWriter:
    def __init__(self, fs: Feishu, folder_token: str):
        self.fs = fs
        self.folder_token = folder_token
        self.target_name = 'test'
        self.fixed_target: tuple[str, str] | None = None
        self._confirmed_grid={}

    def ensure_capacity(self,ss_token,sheet_id,required_row):
        """写值/合并前先确认容量；只在表尾追加空行，不移动历史行或凭据。"""
        if getattr(self.fs,'supports_grid_preflight',False) is not True:return
        if type(required_row) is not int or required_row<1:raise WriteRecoveryError('目标行无效')
        target=(ss_token,sheet_id)
        if self._confirmed_grid.get(target,0)>=required_row:return
        def read_rows():
            sheets=self.fs.req('GET',f'/sheets/v3/spreadsheets/{ss_token}/sheets/query').get('sheets',[])
            matched=[s for s in sheets if s.get('sheet_id')==sheet_id]
            rows=matched[0].get('grid_properties',{}).get('row_count') if len(matched)==1 else None
            if type(rows) is not int or rows<1:raise WriteRecoveryError('无法核实工作表网格容量')
            return rows
        rows=read_rows()
        while rows<required_row:
            count=min(5000,max(500,required_row-rows+32))
            self.fs.req('POST',f'/sheets/v2/spreadsheets/{ss_token}/dimension_range',
                        json={'dimension':{'sheetId':sheet_id,'majorDimension':'ROWS','length':count}})
            verified=read_rows()
            if verified<=rows:raise WriteRecoveryError('追加行后容量尚未确认，未写值或合并')
            rows=verified
        self._confirmed_grid[target]=rows

    # ---- 表格/工作表 ----

    def find_existing_spreadsheet(self, dt: datetime) -> str:
        """只查找当月工作簿，不存在则返回空串，绝不新建。"""
        import os
        from app.test_runtime import ENV, _policy
        if ENV in os.environ:
            _, policy = _policy(os.environ[ENV])
            if self.target_name != "test":
                raise WriteRecoveryError("隔离测试拒绝正式目标")
            return policy["spreadsheet"]
        if not self.folder_token or self.target_name not in ("test", "prod"):
            return ""
        identity = [str(getattr(self.fs, "app_id", "")), self.target_name, self.folder_token]
        key = f"spreadsheet:v2:{_digest(identity)[:32]}:{dt.year}-{dt.month:02d}"
        with get_session() as s:
            row = s.get(State, key)
            if row and row.value:
                return row.value
        return ""

    def find_existing_sheet(self, ss_token: str, dt: datetime) -> str:
        """只查找当日 sheet，不存在则返回空串；不新建、不补表头（只读）。"""
        if not ss_token or not ss_token.strip():
            return ""
        title = sheet_title_for(dt)
        data = self.fs.req("GET", f"/sheets/v3/spreadsheets/{ss_token}/sheets/query")
        for sheet in data.get("sheets", []):
            if sheet["title"] == title:
                return sheet["sheet_id"]
        return ""

    def find_existing_day(self, dt: datetime) -> tuple[str, str]:
        """返回 (spreadsheet_token, sheet_id)；任一层不存在则 ("", "")，不新建。"""
        token = self.find_existing_spreadsheet(dt)
        if not token:
            return "", ""
        sheet = self.find_existing_sheet(token, dt)
        if not sheet:
            return "", ""
        return token, sheet

    def empty_notification_day(self, dt: datetime) -> tuple[str, str]:
        """空批只复用当月工作簿；缺当日日sheet则在这本月表创建，不建新文件。"""
        token, sheet = self.find_existing_day(dt)
        if token and sheet:
            return token, sheet
        token = self.find_existing_spreadsheet(dt)
        if not token:
            return "", ""
        return token, self.get_or_create_sheet(token, dt)

    def get_or_create_spreadsheet(self, dt: datetime) -> str:
        # 测试沙箱只允许指定表，不采纳复制来的月缓存，也不创建新工作簿。
        import os
        from app.test_runtime import ENV, _policy
        if ENV in os.environ:
            _, policy = _policy(os.environ[ENV])
            if self.target_name != 'test':
                raise WriteRecoveryError('隔离测试拒绝正式目标')
            return policy['spreadsheet']
        if not self.folder_token or self.target_name not in ('test', 'prod'):
            raise WriteRecoveryError('缺少明确的表格目标，拒绝使用旧月份缓存')
        # 不迁移无归属的旧键，避免把另一目标的表认领过来；无 schema 变化。
        identity = [str(getattr(self.fs, 'app_id', '')), self.target_name, self.folder_token]
        key = f"spreadsheet:v2:{_digest(identity)[:32]}:{dt.year}-{dt.month:02d}"
        with get_session() as s:
            row = s.get(State, key)
            if row and row.value:
                return row.value
        title = spreadsheet_title_for(dt)
        data = self.fs.req("POST", "/sheets/v3/spreadsheets",
                           json={"title": title, "folder_token": self.folder_token})
        token = data["spreadsheet"]["spreadsheet_token"]
        s2 = get_session()
        s2.merge(State(key=key, value=token))
        s2.commit()
        s2.close()
        logger.info("新建飞书表格「%s」: %s", title, token)
        return token

    def get_or_create_sheet(self, ss_token: str, dt: datetime) -> str:
        title = sheet_title_for(dt)
        data = self.fs.req("GET", f"/sheets/v3/spreadsheets/{ss_token}/sheets/query")
        for sheet in data.get("sheets", []):
            if sheet["title"] == title:
                self._ensure_header(ss_token, sheet["sheet_id"])  # 已存在的表补 I1「待核」
                return sheet["sheet_id"]
        data = self.fs.req("POST", f"/sheets/v2/spreadsheets/{ss_token}/sheets_batch_update",
                           json={"requests": [{"addSheet": {"properties": {"title": title}}}]})
        sheet_id = data["replies"][0]["addSheet"]["properties"]["sheetId"]
        self._write_header(ss_token, sheet_id)
        logger.info("新建工作表「%s」sheet_id=%s", title, sheet_id)
        return sheet_id

    def _ensure_header(self, ss_token: str, sheet_id: str) -> None:
        """已存在工作表：若 I1 不是「待核」则补上（表头从 8 列扩到 9 列的迁移）。"""
        data = self.fs.req("GET", f"/sheets/v2/spreadsheets/{ss_token}/values/{sheet_id}!I1:I1")
        values = data.get("valueRange", {}).get("values", [])
        cell = values[0][0] if values and values[0] else ""
        if cell != "待核":
            self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/values",
                        json={"valueRange": {"range": f"{sheet_id}!I1:I1", "values": [["待核"]]}})
            self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/styles_batch_update",
                        json={"data": [{"ranges": [f"{sheet_id}!I1:I1"], "style": HEADER_STYLE}]})
            logger.info("工作表 %s 补 I1「待核」表头", sheet_id)

    def _write_header(self, ss_token: str, sheet_id: str) -> None:
        self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/values",
                    json={"valueRange": {"range": f"{sheet_id}!A1:I1", "values": [HEADER]}})
        self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/styles_batch_update",
                    json={"data": [{"ranges": [f"{sheet_id}!A1:I1"], "style": HEADER_STYLE}]})
        for col, px in COL_WIDTHS.items():
            self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/dimension_range",
                        json={"dimension": {"sheetId": sheet_id, "majorDimension": "COLUMNS",
                                            "startIndex": _col_index(col), "endIndex": _col_index(col) + 1},
                              "dimensionProperties": {"visible": True, "fixedSize": px}})

    def _next_row(self, ss_token: str, sheet_id: str) -> int:
        """读 A、B 两列确定已用到哪一行，返回下一空行（1 起）。

        飞书会把有样式/合并的空行也返回，所以从后往前找最后一个有内容的行。
        A 列续行为空，故同时看 B 列（产品）。
        """
        data = self.fs.req("GET", f"/sheets/v2/spreadsheets/{ss_token}/values/{sheet_id}!A1:B10000")
        values = data.get("valueRange", {}).get("values", [])
        last = 0
        for i, row in enumerate(values, 1):
            if row and any(c not in (None, "") for c in row):
                last = i
        return last + 1

    def _next_seq(self, ss_token: str, sheet_id: str) -> int:
        """读 A 列已有序号，返回下一序号（最大值 +1；空表从 1 起）。

        跨批次续写时序号必须接着表内已有编号，禁止每批从 1 重计（问题清单 #25）。
        A 列第一行是表头「序号」，非数字跳过；合并单元格的续行为空也跳过。
        """
        data = self.fs.req("GET", f"/sheets/v2/spreadsheets/{ss_token}/values/{sheet_id}!A1:A10000")
        values = data.get("valueRange", {}).get("values", [])
        max_seq = 0
        for row in values:
            if not row:
                continue
            cell = row[0]
            if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                max_seq = max(max_seq, int(cell))
            elif isinstance(cell, str) and cell.strip().isdigit():
                max_seq = max(max_seq, int(cell.strip()))
        return max_seq + 1

    # ---- 写一个订单 ----

    def order_to_rows(self, order: dict, seq: int) -> list[list]:
        return self._serialize_order(order, seq)

    def _serialize_order(self, order: dict, seq: int, *, legacy=False) -> list[list]:
        """订单 -> 表格行。04 文档第 5 节。

        物流列H恒空，明确物流及旧字段内容无损归入地址G。
        legacy仅核验旧冻结凭据，不能用于新写入。
        第 I 列「待核」：拿不准的订单照常写出，只写具体请求（如「缺电话，请补」），
        干净的单留空（问题清单 #26 工作方式：不再拦待确认）。
        """
        from app.pipeline.review_text import review_sentence
        from app.pipeline.logistics_notes import collect_notes, extract_logistics_notes
        if legacy:
            verified_logistics = extract_logistics_notes(dict(order, logistics='')).get('logistics', '')
        else:
            order = collect_notes(deepcopy(order))
            verified_logistics = ''
        from app.pipeline.review_resolution import active_reasons
        review_cell = review_sentence(active_reasons(order, order.get('review_reasons') or []))
        rows = []
        lines = order["lines"]
        for i, line in enumerate(lines):
            rows.append([
                seq if i == 0 else "",
                line.get("product", ""),
                line.get("config", ""),
                line.get("qty", ""),
                order.get("receiver", "") if i == 0 else "",
                order.get("phone", "") if i == 0 else "",
                order.get("address", "") if i == 0 else "",
                verified_logistics if i == 0 else '',  # 只用原文，续行留空
                review_cell if i == 0 else "",  # 待核列
            ])
        if order.get('voided') and rows:
            for row in rows:
                row[1:4] = ['', '', '']
            rows[0][1] = '已取消'
        capacity = order.get('amendment_row_capacity')
        if capacity is not None:
            if type(capacity) is not int or capacity < 1:
                raise WriteRecoveryError('原单修改行容量无效')
            if len(rows) > capacity:
                first = list(rows[0])
                for col in (1, 2, 3):
                    first[col] = '\n'.join(str(row[col]) for row in rows)
                rows = [first]
            rows += [[''] * len(HEADER) for _ in range(capacity - len(rows))]
        return rows

    def write_order(self, ss_token: str, sheet_id: str, order: dict,
                    seq: int, start_row: int) -> int:
        """把一个订单写入 start_row 开始的位置，返回占用的行数。

        合并规则（对照真实表格金标准 26年10月订单.xlsx）：
        - 多行订单：A、E、F、G、H 五列都纵向合并（序号、收货人、电话、地址、物流居中）
        - 立柱多槽分行：B 列首行产品名纵向合并（B8:B10 这种，product 相同的连续 post 行）
        - 焊接描述行 / 换货说明行：B:D 横向合并
        """
        rows = self.order_to_rows(order, seq)
        _validate_rows(rows, start_row)
        from app.models import Message
        from app.pipeline.source_group_guard import source_ids, single_room, mark_blocked, SourceGroupError
        try:
            ids = source_ids(order)
            with get_session() as session:
                single_room(order, {mid:session.get(Message, mid) for mid in ids})
        except SourceGroupError as exc:
            mark_blocked(order, str(exc))
            raise WriteRecoveryError(str(exc)) from exc
        n = len(rows)
        end_row = start_row + n - 1
        self.ensure_capacity(ss_token,sheet_id,end_row+32)
        self._put_order_values(ss_token, f"{sheet_id}!A{start_row}:I{end_row}", rows)
        self._format_order(ss_token, sheet_id, order, start_row, n)
        return n

    def _put_order_values(self, ss_token: str, range_: str, rows: list) -> None:
        if any(len(row) > 7 and row[7] not in ('', None) for row in rows):
            raise WriteRecoveryError('H列物流必须为空；旧冻结载荷需专门迁移，未发出写入')
        try:
            self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/values",
                        json={"valueRange": {"range": range_, "values": rows}})
        except Exception:
            # 只记形状，保留原异常与检查点；不重试、不输出订单内容。
            logger.error("订单写值失败 range=%s rows=%d columns=%d",
                         range_, len(rows), len(HEADER))
            raise

    def _format_order(self, ss_token: str, sheet_id: str, order: dict,
                      start_row: int, n: int) -> None:
        """值与格式分开执行，使格式重试不会重复写值。"""
        end_row = start_row + n - 1
        self.ensure_capacity(ss_token,sheet_id,end_row+32)
        # 21:42废止进备注；notes保留内部说明，不再定位/新建备注列。
        merges = []
        if n > 1:
            # A、E、F、G、H、I 六列纵向合并（序号、收货人、电话、地址、物流、待核居中）
            for col in ("A", "E", "F", "G", "H", "I"):
                merges.append(f"{sheet_id}!{col}{start_row}:{col}{end_row}")
        # 立柱多槽分行：连续 post 行且续行 product 为空时，B 列纵向合并
        lines = order["lines"]
        i = 0
        while i < len(lines):
            if lines[i].get("kind") == "post":
                j = i
                while j + 1 < len(lines) and lines[j + 1].get("kind") == "post" \
                        and not lines[j + 1].get("product"):
                    j += 1
                if j > i:
                    merges.append(f"{sheet_id}!B{start_row + i}:B{start_row + j}")
                i = j + 1
            else:
                i += 1
        # 焊接描述行 / 换货说明行：B:D 横向合并
        for i, line in enumerate(lines):
            if (line.get("kind")=="weld_note" or (line.get('kind')=='order_note'
                    and 'source:instruction_line' in line.get('fixes',[])
                    and not line.get('config') and not line.get('qty'))):
                merges.append(f"{sheet_id}!B{start_row + i}:D{start_row + i}")
        # 统一发合并请求（间隔 100ms 防限流）
        for rng in merges:
            self.fs.req("POST", f"/sheets/v2/spreadsheets/{ss_token}/merge_cells",
                        json={"range": rng, "mergeType": "MERGE_ALL"})
            time.sleep(0.1)
        # 数据居中
        self.fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/styles_batch_update",
                    json={"data": [{"ranges": [f"{sheet_id}!A{start_row}:I{end_row}"],
                                    "style": DATA_STYLE}]})

    @staticmethod
    def _save_checkpoint(order: Order, checkpoint: dict, *, values_written=False,
                         done=False) -> None:
        order.write_checkpoint_json = json.dumps(checkpoint, ensure_ascii=False)
        if values_written or done:
            order.written_rows = json.dumps(checkpoint["rows"])
        if done:
            order.status = "written"
        attached = object_session(order)
        if attached is not None:
            attached.commit()
            return
        # 脱离会话的接口也只更新写表字段，避免 merge 覆盖其他新状态。
        with get_session() as session:
            stored = session.get(Order, order.id)
            if stored is None:
                raise WriteRecoveryError("写表订单尚未持久化")
            stored.write_checkpoint_json = order.write_checkpoint_json
            if values_written or done:
                stored.written_rows = order.written_rows
            if done:
                stored.status = order.status
            session.commit()

    def _checkpoint(self, order: Order) -> tuple[dict, dict, list]:
        """任何远端调用之前校验冻结的目标、范围与完整输出。"""
        try:
            data = json.loads(order.data_json)
            cp = json.loads(order.write_checkpoint_json or "{}")
            written = json.loads(order.written_rows or "[]")
            if not isinstance(data, dict) or not isinstance(cp, dict) or not isinstance(written, list):
                raise ValueError("无效订单或断点")
            if not cp:
                if not written and order.status != "written":
                    _validate_rows(self.order_to_rows(data, 1), 2)
                return data, cp, written
            start, end = cp["rows"]
            seq = cp["seq"]
            if (type(cp["version"]) is not int or cp["version"] != 1
                    or cp["state"] not in ("inflight", "format_pending", "done")
                    or any(type(value) is not int for value in (start, end, seq))
                    or start < 2 or end < start or seq < 1
                    or not isinstance(cp["spreadsheet_token"], str) or not cp["spreadsheet_token"]
                    or not isinstance(cp["sheet_id"], str) or not cp["sheet_id"]
                    or cp["range"] != f'{cp["sheet_id"]}!A{start}:I{end}'
                    or cp["data_digest"] != _digest(data)
                    or cp["payload_digest"] != _digest(cp["payload"])
                    or (cp["payload"] != self.order_to_rows(data, seq)
                        and cp["payload"] != self._serialize_order(data, seq, legacy=True))
                    or len(cp["payload"]) != end - start + 1
                    or (written and written != cp["rows"])
                    or (cp["state"] in ("format_pending", "done") and written != cp["rows"])):
                raise ValueError("断点与订单内容或写入范围不一致")
            _validate_rows(cp["payload"], start)
            if cp['state'] != 'done' and any(row[7] not in ('', None) for row in cp['payload']):
                raise WriteRecoveryError('旧写表断点H含物流，需专门迁移后恢复；未修改冻结凭据')
            return data, cp, written
        except (KeyError, TypeError, ValueError) as exc:
            raise WriteRecoveryError(f"订单 {order.id} 写表断点无效：{exc}") from exc

    def _recover_values(self, ss_token: str, cp: dict) -> bool:
        """返回是否需要向原范围重写；未知响应与冲突一律停止。"""
        try:
            response = self.fs.req("GET", f'/sheets/v2/spreadsheets/{ss_token}/values/{cp["range"]}')
            values = response["valueRange"]["values"]
            height = len(cp["payload"])
            if not isinstance(values, list) or len(values) > height:
                raise ValueError("返回范围无法核实")
            actual = []
            for row in values:
                if not isinstance(row, list) or len(row) > len(HEADER):
                    raise ValueError("返回单元格无法核实")
                actual.append(["" if value is None else value for value in row] +
                              [""] * (len(HEADER) - len(row)))
            actual += [[""] * len(HEADER) for _ in range(height - len(actual))]
            if actual == cp["payload"]:
                return False
            if all(value == "" for row in actual for value in row):
                return True
            raise ValueError("原范围已有其他内容，拒绝追加或覆盖")
        except Exception as exc:
            raise WriteRecoveryError(f'无法安全恢复写入 {cp["range"]}：{exc}') from exc

    def verify_payload(self,ss_token,cp):
        """合并后仍须保留整单值；丢序号时停止，避免下一单复用序号。"""
        if getattr(self.fs,'supports_grid_preflight',False) is not True:return
        from app.pipeline.stage_log import stage
        stage(getattr(self,'_batch_id',0),'readback','start',rows=len(cp['payload']))
        try:
            if self._recover_values(ss_token,cp):
                raise WriteRecoveryError('写出后未完整保留冻结载荷，保留凭据并停止')
        except Exception as error:
            stage(getattr(self,'_batch_id',0),'readback','failed',reason=type(error).__name__)
            raise
        stage(getattr(self,'_batch_id',0),'readback','done',rows=len(cp['payload']))

    def write_orders(self, orders: list[Order], dt: datetime,
                     batch_id: int) -> tuple[int, int]:
        """先持久化写入意图；不确定写入只核实原范围，返回本批全部断点范围。"""
        self._batch_id=batch_id
        if not getattr(self, 'item_error_handler', None) and len({object_session(o) for o in orders}) > 1:
            raise WriteRecoveryError('订单来自不同数据库会话，请重新读取后核对来源')
        prepared = []
        from app.pipeline.source_group_guard import guard_stored_orders, SourceGroupError
        for order in orders:
            try:
                item = (order, *self._checkpoint(order))
                checkpoint = item[2]
                if checkpoint and self.fixed_target and tuple(self.fixed_target) != (
                        checkpoint['spreadsheet_token'], checkpoint['sheet_id']):
                    raise WriteRecoveryError('写表断点与固定目标不一致')
                attached = object_session(order)
                if attached is not None:
                    guard_stored_orders(attached, [order])
                else:
                    with get_session() as session:
                        guard_stored_orders(session, [order])
                prepared.append(item)
            except Exception as error:
                if not getattr(self, 'item_error_handler', None):
                    if isinstance(error, SourceGroupError):
                        raise WriteRecoveryError(str(error)) from error
                    raise
                self.item_error_handler(order, error)
        targets = {(cp["spreadsheet_token"], cp["sheet_id"])
                   for _, _, cp, _ in prepared if cp}
        if self.fixed_target:
            targets.add(tuple(self.fixed_target))
        if len(targets) > 1:
            raise WriteRecoveryError("写表断点与固定目标不一致")
        active = [(order, data, cp) for order, data, cp, written in prepared
                  if cp or (not written and order.status != "written")]
        if not active:
            return 0, 0
        if targets:
            ss_token, sheet_id = next(iter(targets))
        else:
            ss_token = self.get_or_create_spreadsheet(dt)
            sheet_id = self.get_or_create_sheet(ss_token, dt)
        ranges = [cp["rows"] for _, _, cp in active if cp]
        reserved_seq = []
        # An isolated row conflict must not let the next order reuse its frozen
        # physical range or sequence, including a blank/partially written range.
        for order in getattr(self, 'reserved_orders', []):
            try:
                frozen = json.loads(order.write_checkpoint_json)
                start, end = frozen['rows']
                sequence = frozen['seq']
                if (all(type(n) is int for n in (start, end, sequence))
                        and 2 <= start <= end and sequence >= 1
                        and (frozen['spreadsheet_token'], frozen['sheet_id']) == (ss_token, sheet_id)):
                    ranges.append([start, end])
                    reserved_seq.append(sequence)
            except (ValueError, TypeError, KeyError):
                pass
        row = seq = None
        for order, data, cp in active:
            try:
                if cp and cp["state"] == "done":
                    continue
                fresh = not cp
                if fresh:
                    if row is None:
                        row = max(self._next_row(ss_token, sheet_id),
                                  max((bounds[1] + 1 for bounds in ranges), default=2))
                        seq = max(self._next_seq(ss_token, sheet_id),
                              max((n + 1 for n in reserved_seq), default=1),
                                  max((existing["seq"] + 1 for _, _, existing in active if existing), default=1))
                    payload = self.order_to_rows(data, seq)
                    _validate_rows(payload, row)
                    end = row + len(payload) - 1
                    self.ensure_capacity(ss_token,sheet_id,end+32)
                    cp = {"version": 1, "state": "inflight", "spreadsheet_token": ss_token,
                          "sheet_id": sheet_id, "range": f"{sheet_id}!A{row}:I{end}",
                          "rows": [row, end], "seq": seq, "payload": payload,
                          "data_digest": _digest(data), "payload_digest": _digest(payload)}
                    self._save_checkpoint(order, cp)
                    ranges.append(cp["rows"])
                    row, seq = end + 1, seq + 1
                if cp["state"] == "inflight":
                    if fresh or self._recover_values(ss_token, cp):
                        self._put_order_values(ss_token, cp["range"], cp["payload"])
                    cp["state"] = "format_pending"
                    self._save_checkpoint(order, cp, values_written=True)
                self._format_order(ss_token, sheet_id, data, cp["rows"][0], len(cp["payload"]))
                self.verify_payload(ss_token,cp)
                cp["state"] = "done"
                self._save_checkpoint(order, cp, done=True)
            except Exception as error:
                if not getattr(self, 'item_error_handler', None):
                    raise
                self.item_error_handler(order, error)
        return (min(bounds[0] for bounds in ranges), max(bounds[1] for bounds in ranges)) if ranges else (0, 0)

    def sheet_url(self, ss_token: str, sheet_id: str, tenant_domain: str = "") -> str:
        return sheet_url(ss_token, sheet_id, tenant_domain)
