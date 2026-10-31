"""可离线验证的现有 WebView 后端启动策略；不导入或创建真实窗口。"""
import logging


def start_webview(webview) -> None:
    logger = logging.getLogger('main')
    try:
        webview.start(gui='edgechromium')
        logger.info('webview backend: edgechromium (WebView2)')
    except Exception as exc:
        logger.error(
            'WebView2 运行时缺失或异常，嵌入窗口可能卡顿/无法启动：%s。'
            '请安装 Microsoft Edge WebView2 Runtime：https://developer.microsoft.com/microsoft-edge/webview2/', exc)
        webview.start()  # 保留原有默认后端回退，不增加重试。
