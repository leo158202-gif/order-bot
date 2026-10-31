import asyncio
import time

import pytest

from app.diagnostician.mcp_server import CapabilityTokens, create_server
from app.diagnostician.store import DoctorStore


def test_capability_bound_to_action_target_expiry(tmp_path):
    tokens = CapabilityTokens(DoctorStore(tmp_path / 'agent.sqlite'))
    token = tokens.issue(session_id='s', task_id='t', request_id='r', action='corrections_apply',
                         target_id='test-target', capabilities=['correct'], ttl=30)
    assert tokens.resolve(token, action='corrections_apply', target_id='test-target')['session_id'] == 's'
    with pytest.raises(PermissionError):
        tokens.resolve(token, action='corrections_apply', target_id='other')
    with pytest.raises(PermissionError):
        tokens.resolve(token, action='versions_publish', target_id='test-target')
    with pytest.raises(PermissionError):
        tokens.resolve('confirmed=true', action='corrections_apply', target_id='test-target')
    with pytest.raises(PermissionError):
        tokens.resolve(token, action='corrections_apply', target_id='test-target', now=time.time() + 60)


def test_standard_sdk_client_handshake_list_call():
    # Independent SDK client exercises the protocol, not a homemade dispatcher.
    from mcp import ClientSession
    from mcp.shared.memory import create_connected_server_and_client_session
    class Registry:
        def schemas(self):
            return [{'type': 'function', 'function': {'name': 'help_search', 'description': 'help',
                     'parameters': {'type': 'object', 'properties': {}}}}]
        def call(self, name, arguments, context):
            return {'status': 'ok', 'data': {'answer': '本机说明'}, 'evidence_refs': [], 'error': None, 'retryable': False}
    server = create_server(Registry(), lambda name, args: object())
    async def exercise():
        async with create_connected_server_and_client_session(server) as client:
            assert (await client.list_tools()).tools[0].name == 'help_search'
            response = await client.call_tool('help_search', {})
            assert not response.isError
            assert response.structuredContent['data']['answer'] == '本机说明'
    asyncio.run(exercise())
