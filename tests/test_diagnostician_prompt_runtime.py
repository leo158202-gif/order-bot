import hashlib
import json

import pytest

from app.diagnostician.prompt_runtime import active_prompt


def test_only_committed_current_prompt_enters_future_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv('ORDER_BOT_DATA_DIR', str(tmp_path))
    assert active_prompt('core') == 'core'
    root = tmp_path / 'diagnostician'
    root.mkdir()
    prompt = root / 'current_prompt.txt'
    prompt.write_text('core\nverified case', encoding='utf-8')
    with pytest.raises(ValueError): active_prompt('core')
    versions = root / 'versions'; versions.mkdir()
    (versions / 'current.json').write_text(json.dumps({'prompt': {'sha256': hashlib.sha256(prompt.read_text(encoding='utf-8').encode()).hexdigest()}}))
    assert active_prompt('core') == 'core\nverified case'
    with pytest.raises(ValueError): active_prompt('changed core')
    prompt.write_text('core\nunpublished edit', encoding='utf-8')
    with pytest.raises(ValueError): active_prompt('core')
