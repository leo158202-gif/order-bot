"""代理/网络就绪门闩：跑批前探活，未就绪不占 batch、不写 failed 占位（#15 关联）。

跑批需要两类外网：DeepSeek（整理）+ 飞书（写表/通知）。任一不通就不该起批次，
否则 batch 占住消息 batch_id 后 failed，网络恢复也不自愈（2026-10-22 batch6 教训）。
"""
from __future__ import annotations

import logging
import socket
import time
import urllib.request
from urllib.parse import urlsplit

from app.feishu.client import BASE as FEISHU_BASE

logger = logging.getLogger(__name__)

TIMEOUT = 5.0


def _tcp_ok(host: str, port: int, timeout: float = TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = TIMEOUT) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def probe_readiness(cfg: dict) -> tuple[bool, str]:
    """探活代理（如启用）+ 必要外网。返回 (是否就绪, 说明)。

    - 代理启用（wecom.proxy.enabled）时先探代理 host:port，不通直接未就绪。
    - DeepSeek 和飞书按实际 URL、系统/环境代理及绕过名单选择 TCP 探测端点。
    任一不通都判未就绪，调用方据此不起批次。
    """
    proxy = cfg.get("wecom", {}).get("proxy", {})
    if proxy.get("enabled") and proxy.get("url"):
        try:
            host, port = _proxy_endpoint(proxy["url"], default_port=7897)
        except ValueError:
            return False, "企微代理地址无效"
        if not _tcp_ok(host, port):
            return False, f"代理未就绪（{host}:{port} 拒连）"

    try:
        # 与实际请求使用相同的 HTTPX 0.28.1 路由规则（requirements 已锁定）。
        # urllib 的 NO_PROXY / Windows ProxyOverride 语义与 HTTPX 不完全相同。
        from httpx import Proxy, URL, _utils
        pattern_type = _utils.URLPattern
        proxies = urllib.request.getproxies()
        proxy_map = _utils.get_environment_proxies()
        # HTTPX 创建客户端会校验全部代理，包括本轮两个 HTTPS 目标未使用的 HTTP 代理。
        for proxy_url in proxy_map.values():
            if proxy_url is not None:
                proxy_config = Proxy(url=proxy_url)
                _proxy_endpoint(proxy_url)
                if proxy_config.url.scheme in ("socks5", "socks5h"):
                    import socksio  # noqa: F401；与 HTTPX 的可选依赖检查一致，不连接网络
        routes = sorted((pattern_type(pattern), proxy_url)
                        for pattern, proxy_url in proxy_map.items())
    except Exception:
        # 私有接口变动、代理配置解析失败时保守拒绝，不输出可能含认证信息的异常。
        return False, "代理路由解析失败，暂不起批"
    targets = (("DeepSeek", cfg.get("deepseek", {}).get("base_url") or "https://api.deepseek.com"),
               ("飞书", FEISHU_BASE))
    for name, url in targets:
        try:
            target = urlsplit(url)
            host = target.hostname
            if not host or target.scheme not in ("http", "https"):
                return False, f"{name} API 地址无效"
            port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False, f"{name} API 地址无效"
        try:
            system_proxy = proxies.get(target.scheme) or proxies.get("all")
            system_bypass = urllib.request.proxy_bypass(target.netloc) if system_proxy else False
            actual_url = URL(url)
            proxy_url = next((proxy for pattern, proxy in routes
                              if pattern.matches(actual_url)), None)
        except Exception:
            return False, f"{name} 代理路由解析失败，暂不起批"
        if bool(proxy_url) != bool(system_proxy and not system_bypass):
            logger.info("%s 系统代理绕过规则与 HTTPX 不同，按 HTTPX 实际路由探测", name)
        if proxy_url:
            try:
                proxy_host, proxy_port = _proxy_endpoint(proxy_url)
            except ValueError:
                return False, f"{name} 系统/环境代理地址无效"
            if not _tcp_ok(proxy_host, proxy_port):
                return False, f"{name} 代理未就绪（{proxy_host}:{proxy_port} 拒连）"
        elif not _tcp_ok(host, port):
            return False, f"{name} 外网不可达（{host}:{port}）"
    return True, "就绪"


def _proxy_endpoint(url: str, default_port: int | None = None) -> tuple[str, int]:
    """只提取探测端点，说明中不暴露代理认证信息。"""
    parsed = urlsplit(url if "://" in url else "//" + url)
    if not parsed.hostname:
        raise ValueError("代理缺少主机")
    port = parsed.port
    if port is None:
        port = default_port or {"https": 443, "socks5": 1080, "socks5h": 1080}.get(parsed.scheme, 80)
    if not 1 <= port <= 65535:
        raise ValueError("代理端口无效")
    return parsed.hostname, port


def wait_ready(cfg: dict, retries: int = 3, backoff: float = 5.0) -> tuple[bool, str]:
    """有限次重试 + 退避。就绪返回 (True, ...)，否则 (False, 最后的说明)。"""
    for attempt in range(retries):
        ok, why = probe_readiness(cfg)
        if ok:
            return True, why
        if attempt < retries - 1:
            logger.info("网络未就绪（%s），%.0f 秒后重试（%d/%d）", why, backoff, attempt + 1, retries - 1)
            time.sleep(backoff)
            backoff *= 2
    return False, why
