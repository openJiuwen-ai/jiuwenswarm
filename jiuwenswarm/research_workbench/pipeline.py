"""Offline stage handoffs from immutable experiment artifacts to paper materials.

Run: python -m jiuwenswarm.research_workbench.pipeline --research PATH
No model call, shell execution, upload, or scoring guess is made by this module.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .fulltext_runner import FOLDER, POLICY, METHODS, preflight, load_bundle
from .review_scoring import digest, reconcile, statistics_report


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def put(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


def audit(research):
    research = Path(research).resolve()
    state = read(research/'workspace.json')
    store = SimpleNamespace(root=research, read=lambda: state)
    checks = preflight(store)
    if not checks['ready']:
        raise ValueError('Frozen source/code/budget preflight failed: ' + ', '.join(checks['blockers']))
    plan, cases, corpus = load_bundle(store)
    protocol = digest({'plan': plan, 'cases': cases, 'index': corpus, 'policy': POLICY})
    case_map = {c['case_id']: c for c in cases}
    expected = {(c, r) for c in case_map for r in range(plan['repetitions'])}
    records, jobs, seen, response_ids, checked_files = [], [], set(), set(), 0
    for job in state.get('fulltext_runs', []):
        if job['kind'] != 'live_fulltext_experiment':
            continue
        pair = (job['case_id'], job['repetition'])
        if pair not in expected or pair in seen or job['protocol_hash'] != protocol:
            raise ValueError('Run identity/protocol mismatch')
        seen.add(pair)
        folder = (research/'fulltext-runs'/job['id']).resolve()
        if folder.parent != (research/'fulltext-runs').resolve():
            raise ValueError('Invalid run path')
        if 'manifest.json' not in job['files']:
            raise ValueError('Missing manifest hash')
        for name, checksum in job['files'].items():
            path = (folder/name).resolve()
            if not path.is_relative_to(folder) or sha(path) != checksum:
                raise ValueError('Run artifact hash mismatch')
            checked_files += 1
        manifest = read(folder/'manifest.json')
        if manifest['files'] != {k:v for k,v in job['files'].items() if k!='manifest.json'}:
            raise ValueError('Manifest file list mismatch')
        if any(manifest[k] != job[k] for k in ('results', 'trace', 'protocol_hash', 'case_id', 'repetition')):
            raise ValueError('Workspace differs from saved manifest')
        public = read(folder/'public-input.json')
        if public['question'] != case_map[job['case_id']]['question']:
            raise ValueError('Question mismatch')
        if len(job['results']) != 4 or {r['method'] for r in job['results']} != set(METHODS):
            raise ValueError('Four planned conditions must be retained')
        for result in job['results']:
            rid = result['response_id']
            if rid in response_ids:
                raise ValueError('Duplicate response')
            response_ids.add(rid)
            answer = result.get('answer')
            if answer is not None and digest(answer) != result['answer_sha256']:
                raise ValueError('Answer digest mismatch')
            records.append({'response_id': rid, 'case_id': job['case_id'], 'repetition': job['repetition'],
                            'method': result['method'], 'answer': answer, 'answer_sha256': result.get('answer_sha256'),
                            'delivery_status': result['status'], 'opened_ids': result.get('opened_ids', []),
                            'gate_issues': result.get('output_gate', {}).get('issues', [])})
        jobs.append(job)
    job_ids = {j['id'] for j in jobs}
    requests = [r for r in state.get('api_requests', []) if r['task_id'] in job_ids]
    ids = [r['id'] for r in requests]
    traces = [t['request_id'] for j in jobs for t in j['trace']]
    if len(set(ids)) != len(ids) or Counter(ids) != Counter(traces):
        raise ValueError('Request ledger/trace mismatch')
    for request in requests:
        saved = research/'fulltext-runs'/request['task_id']/(request['id']+'-output.json')
        if request['status']=='completed' and read(saved)['usage'] != request:
            raise ValueError('Request usage differs from hash-verified output')
    methods = [{'method': m, 'planned': len(expected), **dict(Counter(
        r['delivery_status'] for r in records if r['method'] == m))} for m in METHODS]
    totals = {'runs': len(jobs), 'planned_runs': len(expected), 'outputs': len(records),
              'planned_outputs': len(expected)*4, 'requests': len(requests), 'verified_artifacts': checked_files,
              'input_tokens': sum(r.get('input_tokens', 0) for r in requests),
              'output_tokens': sum(r.get('output_tokens', 0) for r in requests),
              'estimated_cny': sum(r.get('estimated_units', 0) for r in requests)/1e6,
              'retained_reservation_cny': sum(r['held_units'] for r in requests)/1e6,
              'workspace_requests': len(state.get('api_requests', [])),
              'workspace_reservation_cny': sum(r['held_units'] for r in state.get('api_requests', []))/1e6,
              'request_status': dict(Counter(r['status'] for r in requests)),
              'output_status': dict(Counter(r['delivery_status'] for r in records)),
              'method_execution': methods, 'semantic_effect': None}
    return {'protocol_hash': protocol, 'totals': totals, 'records': records, 'cases': cases,
            'documents': corpus['documents'], 'plan': plan, 'preflight': checks}


def run(research, out=None, review_a=None, review_b=None, adjudications=None):
    research = Path(research).resolve()
    out = Path(out).resolve() if out else research/'pipeline/current'
    review_folder = research/'evaluations/formal-final'
    review_a = review_a or (review_folder/'reviewer-A.json' if (review_folder/'reviewer-A.json').is_file() else None)
    review_b = review_b or (review_folder/'reviewer-B.json' if (review_folder/'reviewer-B.json').is_file() else None)
    adjudications = adjudications or (review_folder/'adjudications.json' if (review_folder/'adjudications.json').is_file() else None)
    result = audit(research)
    records = result['records']
    receipt = {label: len(read(path).get('answers', [])) if path else 0
               for label,path in [('A',review_a),('B',review_b)]}
    review = reconcile(records, read(review_a) if review_a else None,
                       read(review_b) if review_b else None, read(adjudications) if adjudications else None)
    stats = statistics_report(records, result['cases'], review, result['plan']['repetitions'])
    resources = result['totals']
    basis = '用户统一确认（独立性未核实）' if review.get('basis') == 'user_accepted_scoring' else '双人一致或裁定'
    provenance = {'workspace_sha256': sha(research/'workspace.json'),
                  'freeze_sha256': sha(research/FOLDER/'freeze.json'),
                  'analysis_code_sha256': sha(__file__),
                  'scoring_code_sha256': sha(Path(__file__).with_name('review_scoring.py')),
                  'review_files': {str(p): sha(p) for p in [review_a, review_b, adjudications] if p}}
    stages = [
        {'stage': '文献与来源', 'status': 'verified', 'detail': '固定PDF/文本/片段指纹核验；非自动全文精读'},
        {'stage': '研究方案', 'status': 'frozen', 'detail': '读取冻结24题、8来源、2重复、4条件'},
        {'stage': '实验', 'status': 'verified' if resources['runs']==resources['planned_runs'] else 'incomplete',
         'detail': f"复用{resources['requests']}次真实请求；未重跑"},
        {'stage': '评分与统计', 'status': stats['status'], 'detail': f"已接收A {receipt['A']}、B {receipt['B']}条；{basis}{review['resolved']}/{resources['planned_outputs']}"},
        {'stage': '写作材料', 'status': 'draft_ready', 'detail': '已生成真实资源与评分统计；须连同评分来源限制解释' if stats['status']=='scored' else '已生成真实资源表；效果结论等待评分'},
        {'stage': '正式提交', 'status': 'blocked', 'detail': '尚需最终论文、对应Reviewer Token及提交核验'}]
    report = {'version': '0.7.0', 'created_at': datetime.now(timezone.utc).isoformat(),
              'mode': 'offline_verified_handoffs', 'new_api_calls': 0, 'stages': stages,
              'resources': resources, 'statistics': stats,
              'review': {k: v for k, v in review.items() if k != 'ratings'}, 'review_received': receipt, 'provenance': provenance,
              'protocol_hash': result['protocol_hash'], 'submission_ready': False,
              'limitation': 'Deterministic stage orchestration over a bounded QA experiment, not validated autonomous end-to-end research.'}
    put(out/'report.json', report)
    put(out/'review-resolution.private.json', review)
    put(out/'statistics.json', stats)
    put(out/'literature.json', result['documents'])
    lines = ['# 科研流程衔接报告', '', '本次调用0次API；保留冻结实验，不改题、不重跑。', '']
    lines += [f"- {s['stage']}：{s['status']}；{s['detail']}" for s in stages]
    lines += ['', '## 研究问题', '', '在固定压缩记忆和候选检索下，目录短摘录与输出证据契约是否改善论文问答的有依据任务成功率？',
              '', '主比较A11−A00；两次重复先按题平均，再按8论文组描述不确定性。未收到评分时保持null，不补0。',
              '', '## 实际执行', '', json.dumps(resources, ensure_ascii=False, indent=2),
              '', '## 评分确认来源', '', review.get('limitation', 'Documented dual-review path.'), '', '## 当前边界', '', report['limitation']]
    put(out/'report.md', '\n'.join(lines))
    put(out/'resource_report.md', '# 资源报告\n\n'+
        '\n'.join(f'- {k}: {v}' for k,v in resources.items())+
        '\n\n输入/输出用量来自逐次请求日志；费用为记录单价下估算，非供应商账单。共享前处理不重复计入四条件。'
        '\n初始化另1次10Token未并入工作台统计。冻结资源和原始运行保留在research/fulltext-runs。\n')
    table = ['\\begin{tabular}{lrrr}', '\\hline', 'Condition & Planned & Completed & Rejected \\\\', '\\hline']
    table += [f"{r['method']} & {r['planned']} & {r.get('completed',0)} & {r.get('rejected',0)} \\\\" for r in resources['method_execution']]
    table += ['\\hline', '\\end{tabular}']
    put(out/'execution-table.tex', '\n'.join(table)+'\n')
    put(out/'writing-materials.en.md', '# Verified writing inputs\n\n'+
        f"The frozen study contains {len(result['cases'])} distinct questions and {len(result['documents'])} paper sources. "
        f"There are {resources['outputs']} saved condition outputs and {resources['requests']} real requests. "
        f"Usage is {resources['input_tokens']} input and {resources['output_tokens']} output tokens. "
        f"The estimated batch cost is CNY {resources['estimated_cny']:.6f}, not a verified bill.\n\n"
        'Quality findings must be derived from statistics.json only after status=scored. '
        'Gate rejection is not semantic scoring; ungated outputs were not checked by the same gate. '
        'Public-paper memorization, lexical retrieval, small source count and partial masking limit interpretation.\n')
    put(out/'manifest.json', {p.name: sha(p) for p in out.iterdir() if p.is_file() and p.name!='manifest.json'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--review-a', type=Path)
    parser.add_argument('--review-b', type=Path)
    parser.add_argument('--adjudications', type=Path)
    args = parser.parse_args()
    report = run(args.research, args.out, args.review_a, args.review_b, args.adjudications)
    print(json.dumps({'stages': report['stages'], 'new_api_calls': 0}, ensure_ascii=False))
