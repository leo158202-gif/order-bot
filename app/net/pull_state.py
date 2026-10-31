"""拉取白名单状态：红条以「拉取是否因白名单失败」为准，而非仅比对本地 IP 名单（#0）。

puller 每次拉取后调用 report_success / report_error；
主界面 api_status 读 is_whitelist_blocked() 决定是否弹「IP 已变化」红条。
拉取成功即自动消阻断并登记当前 IP（运营商轮换回来后自动恢复）。
"""
from __future__ import annotations

import threading

# 企微白名单相关错误码：301042（GetChatData whiteip not match）、10009（SDK IP 非法）
WHITELIST_ERRCODES = {301042, 10009}

_lock = threading.Lock()
_blocked = False
_last_error_code: int | None = None
_auto_registered: str | None = None
_has_result = False  # 是否已有过至少一次拉取结果（重启后为 False，先显示「检测中」）


def reset() -> None:
    global _blocked, _last_error_code, _auto_registered, _has_result
    with _lock:
        _blocked = False
        _last_error_code = None
        _auto_registered = None
        _has_result = False


def report_success(current_ip: str | None = None) -> None:
    """拉取成功：消阻断；若之前在阻断状态且拿到当前 IP，自动登记（轮换恢复）。"""
    global _blocked, _last_error_code, _auto_registered, _has_result
    with _lock:
        was_blocked = _blocked
        _blocked = False
        _last_error_code = None
        _has_result = True
        if was_blocked and current_ip:
            _auto_registered = current_ip


def report_error(code: int, msg: str = "") -> None:
    """拉取失败：仅白名单错误码（301042/10009）置阻断；其他错误不动红条。"""
    global _blocked, _last_error_code, _has_result
    with _lock:
        _last_error_code = code
        _has_result = True
        if code in WHITELIST_ERRCODES:
            _blocked = True


def has_result() -> bool:
    """是否已有过拉取结果（False=重启后还没拉过，前端显示「检测中」，不默认绿）。"""
    with _lock:
        return _has_result


def is_whitelist_blocked() -> bool:
    with _lock:
        return _blocked


def last_error_code() -> int | None:
    with _lock:
        return _last_error_code


def auto_registered_ip() -> str | None:
    with _lock:
        return _auto_registered


def reset_auto_registered() -> None:
    """清掉一次性自动登记标记（登记完后调用，避免重复登记）。"""
    global _auto_registered
    with _lock:
        _auto_registered = None
