import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult
from openjiuwen.harness.security import PermissionLevel, PermissionResult
from jiuwenswarm.common.behavior_security import (
    BehaviorSecurityBridge, check_agent_skill_install, cloud_authorization_enabled, raw_event,
)
from jiuwenswarm.agents.harness.common.rails.behavior_security_rail import (
    BehaviorSecurityRail,
    OUTPUT_BLOCK_MESSAGE,
)


def context(session=None):
    if session is None:
        data = {}
        session = SimpleNamespace(get_session_id=lambda: 'session', get_state=data.get, update_state=data.update)
    call = ToolCall(id='call-1', type='function', name='install_skill', arguments='{}')
    force_finish_requests = []
    return SimpleNamespace(
        session=session,
        exception=None,
        extra={},
        inputs=SimpleNamespace(
            tool_call=call,
            tool_name=call.name,
            tool_args=call.arguments,
            tool_result=None,
            tool_msg=None,
            execution_started=False,
        ),
        force_finish_requests=force_finish_requests,
        request_force_finish=force_finish_requests.append,
    )


def bridge(enabled=True, decision='allow'):
    events = []
    async def handler(request):
        events.append(json.loads(request.content))
        return httpx.Response(200, json={'decision': decision})
    b = BehaviorSecurityBridge({'permissions': {'enabled': True}, 'xiaoyi_work_security': {
        'cloud_authorization': {'enabled': enabled}}}, transport=httpx.MockTransport(handler))
    return b, events


@pytest.mark.asyncio
@pytest.mark.parametrize('level', [PermissionLevel.ALLOW, PermissionLevel.ASK, PermissionLevel.DENY])
async def test_explicit_rules_report_without_cloud_wait(level):
    b, events = bridge()
    wait = asyncio.Event()
    async def delayed(_):
        await wait.wait()
        return {'decision': 'deny'}
    b._send = AsyncMock(side_effect=delayed)
    result = await asyncio.wait_for(b.decide(raw_event(context(), 'tool.before'), PermissionResult(level)), .1)
    assert result.permission == level
    await asyncio.sleep(0)
    assert b._send.call_args.args[0]['mode'] == 'report'
    await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,expected', [('allow', PermissionLevel.ALLOW), ('deny', PermissionLevel.DENY),
    ('clarify', PermissionLevel.ALLOW), ('unavailable', PermissionLevel.ASK)])
async def test_unknown_one_waited_request_and_no_second_report(decision, expected):
    b, events = bridge(decision=decision)
    event = raw_event(context(), 'tool.before')
    for _ in range(2):
        result = await b.decide(event, PermissionResult(PermissionLevel.UNDETERMINED))
        assert result.permission == expected
    assert len(events) == 1
    assert events[0]['mode'] == 'authorize'
    assert 'businessId' not in events[0]


@pytest.mark.parametrize('enabled,expected', [
    (True, True), (False, False), (None, False), ('false', False), ('flase', False), (1, False),
])
def test_cloud_switch_requires_a_boolean(enabled, expected):
    assert cloud_authorization_enabled({
        'xiaoyi_work_security': {'cloud_authorization': {'enabled': enabled}},
    }) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['tool.before', 'skill.before_install'])
@pytest.mark.parametrize('default,expected', [
    ('allow', PermissionLevel.ALLOW), ('deny', PermissionLevel.DENY),
    ('ask', PermissionLevel.ASK), (None, PermissionLevel.ASK), ('invalid', PermissionLevel.ASK),
])
async def test_cloud_switch_off_reports_and_uses_core_defaults(stage, default, expected):
    b, events = bridge(enabled=False)
    b.config['permissions']['defaults'] = {} if default is None else {'*': default}
    wait = asyncio.Event()
    async def delayed(event):
        events.append(event)
        await wait.wait()
        return {'decision': 'deny'}
    b._send = AsyncMock(side_effect=delayed)
    result = await asyncio.wait_for(
        b.decide(raw_event(context(), stage), PermissionResult(PermissionLevel.UNDETERMINED)), .2,
    )
    assert result.permission == expected
    assert result.matched_rule == ('tiered_policy:defaults.*' if default in {'allow', 'deny', 'ask'}
                                   else 'tiered_policy:fallback(no_config)')
    await asyncio.sleep(0)
    assert len(events) == 1 and events[0]['mode'] == 'report'
    await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_install_confirmation_stops_before_write_and_resumes_once(tmp_path, approved):
    b, events = bridge(decision='unavailable')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    staged = tmp_path / 'staged'
    staged.mkdir()
    (staged / 'SKILL.md').write_text('skill content')
    dest = tmp_path / 'installed'
    dest.mkdir()
    old = dest / 'SKILL.md'
    old.write_text('original')
    await rail.before_tool_call(ctx)
    with pytest.raises(PermissionError):
        await check_agent_skill_install(staged, dest, 'example', 'download')
    assert old.read_text() == 'original'
    assert events[0]['stage'] == 'skill.before_install'
    ctx.inputs.tool_result = {'success': False}
    with pytest.raises(AbortError):
        await rail.after_tool_call(ctx)
    resumed = context(ctx.session)
    resumed.extra[RESUME_USER_INPUT_KEY] = {'approved': approved}
    await rail.before_tool_call(resumed)
    if approved:
        await check_agent_skill_install(staged, dest, 'example', 'download')
    else:
        with pytest.raises(PermissionError, match='用户拒绝'):
            await check_agent_skill_install(staged, dest, 'example', 'download')
    assert len(events) == 1
    assert old.read_text() == 'original'
    await rail.after_tool_call(resumed)


@pytest.mark.asyncio
async def test_output_inspection_waits_and_rejects_before_model_continues():
    b, events = bridge(decision='deny')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    ctx.extra['_interrupt_decision'] = ApproveResult()
    ctx.inputs.tool_result = {'result': 'ok'}
    ctx.inputs.execution_started = True
    await rail.after_tool_call(ctx)
    assert len(events) == 1 and events[0]['stage'] == 'tool.after' and events[0]['mode'] == 'inspect'
    assert ctx.inputs.tool_result == OUTPUT_BLOCK_MESSAGE
    assert ctx.force_finish_requests == [{
        'output': OUTPUT_BLOCK_MESSAGE,
        'result_type': 'answer',
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,blocked', [
    ('allow', False), ('deny', True), ('unavailable', True),
])
async def test_output_inspection_is_fail_closed(decision, blocked):
    b, events = bridge(decision=decision)
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    ctx.inputs.tool_result = {'result': 'ok'}
    ctx.inputs.execution_started = True
    await rail.after_tool_call(ctx)
    assert events[0]['mode'] == 'inspect'
    assert bool(ctx.force_finish_requests) is blocked
    assert ctx.inputs.tool_result == (OUTPUT_BLOCK_MESSAGE if blocked else {'result': 'ok'})


@pytest.mark.asyncio
async def test_large_output_is_fully_chunked_for_inspection():
    b, events = bridge(decision='allow')
    ctx = context()
    ctx.inputs.tool_result = {'content': 'A' * 7000 + 'MIDDLE_MARKER' + 'B' * 7000}
    result = await b.inspect(raw_event(ctx, 'tool.after'))
    assert result.permission == PermissionLevel.ALLOW
    assert len(events) >= 3
    assert all(event['mode'] == 'inspect' for event in events)
    assert all(len(event['tool']['result']['content']) <= 5500 for event in events)
    assert any('MIDDLE_MARKER' in event['tool']['result']['content'] for event in events)


@pytest.mark.asyncio
async def test_oversized_output_fails_closed_without_partial_scan():
    b, events = bridge(decision='allow')
    ctx = context()
    ctx.inputs.tool_result = {'content': 'A' * (65 * 1024)}
    result = await b.inspect(raw_event(ctx, 'tool.after'))
    assert result.permission == PermissionLevel.DENY
    assert events == []


@pytest.mark.asyncio
async def test_cancelled_cloud_request_propagates():
    b, _ = bridge()
    b._send = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await b.decide(raw_event(context(), 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED))


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['allow', 'deny'])
async def test_actual_builtin_install_checkpoint(tmp_path, monkeypatch, decision):
    from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    name = 'xiaoyi-security-smoke'
    builtin = tmp_path / 'builtin'
    src = builtin / name
    src.mkdir(parents=True)
    (src / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: Synthetic installation test skill.\nversion: 1.0.0\n---\n'
        '\n# Installation test\n\nRespond with XIAOYI_SKILL_INSTALL_OK.\n',
        encoding='utf-8',
    )
    monkeypatch.setattr('jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir', lambda: builtin)
    manager = SkillManager(workspace_dir=str(tmp_path / 'workspace'))
    toolkit = SkillToolkit(manager)
    b, events = bridge(decision=decision)
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    try:
        result = await toolkit.install_skill(identifier=name, source='builtin')
        dest = manager._skills_dir / name
        assert result['success'] is (decision == 'allow')
        assert dest.exists() is (decision == 'allow')
        assert len(events) == 1
        assert events[0]['stage'] == 'skill.before_install'
        assert events[0]['mode'] == 'authorize'
        if decision == 'allow':
            assert (dest / 'SKILL.md').read_bytes() == (src / 'SKILL.md').read_bytes()
            assert any(item['name'] == name for item in manager.get_installed_plugins())
        else:
            assert not any(item['name'] == name for item in manager.get_installed_plugins())
    finally:
        await rail.after_tool_call(ctx)
        await b.close()


@pytest.mark.asyncio
async def test_desktop_permission_factory_falls_back_to_real_interrupt(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_CHANNEL_ID
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    cfg = {'enabled': True, 'package_builtin_rules': False, 'defaults': {'*': 'allow'}}
    monkeypatch.setattr('jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
                        lambda **_: cfg.copy())
    b, events = bridge(decision='unavailable')
    ctx = context()
    rail = BehaviorSecurityRail(b)
    await rail.before_tool_call(ctx)
    channel = CURRENT_CHANNEL_ID.set('desktop')
    try:
        permission = build_permission_rail({**b.config, 'permissions': cfg})
        assert permission is not None
        result = await permission.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
        assert isinstance(result, InterruptResult)
        assert len(events) == 1 and events[0]['mode'] == 'authorize'
    finally:
        CURRENT_CHANNEL_ID.reset(channel)
        await rail.after_tool_call(ctx)


@pytest.mark.asyncio
async def test_real_core_callback_chain_install_interrupt(tmp_path):
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs, AgentCallbackEvent
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    b, events = bridge(decision='unavailable')
    rail = BehaviorSecurityRail(b)
    callback_manager = AgentCallbackManager('behavior-security-test')
    agent = SimpleNamespace(agent_callback_manager=callback_manager)
    # Register callbacks via the actual core callback framework (which unwraps AbortError).
    await callback_manager.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, rail.before_tool_call, 100)
    await callback_manager.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, rail.after_tool_call, 100)
    simple = context()
    ctx = AgentCallbackContext(agent=agent, session=simple.session, inputs=ToolCallInputs(
        tool_call=simple.inputs.tool_call, tool_name='install_skill', tool_args='{}'))
    staged = tmp_path / 'SKILL.md'
    staged.write_text('test')
    manager = AbilityManager()
    async def invoke(**_):
        try:
            await check_agent_skill_install(staged, tmp_path / 'dest', 'test', 'download')
        except PermissionError:
            return {'success': False}, None
        return {'success': True}, None
    manager._execute_single_tool_call = invoke
    with pytest.raises(ToolInterruptException):
        await manager._railed_execute_single_tool_call(ctx, simple.inputs.tool_call, simple.session)
    assert ctx.inputs.execution_started
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['deny', 'unavailable'])
async def test_skillnet_worker_stops_before_force_replace(tmp_path, monkeypatch, decision):
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    manager = SkillManager(workspace_dir=str(tmp_path / 'workspace'))
    src = tmp_path / 'downloaded'
    src.mkdir()
    (src / 'SKILL.md').write_text('---\nname: existing\ndescription: new\n---\nnew content')
    dest = manager._skills_dir / 'existing'
    dest.mkdir(parents=True)
    old = dest / 'SKILL.md'
    old.write_text('original content')
    monkeypatch.setattr(manager, '_skillnet_download_sync', lambda *args: str(src))
    b, events = bridge(decision=decision)
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    result = await asyncio.to_thread(manager._skillnet_install_files_sync, 'https://example.test/skill', True)
    assert not result['ok']
    assert old.read_text() == 'original content'
    assert len(events) == 1
    if decision == 'unavailable':
        with pytest.raises(AbortError):
            await rail.after_tool_call(ctx)
    else:
        await rail.after_tool_call(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('permission_enabled', [False, True])
@pytest.mark.parametrize('cloud_reply', ['allow', 'deny', 'clarify', 'unavailable'])
@pytest.mark.parametrize('enabled,default,explicit,expected,mode,matched', [
    (False, 'allow', None, 'allow', 'report', 'tiered_policy:defaults.*'),
    (False, 'deny', None, 'deny', 'report', 'tiered_policy:defaults.*'),
    (False, 'ask', None, 'ask', 'report', 'tiered_policy:defaults.*'),
    (False, None, None, 'ask', 'report', 'default'),
    (False, 'allow', 'deny', 'deny', 'report', 'tools.install_skill'),
    (False, 'allow', 'ask', 'ask', 'report', 'tools.install_skill'),
    (False, 'deny', 'allow', 'allow', 'report', 'tools.install_skill'),
    (True, 'allow', None, 'deny', 'authorize', 'tiered_policy:unmatched'),
    (True, 'allow', 'ask', 'ask', 'report', 'tools.install_skill'),
    (True, 'allow', 'deny', 'deny', 'report', 'tools.install_skill'),
    (True, 'deny', 'allow', 'allow', 'report', 'tools.install_skill'),
])
async def test_permission_factory_uses_defaults_only_when_cloud_is_off(
        monkeypatch, permission_enabled, cloud_reply, enabled, default, explicit, expected, mode, matched):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_CHANNEL_ID
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult, RejectResult
    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    cfg = {'enabled': permission_enabled, 'schema': 'tiered_policy', 'package_builtin_rules': False,
           'defaults': {} if default is None else {'*': default},
           'tools': {} if explicit is None else {'install_skill': explicit}}
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
        lambda **_: cfg.copy(),
    )
    b, events = bridge(enabled=enabled, decision=cloud_reply)
    b.config['permissions'] = cfg
    ctx = context()
    rail = BehaviorSecurityRail(b)
    await rail.before_tool_call(ctx)
    assert 'behavior.local_permission' not in ctx.extra
    assert not events
    channel = CURRENT_CHANNEL_ID.set('desktop')
    try:
        permission = build_permission_rail(b.config)
        assert permission is not None
        # The refreshed snapshot must preserve the switch as well as the initial config.
        assert permission._host.get_permissions_snapshot()['defer_unmatched'] is enabled
        assert permission._host.get_permissions_snapshot()['enabled'] is True
        assert cfg['enabled'] is permission_enabled
        result = await permission.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
        if mode == 'authorize':
            expected = {'allow': 'allow', 'deny': 'deny', 'clarify': 'allow', 'unavailable': 'ask'}[cloud_reply]
        assert isinstance(result, {'allow': ApproveResult, 'deny': RejectResult, 'ask': InterruptResult}[expected])
        assert ctx.extra['behavior.local_permission'].matched_rule == matched
        await asyncio.gather(*b.tasks)
        assert len(events) == 1 and events[0]['mode'] == mode
    finally:
        CURRENT_CHANNEL_ID.reset(channel)
        await rail.after_tool_call(ctx)
        await b.close()


def test_full_access_outside_desktop_keeps_existing_behavior(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    monkeypatch.delenv('CLAW_BEHAVIOR_SECURITY', raising=False)
    assert build_permission_rail({'permissions': {'enabled': False}}) is None


def test_full_access_hot_reload_keeps_permission_checks(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    config = {'permissions': {'enabled': False, 'package_builtin_rules': False},
              'xiaoyi_work_security': {'cloud_authorization': {'enabled': True}}}
    adapter = SimpleNamespace(_permission_rail=None, _model=None)
    JiuWenSwarmDeepAdapter._update_permission_rail(adapter, config)
    permission = adapter._permission_rail
    assert permission is not None
    for enabled in (False, True, False):
        config['permissions']['enabled'] = enabled
        JiuWenSwarmDeepAdapter._update_permission_rail(adapter, config)
        assert adapter._permission_rail is permission
        assert permission._engine.config['enabled'] is True
        assert permission._engine.config['defer_unmatched'] is True
        assert config['permissions']['enabled'] is enabled


@pytest.mark.asyncio
async def test_desktop_full_access_cannot_write_control_plane(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.common import utils

    config_dir = tmp_path / 'config'
    skills_dir = tmp_path / 'skills'
    workspace = tmp_path / 'workspace'
    for path in (config_dir, skills_dir, workspace):
        path.mkdir()
    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    monkeypatch.setattr(utils, 'get_config_dir', lambda: config_dir)
    monkeypatch.setattr(utils, 'get_config_file', lambda: config_dir / 'config.yaml')
    monkeypatch.setattr(utils, 'get_agent_skills_dir', lambda: skills_dir)
    monkeypatch.setattr(utils, 'get_workspace_dir', lambda: workspace)

    rail = build_permission_rail({
        'permissions': {'enabled': False, 'package_builtin_rules': False},
        'xiaoyi_work_security': {'cloud_authorization': {'enabled': False}},
    })
    assert rail is not None
    result = await rail._engine.check_permission(
        'write_file',
        {'file_path': str(config_dir / 'config.yaml'), 'content': 'permissions:\n  enabled: false\n'},
    )
    assert result.permission == PermissionLevel.DENY
    assert result.matched_rule == 'immutable_control_plane'
