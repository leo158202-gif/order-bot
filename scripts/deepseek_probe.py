"""DeepSeek 联通探测：读 rules.yaml 拼系统提示词，把 1 条样本消息发给 DeepSeek，打印 JSON。

用法：.venv\\Scripts\\python.exe scripts\\deepseek_probe.py [样本id，默认 s001]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml

OUTPUT_SCHEMA = """输出 JSON（不要输出多余文字）：
{
  "orders": [
    {
      "source_msgids": ["..."],
      "receiver": "收货人", "phone": "电话", "address": "完整地址及明确物流信息", "logistics": "",
      "urgent": false,
      "lines": [
        {"kind": "post|rail|channel|rail_fitting|channel_fitting|cap|screw|weld_note|sample|other",
         "product": "标准产品名", "config": "配置", "qty": "带单位数量",
         "raw": "原文对应片段", "confidence": 0.0}
      ],
      "unknown_terms": ["规则表里没有的简称"],
      "notes": "", "confidence": 0.0
    }
  ]
}
螺丝和盖子如果用户没写就不要输出，由程序补算。"""


def build_system_prompt(rules_text: str) -> str:
    return f"""你是铝合金楼梯扶手工厂的打单员。把微信群里的订单消息整理成标准表格行。

# 打单规则（结构化 YAML）
{rules_text}

# 要求
- 把简称改写成标准写法（严格按上面的 mapping）
- 一个收货人 = 一单；一组消息里可能有多个订单，按收货人拆开
- 数量单位：立柱用"支"（原文"只"保留），扶手/铝槽用"米"（总米数），配件/螺丝用"个"
- 收货人保留【急】标记；姓名/地址末尾四位方括号及电话里的四位方括号均为虚拟号分机，先用 - 接到手机号，再清理姓名/地址展示；电话已有分机优先且不重复追加，否则姓名优先于地址，冲突保留待核；长物流号不转分机
- H列物流始终为空；所有明确物流信息并入G地址，logistics必须为空字符串；渠道店名只作内部分类
- 拿不准的简称放进 unknown_terms，不要硬猜

{OUTPUT_SCHEMA}"""


def main() -> None:
    sample_id = sys.argv[1] if len(sys.argv) > 1 else "s001"
    sample = yaml.safe_load(Path(f"tests/samples/{sample_id}.yaml").read_text(encoding="utf-8"))
    rules_text = Path("rules/rules.yaml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    ds = cfg["deepseek"]

    user_msg = "群消息：\n" + "\n".join(
        f"[{m['time']}] {m['sender']}: {m['text']}" for m in sample["messages"]
    )

    resp = httpx.post(
        f"{ds['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {ds['api_key']}"},
        json={
            "model": ds["model"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": build_system_prompt(rules_text)},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=120,
    ).json()

    if "error" in resp:
        print(f"API 错误：{resp['error']}")
        sys.exit(1)

    usage = resp.get("usage", {})
    print(f"token 用量：in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}")

    content = resp["choices"][0]["message"]["content"]
    try:
        result = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败：{e}\n原始输出：\n{content}")
        sys.exit(1)

    print("返回合法 JSON [OK]")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 简单结构校验
    orders = result.get("orders", [])
    assert orders, "orders 为空"
    for o in orders:
        for field in ("receiver", "phone", "address", "lines", "confidence"):
            assert field in o, f"订单缺字段 {field}"
        for line in o["lines"]:
            for field in ("kind", "product", "qty"):
                assert field in line, f"行缺字段 {field}"
    print("结构校验通过 [OK]（orders/receiver/phone/address/lines/kind/qty 等字段齐全）")


if __name__ == "__main__":
    main()
