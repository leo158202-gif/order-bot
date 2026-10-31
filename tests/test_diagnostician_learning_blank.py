"""Empty optional gold cells must not invalidate a proven repair chain."""
from copy import deepcopy
import pytest

from tests.test_diagnostician_learning import setup_learning
from tests.test_diagnostician_repair_learning import frozen_plan


@pytest.mark.parametrize('value', [None, ''])
def test_blank_logistics_at_report_head_preserves_frozen_chain(setup_learning, value):
    store, versions, learning, context = setup_learning
    plan, current = frozen_plan(store)
    plan['frozen_differences'][0]['observed']['logistics'] = value
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    frozen_before = deepcopy(store.get_record('owner', 'gold-repair-plans', 'plan'))
    learning.current_order_provider = lambda *args: deepcopy(current)
    result = learning.prepare_from_repair_plan('plan', 7, context)
    assert result['status'] == 'ok', result
    assert result['data']['case_count'] == 1
    assert store.get_record('owner', 'gold-repair-plans', 'plan') == frozen_before


@pytest.mark.parametrize('value', ['None', 0, False])
def test_nonblank_logistics_is_not_coerced_to_empty(setup_learning, value):
    store, versions, learning, context = setup_learning
    plan, current = frozen_plan(store)
    plan['frozen_differences'][0]['observed']['logistics'] = value
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda *args: deepcopy(current)
    result = learning.prepare_from_repair_plan('plan', 7, context)
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'learning_chain_start_mismatch', result
    assert versions.list_versions() == []
