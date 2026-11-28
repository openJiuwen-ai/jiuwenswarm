"""实际ToolCard/invoke模式、外层事件与资源回收。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from _segment_fixture import segment, storage, isolated
from test_batch_execution import batch_fixture
from jiuwenswarm.server.runtime.skill_turbo import agent, skill_turbo_tools as tools
from jiuwenswarm.server.runtime.skill_turbo.context_binding import bind_skill_turbo_context, reset_skill_turbo_context
from jiuwenswarm.agents.harness.common.tool_result_schema import normalize_tool_result, render_tool_result_for_llm


@pytest.fixture
def invoke_segment(segment, monkeypatch):
    parent = segment.storage.session(turbo=False)
    write = AsyncMock()
    monkeypatch.setattr(parent, 'write_stream', write)
    adapter = SimpleNamespace(_instance=SimpleNamespace(card=parent._card),
                              build_skill_turbo_config=lambda: {'enabled': True},
                              restore_skill_turbo_model_from_resume_ctx=Mock())
    instances = []

    def new(config):
        value = segment.new()
        value._env.config.update(config)
        instances.append(value)
        return value

    monkeypatch.setattr(agent, 'SkillTurbo', new)

    async def invoke(arguments=None, *, outer_todo=False, answers=None):
        ctx = SimpleNamespace()
        bind_skill_turbo_context(ctx, adapter=adapter, parent_session=parent,
            request_metadata={**segment.inputs['metadata'], 'session_id': parent.get_session_id(), 'request_id': 'req'},
            outer_todo_active=outer_todo, resume_answers=answers)
        try:
            return await tools.skill_turbo.invoke(arguments or {
                'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'auto_batch': False})
        finally:
            reset_skill_turbo_context(ctx)

    return SimpleNamespace(invoke=invoke, writes=write, instances=instances, adapter=adapter)


async def test_actual_tool_schema_and_intermediate_result(segment, invoke_segment):
    schema = tools.skill_turbo.card.input_params
    assert set(schema['properties']) == {'query', 'fresh_start', 'skill', 'accelerator', 'params', 'accelerators', 'auto_batch'}
    assert tools.skill_turbo.card.properties['resilience'] == {'timeout_s': None}
    result = await invoke_segment.invoke()
    assert result['status'] == 'ok'
    assert '任务已结束' not in str(result)
    assert segment.calls == {'first': 1, 'root': 1}
    assert invoke_segment.writes.await_count


async def test_actual_sdk_accepts_auto_batch_and_uses_batch_facade(segment, invoke_segment):
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerator': 'prep', 'auto_batch': True})
    assert result['mode'] == 'batch' and result['status'] == 'ok', result
    assert result['planned_steps'] == ['prep']
    assert segment.calls == {'first': 1, 'root': 1}


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': True}])
async def test_sdk_defaults_ordinary_start_to_declared_batch(segment, invoke_segment, mode):
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerator': 'prep', **mode})
    assert result['mode'] == 'batch' and result['status'] == 'ok', result
    assert result['planned_steps'] == ['prep']
    assert segment.calls == {'first': 1, 'root': 1}


async def test_sdk_auto_batch_schema_is_nullable_and_defaults_to_none():
    schema = tools.skill_turbo.card.input_params['properties']['auto_batch']
    assert schema['default'] is None
    assert schema['type'] == 'boolean' and schema['nullable'] is True


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': True}, {'auto_batch': False}])
async def test_sdk_start_mode_controls_real_chain(batch_fixture, invoke_segment, monkeypatch, mode):
    batch_fixture.specs[0]['batch'] = {'auto_next': 'outline'}
    batch_fixture.save()
    monkeypatch.setattr(agent, 'SkillTurbo', lambda config: batch_fixture.new())
    invoke_segment.adapter.build_skill_turbo_config = lambda: {'enabled': True}
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerator': 'prep', **mode})
    assert result['status'] == 'ok', result
    assert batch_fixture.calls['prep'] == 1
    assert batch_fixture.calls['outline'] == (0 if mode.get('auto_batch') is False else 1)
    assert batch_fixture.calls['generate'] == 0
    assert result['ledger']['revision'] == (1 if mode.get('auto_batch') is False else 2)
    assert result['delivery_confirmed'] is False


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': False}])
async def test_sdk_explicit_list_is_not_expanded(batch_fixture, invoke_segment, monkeypatch, mode):
    batch_fixture.specs[0]['batch'] = {'auto_next': 'outline'}
    batch_fixture.save()
    monkeypatch.setattr(agent, 'SkillTurbo', lambda config: batch_fixture.new())
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerators': [{'id': 'prep'}], **mode})
    assert result['status'] == 'ok' and result['mode'] == 'batch', result
    assert batch_fixture.calls['prep'] == 1 and batch_fixture.calls['outline'] == 0
    assert result['ledger']['revision'] == 1


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}])
async def test_sdk_default_batch_resumes_original_call(batch_fixture, invoke_segment, monkeypatch, mode):
    batch_fixture.specs[0]['batch'] = {'auto_next': 'outline'}
    batch_fixture.save()
    monkeypatch.setattr(agent, 'SkillTurbo', lambda config: batch_fixture.new())
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep',
                 'params': {'pause': True}, **mode}
    paused = await invoke_segment.invoke(arguments)
    assert '__skill_turbo_hitl__' in paused
    result = await invoke_segment.invoke(arguments, answers=[{'question': 'Review prep?', 'selected_options': ['yes']}])
    assert result['status'] == 'ok' and result['planned_steps'] == ['prep', 'outline'], result
    assert batch_fixture.calls['leaf:prep'] == 1 and batch_fixture.calls['prep'] == 1
    assert batch_fixture.calls['outline'] == 1 and not batch_fixture.calls['generate']


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': False}])
async def test_sdk_batch_disabled_keeps_single_stage(segment, invoke_segment, mode):
    invoke_segment.adapter.build_skill_turbo_config = lambda: {'enabled': True, 'skill_turbo': {'enabled': True, 'batch_enabled': False}}
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerator': 'prep', **mode})
    assert result['mode'] == 'segment' and result['status'] == 'ok', result
    assert segment.calls == {'first': 1, 'root': 1}


async def test_sdk_explicit_auto_batch_still_blocks_when_disabled(segment, invoke_segment):
    invoke_segment.adapter.build_skill_turbo_config = lambda: {'enabled': True, 'skill_turbo': {'enabled': True, 'batch_enabled': False}}
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
        'accelerator': 'prep', 'auto_batch': True})
    assert result['status'] == 'blocked' and result['error'] == 'batch_disabled', result
    assert not segment.calls


@pytest.mark.parametrize('arguments', [
    {'query': 'original', 'auto_batch': True},
    {'query': 'original', 'skill': 'demo-craft', 'auto_batch': True},
    {'query': 'original', 'skill': 'demo-craft', 'accelerator': '__full__', 'auto_batch': True},
    {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'auto_batch': True, 'accelerators': [{'id': 'prep'}]},
])
async def test_sdk_rejects_ambiguous_auto_modes_before_business(segment, invoke_segment, arguments):
    result = await invoke_segment.invoke(arguments)
    assert result['status'] == 'invalid_args' and not segment.calls, result


@pytest.mark.parametrize('extra', [{'force': True}])
async def test_sdk_refuses_unimplemented_top_level_arguments(segment, invoke_segment, extra):
    with pytest.raises(Exception):
        await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', **extra})
    assert not segment.calls


@pytest.mark.parametrize('arguments', [
    {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'accelerators': [{'id': 'prep'}]},
    {'query': 'original', 'accelerator': 'prep'},
    {'query': 'original', 'params': {}},
    {'query': 'original', 'skill': 'demo-craft', 'accelerator': '__full__', 'params': {}},
    {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'params': {'force': True}},
])
async def test_invalid_modes_have_no_business_execution(segment, invoke_segment, arguments):
    result = await invoke_segment.invoke(arguments)
    assert result['status'] == 'invalid_args'
    assert not segment.calls


@pytest.mark.parametrize('extra,code,field', [
    ({'params': {}}, 'invalid_execution_mode', 'params'),
    ({'params': {'pages': 5}}, 'invalid_execution_mode', 'params'),
    ({'auto_batch': True}, 'invalid_auto_batch_mode', 'auto_batch'),
    ({'accelerators': [{'id': 'prep'}]}, 'invalid_batch_execution_mode', 'accelerators'),
])
async def test_full_conflicts_explain_correction_in_model_view(segment, invoke_segment, extra, code, field):
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft',
                                         'accelerator': '__full__', **extra})
    assert result['status'] == 'invalid_args' and result['success'] is False
    assert result['error_code'] == code
    view = render_tool_result_for_llm(normalize_tool_result(result))
    assert '__full__' in view and field in view and 'query' in view
    assert 'tool failed without details' not in view
    assert not segment.calls and not invoke_segment.instances


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': False}])
@pytest.mark.parametrize('fresh_start', [False, True])
async def test_skill_only_lists_exact_candidates_without_model(segment, invoke_segment, mode, fresh_start):
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft', 'fresh_start': fresh_start, **mode})
    assert result['status'] == 'needs_accelerator'
    assert result['success'] is False and result['delivery_confirmed'] is False
    assert result['candidates'] == [{'id': 'prep', 'summary': '准备'}]
    view = render_tool_result_for_llm(normalize_tool_result(result))
    assert '尚未执行' in view and '__full__' in view and 'prep' in view and '准备' in view
    assert 'tool failed without details' not in view
    assert not segment.calls


async def test_segment_ignores_fresh_start_and_ppt_region_keywords(segment, invoke_segment):
    first = await invoke_segment.invoke()
    second = await invoke_segment.invoke({'query': '导出已有 PPT选区 页面', 'skill': 'demo-craft',
                                          'accelerator': 'prep', 'auto_batch': False, 'fresh_start': True})
    assert second['already_done'] is True
    assert first['ledger']['task_key'] == second['ledger']['task_key']
    assert segment.calls == {'first': 1, 'root': 1}


@pytest.mark.parametrize('outer_todo', [False, True])
async def test_all_segments_suppress_inner_tasks(segment, invoke_segment, outer_todo):
    result = await invoke_segment.invoke(outer_todo=outer_todo)
    assert result['success'] is True
    outputs = [call.args[0] for call in invoke_segment.writes.await_args_list]
    assert outputs
    assert all(output.type not in {'task.start', 'task.complete', 'task.update'} for output in outputs)
    assert all(not str(getattr(output, 'payload', {}).get('task_id', '')).startswith('task_') for output in outputs)


@pytest.mark.parametrize('outer_todo', [None, False, True])
@pytest.mark.parametrize('selection', [
    {'accelerator': 'prep', 'auto_batch': False},
    {'accelerator': 'prep'},
    {'accelerators': [{'id': 'prep'}]},
])
async def test_stage_rows_hidden_for_single_auto_and_explicit_batch(
    segment, invoke_segment, outer_todo, selection,
):
    segment.control['root_output'] = {'message': '内部工作内容保留'}
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft', **selection},
                                         outer_todo=outer_todo)
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}
    outputs = [call.args[0] for call in invoke_segment.writes.await_args_list]
    assert outputs
    assert all(output.type not in {'task.start', 'task.complete', 'task.update'} for output in outputs)
    assert all(not str(output.payload.get('task_id', '')).startswith('task_') for output in outputs)
    assert any('内部工作内容保留' in output.payload.get('content', '') for output in outputs)


async def test_tool_answers_without_checkpoint_do_not_fresh_execute(segment, invoke_segment):
    result = await invoke_segment.invoke(answers=[{}])
    assert result['error'] == 'resume_context_unavailable'
    assert not segment.calls


async def test_tool_pause_marker_and_resume_complete(segment, invoke_segment):
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'auto_batch': False, 'params': {'pause': True}}
    paused = await invoke_segment.invoke(arguments)
    assert '__skill_turbo_hitl__' in paused
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    assert checkpoint['task_states']
    result = await invoke_segment.invoke(arguments, answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}
    outputs = [call.args[0] for call in invoke_segment.writes.await_args_list]
    assert outputs
    assert all(output.type not in {'task.start', 'task.complete', 'task.update'} for output in outputs)
    assert all(not str(output.payload.get('task_id', '')).startswith('task_') for output in outputs)


@pytest.mark.parametrize('mode', [{}, {'auto_batch': None}, {'auto_batch': False}])
async def test_explicit_full_builds_selected_root_without_planner(segment, invoke_segment, mode):
    segment.code.write_text('''from skill_turbo_runtime import PlanNode
class Leaf(PlanNode):
    async def _execute(self, inputs):
        return {'status': 'ok', '__artifact__': {'info': {'full_executed': True}}}
class Root(PlanNode):
    async def _execute(self, inputs):
        return await self.execute_subplan(self.sub_plans[0], inputs)
root = Root(plan_name='root', instruction='full', sub_plans=[Leaf(plan_name='full_stage', instruction='full stage')])
''', encoding='utf-8')
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft', 'accelerator': '__full__', **mode})
    value = invoke_segment.instances[0]
    assert value.artifact_holder['full_stage']['info']['full_executed'] is True
    assert '任务已结束' in str(result)
    value._planner.plan.assert_not_awaited()


async def test_unrecoverable_model_identity_fails_without_fresh_run(segment, invoke_segment):
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'auto_batch': False, 'params': {'pause': True}}
    await invoke_segment.invoke(arguments)
    invoke_segment.adapter.restore_skill_turbo_model_from_resume_ctx.side_effect = ValueError('model identity invalid')
    result = await invoke_segment.invoke(arguments, answers=[{}])
    assert result['status'] == 'failed'
    assert 'resume_model_unavailable' in result['error']
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None
