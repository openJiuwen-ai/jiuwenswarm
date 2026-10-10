"""Offline audit and resource tables. No model calls and no inferred human scores."""
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def audit(root):
    root = Path(root).resolve()
    state = json.loads((root / 'workspace.json').read_text(encoding='utf-8'))
    rows, outputs = [], {}
    checked = 0
    requests = state.get('api_requests', [])
    if len({r['id'] for r in requests}) != len(requests):
        raise ValueError('Duplicate request IDs')
    for run in state.get('pilots', []):
        base = (root / 'pilots' / run['id']).resolve()
        if base.parent != root / 'pilots':
            raise ValueError('Invalid run path')
        for name, checksum in run['files'].items():
            path = (base / name).resolve()
            if path.parent != base or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError(f'Artifact mismatch: {run["id"]}/{name}')
            checked += 1
        manifest = json.loads((base / 'manifest.json').read_text(encoding='utf-8'))
        if any(manifest[k] != run[k] for k in ('id', 'case_id', 'results', 'protocol', 'trace')):
            raise ValueError('Workspace and manifest disagree')
        usage = [r for r in requests if r['task_id'] == run['id']]
        traces = [t for t in run['trace'] if 'request_id' in t]
        if len(usage) != run['api_calls'] or {r['id'] for r in usage} != {t['request_id'] for t in traces}:
            raise ValueError('Request count/trace mismatch')
        for req in usage:
            if req['status'] == 'completed':
                saved = json.loads((base / (req['id'] + '-output.json')).read_text(encoding='utf-8'))
                if any(saved['usage'][k] != req[k] for k in ('input_tokens', 'output_tokens')):
                    raise ValueError('Usage differs from preserved response')
        sums = {k: sum(r.get(k, 0) for r in usage) for k in ('input_tokens', 'output_tokens', 'held_units', 'estimated_units')}
        if any(round(run[k] * 1_000_000) != sums[v] for k, v in [('held_cny', 'held_units'), ('estimated_cny', 'estimated_units')]):
            raise ValueError('Run resource totals disagree')
        row = dict(id=run['id'], case_id=run['case_id'], protocol=run['protocol']['version'], calls=len(usage), outputs=len(run['results']), **sums)
        row['cost_note'] = 'Shared preparation/selection included; do not attribute first-variant cost as an independent arm cost.'
        rows.append(row)
        for result in run['results']:
            key = (run['id'], result['method'])
            outputs[key] = dict(run_id=run['id'], case_id=run['case_id'], protocol=row['protocol'], method=result['method'],
                answer_sha256=digest(result['answer']), answer_correct=None, citation_support=None, review_scope=None,
                gate=result.get('output_gate'), abstain=result['answer'].get('abstain'))
    for review in state.get('independent_reviews', []):
        key = (review['run_id'], review['method'])
        if key not in outputs or outputs[key]['answer_sha256'] != review['answer_sha256']:
            raise ValueError('Review does not match preserved answer')
        source = (root.parent / review['source_file']).resolve()
        if not source.is_relative_to(root.parent) or hashlib.sha256(source.read_bytes()).hexdigest() != review['source_sha256']:
            raise ValueError('Review source mismatch')
        if outputs[key]['review_scope'] is not None:
            raise ValueError('Multiple ratings require explicit adjudication; refusing overwrite')
        outputs[key].update({k: review.get(k) for k in ('answer_correct', 'citation_support')})
        outputs[key]['review_scope'] = review['scope']
    grouped = defaultdict(lambda: dict(calls=0, outputs=0, input_tokens=0, output_tokens=0, held_units=0, estimated_units=0, runs=0))
    for row in rows:
        group = grouped[row['protocol']]
        for k in group:
            group[k] += 1 if k == 'runs' else row[k]
    return dict(schema_version=1, scope='Development data only; not held-out, not a significance test',
        checked_artifacts=checked, unique_cases=len({r['case_id'] for r in rows}), runs=rows,
        protocols=dict(grouped), answers=list(outputs.values()), reviewed_outputs=sum(o['review_scope'] is not None for o in outputs.values()),
        total_requests=len(requests), nonpilot_requests=sum(r['task_id'] not in {x['id'] for x in rows} for r in requests),
        usage_statuses={s:sum(r['status']==s for r in requests) for s in sorted({r['status'] for r in requests})},
        total={k:sum(r.get(k,0) for r in requests) for k in ('input_tokens','output_tokens','held_units','estimated_units')},
        actual_invoice_cny=None, semantic_gate_claim=False)


def export(root, out):
    report = audit(root)  # Fail before producing any updated report on invalid evidence.
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    (out/'audit-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines=['# Audited development resources', '', 'Estimates use configured conservative rates; actual invoice unknown. No API calls made by this command.', '',
           '|Protocol|Jobs|Answers|Calls|Input tokens|Output tokens|Estimated CNY|Reserved CNY|', '|---|---:|---:|---:|---:|---:|---:|---:|']
    tex=[r'\begin{tabular}{lrrrrr}', r'\hline Protocol & Jobs & Answers & Calls & Tokens & Est. CNY \\', r'\hline']
    for name, g in sorted(report['protocols'].items()):
        lines.append(f'|{name}|{g["runs"]}|{g["outputs"]}|{g["calls"]}|{g["input_tokens"]}|{g["output_tokens"]}|{g["estimated_units"]/1e6:.6f}|{g["held_units"]/1e6:.6f}|')
        tex.append(f'{name} & {g["runs"]} & {g["outputs"]} & {g["calls"]} & {g["input_tokens"]+g["output_tokens"]} & {g["estimated_units"]/1e6:.6f} '+r'\\')
    tex += [r'\hline', r'\end{tabular}']
    lines += ['', f'Only {report["unique_cases"]} distinct development cases; {report["reviewed_outputs"]} outputs have one teammate rating. Pending ratings remain null.', '', 'Ablation preparation and source selections are shared. Arm costs are not independent. Hashes detect local mismatch, not external authenticity.']
    (out/'resource_report.md').write_text('\n'.join(lines), encoding='utf-8')
    (out/'resources.tex').write_text('\n'.join(tex), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args=parser.parse_args()
    r=export(args.research,args.out)
    print(json.dumps({k:r[k] for k in ('checked_artifacts','unique_cases','total_requests','reviewed_outputs','total')}))
