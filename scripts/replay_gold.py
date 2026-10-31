"""离线只读对照：消息JSON导出 + 绑定提示摘要的模型响应缓存 → 内存回放。

不读取配置/数据库，不连接模型或飞书，不提供任何写库/写表入口。
默认仅估算；--responses 才使用本地缓存回放，模型调用费用始终为零。
估算价格由调用者提供（每百万token），不是当前服务商报价。
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from app.models import Message
from app.pipeline.normalizer import build_messages, prepare_result
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import Rules
from app.pipeline.segmenter import segment
from app.pipeline.validator import validate
from scripts.gold_diff import load_gold
from scripts.gold_diagnosis import diagnosis
from scripts.gold_identity import address, compatible_phone, field_differences, same_person

LOCAL_TZ = timezone(timedelta(hours=8))


def comparison_window(day: str) -> tuple[int, int]:
    """消息登记自然日 [D 00:00,D+1 00:00)，北京时间。"""
    start = datetime.combine(date.fromisoformat(day), time(0), LOCAL_TZ)
    return int(start.timestamp()*1000), int((start+timedelta(days=1)).timestamp()*1000)


def load_messages(path: Path, day: str, rooms: set[str]) -> list[Message]:
    if not rooms:
        raise ValueError("必须明确指定导出样本的群范围")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("消息导出必须是JSON数组")
    start, end = comparison_window(day)
    seen, messages = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("msgid"), str) or not row["msgid"]:
            raise ValueError("消息缺少有效msgid")
        if row["msgid"] in seen:
            raise ValueError("导出消息msgid重复")
        seen.add(row["msgid"])
        if row.get("roomid") not in rooms or row.get("revoked"):
            continue
        if not isinstance(row.get("msgtime"), int):
            raise ValueError("消息时间必须是毫秒整数")
        if start <= row["msgtime"] < end:
            messages.append(Message(**{k: row.get(k, default) for k, default in {
                "msgid": "", "seq": 0, "roomid": "", "sender": "", "msgtime": 0,
                "msgtype": "text", "content": "", "revoked": False}.items()}))
    return sorted(messages, key=lambda m: (m.msgtime, m.seq, m.msgid))


def build_plan(messages: list[Message], rules_text: str) -> list[dict]:
    plan = []
    for group in segment(messages):
        prompt = build_messages(rules_text, group)
        serialized = json.dumps(prompt, ensure_ascii=False, sort_keys=True).encode("utf-8")
        disposition = "model"
        if group.get("non_order") or group.get("quote_kind") == "chat":
            disposition = "non_order"
        elif group.get("supplement"):
            disposition = "supplement_not_replayed"
        elif any(m.msgtype not in ("text",) for m in group["msgs"]):
            disposition = "media_not_replayed"
        plan.append({"key": hashlib.sha256(serialized).hexdigest(), "group": group,
                     "prompt_bytes": len(serialized), "disposition": disposition})
    return plan


def estimate(plan: list[dict], *, input_price=None, output_price=None, output_tokens=8192) -> dict:
    if output_tokens <= 0 or any(p is not None and (not Decimal(str(p)).is_finite() or p < 0)
                                 for p in (input_price, output_price)):
        raise ValueError("估算参数必须非负，输出预算必须为正")
    groups = [p for p in plan if p["disposition"] == "model"]
    # UTF8字节数作保守token预算，另留格式/重试提示预算；不是实际分词计费。
    tokens_in = sum((p["prompt_bytes"]+1024)*2 for p in groups)
    tokens_out = len(groups)*output_tokens*2
    cost = None
    if input_price is not None and output_price is not None:
        cost = float((Decimal(tokens_in)*Decimal(str(input_price)) +
                      Decimal(tokens_out)*Decimal(str(output_price)))/Decimal(1_000_000))
    return {"messages": sum(len(p["group"]["msgs"]) for p in plan), "groups": len(plan),
            "model_groups": len(groups), "max_attempts": 2, "input_token_budget": tokens_in,
            "output_token_budget": tokens_out, "estimated_cost_ceiling": cost,
            "estimate_note": "按调用者价格及输出预算粗估，非供应商报价或保证上限；实际离线调用费用0",
            "cache_keys": [p["key"] for p in groups]}


def run_replay(plan: list[dict], responses: dict, rules: dict) -> dict:
    orders, failures, skipped = [], [], []
    for item in plan:
        key, group = item["key"], item["group"]
        if item["disposition"] != "model":
            skipped.append({"key": key, "reason": item["disposition"]})
            continue
        try:
            cached = deepcopy(responses.get(key))
            if not isinstance(cached, dict) or cached.get("error"):
                raise ValueError("缺少有效缓存响应")
            result = prepare_result(cached, group)
            if not result["orders"]:
                raise ValueError("模型返回空订单，须核查是否丢单")
            current_ids = {m.msgid for m in group["msgs"]}
            completed = []
            for order in result["orders"]:
                if not order["lines"]:
                    raise ValueError("空明细订单须核查")
                if not set(order["source_msgids"]).issubset(current_ids):
                    raise ValueError("响应引用了本组以外的消息")
                processed = process(order, Rules(rules))
                _, reasons = validate(processed, 0.8)
                processed["review_reasons"] = reasons
                completed.append(processed)
            orders.extend(completed)
        except (ValueError, TypeError, KeyError, AttributeError):
            # 不回显缓存正文、配置或异常原文，防止输入中的敏感内容进入日志。
            failures.append({"key": key, "reason": "缓存缺失、结构无效、来源错误或规则处理失败"})
    return {"orders": orders, "failures": failures, "skipped": skipped,
            "complete": not failures and not any(s["reason"] != "non_order" for s in skipped)}


def _text(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _line(line) -> tuple[str, str, str]:
    qty = _text(line.get("qty"))
    numeric = re.fullmatch(r"(\d+(?:\.\d+)?)(.*)", qty)
    if numeric:
        qty = format(Decimal(numeric[1]).normalize(), 'f') + numeric[2]
    config = _text(line.get("config"))
    piece = r"\d+[支只]\d+(?:\.\d+)?米"
    prefix=r'(?:【槽口\d+】)?'
    if re.fullmatch(prefix+piece+r"(?:[,，、+＋]?"+piece+r")*", config):
        config = re.sub(r"[,，、+＋]", "", config)
        config = config.replace('只','支')  # 仅支数×米数配置的等价计件词，不改实际输出或D列单位。
    return _text(line.get("product")), config, qty


def _signature(order) -> tuple:
    return (tuple(_text(order.get(k)) for k in ("receiver", "phone", "address")),
            Counter(_line(line) for line in order.get("lines", [])))


def comparison_lines(order):
    """展开立柱续行的展示省略，槽型行换序不算产品差；不填业务字段。"""
    result = []
    previous = ''
    for source in order.get('lines', []):
        line = dict(source)
        if line.get('product'):
            previous = line['product']
        elif ('立柱' in previous and re.search(r'\d+(?:\.\d+)?(?:米)?高.*(?:单槽|双槽|转角|三槽|无槽)', line.get('config', ''))):
            line['product'] = previous
        result.append(_line(line))
    return Counter(result)


def compare_orders(gold: list[dict], actual: list[dict], *, messages=None) -> dict:
    """严格一对一；金标准独有按覆盖外列示，不推断漏单。"""
    from scripts.accuracy_report import accuracy_summary, whole_order_correct, comparison_checks, dimension_summary
    remaining = set(range(len(actual)))
    paired_checks = []
    whole_correct = 0
    exact, missing, differences, ambiguous = 0, [], [], []
    for gi, expected in enumerate(gold):
        hits = [i for i in sorted(remaining) if compatible_phone(expected.get('phone'), actual[i].get('phone'))]
        # 先用收货身份消歧，不能靠产品相同把同电话另一人的订单当更正来源。
        people = [i for i in hits if same_person(expected, actual[i])]
        addresses = [i for i in people if address(expected, actual[i])
                     and address(expected, actual[i]) == address(actual[i], expected)]
        hits = addresses or people or hits
        # 缺分机的缓存同时对应多个同身份金标准单时，不按遍历顺序抢占。
        if len(hits) == 1:
            observed = actual[hits[0]]
            contenders = [g for g in gold if same_person(g, observed)
                          and address(g, observed) == address(observed, g)]
            if len(contenders) > 1 and field_differences(expected, observed) == ['phone']:
                ambiguous.append({'gold_index':gi, 'candidates':hits, 'cause':'缺分机且同身份金标准多单，未猜归属'})
                continue
        identical = [i for i in hits if not field_differences(expected, actual[i])
                     and comparison_lines(expected) == comparison_lines(actual[i])]
        if identical:
            whole_correct += whole_order_correct(expected, actual[identical[0]], fields_equal=True, lines_equal=True)
            paired_checks.append(dict(gold_index=gi, actual_index=identical[0],
                **comparison_checks(expected, actual[identical[0]], fields_equal=True, lines_equal=True)))
            remaining.remove(identical[0])
            exact += 1
        elif len(hits) == 1:
            index = hits[0]
            remaining.remove(index)
            observed = actual[index]
            want, got = comparison_lines(expected), comparison_lines(observed)
            paired_checks.append(dict(gold_index=gi, actual_index=index,
                **comparison_checks(expected, observed, fields_equal=not field_differences(expected, observed), lines_equal=want == got)))
            differences.append({"gold_index": gi, "actual_index": index,
                "fields": field_differences(expected, observed),
                "missing_lines": list((want-got).elements()), "extra_lines": list((got-want).elements())})
            differences[-1].update(diagnosis(expected, observed, differences[-1], messages or {}))
        elif hits:
            ambiguous.append({"gold_index": gi, "candidates": hits, "cause": "同电话多单，无法唯一对应"})
        else:
            missing.append({"gold_index": gi, "cause": "本窗口未配对；先核收录范围及来源时间，不自动证明覆盖外或漏单"})
    product = sum(bool(d['missing_lines'] or d['extra_lines']) for d in differences)
    accuracy = accuracy_summary(len(gold), len(actual), whole_correct, len(actual)-len(remaining))
    accuracy['paired_dimensions'] = dimension_summary(paired_checks)
    return {"accuracy": accuracy, 'paired_checks': paired_checks,
            "exact": exact, "differences": differences, "missing": missing,
            "ambiguous": ambiguous, "extra": sorted(remaining),
            "five_categories": {"双方精确": exact, "产品差": product,
                "仅联系人差": len(differences)-product, "仅我方有": len(remaining),
                "仅金标准有（覆盖外）": len(missing)},
            "coverage_note": "覆盖外是旧兼容分类名，不自动证明范围排除；先核四群收录范围，再按来源时间及三轮规则查证，未知保留，不强配"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--messages", type=Path, required=True, help="已有消息导出JSON；不支持数据库")
    parser.add_argument("--room", action="append", required=True)
    parser.add_argument("--rules", type=Path, default=Path(__file__).resolve().parents[1]/"rules/rules.yaml")
    parser.add_argument("--responses", type=Path, help="按提示摘要索引的模型响应缓存JSON")
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--sheet")
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--output-price", type=float)
    parser.add_argument("--output-tokens", type=int, default=8192)
    args = parser.parse_args(argv)
    try:
        if bool(args.gold) != bool(args.sheet):
            raise ValueError("金标准文件与sheet须同时提供")
        rules_text = args.rules.read_text(encoding="utf-8")
        selected_messages = load_messages(args.messages, args.date, set(args.room))
        plan = build_plan(selected_messages, rules_text)
        budget = estimate(plan, input_price=args.input_price, output_price=args.output_price,
                          output_tokens=args.output_tokens)
        print(json.dumps({"estimate": budget}, ensure_ascii=False))
        if args.responses:
            responses = json.loads(args.responses.read_text(encoding="utf-8"))
            if not isinstance(responses, dict):
                raise ValueError("缓存必须是JSON对象")
            result = run_replay(plan, responses, yaml.safe_load(rules_text) or {})
            if args.gold:
                result["comparison"] = compare_orders(load_gold(args.gold, args.sheet), result["orders"],
                    messages={m.msgid: {'roomid':m.roomid,'msgtime':m.msgtime,'content':m.content} for m in selected_messages})
                accuracy = result['comparison']['accuracy']
                result['comparison_passed'] = bool(result['complete'] and accuracy['gold_orders']
                    and accuracy['correct_orders'] == accuracy['gold_orders'] == accuracy['actual_orders'])
                result['comparison_passed_basis'] = '仅本次输入逐单全字段一致且无待核；不代替来源范围核清或98%正式验收'
            # 控制台报告不打印完整订单及收货信息。
            result["order_count"] = len(result.pop("orders"))
            print(json.dumps(result, ensure_ascii=False))
            if not result["complete"]:
                return 2
            return 3 if result.get("comparison_passed") is False else 0
        return 0
    except Exception:
        print("输入不完整或格式错误；未调用外部服务、未写库或写表", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
