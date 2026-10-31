"""Official MCP SDK stdio adapter over the same trusted ToolRegistry.

The default process has no write capability. The trusted host may supply an
opaque, short-lived capability through its child environment; model arguments
and uploaded documents cannot mint one. stdout is reserved for MCP frames.
"""
import asyncio
import hashlib
import json
import secrets
import time

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool


class CapabilityTokens:
    def __init__(self, store):
        self.store = store

    def issue(self, *, session_id, task_id, request_id, action, target_id, capabilities, ttl=120):
        """Trusted Python entry only; never register this as a model/MCP tool."""
        if not 0 < ttl <= 120 or not action or not target_id:
            raise ValueError('invalid capability scope')
        token = secrets.token_urlsafe(48)
        self.store.put_record('trusted-capabilities', 'mcp-tokens', self._key(token), {
            'session_id': session_id, 'task_id': task_id, 'request_id': request_id,
            'action': action, 'target_id': target_id, 'capabilities': list(capabilities),
            'expires_at': time.time() + ttl}, expected_revision=0)
        return token

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def resolve(self, token, *, action, target_id, now=None):
        if not isinstance(token, str) or len(token) > 200:
            raise PermissionError('无效工具权限')
        record = self.store.get_record('trusted-capabilities', 'mcp-tokens', self._key(token))
        if not record:
            raise PermissionError('无效工具权限')
        grant = record['payload']
        if ((time.time() if now is None else now) >= grant['expires_at']
                or action != grant['action'] or target_id != grant['target_id']):
            raise PermissionError('工具权限已过期或目标不匹配')
        return grant


def create_server(registry, context_for_call):
    server = Server('orderbot-diagnostician', version='1.0.0',
                    instructions='本机诊断工具；写操作须软件签发指定目标权限；同证据最多核查三轮。')

    @server.list_tools()
    async def list_tools():
        return [Tool(name=s['function']['name'], description=s['function']['description'],
                     inputSchema=s['function']['parameters']) for s in registry.schemas()]

    @server.call_tool(validate_input=True)
    async def call_tool(name, arguments):
        try:
            context = context_for_call(name, arguments)
            result = await asyncio.to_thread(registry.call, name, arguments, context)
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                                  structuredContent=result, isError=result.get('status') in {'failed', 'error', 'denied'})
        except PermissionError:
            return CallToolResult(content=[TextContent(type='text', text='没有本动作的有效权限；请回软件选择具体目标。')], isError=True)
        except Exception:
            # Do not emit paths, configuration or exception repr to stdout.
            return CallToolResult(content=[TextContent(type='text', text='工具未执行成功，诊断记录已保留。')], isError=True)
    return server


async def serve(registry, context_for_call):
    server = create_server(registry, context_for_call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    import argparse
    import os
    from pathlib import Path
    from .contracts import ToolContext
    from .service import DiagnosticianService
    from .store import DoctorStore
    parser = argparse.ArgumentParser(description='OrderBot内置诊断MCP（默认只读）')
    parser.add_argument('--data-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root.resolve(strict=True)
    # Service factory is shared with the web host, never a second tool stack.
    from .integration import build_service
    from app.db import init_db
    sessions = init_db(root / 'db.sqlite')
    service = build_service(root, start_worker=False, session_factory=sessions)
    session_id = secrets.token_hex(24)
    conversation = service.store.ensure_conversation(session_id)
    task = service.store.create_task(session_id, conversation['id'], secrets.token_hex(24), kind='mcp')
    deadline = time.time() + 120
    service.store.update_task(session_id, task['id'], status='running')
    token = os.environ.pop('ORDERBOT_MCP_CAPABILITY', '')
    target = os.environ.pop('ORDERBOT_MCP_TARGET', '')
    tokens = CapabilityTokens(service.store)
    def context_for(name, arguments):
        if token:
            grant = tokens.resolve(token, action=name, target_id=target)
            return ToolContext(session_id=grant['session_id'], task_id=grant['task_id'],
                request_id=grant['request_id'], target_id=grant['target_id'], capability_id=tokens._key(token),
                capabilities=frozenset(grant['capabilities']), deadline=min(deadline, grant['expires_at']))
        return ToolContext(session_id=session_id, task_id=task['id'], request_id=task['request_id'], deadline=deadline)
    try:
        asyncio.run(serve(service.registry, context_for))
    finally:
        service.store.update_task(session_id, task['id'], status='done')
        service.close()


if __name__ == '__main__':
    main()
