"""同技能批次的只读计划校验、串行执行及原段断点恢复。"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.server.runtime.skill_turbo.accelerator_registry import AcceleratorRegistry
from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager, SkillStateError
from jiuwenswarm.server.runtime.skill_turbo.segment_context import data_fingerprint, execution_snapshot
from jiuwenswarm.server.runtime.skill_turbo.segment_execution import resolve_execution_skill
from jiuwenswarm.server.runtime.skill_turbo.segment_inputs import SegmentInputError, validate_batch_inputs, _CONTEXT_KEYS
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import LedgerBusy, LedgerError, SkillRunLedger, skill_execution_lease
from jiuwenswarm.server.runtime.skill_turbo.executor import PlanCodeLoadError
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidationError


@dataclass(frozen=True)
class BatchStep:
    spec: AcceleratorSpec
    params: dict
    force: bool


@dataclass(frozen=True)
class BatchPlan:
    steps: tuple[BatchStep, ...]
    skill_root: str
    snapshot: dict
    warnings: tuple[dict, ...]
    p50_seconds: float


async def build_batch_plan(env, *, skill, accelerators, params, inputs, ledger=None):
    config = env.config.get('skill_turbo', {'enabled': env.config.get('enabled', True)})
    if config.get('enabled', False) is not True or config.get('batch_enabled', True) is not True:
        raise SegmentInputError('blocked', 'batch_disabled')
    maximum = config.get('max_batch_steps', 8)
    if type(maximum) is not int or maximum < 1:
        raise SegmentInputError('invalid_args', 'invalid_batch_limit')
    if not skill or not isinstance(accelerators, list) or not 1 <= len(accelerators) <= min(maximum, 8):
        raise SegmentInputError('invalid_args', 'batch_requires_one_to_eight_steps')
    if params is not None and not isinstance(params, dict):
        raise SegmentInputError('invalid_args', 'params_must_be_object')
    record, root = await asyncio.to_thread(resolve_execution_skill, env, skill)
    if not record.manifest_valid:
        raise SegmentInputError('invalid_args', 'invalid_manifest')
    registry = AcceleratorRegistry(env)
    steps = []
    seen = set()
    for raw in accelerators:
        if not isinstance(raw, dict) or set(raw) - {'id', 'params', 'force'} or not isinstance(raw.get('id'), str):
            raise SegmentInputError('invalid_args', 'invalid_batch_step')
        ident = raw['id']
        spec = registry.get(skill, ident)
        if ident == '__full__' or spec is None or ident in seen or (steps and spec.order <= steps[-1].spec.order):
            raise SegmentInputError('invalid_args', 'unknown_duplicate_or_out_of_order_step', [
                {'code': 'canonical_order', 'ids': [item.id for item in registry.list_for(skill)]}])
        if not spec.batch.standalone and len(accelerators) != 1:
            raise SegmentInputError('invalid_args', 'exclusive_step_requires_single_item_batch')
        if type(raw.get('force', False)) is not bool or not isinstance(raw.get('params', {}), dict):
            raise SegmentInputError('invalid_args', 'invalid_step_params_or_force')
        merged = copy.deepcopy(params or {})
        merged.update({key: copy.deepcopy(value) for key, value in raw.get('params', {}).items() if value is not None})
        # 共享未知键即非法，即使step覆盖或exclusive投影也不能将其隐藏。
        properties = spec.input_schema.get('properties', {})
        if set(raw.get('params', {})) - set(properties) or _CONTEXT_KEYS.intersection(raw.get('params', {})):
            raise SegmentInputError('invalid_args', 'unknown_or_reserved_step_params')
        if set(params or {}) - set(properties) or _CONTEXT_KEYS.intersection(params or {}):
            raise SegmentInputError('invalid_args', 'unknown_shared_params')
        steps.append(BatchStep(spec, merged, raw.get('force', False)))
        seen.add(ident)
    try:
        await asyncio.to_thread(data_fingerprint, {'steps': accelerators, 'params': params})
    except (TypeError, ValueError) as exc:
        raise SegmentInputError('invalid_args', 'batch_parameters_not_json') from exc
    workspace = inputs.get('metadata', {}).get('output_dir', '')
    if not workspace or not Path(workspace).is_absolute():
        raise SegmentInputError('blocked', 'workspace_missing')
    workspace = str(await asyncio.to_thread(Path(workspace).resolve))
    data = ledger.data if ledger is not None else {
        'workspace_base': workspace, 'run_dir': str(Path(workspace) / 'skill_runs' / skill / '_pending_'),
        'facts': {}, 'facts_provenance': {}, 'artifacts': []}
    ctx = {'metadata': inputs.get('metadata', {}), 'skill_root': root, 'capabilities': list(env.tools)}
    future = {}
    artifacts = []
    warnings = []
    for step in steps:
        warnings.extend(await validate_batch_inputs(ctx, step.spec, step.params, data, future, artifacts, params))
        future.update({key: step.spec.id for key in step.spec.outputs.facts})
        artifacts.extend(step.spec.outputs.artifacts)
    p50 = sum(step.spec.cost.p50_seconds for step in steps)
    if p50 > config.get('max_batch_p50_seconds', 600):
        warnings.append({'code': 'batch_cost_budget_exceeded', 'p50_seconds': p50})
    snapshot = await asyncio.to_thread(execution_snapshot, root)
    if not await asyncio.to_thread(env._validate_skill_code_dir, record.name, Path(record.turbo_codes_dir) / record.name,
                                   validator=env._validator_for_external_skill(record.name)):
        raise PlanCodeValidationError(['current_skill_source_invalid'])
    return BatchPlan(tuple(steps), root, snapshot, tuple(warnings), p50)


def _result(skill, status='failed', **values):
    return {'mode': 'batch', 'skill': skill, 'success': status in {'ok', 'partial'}, 'status': status,
            'batch_id': '', 'results': [], 'skipped': [], 'facts': {}, 'facts_provenance': {}, 'artifacts': [],
            'covered_steps': [], 'remaining_steps': [], 'resume_remaining': [], 'warnings': [], 'ledger': {},
            'delivery_confirmed': False, 'stop_reason': '', 'stopped_at': None, 'next_steps': [], 'metrics': {}, **values}


def _aggregate(skill, state, plan, *, status, reason, cursor, stopped=None, ledger=None, elapsed=0):
    results, skipped = state['results'], state['skipped']
    covered = sorted({item for result in results + skipped for item in result.get('covered_steps', [])})
    remaining = [step.spec.id for step in plan.steps[cursor:]]
    later = [step.spec.id for step in plan.steps[cursor + 1:]] if stopped is not None else remaining
    warnings = copy.deepcopy(state['warnings'])
    for result in results + skipped:
        warnings.extend(result.get('warnings', []))
    value = _result(skill, status, batch_id=state['batch_id'], results=copy.deepcopy(results),
        skipped=copy.deepcopy(skipped), warnings=warnings, covered_steps=covered,
        remaining_steps=remaining, resume_remaining=later, stopped_at=stopped, stop_reason=reason,
        ledger=ledger.digest() if ledger else {},
        facts=copy.deepcopy(ledger.data['facts']) if ledger else {},
        facts_provenance=copy.deepcopy(ledger.data['facts_provenance']) if ledger else {},
        artifacts=copy.deepcopy(ledger.data['artifacts']) if ledger else [],
        metrics={'executed': len(results), 'skipped': len(skipped), 'elapsed_seconds': elapsed, 'p50_seconds': plan.p50_seconds})
    if stopped is not None:
        value['next_steps'] = [f'先修复或按需force重跑 {stopped}，再按实际前提调用余段：' + ', '.join(later)]
    elif remaining:
        value['next_steps'] = remaining
    elif cursor:
        last_id = plan.steps[cursor - 1].spec.id
        last = next((result for result in results + skipped if result.get('accelerator') == last_id), {})
        value['next_steps'] = copy.deepcopy(last.get('next_steps', []))
        value['continue_hint'] = last.get('continue_hint', '')
    eligible = [result for result in results if result.get('delivery_confirmed') is True
                and result.get('delivery_evidence', {}).get('new_delivery') is True]
    value['fact_updated_by'] = {key: copy.deepcopy(update) for result in results for key, update in result.get('fact_updated_by', {}).items()}
    value['confirmed_deliveries'] = [{key: copy.deepcopy(result[key]) for key in ('accelerator', 'delivery_owner', 'delivery_evidence') if key in result} for result in eligible]
    if eligible and status == 'ok' and not remaining and reason == 'completed':
        final = eligible[-1]
        for key in ('delivery_confirmed', 'delivery_owner', 'delivery_evidence', 'delivery_summary'):
            if key in final:
                value[key] = copy.deepcopy(final[key])
    return value


def _resume_state(resume, signature):
    control = resume.get('segment')
    if not isinstance(control, dict) or not isinstance(control.get('batch'), dict):
        raise SegmentInputError('failed', 'batch_control_missing')
    state = copy.deepcopy(control['batch'])
    digest = state.pop('fingerprint', None)
    if (not digest or digest != data_fingerprint(state) or type(state.get('version')) is not int
            or state['version'] != 1 or type(state.get('manifest_version')) is not int or state['manifest_version'] != 1):
        raise SegmentInputError('failed', 'batch_control_corrupt')
    if state.get('signature') != signature:
        raise SegmentInputError('failed', 'batch_original_call_changed')
    cursor = state.get('cursor')
    if type(cursor) is not int or not 0 <= cursor < len(signature['steps']):
        raise SegmentInputError('failed', 'batch_cursor_invalid')
    if (not isinstance(state.get('results'), list) or not isinstance(state.get('skipped'), list)
            or not isinstance(state.get('warnings'), list) or type(state.get('partial')) is not bool
            or not isinstance(state.get('ledger'), dict) or not isinstance(state.get('batch_id'), str) or not state['batch_id']
            or type(state.get('ledger_persisted')) is not bool or not isinstance(state.get('ledger_save'), dict)
            or not isinstance(state.get('execution_snapshot'), dict)
            or state['ledger'].get('task_key') != control.get('task_key')
            or state['ledger'].get('workspace_base') != signature['workspace_base']
            or state['ledger'].get('skill') != signature['skill']):
        raise SegmentInputError('failed', 'batch_state_invalid')
    task = state['ledger']
    if (type(task.get('revision')) is not int or task['revision'] < 0 or type(task.get('updated_at')) is not int
            or any(not isinstance(task.get(key), dict) for key in ('facts', 'facts_provenance', 'latest_runs', 'identity'))
            or any(not isinstance(task.get(key), list) for key in ('artifacts', 'runs', 'covered_stages'))
            or any(not isinstance(value, dict) or type(value.get('verified')) is not bool
                   for value in task['facts_provenance'].values())
            or any(not isinstance(value, dict) for value in task['latest_runs'].values())
            or any(not isinstance(value, dict) for value in task['artifacts'] + task['runs'])
            or any(not isinstance(value, dict) or value.get('status') not in {'ok', 'partial'}
                   for value in state['results'] + state['skipped'])):
        raise SegmentInputError('failed', 'batch_ledger_or_results_corrupt')
    prefix = [step['id'] for step in signature['steps'][:cursor]]
    completed = [result.get('accelerator') for result in state['results'] + state['skipped'] if isinstance(result, dict)]
    if len(completed) != cursor or set(completed) != set(prefix):
        raise SegmentInputError('failed', 'batch_completed_prefix_invalid')
    return state


async def run_batch_stream(turbo, *, skill, accelerators=None, inputs, session, params=None, task='',
                           request_id='', channel_id='', task_id='', resume_ctx=None, user_input=None,
                           auto_batch=False, accelerator=''):
    from jiuwenswarm.server.runtime.skill_turbo import agent as agent_module

    start = time.monotonic()
    manager = ResumeContextManager(session)
    turbo._segment_result = _result(skill)
    plan = ledger = state = None
    active_index = None
    paused = False
    task_id = str(task_id or '').removeprefix('todo:')
    workspace = inputs.get('metadata', {}).get('output_dir', '')
    signature = {'skill': skill, 'query': inputs.get('query', task), 'steps': copy.deepcopy(accelerators),
                 'shared_params': copy.deepcopy(params), 'task_id': task_id,
                 'workspace_base': ''}
    try:
        if type(auto_batch) is not bool or (accelerator and not auto_batch):
            raise SegmentInputError('invalid_args', 'invalid_auto_batch_mode')
        if not isinstance(workspace, str) or not workspace.strip() or not Path(workspace).is_absolute():
            raise SegmentInputError('blocked', 'workspace_missing')
        if auto_batch:
            if not skill or not accelerator or accelerator == '__full__' or accelerators is not None:
                raise SegmentInputError('invalid_args', 'invalid_auto_batch_mode')
            if params is not None and not isinstance(params, dict):
                raise SegmentInputError('invalid_args', 'params_must_be_object')
            signature['auto_request'] = {'accelerator': accelerator, 'params': copy.deepcopy(params)}
            if resume_ctx is not None:
                control = resume_ctx.get('segment')
                batch_control = control.get('batch') if isinstance(control, dict) else None
                saved = batch_control.get('signature') if isinstance(batch_control, dict) else None
                if not isinstance(saved, dict) or not isinstance(saved.get('steps'), list):
                    raise SegmentInputError('failed', 'batch_control_corrupt')
                accelerators = copy.deepcopy(saved['steps'])
            else:
                config = turbo._env.config.get('skill_turbo', {'enabled': turbo._env.config.get('enabled', True)})
                if config.get('enabled', False) is not True or config.get('batch_enabled', True) is not True:
                    raise SegmentInputError('blocked', 'batch_disabled')
                maximum = config.get('max_batch_steps', 8)
                if type(maximum) is not int or maximum < 1:
                    raise SegmentInputError('invalid_args', 'invalid_batch_limit')
                record, _ = await asyncio.to_thread(resolve_execution_skill, turbo._env, skill)
                if not record.manifest_valid:
                    raise SegmentInputError('invalid_args', 'invalid_manifest')
                registry = AcceleratorRegistry(turbo._env)
                spec = registry.get(skill, accelerator)
                if spec is None:
                    raise SegmentInputError('invalid_args', 'unknown_or_invalid_accelerator')
                accelerators = []
                while spec is not None and len(accelerators) < min(maximum, 8):
                    accelerators.append({'id': spec.id, **({'params': copy.deepcopy(params or {})} if not accelerators else {})})
                    if (spec.batch.auto_next is None or not spec.batch.standalone
                            or (spec.hitl.kind == 'review' and not (
                                spec.hitl.continue_after_resume and spec.batch.stop_after == 'never'))):
                        break
                    spec = registry.get(skill, spec.batch.auto_next)
            # 起始参数放在步骤内，不能变成每段共享参数；恢复使用首次实际组批列表。
            params = None
            signature['steps'] = copy.deepcopy(accelerators)
            signature['shared_params'] = None
        if workspace:
            signature['workspace_base'] = str(await asyncio.to_thread(Path(workspace).resolve))
        if resume_ctx is not None:
            if resume_ctx.get('resume_in_flight') is not None and type(resume_ctx['resume_in_flight']) is not bool:
                raise SegmentInputError('failed', 'batch_inflight_invalid')
            if resume_ctx.get('resume_in_flight') is True:
                turbo._segment_result = _result(skill, 'busy', error='resume_in_flight')
                return
            state = await asyncio.to_thread(_resume_state, resume_ctx, signature)
        elif user_input is not None:
            raise SegmentInputError('failed', 'resume_context_unavailable')
        async with skill_execution_lease(session, resume_ctx=resume_ctx):
            stored = await SkillRunLedger.current(session, skill=skill, workspace_base=workspace, task_id=task_id)
            if state is not None:
                ledger = SkillRunLedger(state['ledger'])
                ledger.persisted = state['ledger_persisted']
                ledger.save_outcome = copy.deepcopy(state['ledger_save'])
                if stored is None or stored.task_key != ledger.task_key:
                    raise SegmentInputError('failed', 'batch_task_identity_changed')
                if (stored.data['revision'], stored.data['updated_at']) > (ledger.data['revision'], ledger.data['updated_at']):
                    ledger = stored
            else:
                ledger = stored
            plan = await build_batch_plan(turbo._env, skill=skill, accelerators=accelerators,
                                          params=params, inputs=inputs, ledger=ledger)
            if state is not None and state['execution_snapshot'] != plan.snapshot:
                raise SegmentInputError('failed', 'batch_execution_definition_changed')
            if ledger is None:
                ledger = await SkillRunLedger.open(session, skill=skill, workspace_base=workspace,
                                                   task_id=task_id, query=signature['query'])
            if state is None:
                state = {'version': 1, 'manifest_version': 1, 'batch_id': uuid.uuid4().hex,
                         'signature': signature, 'cursor': 0, 'results': [], 'skipped': [],
                         'warnings': list(plan.warnings), 'partial': False,
                         'execution_snapshot': plan.snapshot}
            for index in range(state['cursor'], len(plan.steps)):
                active_index = index
                step = plan.steps[index]
                current = await SkillRunLedger.load(session, skill=skill, task_key=ledger.task_key)
                if current is None:
                    raise SegmentInputError('failed', 'batch_task_missing')
                if (current.data['revision'], current.data['updated_at']) > (ledger.data['revision'], ledger.data['updated_at']):
                    ledger = current
                state.update(cursor=index, ledger=copy.deepcopy(ledger.data), ledger_persisted=ledger.persisted,
                             ledger_save=copy.deepcopy(ledger.save_outcome))
                control = copy.deepcopy(state)
                control['fingerprint'] = await asyncio.to_thread(data_fingerprint, control)
                child = agent_module.SkillTurbo(turbo._env.config)
                try:
                    async for chunk in child.run_accelerator_stream(
                        skill=skill, accelerator=step.spec.id, params=step.params, inputs=inputs,
                        session=session, request_id=request_id, channel_id=channel_id, task_id=task_id,
                        resume_ctx=resume_ctx if index == state['cursor'] else None,
                        user_input=user_input if resume_ctx is not None else None,
                        ledger_override=ledger, batch_control=control, force=step.force,
                    ):
                        if isinstance(chunk.payload, dict):
                            chunk.payload['batch_id'] = state['batch_id']
                            chunk.payload['batch_index'] = index
                            chunk.payload['accelerator'] = step.spec.id
                            chunk.payload['task_key'] = ledger.task_key
                        yield chunk
                except AbortError:
                    paused = True
                    raise
                finally:
                    child.release_registered_tools()
                result = child.segment_result()
                ledger = getattr(child, '_segment_ledger', None) or ledger
                turbo._executor._node_artifacts_holder.update(copy.deepcopy(child.artifact_holder))
                resumed = resume_ctx is not None
                resume_ctx = None
                if result.get('already_done'):
                    state['skipped'].append({**result, 'reason': 'already_done'})
                elif result.get('executed'):
                    state['results'].append(result)
                if result['status'] not in {'ok', 'partial'}:
                    for future in plan.steps[index + 1:]:
                        state['skipped'].append({'accelerator': future.spec.id, 'reason': 'batch_stopped'})
                    turbo._segment_result = _aggregate(skill, state, plan, status=result['status'], reason='step_failed',
                        cursor=index, stopped=step.spec.id, ledger=ledger, elapsed=time.monotonic() - start)
                    turbo._segment_result['stopped_result'] = result
                    return
                state['partial'] |= result['status'] == 'partial'
                review_stop = step.spec.hitl.kind == 'review' and not (
                    step.spec.hitl.continue_after_resume and step.spec.batch.stop_after == 'never')
                clarify_stop = resumed and step.spec.hitl.kind == 'clarify' and not step.spec.hitl.continue_after_resume
                if not result.get('already_done') and (review_stop or clarify_stop):
                    turbo._segment_result = _aggregate(skill, state, plan,
                        status='partial' if state['partial'] else 'ok', reason='review_checkpoint' if review_stop else 'clarify_checkpoint',
                        cursor=index + 1, ledger=ledger, elapsed=time.monotonic() - start)
                    return
            turbo._segment_result = _aggregate(skill, state, plan, status='partial' if state['partial'] else 'ok',
                reason='completed', cursor=len(plan.steps), ledger=ledger, elapsed=time.monotonic() - start)
    except AbortError:
        raise
    except asyncio.CancelledError:
        if resume_ctx is not None:
            try:
                async with skill_execution_lease(session, resume_ctx=resume_ctx):
                    await manager.clear_matching(resume_ctx)
            except (LedgerBusy, SkillStateError, OSError, ValueError):
                pass
        raise
    except (SegmentInputError, LedgerBusy, LedgerError, SkillStateError, PlanCodeLoadError, PlanCodeValidationError,
            OSError, ValueError, TypeError, KeyError) as exc:
        status = 'busy' if isinstance(exc, LedgerBusy) else ('failed' if resume_ctx else getattr(exc, 'status', 'failed'))
        if plan is not None and state is not None and active_index is not None:
            for future in plan.steps[active_index + 1:]:
                state['skipped'].append({'accelerator': future.spec.id, 'reason': 'batch_stopped'})
            turbo._segment_result = _aggregate(skill, state, plan, status=status, reason='step_failed',
                cursor=active_index, stopped=plan.steps[active_index].spec.id, ledger=ledger,
                elapsed=time.monotonic() - start)
            turbo._segment_result['error'] = str(exc)
            return
        turbo._segment_result = _result(skill, status, error=str(exc), warnings=getattr(exc, 'warnings', []),
                                       ledger=ledger.digest() if ledger else {})
        if state is not None:
            turbo._segment_result.update(batch_id=state['batch_id'], results=copy.deepcopy(state['results']),
                skipped=copy.deepcopy(state['skipped']), facts=copy.deepcopy(ledger.data['facts']) if ledger else {},
                facts_provenance=copy.deepcopy(ledger.data['facts_provenance']) if ledger else {})
            turbo._segment_result['warnings'] = copy.deepcopy(state['warnings']) + turbo._segment_result['warnings']
            for result in state['results']:
                turbo._segment_result['warnings'].extend(result.get('warnings', []))
    finally:
        if auto_batch:
            planned = signature['steps'] if isinstance(signature['steps'], list) else []
            turbo._segment_result['planned_steps'] = [step.get('id') for step in planned if isinstance(step, dict)]
            turbo._segment_result['next_accelerator'] = None
            if plan is not None and turbo._segment_result.get('stop_reason') in {
                'completed', 'review_checkpoint', 'clarify_checkpoint',
            }:
                completed = len(plan.steps) - len(turbo._segment_result['remaining_steps'])
                if completed:
                    turbo._segment_result['next_accelerator'] = plan.steps[completed - 1].spec.batch.auto_next
        if resume_ctx is not None and not paused and resume_ctx.get('resume_in_flight') is not True:
            try:
                async with skill_execution_lease(session, resume_ctx=resume_ctx):
                    await manager.clear_matching(resume_ctx)
            except LedgerBusy:
                pass
            except (SkillStateError, OSError, ValueError) as exc:
                turbo._segment_result['warnings'].append({'code': 'resume_cleanup_failed', 'reason': str(exc)})
        turbo.release_registered_tools()
