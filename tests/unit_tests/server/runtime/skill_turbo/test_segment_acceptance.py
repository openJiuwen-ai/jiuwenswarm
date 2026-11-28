"""公开SDK工具入口、批量披露及串行恢复验收。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_batch_execution import batch_fixture, segment, storage, isolated
from jiuwenswarm.server.runtime.skill_turbo import agent as agent_module, skill_turbo_tools as tools
from jiuwenswarm.server.runtime.skill_turbo.context_binding import bind_skill_turbo_context, reset_skill_turbo_context
from jiuwenswarm.server.runtime.skill_turbo.accelerator_renderer import render_guide, render_appendix
from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec
from jiuwenswarm.server.runtime.skill_turbo.resume_context import isolated_skill_state


@pytest.fixture
def invoke_batch(batch_fixture, monkeypatch):
    parent = batch_fixture.storage.session(turbo=False)
    write = AsyncMock()
    monkeypatch.setattr(parent, 'write_stream', write)
    adapter = SimpleNamespace(_instance=SimpleNamespace(card=parent._card),
        build_skill_turbo_config=lambda: {'enabled':True}, restore_skill_turbo_model_from_resume_ctx=Mock())
    async def invoke(arguments, answers=None):
        ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name='skill_acceleration_exec', tool_call=SimpleNamespace(id='outer-batch')))
        bind_skill_turbo_context(ctx, adapter=adapter, parent_session=parent,
            request_metadata={**batch_fixture.segment.inputs['metadata'], 'session_id':parent.get_session_id(), 'request_id':'req'},
            resume_answers=answers)
        try:
            return await tools.skill_turbo.invoke(arguments)
        finally:
            reset_skill_turbo_context(ctx)
    return SimpleNamespace(invoke=invoke, writes=write)


async def test_actual_sdk_batch_schema_and_execution(batch_fixture, invoke_batch):
    schema = tools.skill_turbo.card.input_params
    assert 'accelerators' in schema['properties']
    assert 'force' not in schema['properties'] and 'finish_request' not in schema['properties']
    result = await invoke_batch.invoke({'query':'original', 'skill':'demo-craft',
        'accelerators':[{'id':'prep'},{'id':'outline'}]})
    assert result['mode'] == 'batch' and result['status'] == 'ok', result
    assert result['ledger']['revision'] == 2
    assert batch_fixture.calls['prep'] == batch_fixture.calls['outline'] == 1
    assert invoke_batch.writes.await_count
    assert all(call.args[0].type != 'answer' for call in invoke_batch.writes.call_args_list)


@pytest.mark.parametrize('arguments', [
    {'query':'original','skill':'demo-craft','accelerators':[]},
    {'query':'original','accelerators':[{'id':'prep'}]},
    {'query':'original','skill':'demo-craft','accelerator':'prep','accelerators':[{'id':'prep'}]},
    {'query':'original','skill':'demo-craft','accelerators':[{'id':'prep','force':'true'}]},
    {'query':'original','skill':'demo-craft','accelerators':[{'id':'prep'}],'params':{'unknown':None}},
])
async def test_sdk_invalid_batch_has_no_business(batch_fixture, invoke_batch, arguments):
    result = await invoke_batch.invoke(arguments)
    assert result['status'] == 'invalid_args' and not batch_fixture.calls, result


async def test_actual_tool_pause_marker_and_original_whole_call_resume(batch_fixture, invoke_batch):
    arguments = {'query':'original','skill':'demo-craft','accelerators':[
        {'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]}
    paused = await invoke_batch.invoke(arguments)
    assert tools.get_skill_turbo_hitl_tic() is not None
    assert '__skill_turbo_hitl__' in str(paused)
    checkpoint = await batch_fixture.state()
    assert checkpoint['segment']['batch']['cursor'] == 1
    result = await invoke_batch.invoke(arguments, answers={'Review outline?':'yes'})
    assert result['status'] == 'ok' and batch_fixture.calls['prep'] == 1, result
    assert batch_fixture.calls['generate'] == 1 and await batch_fixture.state() is None


@pytest.mark.parametrize('language', ['cn','en'])
def test_model_guide_only_exposes_enabled_batch_and_step_force(batch_fixture, language):
    enabled = render_guide(language, batch_enabled=True, max_batch_steps=3)
    disabled = render_guide(language, batch_enabled=False)
    assert 'accelerators=[' in enabled and '"force":false' in enabled
    assert 'accelerators=[' not in disabled and '"force":false' not in disabled
    assert ('1～3' if language == 'cn' else '1–3') in enabled
    specs = [AcceleratorSpec.model_validate(raw) for raw in batch_fixture.specs]
    text = render_appendix('demo-craft', specs, {'progress':'no_progress'}, language=language, batch_enabled=True)
    assert '"standalone"' in text and '"stop_after"' in text


async def test_changed_completed_params_need_explicit_force(batch_fixture):
    await batch_fixture.run([{'id':'prep','params':{'value':2}}])
    skipped, _ = await batch_fixture.run([{'id':'prep','params':{'value':3}}])
    assert skipped['results'] == [] and batch_fixture.calls['prep'] == 1
    assert any(w['code'] == 'completed_params_not_reapplied_use_step_force' for w in skipped['warnings'])
    forced, _ = await batch_fixture.run([{'id':'prep','params':{'value':3},'force':True}])
    assert forced['status'] == 'ok' and batch_fixture.calls['prep'] == 2, forced
    assert forced['fact_updated_by']['prep_fact']['revision'] == 2


@pytest.mark.parametrize('continue_after,stop_after,expected', [(False,'review_checkpoint',False),(True,'review_checkpoint',False),
    (False,'never',False),(True,'never',True)])
async def test_review_resumed_completion_obeys_both_fields(batch_fixture, continue_after, stop_after, expected):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    batch_fixture.specs[1]['hitl'] = {'kind':'review','continue_after_resume':continue_after}
    batch_fixture.specs[1]['batch'] = {'stop_after':stop_after}
    batch_fixture.save()
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    assert not batch_fixture.calls['generate']
    checkpoint = await batch_fixture.state()
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert result['status'] == 'ok', result
    assert bool(batch_fixture.calls['generate']) is expected


async def test_clarify_explicit_no_continue_stops_after_answer(batch_fixture):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    batch_fixture.specs[1]['hitl'] = {'kind':'clarify','continue_after_resume':False}
    batch_fixture.save()
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    result, _ = await batch_fixture.run(items, resume_ctx=await batch_fixture.state(), user_input={'Review outline?':'yes'})
    assert result['stop_reason'] == 'clarify_checkpoint' and result['resume_remaining'] == ['generate'], result
    assert not batch_fixture.calls['generate']

async def test_repeated_answers_and_inflight_snapshot_do_not_repeat_prefix(batch_fixture):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    import copy
    inflight = copy.deepcopy(checkpoint)
    inflight['resume_in_flight'] = True
    async with isolated_skill_state(batch_fixture.storage.session(), write=True) as state:
        from jiuwenswarm.server.runtime.skill_turbo.resume_context import SKILL_TURBO_RESUME_CTX_KEY
        state.replace(SKILL_TURBO_RESUME_CTX_KEY, inflight)
    busy, _ = await batch_fixture.run(items, resume_ctx=inflight, user_input={'Review outline?':'yes'})
    assert busy['status'] == 'busy' and await batch_fixture.state() == inflight
    async with isolated_skill_state(batch_fixture.storage.session(), write=True) as state:
        state.replace(SKILL_TURBO_RESUME_CTX_KEY, checkpoint)
    good, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert good['status'] == 'ok', good
    before = batch_fixture.calls.copy()
    repeated, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert repeated['success'] is False and batch_fixture.calls == before, repeated


async def test_two_cards_keep_cursor_and_completed_prefix(batch_fixture):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True,'pause_twice':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    with pytest.raises(AbortError):
        await batch_fixture.run(items, resume_ctx=await batch_fixture.state(), user_input={'Review outline?':'yes'})
    second = await batch_fixture.state()
    assert second['segment']['batch']['cursor'] == 1 and batch_fixture.calls['prep'] == 1
    completed, _ = await batch_fixture.run(items, resume_ctx=second, user_input={'Again outline?':'yes'})
    assert completed['status'] == 'ok' and batch_fixture.calls['leaf:outline'] == 1, completed
    assert batch_fixture.calls['prep'] == batch_fixture.calls['outline'] == batch_fixture.calls['generate'] == 1


async def test_upstream_force_does_not_recompute_downstream_but_reports_staleness(batch_fixture):
    await batch_fixture.run([{'id':'prep'},{'id':'outline'}])
    result, _ = await batch_fixture.run([{'id':'prep','force':True},{'id':'outline'}])
    assert result['status'] == 'ok' and batch_fixture.calls['prep'] == 2 and batch_fixture.calls['outline'] == 1, result
    assert result['fact_updated_by']['prep_fact']['revision'] == 3
    assert any(w['code'] == 'upstream_revision_changed' for w in result['warnings'])
    assert result['delivery_confirmed'] is False


async def test_code_validation_failure_precedes_task_creation(batch_fixture):
    batch_fixture.segment.code.write_text('import subprocess\nroot = None\n', encoding='utf-8')
    result, _ = await batch_fixture.run()
    assert result['status'] == 'failed' and not batch_fixture.calls, result
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    assert await batch_fixture.state(SkillRunLedger.KEY) is None

@pytest.mark.parametrize('consumed,expected', [('prep_fact','partial'),('other_fact','blocked')])
async def test_partial_preserves_verified_and_unverified_facts_independently(batch_fixture, consumed, expected):
    first, second = batch_fixture.specs[:2]
    first['outputs']['facts'].append('other_fact')
    first['outputs']['postconditions'].append({'kind':'fact','key':'other_fact'})
    second['requires'] = [{'kind':'ledger_fact','key':consumed,'producer':'prep'}]
    second['input_schema']['properties'][consumed] = {'type':'string'}
    second['input_schema']['required'] = [consumed]
    batch_fixture.save()
    result, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline'}])
    assert result['status'] == expected, result
    assert result['facts_provenance']['prep_fact']['verified'] is True
    assert result['facts_provenance']['other_fact']['verified'] is False
    assert bool(batch_fixture.calls['outline']) is (consumed == 'prep_fact')


async def test_soft_precondition_does_not_waive_unreachable_required_schema(batch_fixture):
    second = batch_fixture.specs[1]
    second['requires'] = [{'kind':'ledger_fact','key':'unknown_fact','level':'soft'}]
    second['input_schema']['properties']['unknown_fact'] = {'type':'string'}
    second['input_schema']['required'] = ['unknown_fact']
    batch_fixture.save()
    result, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline'}])
    assert result['status'] == 'invalid_args' and not batch_fixture.calls, result
    assert any(w['code'] == 'unreachable_ledger_fact' for w in result['warnings'])

async def test_cancellation_is_not_converted_to_failed_when_cleanup_storage_fails(batch_fixture, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager
    batch_fixture.control['outline'] = {'error':asyncio.CancelledError()}
    monkeypatch.setattr(ResumeContextManager, 'clear', AsyncMock(side_effect=OSError('checkpoint unavailable')))
    with pytest.raises(asyncio.CancelledError):
        await batch_fixture.run()
    assert batch_fixture.calls['prep'] == 1 and not batch_fixture.calls['generate']

@pytest.mark.parametrize('field,value', [('version',True),('ledger_persisted','true'),('batch_id',1),
    ('results',[None]),('facts',[]),('revision','1'),('resume_in_flight','false')])
async def test_typed_batch_control_corruption_is_rejected_before_replay(batch_fixture, field, value):
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
    from jiuwenswarm.server.runtime.skill_turbo.segment_context import data_fingerprint
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import SKILL_TURBO_RESUME_CTX_KEY
    items = [{'id':'prep','params':{'pause':True}},{'id':'outline'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    control = checkpoint['segment']
    batch = control['batch']
    if field == 'resume_in_flight':
        checkpoint[field] = value
    elif field in {'facts','revision'}:
        batch['ledger'][field] = value
    else:
        batch[field] = value
    batch.pop('fingerprint')
    batch['fingerprint'] = data_fingerprint(batch)
    control.pop('control_fingerprint')
    control['control_fingerprint'] = data_fingerprint(control)
    async with isolated_skill_state(batch_fixture.storage.session(), write=True) as state:
        state.replace(SKILL_TURBO_RESUME_CTX_KEY, checkpoint)
    before = batch_fixture.calls.copy()
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review prep?':'yes'})
    assert result['status'] == 'failed' and batch_fixture.calls == before, result
    assert await batch_fixture.state() is None

async def test_invalid_shared_value_cannot_be_hidden_by_step_override(batch_fixture):
    result, _ = await batch_fixture.run([{'id':'prep','params':{'value':2}},{'id':'outline','params':{'value':3}}], params={'value':0})
    assert result['status'] == 'invalid_args' and not batch_fixture.calls, result
    assert not list(batch_fixture.segment.workspace.glob('skill_runs/*/*'))

async def test_between_step_storage_read_error_keeps_prefix_and_identifies_stopped_step(batch_fixture, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    original = SkillRunLedger.load
    calls = 0
    async def load(cls, session, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('state read unavailable')
        return await original(session, **kwargs)
    monkeypatch.setattr(SkillRunLedger, 'load', classmethod(load))
    result, _ = await batch_fixture.run()
    assert result['status'] == 'failed' and result['stopped_at'] == 'outline', result
    assert result['resume_remaining'] == ['generate'] and result['results'][0]['accelerator'] == 'prep'
    assert batch_fixture.calls['prep'] == 1 and not batch_fixture.calls['outline']

async def test_non_adjacent_declared_producer_is_reachable(batch_fixture):
    third = batch_fixture.specs[2]
    third['requires'] = [{'kind':'ledger_fact','key':'prep_fact','producer':'prep'}]
    third['input_schema']['properties']['prep_fact'] = {'type':'string'}
    third['input_schema']['required'] = ['prep_fact']
    batch_fixture.save()
    result, _ = await batch_fixture.run()
    assert result['status'] == 'ok' and batch_fixture.calls['generate'] == 1, result
    assert result['results'][2]['consumed_facts']['prep_fact']['produced_by'] == 'prep'
