"""T2.1 验收：规则加载。"""
from pathlib import Path

import pytest

from app.pipeline.rule_loader import RulesError, load_rules


@pytest.fixture(autouse=True)
def isolated_rules(tmp_path, monkeypatch):
    import app.pipeline.rule_loader as rule_loader
    monkeypatch.setattr(rule_loader, "RULES_SRC",
                        Path(__file__).resolve().parents[1] / "rules" / "rules.yaml")
    monkeypatch.setattr(rule_loader, "RULES_DATA", tmp_path / "data" / "rules.yaml")


def test_load_default():
    """首次加载自动从 rules/rules.yaml 复制到 data/rules.yaml。"""
    import app.pipeline.rule_loader as m
    if m.RULES_DATA.exists():
        m.RULES_DATA.unlink()
    rules = load_rules()
    assert m.RULES_DATA.exists()
    assert rules.line_order[0] == "post"


def test_color_standard():
    rules = load_rules()
    assert rules.color_standard("砂黑") == "黑色"
    assert rules.color_standard("亚黑") == "黑色"
    assert rules.color_standard("深灰") == "灰色"
    assert rules.color_standard("不存在的颜色") is None


def test_typo_fix():
    rules = load_rules()
    assert rules.typo_fix("立住") == "立柱"
    assert rules.typo_fix("免护手") == "免扶手"


def test_channel_tag():
    rules = load_rules()
    assert rules.channel_tag("皓步店") == "皓步店 本地发 拼多多"


def test_all_short_terms():
    rules = load_rules()
    terms = rules.all_short_terms()
    assert "H头" in terms
    assert "T铝槽" in terms
    assert "免扶手" in terms


def test_bad_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  - [unclosed", encoding="utf-8")
    with pytest.raises(RulesError, match="格式错误"):
        load_rules(bad)


def test_missing_section(tmp_path):
    f = tmp_path / "incomplete.yaml"
    f.write_text("normalize: {}\n", encoding="utf-8")
    with pytest.raises(RulesError, match="缺少必需的节"):
        load_rules(f)
