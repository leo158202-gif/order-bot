import time
from types import SimpleNamespace

import httpx
import pytest


def test_diagnostic_request_never_retries_ambiguous_write(monkeypatch):
    from app.diagnostician.network import BoundedFeishu, network_scope
    from app.feishu.client import FeishuError
    seen = []
    def handler(request):
        seen.append(request.method)
        return httpx.Response(429, json={'code': 429})
    client = BoundedFeishu('test', 'secret', transport=httpx.MockTransport(handler))
    monkeypatch.setattr('app.test_runtime.check_request', lambda *args: None)
    client._token, client._token_expire = 'local-token', time.time() + 100
    with network_scope(SimpleNamespace(deadline=time.time() + 10), lambda: False):
        with pytest.raises(FeishuError):
            client.req('PUT', '/sheets/v2/spreadsheets/test/values', json={})
    assert seen == ['PUT']


@pytest.mark.parametrize('expired,cancelled', [(True, False), (False, True)])
def test_expired_or_cancelled_context_never_enters_network(expired, cancelled):
    from app.diagnostician.network import BoundedFeishu, network_scope
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request))
    client = BoundedFeishu('test', 'secret', transport=transport)
    with network_scope(SimpleNamespace(deadline=time.time() + (-1 if expired else 10)), lambda: cancelled):
        with pytest.raises(TimeoutError):
            client.req('GET', '/sheets/v2/spreadsheets/test/metainfo')
    assert seen == []
