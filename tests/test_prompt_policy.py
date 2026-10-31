"""物流规则通过真实提示词拼装传给模型；不请求外部服务。"""
from pathlib import Path

import yaml

from app.models import Message
from app.pipeline.normalizer import build_messages


RULES_PATH = Path(__file__).resolve().parents[1] / "rules" / "rules.yaml"


def test_assembled_prompt_does_not_request_logistics_or_shop_output():
    group = {"msgs": [Message(msgid="policy", sender="u", msgtime=0,
                              content="皓步店 顺丰到付 40扶手2支4米")], "context": []}
    system = build_messages(RULES_PATH.read_text(encoding="utf-8"), group)[0]["content"]
    assert 'H列物流始终为空' in system
    assert '全部明确物流信息并入G列地址' in system
    assert '按金标准写H物流列logistics' not in system
    assert '店名' in system
    assert '和 到付/现付/代收 写进 logistics' not in system
    assert '物流公司（中通、德邦、壹米滴答、顺丰、安能、货拉拉）原文有就写' not in system


def test_rules_do_not_promote_undecided_delivery_details_to_columns():
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert rules['logistics_keep_raw'] == []
    assert 'H列物流始终为空' in rules['logistics_policy']['output']
    assert '撤销历史H输出规则' in rules['logistics_policy']['notes']
    assert 'pending_decision' not in rules['logistics_policy']
