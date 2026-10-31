"""DeepSeek Chat Completions only; tool execution stays in the broker."""
import asyncio
import json

import httpx

from .guides import classify_failure


class ModelError(RuntimeError):
    def __init__(self, code, message, *, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


class ModelClient:
    def __init__(self, get_config, *, transport=None):
        self.get_config = get_config if callable(get_config) else lambda: get_config
        self.transport = transport

    def complete(self, messages, tools, *, timeout):
        cfg = self.get_config().get("deepseek") or {}
        if not cfg.get("api_key") or not cfg.get("base_url") or not cfg.get("model"):
            raise ModelError("not_configured", "模型配置不完整，本机说明书和账号操作指引仍可查看。")
        remaining = max(0.001, min(timeout, 120))

        async def fetch():
            async with httpx.AsyncClient(trust_env=False, transport=self.transport, timeout=remaining, follow_redirects=False) as client:
                async with client.stream("POST", cfg["base_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + cfg["api_key"]},
                        json={"model": cfg["model"], "messages": messages, "tools": tools, "temperature": 0,
                              "max_tokens": min(int(cfg.get("max_tokens", 4096)), 8192)}) as response:
                    if response.status_code >= 400:
                        failure = classify_failure(http_status=response.status_code)
                        raise ModelError(failure["category"], failure["message"], retryable=response.status_code == 429 or response.status_code >= 500)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise ModelError("invalid_response", "模型响应超过大小限制，已停止读取。")
                    return bytes(body)

        async def bounded_fetch():
            # A total deadline cancels connect/read/drip responses as one call,
            # rather than resetting a per-socket timeout for every data chunk.
            return await asyncio.wait_for(fetch(), timeout=remaining)

        try:
            body = asyncio.run(bounded_fetch())
        except (httpx.RequestError, asyncio.TimeoutError):
            failure = classify_failure(network_error="request")
            raise ModelError("network", failure["message"], retryable=True) from None
        try:
            choice = json.loads(body)["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") == "length" or message.get("role") != "assistant":
                raise ValueError()
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError()
            if len(message.get("tool_calls", [])) > 12:
                raise ValueError()
            json.dumps(message, allow_nan=False)
            return message
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelError("invalid_response", "模型返回的内容不完整或格式不正确，未执行其中动作。") from None
