"""批量真实PlanNode/SDK状态，外部工具与模型边界替身。"""

import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback import AbortError

from _segment_fixture import segment, storage, isolated
from test_accelerator_manifest import declaration
from jiuwenswarm.server.runtime.skill_turbo import agent as agent_module
from jiuwenswarm.server.runtime.skill_turbo.resume_context import SKILL_TURBO_RESUME_CTX_KEY, isolated_skill_state

SOURCE = '''from skill_turbo_runtime import PlanNode
class Leaf(PlanNode):
    async def _execute(self, inputs):
        return await self.call_tool('batch_probe', stage='leaf:STAGE', inputs=inputs)
class Root(PlanNode):
    async def _execute(self, inputs):
        await self.execute_subplan(self.sub_plans[0], inputs)
        if inputs.get('pause'):
            await self.call_tool('ask_user', questions=[{'question':'Review STAGE?', 'options':['yes','edit']}])
            if inputs.get('pause_twice'):
                await self.call_tool('ask_user', questions=[{'question':'Again STAGE?', 'options':['yes','edit']}])
        return await self.call_tool('batch_probe', stage='STAGE', inputs=inputs)
root=Root(plan_name='root_STAGE', instruction='STAGE', sub_plans=[Leaf(plan_name='leaf_STAGE', instruction='leaf')])
'''


@pytest.fixture
def batch_fixture(segment, monkeypatch, storage):
    specs = [declaration(name, index * 10) for index, name in enumerate(('prep', 'outline', 'generate'), 1)]
    for index, spec in enumerate(specs):
        props = {'pause': {'type': 'boolean'}, 'pause_twice': {'type': 'boolean'}, 'value': {'type': 'integer', 'minimum': 1}}
        if index:
            key = specs[index - 1]['outputs']['facts'][0]
            props[key] = {'type': 'string'}
            spec['input_schema']['required'] = [key]
            spec['requires'] = [{'kind': 'ledger_fact', 'key': key, 'producer': specs[index - 1]['id']}]
        spec['input_schema']['properties'] = props
        spec['cost'] = {'p50_seconds': 1.0, 'llm_calls': 1}
        (segment.code.parent / (spec['entry']['module'] + '.py')).write_text(SOURCE.replace('STAGE', spec['id']), encoding='utf-8')
    raw = {**segment.raw, 'accelerators': specs}
    manifest = segment.skill / 'turbo/accelerators.json'
    def save():
        manifest.write_text(json.dumps(raw), encoding='utf-8')
    save()
    calls = Counter()
    inputs_seen = []
    control = {}

    async def probe(stage, inputs):
        calls[stage] += 1
        inputs_seen.append((stage, copy.deepcopy(inputs)))
        if stage == 'common' or stage.startswith('leaf:'):
            return {'status': 'ok'}
        setting = control.get(stage, {})
        if setting.get('wait'):
            await setting['wait']()
        if setting.get('error'):
            raise setting['error']
        path = Path(inputs['run_dir']) / (stage + '.json')
        path.write_text(json.dumps({'stage': stage, 'value': inputs.get('value')}), encoding='utf-8')
        result = {'status': setting.get('status', 'ok'), 'facts': {stage + '_fact': stage + '-ready'}}
        result.update(copy.deepcopy(setting.get('result', {})))
        return result

    def new():
        value = segment.new()
        monkeypatch.setattr(value._env, 'get_tool_function', lambda name: probe if name == 'batch_probe' else None)
        return value
    monkeypatch.setattr(agent_module, 'SkillTurbo', lambda config: new())

    async def run(items=None, *, params=None, resume_ctx=None, user_input=None, config=None,
                  auto_batch=False, accelerator='', **changes):
        value = new()
        if config is not None:
            value._env.config['skill_turbo'] = {'enabled': True, **config}
        frames = [frame async for frame in value.run_batch_stream(
            skill='demo-craft', accelerators=items if items is not None or auto_batch else [{'id': s['id']} for s in specs],
            auto_batch=auto_batch, accelerator=accelerator,
            params=params, inputs={**segment.inputs, **changes}, session=storage.session(),
            request_id='req', channel_id='test', resume_ctx=resume_ctx, user_input=user_input)]
        return value.segment_result(), frames

    async def state(key=SKILL_TURBO_RESUME_CTX_KEY):
        async with isolated_skill_state(storage.session()) as data:
            return data.get(key)

    return SimpleNamespace(run=run, calls=calls, control=control, specs=specs, raw=raw, save=save,
        new=new, state=state, segment=segment, storage=storage, inputs_seen=inputs_seen)


async def test_three_fresh_instances_commit_and_consume_actual_upstream(batch_fixture):
    result, frames = await batch_fixture.run()
    assert result['mode'] == 'batch' and result['status'] == 'ok', result
    assert [step['accelerator'] for step in result['results']] == ['prep', 'outline', 'generate']
    assert result['ledger']['revision'] == 3 and result['metrics']['executed'] == 3
    assert batch_fixture.calls == {name: 1 for stage in ('prep', 'outline', 'generate') for name in (stage, 'leaf:' + stage)}
    assert result['resume_remaining'] == [] and not result['delivery_confirmed']
    assert all(frame.payload.get('batch_id') == result['batch_id'] for frame in frames if frame.payload)


@pytest.mark.parametrize('items', [[], [{'id':'prep'}] * 9, [{'id':'prep'}, {'id':'prep'}],
    [{'id':'outline'}, {'id':'prep'}], [{'id':'__full__'}], [{'id':'unknown'}],
    [{'id':'prep', 'force':'false'}], [{'id':'prep','skill':'other'}], [{'id':'prep','params':[]}], [None]])
async def test_static_invalid_batches_have_zero_business_and_no_task(batch_fixture, items):
    result, _ = await batch_fixture.run(items)
    assert result['status'] == 'invalid_args', result
    assert not batch_fixture.calls
    assert not list(batch_fixture.segment.workspace.glob('skill_runs/*/*'))
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    assert await batch_fixture.state(SkillRunLedger.KEY) is None


async def test_exclusive_single_item_and_force(batch_fixture):
    batch_fixture.specs[0]['batch'] = {'standalone': False}
    batch_fixture.save()
    bad, _ = await batch_fixture.run([{'id':'prep'}, {'id':'outline'}])
    assert bad['status'] == 'invalid_args' and not batch_fixture.calls
    first, _ = await batch_fixture.run([{'id':'prep'}])
    mixed, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline'}])
    assert mixed['status'] == 'invalid_args' and batch_fixture.calls['prep'] == 1
    second, _ = await batch_fixture.run([{'id':'prep'}])
    third, _ = await batch_fixture.run([{'id':'prep', 'force': True}])
    assert first['ledger']['revision'] == 1 and second['results'] == []
    assert second['skipped'][0]['reason'] == 'already_done'
    assert third['ledger']['revision'] == 2 and batch_fixture.calls['prep'] == 2


async def test_shared_params_none_override_and_unknown_keys(batch_fixture):
    bad, _ = await batch_fixture.run(params={'unknown': 1})
    assert bad['status'] == 'invalid_args' and not batch_fixture.calls
    bad, _ = await batch_fixture.run(params={'value': 0})
    assert bad['status'] == 'invalid_args' and not batch_fixture.calls
    good, _ = await batch_fixture.run([{'id':'prep','params':{'value':None}}, {'id':'outline','params':{'value':7}}], params={'value':2})
    assert good['status'] == 'ok', good
    assert next(value['value'] for stage, value in batch_fixture.inputs_seen if stage == 'prep') == 2
    assert next(value['value'] for stage, value in batch_fixture.inputs_seen if stage == 'outline') == 7


async def test_unreachable_producer_and_future_required(batch_fixture):
    bad, _ = await batch_fixture.run([{'id':'outline'}])
    assert bad['status'] == 'blocked' and not batch_fixture.calls
    good, _ = await batch_fixture.run([{'id':'prep'}, {'id':'outline'}])
    assert good['status'] == 'ok', good


@pytest.mark.parametrize('status', ['failed', 'blocked', 'not_applicable'])
async def test_stop_keeps_prior_results_and_does_not_execute_later(batch_fixture, status):
    batch_fixture.control['outline'] = {'status': status}
    result, _ = await batch_fixture.run()
    assert result['status'] == status and result['stopped_at'] == 'outline', result
    assert result['resume_remaining'] == ['generate']
    assert batch_fixture.calls['prep'] == 1 and batch_fixture.calls['generate'] == 0
    assert result['ledger']['revision'] == 2


async def test_partial_verified_facts_continue_without_becoming_ok(batch_fixture):
    batch_fixture.control['prep'] = {'status':'partial'}
    result, _ = await batch_fixture.run()
    assert result['status'] == 'partial' and batch_fixture.calls['generate'] == 1, result


@pytest.mark.parametrize('continue_after,stop_after,expected', [(False,'review_checkpoint',False), (True,'review_checkpoint',False),
    (False,'never',False),(True,'never',True)])
async def test_review_combination_without_a_card(batch_fixture, continue_after, stop_after, expected):
    batch_fixture.specs[1]['hitl'] = {'kind':'review','continue_after_resume':continue_after}
    batch_fixture.specs[1]['batch'] = {'stop_after':stop_after}
    batch_fixture.save()
    result, _ = await batch_fixture.run()
    assert result['status'] == 'ok', result
    assert bool(batch_fixture.calls['generate']) is expected
    assert result['stop_reason'] == ('completed' if expected else 'review_checkpoint')


async def test_pause_resume_skips_completed_prefix_and_leaf(batch_fixture):
    items = [{'id':'prep'}, {'id':'outline','params':{'pause':True}}, {'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    assert checkpoint['segment']['batch']['cursor'] == 1
    assert checkpoint['resume_in_flight'] is None
    assert batch_fixture.calls['prep'] == 1 and not batch_fixture.calls['generate']
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert result['status'] == 'ok', result
    assert batch_fixture.calls['prep'] == 1 and batch_fixture.calls['leaf:outline'] == 1
    assert batch_fixture.calls['outline'] == batch_fixture.calls['generate'] == 1
    assert await batch_fixture.state() is None


async def test_future_source_drift_refuses_resume_before_current_node(batch_fixture):
    items = [{'id':'prep'}, {'id':'outline','params':{'pause':True}}, {'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    future = batch_fixture.segment.code.parent / 'accel_generate.py'
    future.write_text(future.read_text(encoding='utf-8') + '\n# changed future source\n', encoding='utf-8')
    before = batch_fixture.calls.copy()
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert result['status'] == 'failed' and batch_fixture.calls == before, result
    assert await batch_fixture.state() is None


async def test_result_save_failure_keeps_verified_facts_for_later_steps(batch_fixture, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    original = SkillRunLedger.save
    async def save(ledger, session):
        if ledger.data['revision'] == 1:
            batch_fixture.storage.cp.mode = 'before'
            try:
                return await original(ledger, session)
            finally:
                batch_fixture.storage.cp.mode = ''
        return await original(ledger, session)
    monkeypatch.setattr(SkillRunLedger, 'save', save)
    result, _ = await batch_fixture.run()
    assert result['status'] == 'ok' and result['ledger']['revision'] == 3, result
    assert any(w['code'] == 'ledger_save_failed' for w in result['warnings'])
    assert result['results'][0]['ledger']['persisted'] is False


async def test_deleted_completed_artifact_does_not_silently_rerun(batch_fixture):
    first, _ = await batch_fixture.run([{'id':'prep'}])
    Path(first['artifacts'][0]['path']).unlink()
    result, _ = await batch_fixture.run([{'id':'prep'}])
    assert result['status'] == 'blocked' and batch_fixture.calls['prep'] == 1, result

@pytest.mark.parametrize('mode', ['before', 'drop', 'after', 'confirm'])
async def test_initial_identity_requires_confirmed_save(batch_fixture, mode):
    batch_fixture.storage.cp.mode = mode
    result, _ = await batch_fixture.run()
    assert result['status'] != 'ok' and not batch_fixture.calls, result


async def test_force_requires_confirmed_invalidation(batch_fixture):
    await batch_fixture.run([{'id':'prep'}])
    before = batch_fixture.calls.copy()
    batch_fixture.storage.cp.mode = 'before'
    result, _ = await batch_fixture.run([{'id':'prep','force':True}, {'id':'outline'}])
    assert result['status'] == 'blocked' and batch_fixture.calls == before, result


async def test_unknown_none_step_param_is_not_erased(batch_fixture):
    result, _ = await batch_fixture.run([{'id':'prep','params':{'unknown':None}}])
    assert result['status'] == 'invalid_args' and not batch_fixture.calls, result


@pytest.mark.parametrize('kind', ['anyOf', 'oneOf'])
async def test_union_missing_reachable_fact_but_bad_explicit_value_is_rejected(batch_fixture, kind):
    second = batch_fixture.specs[1]['input_schema']
    second.pop('required')
    second[kind] = [{'required':['prep_fact']}, {'required':['value'], 'properties':{'value':{'minimum':5}}}]
    batch_fixture.save()
    bad, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline','params':{'value':'bad'}}])
    assert bad['status'] == 'invalid_args' and not batch_fixture.calls, bad
    good, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline'}])
    assert good['status'] == 'ok', good


async def test_ambiguous_oneof_cannot_be_hidden_by_unrelated_future_fact(batch_fixture):
    schema = batch_fixture.specs[1]['input_schema']
    schema['properties'].update(x={'type':'integer'}, y={'type':'integer'})
    schema['oneOf'] = [{'required':['x']},{'required':['y']}]
    batch_fixture.save()
    result, _ = await batch_fixture.run([{'id':'prep'},{'id':'outline','params':{'x':1,'y':2}}])
    assert result['status'] == 'invalid_args' and not batch_fixture.calls, result


async def test_partial_unverified_fact_blocks_hard_consumer(batch_fixture):
    batch_fixture.control['prep'] = {'status':'partial', 'result':{'facts':{}}}
    result, _ = await batch_fixture.run()
    assert result['status'] == 'blocked' and result['results'][0]['status'] == 'partial', result
    assert not batch_fixture.calls['outline'] and not batch_fixture.calls['generate']


async def test_partial_then_review_preserves_partial_and_skipped_review_does_not_stop_again(batch_fixture):
    batch_fixture.specs[1]['hitl'] = {'kind':'review'}
    batch_fixture.save()
    batch_fixture.control['prep'] = {'status':'partial'}
    first, _ = await batch_fixture.run()
    assert first['status'] == 'partial' and first['stop_reason'] == 'review_checkpoint', first
    second, _ = await batch_fixture.run([{'id':'outline'},{'id':'generate'}])
    assert second['status'] == 'ok' and second['stop_reason'] == 'completed', second
    assert batch_fixture.calls['outline'] == 1 and batch_fixture.calls['generate'] == 1


async def test_can_pause_false_does_not_leave_card_or_resume(batch_fixture):
    batch_fixture.specs[1]['hitl'] = {'can_pause':False}
    batch_fixture.save()
    result, frames = await batch_fixture.run([{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}])
    assert result['status'] == 'failed' and result['stop_reason'] == 'step_failed', result
    assert result['stopped_result']['error'] == '该段不支持人工中断'
    assert not batch_fixture.calls['generate'] and await batch_fixture.state() is None
    assert not any(frame.payload and frame.payload.get('event_type') == 'chat.ask_user_question' for frame in frames)


async def test_cancellation_stops_later_and_does_not_record_cancelled_segment(batch_fixture):
    import asyncio
    batch_fixture.control['outline'] = {'error': asyncio.CancelledError()}
    with pytest.raises(asyncio.CancelledError):
        await batch_fixture.run()
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    book = await batch_fixture.state(SkillRunLedger.KEY)
    task = next(iter(book['tasks'].values()))
    assert task['revision'] == 1 and 'outline' not in task['latest_runs']
    assert not batch_fixture.calls['generate']


async def test_busy_batch_does_not_clear_active_owner(batch_fixture):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait():
        entered.set()
        await release.wait()
    batch_fixture.control['prep'] = {'wait':wait}
    running = asyncio.create_task(batch_fixture.run())
    await entered.wait()
    try:
        busy, _ = await batch_fixture.run()
        assert busy['status'] == 'busy', busy
        single, _ = await batch_fixture.segment.run()
        assert single['status'] == 'busy', single
    finally:
        release.set()
    completed, _ = await running
    assert completed['status'] == 'ok' and batch_fixture.calls['prep'] == 1, completed


async def test_force_prefix_save_failure_survives_pause_and_resume_without_repeat(batch_fixture, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger
    await batch_fixture.run([{'id':'prep'}])
    original = SkillRunLedger.save
    async def save(ledger, session):
        if ledger.data['revision'] == 2:
            batch_fixture.storage.cp.mode = 'before'
            try:
                return await original(ledger, session)
            finally:
                batch_fixture.storage.cp.mode = ''
        return await original(ledger, session)
    monkeypatch.setattr(SkillRunLedger, 'save', save)
    items = [{'id':'prep','force':True},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    saved = checkpoint['segment']['batch']
    assert saved['ledger']['revision'] == 2 and saved['ledger_persisted'] is False
    assert saved['ledger']['facts_provenance']['prep_fact']['verified'] is True
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert result['status'] == 'ok' and result['ledger']['revision'] == 4, result
    assert batch_fixture.calls['prep'] == 2 and batch_fixture.calls['leaf:outline'] == 1
    assert any(w['code'] == 'ledger_save_failed' for w in result['warnings'])


async def test_new_sdk_checkpointer_and_session_reload_original_cursor(batch_fixture, monkeypatch, tmp_path):
    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer
    from openjiuwen.core.foundation.store.kv import ShelveStore
    path = str(tmp_path / 'sdk-state' / 'checkpoint')
    monkeypatch.setattr(CheckpointerFactory, '_default_checkpointer', PersistenceCheckpointer(ShelveStore(path)))
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    monkeypatch.setattr(CheckpointerFactory, '_default_checkpointer', PersistenceCheckpointer(ShelveStore(path)))
    checkpoint = await batch_fixture.state()
    assert checkpoint['segment']['batch']['cursor'] == 1
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'})
    assert result['status'] == 'ok' and result['ledger']['revision'] == 3, result
    assert batch_fixture.calls['prep'] == 1 and await batch_fixture.state() is None


@pytest.mark.parametrize('mutation', ['query','force','cursor','missing'])
async def test_original_call_or_control_changes_fail_without_recovery(batch_fixture, mutation):
    from jiuwenswarm.server.runtime.skill_turbo.segment_context import data_fingerprint
    items = [{'id':'prep'},{'id':'outline','params':{'pause':True}},{'id':'generate'}]
    with pytest.raises(AbortError):
        await batch_fixture.run(items)
    checkpoint = await batch_fixture.state()
    changes = {}
    if mutation == 'query':
        changes['query'] = 'rewritten while paused'
    elif mutation == 'force':
        items[1]['force'] = True
    else:
        batch = checkpoint['segment']['batch']
        if mutation == 'cursor':
            batch['cursor'] = 99
        else:
            del batch['results']
        batch.pop('fingerprint')
        batch['fingerprint'] = data_fingerprint(batch)
        control = checkpoint['segment']
        control.pop('control_fingerprint')
        control['control_fingerprint'] = data_fingerprint(control)
        async with isolated_skill_state(batch_fixture.storage.session(), write=True) as state:
            state.replace(SKILL_TURBO_RESUME_CTX_KEY, checkpoint)
    before = batch_fixture.calls.copy()
    result, _ = await batch_fixture.run(items, resume_ctx=checkpoint, user_input={'Review outline?':'yes'}, **changes)
    assert result['status'] == 'failed' and batch_fixture.calls == before, result
    assert await batch_fixture.state() is None


async def test_batch_switch_and_cost_warning(batch_fixture):
    disabled, _ = await batch_fixture.run(config={'batch_enabled':False})
    assert disabled['status'] == 'blocked' and not batch_fixture.calls
    good, _ = await batch_fixture.run(config={'max_batch_p50_seconds':0})
    assert good['status'] == 'ok' and any(w['code'] == 'batch_cost_budget_exceeded' for w in good['warnings']), good

async def test_identical_inner_tool_arguments_in_different_steps_get_distinct_call_ids(batch_fixture):
    for spec in batch_fixture.specs:
        path = batch_fixture.segment.code.parent / (spec['entry']['module'] + '.py')
        source = path.read_text(encoding='utf-8').replace('        await self.execute_subplan',
            "        await self.call_tool('batch_probe', stage='common', inputs={})\n        await self.execute_subplan", 1)
        path.write_text(source, encoding='utf-8')
    result, frames = await batch_fixture.run()
    assert result['status'] == 'ok', result
    calls = [frame.payload for frame in frames if frame.payload and frame.payload.get('event_type') == 'chat.tool_call']
    common = [call['tool_call']['tool_call_id'] for call in calls if call['tool_call']['arguments'].get('stage') == 'common']
    assert len(common) == 3 and len(set(common)) == 3, calls
