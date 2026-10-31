"""规则引擎：对 AI 输出做确定性修正。02 文档第 7 节，验收用例 06 文档 R01~R20。

输入：normalizer 的订单 dict；输出：修正后的订单 dict，每处修正记进 line.fixes。
"""
from __future__ import annotations

import math
import re

from app.pipeline.rule_loader import Rules
from app.pipeline.gold_rules import (
    normalize_lines, add_channel_kits, review as rule_review, preserve_brushed_black,
    normalize_black_lamp_fitting_columns, separate_slot_width,
    normalize_gold_output_names, normalize_gold_channel_names,
    normalize_standalone_30_fittings, normalize_grey_30_cut_columns,
)
from app.pipeline.fitting_terms import (
    normalize_fittings, normalize_gold_fitting_names, resolve_color_placeholders,
)
from app.pipeline.known_terms import normalize_piece_numbers, resolve_verified_terms
from app.pipeline.phone_extensions import normalize_extensions

# 配置里的 "N支N米" / "N只N米"
QTY_RE = re.compile(r"(\d+)\s*[支只]\s*(\d+(?:\.\d+)?)\s*米")

MODIFY_KEYWORDS = re.compile(r"改|撤|取消|不要了|换成|刚才|上面那|作废|重发")

# 免扶手款/平头/耳朵款 不配钻尾（rules.yaml auto_calc.drill_screw.exclude_styles）
NO_DRILL_STYLES = {"mianfushou", "pingtou", "erduo"}


def _fix_symbols(text: str) -> str:
    for sym in ("x", "X", "×", "＊"):
        text = text.replace(sym, "*")
    return text


def _normalize_text(rules: Rules, text: str, fixes: list[str]) -> str:
    """符号统一 + 错别字。"""
    new = _fix_symbols(text)
    if new != text:
        fixes.append("symbol")
    typo_fixed = rules.typo_fix(new)
    if typo_fixed != new:
        fixes.append("typo")
    return typo_fixed


def _normalize_color_in_text(rules: Rules, text: str, fixes: list[str]) -> str:
    """颜色同义词归一：在文本里找已知颜色别名，替换成标准色。

    用正则一次性匹配所有别名和标准色，只替换"不在标准色内部"的别名出现，
    避免把 "黑色" 里的 "黑" 又换成 "黑色"（变成 "黑色色"）。
    """
    colors = rules._d.get("normalize", {}).get("colors", [])
    alias_map: dict[str, str] = {}
    standards = set()
    for g in colors:
        standards.add(g["standard"])
        for alias in g.get("aliases", []):
            if alias != g["standard"]:
                alias_map[alias] = g["standard"]
    if not alias_map:
        return text

    # 长的先匹配（"砂黑" 优先于 "黑"）
    # 拉丝黑是老板表保留的表面工艺，不让其中“黑”再次归一吞掉工艺或制造重复修正。
    all_words = sorted(set(alias_map) | standards | {'拉丝黑','拉丝黑色'}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(w) for w in all_words))

    out = []
    last = 0
    for m in pattern.finditer(text):
        word = m.group()
        out.append(text[last:m.start()])
        if word in alias_map:
            out.append(alias_map[word])
            fixes.append(f"color:{word}->{alias_map[word]}")
        else:
            out.append(word)  # 已是标准色，原样保留
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _calc_total_meters(config: str) -> float | None:
    """从配置里解析所有 N支N米，求和。没有匹配返回 None。"""
    total = 0.0
    found = False
    for n, m in QTY_RE.findall(config):
        total += int(n) * float(m)
        found = True
    return total if found else None


def _fmt_meter(v: float) -> str:
    return f"{int(v)}米" if v == int(v) else f"{v:g}米"


def _split_post_slots(order: dict, fixes: list[str]) -> None:
    """立柱多槽分行：一行 config 里有多个槽型的拆成多行。"""
    new_lines = []
    for line in order["lines"]:
        if line.get("kind") != "post":
            new_lines.append(line)
            continue
        config = line.get("config", "")
        # 找 "N支/只 ... 槽" 重复出现的模式（如 "2支双槽 6支单槽" 或 "2双6单" 已被 AI 拆开的情况）
        slot_parts = re.findall(r"(\d+)\s*([支只])?\s*(单槽|双槽|转角|三槽|无槽)", config)
        # 也兼容 "2双6单" 这种简写
        if not slot_parts:
            short = re.findall(r"(\d+)\s*(双|单)", config)
            slot_map = {"单": "单槽", "双": "双槽"}
            slot_parts = [(n, "支", slot_map[s]) for n, s in short]
        if len(slot_parts) <= 1:
            new_lines.append(line)
            continue
        # 拆行
        base_config = config
        # 去掉槽型部分，保留壁厚/高度前缀（如 【3.0】1170高）
        prefix = re.split(r"\d+\s*[支只]?\s*(?:单槽|双槽|转角|三槽|无槽|双|单)", config)[0].strip()
        for i, (n, unit, slot) in enumerate(slot_parts):
            unit = unit or "支"
            new_line = dict(line)
            new_line["config"] = f"{prefix} {slot}".strip()
            new_line["qty"] = f"{n}{unit}"
            if i > 0:
                new_line["product"] = ""  # 续行 B 列留空
            new_line.setdefault("fixes", []).append("slot_split")
            new_lines.append(new_line)
        fixes.append("slot_split")
    order["lines"] = new_lines


def _post_style(order: dict, line: dict) -> str:
    """从 rules 的 posts.mapping 里找这个立柱产品的 style。"""
    product = re.sub(r'焊接(?:款)?\s*','',line.get("product", ""))
    posts = Rules_current.posts.get("mapping", [])
    for item in posts:
        b = item.get("b", "")
        # 模板里的 {颜色} 替换成任意颜色来匹配
        pattern = re.escape(b).replace(re.escape("{颜色}"), r"\S+")
        if re.search(pattern, product):
            return item.get("style", "")
    return ""


def _drill_multiplier(order, rules):
    """只启用金标准已证的内卡槽无头/H款，混入大柱或未知款式仍保留边界。"""
    config=rules.auto_calc.get('drill_screw',{})
    allowed=set(config.get('catalog_post_styles',[]))
    previous='';styles=[]
    for line in order['lines']:
        if line.get('kind')!='post':continue
        product=line.get('product') or previous
        previous=product
        qty = line.get('qty', '')
        # A finite display-unit correction must preserve its original counting
        # semantics on repeated processing; it does not extend the catalogue.
        retained_count_unit = (
            'gold:g27b_post_units' in line.get('fixes', [])
            and re.fullmatch(r'\d+个', qty)
            and line.get('source_g27b_qty') == qty[:-1] + '支')
        # The A view is recomputed by the complete-profile guard before every
        # calculation; a historical marker alone cannot retain this allowance.
        retained_a_count_unit = (
            'gold:remaining_a_446_precalc' in line.get('fixes', [])
            and re.fullmatch(r'\d+个', qty)
            and line.get('remaining_a_446_calculation_qty') == qty[:-1] + '支')
        if '内卡槽立柱' not in product or not (re.fullmatch(r'\d+[支只]', qty) or retained_count_unit or retained_a_count_unit):
            return 1
        styles.append(_post_style(order,dict(line,product=product)))
    multiplier=config.get('catalog_per_point',1)
    return multiplier if styles and set(styles)<=allowed and type(multiplier) is int and multiplier>0 else 1


def _fold_post_continuations(order: dict) -> None:
    """同一原文、同规格多槽已被模型拆行时，续行不重复产品名。"""
    previous = None
    for line in order['lines']:
        current = dict(line)
        if previous and line.get('kind') == previous.get('kind') == 'post':
            raw = line.get('raw', '')
            a = re.fullmatch(r'(.*?)(单槽|双槽|转角|三槽|无槽)', previous.get('config', '').strip())
            b = re.fullmatch(r'(.*?)(单槽|双槽|转角|三槽|无槽)', line.get('config', '').strip())
            if (raw and raw == previous.get('raw') and line.get('product')
                    and line['product'] == previous.get('product') and a and b
                    and a[1].strip() == b[1].strip() and a[2] != b[2]):
                def supported(row, slot):
                    word = {'单槽':'单','双槽':'双'}.get(slot, slot)
                    count = re.fullmatch(r'(\d+)[支只]', row.get('qty', ''))
                    return count and re.search(r'(?<!\d)'+count[1]+r'\s*'+word, raw)
                if supported(previous, a[2]) and supported(line, b[2]):
                    line['product'] = ''
                    line.setdefault('fixes', []).append('source:post_continuation')
        previous = current


def _restore_post_count_units(order: dict) -> None:
    """本行槽数明确时补展示单位，避免已证配套公式因裸数字退回。"""
    aliases = {'单': '单槽', '双': '双槽', '转': '转角'}
    for line in order['lines']:
        qty, raw = line.get('qty', ''), line.get('raw', '')
        slot = re.search(r'(单槽|双槽|转角|三槽|无槽)$', line.get('config', '').strip())
        if (line.get('kind') != 'post' or not re.fullmatch(r'[1-9]\d*', qty) or not slot
                or re.search(r'不要|取消|修改|改为|改成|撤', raw)):
            continue
        tokens = re.findall(r'(?<![\d.])(\d+)\s*(?:支|只)?\s*'
                            r'(单槽|双槽|转角|三槽|无槽|单|双|转)(?![\u4e00-\u9fff])', raw)
        counts = [n for n, name in tokens if aliases.get(name, name) == slot[1]]
        if counts == [qty]:
            line['qty'] = qty + '支'
            line.setdefault('fixes', []).append('source:post_count_unit')


Rules_current: Rules  # process() 里赋值，供 _post_style 用


def _count_posts_and_walls(order: dict) -> tuple[int, int, str]:
    """数立柱总支数和靠墙封口个数，返回 (post_count, wall_count, post_color)。"""
    post_count = 0
    post_color = ""
    for line in order["lines"]:
        if line.get("kind") == "post":
            m = re.match(r"(\d+)", line.get("qty", ""))
            if m:
                post_count += int(m.group(1))
            if not post_color:
                cm = re.match(r"(黑色|灰色|咖啡色|白色|肌肤黑|金属灰|香槟金)", line.get("product", ""))
                if cm:
                    post_color = cm.group(1)
    wall_count = 0
    for line in order["lines"]:
        product = line.get("product", "")
        if "靠墙" in product and ("封口" in product or (
                "盖子+固定件" in product and "36*88" in line.get("config", "")) or (
                '盖子+角码' in product and '20*20配件' in line.get('config',''))):
            m = re.match(r"(\d+)", line.get("qty", ""))
            if m:
                wall_count += int(m.group(1))
    return post_count, wall_count, post_color


def _has_line(order: dict, *keywords: str) -> bool:
    return any(all(k in l.get("product", "") for k in keywords) for l in order["lines"])


def _add_line(order: dict, kind: str, product: str, qty: str, config: str = "", fixes: list | None = None):
    order["lines"].append({"kind": kind, "product": product, "config": config,
                           "qty": qty, "raw": "程序补算", "confidence": 1.0,
                           "fixes": fixes or ["auto_calc"]})


def process(order: dict, rules: Rules) -> dict:
    """对一个订单做 9 步确定性修正，返回修正后的订单。"""
    global Rules_current
    Rules_current = rules
    from app.pipeline.logistics_notes import collect_notes
    collect_notes(order)
    resolve_color_placeholders(order, rules)

    # 1. 文本归一（每行的 product/config）
    for line in order["lines"]:
        fixes = line.setdefault("fixes", [])
        for field in ("product", "config"):
            if line.get(field):
                new = _normalize_text(rules, line[field], fixes)
                new = _normalize_color_in_text(rules, new, fixes)
                line[field] = new

    normalize_gold_channel_names(order)
    normalize_piece_numbers(order)
    from app.pipeline.source_lengths import restore_length_column
    restore_length_column(order)
    from app.pipeline.verified_r3 import normalize_verified_products
    normalize_verified_products(order)
    normalize_lines(order)
    normalize_fittings(order,rules)
    from app.pipeline.verified_r11 import normalize_verified_products as normalize_r11
    normalize_r11(order)

    # 3. 立柱多槽分行（先做，后面的计数依赖拆行结果）
    order_fixes = order.setdefault("fixes", [])
    _restore_post_count_units(order)
    _split_post_slots(order, order_fixes)
    from app.pipeline.source_post_slots import reconcile_slots
    reconcile_slots(order)
    _fold_post_continuations(order)

    # 2. 数量：扶手/铝槽从配置算总米数
    for line in order["lines"]:
        if line.get("kind") in ("rail", "channel"):
            separate_slot_width(line)
            config = line.get("config", "")
            total = _calc_total_meters(config)
            if total is not None:
                calc = _fmt_meter(total)
                if line.get("qty") != calc:
                    line.setdefault("fixes", []).append("qty_fixed")
                    line["_qty_diff"] = (line.get("qty"), calc)  # 供 validator 判断差异
                    line["qty"] = calc

    from app.pipeline.gold_remaining_a import normalize_remaining_a_precalc
    order = normalize_remaining_a_precalc(order)
    from app.pipeline.gold_products_20261030 import normalize_evidenced_post_precalc
    normalize_evidenced_post_precalc(order)
    post_count, wall_count, post_color = _count_posts_and_walls(order)
    has_weld = any("焊接" in l.get("product", "") for l in order["lines"] if l.get("kind") == "post")
    is_h = any("H款" in l.get("product", "") for l in order["lines"] if l.get("kind") == "post")
    catalog_posts=[l for l in order['lines'] if l.get('kind')=='post']
    catalog_no_post_kit=bool(catalog_posts) and all('gold:950_black_120_bundle' in l.get('fixes',[]) for l in catalog_posts)

    # 4. 螺丝补算（订单里没有对应螺丝行时补）
    if post_count > 0 and not catalog_no_post_kit:
        if not _has_line(order, "膨胀"):
            n = (post_count + math.ceil(wall_count / 2)) * 4
            _add_line(order, "screw", "膨胀螺丝", f"{n}个")
        # 钻尾：免扶手/平头/耳朵款不配
        style = ""
        for line in order["lines"]:
            if line.get("kind") == "post":
                style = _post_style(order, line)
                break
        if style not in NO_DRILL_STYLES:
            # 老板表10.20序36、10.21序5/43、10.22序9证实无头/H款每点4个。
            # 大柱/未知款式不套用这条目录规则，显式数量不覆盖。
            multiplier=_drill_multiplier(order,rules)
            n = (post_count + wall_count)*multiplier
            expected_qty = f"{n}个"
            if not _has_line(order, "钻尾"):
                _add_line(order, "screw", "小号钻尾螺丝", expected_qty)
            else:
                # 历史程序生成的数量可保留，但不能冒充当前公式已经算平。
                for line in order['lines']:
                    if (line.get('kind') == 'screw' and line.get('product') == '小号钻尾螺丝'
                            and line.get('raw') == '程序补算' and 'auto_calc' in line.get('fixes', [])):
                        marker='review:legacy_drill_count'
                        if (multiplier>1 and line.get('qty') != expected_qty
                                and sum('钻尾' in l.get('product','') for l in order['lines'])==1):
                            # 新金标10.23再次验证目录公式；仅修有生成标记的旧缓存。
                            line.setdefault('source_previous_drill_qty',line.get('qty'))
                            line['qty']=expected_qty
                            if 'gold:r8_cached_drill' not in line['fixes']:
                                line['fixes'].append('gold:r8_cached_drill')
                        if line.get('qty') != expected_qty:
                            line['rule_expected_qty'] = expected_qty
                            if marker not in line['fixes']:
                                line['fixes'].append(marker)
                            rule_review(order, f"钻尾数量{line.get('qty')}与配套计算{expected_qty}不一致，待核依据")
                        elif multiplier>1 and marker in line['fixes']:
                            line['fixes'].remove(marker)
                            line.pop('rule_expected_qty',None)
                            if not any(other.get('product')=='小号钻尾螺丝' and other.get('raw')=='程序补算'
                                    and 'auto_calc' in other.get('fixes',[]) and other.get('qty')!=expected_qty
                                    for other in order['lines']):
                                order['deterministic_review_reasons']=[r for r in order.get('deterministic_review_reasons',[])
                                    if r!='请确认钻尾螺丝数量' and not r.startswith('钻尾数量')]
            if (any('gold:4060_rectangular_post' in line.get('fixes',[]) for line in order['lines'])
                    and any(line.get('product')=='小号钻尾螺丝' and line.get('raw')=='程序补算'
                            and 'auto_calc' in line.get('fixes',[]) for line in order['lines'])):
                # 方管目录有反例，不能把回退计算或旧缓存相等当作数量已证。
                rule_review(order,'请确认钻尾螺丝数量')

    # 5. 大柱原文明示自带平盖时，不再重复补普通立柱盖子。
    included_caps = 0
    for line in order["lines"]:
        if (line.get('kind')=='post' and {'gold:4060_rectangular_post','gold:950_black_120_bundle'}.intersection(line.get('fixes',[]))
                and re.fullmatch(r'\d+支',line.get('qty',''))):
            # 老板方管目录不列普通内卡槽立柱盖子；手写盖子行仍保留。
            included_caps+=int(line['qty'][:-1])
            continue
        if line.get("kind") != "post" or "大柱+平盖" not in line.get("product", ""):
            continue
        raw = line.get("raw", "")
        match = re.search(r"四方大柱\s*\+\s*平盖\s*(\d+)支", raw)
        if match and line.get("qty") == match[1]+"支" and not re.search(r"不要|不带|取消|改为|改成", raw):
            included_caps += int(match[1])
    cap_count = max(0, post_count-included_caps)
    caps = [line for line in order["lines"] if "立柱盖子" in line.get("product", "")]
    # 旧缓存仅在明确程序补算且仍等于旧总支数时重算；不覆盖手写或不明数量。
    if included_caps and len(caps) == 1:
        cap = caps[0]
        if cap.get("raw") == "程序补算" and "auto_calc" in cap.get("fixes", []) and cap.get("qty") == f"{post_count}个":
            if cap_count:
                cap["qty"] = f"{cap_count}个"
                cap.setdefault("fixes", []).append("source:included_flat_cap")
            else:
                order.setdefault('source_removed_auto_lines',[]).append(dict(cap))
                order["lines"].remove(cap)
    if cap_count > 0 and not any(
            line.get("kind") == "cap" or "立柱盖子" in line.get("product", "")
            for line in order["lines"]):
        cap_name = rules.posts.get("cap", {}).get("template", "{颜色}立柱盖子").replace("{颜色}", post_color)
        _add_line(order, "cap", cap_name, f"{cap_count}个")

    # 6. 焊接描述行
    # 金标准对照（10.21）：所有 H款 立柱都是焊接工艺，即使原文没写「焊接」，
    # 产品名也要写「焊接 H款」并补焊接说明行（老板确认规则5：必须保留「焊接」+正确款式）。
    has_weld = has_weld or is_h
    if has_weld and not any(l.get("kind") == "weld_note" for l in order["lines"]):
        weld_cfg = rules.posts.get("weld_note", {})
        paired_heights = {line['source_weld_ground_cm'] for line in order['lines']
                          if line.get('kind') == 'post' and {'gold:rocker_4060_pair','gold:black_rocker_catalog'}.intersection(line.get('fixes', []))
                          and line.get('source_weld_ground_cm')}
        ground_cm = next(iter(paired_heights)) if len(paired_heights) == 1 else str(weld_cfg.get("default_ground_cm", 10))
        text = weld_cfg.get("default", "焊接 新款三槽主体 卡条离地{离地}公分").replace(
            "{离地}", ground_cm)
        if is_h:
            text += weld_cfg.get("h_suffix", " H头用锁螺丝的")
        # 插在立柱行之后
        idx = max((i for i, l in enumerate(order["lines"]) if l.get("kind") == "post"), default=-1)
        order["lines"].insert(idx + 1, {"kind": "weld_note", "product": text,
                                        "config": "", "qty": "", "raw": "程序补算",
                                        "confidence": 1.0, "fixes": ["auto_calc"]})

    add_channel_kits(order, rules)
    from app.pipeline.verified_r7 import h960_six_meter_kit
    h960_six_meter_kit(order)

    # 7. 排序
    order_map = {k: i for i, k in enumerate(rules.line_order)}
    order["lines"] = sorted(
        enumerate(order["lines"]),
        key=lambda p: (order_map.get(p[1].get("kind", "other"), 99), p[0]),
    )
    order["lines"] = [l for _, l in order["lines"]]

    # 8. 收货信息清洗
    normalize_extensions(order)
    if order.get("receiver"):
        # 物流话术误入收货人（如「示例收件人347」）：剥掉「送货上门」前缀
        recv = order["receiver"]
        if "送货上门" in recv:
            m = re.search(r"《([^》]+)》", recv)
            order["receiver"] = m.group(1) if m else re.sub(r"送货上门[《\s]*", "", recv).strip("》 ").strip()
            if order["receiver"] != recv:
                order_fixes.append("receiver:送货上门")

    # 9. 物流列：渠道标记只进内部分类字段 channel_tags，不写 logistics
    # （物流列写出层恒空，店名不进表格——问题清单 #30）
    raw_all = " ".join(l.get("raw", "") for l in order["lines"]) + " " + order.get("notes", "")
    tags = order.setdefault("channel_tags", [])
    for tag, full in rules._d.get("channels_tag", {}).items():
        if tag in raw_all and full not in tags:
            tags.append(full)
            order_fixes.append(f"channel:{tag}")

    preserve_brushed_black(order)
    normalize_black_lamp_fitting_columns(order)
    resolve_verified_terms(order,rules)
    from app.pipeline.verified_r8 import normalize_catalog_names
    normalize_catalog_names(order)
    normalize_r11(order)
    from app.pipeline.verified_r10 import normalize_verified_catalog
    normalize_verified_catalog(order)
    from app.pipeline.verified_r12b import normalize_catalog
    normalize_catalog(order)
    from app.pipeline.verified_r19 import normalize_catalog as normalize_r19
    normalize_r19(order)
    normalize_gold_fitting_names(order)
    normalize_gold_output_names(order)
    # Restore a missing display unit only when this line's explicit source
    # contains the same unique piece count. Never infer counts from formulas.
    for line in order['lines']:
        qty=str(line.get('qty') or '')
        if (line.get('kind') in ('rail_fitting','channel_fitting','cap','screw')
                and re.fullmatch(r'\d+',qty)
                and set(re.findall(r'(?<!\d)(\d+)\s*个',line.get('raw') or ''))=={qty}):
            line['qty']=qty+'个'
            line.setdefault('fixes',[]).append('source:explicit_piece_unit')
        elif line.get('kind') == 'rail_fitting' and re.fullmatch(r'\d+', qty):
            # Gold 10.26 #4: 1平弯 / 4竖弯 / 4墙固 are piece counts.
            # Require the whole source fragment and the corresponding product;
            # mixed fragments, questions and different counts remain unchanged.
            counted = re.fullmatch(r'\s*(\d+)\s*(平弯|竖弯|墙固)\s*', line.get('raw') or '')
            names = {'平弯': '压铸配件 平弯', '竖弯': '压铸配件 竖直角', '墙固': '靠墙封口'}
            if (counted and counted.group(1) == qty
                    and line.get('product') == names[counted.group(2)]):
                line['qty'] = qty + '个'
                line.setdefault('fixes', []).append('source:count_before_fitting')
    normalize_standalone_30_fittings(order)
    normalize_grey_30_cut_columns(order)
    from app.pipeline.gold_rules import normalize_today_admitted_cases
    normalize_today_admitted_cases(order)
    from app.pipeline.gold_remaining_a import normalize_remaining_a_cases
    order = normalize_remaining_a_cases(order)
    from app.pipeline.gold_remaining_b import normalize_remaining_b_cases
    order = normalize_remaining_b_cases(order)
    from app.pipeline.gold_products_20261030 import normalize_evidenced_product_columns
    normalize_evidenced_product_columns(order)
    from app.diagnostician.learning import apply_learned_cases
    from app.pipeline.logistics_notes import normalize_logistics_address
    return normalize_logistics_address(apply_learned_cases(order, rules))
