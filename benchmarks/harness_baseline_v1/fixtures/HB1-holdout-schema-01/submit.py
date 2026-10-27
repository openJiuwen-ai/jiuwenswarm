"""Local schema API with actionable validation feedback."""
import hashlib
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
if '--help' in sys.argv:
    print('python3 submit.py: read config.json; exact keys request_id, timeout_ms, max_attempts; '
          'timeout_ms and max_attempts must be integers, not bool; values must match request.json; '
          'writes out/receipt.json only after acceptance')
    raise SystemExit(0)
cfg = json.loads((root / 'config.json').read_text())
request = json.loads((root / 'request.json').read_text())
errors = []
if set(cfg) != set(request):
    errors.append('config keys must be exactly ' + ', '.join(sorted(request)))
for name in ('timeout_ms', 'max_attempts'):
    if type(cfg.get(name)) is not int:
        errors.append(name + ' must be an integer (not a string, float or bool)')
for key, expected in request.items():
    if cfg.get(key) != expected:
        errors.append(key + ' must equal ' + repr(expected))
if errors:
    print(json.dumps({'status': 'rejected', 'errors': errors}, ensure_ascii=False))
    raise SystemExit(2)
canonical = json.dumps(cfg, sort_keys=True, separators=(',', ':')).encode()
receipt = {'status': 'accepted', 'request_id': cfg['request_id'],
           'config_sha256': hashlib.sha256(canonical).hexdigest()}
(root / 'out').mkdir(exist_ok=True)
(root / 'out/receipt.json').write_text(json.dumps(receipt, sort_keys=True) + '\n')
print(json.dumps(receipt))
