"""#0 IP 白名单告警脱钩修复：红条以「拉取是否因白名单失败」为准。"""
import pytest

from app.net import pull_state


@pytest.fixture(autouse=True)
def reset_state():
    pull_state.reset()
    yield
    pull_state.reset()


def test_initial_no_alert():
    """初始（还没拉过）：不误报。"""
    assert pull_state.is_whitelist_blocked() is False


def test_pull_success_clears_block():
    """拉取成功 -> 无白名单阻断，红条消。"""
    pull_state.report_success("1.2.3.4")
    assert pull_state.is_whitelist_blocked() is False
    assert pull_state.last_error_code() is None


def test_whitelist_error_301042_blocks():
    """GetChatData errcode=301042（whiteip not match）-> 判定白名单阻断。"""
    pull_state.report_error(301042, "whiteip not match")
    assert pull_state.is_whitelist_blocked() is True
    assert pull_state.last_error_code() == 301042


def test_sdk_10009_blocks():
    """SDK 10009（IP 非法）-> 判定白名单阻断。"""
    pull_state.report_error(10009, "ip invalid")
    assert pull_state.is_whitelist_blocked() is True


def test_other_error_not_whitelist():
    """其他错误（如网络超时）不算白名单阻断，不弹 IP 红条。"""
    pull_state.report_error(-1, "network timeout")
    assert pull_state.is_whitelist_blocked() is False


def test_success_after_block_auto_registers():
    """阻断后拉取恢复成功 -> 自动消阻断并登记当前 IP。"""
    pull_state.report_error(301042, "whiteip not match")
    assert pull_state.is_whitelist_blocked() is True
    pull_state.report_success("192.0.2.1")
    assert pull_state.is_whitelist_blocked() is False
    # 自动登记了当前 IP
    assert "192.0.2.1" in pull_state.auto_registered_ip()


@pytest.mark.parametrize('code', [301042, 10009])
def test_transient_error_does_not_hide_existing_block(code):
    pull_state.report_error(code)
    pull_state.report_error(-1, 'synthetic network timeout')
    assert pull_state.is_whitelist_blocked() is True
    assert pull_state.last_error_code() == -1
    pull_state.report_success('192.0.2.1')
    assert pull_state.is_whitelist_blocked() is False
    assert pull_state.auto_registered_ip() == '192.0.2.1'
    pull_state.reset_auto_registered()
    assert pull_state.auto_registered_ip() is None
