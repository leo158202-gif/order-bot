import ipaddress
import logging
import re
import socket
from collections import Counter

import httpx

logger = logging.getLogger(__name__)

# 注意：myip.ipip.net 的 https 在双栈网络下会优先返回 IPv6，用 http 可拿到 IPv4
DEFAULT_SERVICES = [
    "http://myip.ipip.net",
    "https://ip.3322.net",
    "http://ip.3322.net",
]

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TIMEOUT = 5.0


def _extract_ipv4(text: str) -> str | None:
    for m in IPV4_RE.findall(text):
        try:
            ipaddress.IPv4Address(m)
            return m
        except ipaddress.AddressValueError:
            continue
    return None


def query_one(url: str, timeout: float = TIMEOUT) -> str | None:
    """查询单个服务，返回 IPv4 字符串；失败返回 None。"""
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return _extract_ipv4(resp.text)
    except Exception as exc:
        logger.warning("IP 查询服务失败 %s: %s", url, exc)
        return None


def get_public_ip(services: list[str] | None = None, timeout: float = TIMEOUT) -> dict:
    """同时查多个服务，取多数一致的结果。

    返回 {"ip": str|None, "network_ok": bool, "results": {url: ip|None}, "error": str|None}
    - 多数（≥2）一致 -> 该 IP
    - 只有 1 个成功 -> 用它（ip 字段），error 注明只有单源
    - 全部失败 -> ip=None；若 DNS 解析失败判定为断网（network_ok=False），不误报 IP 变化
    """
    services = services or DEFAULT_SERVICES
    results: dict[str, str | None] = {}
    dns_failures = 0
    for url in services:
        try:
            ip = query_one(url, timeout)
        except Exception:
            ip = None
        results[url] = ip

    # 单独统计 DNS 失败（query_one 已吞掉异常，这里重查一次判断网络）
    successes = [ip for ip in results.values() if ip]
    if not successes:
        network_ok = True
        for url in services:
            host = re.sub(r"^https?://", "", url).split("/")[0]
            try:
                socket.getaddrinfo(host, 443)
            except socket.gaierror:
                dns_failures += 1
        if dns_failures == len(services):
            network_ok = False
        return {"ip": None, "network_ok": network_ok, "results": results,
                "error": None if network_ok else "网络断开（DNS 全部解析失败）"}

    ip, count = Counter(successes).most_common(1)[0]
    error = None
    if count == 1 and len(successes) < 2:
        error = "只有一个查询服务可用，结果仅供参考"
    elif len(set(successes)) > 1:
        error = f"各服务结果不一致，取多数：{ip}"
    return {"ip": ip, "network_ok": True, "results": results, "error": error}
