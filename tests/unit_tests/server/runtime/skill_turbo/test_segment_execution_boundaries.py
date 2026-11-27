"""用真实节点、SDK状态和磁盘副作用验证段执行的边界。"""

import asyncio
import json
import os
import py_compile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from _segment_fixture import segment, storage, isolated, SOURCE
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import DeepAgentFallbackHandler
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.resume_context import ResumeContextManager
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger


@pytest.mark.parametrize('write_file,contract,expected', [(True, True, 'ok'), (False, True, 'partial'), (True, False, 'failed')])
async def test_real_fallback_repair_is_checked_against_files(segment, write_file, contract, expected):
    segment.control.update(exception=RuntimeError('root failed'), child_status='failed')
    value = segment.new()

    async def spawn(query, **kwargs):
        if write_file:
            ledger = await SkillRunLedger.current(segment.storage.session(), skill='demo-craft',
                                                  workspace_base=str(segment.workspace))
            (Path(ledger.data['run_dir']) / 'prep.json').write_text('{}', encoding='utf-8')
        return json.dumps({'success': contract, 'result': {'facts': {'prep_fact': 'repaired'}}})

    boundary = AsyncMock(side_effect=spawn)
    value._env._fallback_handler = DeepAgentFallbackHandler(SimpleNamespace(spawn_fallback=boundary))
    result, events = await segment.run(instance=value)
    assert result['status'] == expected
    assert result['fallback_count'] == 1
    assert boundary.await_count == 1
    assert not any(e.payload and e.payload.get('event_type') == 'chat.error' for e in events)
    if contract:
        assert result['facts']['prep_fact'] == 'repaired'


@pytest.mark.parametrize('source', ['def broken(:', "raise RuntimeError('import unavailable')\n"])
async def test_invalid_or_unloadable_code_never_executes_or_records(segment, source):
    segment.code.write_text(source, encoding='utf-8')
    result, _ = await segment.run()
    assert result['status'] == 'failed' and result['success'] is False
    assert not segment.calls
    ledger = await SkillRunLedger.current(segment.storage.session(), skill='demo-craft',
                                          workspace_base=str(segment.workspace))
    assert ledger is None or ledger.data['runs'] == []


@pytest.mark.parametrize('send_status,confirmed', [('sent', True), ('failed', False), ('pending', False)])
async def test_delivery_uses_actual_child_owner_independent_of_business_status(segment, send_status, confirmed):
    segment.control['child_output'] = {'__artifact__': {
        'info': {'send_file_status': send_status}, 'files': [], 'delivery_summary': '真实发送摘要'}}
    value = segment.new()
    result, events = await segment.run(instance=value)
    assert result['status'] == 'ok'
    assert result['delivery_confirmed'] is confirmed
    assert value.artifact_holder['first']['info']['send_file_status'] == send_status
    if confirmed:
        assert result['delivery_owner'] == 'first'
        assert result['delivery_summary'] == '真实发送摘要'
    assert '__artifact__' not in json.dumps([e.payload for e in events])


async def test_nested_stream_artifact_keeps_its_real_owner(segment):
    source = SOURCE.replace("await self.execute_subplan(child, inputs)",
                            "async for chunk in self.execute_subplan_stream(child, inputs):\n                yield chunk")
    source = source.replace('async def _execute(self, inputs):\n        for child',
                            'async def _execute_stream(self, inputs):\n        for child')
    source = source.replace("return await self.call_tool('segment_probe', stage='root', inputs=inputs)",
                            "yield await self.call_tool('segment_probe', stage='root', inputs=inputs)")
    source = source.replace('class Root(PlanNode):', 'class Root(PlanNode):\n    async def _execute(self, inputs):\n        return {}\n')
    segment.code.write_text(source, encoding='utf-8')
    segment.control['child_output'] = {'__artifact__': {'info': {'send_file_status': 'sent'}},
                                       '__segment_handoff__': {'status': 'failed'}}
    value = segment.new()
    result, events = await segment.run(instance=value)
    assert result['status'] == 'ok', result
    assert result['delivery_owner'] == 'first'
    assert '__artifact__' not in json.dumps([e.payload for e in events])
    assert '__segment_handoff__' not in json.dumps([e.payload for e in events])


async def test_consecutive_cards_replace_checkpoint_and_skip_completed_stage(segment):
    params = {'pause': True, 'pause_twice': True}
    manager = ResumeContextManager(segment.storage.session())
    with pytest.raises(AbortError):
        await segment.run(params=params)
    first = await manager.load()
    with pytest.raises(AbortError):
        await segment.run(params=params, resume_ctx=first, request_id='different-request',
                          answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    second = await manager.load()
    assert second['pending_tool_call_id'] != first['pending_tool_call_id'], second
    assert second['segment']['task_key'] == first['segment']['task_key']
    result, _ = await segment.run(params=params, resume_ctx=second,
                                  answers=[{'question': '再次确认？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok', result
    assert segment.calls == {'first': 1, 'root': 1}
    assert await manager.load() is None


async def test_resume_resource_failure_before_nodes_keeps_verified_checkpoint(segment, monkeypatch):
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    manager = ResumeContextManager(segment.storage.session())
    checkpoint = await manager.load()
    value = segment.new()
    monkeypatch.setattr(value._env, 'register_tools', AsyncMock(side_effect=OSError('temporary outage')))
    result, _ = await segment.run(instance=value, params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
    assert result['checkpoint_reusable'] is True
    stored = await manager.load()
    assert not stored.get('resume_in_flight')
    assert stored['inputs'] == checkpoint['inputs']
    result, _ = await segment.run(params={'pause': True}, resume_ctx=stored,
                                  answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok', result
    assert segment.calls == {'first': 1, 'root': 1}


async def test_resume_result_save_and_cleanup_failure_keep_real_result_without_reexecution(segment):
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()

    async def outage():
        segment.storage.cp.mode = 'before'

    segment.control['wait'] = outage
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint,
                                  answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    segment.storage.cp.mode = ''
    assert result['status'] == 'ok' and result['ledger']['persisted'] is False
    assert {'ledger_save_failed', 'resume_cleanup_failed'}.issubset({w['code'] for w in result['warnings']})
    pending = await ResumeContextManager(segment.storage.session()).load()
    assert pending['resume_in_flight'] is True
    rejected, _ = await segment.run(params={'pause': True}, resume_ctx=pending, answers=[{}])
    assert rejected['status'] == 'busy'
    assert segment.calls == {'first': 1, 'root': 1}


async def test_pending_resume_and_new_call_cannot_execute_concurrently(segment):
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()

    segment.control['wait'] = wait
    task = asyncio.create_task(segment.run(params={'pause': True}, resume_ctx=checkpoint,
        answers=[{'question': '确认继续？', 'selected_options': ['继续']}]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        for arguments in ({}, {'params': {'pause': True}, 'resume_ctx': checkpoint, 'answers': [{}]}):
            result, _ = await segment.run(**arguments)
            assert result['status'] == 'busy'
    finally:
        release.set()
        result, _ = await task
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}


async def test_duplicate_answer_before_inflight_mark_cannot_clear_owner_checkpoint(segment, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo import segment_execution

    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    entered, release = asyncio.Event(), asyncio.Event()
    validate = segment_execution.validate_segment_replay_inputs

    async def waiting_validate(*args):
        result = await validate(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(segment_execution, 'validate_segment_replay_inputs', waiting_validate)
    task = asyncio.create_task(segment.run(params={'pause': True}, resume_ctx=checkpoint,
        answers=[{'question': '确认继续？', 'selected_options': ['继续']}]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        rejected, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
        assert rejected['status'] == 'busy'
        assert await ResumeContextManager(segment.storage.session()).load() == checkpoint
    finally:
        release.set()
        result, _ = await task
    assert result['status'] == 'ok'
    assert segment.calls == {'first': 1, 'root': 1}


async def test_independent_sessions_can_use_identical_parameters(segment):
    second_workspace = segment.workspace / 'second-session'
    second_workspace.mkdir()
    results = await asyncio.gather(segment.run(sid='first-session'), segment.run(sid='second-session',
        metadata={**segment.inputs['metadata'], 'output_dir': str(second_workspace)}))
    assert all(result['status'] == 'ok' for result, _ in results), results
    assert results[0][0]['ledger']['task_key'] != results[1][0]['ledger']['task_key']
    assert segment.calls == {'first': 2, 'root': 2}


async def test_working_file_changes_do_not_change_execution_definition(segment):
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    work = Path(checkpoint['inputs']['run_dir']) / 'user-outline.md'
    work.write_text('用户修改的大纲', encoding='utf-8')
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint,
                                  answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok'
    assert work.read_text(encoding='utf-8') == '用户修改的大纲'
    assert segment.calls == {'first': 1, 'root': 1}


async def test_can_pause_false_clears_checkpoint_written_by_lower_owner(segment):
    segment.raw['accelerators'][0]['hitl'] = {'can_pause': False}
    (segment.skill / 'turbo/accelerators.json').write_text(json.dumps(segment.raw), encoding='utf-8')
    value = segment.new()

    async def lower_interrupt():
        context = value._executor.segment_context
        await ResumeContextManager(segment.storage.session()).save(
            plan_code=context.spec.plan_code, inputs=value._executor._execution_inputs,
            pending_tool_call_id='lower-ask-user', segment=context.checkpoint(value._executor._execution_inputs,
                task_states=[], pending_tool_call_id='lower-ask-user'))
        raise AbortError('lower interrupt')

    segment.control['wait'] = lower_interrupt
    result, events = await segment.run(instance=value)
    assert result['error'] == '该段不支持人工中断'
    assert not result['success']
    assert await ResumeContextManager(segment.storage.session()).load() is None
    assert segment.calls == {'first': 1, 'root': 1}
    assert not any(e.payload and e.payload.get('event_type') in {'__interaction__', 'chat.invocation_paused'} for e in events)


async def test_empty_subtree_can_pause_and_resume(segment):
    segment.code.write_text(SOURCE.replace("[Leaf(plan_name='first', instruction='first stage')]", '[]'), encoding='utf-8')
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    assert checkpoint['task_states'] == []
    result, _ = await segment.run(params={'pause': True}, resume_ctx=checkpoint,
                                  answers=[{'question': '确认继续？', 'selected_options': ['继续']}])
    assert result['status'] == 'ok'
    assert segment.calls == {'root': 1}


async def test_no_pause_filters_lower_stream_card_before_forwarding(segment):
    segment.raw['accelerators'][0]['hitl'] = {'can_pause': False}
    (segment.skill / 'turbo/accelerators.json').write_text(json.dumps(segment.raw), encoding='utf-8')
    segment.code.write_text('''from skill_turbo_runtime import PlanNode
class Root(PlanNode):
    async def _execute(self, inputs):
        return {}
    async def _execute_stream(self, inputs):
        yield {'event_type': '__interaction__', 'content': 'lower card'}
        yield {'event_type': 'chat.ask_user_question', 'content': 'lower card'}
        yield {'event_type': 'invocation_paused'}
        await self.call_tool('ask_user', questions=[{'question': '继续？', 'options': ['继续']}])
root = Root(plan_name='root', instruction='lower interrupt', sub_plans=[])
''', encoding='utf-8')
    result, events = await segment.run()
    assert result['error'] == '该段不支持人工中断'
    assert not any(e.payload and e.payload.get('event_type') in {
        '__interaction__', 'chat.ask_user_question', 'invocation_paused'} for e in events)
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_root_result_without_handoff_still_uses_real_postconditions(segment):
    segment.control['handoff'] = False
    result, _ = await segment.run()
    assert result['status'] == 'ok' and result['verified'] is True


async def test_pause_requires_confirmed_artifact_namespace_before_card(segment, monkeypatch):
    value = segment.new()
    boundary = value._env.get_tool_function('segment_probe')

    async def drop_after_child(stage, inputs):
        result = await boundary(stage, inputs)
        if stage == 'first':
            segment.storage.cp.mode = 'drop'
        return result

    monkeypatch.setattr(value._env, 'get_tool_function', lambda name: drop_after_child if name == 'segment_probe' else None)
    result, events = await segment.run(instance=value, params={'pause': True})
    segment.storage.cp.mode = ''
    assert result['status'] == 'failed'
    assert 'segment_checkpoint_save_failed' in result['error']
    assert result['fallback_count'] == 0
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_failed_root_keeps_error_and_repair_scope(segment):
    segment.control['root_output'] = {'status': 'failed', 'error': '缺少业务数据'}
    result, _ = await segment.run()
    assert result['status'] == 'failed'
    assert result['error'] == '缺少业务数据'
    assert result['remaining_steps'] == ['prep']


async def test_source_unreadable_after_registration_never_keeps_reusable_checkpoint(segment, monkeypatch):
    with pytest.raises(AbortError):
        await segment.run(params={'pause': True})
    checkpoint = await ResumeContextManager(segment.storage.session()).load()
    value = segment.new()

    async def register(*args, **kwargs):
        def unavailable(*args):
            raise PermissionError('source unavailable')
        monkeypatch.setattr('jiuwenswarm.server.runtime.skill_turbo.segment_context.execution_snapshot', unavailable)

    monkeypatch.setattr(value._env, 'register_tools', register)
    result, _ = await segment.run(instance=value, params={'pause': True}, resume_ctx=checkpoint, answers=[{}])
    assert result['status'] == 'failed'
    assert not result.get('checkpoint_reusable')
    assert segment.calls == {'first': 1}
    assert await ResumeContextManager(segment.storage.session()).load() is None


async def test_same_stat_source_edit_does_not_execute_cached_module_or_pyc(segment):
    stat = segment.code.stat()
    py_compile.compile(str(segment.code), doraise=True)
    segment.control['artifact'] = False
    first, _ = await segment.run()
    assert first['status'] == 'partial'
    segment.code.write_text(SOURCE.replace("plan_name='first'", "plan_name='other'"), encoding='utf-8')
    os.utime(segment.code, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    segment.control['artifact'] = True
    second, _ = await segment.run()
    assert second['status'] == 'ok'
    assert segment.calls == {'first': 1, 'other': 1, 'root': 2}


@pytest.mark.parametrize('terminal', ['ok', 'failed', 'pause', 'cancel'])
async def test_real_registered_tool_is_reclaimed_on_each_exit(segment, monkeypatch, terminal):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.foundation.tool import tool
    from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment
    from jiuwenswarm.server.runtime.skill_turbo import tools_loader

    @tool(name='send_file_to_user', description='临时注册边界')
    async def send_file_to_user(path: str) -> str:
        return path

    value = segment.new()
    value._env.config['tool_info_export_path'] = str(segment.workspace / 'test-tools.json')
    monkeypatch.setattr(value._env, 'register_tools', SkillTurboEnvironment.register_tools.__get__(value._env))
    monkeypatch.setattr(tools_loader, 'load_all', AsyncMock(return_value=[]))
    monkeypatch.setattr(tools_loader, 'load_send_file_tools', lambda ctx: [send_file_to_user])
    segment.control['status'] = terminal if terminal in {'ok', 'failed'} else 'ok'
    if terminal == 'cancel':
        segment.control['exception'] = asyncio.CancelledError()
    try:
        if terminal in {'pause', 'cancel'}:
            with pytest.raises(AbortError if terminal == 'pause' else asyncio.CancelledError):
                await segment.run(instance=value, params={'pause': terminal == 'pause'})
        else:
            result, _ = await segment.run(instance=value)
            assert result['status'] == terminal
        assert value._env.tools['send_file_to_user'].id == send_file_to_user.card.id
        assert Runner.resource_mgr.get_tool(send_file_to_user.card.id) is None
        assert not value._env._self_registered_tool_ids
    finally:
        value.release_registered_tools()


