"""把企业微信会话存档的消息拉进本地数据库（每 60 秒一轮，Ctrl+C 停止）。

用法：
  .venv\\Scripts\\python.exe scripts\\pull_loop.py            # 持续拉
  .venv\\Scripts\\python.exe scripts\\pull_loop.py --once     # 只拉一轮
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from app.db import get_session, init_db
from app.wecom.puller import build_priv_keys, pull_once
from app.wecom.sdk import WeComFinanceSdk, WeComSdkError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("pull_loop")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只拉一轮就退出")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    corpid = cfg["wecom"]["corpid"]
    secret = cfg["wecom"]["archive_secret"]
    interval = cfg["wecom"].get("pull_interval_sec", 60)
    priv_keys = build_priv_keys(cfg["wecom"]["private_keys"])

    init_db()

    with WeComFinanceSdk(corpid, secret) as sdk:
        while True:
            session = get_session()
            try:
                n = pull_once(session, priv_keys=priv_keys, sdk=sdk)
                if n:
                    logger.info("入库 %d 条新消息", n)
                else:
                    logger.info("无新消息")
            except WeComSdkError as e:
                logger.error("SDK 错误：%s", e)
            except Exception as e:
                logger.exception("拉取异常：%s", e)
            finally:
                session.close()
            if args.once:
                break
            time.sleep(interval)


if __name__ == "__main__":
    main()
