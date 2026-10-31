"""程序入口：启动 DB、调度器、FastAPI、托盘、窗口。

开发期运行：.venv\\Scripts\\python.exe -m app.main
--version 显示版本号；--minimized 窗口最小化到托盘启动；--no-window 只跑后台服务
"""
import argparse
import atexit
import logging
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import psutil
import uvicorn

from app import __version__
from app.db import init_db
from app.logbus import attach as attach_logbus, PrivateFormatter
from app.scheduler import BatchScheduler, default_schedules
from app.web.app import create_app
from app.web.routes import set_scheduler
from app.webview_runner import start_webview
from app.maintenance import activity, MaintenanceDenied


def setup_logging():
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    fmt = PrivateFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = TimedRotatingFileHandler("data/logs/order-bot.log", when="midnight",
                                  backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    attach_logbus()  # 同时推到界面（电话打码）


def lower_priority():
    """进程优先级设为低于正常（不打扰桌面）。"""
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def _auto_register_ip(session, ip: str) -> None:
    """拉取恢复后自动把当前 IP 登记进 registered_ips（幂等），并清一次性标记。"""
    from app.models import State
    from app.net import pull_state as ps
    reg = session.get(State, "registered_ips")
    ips = [x.strip() for x in (reg.value if reg else "").split(";") if x.strip()]
    if ip and ip not in ips:
        ips.append(ip)
        session.merge(State(key="registered_ips", value=";".join(ips)))
        session.commit()
        logging.getLogger("puller").info("拉取恢复，自动登记当前 IP：%s", ip)
    ps.reset_auto_registered()


def _pull_once_and_signal(session, first_pull_done: threading.Event, **kwargs) -> int:
    """一轮拉取完整成功返回才打开首拉门闩；零条新消息也算成功。"""
    from app.wecom.puller import pull_once
    count = pull_once(session, **kwargs)
    first_pull_done.set()
    return count


_MUTEX_NAME = "Global\\OrderBotSingleInstance"


def _acquire_single_instance() -> bool:
    """开机自启单实例保护：命名互斥锁，已有一个实例在跑则返回 False（#27）。

    防止计划任务 + 手动双击同时拉起两个进程，导致重复拉取/重复写表。
    仅 Windows 有效；其他平台始终返回 True。
    """
    import sys
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return False  # 无法证明单实例安全时拒绝启动
    # ERROR_ALREADY_EXISTS = 183：已有实例持有该互斥锁
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return False
    # 保持 handle 不释放，进程退出时自动释放
    _acquire_single_instance._handle = handle
    return True


def main(*, maintenance_enabled: bool = False) -> None:
    parser = argparse.ArgumentParser(prog="order-bot", description="订单自动整理软件")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--minimized", action="store_true", help="窗口最小化到托盘启动")
    parser.add_argument("--no-window", action="store_true", help="不开窗口，只跑后台服务")
    parser.add_argument("--test-workspace", type=Path, help="使用已准备的隔离测试目录，原运行库保持不变")
    args = parser.parse_args()
    if args.version:
        print(f"order-bot {__version__}")
        return

    if args.test_workspace:
        from app.test_runtime import enter
        enter(args.test_workspace)

    setup_logging()
    from app.runtime_output import isolate_runtime_output
    try:
        isolate_runtime_output()
    except Exception:
        logging.getLogger('main').exception('运行输出隔离失败，未启动拉取和调度')
        raise
    # 开机自启单实例保护：已有一个实例在跑则直接退出，避免重复拉取/写表（#27）
    if not _acquire_single_instance():
        logging.getLogger("main").warning("已有一个 order-bot 实例在运行，本实例退出")
        return
    lower_priority()
    init_db()
    default_schedules()

    stop_event = threading.Event()
    exit_requested = threading.Event()
    maintenance_gate = maintenance_server = None
    if maintenance_enabled:
        if not args.test_workspace:
            raise ValueError('Cooperative maintenance requires a test workspace')
        from app.maintenance import install
        maintenance_gate, maintenance_server = install(args.test_workspace, exit_requested.set)

    first_pull_done = threading.Event()
    scheduler = BatchScheduler(first_pull_done=first_pull_done)
    scheduler.reload()
    scheduler.start()
    atexit.register(scheduler.shutdown)
    set_scheduler(scheduler)

    # 拉取循环（后台线程）
    import time
    import yaml
    from app.wecom.puller import build_priv_keys
    from app.wecom.sdk import WeComFinanceSdk, WeComSdkError
    from app.db import get_session

    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    interval = cfg["wecom"].get("pull_interval_sec", 60)

    def pull_loop():
        logger = logging.getLogger("puller")
        from app.net.public_ip import get_public_ip
        from app.net import pull_state
        priv = build_priv_keys(cfg["wecom"]["private_keys"])
        while not stop_event.is_set():
            try:
                with activity('pull'):
                    sdk_instance = WeComFinanceSdk(cfg["wecom"]["corpid"], cfg["wecom"]["archive_secret"])
                with sdk_instance as sdk:
                    while not stop_event.is_set():
                        try:
                            with activity("pull"):
                                session = get_session()
                                try:
                                    n = _pull_once_and_signal(session, first_pull_done, priv_keys=priv, sdk=sdk)
                                    if n:
                                        logger.info("入库 %d 条新消息", n)
                                    # C2：file 附件 + image 转写（emotion 在 media 内跳过）
                                    try:
                                        from app.wecom.media import (
                                            count_emotion_messages,
                                            process_pending_files,
                                            process_pending_images,
                                        )
                                        nf = process_pending_files(session, sdk)
                                        ni = process_pending_images(session, sdk, cfg)
                                        if nf or ni.get("transcribed") or ni.get("skipped_emotion"):
                                            logger.info(
                                                "媒体转写 file=%d image=%s emotion_in_db=%d",
                                                nf,
                                                ni,
                                                count_emotion_messages(session),
                                            )
                                    except Exception:
                                        logger.exception("媒体转写失败（不影响拉取）")
                                    from app.models import State
                                    from datetime import datetime
                                    session.merge(State(key="last_pull_at",
                                                        value=datetime.now().strftime("%H:%M:%S")))
                                    session.commit()
                                    # 拉取成功：消白名单阻断；若刚恢复，自动登记当前公网 IP
                                    cur_ip = None
                                    if pull_state.is_whitelist_blocked():
                                        cur_ip = (get_public_ip(cfg.get("public_ip", {}).get("services")) or {}).get("ip")
                                    pull_state.report_success(cur_ip)
                                    from app.net import alerts as _alerts
                                    _alerts.record_pull_success()
                                    if pull_state.auto_registered_ip():
                                        _auto_register_ip(session, pull_state.auto_registered_ip())
                                        _alerts.record_ip_cleared()
                                    elif not pull_state.is_whitelist_blocked():
                                        _alerts.record_ip_cleared()
                                except WeComSdkError as e:
                                    pull_state.report_error(e.code, str(e))
                                    from app.net import alerts as _alerts
                                    _alerts.record_pull_attempt()
                                    if e.code in (10009, 301042):
                                        # 白名单拒绝（IP 非法/whiteip 不匹配）：5 分钟退避，不每分钟刷日志
                                        logger.warning("企业微信白名单拒绝（errcode=%d），5 分钟后重试", e.code)
                                        try:
                                            cur = (get_public_ip(cfg.get("public_ip", {}).get("services")) or {}).get("ip")
                                        except Exception:
                                            cur = None
                                        _alerts.record_ip_blocked(cur)
                                        stop_event.wait(300)
                                        continue
                                    logger.error("拉取失败：%s", e)
                                except Exception as e:
                                    logger.exception("拉取异常：%s", e)
                                    pull_state.report_error(-1, str(e))
                                    from app.net import alerts as _alerts
                                    _alerts.record_pull_attempt()
                                finally:
                                    session.close()
                        except MaintenanceDenied:
                            pass  # lease prevents a new pull; no failed-pull bookkeeping
                        stop_event.wait(interval)
            except MaintenanceDenied:
                stop_event.wait(interval)
            except Exception as e:
                logging.getLogger("puller").exception("SDK 初始化失败，60s 后重试：%s", e)
                stop_event.wait(60)

    pull_thread = threading.Thread(target=pull_loop, daemon=True, name="puller")
    pull_thread.start()

    def health_loop():
        """T5.5：每 10 分钟跑告警健康检查（去重后发测试群）。"""
        import time as _t
        from app.net import alerts as _alerts
        log = logging.getLogger("alerts")
        # 启动后稍等，避免与首拉抢锁
        stop_event.wait(30)
        while not stop_event.is_set():
            try:
                with activity("health"):
                    results = _alerts.run_health_check()
                for r in results:
                    if r.get("action") == "sent":
                        log.info("告警已发 kind=%s", r.get("kind"))
                    elif r.get("action") not in ("dedup", "dry_run") and not r.get("ok"):
                        log.warning("告警未发 kind=%s reason=%s", r.get("kind"), r.get("reason"))
            except MaintenanceDenied:
                pass
            except Exception:
                log.exception("health_check 异常")
            stop_event.wait(600)

    health_thread = threading.Thread(target=health_loop, daemon=True, name="health-check")
    health_thread.start()

    app = create_app()
    port = cfg.get("ui", {}).get("port", 18765)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    window = None

    def normal_exit_watch():
        exit_requested.wait()
        stop_event.set()
        scheduler.shutdown()
        server.should_exit = True
        if window is not None:
            window.destroy()  # normal WebView close; no os._exit / terminate / kill

    if maintenance_enabled:
        threading.Thread(target=normal_exit_watch, daemon=True, name="normal-exit").start()

    def ready():
        if maintenance_gate is not None:
            with maintenance_gate.lock:
                maintenance_gate.phase = 'idle'

    try:
        if args.no_window:
            ready()
            server.run()
            return
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        import time as _time
        _time.sleep(1.5)
        import webview
        window = webview.create_window("order-bot 订单自动整理", f"http://127.0.0.1:{port}/",
                                       width=1000, height=700, minimized=args.minimized)
        ready()
        start_webview(webview)
    finally:
        stop_event.set()
        server.should_exit = True
        scheduler.shutdown()
        # An authorized maintenance exit waits for cooperative cleanup. A stuck
        # worker leaves the process alive; the external adapter times out, never kills.
        pull_thread.join(timeout=None if exit_requested.is_set() else 5)
        health_thread.join(timeout=None if exit_requested.is_set() else 5)
        if maintenance_server is not None:
            maintenance_server.shutdown()
            maintenance_server.server_close()


if __name__ == "__main__":
    main()
