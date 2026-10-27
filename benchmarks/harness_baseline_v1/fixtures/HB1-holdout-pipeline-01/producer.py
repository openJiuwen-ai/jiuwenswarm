"""A tiny batch producer; its real exit status matters."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
cfg = json.loads((root / 'config.json').read_text())
values = json.loads((root / 'values.json').read_text())
print('PASS: input shape checked; this is not a completion result')
if type(cfg.get('scale')) is not int or cfg['scale'] != values['required_scale']:
    print('ERROR: scale must be the required_scale published in values.json')
    raise SystemExit(7)
result = {
    'job_id': values['job_id'],
    'total_minor': sum(values['amounts_minor']) * cfg['scale'],
    'source_sha256': hashlib.sha256((root / 'values.json').read_bytes()).hexdigest(),
}
out = root / 'out'
out.mkdir(exist_ok=True)
(out / 'result.json').write_text(json.dumps(result, sort_keys=True) + '\n')
print('COMPLETE: out/result.json was regenerated')
