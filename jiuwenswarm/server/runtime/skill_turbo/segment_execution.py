"""单段门面编排；状态、输入与后置验收分别委托给原 owner。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

from jiuwenswarm.server.runtime.skill_turbo.accelerator_registry import AcceleratorRegistry
from jiuwenswarm.server.runtime.skill_turbo.environment import Skill
from jiuwenswarm.server.runtime.skill_turbo.executor import PlanCodeLoadError
from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import segment_artifact_namespace
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager, SkillStateError
from jiuwenswarm.server.runtime.skill_turbo.segment_context import (
    SegmentContext, SegmentRunState, data_fingerprint, execution_snapshot,
)
from jiuwenswarm.server.runtime.skill_turbo.segment_inputs import (
    SegmentInputError, assemble_segment_inputs, finalize_segment_result,
    validate_segment_replay_inputs,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import (
    LedgerBusy, LedgerError, SkillRunLedger, skill_execution_lease,
)
from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
    discover_external_turbo_skills, ensure_turbo_package,
)
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidationError


logger = logging.getLogger(__name__)


def resolve_execution_skill(env, name: str):
    # 直接发现，不使用环境注册或描述的TTL/last-good缓存授权执行。
    matches = [item for item in discover_external_turbo_skills() if item.external_name == name]
    if len(matches) != 1:
        raise SegmentInputError('invalid_args', 'unknown_or_ambiguous_skill')
    item = matches[0]
    root = Path(item.turbo_codes_dir).parent.parent
    meta_file = root / 'turbo/meta.json'
    if meta_file.exists():
        meta = json.loads(meta_file.read_bytes())
        if not isinstance(meta, dict) or meta.get('enable_turbo', True) is not True:
            raise SegmentInputError('blocked', 'skill_disabled_or_invalid')
    package = ensure_turbo_package(item.skill_name, item.turbo_codes_dir)
    if not package:
        raise PlanCodeLoadError('execution_package_collision')
    record = Skill(
        name=item.skill_name, description=str(item.meta.get('description', '')), skill_md='',
        external_name=item.external_name, package_name=package, turbo_codes_dir=item.turbo_codes_dir,
        accelerators=item.accelerators, manifest_fingerprint=item.manifest_fingerprint,
        manifest_source=item.manifest_source, manifest_valid=item.manifest_valid, manifest_errors=item.manifest_errors,
    )
    env.register_skill(record)
    env.set_skill_name(record)
    return record, str(root.resolve())


def _result(skill, accelerator, status, **values):
    return {'success': status in {'ok', 'partial'}, 'mode': 'segment', 'skill': skill,
            'accelerator': accelerator, 'status': status, 'facts': {}, 'artifacts': [], 'verified': False,
            'covered_steps': [], 'remaining_steps': [], 'next_steps': [], 'warnings': [],
            'ledger': {}, 'delivery_confirmed': False, 'continue_hint': '', **values}


def _verify_resume(resume, *, skill, accelerator, query, params, workspace, task_id, spec, snapshot):
    control = copy.deepcopy(resume.get('segment'))
    if not isinstance(control, dict):
        raise ValueError('segment_control_missing')
    digest = control.pop('control_fingerprint', None)
    if not digest or digest != data_fingerprint(control):
        raise ValueError('segment_control_corrupt')
    expected = {'version': 1, 'skill': skill, 'accelerator': accelerator, 'query': query,
                'params': params, 'workspace_base': str(Path(workspace).resolve()), 'task_id': task_id,
                'manifest_version': 1, 'plan_code': spec.plan_code, 'execution_snapshot': snapshot}
    if any(control.get(key) != value for key, value in expected.items()):
        raise ValueError('segment_definition_or_identity_changed')
    inputs = resume.get('inputs')
    if not isinstance(inputs, dict) or not isinstance(control.get('initial_inputs'), dict):
        raise ValueError('segment_checkpoint_corrupt')
    if control.get('inputs_fingerprint') != data_fingerprint(inputs):
        raise ValueError('segment_checkpoint_corrupt')
    task_key = control.get('task_key')
    if not isinstance(task_key, str) or not task_key:
        raise ValueError('segment_checkpoint_corrupt')
    if inputs.get('task_key') != task_key or inputs.get('skill_name') != skill:
        raise ValueError('segment_checkpoint_corrupt')
    if resume.get('plan_code') != spec.plan_code:
        raise ValueError('segment_checkpoint_corrupt')
    pending_call = resume.get('pending_tool_call_id')
    if not isinstance(pending_call, str) or not pending_call:
        raise ValueError('segment_checkpoint_corrupt')
    if pending_call != control.get('pending_tool_call_id'):
        raise ValueError('segment_checkpoint_corrupt')
    task_states = resume.get('task_states')
    if not isinstance(task_states, list):
        raise ValueError('segment_checkpoint_corrupt')
    if control.get('task_states_fingerprint') != data_fingerprint(task_states):
        raise ValueError('segment_checkpoint_corrupt')
    child_statuses = control.get('child_statuses')
    answered_tools = control.get('answered_tools')
    if not isinstance(child_statuses, list) or not isinstance(answered_tools, list):
        raise ValueError('segment_checkpoint_corrupt')
    for item in answered_tools:
        if not isinstance(item, dict) or set(item) != {'node', 'arguments', 'index', 'result'}:
            raise ValueError('segment_checkpoint_corrupt')
        if not isinstance(item.get('node'), str) or not isinstance(item.get('arguments'), dict):
            raise ValueError('segment_checkpoint_corrupt')
        index = item.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError('segment_checkpoint_corrupt')
    for item in child_statuses:
        if not isinstance(item, dict) or set(item) != {'node', 'status'}:
            raise ValueError('segment_checkpoint_corrupt')
        if not isinstance(item.get('node'), str) or not isinstance(item.get('status'), str):
            raise ValueError('segment_checkpoint_corrupt')
    return control


async def run_segment_stream(
    env, executor, state: SegmentRunState, *, skill, accelerator, params, inputs, session, request_id, channel_id,
    task_id='', resume_ctx=None, user_input=None, ledger_override=None, batch_control=None, force=False,
):
    task_id = str(task_id or '').removeprefix('todo:')
    query = inputs.get('query', '')
    state.result = _result(skill, accelerator, 'failed')
    ledger = context = None
    manager = ResumeContextManager(session)
    validated_resume = marked = paused = reusable = False
    spec = None
    warnings = []
    try:
        execution_config = env.config.get('skill_turbo', {'enabled': env.config.get('enabled', True)})
        if execution_config.get('enabled', False) is not True:
            raise SegmentInputError('blocked', 'skill_turbo_disabled')
        record, skill_root = await asyncio.to_thread(resolve_execution_skill, env, skill)
        registry = AcceleratorRegistry(env)
        spec = registry.get(skill, accelerator)
        if not accelerator:
            state.result = _result(skill, accelerator, 'needs_accelerator', candidates=[
                {'id': s.id, 'summary': s.summary} for s in registry.list_for(skill)
            ])
            return
        if not record.manifest_valid or spec is None:
            raise SegmentInputError('invalid_args', 'unknown_or_invalid_accelerator')
        if accelerator == '__full__':
            raise SegmentInputError('invalid_args', 'full_pipeline_requires_legacy_boundary')
        snapshot = await asyncio.to_thread(execution_snapshot, skill_root)
        if batch_control is not None and snapshot != batch_control['execution_snapshot']:
            raise SegmentInputError('failed', 'batch_execution_definition_changed')
        if not await asyncio.to_thread(
            env.validate_external_skill_code, record.name, Path(record.turbo_codes_dir) / record.name,
        ):
            raise PlanCodeValidationError(['current_skill_source_invalid'])
        workspace = inputs.get('metadata', {}).get('output_dir', '')
        if not workspace:
            raise SegmentInputError('blocked', 'workspace_missing')
        if user_input is not None and resume_ctx is None:
            raise SegmentInputError('failed', 'resume_context_unavailable')
        control = None
        if resume_ctx is not None:
            if resume_ctx.get('resume_in_flight'):
                state.result = _result(skill, accelerator, 'busy', error='resume_in_flight')
                return
            if bool(resume_ctx.get('segment', {}).get('batch')) != (batch_control is not None):
                raise SegmentInputError('failed', 'resume_execution_mode_changed')
            control = _verify_resume(
                resume_ctx, skill=skill, accelerator=accelerator, query=query, params=params,
                workspace=workspace, task_id=task_id, spec=spec, snapshot=snapshot,
            )
            validated_resume = True
        async with skill_execution_lease(session, resume_ctx=resume_ctx):
            ctx = {
                **copy.deepcopy(inputs), 'inputs': copy.deepcopy(inputs),
                'metadata': copy.deepcopy(inputs.get('metadata', {})),
                'skill_root': skill_root, 'skill_name': skill, 'capabilities': list(env.tools),
            }
            if control is not None:
                ledger = ledger_override or await SkillRunLedger.load(
                    session, skill=skill, task_key=control['task_key'],
                )
                if ledger is None or ledger.data['workspace_base'] != control['workspace_base']:
                    raise ValueError('segment_task_identity_mismatch')
                actual_inputs = copy.deepcopy(resume_ctx['inputs'])
                if (actual_inputs.get('run_dir') != ledger.data['run_dir']
                        or control.get('namespace') != segment_artifact_namespace(ledger.task_key, skill, accelerator)):
                    raise ValueError('segment_namespace_mismatch')
                # 仅重新验证原输入/当前文件，不重新装配或从台账覆盖原值。
                warnings = await validate_segment_replay_inputs(ctx, spec, actual_inputs, ledger)
                await manager.mark_in_flight(resume_ctx)
                marked = True
                executor.set_pending_resume(expected_tool_call_id=resume_ctx['pending_tool_call_id'],
                                            user_input=user_input, task_states=resume_ctx.get('task_states'))
                initial_inputs = copy.deepcopy(control['initial_inputs'])
            else:
                ledger = ledger_override or await SkillRunLedger.current(
                    session, skill=skill, workspace_base=workspace, task_id=task_id,
                )
                if ledger is None:
                    ledger = await SkillRunLedger.open(session, skill=skill, workspace_base=workspace,
                                                       task_id=task_id, query=query)
                actual_inputs, warnings = await assemble_segment_inputs(ctx, spec, params, ledger)
                previous = ledger.data['latest_runs'].get(accelerator)
                if previous and previous['status'] == 'ok' and not force:
                    facts = {
                        key: ledger.data['facts'][key] for key in spec.outputs.facts if key in ledger.data['facts']
                    }
                    checked = await finalize_segment_result(
                        ctx, spec, {'status': 'ok', 'facts': facts}, actual_inputs, initial_inputs=actual_inputs,
                    )
                    trusted = all(
                        ledger.data['facts_provenance'].get(key, {}).get('verified') for key in spec.outputs.facts
                    )
                    if checked['status'] == 'ok' and trusted:
                        historical = copy.deepcopy(previous.get('delivery_evidence', {}))
                        historical_matches = bool(historical.get('sha256')) and any(
                            item.get('path') == historical.get('path') and item.get('sha256') == historical['sha256']
                            for item in checked['artifacts'])
                        stale = [key for key, source in previous.get('consumed_facts', {}).items()
                                 if source != ledger.data['facts_provenance'].get(key)]
                        if previous.get('params') != (params or {}):
                            checked['warnings'].append({'code': 'completed_params_not_reapplied_use_step_force'})
                        if stale:
                            checked['warnings'].append({'code': 'upstream_revision_changed', 'keys': stale})
                        state.result = {**_result(skill, accelerator, 'ok'), **checked,
                            'already_done': True, 'ledger': ledger.digest(), 'verified': True,
                            'next_steps': list(spec.handoff.next_steps), 'continue_hint': spec.handoff.continue_hint,
                            'covered_steps': list(spec.covers.steps), 'warnings': warnings + checked['warnings'],
                            'delivery_confirmed': (
                                previous.get('delivery_confirmed', False) is True and historical_matches and not stale
                            ),
                            'delivery_owner': previous.get('delivery_owner', ''),
                            'historical_delivery': historical,
                            'historical_delivery_matches_current_file': historical_matches}
                        return
                    if batch_control is not None:
                        raise SegmentInputError(
                            'blocked', 'completed_artifacts_invalid_repair_or_force', checked['warnings'],
                        )
                if previous is not None or any(
                    p.get('produced_by') == accelerator for p in ledger.data['facts_provenance'].values()
                ):
                    if not await ledger.prepare_rerun(session, spec=spec):
                        raise SegmentInputError('blocked', 'rerun_invalidation_not_persisted')
                initial_inputs = copy.deepcopy(actual_inputs)
                control = {'version': 1, 'skill': skill, 'accelerator': accelerator, 'query': query,
                           'params': copy.deepcopy(params), 'workspace_base': ledger.data['workspace_base'],
                           'task_key': ledger.task_key, 'task_id': task_id, 'manifest_version': 1,
                           'plan_code': spec.plan_code, 'execution_snapshot': snapshot,
                           'namespace': segment_artifact_namespace(ledger.task_key, skill, accelerator)}
            if batch_control is not None:
                batch = copy.deepcopy(batch_control)
                batch.update(ledger=copy.deepcopy(ledger.data), ledger_persisted=ledger.persisted,
                             ledger_save=copy.deepcopy(ledger.save_outcome))
                batch.pop('fingerprint', None)
                batch['fingerprint'] = data_fingerprint(batch)
                control['batch'] = batch
            context = SegmentContext(
                skill, accelerator, ledger.task_key, spec, control['namespace'], control, initial_inputs,
            )
            if resume_ctx is not None:
                context.child_statuses = {item['node']: item['status'] for item in control.get('child_statuses')}
                context.answered_tools = copy.deepcopy(control.get('answered_tools'))
            executor.segment_context = context
            try:
                async for chunk in executor.execute_plan_stream(spec.plan_code, actual_inputs, request_id, channel_id):
                    payload = chunk.payload
                    if isinstance(payload, dict) and payload.get('event_type') == 'chat.error':
                        context.error = str(payload.get('error') or 'execution_failed')
                        continue
                    yield chunk
            except AbortError:
                if spec.hitl.can_pause:
                    paused = True
                    raise
                context.error = '该段不支持人工中断'
            except asyncio.CancelledError:
                try:
                    await manager.clear()
                except (SkillStateError, OSError, ValueError) as exc:
                    logger.warning('cancelled segment checkpoint cleanup failed: %s', exc)
                raise
            except (PlanCodeValidationError, PlanCodeLoadError):
                raise
            except Exception as exc:
                if validated_resume and not context.executed and isinstance(exc, (OSError, asyncio.TimeoutError)):
                    reusable = True
                context.error = str(exc)
            if reusable:
                state.result = _result(skill, accelerator, 'failed', error=context.error,
                                                checkpoint_reusable=True, ledger=ledger.digest())
                return
            checked = await finalize_segment_result(
                ctx, spec, context.outcome(), context.outputs or actual_inputs,
                context.handoff, initial_inputs=initial_inputs,
            )
            checked['warnings'] = warnings + checked['warnings']
            checked['params'] = copy.deepcopy(params or {})
            checked['consumed_facts'] = {req.key: copy.deepcopy(ledger.data['facts_provenance'].get(req.key, {}))
                                         for req in spec.requires if req.kind == 'ledger_fact'}
            if batch_control is not None:
                checked.update(batch_id=batch_control['batch_id'], batch_index=batch_control['cursor'])
            holder = executor.node_artifacts
            delivery = [(owner, entry) for owner, entry in holder.items()
                        if entry.get('info', {}).get('send_file_status') == 'sent']
            if delivery:
                owner, entry = delivery[-1]
                checked['delivery_confirmed'] = True
                checked['delivery_owner'] = owner
                evidence = copy.deepcopy(entry.get('delivery_evidence', {}))
                if evidence:
                    evidence['send_calls'] = copy.deepcopy(context.send_calls)
                    entry['delivery_evidence'] = evidence
                    checked['delivery_evidence'] = evidence
            if context.executed:
                ledger.record_segment(spec=spec, result=checked, plan_code_hash=executor.hash_plan_code(spec.plan_code))
                if not await ledger.save(session):
                    checked['warnings'].append({'code': 'ledger_save_failed'})
            next_steps = spec.handoff.next_steps
            if isinstance(context.handoff, dict):
                next_steps = context.handoff.get('next_steps', next_steps)
            if not isinstance(next_steps, (list, tuple)) or any(not isinstance(step, str) for step in next_steps):
                checked['warnings'].append({'code': 'handoff_next_steps_invalid'})
                next_steps = spec.handoff.next_steps
            state.result = {**_result(skill, accelerator, checked['status']), **checked,
                'ledger': ledger.digest(), 'verified': checked['status'] == 'ok',
                'covered_steps': list(spec.covers.steps) if checked['status'] == 'ok' else [],
                'remaining_steps': [] if checked['status'] == 'ok' else list(spec.covers.steps),
                'next_steps': list(next_steps), 'fallback_count': context.fallback_count,
                'continue_hint': spec.handoff.continue_hint,
                'delivery_confirmed': bool(delivery)}
            error = context.error or context.root_output.get('error')
            if error:
                state.result['error'] = str(error)
            if delivery:
                owner, entry = delivery[-1]
                state.result['delivery_owner'] = owner
                if entry.get('delivery_evidence'):
                    state.result['delivery_evidence'] = copy.deepcopy(entry['delivery_evidence'])
                if entry.get('delivery_summary'):
                    state.result['delivery_summary'] = entry['delivery_summary']
    except AbortError:
        raise
    except (
        LedgerError, SkillStateError,
        PlanCodeValidationError, PlanCodeLoadError, OSError, ValueError, TypeError,
    ) as exc:
        status = 'failed' if resume_ctx is not None else getattr(exc, 'status', 'failed')
        if isinstance(exc, LedgerBusy):
            status = 'busy'
        state.result = _result(skill, accelerator, status, error=str(exc),
                                       warnings=getattr(exc, 'warnings', []), ledger=ledger.digest() if ledger else {},
                                       remaining_steps=list(spec.covers.steps) if spec else [],
                                       continue_hint=spec.handoff.continue_hint if spec else '')
        for owner, entry in executor.node_artifacts.items():
            if entry.get('info', {}).get('send_file_status') == 'sent':
                state.result.update(delivery_confirmed=True, delivery_owner=owner,
                    delivery_evidence=copy.deepcopy(entry.get('delivery_evidence', {})))
    finally:
        state.ledger = ledger
        state.result['executed'] = bool(context and context.executed)
        try:
            if resume_ctx is not None and not paused:
                expected_checkpoint = {**resume_ctx, 'resume_in_flight': True} if marked else resume_ctx
                try:
                    # busy 拒绝可能早于原恢复写 in-flight；清理也须持有同一业务租约。
                    async with skill_execution_lease(session, resume_ctx=expected_checkpoint):
                        if reusable:
                            await manager.clear_in_flight()
                        elif not resume_ctx.get('resume_in_flight'):
                            await manager.clear_matching(expected_checkpoint)
                except LedgerBusy:
                    pass
            elif context is not None and not spec.hitl.can_pause:
                # 防止下层提前保存本次断点；不触碰其他任务的暂停槽。
                pending = await manager.load()
                if pending and pending.get('segment', {}).get('task_key') == context.task_key:
                    await manager.clear()
        except (SkillStateError, OSError, ValueError) as exc:
            state.result['warnings'].append({'code': 'resume_cleanup_failed', 'reason': str(exc)})
        finally:
            env.release_registered_tools()
