"""Deterministic acceptance owned by this benchmark, independent of an LLM."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

MAX_BYTES = 2 * 1024 * 1024


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def same(left: object, right: object) -> bool:
    return canonical(left) == canonical(right)


def _unique(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('file escapes workspace: ' + relative)
    if not path.is_file():
        raise ValueError('missing file: ' + relative)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('file too large: ' + relative)
    return path


def read_json(root: Path, relative: str) -> object:
    return json.loads(safe_file(root, relative).read_text(encoding='utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON: ' + value)))


def _child(code: str, folder: Path, argument: object = None) -> tuple[bool, str]:
    # Deliberately omit model credentials and user environment from candidate code.
    env = {'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8', 'PYTHONNOUSERSITE': '1'}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(folder), canonical(argument)],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=5)
        return result.returncode == 0, (result.stdout + '\n' + result.stderr)[-5000:]
    except subprocess.TimeoutExpired:
        return False, 'candidate check exceeded 5 seconds'


def _module_values(folder: Path, module: str, function: str, values: list) -> tuple[bool, object]:
    code = '''import importlib, json, sys
sys.path.insert(0, sys.argv[1])
a=json.loads(sys.argv[2]); module=importlib.import_module(a['module']); fn=getattr(module,a['function'])
result=[fn(*v) for v in a['values']]
print('__HB_RESULT__='+json.dumps(result,ensure_ascii=False,allow_nan=False))
'''
    ok, text = _child(code, folder, {'module': module, 'function': function, 'values': values})
    for line in reversed(text.splitlines()):
        if line.startswith('__HB_RESULT__='):
            try:
                return ok, json.loads(line[len('__HB_RESULT__='):])
            except ValueError:
                break
    return False, text[-1500:]


def _checker(folder: Path) -> tuple[bool, str]:
    return _child("import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[1]+'/checker.py',run_name='__main__')", folder)


def interface_executed(events: list, script: str, marker: str) -> bool:
    for event in events:
        if event.get('tool_name') not in {'bash', 'powershell'}:
            continue
        args = event.get('call_args', {})
        if not isinstance(args, dict):
            continue
        try:
            lexer = shlex.shlex(str(args.get('command', '')), posix=os.name != 'nt', punctuation_chars=';&|\n')
            lexer.whitespace = ' \t\r'
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        found = False
        for index, token in enumerate(tokens):
            name = token.strip('\"\'').replace('\\', '/').split('/')[-1]
            if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', name) is None:
                continue
            if index and not all(char in ';&|\n' for char in tokens[index - 1]):
                continue
            following = index + 1
            while following < len(tokens) and tokens[following].startswith('-'):
                option = tokens[following]
                if option in {'-c', '-m'}:
                    break
                following += 2 if option in {'-W', '-X'} else 1
            if following < len(tokens) and tokens[following].strip('\"\'').replace('\\', '/').split('/')[-1] == script:
                found = True
        result = event.get('call_result', {})
        success = result.get('success') is True if isinstance(result, dict) else 'success=True' in str(result)
        if found and success and marker in str(result).replace('\\', ''):
            return True
    return False


class Acceptance:
    def __init__(self, suite_root: Path) -> None:
        self.suite_root = suite_root.resolve()

    def validate_case(self, case: dict) -> None:
        from build_cases import VERSION, FAMILIES, protocol_digest
        b = case.get('benchmark', {})
        if b.get('version') != VERSION or b.get('family') not in FAMILIES:
            raise ValueError('unsupported benchmark protocol')
        if b.get('grader_sha256') != protocol_digest():
            raise ValueError('benchmark implementation changed; rebuild datasets and start a new evaluation')
        if not (self.suite_root / b['public_root']).resolve().is_relative_to(self.suite_root):
            raise ValueError('invalid fixture root')
        if set(case.get('assets', [])) != set(b.get('asset_sha256', {})):
            raise ValueError('asset inventory/hash mismatch')
        for rel, digest in b['asset_sha256'].items():
            if hashlib.sha256(safe_file(self.suite_root, rel).read_bytes()).hexdigest() != digest:
                raise ValueError('source fixture has changed: ' + rel)

    def grade(self, case: dict, workspace: Path, tool_events: list | None = None) -> dict:
        self.validate_case(case)
        workspace = workspace.resolve()
        b = case['benchmark']; family = b['family']; rel = b['public_root']
        pristine = self.suite_root / rel
        live = workspace / rel
        items = []

        def check(name: str, passed: bool, evidence: str) -> None:
            items.append({'requirement_id': name, 'group': 'requirement', 'passed': bool(passed),
                          'score': float(bool(passed)), 'evidence': evidence[:2500],
                          'source': 'harness_baseline_v1.acceptance'})

        try:
            report = read_json(workspace, 'deliverables/report.json')
        except (ValueError, OSError) as exc:
            report = None
            report_error = str(exc)
        else:
            report_error = ''
        try:
            if family == 'delivery':
                rows = list(csv.DictReader((pristine / 'orders.csv').open(encoding='utf-8')))
                total = sum(int(row['unit_minor']) * int(row['quantity']) for row in rows)
                expected = {'currency': 'CNY', 'order_count': len(rows), 'total_minor': total, 'status': 'complete'}
                check('totals', isinstance(report, dict) and same([report.get('order_count'), report.get('total_minor')], [len(rows), total]),
                      f'deliverables/report.json: expected count={len(rows)}, total_minor={total}; {report_error}')
                check('report_schema', same(report, expected), f'deliverables/report.json: exact published schema; actual={report!r}')
                try:
                    text = safe_file(workspace, 'deliverables/summary.md').read_text(encoding='utf-8')
                    blocks = re.split(r'(?m)^#{1,6}\s+(R[123])\b[^\n]*\n', text)
                    sections = {blocks[i]: blocks[i + 1].strip() for i in range(1, len(blocks) - 1, 2)}
                    good = all(bool(sections.get(k, '').strip()) for k in ['R1', 'R2', 'R3'])
                    good = good and re.search(r'(?m)^order_count:\s*' + str(len(rows)) + r'\s*$', sections.get('R1', '')) is not None
                    good = good and re.search(r'(?m)^total_minor:\s*' + str(total) + r'\s*$', sections.get('R1', '')) is not None
                    good = good and re.search(r'(?m)^method:\s*integer_sum\s*$', sections.get('R2', '')) is not None
                    good = good and re.search(r'(?m)^verification:\s*(derived|executed)\s*$', sections.get('R2', '')) is not None
                    good = good and re.search(r'(?m)^unresolved:\s*none\s*$', sections.get('R3', '')) is not None
                    check('written_delivery', bool(good), 'summary.md: published R1 numeric fields, R2 method/status, R3 unresolved field')
                except (ValueError, OSError) as exc:
                    check('written_delivery', False, str(exc))
            elif family == 'pipeline':
                values = json.loads((pristine / 'values.json').read_text())
                cfg = read_json(workspace, rel + '/config.json')
                expected_cfg = {'scale': values['required_scale'], 'note': 'keep-this-note'}
                check('config', same(cfg, expected_cfg), f'{rel}/config.json: expected={expected_cfg}, actual={cfg}')
                expected = {'job_id': values['job_id'], 'total_minor': sum(values['amounts_minor']) * values['required_scale'],
                            'source_sha256': hashlib.sha256((pristine / 'values.json').read_bytes()).hexdigest()}
                try:
                    result = read_json(workspace, rel + '/out/result.json')
                    check('fresh_producer_output', same(result, expected), f'{rel}/out/result.json: expected={expected}, actual={result}')
                except (ValueError, OSError) as exc:
                    check('fresh_producer_output', False, str(exc))
                check('interface_executed', interface_executed(tool_events or [], 'producer.py', 'COMPLETE: out/result.json was regenerated'),
                      'native tool trace must contain successful direct Python producer.py invocation and completion output')
                check('handoff', same(report, {'job_id': values['job_id'], 'total_minor': expected['total_minor'], 'producer_status': 'complete'}),
                      f'deliverables/report.json: actual={report}; {report_error}')
            elif family == 'oracle':
                ok, values = _module_values(live, 'pricing', 'discounted', [[199, 2500], [101, 5000], [17, 3333], [499, 1], [300, 10000]])
                check('implementation_preserved', ok and same(values, [149, 50, 11, 498, 0]), f'pricing.discounted independent integer probes: {values}')
                positive, detail = _checker(live)
                check('checker_accepts_correct', positive, 'checker.py against unchanged correct pricing.py: ' + detail)
                negatives = []
                for body in ['return 0', 'return amount_minor']:
                    with tempfile.TemporaryDirectory(prefix='hb1-negative-') as td:
                        temp = Path(td)
                        shutil.copy2(safe_file(workspace, rel + '/checker.py'), temp / 'checker.py')
                        (temp / 'pricing.py').write_text('def discounted(amount_minor, discount_bp):\n    ' + body + '\n')
                        accepted, output = _checker(temp)
                        negatives.append(not accepted and 'exceeded' not in output)
                check('checker_rejects_wrong', all(negatives), f'checker.py rejects both all-zero and undiscounted mutants: {negatives}')
                samples = [amount * (10000 - rate) // 10000 for amount, rate in b['extra']['sample_inputs']]
                check('reported_values', same(report, {'status': 'verified', 'samples': samples}), f'deliverables/report.json: actual={report}; expected samples={samples}; {report_error}')
            elif family == 'regression':
                values = b['extra']['probe_values']; name = b['extra']['function']
                ok, result = _module_values(live, 'labels', name, [[v] for v in values] + [['  MiXeD  '], ['İ']])
                expected = ['' if v is None else v.strip().casefold() for v in values] + ['mixed', 'i\u0307']
                check('requested_behavior', ok and same(result, expected), f'labels.{name}: expected={expected}, actual={result}')
                ok, result = _module_values(live, 'labels', 'display_label', [[' A '], [''], ['X']])
                check('regression_behavior', ok and same(result, ['[ A ]', '[]', '[X]']), f'labels.display_label unchanged: {result}')
                expected_report = {'status': 'verified', 'outputs': expected[:len(values)]}
                check('reported_values', same(report, expected_report), f'deliverables/report.json: actual={report}; {report_error}')
            elif family == 'schema':
                expected = json.loads((pristine / 'request.json').read_text())
                cfg = read_json(workspace, rel + '/config.json')
                check('schema', same(cfg, expected), f'config.json: exact request/schema, expected={expected}, actual={cfg}')
                receipt = {'status': 'accepted', 'request_id': expected['request_id'],
                           'config_sha256': hashlib.sha256(canonical(expected).encode()).hexdigest()}
                try:
                    actual = read_json(workspace, rel + '/out/receipt.json')
                    check('receipt', same(actual, receipt), f'out/receipt.json exact accepted request fingerprint: {actual}')
                except (ValueError, OSError) as exc:
                    check('receipt', False, str(exc))
                check('interface_executed', interface_executed(tool_events or [], 'submit.py', receipt['config_sha256']),
                      'native tool trace must contain successful direct Python submit.py invocation and this configuration fingerprint')
                check('handoff', same(report, {'request_id': expected['request_id'], 'status': 'accepted'}),
                      f'deliverables/report.json: actual={report}; {report_error}')
            elif family == 'logs':
                events = [json.loads(line) for line in (pristine / 'events.jsonl').read_text().splitlines()]
                events = [e for e in events if e['job'] == b['extra']['job'] and e['run'] == b['extra']['run']]
                root_event = next((e for e in events if e.get('code') == 'SCHEMA_MISMATCH'), None)
                expected = {'job': b['extra']['job'], 'run': b['extra']['run'], 'status': 'failed' if root_event else 'success',
                            'cause': root_event['code'] if root_event else None, 'root_event_id': root_event['event_id'] if root_event else None,
                            'expected_version': root_event['expected_version'] if root_event else None,
                            'actual_version': root_event['actual_version'] if root_event else None}
                fields = [('run_identity', ['job', 'run', 'status']), ('root_cause', ['cause', 'root_event_id']),
                          ('version_evidence', ['expected_version', 'actual_version'])]
                for key, names in fields:
                    check(key, isinstance(report, dict) and same({k: report.get(k) for k in names}, {k: expected[k] for k in names}) and set(report) == set(expected),
                          f'deliverables/report.json {names}: expected={ {k:expected[k] for k in names} }; actual={report}; {report_error}')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # Missing/invalid candidate files are task failures, not fabricated grades.
            check('candidate_files_readable', False, str(exc))

        protected_errors = []
        for name, digest in b['asset_sha256'].items():
            if name in b['mutable_assets']:
                continue
            try:
                actual = hashlib.sha256(safe_file(workspace, name).read_bytes()).hexdigest()
                if actual != digest:
                    protected_errors.append(name + ': changed')
            except (OSError, ValueError) as exc:
                protected_errors.append(str(exc))
        check('protected_inputs', not protected_errors, 'protected source identities: ' + repr(protected_errors))
        published = [r['id'] for r in case['reference']['required_behaviors']]
        present = {row['requirement_id'] for row in items}
        for missing in published:
            if missing not in present:
                check(missing, False, 'not evaluated because an earlier required candidate file was invalid or missing')
        passed = all(row['passed'] for row in items)
        partial = sum(row['passed'] for row in items if row['requirement_id'] in published) / len(published)
        return {'case_id': case['case_id'], 'passed': passed, 'score': float(passed), 'partial_score': partial,
                'items': items, 'protocol': b['version']}
