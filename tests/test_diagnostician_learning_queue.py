from copy import deepcopy
from dataclasses import replace

import yaml

from app.diagnostician.integration import register_versions
from app.diagnostician.learning_queue import LearningQueue
from app.diagnostician.service import DiagnosticianService
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_learning import setup_learning, BEFORE
from tests.test_diagnostician_repair_learning import frozen_plan


def test_completed_repair_auto_publishes_both_versions_and_new_tasks_load_them(setup_learning, tmp_path, monkeypatch):
    from app.diagnostician.learning import apply_learned_cases
    from app.diagnostician.prompt_runtime import active_prompt
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    from app.pipeline.rule_loader import load_rules
    store, versions, learning, ctx = setup_learning
    plan, current = frozen_plan(store)
    conversation = store.ensure_conversation('owner')
    plan['conversation_id'] = conversation['id']
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda *args: deepcopy(current)
    registry = ToolRegistry(store, writes_available=lambda: True)
    register_versions(registry, versions)
    registry.register('learning_from_repair_plan', 'fixture registration',
        {'type': 'object', 'properties': {'plan_id': {'type': 'string'}, 'order_id': {'type': 'integer'},
         'kind': {'type': 'string'}}, 'additionalProperties': False},
        lambda a, c: learning.prepare_from_repair_plan(a['plan_id'], a['order_id'], c, kind=a['kind']))
    service = DiagnosticianService(store, registry=registry, get_config=lambda: {}, upload_root=tmp_path / 'uploads',
        auto_start=False, context_factory=lambda task, original: replace(original, target_id='test',
            capability_id='trusted-plan', capabilities=frozenset({'versions_write'})))
    service.versions = versions
    service.learning_queue = LearningQueue(service)
    service.learning_queue.enqueue('owner', 'plan')
    session_id, task_id = service._queue.get_nowait()
    service.run_task(session_id, task_id)
    task = store.get_task(session_id, task_id)
    assert task['status'] == 'done', task['result']
    assert task['model_calls'] == 0 and task['tool_calls'] == 6
    assert len(versions.list_versions()) == 2
    service.learning_queue.enqueue('owner', 'plan')
    assert service._queue.empty()
    rules = load_rules(versions.paths['rules'])
    assert apply_learned_cases(BEFORE, rules)['lines'] == current['lines']
    monkeypatch.setenv('ORDER_BOT_DATA_DIR', str(versions.root))
    prompt = active_prompt(SYSTEM_TEMPLATE)
    assert prompt.startswith(SYSTEM_TEMPLATE) and current['lines'][0]['product'] in prompt
    assert yaml.safe_load(versions.current_text('rules'))['scoped_learned_cases']
