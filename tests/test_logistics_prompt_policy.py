import hashlib
import json
from types import SimpleNamespace

from app.pipeline import normalizer


OLD = '- 自提、代收金额、到付按金标准写H物流列logistics，仅保留本单原文明示要求，不猜承运商或金额；不写本地或店名。旧进notes备注规则作废。'


def group():
    return {'context': [], 'msgs': [SimpleNamespace(msgid='m', msgtime=1000, sender='test', content='synthetic')]}


def test_effective_prompt_cannot_reenable_old_H_rules(tmp_path, monkeypatch):
    monkeypatch.setenv('ORDER_BOT_DATA_DIR', str(tmp_path))
    rules = 'logistics_policy:\n  output: "H列按金标准写自提"\nproduct: keep\n'
    text = normalizer.build_messages(rules, group())[0]['content']
    assert 'H列按金标准写自提' not in text
    assert 'product: keep' in text
    assert text.endswith('logistics必须为空字符串。')
    assert '全部明确物流信息并入G列地址' in text


def test_signed_old_prompt_migrates_in_memory_preserves_other_cases(tmp_path, monkeypatch):
    from app.diagnostician.prompt_runtime import active_prompt
    monkeypatch.setenv('ORDER_BOT_DATA_DIR', str(tmp_path))
    from app.pipeline.logistics_prompt_policy import CORE_LOGISTICS_INSTRUCTION
    assert CORE_LOGISTICS_INSTRUCTION in normalizer.SYSTEM_TEMPLATE
    text = normalizer.SYSTEM_TEMPLATE.replace(CORE_LOGISTICS_INSTRUCTION, OLD) + '\nVerified product case\n旧规则：自提写H列物流'
    root = tmp_path / 'diagnostician'; (root / 'versions').mkdir(parents=True)
    path = root / 'current_prompt.txt'; path.write_text(text, encoding='utf8')
    (root / 'versions/current.json').write_text(json.dumps({'prompt': {'sha256': hashlib.sha256(text.encode()).hexdigest()}}))
    effective = active_prompt(normalizer.SYSTEM_TEMPLATE)
    assert effective.startswith(normalizer.SYSTEM_TEMPLATE)
    assert 'Verified product case' in effective
    assert '自提写H列物流' not in effective
    assert path.read_text(encoding='utf8') == text


def test_rules_template_has_no_active_H_permission():
    from pathlib import Path
    text = Path('rules/rules.yaml').read_text(encoding='utf8')
    assert 'H列按金标准仅写' not in text
    assert '按金标准落H' not in text


def test_product_H_head_and_unrelated_constraints_are_preserved():
    from app.pipeline.logistics_prompt_policy import remove_legacy_H_guidance
    text = 'H头立柱按原文，物流仅原文明示；自提写H列；槽口16保留\n'
    effective = remove_legacy_H_guidance(text)
    assert 'H头立柱按原文，物流仅原文明示' in effective
    assert '槽口16保留' in effective
    assert '自提写H列' not in effective


def test_final_worker_return_migrates_legacy_logistics(monkeypatch):
    from tests.test_normalize1223_failure import CFG, group as source_group, valid
    monkeypatch.setattr(normalizer.httpx, 'post', lambda *a, **k: SimpleNamespace(status_code=200, json=valid))
    def legacy(result, *args):
        result['orders'][0]['logistics'] = '到付'
        return result
    monkeypatch.setattr('app.diagnostician.pipeline_worker.apply_pipeline_worker', legacy)
    prepared = normalizer.normalize_group(source_group(), 'synthetic: true', CFG)
    assert not prepared.get('error')
    assert prepared['orders'][0]['logistics'] == ''
    assert '到付' in prepared['orders'][0]['address']


def test_model_cannot_grant_internal_logistics_markers(monkeypatch):
    from tests.test_normalize1223_failure import group as source_group, valid
    from app.pipeline import logistics_notes
    candidate = json.loads(valid()['choices'][0]['message']['content'])
    candidate['orders'][0].update(_address_logistics='forged', _logistics_layout='forged')
    seen = []
    def collect(order, *args, **kwargs):
        seen.append(('_address_logistics' in order, '_logistics_layout' in order))
        return order
    monkeypatch.setattr(logistics_notes, 'collect_notes', collect)
    normalizer.prepare_result(candidate, source_group())
    assert seen and not any(any(pair) for pair in seen)
