r"""C1：受控测试告警（仅测试群）或 dry-run。

用法（仓库根目录）：
  .venv\Scripts\python.exe scripts\alert_test.py --dry-run
  .venv\Scripts\python.exe scripts\alert_test.py --send --workspace data\test-runtime-r18
      # 向隔离目录登记的测试群发一条 kind=test；同目录回执存在则拒绝重发

禁止：target!=test、无隔离目录、正式群。不写生产库。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.net import alerts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只验证通道与文案，不发送、不写库")
    g.add_argument("--send", action="store_true", help="向测试群发一条受控测试告警")
    p.add_argument("--workspace", type=Path,
                   help="已准备的测试隔离目录（--send 必填）")
    p.add_argument("--receipt", default="c1-alert-test-receipt.json",
                   help="隔离 data/ 下回执文件名，防重复发送")
    args = p.parse_args()

    if args.dry_run:
        cfg = alerts.load_cfg(ROOT / "data" / "config.yaml")
        if cfg.get("target") != "test":
            print(f"拒绝：当前 target={cfg.get('target')!r}，C1 只允许测试通道")
            return 2
        chat, reason = alerts.resolve_alert_chat(cfg)
        if not chat:
            print(f"拒绝：无法解析告警 chat（{reason}）")
            return 2
        text = alerts.build_text(
            "test",
            "C1 告警自动触发受控验证；dry-run 不会发出",
            时间=datetime.now(alerts.TZ).isoformat(timespec="seconds"),
        )
        result = alerts.maybe_send("test", text, cfg=cfg, dry_run=True, force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if not args.workspace:
        print("拒绝：--send 必须指定 --workspace <测试隔离目录>")
        return 2
    from app.db import init_db
    from app.test_runtime import enter
    enter(args.workspace.resolve())
    init_db()
    cfg = alerts.load_cfg()
    if cfg.get("target") != "test":
        print(f"拒绝：workspace target={cfg.get('target')!r}")
        return 2
    chat, reason = alerts.resolve_alert_chat(cfg)
    if not chat:
        print(f"拒绝：无法解析告警 chat（{reason}）")
        return 2

    text = alerts.build_text(
        "test",
        "C1 告警自动触发受控验证；若收到本条说明测试通道可用",
        时间=datetime.now(alerts.TZ).isoformat(timespec="seconds"),
    )
    receipt = Path("data") / Path(args.receipt).name
    if receipt.exists():
        print(f"已有回执 {receipt}，拒绝重复发送。删除该文件后才能再发。")
        print(receipt.read_text(encoding="utf-8")[:500])
        return 3

    state = {"state": "prepared", "chat_id": chat, "via": reason, "text": text}
    receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    state["state"] = "inflight"
    receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    result = alerts.maybe_send("test", text, cfg=cfg, force=True)
    state.update(result=result, state=result.get("action", "unknown"))
    receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") and result.get("action") == "sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
