"""Trusted fixed entry. Candidate business modules execute only inside AppContainer."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from business.formatting import transform

payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
result = transform(payload)
Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
