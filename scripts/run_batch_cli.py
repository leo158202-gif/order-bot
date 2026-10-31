"""受控批次入口：新窗口或指定批次恢复，统一调用 batch_runner.run_batch。

    python scripts/run_batch_cli.py --start-ms 1000 --end-ms 2000
    python scripts/run_batch_cli.py --batch-id 8

新窗口为半开区间 [start-ms, end-ms)，时间单位是Unix毫秒。
参数校验/help不加载业务入口。实际执行使用本项目目录下的数据与日志。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import logging
from logging.handlers import TimedRotatingFileHandler
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def setup_logging() -> None:
    """与主程序使用相同日志位置和广播；不导入主程序或启动服务。"""
    from app.logbus import attach, PrivateFormatter

    log_path = Path("data/logs/order-bot.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(log_path, when="midnight", backupCount=30,
                                      encoding="utf-8")
    handler.setFormatter(PrivateFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    attach()


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须为整数") from None
    if number < 0:
        raise argparse.ArgumentTypeError("不得小于0")
    return number


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过统一批次入口新建或恢复批次")
    parser.add_argument("--batch-id", type=_nonnegative, help="恢复现有批次ID，不新建批次")
    parser.add_argument("--start-ms", type=_nonnegative, help="历史来源范围起点（Unix毫秒，包含）")
    parser.add_argument("--end-ms", type=_nonnegative, help="历史来源范围终点（Unix毫秒，不包含）")
    parser.add_argument("--kind", choices=("manual", "scheduled"), help="新批次类型，默认manual")
    parser.add_argument("--resend", action="store_true", help="历史范围通知标记为补发")
    parser.add_argument("--workspace", type=Path, help="已准备的独立测试目录（执行时必填）")
    args = parser.parse_args(argv)
    if args.batch_id is not None:
        if args.batch_id == 0:
            parser.error("--batch-id必须大于0")
        if args.start_ms is not None or args.end_ms is not None or args.kind or args.resend:
            parser.error("--batch-id不能与历史来源范围参数混用")
    elif args.start_ms is None or args.end_ms is None:
        parser.error("新批次必须同时指定--start-ms和--end-ms")
    elif args.end_ms <= args.start_ms:
        parser.error("--end-ms必须大于--start-ms")
    else:
        try:
            for timestamp in (args.start_ms, args.end_ms):
                datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            parser.error("来源范围超出有效时间范围")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    # 直接执行脚本时，sys.path起点为scripts；校验完成后才加入项目根目录。
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    if args.workspace is None:
        print('拒绝默认运行库：请指定 --workspace 独立测试目录', file=sys.stderr)
        return 2
    from app.test_runtime import enter
    enter(args.workspace)
    setup_logging()
    runner = importlib.import_module("app.pipeline.batch_runner")
    try:
        if args.batch_id is not None:
            # batch_id分支只认持久化成员及检查点，前三个参数仅满足兼容签名。
            batch = runner.run_batch("manual", 0, 0, resend=True,
                                     trigger="cli:resume", batch_id=args.batch_id)
        else:
            batch = runner.run_batch(args.kind or "manual", args.start_ms, args.end_ms,
                                     resend=args.resend, trigger="cli:new",
                                     source_range=(args.start_ms, args.end_ms))
    except runner.BatchBusyError:
        logging.getLogger(__name__).warning("批次忙，CLI未执行")
        return 2
    except runner.NetworkNotReadyError:
        logging.getLogger(__name__).warning("网络未就绪，CLI未执行")
        return 3
    except Exception:
        logging.getLogger(__name__).exception("CLI批次失败")
        return 1
    print(f"batch_id={batch.id} status={batch.status} orders={batch.order_count}")
    return 0 if batch.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
