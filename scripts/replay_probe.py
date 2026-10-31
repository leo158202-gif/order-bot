"""用数据库里的真实消息回放整理流程：segment -> normalize -> rule_engine，打印结果。

用法：PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe scripts\\replay_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from app.db import get_session, init_db
from app.models import Message
from app.pipeline.normalizer import normalize_group
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.segmenter import segment

cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
init_db()
s = get_session()

# 取已撤回之外的真实 text 消息（排除我们发的"测试"消息）
msgs = (s.query(Message)
        .filter(Message.msgtype == "text", Message.revoked == False)
        .order_by(Message.msgtime).all())
msgs = [m for m in msgs if "测试" not in (m.content or "")]

rules = load_rules()
rules_text = Path("rules/rules.yaml").read_text(encoding="utf-8")

print(f"回放 {len(msgs)} 条真实消息\n")
groups = segment(msgs)
print(f"切成 {len(groups)} 组\n")

for i, g in enumerate(groups):
    print(f"===== 组 {i+1}（{len(g['msgs'])} 条，modify={g['modify']}）=====")
    for m in g["msgs"]:
        print(f"  原文: {m.content[:100]}")
    result = normalize_group(g, rules_text, cfg["deepseek"], session=s)
    if result.get("error"):
        print(f"  [失败] {result['error']}")
        continue
    for o in result["orders"]:
        o2 = process(o, rules)
        print(f"  订单: {o2.get('receiver')} {o2.get('phone')} {o2.get('address')}")
        print(f"        物流: {o2.get('logistics')}  置信度: {o2.get('confidence')}")
        if o2.get("unknown_terms"):
            print(f"        未识别: {o2['unknown_terms']}")
        for l in o2["lines"]:
            fixes = f"  [{','.join(l['fixes'])}]" if l.get("fixes") else ""
            print(f"        {l['kind']:12s} | {l.get('product','')[:30]:32s} | {l.get('config','')[:24]:26s} | {l.get('qty','')}{fixes}")
    print()
s.close()
