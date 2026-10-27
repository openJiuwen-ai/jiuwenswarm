"""Attach benchmark-owned deterministic acceptance to the native RSI evaluator."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openjiuwen.rsi.harness_rsi.evaluator.judger.base import EvaluationJudger, JudgeResult
from openjiuwen.rsi.harness_rsi.evaluator.requirement_results import requirement_results_contract
from openjiuwen.rsi.harness_rsi.evaluator.team_evaluator import TeamEvaluator

from grader import Acceptance


class HarnessAcceptanceJudger(EvaluationJudger):
    method = 'harness_baseline_v1'

    def __init__(self, suite_root: Path) -> None:
        self.acceptance = Acceptance(suite_root)

    def validate_case(self, case: dict) -> None:
        self.acceptance.validate_case(case)

    async def judge(self, *, case: dict, execution_result, output_dir: str = '') -> JudgeResult:
        if not execution_result.workspace_dir:
            raise ValueError('native Solver did not provide its case workspace')
        execution_evidence = execution_result.metadata.get('execution_evidence', {})
        events = []
        trace_available = False
        trace = execution_evidence.get('role_trace_path')
        if trace and output_dir:
            path = Path(trace).resolve()
            if not path.is_relative_to(Path(output_dir).resolve()):
                raise ValueError('execution evidence escapes the case directory')
            if path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
                archive = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(archive, dict) and isinstance(archive.get('steps'), list):
                    events = [s['detail'] for s in archive['steps'] if s.get('kind') == 'tool']
                    trace_available = True
        result = await asyncio.to_thread(self.acceptance.grade, case, Path(execution_result.workspace_dir), events)
        if case['benchmark']['family'] in {'pipeline', 'schema'}:
            confirmed = any(i['requirement_id'] == 'interface_executed' and i['passed'] for i in result['items'])
            shell_payload_clipped = any(
                e.get('tool_name') in {'bash', 'powershell'} and '[truncated' in str([e.get('call_args'), e.get('call_result')])
                for e in events
            )
            if not confirmed and (not trace_available or execution_evidence.get('status') != 'complete' or shell_payload_clipped):
                from openjiuwen.rsi.harness_rsi.evaluator.errors import EvaluationInfrastructureError
                raise EvaluationInfrastructureError('required interface execution evidence is incomplete; cannot infer non-execution')
        if output_dir:
            path = Path(output_dir) / 'verifier' / 'acceptance.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        failed = [r['requirement_id'] for r in result['items'] if not r['passed']]
        return JudgeResult(method=self.method, score=result['score'], passed=result['passed'],
                           reason='all published checks passed' if not failed else 'failed checks: ' + ', '.join(failed),
                           metadata={'requirement_results': requirement_results_contract(result['items']),
                                     'optimization_signals': {'schema_version': 1, 'continuous_score': {
                                         'availability': 'available', 'value': result['partial_score'],
                                         'source': self.method + '.partial_score'}, 'dimensions': {},
                                         'promotion_authority': 'eval_ref_case_score'},
                                     'acceptance_protocol': result['protocol']})


class HarnessAcceptanceEvaluator(TeamEvaluator):
    def _make_case_runner(self):
        runner = super()._make_case_runner()
        runner.judger = HarnessAcceptanceJudger(Path(__file__).resolve().parent)
        return runner
