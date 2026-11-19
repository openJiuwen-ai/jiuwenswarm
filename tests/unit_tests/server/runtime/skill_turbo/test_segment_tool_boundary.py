"""实际ToolCard/invoke模式、外层事件与资源回收。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from _segment_fixture import segment, storage, isolated
from jiuwenswarm.server.runtime.skill_turbo import agent, skill_turbo_tools as tools
from jiuwenswarm.server.runtime.skill_turbo.context_binding import bind_skill_turbo_context, reset_skill_turbo_context


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
                'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep'})
        finally:
            reset_skill_turbo_context(ctx)

    return SimpleNamespace(invoke=invoke, writes=write, instances=instances, adapter=adapter)


async def test_actual_tool_schema_and_intermediate_result(segment, invoke_segment):
    schema = tools.skill_turbo.card.input_params
    assert set(schema['properties']) == {'query', 'fresh_start', 'skill', 'accelerator', 'params', 'accelerators'}
    assert tools.skill_turbo.card.properties['resilience'] == {'timeout_s': None}
    result = await invoke_segment.invoke()
    assert result['status'] == 'ok'
    assert '任务已结束' not in str(result)
    assert segment.calls == {'first': 1, 'root': 1}
    assert invoke_segment.writes.await_count


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


async def test_skill_only_lists_exact_candidates_without_model(segment, invoke_segment):
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft'})
    assert result['status'] == 'needs_accelerator'
    assert result['candidates'] == [{'id': 'prep', 'summary': '准备'}]
    assert not segment.calls


async def test_segment_ignores_fresh_start_and_ppt_region_keywords(segment, invoke_segment):
    first = await invoke_segment.invoke()
    second = await invoke_segment.invoke({'query': '导出已有 PPT选区 页面', 'skill': 'demo-craft',
                                          'accelerator': 'prep', 'fresh_start': True})
    assert second['already_done'] is True
    assert first['ledger']['task_key'] == second['ledger']['task_key']
    assert segment.calls == {'first': 1, 'root': 1}


async def test_outer_todo_suppresses_inner_tasks(segment, invoke_segment):
    result = await invoke_segment.invoke(outer_todo=True)
    assert result['success'] is True
    outputs = [call.args[0] for call in invoke_segment.writes.await_args_list]
    assert all(output.type not in {'task.start', 'task.complete', 'task.update'} for output in outputs)
    assert all(not str(getattr(output, 'payload', {}).get('task_id', '')).startswith('task_') for output in outputs)


async def test_tool_answers_without_checkpoint_do_not_fresh_execute(segment, invoke_segment):
    result = await invoke_segment.invoke(answers=[{}])
    assert result['error'] == 'resume_context_unavailable'
    assert not segment.calls


async def test_tool_pause_marker_and_resume_complete(segment, invoke_segment):
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'params': {'pause': True}}
    paused = await invoke_segment.invoke(arguments)
    assert '__skill_turbo_hitl__' in paused
    result = await invoke_segment.invoke(arguments, answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}


async def test_explicit_full_builds_selected_root_without_planner(segment, invoke_segment):
    segment.code.write_text('''from skill_turbo_runtime import PlanNode
class Leaf(PlanNode):
    async def _execute(self, inputs):
        return {'status': 'ok', '__artifact__': {'info': {'full_executed': True}}}
class Root(PlanNode):
    async def _execute(self, inputs):
        return await self.execute_subplan(self.sub_plans[0], inputs)
root = Root(plan_name='root', instruction='full', sub_plans=[Leaf(plan_name='full_stage', instruction='full stage')])
''', encoding='utf-8')
    result = await invoke_segment.invoke({'query': 'original', 'skill': 'demo-craft', 'accelerator': '__full__'})
    value = invoke_segment.instances[0]
    assert value.artifact_holder['full_stage']['info']['full_executed'] is True
    assert '任务已结束' in str(result)
    value._planner.plan.assert_not_awaited()


async def test_unrecoverable_model_identity_fails_without_fresh_run(segment, invoke_segment):
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager

    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'params': {'pause': True}}
    await invoke_segment.invoke(arguments)
    invoke_segment.adapter.restore_skill_turbo_model_from_resume_ctx.side_effect = ValueError('model identity invalid')
    result = await invoke_segment.invoke(arguments, answers=[{}])
    assert result['status'] == 'failed'
    assert 'resume_model_unavailable' in result['error']
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None
