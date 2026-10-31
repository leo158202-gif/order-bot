"""样本集回放评测：跑 tests/samples/ 下全部样本，输出准确率报告。

用法：PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe scripts\\eval_samples.py

输出：
- 自动通过的订单数、逐格（B/C/D/E/F/G/H）正确率（目标 ≥95%）
- expect_pending 的样本是否都进了待确认（要求 100%）
- 每个错误格子的对比
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from app.pipeline.normalizer import normalize_group
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.segmenter import segment
from app.pipeline.validator import validate
from app.models import Message

FIELDS = ["product", "config", "qty"]  # B/C/D


def load_samples():
    samples = []
    for f in sorted(Path("tests/samples").glob("*.yaml")):
        samples.append(yaml.safe_load(f.read_text(encoding="utf-8")))
    return samples


def sample_to_messages(sample) -> list[Message]:
    msgs = []
    for i, m in enumerate(sample["messages"]):
        msgs.append(Message(msgid=f"{sample['id']}-{i}", seq=i, roomid="sample",
                            sender=m["sender"], msgtime=1000000 + i * 1000,
                            msgtype="text", content=m["text"]))
    return msgs


def main():
    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    rules = load_rules()
    rules_text = Path("data/rules.yaml").read_text(encoding="utf-8")
    samples = load_samples()
    if not samples:
        print("tests/samples/ 下没有样本")
        return

    auto_ok = 0
    pending = 0
    cell_total = 0
    cell_right = 0
    errors = []
    pending_correct = 0
    pending_expect = 0

    for sample in samples:
        expect_pending = sample.get("expect_pending", False)
        if expect_pending:
            pending_expect += 1
        groups = segment(sample_to_messages(sample))
        for g in groups:
            result = normalize_group(g, rules_text, cfg["deepseek"])
            if result.get("error"):
                pending += 1
                if expect_pending:
                    pending_correct += 1
                continue
            for o in result["orders"]:
                o = process(o, rules)
                status, reasons = validate(o, cfg["deepseek"]["confidence_threshold"])
                if status == "pending":
                    pending += 1
                    if expect_pending:
                        pending_correct += 1
                    print(f"[待确认] {sample['id']}: {'；'.join(reasons)}")
                    continue
                auto_ok += 1
                # 逐格对比（第一个期望订单）
                if sample.get("expected"):
                    exp = sample["expected"][0]
                    for field in ("receiver", "phone", "address", "logistics"):
                        cell_total += 1
                        if o.get(field, "").strip() == (exp.get(field) or "").strip():
                            cell_right += 1
                        else:
                            errors.append((sample["id"], field, exp.get(field), o.get(field)))
                    exp_lines = exp.get("lines", [])
                    for i, (el, al) in enumerate(zip(exp_lines, o["lines"])):
                        for j, field in enumerate(FIELDS):
                            cell_total += 1
                            if (al.get(field) or "") == (el[j] or ""):
                                cell_right += 1
                            else:
                                errors.append((sample["id"], f"行{i+1}.{field}", el[j], al.get(field)))

    print("\n===== 准确率报告 =====")
    print(f"样本数：{len(samples)}，自动通过订单：{auto_ok}，待确认：{pending}")
    if cell_total:
        print(f"逐格正确率：{cell_right}/{cell_total} = {cell_right/cell_total:.1%}（目标 ≥95%）")
    if pending_expect:
        print(f"应进待确认的样本：{pending_correct}/{pending_expect} 正确识别（要求 100%）")
    if errors:
        print(f"\n错误格子（{len(errors)} 个）：")
        for sid, field, exp, act in errors:
            print(f"  {sid} {field}: 期望「{exp}」实际「{act}」")


if __name__ == "__main__":
    main()
