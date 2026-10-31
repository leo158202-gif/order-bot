"""飞书 API 客户端：tenant_access_token 缓存、统一请求、429 退避重试。02 文档第 10 节。"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date

import httpx

logger = logging.getLogger(__name__)

BASE = "https://open.feishu.cn/open-apis"


def is_order_sheet_title(title) -> bool:
    """订单月表只接受不补零的 M.D；无年份时允许可能存在的闰日。"""
    if not isinstance(title, str) or not re.fullmatch(r'(?:[1-9]|1[0-2])\.(?:[1-9]|[12][0-9]|3[01])', title):
        return False
    month, day = map(int, title.split('.'))
    try:
        date(2000, month, day)
    except ValueError:
        return False
    return True


class FeishuError(Exception):
    def __init__(self, code, msg, path=""):
        self.code = code
        super().__init__(f"FeishuError({code}) {msg} [{path}]")


class Feishu:
    supports_grid_preflight=True
    def __init__(self, app_id: str, app_secret: str):
        self.app_id, self.app_secret = app_id, app_secret
        self._token = None
        self._token_expire = 0
        self.min_request_interval=1.0 if os.environ.get('ORDER_BOT_TEST_RUNTIME') else 0.0
        self._last_request_at=None
        self._pace_lock=threading.Lock()
        from app.deployment import bind_client
        bind_client(self)

    def _pace(self):
        if self.min_request_interval<=0:return
        with self._pace_lock:
            now=time.monotonic()
            if self._last_request_at is not None:
                delay=self.min_request_interval-(now-self._last_request_at)
                if delay>0:time.sleep(delay)
            self._last_request_at=time.monotonic()

    @property
    def token(self) -> str:
        from app.deployment import check_client
        check_client(self)
        if not self._token or time.time() > self._token_expire:
            r = httpx.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                           json={"app_id": self.app_id, "app_secret": self.app_secret},
                           timeout=10).json()
            check_client(self)
            if r.get("code") != 0:
                raise FeishuError(r.get("code"), r.get("msg"), "gettoken")
            self._token = r["tenant_access_token"]
            self._token_expire = time.time() + r.get("expire", 7200) - 300
        return self._token

    def req(self, method: str, path: str, _retries: int = 4, **kw) -> dict:
        """统一请求；429 限流退避重试。"""
        from app.test_runtime import check_request
        from app.deployment import check_client, observe_response
        check_request(method,path,kw,self)
        last_code=429
        refreshed=False
        for attempt in range(_retries+1):
            if attempt>=_retries and not refreshed:break
            self._pace()
            token = self.token
            check_client(self)
            check_request(method,path,kw,self)
            r = httpx.request(method, f"{BASE}{path}",
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=30, **kw)
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning("飞书限流 429，%ds 后重试（%s %s）", wait, method, path)
                time.sleep(wait)
                continue
            data = r.json()
            # 官方通用错误码：tenant_access_token失效；明确拒绝才刷新重试一次。
            # https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code
            if data.get('code')==99991663 and not refreshed:
                self._token=None;self._token_expire=0;refreshed=True
                continue
            if data.get('code')==90217 and path.startswith('/sheets/'):
                last_code=90217
                if attempt+1<_retries:
                    wait=5*(2**attempt)
                    logger.warning('飞书请求过快，%ds后重试（%s %s）',wait,method,path)
                    time.sleep(wait)
                continue
            if data.get("code") != 0:
                raise FeishuError(data.get("code"), data.get("msg"), path)
            payload = data.get("data", {})
            observe_response(self,method,path,kw,payload)
            return payload
        raise FeishuError(last_code, "限流重试耗尽", path)
