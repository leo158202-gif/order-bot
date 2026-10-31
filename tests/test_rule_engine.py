"""06 文档第 2 节 R01~R20 验收用例。"""
from pathlib import Path

import pytest

from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules


@pytest.fixture(scope="module")
def rules():
    return load_rules(Path(__file__).resolve().parents[1] / "rules" / "rules.yaml")


def mkorder(lines, receiver="张三", phone="19900000009", address="浙江省xx",
            logistics="", notes="", raw_all=""):
    """构造一个订单 dict。lines 元素是 (kind, product, config, qty) 或 dict。"""
    out_lines = []
    for l in lines:
        if isinstance(l, dict):
            out_lines.append({**{"raw": "", "confidence": 0.95}, **l})
        else:
            kind, product, config, qty = l
            out_lines.append({"kind": kind, "product": product, "config": config,
                              "qty": qty, "raw": raw_all or product, "confidence": 0.95})
    return {"receiver": receiver, "phone": phone, "address": address,
            "logistics": logistics, "notes": notes, "lines": out_lines,
            "unknown_terms": [], "confidence": 0.9}


def test_R01_symbol_x_to_star(rules):
    o = process(mkorder([("rail", "30x30黑色铝合金卡槽扶手", "2支4米", "8米")]), rules)
    assert "30*30" in o["lines"][0]["product"]


def test_R02_color_black(rules):
    o = process(mkorder([("rail", "30*30*1.7砂黑铝合金卡槽扶手", "2支4米", "8米")]), rules)
    assert "黑色" in o["lines"][0]["product"] and "砂黑" not in o["lines"][0]["product"]
    o2 = process(mkorder([("rail", "30*30*1.7亚黑铝合金卡槽扶手", "2支4米", "8米")]), rules)
    assert "黑色" in o2["lines"][0]["product"]


def test_R03_color_gray(rules):
    o = process(mkorder([("rail", "50*80深灰铝合金扶手", "1支2米", "2米")]), rules)
    assert "灰色" in o["lines"][0]["product"]


def test_R04_typos(rules):
    o = process(mkorder([("post", "灰色内卡槽立住 免护手款", "【2.0】975高 单槽", "4支")]), rules)
    assert "立柱" in o["lines"][0]["product"] and "免扶手" in o["lines"][0]["product"]


def test_R05_total_meters(rules):
    o = process(mkorder([("rail", "40*80黑色铝合金扶手", "3支6米 2支4米 2支3米", "")]), rules)
    assert o["lines"][0]["qty"] == "32米"


def test_R06_total_meters_decimal(rules):
    o = process(mkorder([("rail", "30*30黑色铝合金卡槽扶手", "【2.5】2支1.5米", "")]), rules)
    assert o["lines"][0]["qty"] == "3米"


def test_R07_slot_split(rules):
    o = process(mkorder([("post", "黑色内卡槽立柱 焊接款 无头", "【2.0】975高 2双6单", "8支")]), rules)
    posts = [l for l in o["lines"] if l["kind"] == "post"]
    assert len(posts) == 2
    assert posts[0]["qty"] == "2支" and "双槽" in posts[0]["config"]
    assert posts[1]["qty"] == "6支" and "单槽" in posts[1]["config"]
    assert posts[1]["product"] == ""  # 续行 B 列留空


def test_R08_screw_and_cap_calc(rules):
    # 金标准无头目录每点4个：12支+8靠墙 -> 钻尾80；膨胀64、盖子12。
    o = process(mkorder([
        ("post", "黑色内卡槽立柱 焊接款 无头", "【2.0】975高 单槽", "12支"),
        ("rail_fitting", "新款 靠墙封口", "黑色40*80配件", "8个"),
    ]), rules)
    products = {l["product"]: l["qty"] for l in o["lines"]}
    assert products.get("膨胀螺丝") == "64个"
    assert products.get("小号钻尾螺丝") == "80个"
    assert products.get("黑色立柱盖子") == "12个"


def test_R09_mianfushou_no_drill(rules):
    # 免扶手 4 支 -> 膨胀16，不补钻尾，盖子4
    o = process(mkorder([
        ("post", "灰色内卡槽立柱 免扶手款", "【2.0】975高 单槽", "4支"),
    ]), rules)
    products = {l["product"]: l["qty"] for l in o["lines"]}
    assert products.get("膨胀螺丝") == "16个"
    assert "小号钻尾螺丝" not in products
    assert products.get("灰色立柱盖子") == "4个"


def test_R10_user_qty_kept(rules):
    # 用户写了膨胀螺丝 32个 -> 不覆盖
    o = process(mkorder([
        ("post", "黑色内卡槽立柱 焊接款 无头", "【2.0】975高 单槽", "12支"),
        ("screw", "膨胀螺丝", "", "32个"),
    ]), rules)
    products = {l["product"]: l["qty"] for l in o["lines"]}
    assert products["膨胀螺丝"] == "32个"


def test_R11_weld_note_h(rules):
    # 焊接 H 款立柱 -> 插入描述行带 H 头后缀
    o = process(mkorder([
        ("post", "灰色内卡槽立柱 焊接 H款", "【3.0】1170高 单槽", "2支"),
    ]), rules)
    weld = [l for l in o["lines"] if l["kind"] == "weld_note"]
    assert len(weld) == 1
    assert "焊接 新款三槽主体 卡条离地10公分" in weld[0]["product"]
    assert "H头用锁螺丝的" in weld[0]["product"]
    # 位置：紧跟立柱行
    kinds = [l["kind"] for l in o["lines"]]
    assert kinds.index("weld_note") == kinds.index("post") + 1


def test_R12_line_order(rules):
    o = process(mkorder([
        ("screw", "膨胀螺丝", "", "64个"),
        ("rail", "40*80黑色铝合金扶手", "4支3米", "12米"),
        ("post", "黑色内卡槽立柱 焊接款 无头", "【2.0】975高 单槽", "12支"),
        ("cap", "黑色立柱盖子", "", "12个"),
    ]), rules)
    kinds = [l["kind"] for l in o["lines"]]
    assert kinds == ["post", "weld_note", "cap", "screw", "screw", "rail"]
    # post 后自动补了 weld_note（焊接款）和钻尾


def test_R13_receiver_bracket(rules):
    o = process(mkorder([], receiver="高卫生[5877]"), rules)
    assert o["receiver"] == "高卫生"
    assert o["phone"] == "19900000009-5877"


def test_R14_address_bracket(rules):
    o = process(mkorder([], address="浙江省杭州市示例桥[5877]"), rules)
    assert o["address"] == "浙江省杭州市示例桥"
    assert o["phone"] == "19900000009-5877"


def test_R15_channel_tag(rules):
    # #30：渠道标记只进内部分类 channel_tags，不再写 logistics（店名不进表格）
    o = process(mkorder([("rail", "30*30黑色扶手", "2支4米", "8米")],
                        raw_all="扶手 皓步店"), rules)
    assert "皓步店 本地发 拼多多" in o.get("channel_tags", [])
    assert "皓步店" not in (o.get("logistics") or "")


def test_R16_qty_mismatch_marked(rules):
    # AI 给 D=10米，配置 3支4米 -> 改 12米，记 qty_fixed
    o = process(mkorder([("rail", "40*80黑色铝合金扶手", "3支4米", "10米")]), rules)
    assert o["lines"][0]["qty"] == "12米"
    assert "qty_fixed" in o["lines"][0]["fixes"]
    assert o["lines"][0]["_qty_diff"] == ("10米", "12米")


def test_R20_zhi_unit_kept(rules):
    # 原文用"只"：1只3米 1只1米 -> D=4米
    o = process(mkorder([("rail", "40*80黑色铝合金扶手", "1只3米 1只1米", "")]), rules)
    assert o["lines"][0]["qty"] == "4米"


def test_color_no_double_replace(rules):
    # AI 已输出标准色"黑色"时，"黑"不能再被替换成"黑色"（防止"黑色色"）
    o = process(mkorder([("rail", "30*30*1.7黑色铝合金卡槽扶手", "2支4米", "8米")]), rules)
    assert "黑色色" not in o["lines"][0]["product"]
    assert "黑色铝合金" in o["lines"][0]["product"]


def test_phone_bracket_appended_as_extension(rules):
    # 四位方括号转为虚拟号分机，必须保留在电话字段。
    o = process(mkorder([], phone="19900000027[1076]"), rules)
    assert o["phone"] == "19900000027-1076"
    # 本身就是分机格式的保留
    o2 = process(mkorder([], phone="19900000009-5672"), rules)
    assert o2["phone"] == "19900000009-5672"


def test_h_post_implies_weld_note(rules):
    """P1：H款 立柱即使原文/AI 没写「焊接」，也补焊接说明行（10.21 金标准偏差最高频）。"""
    o = process(mkorder([
        ("post", "灰色内卡槽立柱 H款", "【2.0】850高 单槽", "46支"),
    ]), rules)
    weld = [l for l in o["lines"] if l["kind"] == "weld_note"]
    assert len(weld) == 1
    assert "焊接 新款三槽主体 卡条离地10公分" in weld[0]["product"]
    assert "H头用锁螺丝的" in weld[0]["product"]


def test_receiver_songhuoshangmen_stripped(rules):
    """P5：收货人混入「送货上门」时剥掉，保留《》里的真实收货人。"""
    o = process(mkorder([], receiver="送货上门《合成门窗收》"), rules)
    assert o["receiver"] == "合成门窗收"
    # 没有《》的兜底：去掉前缀
    o2 = process(mkorder([], receiver="送货上门 张三"), rules)
    assert "送货上门" not in o2["receiver"]


def test_local_only_groups_loaded(rules):
    """#29：rules.yaml group_routing 能读出来。"""
    assert rules.local_only_groups() == ["春明订单接单群", "CC订单群（原碧湖帮）", "碧湖帮（料头及补发）CC专用", "蓝烨订单沟通群"]
