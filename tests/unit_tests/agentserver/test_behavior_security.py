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
    PENDING_OUTPUT, BehaviorSecurityBridge, check_agent_skill_install, cloud_authorization_enabled, raw_event,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["xiaoyi", "a2a"])
async def test_request_profile_enables_missing_rail_without_changing_disabled_global_policy(monkeypatch, channel):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )

    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "0")
    stored = {"permissions": {"enabled": False, "package_builtin_rules": False}}
    monkeypatch.setattr(interface_deep, "get_config", lambda: stored)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: stored)
    adapter = interface_deep.JiuWenSwarmDeepAdapter.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._permission_rail = None
    adapter._model = None
    adapter._instance = SimpleNamespace(register_rail=AsyncMock())
    request = SimpleNamespace(channel_id=channel, metadata={"xiaoyi_permission_profile": "default"})
    token = setup_permission_context(request)
    try:
        await adapter._update_request_permission_rail(channel)
        assert adapter._permission_rail._engine.config["enabled"] is True
        adapter._instance.register_rail.assert_awaited_once_with(adapter._permission_rail)
    finally:
        cleanup_permission_context(token)
    await adapter._update_request_permission_rail(channel)
    assert adapter._permission_rail._host.get_permissions_snapshot()["enabled"] is False
    assert stored["permissions"]["enabled"] is False


@pytest.mark.asyncio
async def test_request_profiles_are_task_local_and_web_cannot_reuse_xiaoyi_override(monkeypatch):
    from jiuwenswarm.common.behavior_security import runtime_permission_config
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )
    from openjiuwen.harness.security.core import PermissionEngine

    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "0")
    stored = {"permissions": {"enabled": True, "package_builtin_rules": False, "tools": {"bash": "ask"}}}

    async def evaluate(channel, profile):
        token = setup_permission_context(SimpleNamespace(channel_id=channel, metadata={"xiaoyi_permission_profile": profile}))
        try:
            await asyncio.sleep(0)
            engine = PermissionEngine(config=runtime_permission_config(stored))
            result = await engine.check_permission("bash", {"command": "profile-operation"})
            return result.permission
        finally:
            cleanup_permission_context(token)

    results = await asyncio.gather(evaluate("xiaoyi", "full_access"), evaluate("a2a", "default"),
                                   evaluate("web", "full_access"), evaluate("xiaoyi", "auto_approve"))
    assert results == [PermissionLevel.ALLOW, PermissionLevel.ASK, PermissionLevel.ASK, PermissionLevel.ALLOW]
    assert stored["permissions"]["enabled"] is True
    assert stored["permissions"]["tools"]["bash"] == "ask"


@pytest.mark.asyncio
async def test_full_access_request_keeps_desktop_security_denials(monkeypatch):
    from jiuwenswarm.common.behavior_security import runtime_permission_config
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )
    from openjiuwen.harness.security.core import PermissionEngine

    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "1")
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "test-token")
    stored = {"permissions": {"enabled": True, "package_builtin_rules": False,
                               "tools": {"bash": "ask", "blocked_tool": "deny"}}}
    token = setup_permission_context(SimpleNamespace(channel_id="xiaoyi", metadata={"xiaoyi_permission_profile": "full_access"}))
    try:
        engine = PermissionEngine(config=runtime_permission_config(stored))
        assert engine.config["enabled"] is True
        assert (await engine.check_permission("blocked_tool", {})).permission == PermissionLevel.DENY
        assert (await engine.check_permission("bash", {"command": "profile-operation"})).permission == PermissionLevel.ALLOW
    finally:
        cleanup_permission_context(token)
    assert stored["permissions"]["tools"]["bash"] == "ask"


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
    ('clarify', PermissionLevel.ASK), ('unavailable', PermissionLevel.ALLOW)])
async def test_unknown_one_waited_request_and_no_second_report(decision, expected):
    b, events = bridge(decision=decision)
    event = raw_event(context(), 'tool.before')
    for _ in range(2):
        result = await b.decide(event, PermissionResult(PermissionLevel.UNDETERMINED))
        assert result.permission == expected
    assert len(events) == 1
    assert events[0]['mode'] == 'authorize'
    assert 'businessId' not in events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_closed,profile,stored_enabled,expected', [
    (True, 'default', True, PermissionLevel.ASK),
    (True, 'full_access', True, PermissionLevel.ALLOW),
    (True, 'default', False, PermissionLevel.ASK),
    (True, 'auto_approve', True, PermissionLevel.ASK),
    (True, None, True, PermissionLevel.ASK),
    (True, None, False, PermissionLevel.ALLOW),
    (False, 'default', True, PermissionLevel.ALLOW),
    (False, 'full_access', True, PermissionLevel.ALLOW),
])
async def test_cloud_unavailable_respects_fail_closed_and_effective_profile(
        fail_closed, profile, stored_enabled, expected):
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )

    b, _ = bridge(decision='unavailable')
    b.config['security'] = {'fail_closed': {'enabled': fail_closed}}
    b.config['permissions']['enabled'] = stored_enabled
    token = setup_permission_context(SimpleNamespace(
        channel_id='xiaoyi' if profile else 'web',
        metadata={'xiaoyi_permission_profile': profile} if profile else {},
    ))
    try:
        result = await b.decide(raw_event(context(), 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED))
        assert result.permission == expected
    finally:
        cleanup_permission_context(token)
        await b.close()


def test_behavior_policy_revision_changes_with_request_profile():
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )

    b, _ = bridge()
    revisions = []
    for profile in ('default', 'full_access', 'auto_approve'):
        token = setup_permission_context(SimpleNamespace(
            channel_id='xiaoyi', metadata={'xiaoyi_permission_profile': profile},
        ))
        try:
            revisions.append(b.policy_revision())
        finally:
            cleanup_permission_context(token)
    assert len(set(revisions)) == len(revisions)


@pytest.mark.asyncio
@pytest.mark.parametrize('cloud_enabled', [False, True])
async def test_concurrent_request_profiles_do_not_share_fallback_decisions(cloud_enabled):
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )

    b, events = bridge(enabled=cloud_enabled, decision='unavailable')
    b.config['security'] = {'fail_closed': {'enabled': True}}

    async def evaluate(profile):
        token = setup_permission_context(SimpleNamespace(
            channel_id='xiaoyi', metadata={'xiaoyi_permission_profile': profile},
        ))
        try:
            ctx = context()
            ctx.extra['behavior.policy_revision'] = b.policy_revision()
            await asyncio.sleep(0)
            result = await b.decide(raw_event(ctx, 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED))
            return result.permission
        finally:
            cleanup_permission_context(token)

    try:
        results = await asyncio.gather(evaluate('default'), evaluate('full_access'))
        assert results == [PermissionLevel.ASK, PermissionLevel.ALLOW]
        await asyncio.gather(*b.tasks)
        assert len(events) == 2
        assert events[0]['eventId'] != events[1]['eventId']
        assert all(event['mode'] == ('authorize' if cloud_enabled else 'report') for event in events)
    finally:
        await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,expected', [('allow', PermissionLevel.ALLOW), ('deny', PermissionLevel.DENY),
    ('clarify', PermissionLevel.ASK), ('unavailable', PermissionLevel.ASK)])
async def test_decide_authoritative_always_waits_for_cloud(decision, expected):
    """安装审核权威口径：不看本地裁决，一律 authorize 同步等云端；unavailable/clarify 转 ASK。"""
    b, events = bridge(decision=decision)
    event = raw_event(context(), 'skill.before_install')
    for _ in range(2):
        result = await b.decide_authoritative(event)
        assert result.permission == expected
    assert len(events) == 1
    assert events[0]['mode'] == 'authorize'
    await b.close()


@pytest.mark.asyncio
async def test_skill_install_stays_ask_when_cloud_unavailable():
    b, _ = bridge(decision='unavailable')
    result = await b.decide(
        raw_event(context(), 'skill.before_install'), PermissionResult(PermissionLevel.UNDETERMINED),
    )
    assert result.permission == PermissionLevel.ASK
    await b.close()


@pytest.mark.asyncio
async def test_decide_authoritative_unavailable_fail_open_allows():
    """skill_install_fail_open: true 时云端不可用静默放行（缺省 false 转用户确认）。"""
    b, events = bridge(decision='unavailable')
    b.config['xiaoyi_work_security']['cloud_authorization']['skill_install_fail_open'] = True
    result = await b.decide_authoritative(raw_event(context(), 'skill.before_install'))
    assert result.permission == PermissionLevel.ALLOW
    assert events[0]['mode'] == 'authorize'
    await b.close()


@pytest.mark.asyncio
async def test_policy_revision_changes_event_identity_and_discards_old_reply():
    b, _ = bridge(decision='allow')
    ctx = context()
    rail = BehaviorSecurityRail(b)
    await rail.before_tool_call(ctx)
    old_event = raw_event(ctx, 'tool.before')
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(_event):
        started.set()
        await release.wait()
        return {'decision': 'allow'}

    b._send = delayed
    pending = asyncio.create_task(b.decide(old_event, PermissionResult(PermissionLevel.UNDETERMINED)))
    await started.wait()
    changed = dict(b.config)
    changed['security'] = {'fail_closed': {'enabled': True}}
    b.update_config(changed)
    await rail.before_tool_call(ctx)
    assert raw_event(ctx, 'tool.before')['eventId'] != old_event['eventId']
    release.set()
    assert (await pending).permission == PermissionLevel.DENY
    assert not b.decisions
    await b.close()


@pytest.mark.asyncio
async def test_install_gate_cloud_deny_blocks_despite_local_allow(tmp_path):
    """云端权威化回归：发起工具本地已 ALLOW 时，云端 REJECT 仍阻断安装（此前只 report 无人消费）。"""
    b, events = bridge(decision='deny')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    ctx.extra['behavior.local_permission'] = PermissionResult(PermissionLevel.ALLOW)
    staged = tmp_path / 'staged'
    staged.mkdir()
    (staged / 'SKILL.md').write_text('skill content')
    dest = tmp_path / 'installed'
    dest.mkdir()
    old = dest / 'SKILL.md'
    old.write_text('original')
    await rail.before_tool_call(ctx)
    with pytest.raises(PermissionError, match='已拒绝安装'):
        await check_agent_skill_install(staged, dest, 'example', 'download')
    assert old.read_text() == 'original'
    assert events[0]['stage'] == 'skill.before_install'
    assert events[0]['mode'] == 'authorize'
    await rail.after_tool_call(ctx)
    await b.close()


@pytest.mark.asyncio
async def test_install_gate_download_url_passthrough(tmp_path):
    """来源下载地址透传进事件 skill.downloadUrl（云端回拉全量包审核）；缺省不出现该键。"""
    b, events = bridge(decision='allow')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    staged = tmp_path / 'staged'
    staged.mkdir()
    (staged / 'SKILL.md').write_text('skill content')
    dest = tmp_path / 'installed'
    await rail.before_tool_call(ctx)
    await check_agent_skill_install(staged, dest, 'example', 'clawhub',
                                    download_url='https://clawhub.ai/api/v1/download?slug=example')
    assert events[0]['skill']['downloadUrl'] == 'https://clawhub.ai/api/v1/download?slug=example'
    await rail.after_tool_call(ctx)
    await b.close()

    b2, events2 = bridge(decision='allow')
    rail2 = BehaviorSecurityRail(b2)
    ctx2 = context()
    await rail2.before_tool_call(ctx2)
    await check_agent_skill_install(staged, dest, 'example', 'builtin')
    assert 'downloadUrl' not in events2[0]['skill']
    await rail2.after_tool_call(ctx2)
    await b2.close()


@pytest.mark.parametrize('enabled,expected', [
    (True, True), (False, False), (None, False), ('false', False), ('flase', False), (1, False),
])
def test_cloud_switch_requires_a_boolean(enabled, expected):
    assert cloud_authorization_enabled({
        'xiaoyi_work_security': {'cloud_authorization': {'enabled': enabled}},
    }) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('default,expected', [
    ('allow', PermissionLevel.ALLOW), ('deny', PermissionLevel.DENY),
    ('ask', PermissionLevel.ASK), (None, PermissionLevel.ASK), ('invalid', PermissionLevel.ASK),
])
async def test_cloud_switch_off_keeps_install_core_defaults(default, expected):
    b, events = bridge(enabled=False)
    b.config['permissions']['defaults'] = {} if default is None else {'*': default}
    wait = asyncio.Event()
    async def delayed(event):
        events.append(event)
        await wait.wait()
        return {'decision': 'deny'}
    b._send = AsyncMock(side_effect=delayed)
    result = await asyncio.wait_for(
        b.decide(raw_event(context(), 'skill.before_install'), PermissionResult(PermissionLevel.UNDETERMINED)), .2,
    )
    assert result.permission == expected
    assert result.matched_rule == ('tiered_policy:defaults.*' if default in {'allow', 'deny', 'ask'}
                                   else 'tiered_policy:fallback(no_config)')
    await asyncio.sleep(0)
    assert len(events) == 1 and events[0]['mode'] == 'report'
    await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cloud_enabled', [False, True])
@pytest.mark.parametrize('default', ['allow', 'ask', 'deny'])
@pytest.mark.parametrize('fail_closed,expected', [
    (True, PermissionLevel.ASK), (False, PermissionLevel.ALLOW),
])
async def test_unmatched_tool_uses_one_fallback_when_cloud_off_or_unavailable(
        cloud_enabled, default, fail_closed, expected):
    b, events = bridge(enabled=cloud_enabled, decision='unavailable')
    b.config['permissions']['defaults'] = {'*': default}
    b.config['security'] = {'fail_closed': {'enabled': fail_closed}}
    try:
        result = await b.decide(
            raw_event(context(), 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED),
        )
        assert result.permission == expected
        assert result.matched_rule is None
        await asyncio.gather(*b.tasks)
        assert len(events) == 1
        assert events[0]['mode'] == ('authorize' if cloud_enabled else 'report')
    finally:
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
    """deny：输出被替换为安全指令模板（原输出不进模型视野），默认不再强制收尾。"""
    b, events = bridge(decision='deny')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    ctx.extra['_interrupt_decision'] = ApproveResult()
    ctx.inputs.tool_result = {'result': 'ok'}
    ctx.inputs.execution_started = True
    await rail.after_tool_call(ctx)
    assert len(events) == 1 and events[0]['stage'] == 'tool.after' and events[0]['mode'] == 'inspect'
    assert isinstance(ctx.inputs.tool_result, str)
    assert ctx.inputs.tool_result.startswith('[安全风控]')
    assert 'security_notice' in ctx.inputs.tool_result
    assert events[0]['eventId'] in ctx.inputs.tool_result
    assert ctx.force_finish_requests == []


@pytest.mark.asyncio
async def test_output_inspection_deny_force_finish_legacy_fallback():
    """tool_output_force_finish: true 回退旧语义：屏蔽文案 + request_force_finish。"""
    b, _ = bridge(decision='deny')
    b.config['xiaoyi_work_security']['cloud_authorization']['tool_output_force_finish'] = True
    rail = BehaviorSecurityRail(b)
    ctx = context()
    ctx.inputs.tool_result = {'result': 'ok'}
    ctx.inputs.execution_started = True
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result.startswith('[安全风控]')
    assert ctx.force_finish_requests == [{
        'output': OUTPUT_BLOCK_MESSAGE,
        'result_type': 'answer',
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,expected_kind', [
    ('allow', 'unchanged'), ('deny', 'blocked'), ('unavailable', 'unchanged'), ('clarify', 'clarified'),
])
async def test_output_inspection_blocks_only_explicit_reject(decision, expected_kind):
    b, events = bridge(decision=decision)
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    ctx.inputs.tool_result = {'result': 'ok'}
    ctx.inputs.tool_msg = SimpleNamespace(content='原始输出内容')
    ctx.inputs.execution_started = True
    if expected_kind == 'clarified':
        # clarify（2026-10-18 二次迭代）：输出暂存 + 打断点弹确定性确认卡——
        # 用户确认前输出不进模型上下文（替代旧的「放行+模型自觉 ask_user」后缀方案）
        with pytest.raises(AbortError) as exc_info:
            await rail.after_tool_call(ctx)
        request = exc_info.value.cause.request
        assert '疑似风险' in request.message
        assert 'call-1' in str(exc_info.value.cause.tool_call.id)
        assert ctx.session.get_state(f'{PENDING_OUTPUT}:call-1') == events[0]['eventId']
        stashed = b.output_stash.get(events[0]['eventId'])
        assert stashed is not None and stashed.tool_msg.content == '原始输出内容'
        return
    await rail.after_tool_call(ctx)
    assert events[0]['mode'] == 'inspect'
    assert ctx.force_finish_requests == []
    if expected_kind == 'blocked':
        assert ctx.inputs.tool_result.startswith('[安全风控]')
        assert '原始输出内容' not in ctx.inputs.tool_result
        assert ctx.inputs.tool_msg.content == ctx.inputs.tool_result
    else:
        assert ctx.inputs.tool_result == {'result': 'ok'}
        assert ctx.inputs.tool_msg.content == '原始输出内容'


@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_output_clarify_resume_restores_or_masks_without_rerun(approved):
    """确认卡续跑：批准→暂存输出原样拼回（带采纳尾注）；拒绝→屏蔽指令。均 _skip_tool 不重执行。"""
    b, events = bridge(decision='clarify')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    ctx.inputs.tool_result = '机密输出原文'
    ctx.inputs.tool_msg = SimpleNamespace(content='机密输出原文')
    ctx.inputs.execution_started = True
    with pytest.raises(AbortError):
        await rail.after_tool_call(ctx)
    event_id = events[0]['eventId']

    resumed = context(ctx.session)
    resumed.inputs.tool_name = 'bash'
    resumed.extra[RESUME_USER_INPUT_KEY] = {'approved': approved, 'feedback': '' if approved else '别用这个'}
    await rail.before_tool_call(resumed)
    assert resumed.extra.get('_skip_tool') is True
    assert ctx.session.get_state(f'{PENDING_OUTPUT}:call-1') is None
    assert b.output_stash.get(event_id) is None
    if approved:
        assert '机密输出原文' in resumed.inputs.tool_result
        assert '用户确认采纳' in resumed.inputs.tool_result
        assert resumed.inputs.tool_msg.content == resumed.inputs.tool_result
    else:
        assert '机密输出原文' not in resumed.inputs.tool_result
        assert resumed.inputs.tool_result.startswith('[安全风控]')
        assert '别用这个' in resumed.inputs.tool_result
        assert resumed.inputs.tool_msg.content == resumed.inputs.tool_result
    # after_tool_call 对跳过的调用不再送审（execution_started=False）
    await rail.after_tool_call(resumed)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_output_clarify_stale_marker_without_user_input_falls_through():
    """陈旧待确认标记（中断后未恢复直接新轮）：清掉标记，工具按正常流程走。"""
    b, _ = bridge(decision='allow')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    ctx.session.update_state({f'{PENDING_OUTPUT}:call-1': 'stale-event'})
    await rail.before_tool_call(ctx)
    assert ctx.session.get_state(f'{PENDING_OUTPUT}:call-1') is None
    assert '_skip_tool' not in ctx.extra
    assert ctx.extra.get('behavior.context_token') is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [
    {'decision': 'unavailable', 'reason': 'timeout'},
    {'decision': 'unavailable', 'reason': 'request_failed'},
    {'decision': 'unavailable', 'reason': 'invalid_security_result'},
    {'decision': 'unavailable', 'reason': 'queue_unavailable'},
    {'decision': 'unavailable'},
])
async def test_inspect_exceptions_fail_open(reply):
    b, _ = bridge()
    calls = []

    async def send(event):
        calls.append(event)
        return reply

    b._send = send
    ctx = context()
    ctx.inputs.tool_result = {'result': 'ok'}
    outcome = await b.inspect(raw_event(ctx, 'tool.after'))
    assert outcome.result.permission == PermissionLevel.ALLOW
    assert outcome.verdict == 'allow'
    assert len(calls) == 1
    await b.close()


@pytest.mark.asyncio
async def test_inspect_reject_on_one_chunk_blocks_whole_output():
    b, _ = bridge()
    calls = []

    async def send(event):
        calls.append(event)
        if event['tool']['result'].get('chunkIndex') == 0:
            return {'decision': 'deny', 'reason': 'blocked'}
        return {'decision': 'unavailable', 'reason': 'timeout'}

    b._send = send
    ctx = context()
    ctx.inputs.tool_result = {'content': 'A' * 7000 + 'MIDDLE' + 'B' * 7000}
    outcome = await b.inspect(raw_event(ctx, 'tool.after'))
    assert outcome.result.permission == PermissionLevel.DENY
    assert outcome.verdict == 'deny'
    assert outcome.reason == 'blocked'
    await b.close()


@pytest.mark.asyncio
async def test_large_output_is_fully_chunked_for_inspection():
    b, events = bridge(decision='allow')
    ctx = context()
    ctx.inputs.tool_result = {'content': 'A' * 7000 + 'MIDDLE_MARKER' + 'B' * 7000}
    outcome = await b.inspect(raw_event(ctx, 'tool.after'))
    assert outcome.result.permission == PermissionLevel.ALLOW
    assert len(events) >= 3
    assert all(event['mode'] == 'inspect' for event in events)
    assert all(len(event['tool']['result']['content']) <= 5500 for event in events)
    assert any('MIDDLE_MARKER' in event['tool']['result']['content'] for event in events)


@pytest.mark.asyncio
async def test_oversized_output_fails_open_without_partial_scan():
    b, events = bridge(decision='deny')
    ctx = context()
    ctx.inputs.tool_result = {'content': 'A' * (65 * 1024)}
    outcome = await b.inspect(raw_event(ctx, 'tool.after'))
    assert outcome.result.permission == PermissionLevel.ALLOW
    assert events == []


@pytest.mark.asyncio
async def test_cancelled_cloud_request_propagates():
    b, _ = bridge()
    b._send = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await b.decide(raw_event(context(), 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED))


# ---------------------------------------------------------------------------
# 风控三态 UI（2026-10-18）：指令模板 / CLARIFY 分支 / security_notice 豁免 / 本地安装闸口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_deny_reason_carries_security_notice_instruction():
    """toolInput deny：reason=指令模板（notice_id=eventId，引导模型调 security_notice 补写横幅）。"""
    b, _ = bridge(decision='deny')
    ctx = context()
    event = raw_event(ctx, 'tool.before')
    result = await b.decide(event, PermissionResult(PermissionLevel.UNDETERMINED))
    assert result.permission == PermissionLevel.DENY
    assert result.reason.startswith('[安全风控]')
    assert event['eventId'] in result.reason
    assert 'security_notice' in result.reason
    await b.close()


@pytest.mark.asyncio
async def test_deny_instruction_carries_labels_not_raw_remind():
    """防自我命中回归（2026-10-18 二次迭代）：指令带风险类别 labels，
    绝不带 remind 原文（劣质 remind 含命中特征，进入模型上下文会被滑窗二次 REJECT 清屏）。"""
    b, _ = bridge()
    b._send = AsyncMock(return_value={
        'decision': 'deny', 'reason': 'qc: 命中恶意特征[tcpclient]', 'labels': ['恶意代码'],
    })
    event = raw_event(context(), 'tool.before')
    result = await b.decide(event, PermissionResult(PermissionLevel.UNDETERMINED))
    assert result.permission == PermissionLevel.DENY
    assert '恶意代码' in result.reason
    assert 'tcpclient' not in result.reason
    assert '命中恶意特征' not in result.reason
    await b.close()


@pytest.mark.asyncio
async def test_decide_clarify_asks_with_remind():
    """toolInput clarify → ASK（转用户审批卡），reason 带云端 remind。"""
    b, _ = bridge()
    b._send = AsyncMock(return_value={'decision': 'clarify', 'reason': '疑似敏感操作'})
    result = await b.decide(raw_event(context(), 'tool.before'), PermissionResult(PermissionLevel.UNDETERMINED))
    assert result.permission == PermissionLevel.ASK
    assert '疑似敏感操作' in result.reason
    await b.close()


@pytest.mark.asyncio
async def test_decide_authoritative_deny_carries_skill_install_instruction():
    """skill 安装 deny：reason=安装拒绝指令模板（含技能名/风险类别 labels/notice_id，
    不带 remind 原文）。"""
    b, _ = bridge()
    b._send = AsyncMock(return_value={
        'decision': 'deny', 'reason': '发现恶意行为[tcpclient]', 'labels': ['恶意软件'],
    })
    event = raw_event(context(), 'skill.before_install')
    event['skill'] = {'name': 'evil-skill', 'source': 'local-import', 'stagedPath': '/tmp/x', 'destination': '/tmp/y'}
    result = await b.decide_authoritative(event)
    assert result.permission == PermissionLevel.DENY
    assert result.reason.startswith('[安全风控]')
    assert 'evil-skill' in result.reason
    assert '恶意软件' in result.reason
    assert 'tcpclient' not in result.reason
    assert event['eventId'] in result.reason
    await b.close()


@pytest.mark.asyncio
async def test_decide_authoritative_clarify_asks_with_remind():
    """skill 安装 clarify → ASK（复用安装确认打断点），reason 带云端 remind。"""
    b, _ = bridge()
    b._send = AsyncMock(return_value={'decision': 'clarify', 'reason': '疑似收集个人信息'})
    result = await b.decide_authoritative(raw_event(context(), 'skill.before_install'))
    assert result.permission == PermissionLevel.ASK
    assert '疑似收集个人信息' in result.reason
    await b.close()


@pytest.mark.asyncio
async def test_security_notice_tool_is_exempt_from_evaluation_and_inspection():
    """安全提示工具：权限评估本地放行且不上报；after_tool_call 不送审（防递归/防审批弹窗）。"""
    b, events = bridge(decision='deny')
    rail = BehaviorSecurityRail(b)
    ctx = context()
    ctx.inputs.tool_call = ToolCall(id='call-sn', type='function', name='security_notice', arguments='{}')
    ctx.inputs.tool_name = 'security_notice'
    await rail.before_tool_call(ctx)
    request = SimpleNamespace(ctx=ctx, result=PermissionResult(PermissionLevel.UNDETERMINED))
    result = await b.on_permission_evaluated(request)
    assert result.permission == PermissionLevel.ALLOW
    assert events == []
    ctx.inputs.tool_result = 'ok'
    ctx.inputs.execution_started = True
    await rail.after_tool_call(ctx)
    assert events == []
    await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['allow', 'deny'])
async def test_install_skill_local_source_gates_before_copy(tmp_path, decision):
    """install_skill source='local'：落盘前过云端权威闸口（source=local-import），
    deny 阻断且 skills 目录无残留。"""
    from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    staged = tmp_path / 'staged' / 'demo-skill'
    staged.mkdir(parents=True)
    (staged / 'SKILL.md').write_text(
        '---\nname: demo-skill\ndescription: Synthetic local install test.\n---\n# demo\n',
        encoding='utf-8',
    )
    manager = SkillManager(workspace_dir=str(tmp_path / 'workspace'))
    toolkit = SkillToolkit(manager)
    b, events = bridge(decision=decision)
    rail = BehaviorSecurityRail(b)
    ctx = context()
    await rail.before_tool_call(ctx)
    result = await toolkit.install_skill(identifier=str(staged), source='local')
    dest = manager._skills_dir / 'demo-skill'
    assert result['success'] is (decision == 'allow')
    assert dest.exists() is (decision == 'allow')
    assert len(events) == 1
    assert events[0]['stage'] == 'skill.before_install'
    assert events[0]['mode'] == 'authorize'
    assert events[0]['skill']['source'] == 'local-import'
    if decision == 'allow':
        assert (dest / 'SKILL.md').read_bytes() == (staged / 'SKILL.md').read_bytes()
    await rail.after_tool_call(ctx)
    await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['relative', 'missing', 'no_skill_md'])
async def test_install_local_rejects_invalid_sources(tmp_path, kind):
    """本地安装入口参数校验：相对路径/不存在目录/无 SKILL.md 一律拒绝（不触发闸口）。"""
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    manager = SkillManager(workspace_dir=str(tmp_path / 'workspace'))
    if kind == 'relative':
        path = 'some/relative/path'
    elif kind == 'missing':
        path = str(tmp_path / 'not-exist')
    else:
        empty = tmp_path / 'empty'
        empty.mkdir()
        path = str(empty)
    result = await manager.handle_skills_install_local({'path': path})
    assert result['success'] is False


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
@pytest.mark.parametrize('profile,fail_closed,expected', [
    ('default', True, 'ask'),
    ('full_access', True, 'allow'),
    ('default', False, 'allow'),
])
async def test_desktop_permission_factory_cloud_unavailable_follows_profile(
        monkeypatch, profile, fail_closed, expected):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_CHANNEL_ID
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        setup_permission_context, cleanup_permission_context,
    )
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult

    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    cfg = {'enabled': True, 'package_builtin_rules': False, 'defaults': {'*': 'allow'}}
    monkeypatch.setattr('jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
                        lambda **_: cfg.copy())
    b, events = bridge(decision='unavailable')
    b.config['permissions'] = cfg
    b.config['security'] = {'fail_closed': {'enabled': fail_closed}}
    ctx = context()
    rail = BehaviorSecurityRail(b)
    profile_token = setup_permission_context(SimpleNamespace(
        channel_id='xiaoyi', metadata={'xiaoyi_permission_profile': profile},
    ))
    channel = CURRENT_CHANNEL_ID.set('desktop')
    try:
        await rail.before_tool_call(ctx)
        permission = build_permission_rail({**b.config, 'permissions': cfg})
        assert permission is not None
        result = await permission.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
        assert isinstance(result, InterruptResult if expected == 'ask' else ApproveResult)
        assert len(events) == 1 and events[0]['mode'] == 'authorize'
    finally:
        CURRENT_CHANNEL_ID.reset(channel)
        cleanup_permission_context(profile_token)
        await rail.after_tool_call(ctx)
        await b.close()


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
    (False, 'allow', None, 'ask', 'report', 'tiered_policy:unmatched'),
    (False, 'deny', None, 'ask', 'report', 'tiered_policy:unmatched'),
    (False, 'ask', None, 'ask', 'report', 'tiered_policy:unmatched'),
    (False, None, None, 'ask', 'report', 'tiered_policy:unmatched'),
    (False, 'allow', 'deny', 'deny', 'report', 'tools.install_skill'),
    (False, 'allow', 'ask', 'ask', 'report', 'tools.install_skill'),
    (False, 'deny', 'allow', 'allow', 'report', 'tools.install_skill'),
    (True, 'allow', None, 'deny', 'authorize', 'tiered_policy:unmatched'),
    (True, 'allow', 'ask', 'ask', 'report', 'tools.install_skill'),
    (True, 'allow', 'deny', 'deny', 'report', 'tools.install_skill'),
    (True, 'deny', 'allow', 'allow', 'report', 'tools.install_skill'),
])
async def test_permission_factory_uses_shared_fallback_for_unmatched_tools(
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
    b.config['security'] = {'fail_closed': {'enabled': True}}
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
        assert permission._host.get_permissions_snapshot()['defer_unmatched'] is True
        assert permission._host.get_permissions_snapshot()['strict_tool_command_merge'] is enabled
        assert permission._host.get_permissions_snapshot()['enabled'] is True
        assert cfg['enabled'] is permission_enabled
        result = await permission.resolve_interrupt(ctx, ctx.inputs.tool_call, None)
        if mode == 'authorize':
            expected = {'allow': 'allow', 'deny': 'deny', 'clarify': 'ask', 'unavailable': 'ask'}[cloud_reply]
        if explicit is None and not permission_enabled and (not enabled or cloud_reply == 'unavailable'):
            expected = 'allow'
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
        assert permission._engine.config['strict_tool_command_merge'] is True
        assert config['permissions']['enabled'] is enabled


@pytest.mark.parametrize('desktop,cloud,expected', [
    (False, True, None), (True, False, False), (True, True, True), (True, 'true', False),
])
def test_strict_merge_is_owned_by_desktop_cloud_host(monkeypatch, desktop, cloud, expected):
    from copy import deepcopy
    from jiuwenswarm.common.behavior_security import runtime_permission_config

    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1' if desktop else '0')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    cfg = {'permissions': {'enabled': False, 'strict_tool_command_merge': True},
           'xiaoyi_work_security': {'cloud_authorization': {'enabled': cloud}}}
    original = deepcopy(cfg)
    runtime = runtime_permission_config(cfg)
    assert runtime.get('strict_tool_command_merge') is expected
    if desktop:
        assert runtime['defer_unmatched'] is True
    assert cfg == original


@pytest.mark.asyncio
@pytest.mark.parametrize('composed', [False, True])
async def test_cloud_hot_reload_survives_session_snapshot_refresh(monkeypatch, composed):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    stored = {'enabled': False, 'package_builtin_rules': False, 'tools': {'bash': 'allow'},
              'strict_tool_command_merge': False, 'defer_unmatched': False}
    if composed:
        stored['composition'] = {'schema_version': 1, 'revision': 'test-policy', 'source_order': ['user']}
    live_config = {}
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: live_config)
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
        lambda **_: stored.copy(),
    )
    b, events = bridge(enabled=False, decision='deny')
    adapter = SimpleNamespace(_permission_rail=None, _model=None, _cspl_sentinel_rail=BehaviorSecurityRail(b))
    for index, enabled in enumerate((True, False, True)):
        cfg = {'permissions': stored, 'xiaoyi_work_security': {'cloud_authorization': {'enabled': enabled}}}
        live_config.update(cfg)
        JiuWenSwarmDeepAdapter._update_permission_rail(adapter, cfg)
        assert not b.decisions and not b.local_decisions
        rail = adapter._permission_rail
        snapshot = rail._host.get_permissions_snapshot('test-session')
        assert snapshot['strict_tool_command_merge'] is enabled
        assert snapshot['defer_unmatched'] is True
        rail.update_config(snapshot)
        result = await rail._engine.check_permission('bash', {'command': 'demo x'})
        assert result.permission == (PermissionLevel.UNDETERMINED if enabled else PermissionLevel.ALLOW)
        event = {'version': 1, 'eventId': f'reload-{index}', 'stage': 'tool.before', 'sessionId': 'reload',
                 'tool': {'id': str(index), 'name': 'bash', 'arguments': {'command': 'demo x'}}}
        resolved = await b.decide(event, result)
        assert resolved.permission == (PermissionLevel.DENY if enabled else PermissionLevel.ALLOW)
        await asyncio.gather(*b.tasks)
    assert [event['mode'] for event in events] == ['authorize', 'report', 'authorize']
    await b.close()
    assert stored['strict_tool_command_merge'] is False


@pytest.mark.asyncio
async def test_composed_snapshot_reads_live_cloud_switch_before_rail_reload(monkeypatch):
    from copy import deepcopy
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from openjiuwen.harness.security.permission_engine.core import PermissionEngine

    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    live_config = {
        'permissions': {
            'enabled': True, 'package_builtin_rules': False, 'tools': {'bash': 'allow'},
            'composition': {'schema_version': 1, 'revision': 'test-policy', 'source_order': ['user']},
        },
        'xiaoyi_work_security': {'cloud_authorization': {'enabled': False}},
    }
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: live_config)
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
        lambda **_: deepcopy(live_config['permissions']),
    )
    rail = build_permission_rail(live_config)
    for enabled in (True, False, True):
        live_config['xiaoyi_work_security']['cloud_authorization']['enabled'] = enabled
        snapshot = rail._host.get_permissions_snapshot('test-session')
        assert snapshot['defer_unmatched'] is True
        assert snapshot['strict_tool_command_merge'] is enabled
        result = await PermissionEngine(snapshot).check_permission('bash', {'command': 'demo x'})
        assert result.permission == (PermissionLevel.UNDETERMINED if enabled else PermissionLevel.ALLOW)
        unmatched = await PermissionEngine(snapshot).check_permission('unlisted_tool', {})
        assert unmatched.permission == PermissionLevel.UNDETERMINED
    assert rail._engine.config['defer_unmatched'] is True
    assert 'strict_tool_command_merge' not in live_config['permissions']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_level,command,rules,remembered,reply,expected,mode', [
    ('allow', 'demo x', [], False, 'allow', 'allow', 'authorize'),
    ('allow', 'demo x', [], False, 'deny', 'deny', 'authorize'),
    ('allow', 'demo x', [], False, 'unavailable', 'allow', 'authorize'),
    ('allow', 'demo x', [('demo x', 'allow')], False, 'deny', 'allow', 'report'),
    ('ask', 'demo x', [('demo x', 'allow')], False, 'allow', 'ask', 'report'),
    ('deny', 'demo x', [('demo x', 'allow')], True, 'allow', 'deny', 'report'),
    ('allow', 'git status && npm run build', [('git status', 'allow')], False, 'allow', 'allow', 'authorize'),
    ('allow', 'git status && npm run build', [('git status && npm run build', 'allow')], False, 'deny', 'allow', 'report'),
    ('allow', 'demo x', [], True, 'deny', 'allow', 'report'),
])
async def test_strict_merge_through_permission_rail_and_cloud_bridge(
        monkeypatch, tool_level, command, rules, remembered, reply, expected, mode):
    from copy import deepcopy
    from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_CHANNEL_ID
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
    from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult, RejectResult
    from openjiuwen.harness.security.permission_engine.approve.operation_grants import build_operation_grant

    monkeypatch.setenv('CLAW_BEHAVIOR_SECURITY', '1')
    monkeypatch.setenv('CLAW_SKILL_TOKEN', 'test-token')
    args = {'command': command}
    cfg = {'enabled': True, 'package_builtin_rules': False, 'tools': {'bash': tool_level},
           'rules': [{'tools': ['shell'], 'pattern': p, 'action': a} for p, a in rules]}
    if remembered:
        cfg['approval_overrides'] = [build_operation_grant('bash', args, cfg)]
    monkeypatch.setattr(
        'jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist.get_permissions_with_session_overlay',
        lambda **_: deepcopy(cfg),
    )
    b, events = bridge(decision=reply)
    b.config['permissions'] = cfg
    waiting, release = asyncio.Event(), asyncio.Event()

    async def delayed(event):
        events.append(event)
        waiting.set()
        await release.wait()
        return {'decision': reply}

    b._send = AsyncMock(side_effect=delayed)
    ctx = context()
    ctx.inputs.tool_call = ToolCall(id='strict-call', type='function', name='bash', arguments=json.dumps(args))
    ctx.inputs.tool_name, ctx.inputs.tool_args = 'bash', args
    behavior = BehaviorSecurityRail(b)
    await behavior.before_tool_call(ctx)
    channel = CURRENT_CHANNEL_ID.set('desktop')
    pending = None
    try:
        permission = build_permission_rail(b.config)
        pending = asyncio.create_task(permission.resolve_interrupt(ctx, ctx.inputs.tool_call, None))
        await asyncio.wait_for(waiting.wait(), 2)
        if mode == 'authorize':
            assert not pending.done(), 'Execution permission must wait for the cloud reply'
            assert ctx.extra['behavior.local_permission'].is_undetermined
        else:
            await asyncio.wait_for(asyncio.shield(pending), 2)
        release.set()
        result = await asyncio.wait_for(pending, 2)
        assert isinstance(result, {'allow': ApproveResult, 'deny': RejectResult, 'ask': InterruptResult}[expected])
        await asyncio.gather(*b.tasks)
        assert len(events) == 1 and events[0]['mode'] == mode
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        CURRENT_CHANNEL_ID.reset(channel)
        await behavior.after_tool_call(ctx)
        await b.close()


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
