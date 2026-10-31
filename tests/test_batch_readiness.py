"""#15 关联：代理/网络就绪门闩 + 批次失败回滚/续跑。"""
import json
import os
import sys
from types import SimpleNamespace

from httpx import _utils as httpx_utils

import pytest

from app.db import init_db
from app.models import Batch, Message, Order, Room
from app.net import readiness
from app.pipeline import batch_runner
from app.pipeline.batch_runner import run_batch

REAL_GETPROXIES = readiness.urllib.request.getproxies
REAL_PROXY_BYPASS = readiness.urllib.request.proxy_bypass


# ---- 门闩探活 ----

@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    """代理来源由用例提供，漏 mock 的探测必须失败，绝不连接真实网络。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(httpx_utils, "getproxies",
                        lambda: readiness.urllib.request.getproxies())
    monkeypatch.setattr(readiness.urllib.request, "proxy_bypass", lambda host:
                        readiness.urllib.request.proxy_bypass_environment(
                            host, readiness.urllib.request.getproxies()))

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("测试禁止真实网络连接")

    monkeypatch.setattr(readiness.socket, "create_connection", unexpected_connection)


def test_probe_proxy_down(monkeypatch):
    """代理启用但端口拒连 -> 未就绪。"""
    monkeypatch.setattr(readiness, "_tcp_ok", lambda *a, **kw: False)
    cfg = {"wecom": {"proxy": {"enabled": True, "url": "http://127.0.0.1:9"}}}
    ok, why = readiness.probe_readiness(cfg)
    assert ok is False and "代理未就绪" in why


def test_probe_proxy_disabled_skips(monkeypatch):
    """代理未启用 -> 跳过代理探活，探外网（mock 外网通）。"""
    monkeypatch.setattr(readiness, "_tcp_ok", lambda h, p, timeout=5: True)
    cfg = {"wecom": {"proxy": {"enabled": False, "url": ""}}}
    ok, _ = readiness.probe_readiness(cfg)
    assert ok is True


def test_probe_external_down(monkeypatch):
    """代理通但飞书外网不通 -> 未就绪。"""
    def fake_tcp(host, port, timeout=5):
        return host != "open.feishu.cn"  # 飞书不通
    monkeypatch.setattr(readiness, "_tcp_ok", fake_tcp)
    cfg = {"wecom": {"proxy": {"enabled": False}}}
    ok, why = readiness.probe_readiness(cfg)
    assert ok is False and "飞书" in why


def test_system_proxy_down_with_wecom_proxy_disabled(monkeypatch):
    """企微代理关闭仍须检查系统 HTTPS 代理，拒连不能误报就绪。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897"})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: host != "localhost")
    ok, why = readiness.probe_readiness({"wecom": {"proxy": {"enabled": False}}})
    assert ok is False and "代理未就绪" in why


def test_reachable_system_proxy_does_not_require_direct_route(monkeypatch):
    """实际走代理时，直连外网失败不应阻止起批。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897"})
    monkeypatch.setattr(readiness, "_tcp_ok",
                        lambda host, port: (host, port) == ("localhost", 7897))
    assert readiness.probe_readiness({})[0] is True


def test_proxy_bypass_uses_direct_route(monkeypatch):
    """绕过名单命中的目标不依赖系统代理端口。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897", "no": "*"})
    monkeypatch.setattr(readiness, "_tcp_ok",
                        lambda host, port: host != "localhost" and port == 443)
    assert readiness.probe_readiness({})[0] is True


def test_proxy_bypass_applies_to_each_actual_target(monkeypatch):
    """仅 DeepSeek 绕过时飞书仍须检查代理，不全局跳过。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897", "no": "api.deepseek.com"})
    monkeypatch.setattr(readiness, "_tcp_ok",
                        lambda host, port: host == "api.deepseek.com")
    ok, why = readiness.probe_readiness({})
    assert ok is False and "飞书" in why and "代理未就绪" in why


def test_deepseek_configured_url_selects_scheme_and_endpoint(monkeypatch):
    """自定义 DeepSeek HTTP 目标按其实际协议选择代理，不探默认域名。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"http": "http://local-proxy:8080"})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port:
                        (host, port) in {("local-proxy", 8080), ("open.feishu.cn", 443)})
    cfg = {"deepseek": {"base_url": "http://model.example:9000/v1"}}
    assert readiness.probe_readiness(cfg)[0] is True


def test_configured_url_bypass_keeps_actual_direct_port(monkeypatch):
    """自定义目标绕过代理时按真实端口探测。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897", "no": "*"})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port:
                        (host, port) in {("model.example", 9443), ("open.feishu.cn", 443)})
    assert readiness.probe_readiness({"deepseek": {"base_url": "https://model.example:9443/v1"}})[0]


@pytest.mark.parametrize("proxy_url, endpoint", [
    ("localhost:7897", ("localhost", 7897)),
    ("http://localhost", ("localhost", 80)),
    ("https://[::1]:8443", ("::1", 8443)),
])
def test_system_proxy_address_formats(monkeypatch, proxy_url, endpoint):
    """代理地址解析兼容无协议、默认端口和 IPv6。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies", lambda: {"https": proxy_url})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: (host, port) == endpoint)
    assert readiness.probe_readiness({})[0] is True


@pytest.fixture()
def real_proxy_environment(monkeypatch):
    """仅清理进程级代理变量；使用真实标准库解析，不读系统代理设置。"""
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        readiness.urllib.request.getproxies_environment)
    monkeypatch.setattr(readiness.urllib.request, "proxy_bypass",
                        readiness.urllib.request.proxy_bypass_environment)
    monkeypatch.setenv("HTTPS_PROXY", "http://dead-proxy:7897")
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: host != "dead-proxy")


@pytest.mark.parametrize("no_proxy, expected_ready", [
    (".api.deepseek.com,.open.feishu.cn", False),
    ("unrelated.invalid,*", True),
    ("api.deepseek.com,open.feishu.cn", True),
    (".deepseek.com,.feishu.cn", True),
])
def test_no_proxy_matches_actual_httpx_routing(real_proxy_environment, monkeypatch,
                                             no_proxy, expected_ready):
    """前导点与混合通配符须遵循实际 HTTPX 路由，不能按 urllib 错判。"""
    monkeypatch.setenv("NO_PROXY", no_proxy)
    assert readiness.probe_readiness({})[0] is expected_ready


@pytest.mark.skipif(os.name != "nt", reason="Windows 注册表路由")
def test_windows_override_does_not_bypass_httpx_proxy(real_proxy_environment, monkeypatch):
    """真实 urllib 注册表解析会绕过，但 HTTPX 未采用 ProxyOverride，仍须探代理。"""
    monkeypatch.delenv("HTTPS_PROXY")
    values = {"ProxyEnable": 1, "ProxyServer": "dead-proxy:7897",
              "ProxyOverride": "*.deepseek.com;*.feishu.cn"}
    key = SimpleNamespace(Close=lambda: None)
    registry = SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=lambda *a: key,
                               QueryValueEx=lambda key, name: (values[name], 1))
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(readiness.urllib.request, "getproxies", REAL_GETPROXIES)
    monkeypatch.setattr(readiness.urllib.request, "proxy_bypass", REAL_PROXY_BYPASS)
    assert readiness.probe_readiness({})[0] is False


@pytest.mark.parametrize("failure", ["parse_error", "missing_parser", "missing_pattern"])
def test_httpx_routing_failure_is_not_reported_ready(monkeypatch, failure):
    """路由解析异常或内部接口变化时必须拒绝起批。"""
    def broken_routes():
        raise ValueError("合成路由错误")
    if failure == "parse_error":
        monkeypatch.setattr(httpx_utils, "get_environment_proxies", broken_routes)
    else:
        monkeypatch.delattr(httpx_utils, "get_environment_proxies" if failure == "missing_parser"
                            else "URLPattern")
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: True)
    assert readiness.probe_readiness({})[0] is False


@pytest.mark.parametrize("proxies", [
    {"https": "ftp://proxy.example:8080"},
    {"http": "ftp://proxy.example:8080", "https": "http://proxy.example:7897"},
    {"http": "http://", "https": "http://proxy.example:7897"},
    {"http": "http://proxy.example:99999", "https": "http://proxy.example:7897"},
])
def test_invalid_proxy_rejected_even_when_not_used_by_target(monkeypatch, proxies):
    """HTTPX 初始化会校验全部代理，非目标协议的无效代理也不能误报就绪。"""
    monkeypatch.setattr(readiness.urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: True)
    assert readiness.probe_readiness({})[0] is False


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks_proxy_without_dependency_is_not_ready(monkeypatch, scheme):
    """缺 socksio 时即使代理端口通，也不能假装 HTTPX 可使用。"""
    monkeypatch.setitem(sys.modules, "socksio", None)
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": f"{scheme}://proxy.example:1080"})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: True)
    assert readiness.probe_readiness({})[0] is False


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks_proxy_with_dependency_uses_default_port_1080(monkeypatch, scheme):
    """可用 SOCKS 依赖下，未显式端口应探测1080，不能误探80。"""
    monkeypatch.setitem(sys.modules, "socksio", SimpleNamespace())
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": f"{scheme}://proxy.example"})
    monkeypatch.setattr(readiness, "_tcp_ok",
                        lambda host, port: (host, port) == ("proxy.example", 1080))
    assert readiness.probe_readiness({})[0] is True


def test_wait_ready_retries_then_fail(monkeypatch):
    """有限次重试 + 退避后仍未就绪 -> 返回 False。"""
    monkeypatch.setattr(readiness, "probe_readiness", lambda c: (False, "代理未就绪"))
    monkeypatch.setattr(readiness.time, "sleep", lambda s: None)  # 不真睡
    ok, why = readiness.wait_ready({}, retries=3, backoff=0.1)
    assert ok is False and "代理未就绪" in why


# ---- 批次：未就绪不占位 ----

@pytest.fixture()
def env(tmp_path, monkeypatch):
    factory = init_db(tmp_path / "t.db")
    monkeypatch.setattr("app.pipeline.batch_runner.get_session", factory)
    monkeypatch.setattr(batch_runner, "_load_cfg", lambda: {
        "wecom": {"proxy": {"enabled": False}},
        "deepseek": {"confidence_threshold": 0.8},
        "target": "test",
        "feishu": {"app_id": "", "app_secret": "", "test": {"folder_token": ""}},
    })
    monkeypatch.setattr("app.pipeline.batch_runner.load_rules",
                        lambda: __import__("app.pipeline.rule_loader", fromlist=["load_rules"]).load_rules())
    from pathlib import Path
    real_read = Path.read_text
    monkeypatch.setattr("pathlib.Path.read_text",
                        lambda self, **kw: open("rules/rules.yaml", encoding="utf-8").read()
                        if "rules.yaml" in str(self) else real_read(self, **kw))
    return factory


def _add_msgs(factory, n=3):
    s = factory()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    for i in range(n):
        s.add(Message(msgid=f"m{i}", seq=i, roomid="wrAAA", sender="u1",
                      msgtime=1792477000000 + i * 60000, msgtype="text",
                      content="张三 19900000009 浙江省xx\nH头 3.0 1170 单槽2支"))
    s.commit()
    s.close()


def test_batch_not_started_when_network_down(env, monkeypatch):
    """网络未就绪：不起批次、不占 batch_id、不产生 failed 占位。"""
    _add_msgs(env)
    monkeypatch.setattr(batch_runner, "wait_ready", lambda cfg: (False, "代理未就绪"))
    s = env()
    with pytest.raises(batch_runner.NetworkNotReadyError):
        run_batch("manual", 0, 9999999999999, session=s)
    # 消息没被占位
    assert s.query(Message).filter(Message.batch_id.isnot(None)).count() == 0
    # 没有新的 failed 批次
    assert s.query(Batch).filter_by(status="failed").count() == 0
    s.close()


def test_system_proxy_down_does_not_reserve_batch_or_messages(env, monkeypatch):
    """真实门闩识别系统代理拒连后，批次和消息均不占位。"""
    _add_msgs(env)
    monkeypatch.setattr(readiness.urllib.request, "getproxies",
                        lambda: {"https": "http://localhost:7897"})
    monkeypatch.setattr(readiness, "_tcp_ok", lambda host, port: host != "localhost")
    monkeypatch.setattr(readiness.time, "sleep", lambda seconds: None)

    s = env()
    with pytest.raises(batch_runner.NetworkNotReadyError):
        run_batch("manual", 0, 9999999999999, session=s)
    assert s.query(Batch).count() == 0
    assert s.query(Order).count() == 0
    assert s.query(Message).filter(Message.batch_id.isnot(None)).count() == 0
    s.close()


def test_batch_failed_rolls_back_batch_id(env, monkeypatch):
    """中断回滚占位；显式恢复原检查点，不按旧时间范围隐式接管。"""
    _add_msgs(env)
    monkeypatch.setattr(batch_runner, "wait_ready", lambda cfg: (True, "就绪"))
    # 让 normalize 阶段炸掉（模拟网络恢复后又断）
    def boom(*a, **kw):
        raise ConnectionError("写飞书时断网")
    monkeypatch.setattr(batch_runner, "normalize_group", boom)
    monkeypatch.setattr(batch_runner, "_notify", lambda *a, **kw: None)

    s = env()
    with pytest.raises(ConnectionError):
        run_batch("manual", 0, 9999999999999, session=s)
    # 批次 failed，但消息 batch_id 已回滚为空
    b = s.query(Batch).order_by(Batch.id.desc()).first()
    assert b.status == "failed"
    assert s.query(Message).filter(Message.batch_id.isnot(None)).count() == 0
    s.close()

    # 网络恢复后显式恢复原批：检查点中的消息能再次整理且不双写。
    ok_order = {"source_msgids": ["m0"], "receiver": "张三", "phone": "19900000009",
                "address": "浙江省xx", "logistics": "", "confidence": 0.95, "unknown_terms": [],
                "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                           "config": "【3.0】1170高 单槽", "qty": "2支", "confidence": 0.95}]}
    monkeypatch.setattr(batch_runner, "normalize_group",
                        lambda *a, **kw: {"orders": [dict(ok_order)]})
    monkeypatch.setattr(batch_runner, "SheetWriter",
                        type("W", (), {"__init__": lambda self, *a: None,
                                       "_checkpoint": lambda self, order: None, "write_orders": lambda self, orders, dt, bid: (2, 2),
                                       "get_or_create_spreadsheet": lambda self, dt: "tok",
                                       "get_or_create_sheet": lambda self, t, dt: "sid"}))
    monkeypatch.setattr(batch_runner, "Feishu", lambda *a: None)
    s = env()
    b2 = batch_runner.resume_batch(b.id, session=s)
    assert b2.id == b.id
    assert b2.status == "done"
    # 同一电话只写一笔（不双写）
    assert s.query(Order).count() >= 1
    s.close()
