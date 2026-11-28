"""宿主project_dir经过真实请求绑定、工具上下文及SDK段入口的回归。"""

import asyncio
import copy
import json
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from test_batch_execution import batch_fixture, segment, storage, isolated
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
    set_effective_request_workspace_dir, reset_effective_request_workspace_dir,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools as tools
from jiuwenswarm.server.runtime.skill_turbo.context_binding import reset_skill_turbo_context


@pytest.fixture(params=['deep', 'flash'])
def host_request(request, batch_fixture, monkeypatch, tmp_path):
    parent = batch_fixture.storage.session(turbo=False)
    monkeypatch.setattr(parent, 'write_stream', AsyncMock())
    kind = interface_deep.JiuWenSwarmDeepAdapter if request.param == 'deep' else JiuwenSwarmFlashAdapter
    adapter = object.__new__(kind)
    adapter._instance = SimpleNamespace(card=parent._card)
    adapter._project_dir = None
    adapter._workspace_dir = str(tmp_path / 'internal')
    adapter._runtime_prompt_rail = adapter._response_prompt_rail = adapter._permission_rail = None
    adapter._stream_event_rail = JiuSwarmStreamEventRail()
    adapter._stream_event_rail.set_skill_turbo_adapter(adapter)
    adapter._task_execution_rail = Mock()
    monkeypatch.setattr(interface_deep, 'get_sandbox_runtime', lambda: {'enabled': False})
    monkeypatch.setattr('jiuwenswarm.common.utils.get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setenv('JIUWENSWARM_TASKS_DIR', str(tmp_path / 'tasks'))
    monkeypatch.setenv('JIUWENSWARM_TASK_REGISTRY_DIR', str(tmp_path / 'registry'))
    for name in ('_update_rails_for_mode', '_set_user_interaction_enabled', '_update_tools_for_mode', '_update_session_tools'):
        monkeypatch.setattr(adapter, name, AsyncMock())
    for name in ('_seed_runtime_cwd', '_schedule_runtime_state_write', '_refresh_acp_runtime_tools', '_update_prompt_for_mode'):
        monkeypatch.setattr(adapter, name, Mock())
    monkeypatch.setattr(adapter, '_resolve_runtime_language', lambda: 'cn')
    monkeypatch.setattr(adapter, 'build_skill_turbo_config', lambda: {'enabled': True})
    monkeypatch.setattr(adapter, 'restore_skill_turbo_model_from_resume_ctx', Mock())
    if request.param == 'flash':
        monkeypatch.setattr(adapter, '_sync_unified_memory_tool_visibility', Mock())
        monkeypatch.setattr(adapter, '_refresh_flash_turbo_description', Mock())
    metadata_token = tools.set_current_request_metadata(None)
    workspace_token = set_effective_request_workspace_dir(None)

    async def configure(*, project_dir=None, metadata=None, request_id='host-request', sid=None):
        wire = AgentRequest(request_id=request_id, channel_id='officeclaw', session_id=sid or parent.get_session_id(),
                            params={'query': 'original', 'project_dir': project_dir}, metadata=metadata)
        original = copy.deepcopy(wire.metadata)
        await adapter._update_runtime_config(adapter._RuntimeConfig(
            session_id=wire.session_id, request_id=wire.request_id, channel_id=wire.channel_id,
            mode=request.param, project_dir=wire.params.get('project_dir'),
            request_metadata={**(wire.metadata or {}), '__skill_turbo_model_identity__': {'model_ref': 'test'}}))
        assert wire.metadata == original
        return copy.deepcopy(adapter._stream_event_rail._skill_turbo_request_metadata)

    async def invoke(arguments, *, answers=None):
        call = ToolCall(id='host-tool-call', type='function', name='skill_acceleration_exec', arguments=json.dumps(arguments))
        ctx = AgentCallbackContext(agent=adapter._instance, session=parent,
            inputs=ToolCallInputs(tool_call=call, tool_name=call.name),
            extra={RESUME_USER_INPUT_KEY: answers} if answers is not None else {})
        try:
            await adapter._stream_event_rail.before_tool_call(ctx)
            return await tools.skill_turbo.invoke(arguments)
        finally:
            reset_skill_turbo_context(ctx)

    yield SimpleNamespace(configure=configure, invoke=invoke, adapter=adapter, parent=parent)
    tools.reset_current_request_metadata(metadata_token)
    reset_effective_request_workspace_dir(workspace_token)


@pytest.mark.parametrize('existing', [None, '', '  '])
async def test_explicit_project_dir_populates_both_request_and_rail_metadata(host_request, batch_fixture, existing):
    metadata = {} if existing is None else {'output_dir': existing}
    result = await host_request.configure(project_dir='  ' + str(batch_fixture.segment.workspace) + '  ', metadata=metadata)
    expected = str(batch_fixture.segment.workspace)
    assert result['output_dir'] == expected
    assert tools.get_current_request_metadata()['output_dir'] == expected
    host_request.adapter._task_execution_rail.set_skill_turbo_request_metadata.assert_called_with(result)


async def test_project_dir_reaches_real_segment_tool_and_creates_scoped_artifact(host_request, batch_fixture):
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace))
    result = await asyncio.create_task(host_request.invoke(
        {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep'}), context=Context())
    assert result['status'] == 'ok', result
    assert batch_fixture.calls['prep'] == 1 and result['ledger']['revision'] == 1
    run_dir = Path(result['ledger']['run_dir'])
    assert run_dir.parent.parent.parent == batch_fixture.segment.workspace
    assert json.loads((run_dir / 'prep.json').read_text())['stage'] == 'prep'


async def test_explicit_output_dir_keeps_precedence(host_request, batch_fixture, tmp_path):
    workspace = str(batch_fixture.segment.workspace)
    result = await host_request.configure(project_dir=str(Path(workspace) / 'project'), metadata={'output_dir': workspace})
    assert result['output_dir'] == workspace
    value = await host_request.invoke({'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep'})
    assert (value['status'], value.get('error')) == ('ok', None), value
    assert Path(value['ledger']['run_dir']).is_relative_to(workspace)


@pytest.mark.parametrize('project_dir', [None, '', 'relative-project'])
async def test_no_default_workspace_or_cached_project_is_promoted(host_request, batch_fixture, project_dir):
    host_request.adapter._project_dir = str(batch_fixture.segment.workspace)
    result = await host_request.configure(project_dir=project_dir, metadata={'effective_project_dir': str(batch_fixture.segment.workspace)})
    assert 'output_dir' not in result
    value = await host_request.invoke({'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep'})
    assert value['status'] == 'blocked' and value['error'] == 'workspace_missing', value
    assert not batch_fixture.calls and not list(batch_fixture.segment.workspace.glob('skill_runs/*/*'))


async def test_batch_resume_uses_original_project_and_does_not_repeat_prefix(host_request, batch_fixture):
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerators': [
        {'id': 'prep'}, {'id': 'outline', 'params': {'pause': True}}, {'id': 'generate'}]}
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace))
    paused = await host_request.invoke(arguments)
    assert '__skill_turbo_hitl__' in str(paused)
    checkpoint = await batch_fixture.state()
    assert checkpoint['segment']['batch']['cursor'] == 1
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace), request_id='resume-request')
    result = await host_request.invoke(arguments, answers={'Review outline?': 'yes'})
    assert result['status'] == 'ok' and result['ledger']['revision'] == 3, result
    assert batch_fixture.calls['prep'] == batch_fixture.calls['leaf:outline'] == batch_fixture.calls['generate'] == 1
    assert await batch_fixture.state() is None


async def test_metadata_copies_do_not_reuse_previous_request_workspace(host_request, batch_fixture, tmp_path):
    first = await host_request.configure(project_dir=str(batch_fixture.segment.workspace), sid='session-one')
    second_workspace = tmp_path / 'second'
    second_workspace.mkdir()
    second = await host_request.configure(project_dir=str(second_workspace), sid='session-two')
    missing = await host_request.configure(sid='session-three')
    assert first['output_dir'] == str(batch_fixture.segment.workspace)
    assert second['output_dir'] == str(second_workspace)
    assert 'output_dir' not in missing
    assert first['session_id'] != second['session_id'] != missing['session_id']


async def test_single_segment_resume_keeps_workspace_and_completed_leaf(host_request, batch_fixture):
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerator': 'prep', 'params': {'pause': True}}
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace))
    paused = await host_request.invoke(arguments)
    assert '__skill_turbo_hitl__' in str(paused)
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace), request_id='single-resume')
    result = await host_request.invoke(arguments, answers={'Review prep?': 'yes'})
    assert result['status'] == 'ok' and result['ledger']['revision'] == 1, result
    assert batch_fixture.calls['leaf:prep'] == batch_fixture.calls['prep'] == 1
    assert await batch_fixture.state() is None


async def test_resume_with_changed_project_rejects_without_new_business(host_request, batch_fixture, tmp_path):
    arguments = {'query': 'original', 'skill': 'demo-craft', 'accelerators': [
        {'id': 'prep'}, {'id': 'outline', 'params': {'pause': True}}, {'id': 'generate'}]}
    await host_request.configure(project_dir=str(batch_fixture.segment.workspace))
    await host_request.invoke(arguments)
    assert await batch_fixture.state() is not None
    before = batch_fixture.calls.copy()
    changed = tmp_path / 'changed-project'
    changed.mkdir()
    await host_request.configure(project_dir=str(changed), request_id='changed-resume')
    result = await host_request.invoke(arguments, answers={'Review outline?': 'yes'})
    assert result['status'] == 'failed' and batch_fixture.calls == before, result
    assert not list(changed.glob('skill_runs/*/*'))
