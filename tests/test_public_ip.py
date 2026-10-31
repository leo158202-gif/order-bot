from app.net import public_ip


def test_extract_ipv4():
    assert public_ip._extract_ipv4("您的IP是：[123.45.67.89] 来自：xx") == "123.45.67.89"
    assert public_ip._extract_ipv4("1.2.3.4") == "1.2.3.4"
    assert public_ip._extract_ipv4("999.1.1.1") is None
    assert public_ip._extract_ipv4("没有IP") is None


def test_majority_vote(monkeypatch):
    fake = {
        "a": "1.2.3.4",
        "b": "1.2.3.4",
        "c": "5.6.7.8",
    }
    monkeypatch.setattr(public_ip, "query_one", lambda url, timeout=5: fake[url])
    r = public_ip.get_public_ip(["a", "b", "c"])
    assert r["ip"] == "1.2.3.4"
    assert r["network_ok"] is True
    assert "不一致" in r["error"]


def test_single_success(monkeypatch):
    fake = {"a": "1.2.3.4", "b": None, "c": None}
    monkeypatch.setattr(public_ip, "query_one", lambda url, timeout=5: fake[url])
    r = public_ip.get_public_ip(["a", "b", "c"])
    assert r["ip"] == "1.2.3.4"
    assert r["network_ok"] is True


def test_all_failed_dns_down(monkeypatch):
    monkeypatch.setattr(public_ip, "query_one", lambda url, timeout=5: None)

    def fake_gaierror(host, port):
        raise socket.gaierror()

    import socket
    monkeypatch.setattr(socket, "getaddrinfo", fake_gaierror)
    r = public_ip.get_public_ip(["https://x1.example", "https://x2.example"])
    assert r["ip"] is None
    assert r["network_ok"] is False
