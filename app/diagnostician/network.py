"""Diagnostic-only Feishu transport: bounded time/body, no blind write retries."""
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import json
import time

import httpx

from app.feishu.client import BASE, Feishu, FeishuError

_scope = ContextVar('doctor_network_scope', default=None)


@contextmanager
def network_scope(context, cancelled):
    token = _scope.set((context, cancelled))
    try:
        yield
    finally:
        _scope.reset(token)


class BoundedFeishu(Feishu):
    def __init__(self, app_id, app_secret, *, transport=None):
        super().__init__(app_id, app_secret)
        self.transport = transport

    def _remaining(self, local_deadline):
        scope = _scope.get()
        deadline = local_deadline
        if scope:
            context, cancelled = scope
            if cancelled():
                raise TimeoutError('诊断任务已停止')
            if getattr(context, 'deadline', None) is not None:
                deadline = min(deadline, context.deadline)
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError('诊断请求时间预算已用完')
        return min(5, remaining)

    def _json(self, method, path, **kw):
        deadline = time.time() + 15
        timeout = self._remaining(deadline)
        scope = _scope.get()
        if scope and getattr(scope[0], 'deadline', None) is not None:
            deadline = min(deadline, scope[0].deadline)
        async def fetch():
            from app.deployment import check_client
            check_client(self)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout,
                                         transport=self.transport) as client:
                self._remaining(deadline)
                check_client(self)
                if path != '/auth/v3/tenant_access_token/internal':
                    from app.test_runtime import check_request
                    check_request(method, path, {key: value for key, value in kw.items() if key != 'headers'}, self)
                async with client.stream(method, BASE + path, **kw) as response:
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        self._remaining(deadline)
                        raw.extend(part)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError('诊断响应超过大小上限')
                    if response.status_code >= 400:
                        raise FeishuError(response.status_code, '单次请求未确认成功，未自动重试', path)
                    return json.loads(raw)
        async def bounded():
            return await asyncio.wait_for(fetch(), timeout=max(0.001, deadline - time.time()))
        return asyncio.run(bounded())

    @property
    def token(self):
        from app.deployment import check_client
        check_client(self)
        if not self._token or time.time() >= self._token_expire:
            body = self._json('POST', '/auth/v3/tenant_access_token/internal',
                              json={'app_id': self.app_id, 'app_secret': self.app_secret})
            check_client(self)
            if body.get('code') != 0:
                raise FeishuError(body.get('code'), '凭据检查未成功', 'gettoken')
            self._token = body['tenant_access_token']
            self._token_expire = time.time() + body.get('expire', 7200) - 300
        return self._token

    def req(self, method, path, _retries=1, **kw):
        from app.test_runtime import check_request
        check_request(method, path, kw, self)
        self._remaining(time.time() + 15)
        token = self.token
        self._remaining(time.time() + 15)
        check_request(method, path, kw, self)
        body = self._json(method, path, headers={'Authorization': 'Bearer ' + token}, **kw)
        if body.get('code') != 0:
            if body.get('code') == 99991663:
                self._token, self._token_expire = None, 0
            raise FeishuError(body.get('code'), '单次请求未确认成功，保留凭据供回读', path)
        from app.deployment import observe_response
        data = body.get('data', {})
        observe_response(self, method, path, kw, data)
        return data
