"""Source-owned finite YAML comments enter default and published prompts."""
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from app.pipeline import normalizer
from app.pipeline import gold_rules


def group():
    return {'context':[],'msgs':[SimpleNamespace(msgid='s1',msgtime=1000,sender='synthetic',
        content='Synthetic product source')]}


@pytest.mark.parametrize('rules',[
    'rails:\n  enabled: true\n',
    'value: 3 # keep this original comment',
    'note: |+\n  keep exact trailing lines\n\n',
    'note: |+\n  no final newline',
])
def test_effective_rules_preserve_original_segment_and_yaml_values(rules):
    effective=normalizer.effective_rules_text(rules)
    assert effective.endswith(rules)
    prefix=effective[:-len(rules)]
    assert prefix.strip() and all(not line.strip() or line.lstrip().startswith('#') for line in prefix.splitlines())
    assert yaml.safe_load(effective)==yaml.safe_load(rules)
    assert gold_rules.EVIDENCE_RULE_COMMENTS.strip() in effective


def test_default_prompt_contains_static_constraints_and_original_rules(tmp_path,monkeypatch):
    monkeypatch.setenv('ORDER_BOT_DATA_DIR',str(tmp_path))
    rules='keep_original: yes\n'
    system=normalizer.build_messages(rules,group())[0]['content']
    assert normalizer.effective_rules_text(rules) in system
    assert rules in system


def publish_prompt(tmp_path,text,sha=None):
    root=tmp_path/'diagnostician';root.mkdir()
    (root/'current_prompt.txt').write_text(text,encoding='utf-8')
    versions=root/'versions';versions.mkdir()
    (versions/'current.json').write_text(json.dumps({'prompt':{'sha256':sha or hashlib.sha256(text.encode()).hexdigest()}}))


def test_hash_valid_core_preserving_override_also_receives_constraints(tmp_path,monkeypatch):
    monkeypatch.setenv('ORDER_BOT_DATA_DIR',str(tmp_path))
    published=normalizer.SYSTEM_TEMPLATE+'\nPublished finite case note.'
    publish_prompt(tmp_path,published)
    system=normalizer.build_messages('original: 42\n',group())[0]['content']
    assert normalizer.effective_rules_text('original: 42\n') in system
    from app.pipeline.logistics_prompt_policy import FINAL_LAYOUT_CONSTRAINT
    assert 'Published finite case note.' in system
    assert system.endswith(FINAL_LAYOUT_CONSTRAINT)


@pytest.mark.parametrize('damage',['missing_manifest','wrong_hash','changed_core'])
def test_invalid_override_is_rejected_instead_of_falling_back(tmp_path,monkeypatch,damage):
    monkeypatch.setenv('ORDER_BOT_DATA_DIR',str(tmp_path))
    text=normalizer.SYSTEM_TEMPLATE if damage!='changed_core' else 'Untrusted replacement'
    publish_prompt(tmp_path,text,'wrong' if damage=='wrong_hash' else None)
    if damage=='missing_manifest':(tmp_path/'diagnostician/versions/current.json').unlink()
    with pytest.raises(ValueError):normalizer.build_messages('original: 1',group())


@pytest.mark.parametrize('comments',['', 'injected_yaml: true', '# comment\ninjected_yaml: true'])
def test_static_guidance_must_remain_nonempty_yaml_comments(monkeypatch,comments):
    monkeypatch.setattr(gold_rules,'EVIDENCE_RULE_COMMENTS',comments)
    with pytest.raises(ValueError):normalizer.effective_rules_text('original: 1')


def test_windows_bom_stays_at_stream_start_and_original_yaml_body_is_unchanged():
    rules='\ufefforiginal: 1\r\n'
    effective=normalizer.effective_rules_text(rules)
    assert effective.startswith('\ufeff') and effective.count('\ufeff')==1
    assert effective.endswith(rules[1:])
    assert yaml.safe_load(effective)==yaml.safe_load(rules)


def test_new_product_column_guidance_reaches_real_prompt_without_changing_runtime_rules(tmp_path, monkeypatch):
    from app.pipeline.gold_products_20261030 import RULE_COMMENTS
    from app.pipeline.logistics_prompt_policy import FINAL_LAYOUT_CONSTRAINT
    monkeypatch.setenv('ORDER_BOT_DATA_DIR', str(tmp_path))
    rules = 'custom_customer_rule: keep\n'
    system = normalizer.build_messages(rules, group())[0]['content']
    assert RULE_COMMENTS.strip() in system
    assert rules in system
    assert system.endswith(FINAL_LAYOUT_CONSTRAINT)
    assert yaml.safe_load(normalizer.effective_rules_text(rules)) == yaml.safe_load(rules)
