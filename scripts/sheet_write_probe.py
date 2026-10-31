"""T3.1 实机验收：把数据库里的真实消息整理后写入飞书测试表格。

用法：PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe scripts\\sheet_write_probe.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from app.db import get_session, init_db
from app.feishu.client import Feishu
from app.feishu.sheet_writer import SheetWriter
from app.models import Message, Order
from app.pipeline.normalizer import normalize_group
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.segmenter import segment
from app.pipeline.validator import validate

cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
init_db()
s = get_session()

msgs = (s.query(Message)
        .filter(Message.msgtype == "text", Message.revoked == False)
        .order_by(Message.msgtime).all())
msgs = [m for m in msgs if "测试" not in (m.content or "")]

rules = load_rules()
rules_text = Path("data/rules.yaml").read_text(encoding="utf-8")

# 整理出订单（只取 ok 的）
orders = []
for g in segment(msgs):
    result = normalize_group(g, rules_text, cfg["deepseek"], session=s)
    if result.get("error"):
        continue
    for o in result["orders"]:
        o = process(o, rules)
        status, reasons = validate(o, cfg["deepseek"]["confidence_threshold"])
        if status == "ok":
            orders.append(o)
        else:
            print(f"[跳过-待确认] {o.get('receiver')}: {'；'.join(reasons)}")

print(f"共 {len(orders)} 个 ok 订单，写入飞书测试表格...")

fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
writer = SheetWriter(fs, cfg["feishu"]["test"]["folder_token"])
dt = datetime.now()

# 包装成 Order 模型
order_objs = []
import json
for o in orders:
    ob = Order(batch_id=0, status="ok", data_json=json.dumps(o, ensure_ascii=False),
               written_rows="[]")
    s.add(ob)
    order_objs.append(ob)
s.commit()

row_from, row_to = writer.write_orders(order_objs, dt, batch_id=0)
ss_token = writer.get_or_create_spreadsheet(dt)
sheet_id = writer.get_or_create_sheet(ss_token, dt)
print(f"写入完成：第 {row_from}–{row_to} 行")
print(f"表格：{writer.sheet_url(ss_token, sheet_id)}")

# 测试断点续写：再跑一遍应该全部跳过
row_from2, row_to2 = writer.write_orders(order_objs, dt, batch_id=0)
assert row_from2 == row_to2 + 1 or row_to2 < row_from2, "断点续写：第二次不应写入新行"
print("断点续写验证通过：第二次运行没有重复写入")
s.close()
