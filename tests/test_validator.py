"""T2.4 验收：validator。06 文档 R17/R18/R19 + 其余校验规则。"""
import pytest

from app.pipeline.validator import validate


def ok_order(**kw):
    base = {"receiver": "张三", "phone": "19900000009", "address": "浙江省xx",
            "logistics": "德邦", "confidence": 0.95, "unknown_terms": [],
            "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                       "config": "【3.0】1170高 单槽", "qty": "2支", "confidence": 0.95}]}
    base.update(kw)
    return base


def test_R17_modify_flag():
    o = ok_order(modify_flag=True)
    status, reasons = validate(o)
    assert status == "pending"
    assert '请确认修改后的产品和数量' in reasons


def test_R18_missing_phone():
    o = ok_order(phone="")
    status, reasons = validate(o)
    assert status == "pending"
    assert any("缺电话" in r for r in reasons)


def test_R19_sample_exempt():
    # 料头订单缺地址 -> 不进待确认
    o = ok_order(address="", lines=[{"kind": "sample", "product": "黑色内卡槽立柱 料头",
                                     "config": "【3.0】单槽", "qty": "10公分", "confidence": 0.95}])
    status, _ = validate(o)
    assert status == "ok"


def test_unknown_terms():
    o = ok_order(unknown_terms=["H960的加厚垫片"])
    status, reasons = validate(o)
    assert status == "pending"
    assert '请确认H960的加厚垫片的名称规格' in reasons


def test_low_confidence():
    o = ok_order(confidence=0.5)
    status, _ = validate(o)
    assert status == "ok"


def test_line_low_confidence():
    o = ok_order()
    o["lines"][0]["confidence"] = 0.5
    status, _ = validate(o)
    assert status == "ok"


def test_qty_diff_over_10pct():
    o = ok_order()
    o["lines"][0]["_qty_diff"] = ("10米", "12米")  # 差 16%
    status, reasons = validate(o)
    assert status == "pending"
    assert '数量与原文不一致，请确认几米' in reasons


def test_qty_diff_within_10pct_ok():
    o = ok_order()
    o["lines"][0]["_qty_diff"] = ("11.5米", "12米")  # 差 4%
    status, _ = validate(o)
    assert status == "ok"


def test_ziti_exempt():
    o = ok_order(address="", logistics="自提")
    status, _ = validate(o)
    assert status == "ok"


def test_has_media():
    o = ok_order(has_media=True)
    status, _ = validate(o)
    assert status == "pending"


def test_all_ok():
    status, reasons = validate(ok_order())
    assert status == "ok" and reasons == []
