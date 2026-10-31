from app import deployment
from app.diagnostician.target_policy import target_allowed


def test_source_execution_never_opens_production_by_environment(monkeypatch):
    monkeypatch.setattr(deployment, '_authority', None)
    monkeypatch.setenv('ORDER_BOT_INSTALL_ROOT', 'fake-install')
    assert target_allowed('test') is True
    assert target_allowed('prod') is False


def test_installed_target_still_requires_exact_host_owned_scope(monkeypatch):
    monkeypatch.setattr(deployment, 'active', lambda: True)
    monkeypatch.setattr(deployment, 'current_target_allowed', lambda target, data_root=None:
                        target == 'prod' and data_root == 'owned-data')
    assert target_allowed('prod', 'owned-data') is True
    assert target_allowed('test', 'owned-data') is False
    assert target_allowed('prod', 'foreign-data') is False
