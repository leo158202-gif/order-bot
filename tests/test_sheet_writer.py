"""T3.1 验收：sheet_writer。"""
import json
from datetime import datetime

import pytest

from app.feishu.sheet_writer import (SheetWriter, sheet_title_for,
                                     spreadsheet_title_for)
from app.models import Order, Message


def test_titles():
    dt = datetime(2026, 10, 20, 1, 0)
    assert sheet_title_for(dt) == "10.20"
    assert spreadsheet_title_for(dt) == "26年10月订单"
    dt2 = datetime(2026, 1, 5)
    assert sheet_title_for(dt2) == "1.5"  # 不补零
    assert spreadsheet_title_for(dt2) == "26年1月订单"


def _order(lines, receiver="张三", phone="19900000009", address="浙江省xx", logistics=""):
    return {"source_msgids":["sheet-source"], "receiver": receiver, "phone": phone, "address": address,
            "logistics": logistics, "lines": lines}


class TestOrderToRows:
    def setup_method(self):
        self.w = SheetWriter(None, "folder")

    def test_multi_line_order(self):
        o = _order([
            {"kind": "post", "product": "灰色内卡槽立柱 焊接 H款", "config": "【3.0】1170高 单槽", "qty": "2支"},
            {"kind": "post", "product": "", "config": "【3.0】1170高 双槽", "qty": "1支"},
            {"kind": "cap", "product": "灰色立柱盖子", "config": "", "qty": "3个"},
        ])
        rows = self.w.order_to_rows(o, seq=6)
        assert len(rows) == 3
        assert rows[0] == [6, "灰色内卡槽立柱 焊接 H款", "【3.0】1170高 单槽", "2支",
                           "张三", "19900000009", "浙江省xx", "", ""]  # H 物流空 + I 待核空
        assert rows[1][0] == "" and rows[1][1] == ""  # 续行 A、B 留空
        assert rows[1][4:] == ["", "", "", "", ""]
        assert rows[2][0] == ""

    def test_review_column_marked(self):
        """拿不准的订单照常写出，I 列「待核」写原因；干净的单留空。"""
        o = _order([{"kind": "post", "product": "A", "config": "c", "qty": "1支"}])
        o["review_reasons"] = ["缺电话", "置信度低"]
        rows = self.w.order_to_rows(o, seq=1)
        assert rows[0][8] == "缺电话，请补"
        o2 = _order([{"kind": "post", "product": "A", "config": "c", "qty": "1支"}])
        assert self.w.order_to_rows(o2, seq=1)[0][8] == ""

    def test_logistics_column_always_blank(self):
        """#30：订单 logistics 字段即使有内容，写出物流列也恒为空。"""
        o = _order([{"kind": "rail", "product": "40*80黑色铝合金扶手",
                     "config": "1支2米", "qty": "2米"}],
                   logistics="到付30 皓步店 本地发 拼多多")
        rows = self.w.order_to_rows(o, seq=1)
        assert rows[0][7] == ""

    def test_single_line(self):
        o = _order([{"kind": "rail", "product": "40*80黑色铝合金扶手",
                     "config": "1支2米", "qty": "2米"}])
        rows = self.w.order_to_rows(o, seq=1)
        assert len(rows) == 1
        assert rows[0][0] == 1


class TestResume:
    def test_written_order_skipped(self, tmp_path, monkeypatch):
        """断点续写：written_rows 非空的订单跳过。"""
        from app.db import init_db
        factory = init_db(tmp_path / "t.db")
        monkeypatch.setattr("app.feishu.sheet_writer.get_session", factory)

        s = factory()
        s.add(Message(msgid='sheet-source',seq=1,roomid='test-room',sender='test',msgtime=1000,msgtype='text',content='合成订单'))
        o1 = Order(batch_id=1, status="written",
                   data_json=json.dumps(_order([{"kind": "post", "product": "A",
                                                 "config": "c", "qty": "1支"}])),
                   written_rows="[2, 2]")
        o2 = Order(batch_id=1, status="ok",
                   data_json=json.dumps(_order([{"kind": "post", "product": "B",
                                                 "config": "c", "qty": "1支"}])),
                   written_rows="[]")
        s.add_all([o1, o2])
        s.commit()
        ids = [o1.id, o2.id]
        s.close()

        calls = []

        class FakeFs:
            def req(self, method, path, **kw):
                calls.append((method, path))
                if "sheets/query" in path:
                    return {"sheets": [{"title": "10.20", "sheet_id": "sid"}]}
                if method == "GET" and "I1" in path:
                    return {"valueRange": {"values": [["待核"]]}}  # 已有表头
                if "values/" in path and method == "GET":
                    # A1:B10000 和 A1:A10000 都给同一内容：表头 + 已有序号 1
                    return {"valueRange": {"values": [["序号"], [1]]}}
                return {"spreadsheet": {"spreadsheet_token": "tok"},
                        "replies": [{"addSheet": {"properties": {"sheetId": "sid"}}}]}

        w = SheetWriter(FakeFs(), "folder")
        s2 = factory()
        orders = s2.query(Order).order_by(Order.id).all()
        row_from, row_to = w.write_orders(orders, datetime(2026, 10, 20), batch_id=1)
        # 只有 o2 写了 1 行，从第 3 行开始（前 2 行：表头 + o1）
        assert row_from == 3 and row_to == 3
        # o2 标记为 written
        assert json.loads(orders[1].written_rows) == [3, 3]
        s2.close()

    def test_seq_continues_across_batches(self, tmp_path, monkeypatch):
        """#25：第二批接着表内已有序号编号，不从 1 重计。"""
        from app.db import init_db
        factory = init_db(tmp_path / "t.db")
        monkeypatch.setattr("app.feishu.sheet_writer.get_session", factory)

        s = factory()
        s.add(Message(msgid='sheet-source',seq=1,roomid='test-room',sender='test',msgtime=1000,msgtype='text',content='合成订单'))
        o = Order(batch_id=2, status="ok",
                  data_json=json.dumps(_order([{"kind": "post", "product": "B",
                                                "config": "c", "qty": "1支"}])),
                  written_rows="[]")
        s.add(o)
        s.commit()
        s.close()

        written_rows_payload = []

        class FakeFs:
            def req(self, method, path, **kw):
                if "sheets/query" in path:
                    return {"sheets": [{"title": "10.20", "sheet_id": "sid"}]}
                if method == "GET" and "I1" in path:
                    return {"valueRange": {"values": [["待核"]]}}
                if "values/" in path and method == "GET":
                    # 表里已有表头 + 序号 1..5
                    return {"valueRange": {"values": [["序号"], [1], [""], [2], [""], [5]]}}
                if method == "PUT" and path.endswith("/values"):
                    written_rows_payload.append(kw["json"]["valueRange"]["values"])
                    return {}
                return {"spreadsheet": {"spreadsheet_token": "tok"},
                        "replies": [{"addSheet": {"properties": {"sheetId": "sid"}}}]}

        w = SheetWriter(FakeFs(), "folder")
        s2 = factory()
        orders = s2.query(Order).order_by(Order.id).all()
        w.write_orders(orders, datetime(2026, 10, 20), batch_id=2)
        # 表内最大序号 5，本单应写序号 6
        assert written_rows_payload[0][0][0] == 6
        s2.close()

    def test_next_seq_empty_sheet(self):
        """#25：空表（只有表头）从 1 起；非数字/表头跳过。"""
        class FakeFs:
            def req(self, method, path, **kw):
                return {"valueRange": {"values": [["序号"]]}}
        assert SheetWriter(FakeFs(), "f")._next_seq("tok", "sid") == 1

        class FakeFs2:
            def req(self, method, path, **kw):
                return {"valueRange": {"values": [["序号"], [3], [""], ["7"], ["abc"]]}}
        assert SheetWriter(FakeFs2(), "f")._next_seq("tok", "sid") == 8

    def test_ensure_header_adds_review_col(self):
        """已存在工作表缺 I1「待核」时自动补；已有则不重复写。"""
        put_ranges = []
        class FakeFs:
            def req(self, method, path, **kw):
                if "sheets/query" in path:
                    return {"sheets": [{"title": "10.21", "sheet_id": "sid"}]}
                if method == "GET" and "I1" in path:
                    return {"valueRange": {"values": [[""]]}}  # 缺 I1
                if method == "PUT":
                    put_ranges.append(kw.get("json", {}))
                return {}
        w = SheetWriter(FakeFs(), "f")
        w.get_or_create_sheet("tok", datetime(2026, 10, 21))
        # 补了 I1：valueRange.range 含 I1，或样式 ranges 含 I1
        assert any("I1" in str(r) for r in put_ranges)

        # 已有「待核」则不再写
        put_ranges.clear()
        class FakeFs2(FakeFs):
            def req(self, method, path, **kw):
                if method == "GET" and "I1" in path:
                    return {"valueRange": {"values": [["待核"]]}}
                if "sheets/query" in path:
                    return {"sheets": [{"title": "10.21", "sheet_id": "sid"}]}
                return {}
        SheetWriter(FakeFs2(), "f").get_or_create_sheet("tok", datetime(2026, 10, 21))
        assert not put_ranges
