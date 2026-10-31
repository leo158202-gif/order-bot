"""Only an atomically published, hash-matched prompt can enter normalization."""
import hashlib
import json
import os
from pathlib import Path


def active_prompt(default):
    root = Path(os.environ.get('ORDER_BOT_DATA_DIR', 'data'))
    path = root / 'diagnostician' / 'current_prompt.txt'
    if not path.exists():
        return default
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('提示词路径未通过版本校验')
    manifest = root / 'diagnostician' / 'versions' / 'current.json'
    published = json.loads(manifest.read_text(encoding='utf-8')).get('prompt') if manifest.exists() else None
    text = path.read_text(encoding='utf-8')
    if not published or hashlib.sha256(text.encode()).hexdigest() != published.get('sha256'):
        raise ValueError('提示词发布尚未完成，保留原消息待核')
    from app.pipeline.logistics_prompt_policy import compatible_prompt_core, remove_legacy_H_guidance
    text = compatible_prompt_core(text, default)
    return remove_legacy_H_guidance(text)
