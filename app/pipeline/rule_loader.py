"""加载 rules.yaml，提供查询接口。02 文档 T2.1。

规则文件首次运行时从 rules/rules.yaml 复制到 data/rules.yaml，之后以 data/ 里的为准
（后台"简称规则"页改的是 data/ 里这份）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

RULES_SRC = Path(__file__).resolve().parents[2] / "rules" / "rules.yaml"
RULES_DATA = Path("data/rules.yaml")


class RulesError(Exception):
    """规则文件格式错误。"""


class Rules:
    """rules.yaml 的查询封装。属性即顶层键。"""

    def __init__(self, data: dict):
        self._d = data

    def __getattr__(self, name: str):
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(f"rules.yaml 里没有 '{name}' 这个节")

    # ---- 常用查询 ----

    def color_standard(self, text: str) -> str | None:
        """颜色同义词归一：砂黑/亚黑/磨砂黑 -> 黑色。找不到返回 None。"""
        for group in self._d.get("normalize", {}).get("colors", []):
            if text in group.get("aliases", []):
                return group["standard"]
        return None

    def typo_fix(self, text: str) -> str:
        """错别字表替换（立住->立柱 等），返回替换后的整段文本。"""
        for wrong, right in self._d.get("normalize", {}).get("typos", {}).items():
            text = text.replace(wrong, right)
        return text

    def channel_tag(self, text: str) -> str | None:
        """渠道标记：皓步店 -> 皓步店 本地发 拼多多。"""
        return self._d.get("channels_tag", {}).get(text)

    @property
    def line_order(self) -> list[str]:
        return self._d.get("line_order", [])

    @property
    def auto_calc(self) -> dict:
        return self._d.get("auto_calc", {})

    def all_short_terms(self) -> set[str]:
        """所有已知的简称（rails/posts/channels/fittings/samples 的 short），用于识别未识别词。"""
        terms = set()
        for section in ("rails", "posts", "channels", "fittings", "samples"):
            for item in self._d.get(section, {}).get("mapping", []) if isinstance(
                    self._d.get(section), dict) else self._d.get(section, []):
                short = item.get("short")
                if isinstance(short, list):
                    terms.update(short)
                elif short:
                    terms.add(short)
        return terms

    def local_only_groups(self) -> list[str]:
        """按群名分流的特殊群列表：订单原文含「本地」才登记（rules.yaml group_routing，#29）。"""
        return list(self._d.get("group_routing", {}).get("special_local_only_groups", []))


def load_rules(path: Path | None = None) -> Rules:
    """加载规则；path 默认 data/rules.yaml（不存在时从 rules/rules.yaml 复制）。"""
    if path is None:
        if not RULES_DATA.exists():
            RULES_DATA.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(RULES_SRC, RULES_DATA)
        path = RULES_DATA
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RulesError(f"规则文件 {path} 格式错误：{e}")
    if not isinstance(data, dict):
        raise RulesError(f"规则文件 {path} 顶层必须是键值结构")
    for required in ("normalize", "rails", "posts", "channels", "fittings", "auto_calc", "line_order"):
        if required not in data:
            raise RulesError(f"规则文件 {path} 缺少必需的节：{required}")
    return Rules(data)
