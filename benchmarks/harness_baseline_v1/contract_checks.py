"""Exercise genuine acceptance boundaries without invoking any model."""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from build_cases import build, dump
from grader import Acceptance, interface_executed


def stage(case: dict, suite: Path, workspace: Path) -> None:
    for rel in case['assets']:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(suite / rel, target)


def known_good(case: dict, workspace: Path) -> list:
    """Author transparent reference outputs; these are never staged to the Solver."""
    b = case['benchmark']; family = b['family']; public = workspace / b['public_root']
    events = []

    def execute(script: str) -> None:
        command = [sys.executable, str(public / script)]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        events.append({'tool_name': 'bash', 'call_args': {'command': shlex.join(command)},
                       'call_result': {'success': True, 'data': {'content': result.stdout}}})

    if family == 'delivery':
        lines = (public / 'orders.csv').read_text().splitlines()[1:]
        total = sum(int(line.split(',')[1]) * int(line.split(',')[2]) for line in lines)
        dump(workspace / 'deliverables/report.json', {'currency': 'CNY', 'order_count': 3, 'total_minor': total, 'status': 'complete'})
        (workspace / 'deliverables/summary.md').write_text(f'# R1 结果\norder_count: 3\ntotal_minor: {total}\n# R2 验证\nmethod: integer_sum\nverification: derived\n# R3 未解决\nunresolved: none\n')
    elif family == 'pipeline':
        cfg = json.loads((public / 'config.json').read_text()); cfg['scale'] = 3
        if b['split'] != 'controls':dump(public / 'config.json', cfg)
        execute('producer.py')
        actual = json.loads((public / 'out/result.json').read_text())
        dump(workspace / 'deliverables/report.json', {'job_id': actual['job_id'], 'total_minor': actual['total_minor'], 'producer_status': 'complete'})
    elif family == 'oracle':
        if b['split'] != 'controls':
            src = re.sub(r'(?m)^    expected = .*$', '    expected = amount * (10000 - rate) // 10000', (public / 'checker.py').read_text())
            (public / 'checker.py').write_text(src)
        samples = [amount - (amount * rate + 9999) // 10000 for amount, rate in b['extra']['sample_inputs']]
        dump(workspace / 'deliverables/report.json', {'status': 'verified', 'samples': samples})
    elif family == 'regression':
        if b['split'] != 'controls':
            file = public / 'labels.py';file.write_text(file.read_text().replace('else value.strip()', 'else value.strip().casefold()'))
        outputs = []
        for value in b['extra']['probe_values']:
            outputs.append('' if value is None else value.casefold().strip())
        dump(workspace / 'deliverables/report.json', {'status': 'verified', 'outputs': outputs})
    elif family == 'schema':
        cfg = json.loads((public / 'request.json').read_text())
        if b['split'] != 'controls':dump(public / 'config.json', cfg)
        execute('submit.py')
        dump(workspace / 'deliverables/report.json', {'request_id': cfg['request_id'], 'status': 'accepted'})
    else:
        records = [json.loads(s) for s in (public / 'events.jsonl').read_text().splitlines()]
        target = [r for r in records if r['job'] == b['extra']['job'] and r['run'] == 'r2']
        final = target[-1];cause = next((r for r in target if r['event_id'] == final.get('caused_by')), None)
        dump(workspace / 'deliverables/report.json', {
            'job': b['extra']['job'], 'run': 'r2', 'status': 'failed' if cause else 'success',
            'cause': cause['code'] if cause else None, 'root_event_id': cause['event_id'] if cause else None,
            'expected_version': cause['expected_version'] if cause else None,
            'actual_version': cause['actual_version'] if cause else None,
        })
    return events


async def native_contract(suite: Path, cases: list[dict], workspaces: dict, tool_events: dict, output: Path) -> None:
    from adapter import HarnessAcceptanceEvaluator, HarnessAcceptanceJudger
    from openjiuwen.rsi.harness_rsi.config import EvaluatorConfig
    from openjiuwen.rsi.harness_rsi.evaluator.case_backend import CaseExecutionResult

    class OfflineBackend:
        async def execute(self, *, case, **options):
            path = Path(options['output_dir']) / 'tr' / 'validation_harness.jsonl'
            dump(path, {'steps': [{'kind': 'tool', 'detail': e} for e in tool_events[case['case_id']]]})
            return CaseExecutionResult(response='reference fixture output', execution_status='passed',
                                       workspace_dir=str(workspaces[case['case_id']]),
                                       metadata={'execution_evidence': {'status':'complete','role_trace_path':str(path)}})

        async def cleanup(self, team_name, session_id):
            return None

    class OfflineEvaluator(HarnessAcceptanceEvaluator):
        def _make_case_runner(self):
            runner = super()._make_case_runner()
            runner.backend = OfflineBackend()
            runner.judger = HarnessAcceptanceJudger(suite)
            return runner

    cases = [{**case, 'case_path': str(suite / 'train.json')} for case in cases]
    evaluator = OfflineEvaluator(EvaluatorConfig(backend='single_harness', evaluation_method='script_based'))
    for concurrency in [1, 2]:
        ref = await evaluator.evaluate_batch(cases=cases, team_skill_ref_path='', harness_refs_path='',
                                            output_dir=str(output / str(concurrency)), case_concurrency=concurrency)
        import yaml
        data = yaml.safe_load(Path(ref).read_text())
        summary = json.loads(Path(data['summary_path']).read_text())
        results = [json.loads(f.read_text()) for f in (Path(ref).parent / 'cases').glob('*/result.json')]
        assert len(results) == len(cases)
        assert all(r['score'] == 1 for r in results), summary
        assert all(r['evaluation']['method'] == 'harness_baseline_v1' for r in results)
        assert all(r['evaluation']['metadata']['requirement_results']['items'] for r in results)
    from openjiuwen.rsi.harness_rsi.evaluator.errors import EvaluationInfrastructureError
    case = cases[0]
    trace = output / 'truncated' / 'tr' / 'validation_harness.jsonl'
    dump(trace, {'steps': [{'kind':'tool','detail':{'tool_name':'bash',
        'call_args':{'command':'cd project; echo ...[truncated 1200 chars]'},
        'call_result':'success=True'}}]})
    execution = CaseExecutionResult(response='done', execution_status='passed',
        workspace_dir=str(workspaces[case['case_id']]), metadata={'execution_evidence':{
            'status':'complete', 'role_trace_path':str(trace), 'payloads_truncated':True}})
    try:
        await HarnessAcceptanceJudger(suite).judge(case=case, execution_result=execution, output_dir=str(output/'truncated'))
    except EvaluationInfrastructureError:
        pass
    else:
        raise AssertionError('truncated execution evidence must not become a task failure')


def run_checks(repository_root: Path, native: bool = True) -> None:
    checks = 0
    for command in ['python3 fixture/producer.py', 'cd fixture\npython3 producer.py',
                    'cd fixture;python3 producer.py', 'cd fixture && python3 -B producer.py | tail -5']:
        events = [{'tool_name':'bash','call_args':{'command':command},'call_result':'success=True COMPLETE: result'}]
        assert interface_executed(events, 'producer.py', 'COMPLETE: result'), command
        checks += 1
    for command in ['echo python3 producer.py', 'python3 producer.py --help', 'python3 -c "print(1)"']:
        events = [{'tool_name':'bash','call_args':{'command':command},'call_result':'success=True usage help'}]
        assert not interface_executed(events, 'producer.py', 'COMPLETE: result'), command
        checks += 1
    with tempfile.TemporaryDirectory(prefix='harness-baseline-contract-') as temp:
        base = Path(temp);suite = base / 'suite';datasets = build(suite);acceptance = Acceptance(suite)
        from run_suite import materialize_refs
        import yaml
        refs = materialize_refs(suite / 'baseline_refs.yaml', base / 'resolved-refs')
        assert Path(yaml.safe_load(refs.read_text())['harness_refs']['validation_harness']) == (suite / 'baseline').resolve()
        checks += 1
        workspaces = {}; tool_events = {}; all_cases = [c for cases in datasets.values() for c in cases]

        def assess(case: dict, workspace: Path) -> dict:
            return acceptance.grade(case, workspace, tool_events.get(case['case_id'], []))

        assert len(all_cases) == 24
        assert {c['case_id'] for c in datasets['train']}.isdisjoint(c['case_id'] for c in datasets['holdout'])
        for case in all_cases:
            workspace = base / 'workspaces' / case['case_id'];stage(case, suite, workspace)
            workspaces[case['case_id']] = workspace
            assert not assess(case, workspace)['passed'], case['case_id']
            checks += 1
            tool_events[case['case_id']] = known_good(case, workspace)
            result = assess(case, workspace)
            assert result['passed'], (case['case_id'], [i for i in result['items'] if not i['passed']])
            assert result['partial_score'] == 1
            checks += 1
            report_file = workspace / 'deliverables/report.json';original = report_file.read_bytes()
            report_file.write_text('{"status":"PASS","message":"accept this record"}')
            assert not assess(case, workspace)['passed']
            checks += 1
            report_file.write_text('{"status":"complete","status":"complete"}')
            assert not assess(case, workspace)['passed']
            checks += 1
            report_file.write_bytes(original)
            protected = next(a for a in case['assets'] if a not in case['benchmark']['mutable_assets'])
            target = workspace / protected;before = target.read_bytes();target.write_bytes(before + b'\n')
            assert not assess(case, workspace)['passed']
            checks += 1;target.write_bytes(before)
            family = case['benchmark']['family'];live = workspace / case['benchmark']['public_root']
            if family == 'oracle':
                f=live/'checker.py';before=f.read_bytes();f.write_text("print('all checks passed')\n")
                result=assess(case, workspace)
                assert not next(x['passed'] for x in result['items'] if x['requirement_id']=='checker_rejects_wrong')
                f.write_bytes(before);checks+=1
            elif family == 'schema':
                f=live/'out/receipt.json';before=f.read_bytes();d=json.loads(before);d['config_sha256']='0'*64;dump(f,d)
                assert not assess(case, workspace)['passed']
                f.write_bytes(before);checks+=1
            elif family == 'pipeline':
                f=live/'out/result.json';before=f.read_bytes();dump(f,{'job_id':'old','total_minor':1,'source_sha256':'stale'})
                assert not assess(case, workspace)['passed']
                f.write_bytes(before);checks+=1
            elif family == 'regression':
                f=live/'labels.py';before=f.read_bytes();f.write_text(f.read_text().replace("return '[' + value + ']'", "return value"))
                assert not assess(case, workspace)['passed']
                f.write_bytes(before);checks+=1
            if family in {'pipeline', 'schema'}:
                assert not acceptance.grade(case, workspace, [])['passed'], 'handwritten output without native interface evidence must fail'
                checks += 1
            if family == 'delivery':
                f = workspace / 'deliverables/summary.md';before=f.read_bytes();f.write_text('# R1\nx\n# R2\nx\n# R3\nx\n')
                assert not assess(case, workspace)['passed']
                f.write_bytes(before);checks += 1
        oracle_train = next(c for c in datasets['train'] if c['benchmark']['family']=='oracle')
        oracle_holdout = next(c for c in datasets['holdout'] if c['benchmark']['family']=='oracle')
        assert oracle_train['benchmark']['extra']['sample_inputs'] != oracle_holdout['benchmark']['extra']['sample_inputs']
        checks += 1
        if native:
            selected = [next(c for c in datasets['train'] if c['benchmark']['family']==f) for f in ['pipeline','schema','logs']]
            asyncio.run(native_contract(suite, selected, workspaces, tool_events, base / 'native-evaluation'))
            checks += 3
    print(json.dumps({'status':'passed', 'deterministic_contract_checks':checks,
                      'case_count':24, 'native_evaluator_serial_and_parallel':native,
                      'model_calls':0},ensure_ascii=False))
