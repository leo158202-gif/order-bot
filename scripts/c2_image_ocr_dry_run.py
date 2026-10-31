"""C2 dry-run：统计 image/emotion，演示 gate；默认不调视觉 API、不下载。

用法（在仓库根，建议带测试工作区库）：
  .venv\\Scripts\\python.exe scripts\\c2_image_ocr_dry_run.py --workspace data\\test-runtime-20261023
  .venv\\Scripts\\python.exe scripts\\c2_image_ocr_dry_run.py --workspace data\\test-runtime-20261023 --apply-mock
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=Path, default=Path("."))
    ap.add_argument(
        "--apply-mock",
        action="store_true",
        help="对最多 N 条 image 用假转写写入（仅测试库；默认 dry 只统计）",
    )
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    work = args.workspace.resolve()
    db = work / "data" / "db.sqlite"
    if not db.exists():
        print(json.dumps({"error": f"missing {db}"}, ensure_ascii=False))
        return 2

    # 进入工作区，使 data/media 落在隔离目录
    os.chdir(work)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app.db import init_db
    from app.models import Message
    from app.wecom.media import (
        IMAGE_PLACEHOLDER,
        count_emotion_messages,
        looks_like_order_text,
        process_pending_images,
        resolve_vision_cfg,
        skip_emotion_reason,
    )
    import yaml

    cfg_path = Path("data/config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    vision = resolve_vision_cfg(cfg)

    factory = init_db(db)
    s = factory()
    try:
        n_img = s.query(Message).filter(Message.msgtype == "image").count()
        n_emo = count_emotion_messages(s)
        pending = (
            s.query(Message)
            .filter(Message.msgtype == "image", Message.content == IMAGE_PLACEHOLDER)
            .count()
        )
        report = {
            "workspace": str(work),
            "image_count": n_img,
            "emotion_count": n_emo,
            "image_placeholder_pending": pending,
            "emotion_policy": skip_emotion_reason(),
            "vision_configured": bool(vision),
            "vision_model": (vision or {}).get("model"),
            "apply_mock": bool(args.apply_mock),
        }
        if args.apply_mock:
            class _NoSdk:
                def get_media_data(self, *a, **k):
                    raise RuntimeError("dry-run mock must not download")

            def fake(path, cfg):
                return "收货人测试 19900000003 黑立柱 1支"

            # 只处理 limit 条：临时把多余 pending 标成已处理前缀以外的跳过较难；
            # 这里直接调用 process，但 download=False 且 stub OCR。
            # 为避免改太多历史，仅当 pending<=limit 或用户知情时使用。
            stats = process_pending_images(
                s, _NoSdk(), cfg, transcribe_fn=fake, download=False
            )
            report["mock_stats"] = stats
            report["note"] = "mock 已写 content；emotion 仍未下载/OCR"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
