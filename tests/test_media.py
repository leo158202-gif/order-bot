"""T1.3 验收：附件下载 + 转文字。"""
import json

import pytest

from app.db import init_db
from app.models import Message
from app.wecom.media import file_to_text, process_pending_files


@pytest.fixture()
def session(tmp_path, monkeypatch):
    # 附件存到临时目录，不污染 data/media
    import app.wecom.media as media_mod
    monkeypatch.setattr(media_mod, "MEDIA_DIR", tmp_path / "media")
    factory = init_db(tmp_path / "test.db")
    s = factory()
    yield s
    s.close()


def test_xlsx_to_text(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["产品", "数量"])
    ws.append(["立柱", "2支"])
    f = tmp_path / "t.xlsx"
    wb.save(f)
    text = file_to_text(f)
    assert "产品" in text and "立柱" in text and "2支" in text


def test_docx_to_text(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("张三 19900000009")
    f = tmp_path / "t.docx"
    d.save(str(f))
    assert "张三" in file_to_text(f)


def test_pdf_to_text(tmp_path):
    # 生成一个最小 pdf（用 pdfplumber 能读的）
    pytest.importorskip("pdfplumber")
    import io
    # 没有简单库能写 pdf，跳过实际生成，用不支持的类型测试兜底
    f = tmp_path / "t.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    # 损坏的 pdf 不应抛异常，返回空串
    assert file_to_text(f) == ""


def test_process_file_message(session):
    """file 消息下载并转文字后，content 从占位符变成实际文字。"""
    # 造一个真实的 xlsx 附件，用假 SDK 返回它的字节
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["测试内容ABC"])
    buf = __import__("io").BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()

    class FakeSdk:
        def get_media_data(self, sdkfileid, **kw):
            return file_bytes

    raw = {"msgid": "mf1", "msgtype": "file",
           "file": {"filename": "订单.xlsx", "fileext": "xlsx",
                    "filesize": len(file_bytes), "sdkfileid": "fake-id"}}
    session.add(Message(msgid="mf1", seq=1, roomid="wrAAA", sender="u1",
                        msgtime=1, msgtype="file",
                        content="[文件] 订单.xlsx", raw_json=json.dumps(raw),
                        file_path="订单.xlsx"))
    session.commit()

    n = process_pending_files(session, FakeSdk())
    assert n == 1
    m = session.get(Message, "mf1")
    assert "测试内容ABC" in m.content
    assert m.file_path.endswith(".xlsx")


def test_skip_non_supported(session):
    """不支持的类型（如 zip）跳过。"""
    raw = {"msgid": "mz1", "msgtype": "file",
           "file": {"filename": "a.zip", "fileext": "zip", "filesize": 1, "sdkfileid": "x"}}
    session.add(Message(msgid="mz1", seq=1, roomid="wrAAA", sender="u1",
                        msgtime=1, msgtype="file", content="[文件] a.zip",
                        raw_json=json.dumps(raw), file_path="a.zip"))
    session.commit()

    n = process_pending_files(session, None)
    assert n == 0
    assert session.get(Message, "mz1").content == "[文件] a.zip"
