import logging
from unittest.mock import Mock, call

import pytest

from app.webview_runner import start_webview


def test_edge_success_does_not_fallback(caplog):
    backend = Mock()
    with caplog.at_level(logging.INFO):
        start_webview(backend)
    assert backend.start.call_args_list == [call(gui='edgechromium')]
    assert 'webview backend: edgechromium' in caplog.text


def test_edge_failure_uses_existing_default_fallback(caplog):
    backend = Mock()
    backend.start.side_effect = [RuntimeError('synthetic missing runtime'), None]
    start_webview(backend)
    assert backend.start.call_args_list == [call(gui='edgechromium'), call()]
    assert 'WebView2' in caplog.text
    assert 'webview backend: edgechromium' not in caplog.text


def test_both_backends_fail_without_retry_loop():
    backend = Mock()
    backend.start.side_effect = [RuntimeError('first'), RuntimeError('second')]
    with pytest.raises(RuntimeError, match='second'):
        start_webview(backend)
    assert backend.start.call_count == 2


def test_interrupt_does_not_restart_backend():
    backend = Mock()
    backend.start.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        start_webview(backend)
    assert backend.start.call_count == 1
