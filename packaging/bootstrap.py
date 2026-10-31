"""Small stable bootstrap; selected application source stays versioned."""
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parent
pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
version = pointer.get('current', '')
if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', version):
    raise RuntimeError('invalid current version')
release = root / 'releases' / version
if release.is_symlink() or not release.resolve().is_relative_to(root / 'releases'):
    raise RuntimeError('release path escaped installation')
manifest = json.loads((release / 'release-files.json').read_text(encoding='utf-8'))
for name, digest in manifest['files'].items():
    path = release / name
    if path.is_symlink() or not path.resolve().is_relative_to(release) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError('release file verification failed: ' + name)
sys.path.insert(0, str(release))
from app.launcher import main
main(root)
