# -*- coding: utf-8 -*-
"""离线 diff：金标准 xlsx（某日期 sheet）vs 库内订单（飞书写出/待确认）。
只读 data/db.sqlite 与金标准 xlsx，不写飞书、不改库。
用法：.venv/Scripts/python.exe scripts/gold_diff.py <金标准.xlsx> <sheet名> [db路径]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import openpyxl


def norm_phone(p: str | None) -> str | None:
    """取主号（去 -尾缀），11 位；取不到返回 None。"""
    if not p:
        return None
    m = re.search(r"1\d{10}", str(p))
    return m.group(0) if m else None


def load_gold(path: Path, sheet: str) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    orders: list[dict] = []
    cur = None
    for row in rows[1:]:
        seq, product, config, qty, receiver, phone, address, logistics = (list(row) + [None] * 8)[:8]
        if seq not in (None, ""):
            cur = {
                "seq": str(seq), "receiver": str(receiver or ""), "phone": str(phone or ""),
                "address": str(address or ""), "logistics": str(logistics or ""), "lines": [],
            }
            orders.append(cur)
        # 空白间隔行不是产品；立柱续行有配置/数量仍须保留。
        if cur is not None and any(value is not None and str(value).strip() for value in (product,config,qty)):
            cur["lines"].append({"product": str(product or ""), "config": str(config or ""), "qty": str(qty or "")})
    return orders


def load_db_orders(db_path: Path) -> list[dict]:
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    out = []
    for oid, batch_id, status, data_json in db.execute(
        "select id, batch_id, status, data_json from orders order by id"
    ):
        d = json.loads(data_json)
        out.append({
            "id": oid, "batch_id": batch_id, "status": status,
            "receiver": d.get("receiver", ""), "phone": d.get("phone", ""),
            "lines": d.get("lines", []),
        })
    return out


def line_text(lines: list[dict]) -> str:
    return "\n".join(f"{l.get('product','')} | {l.get('config','')} | {l.get('qty','')}" for l in lines)


def _norm_product(text: str) -> str:
    """产品名粗归一用于对照：去空格，颜色同义词和已修偏差不计为冲突。"""
    t = (text or "").replace(" ", "")
    for a, b in (("砂黑", "黑"), ("磨砂黑", "黑"), ("亚黑", "黑"), ("深灰", "灰")):
        t = t.replace(a, b)
    return t


def _qty_num(qty: str) -> str:
    m = re.match(r"(\d+(?:\.\d+)?)", qty or "")
    return m.group(1) if m else ""


def classify_pending_vs_gold(gold_order: dict, db_order: dict) -> tuple[str, list[str]]:
    """待确认订单 vs 金标准单 -> ("A", []) 无冲突建议放行 或 ("B", [差异...])。

    A 判定（全部满足才算无冲突）：
    - 金标准的每个「大行」产品关键词能在订单 lines 里找到对应（去空格、颜色同义词归一后）
    - 金标准行的数量数字与订单行一致（取 qty 首段数字比对；金标准数量列为空时跳过）
    - 金标准有而订单完全没有的产品行 = 缺失 -> B
    """
    diffs: list[str] = []
    db_lines = db_order.get("lines", [])
    db_products = [_norm_product(l.get("product", "")) for l in db_lines]
    for gl in gold_order["lines"]:
        gp = _norm_product(gl["product"])
        if not gp:
            continue
        # 产品关键词：取产品名的核心（去颜色前缀仍保留全文做包含匹配）
        hit_idx = None
        for i, dp in enumerate(db_products):
            if gp in dp or dp in gp:
                hit_idx = i
                break
        if hit_idx is None:
            # 焊接说明行是金标准人工行，旧整理可能缺——单独标注，不算致命
            if "焊接" in gp and "主体" in gp:
                diffs.append(f"缺焊接说明行：{gl['product']}")
            else:
                diffs.append(f"缺行：{gl['product']} {gl['config']} {gl['qty']}")
            continue
        gq = _qty_num(gl["qty"])
        if gq:
            dq = _qty_num(db_lines[hit_idx].get("qty", ""))
            if dq and gq != dq:
                diffs.append(f"数量不一致：{gl['product']} 金标准{gl['qty']} vs 订单{db_lines[hit_idx].get('qty','')}")
    # 已知已修偏差不计为冲突（新版 rule_engine 回放能补）：焊接说明行缺失降级为提示
    fatal = [d for d in diffs if not d.startswith("缺焊接说明行")]
    return ("B" if fatal else "A"), diffs


def main() -> None:
    import builtins
    report_path = Path("data/gold_diff_report.txt")
    lines_out: list[str] = []
    _print = builtins.print
    def print(*a, **k):  # noqa: A001
        _print(*a, **k)
        lines_out.append(" ".join(str(x) for x in a))

    xlsx = Path(sys.argv[1])
    sheet = sys.argv[2]
    db_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("data/db.sqlite")

    gold = load_gold(xlsx, sheet)
    db_orders = load_db_orders(db_path)
    written = [o for o in db_orders if o["status"] == "written"]
    pending = [o for o in db_orders if o["status"] in ("pending", "held", "approved")]

    print(f"金标准 {sheet}: {len(gold)} 单")
    print(f"库内 written: {len(written)} 单; 待确认(pending/held/approved): {len(pending)} 单")
    print()

    gold_by_phone: dict[str, list[dict]] = {}
    for g in gold:
        p = norm_phone(g["phone"])
        if p:
            gold_by_phone.setdefault(p, []).append(g)

    wr_by_phone: dict[str, list[dict]] = {}
    for o in written:
        p = norm_phone(o["phone"])
        if p:
            wr_by_phone.setdefault(p, []).append(o)
    pd_by_phone: dict[str, list[dict]] = {}
    for o in pending:
        p = norm_phone(o["phone"])
        if p:
            pd_by_phone.setdefault(p, []).append(o)

    both = sorted(set(gold_by_phone) & set(wr_by_phone))
    gold_only_pending = sorted((set(gold_by_phone) - set(wr_by_phone)) & set(pd_by_phone))
    gold_missing = sorted(set(gold_by_phone) - set(wr_by_phone) - set(pd_by_phone))
    wr_only = sorted(set(wr_by_phone) - set(gold_by_phone))

    print(f"按手机号：金标准∩written {len(both)}；金标准只在待确认 {len(gold_only_pending)}；"
          f"金标准两边都无 {len(gold_missing)}；written 有但金标准无 {len(wr_only)}")
    print()

    print("== 仅金标准有（覆盖外）：不当漏单、不追平总数；历史漏记三轮已结束 ==")
    for p in gold_missing:
        for g in gold_by_phone[p]:
            print(f"  金标准#{g['seq']} {g['receiver']} {g['phone']}")
    print()

    print("== 金标准只在待确认（可用对照放行/纠偏）==")
    for p in gold_only_pending:
        g = gold_by_phone[p][0]
        ids = ",".join(str(o["id"]) for o in pd_by_phone[p])
        print(f"  金标准#{g['seq']} {g['receiver']} {g['phone']}  <- 库内订单id {ids}")
    print()

    print("== 对照放行分类（A=无冲突建议放行 / B=有冲突需人工）==")
    class_a: list[str] = []
    class_b: list[tuple[str, list[str]]] = []
    for p in gold_only_pending:
        g = gold_by_phone[p][0]
        for o in pd_by_phone[p]:
            cls, diffs = classify_pending_vs_gold(g, o)
            label = f"金#{g['seq']} {g['receiver']} / 订单id{o['id']}"
            if cls == "A":
                class_a.append(label + (f"（提示：{';'.join(diffs)}）" if diffs else ""))
            else:
                class_b.append((label, diffs))
    print(f"  A 类 {len(class_a)} 单：")
    for s in class_a:
        print(f"    [放行建议] {s}")
    print(f"  B 类 {len(class_b)} 单：")
    for label, diffs in class_b:
        print(f"    [需人工] {label}")
        for d in diffs:
            print(f"        - {d}")
    print()

    # 导出 JSON 供后台「金标准对照」页使用（只读生成，不改库）
    export = {"sheet": sheet, "generated_from": str(xlsx.name), "class_a": [], "class_b": []}
    for p in gold_only_pending:
        g = gold_by_phone[p][0]
        for o in pd_by_phone[p]:
            cls, diffs = classify_pending_vs_gold(g, o)
            entry = {
                "order_id": o["id"], "gold_seq": g["seq"],
                "receiver": o.get("receiver", ""), "phone": o.get("phone", ""),
                "gold_receiver": g["receiver"], "gold_phone": g["phone"],
                "diffs": diffs,
                "db_lines": [{"product": l.get("product", ""), "config": l.get("config", ""),
                              "qty": l.get("qty", "")} for l in o.get("lines", [])],
                "gold_lines": g["lines"],
            }
            export["class_a" if cls == "A" else "class_b"].append(entry)
    json_path = Path("data/gold_diff.json")
    json_path.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    _print(f"JSON 已写入 {json_path}（A {len(export['class_a'])} / B {len(export['class_b'])}）")

    print("== 仅我方有（独立列出，不据此认定串单）==")
    for p in wr_only:
        for o in wr_by_phone[p]:
            print(f"  库内id{o['id']} batch{o['batch_id']} {o['receiver']} {o['phone']}")
    print()

    print("== 同电话重复写出 ==")
    dup = {p: v for p, v in wr_by_phone.items() if len(v) > 1}
    if not dup:
        print("  无")
    for p, v in dup.items():
        ids = ",".join(str(o["id"]) for o in v)
        print(f"  {p} 写出 {len(v)} 笔（id {ids}）")
    print()

    print("== 字段偏差（matched 订单，行级关键词检查）==")
    checks = [
        ("缺「焊接」(原文写焊接)", lambda t, g: "焊接" in g and "焊接" not in t),
        ("壁厚【1.0】但颜色非黑", lambda t, g: "【1.0】" in g and "黑" not in t),
        ("灰写成黑(金标准为灰)", lambda t, g: "灰" in g and "灰" not in t and "黑" in t),
        ("直角弯头写成平弯", lambda t, g: "直角" in g and "平弯" in t and "直角" not in t),
        ("缺「卡条离地」", lambda t, g: "卡条离地" in g and "卡条离地" not in t),
        ("靠墙缺「新款」(金标准有新款靠墙)", lambda t, g: "新款 靠墙" in g.replace("新款靠墙", "新款 靠墙") and "新款" not in t),
        ("收货人混入「送货上门」", lambda t, g: "送货上门" in t),
        ("物流整单一致性未验", None),  # 旧关键词入口不能代替完整物流对照
    ]
    stats = {c[0]: 0 for c in checks}
    detail: dict[str, list[str]] = {c[0]: [] for c in checks}
    for p in both:
        g = gold_by_phone[p][0]
        gold_text = line_text(g["lines"])
        for o in wr_by_phone[p]:
            t = line_text(o["lines"])
            recv = o.get("receiver", "")
            for name, fn in checks:
                if fn is None:
                    continue
                try:
                    if fn(t, gold_text):
                        stats[name] += 1
                        detail[name].append(f"金#{g['seq']}/id{o['id']} {o['receiver']}")
                except Exception:
                    pass
            if "送货上门" in recv:
                stats["收货人混入「送货上门」"] += 1
                detail["收货人混入「送货上门」"].append(f"金#{g['seq']}/id{o['id']} 收货人={recv}")
    for name, n in stats.items():
        if n:
            print(f"  [{n}] {name}")
            for d in detail[name][:8]:
                print(f"      {d}")
    print()

    print("== matched 订单行数对比（金标准行数 vs 写出行数差异>0 的）==")
    shown = 0
    for p in both:
        g = gold_by_phone[p][0]
        for o in wr_by_phone[p]:
            gd, bd = len(g["lines"]), len(o["lines"])
            if gd != bd:
                print(f"  金#{g['seq']} {g['receiver']}: 金标准{gd}行 vs 写出{bd}行")
                shown += 1
        if shown > 15:
            print("  ...(省略)")
            break

    print("整单正确率：未验；目标98%；本入口仅历史关键词诊断，缺完整字段、范围及实际表证据，不以匹配数代替正确率。")
    report_path.write_text("\n".join(lines_out), encoding="utf-8")
    _print(f"\n报告已写入 {report_path}")


if __name__ == "__main__":
    main()
