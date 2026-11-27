"""自动展开复用真实批次执行，验证起始参数隔离及原批恢复。"""

import copy

import pytest
from openjiuwen.core.runner.callback import AbortError

from test_batch_execution import batch_fixture
from _segment_fixture import segment, storage, isolated
from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import validate_accelerator_manifest


def link_chain(fixture):
    fixture.specs[0]['batch'] = {'auto_next': 'outline'}
    fixture.specs[1]['batch'] = {'auto_next': 'generate'}
    fixture.save()


async def test_auto_batch_keeps_start_only_params_out_of_later_schemas(batch_fixture):
    link_chain(batch_fixture)
    batch_fixture.specs[0]['input_schema']['properties']['prep_only'] = {'type': 'string'}
    batch_fixture.save()
    result, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', params={'prep_only': 'first'})
    assert result['status'] == 'ok', result
    assert result['planned_steps'] == ['prep', 'outline', 'generate']
    assert result['ledger']['revision'] == 3
    assert [step['accelerator'] for step in result['results']] == result['planned_steps']
    assert next(inputs for stage, inputs in batch_fixture.inputs_seen if stage == 'prep')['prep_only'] == 'first'
    assert all('prep_only' not in inputs for stage, inputs in batch_fixture.inputs_seen if stage in {'outline', 'generate'})


@pytest.mark.parametrize('target', ['prep', 'missing', '__full__'])
def test_invalid_auto_links_are_rejected(batch_fixture, target):
    batch_fixture.specs[0]['batch'] = {'auto_next': target}
    assert validate_accelerator_manifest(batch_fixture.raw, skill='demo-craft', skill_name='demo')


def test_disabled_or_exclusive_successor_is_rejected(batch_fixture):
    link_chain(batch_fixture)
    batch_fixture.specs[1]['enabled'] = False
    assert validate_accelerator_manifest(batch_fixture.raw, skill='demo-craft', skill_name='demo')
    batch_fixture.specs[1]['enabled'] = True
    batch_fixture.specs[1]['batch']['standalone'] = False
    assert validate_accelerator_manifest(batch_fixture.raw, skill='demo-craft', skill_name='demo')


async def test_auto_batch_reports_limit_and_never_runs_past_review(batch_fixture):
    link_chain(batch_fixture)
    first, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', config={'max_batch_steps': 1})
    assert first['planned_steps'] == ['prep'] and first['next_accelerator'] == 'outline', first
    assert batch_fixture.calls['outline'] == 0
    batch_fixture.specs[1]['hitl'] = {'kind': 'review'}
    batch_fixture.save()
    second, _ = await batch_fixture.run(auto_batch=True, accelerator='prep')
    assert second['planned_steps'] == ['prep', 'outline']
    assert second['stop_reason'] == 'review_checkpoint' and batch_fixture.calls['generate'] == 0


async def test_auto_batch_disabled_or_bad_parameters_execute_nothing(batch_fixture):
    link_chain(batch_fixture)
    disabled, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', config={'batch_enabled': False})
    assert disabled['status'] == 'blocked' and not batch_fixture.calls
    bad, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', params={'value': 'invalid'})
    assert bad['status'] == 'invalid_args' and not batch_fixture.calls
    assert not list(batch_fixture.segment.workspace.glob('skill_runs/*/*'))


@pytest.mark.parametrize('items', [[], [{'id': 'prep'}], 1])
async def test_auto_batch_cannot_also_select_explicit_batch(batch_fixture, items):
    result, _ = await batch_fixture.run(items, auto_batch=True, accelerator='prep')
    assert result['status'] == 'invalid_args' and result['error'] == 'invalid_auto_batch_mode', result
    assert not batch_fixture.calls and not list(batch_fixture.segment.workspace.glob('skill_runs/*/*'))


async def test_auto_batch_failure_does_not_suggest_skipping_to_successor(batch_fixture):
    link_chain(batch_fixture)
    batch_fixture.specs[1]['hitl'] = {'kind': 'review'}
    batch_fixture.save()
    batch_fixture.control['prep'] = {'status': 'failed'}
    result, _ = await batch_fixture.run(auto_batch=True, accelerator='prep')
    assert result['status'] == 'failed' and result['stopped_at'] == 'prep', result
    assert result['next_accelerator'] is None and batch_fixture.calls['outline'] == 0


async def test_auto_batch_clarification_reports_actual_stop_successor(batch_fixture):
    link_chain(batch_fixture)
    batch_fixture.specs[0]['hitl'] = {'kind': 'clarify', 'continue_after_resume': False}
    batch_fixture.save()
    with pytest.raises(AbortError):
        await batch_fixture.run(auto_batch=True, accelerator='prep', params={'pause': True})
    checkpoint = await batch_fixture.state()
    result, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', params={'pause': True},
                                        resume_ctx=checkpoint, user_input={'Review': 'yes'})
    assert result['status'] == 'ok' and result['stop_reason'] == 'clarify_checkpoint', result
    assert result['next_accelerator'] == 'outline' and batch_fixture.calls['outline'] == 0


async def test_auto_batch_resume_reuses_plan_and_does_not_repeat_prefix(batch_fixture):
    link_chain(batch_fixture)
    with pytest.raises(AbortError):
        await batch_fixture.run(auto_batch=True, accelerator='prep', params={'pause': True})
    checkpoint = await batch_fixture.state()
    result, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', params={'pause': True},
                                        resume_ctx=checkpoint, user_input={'Review': 'yes'})
    assert result['status'] == 'ok', result
    assert result['planned_steps'] == ['prep', 'outline', 'generate']
    assert batch_fixture.calls['leaf:prep'] == 1 and batch_fixture.calls['generate'] == 1


@pytest.mark.parametrize('mutation', ['params', 'definition'])
async def test_auto_batch_changed_original_call_or_definition_refuses_resume(batch_fixture, mutation):
    link_chain(batch_fixture)
    params = {'pause': True}
    with pytest.raises(AbortError):
        await batch_fixture.run(auto_batch=True, accelerator='prep', params=params)
    checkpoint = await batch_fixture.state()
    before = copy.deepcopy(batch_fixture.calls)
    if mutation == 'params':
        params['value'] = 2
    else:
        batch_fixture.specs[0]['batch'].pop('auto_next')
        batch_fixture.save()
    result, _ = await batch_fixture.run(auto_batch=True, accelerator='prep', params=params,
                                        resume_ctx=checkpoint, user_input={'Review': 'yes'})
    assert result['status'] == 'failed' and batch_fixture.calls == before, result
