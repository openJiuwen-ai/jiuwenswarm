"""Message-bound group routing stays isolated from shared agent history."""

import asyncio
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, UserMessageInputs, SteeringInput
from openjiuwen.core.common.run_context import RunContext
from jiuwenswarm.agents.swarm import group_turn as turns
from jiuwenswarm.agents.swarm import group_execution as policy


MEMBERS = [
    {"member_name": "business", "kind": "agent_group"},
    {"member_name": "architecture", "kind": "agent_group"},
    {"member_name": "writer", "kind": "agent_template"},
]
GROUP = {"members": MEMBERS}


def notice(message_id, language="en"):
    if language == "cn":
        return f"群聊消息通知。\n本次群聊时间范围（毫秒）：(0, 100]。\n同时包含本次 @ 的触发消息：{message_id}。\n群聊历史文件：ignored\n"
    return f"Group message notice.\nGroup history time range (milliseconds): (0, 100].\nThe triggering mention is also included: {message_id}.\nGroup history file: ignored\n"


def backend(tmp_path, rows):
    path = tmp_path / "history.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))

    async def conversation():
        return SimpleNamespace(history_path=path)

    return SimpleNamespace(group_conversation=conversation)


def row(message_id, mentions=None):
    return {"message_id": message_id, "client_message_id": f"client-{message_id}", "sender": "user",
            "mentions": mentions if mentions is not None else ["business", "architecture"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
async def test_exact_consumed_notice_wins_over_latest_history(tmp_path, language):
    source = backend(tmp_path, [row("a"), row("b"), row("c")])
    turn = await turns.resolve_group_turn(source, "session", "business", GROUP, [notice("b", language)])
    assert turn.client_message_id == "client-b"
    assert turn.route == "organization"
    with pytest.raises(FrozenInstanceError):
        turn.client_message_id = "client-c"


@pytest.mark.asyncio
@pytest.mark.parametrize("mentions", [["business"], ["business", "writer"], ["business", "architecture", "writer"]])
async def test_single_and_mixed_mentions_stay_direct(tmp_path, mentions):
    source = backend(tmp_path, [row("direct", mentions), row("later-org")])
    turn = await turns.resolve_group_turn(source, "session", "business", GROUP, [notice("direct")])
    assert turn.route == "direct"
    assert turn.client_message_id == "client-direct"
    assert turn.mixed_mentions == ("writer" in mentions)


@pytest.mark.asyncio
async def test_unknown_binding_never_guesses_a_pending_or_latest_message(tmp_path):
    source = backend(tmp_path, [row("a"), row("other", ["writer"])])
    for parts in [["ordinary internal input"], [notice("missing")], [notice("other")], [notice("a"), notice("other")]]:
        turn = await turns.resolve_group_turn(source, "session", "business", GROUP, parts)
        assert turn.route == "unknown"
        assert turn.client_message_id is None


@pytest.mark.asyncio
async def test_org_batch_keeps_unconsumed_input_before_later_arrivals(tmp_path):
    source = backend(tmp_path, [row("a"), row("b"), row("c")])
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notice("a"), notice("b")]))
    ctx.bind_steering_queue(asyncio.Queue())
    ctx.push_steering(notice("c"))
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.inputs.parts == [notice("a")]
    assert ctx.extra[turns.TURN_KEY].client_message_id == "client-a"
    assert ctx.drain_steering() == [notice("b"), notice("c")]


@pytest.mark.asyncio
async def test_direct_batches_are_not_split(tmp_path):
    source = backend(tmp_path, [row("a", ["business"]), row("b", ["business", "writer"])])
    parts = [notice("a"), notice("b")]
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=list(parts)))
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.inputs.parts == parts
    assert ctx.steering_queue is None
    assert ctx.extra[turns.TURN_KEY].route == "direct"
    assert ctx.extra[turns.TURN_KEY].mixed_mentions


@pytest.mark.asyncio
async def test_org_batch_preserves_contextual_backlog_and_lookahead(tmp_path):
    source = backend(tmp_path, [row("a"), row("b")])
    context_a = RunContext(session_id="a")
    context_c = RunContext(session_id="c")
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notice("a"), notice("b")]),
                               extra={"run_context": context_a})
    ctx.bind_steering_queue(asyncio.Queue())
    ctx.push_steering(SteeringInput("context-c", run_context=context_c))
    ctx.push_steering(SteeringInput("explicit-none", run_context=None))
    ctx.push_steering("raw")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.extra["run_context"] is context_a
    assert ctx.drain_steering() == [notice("b")]
    assert ctx.extra["run_context"] is context_a
    assert ctx.drain_steering() == ["context-c"]
    assert ctx.extra["run_context"] is context_c
    assert ctx.drain_steering() == ["explicit-none"]
    assert ctx.extra["run_context"] is None
    assert ctx.drain_steering() == ["raw"]
    assert not ctx.has_pending_steering()


@pytest.mark.asyncio
async def test_tool_context_snapshot_survives_new_input_and_parallel_calls(monkeypatch):
    monkeypatch.setattr(policy, "read_group", lambda _: GROUP)
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id="session", member_name="business"))
    a = turns.GroupTurn("session", "business", "a", "organization")
    b = turns.GroupTurn("session", "business", "b", "direct")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def call(turn, failure):
        ctx = SimpleNamespace(
            inputs=SimpleNamespace(tool_name="run_team", tool_result={"success": True, "data": {"accepted": True}}),
            extra={turns.TURN_KEY: turn},
        )
        await rail.before_tool_call(ctx)
        if turn is a:
            entered.set()
            await release.wait()
        else:
            release.set()
        assert turns.current_group_turn("session", "business") is turn
        assert turns.current_group_turn("other", "business") is None
        if failure:
            await rail.on_tool_exception(ctx)
        else:
            await rail.after_tool_call(ctx)
        assert turns.current_group_turn("session", "business") is None

    first = asyncio.create_task(call(a, False))
    await entered.wait()
    await call(b, True)
    await first


@pytest.mark.asyncio
async def test_current_org_prompt_does_not_follow_archive_or_leak_to_direct_turn():
    builder = Mock()
    ctx = SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder), extra={
        turns.TURN_KEY: turns.GroupTurn("session", "business", "message-b", "organization"),
    })
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id="session", member_name="business"))
    await rail.before_model_call(ctx)
    content = builder.add_section.call_args.args[0].content["en"]
    assert '"plan_id": "message-b"' in content
    assert "not replacement instructions" in content
    ctx.extra[turns.TURN_KEY] = turns.GroupTurn("session", "business", "message-c", "direct")
    builder.reset_mock()
    await rail.before_model_call(ctx)
    builder.remove_section.assert_called_once_with("group_current_organization_turn")
    builder.add_section.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "organization", None])
@pytest.mark.parametrize("source", ["resume", "steering"])
async def test_internal_and_resume_inputs_preserve_binding_via_real_rail(monkeypatch, route, source):
    monkeypatch.setattr(policy, "read_group", lambda _: GROUP)
    monkeypatch.setattr(policy, "get_team_backend", lambda _: None)
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id="session", member_name="business"))
    turn = turns.GroupTurn("session", "business", "original-message", route) if route else None
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=["approved, continue"], source=source))
    if turn:
        ctx.extra[turns.TURN_KEY] = turn
    await rail.on_user_message(ctx)
    assert ctx.extra.get(turns.TURN_KEY) is turn


@pytest.mark.asyncio
async def test_malformed_notice_cannot_reuse_a_previous_organization_binding(monkeypatch):
    monkeypatch.setattr(policy, "read_group", lambda _: GROUP)
    monkeypatch.setattr(policy, "get_team_backend", lambda _: None)
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id="session", member_name="business"))
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=["Group message notice.\nMissing trigger header"]))
    ctx.extra[turns.TURN_KEY] = turns.GroupTurn("session", "business", "old", "organization")
    await rail.on_user_message(ctx)
    assert ctx.extra[turns.TURN_KEY].route == "unknown"
    assert ctx.extra[turns.TURN_KEY].client_message_id is None


@pytest.mark.asyncio
async def test_new_org_waits_for_current_proxy_handoff_without_starting_it(tmp_path, monkeypatch):
    accepted = set()
    monkeypatch.setattr(turns, "_unaccepted_organization", lambda turn: turn is not None and turn.route == "organization" and turn.client_message_id not in accepted)
    source = backend(tmp_path, [row("a"), row("b")])
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notice("a")]))
    ctx.bind_steering_queue(asyncio.Queue())
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    ctx.inputs = UserMessageInputs(parts=[notice("b")], source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.inputs.parts == []
    assert ctx.extra[turns.TURN_KEY].client_message_id == "client-a"
    assert ctx.has_pending_steering()
    accepted.add("client-a")
    ctx.inputs = UserMessageInputs(parts=ctx.drain_steering(), source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.extra[turns.TURN_KEY].client_message_id == "client-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["run_team", "group_send_message", "model"])
async def test_direct_interruption_keeps_its_route_then_resumes_unaccepted_org(tmp_path, monkeypatch, completion):
    monkeypatch.setattr(turns, "_unaccepted_organization", lambda turn: turn is not None and turn.route == "organization")
    source = backend(tmp_path, [row("a"), row("direct", ["business"]), row("b")])
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notice("a")]))
    ctx.bind_steering_queue(asyncio.Queue())
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    ctx.inputs = UserMessageInputs(parts=[notice("direct")], source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.extra[turns.TURN_KEY].route == "direct"
    ctx.inputs = UserMessageInputs(parts=[notice("b")], source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.inputs.parts == []
    assert ctx.extra[turns.TURN_KEY].client_message_id == "client-direct"
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id="session", member_name="business"))
    if completion == "model":
        ctx.inputs = SimpleNamespace(response=SimpleNamespace(tool_calls=[]))
        await rail.after_model_call(ctx)
    else:
        from openjiuwen.core.foundation.tool import ToolOutput

        ctx.inputs = SimpleNamespace(tool_name=completion, tool_result=ToolOutput(success=False, error="retry"))
        await rail.after_tool_call(ctx)
        assert len(ctx.extra[turns.STATE_KEY]["suspended"]) == 1
        ctx.inputs.tool_result = ToolOutput(success=True, data={"accepted": True})
        await rail.after_tool_call(ctx)
    assert ctx.drain_steering() == [notice("a"), notice("b")]
    assert ctx.extra[turns.STATE_KEY]["suspended"] == []


@pytest.mark.asyncio
async def test_direct_notice_behind_blocked_org_in_same_batch_is_still_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(turns, "_unaccepted_organization", lambda turn: turn is not None and turn.route == "organization")
    source = backend(tmp_path, [row("a"), row("b"), row("direct", ["business", "writer"])])
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notice("a")]))
    ctx.bind_steering_queue(asyncio.Queue())
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    ctx.inputs = UserMessageInputs(parts=[notice("b"), notice("direct")], source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert ctx.inputs.parts == [notice("direct")]
    assert ctx.extra[turns.TURN_KEY].route == "direct"
    assert len(ctx.extra[turns.STATE_KEY]["suspended"]) == 1
    ctx.inputs = UserMessageInputs(parts=[notice("direct")], source="steering")
    await turns.bind_consumed_group_turn(ctx, source, "session", "business", GROUP)
    assert len(ctx.extra[turns.STATE_KEY]["suspended"]) == 1
    assert ctx.drain_steering() == [notice("b")]


@pytest.mark.parametrize("status", ["failed", "expired", "done"])
def test_terminal_plan_never_holds_later_organization(monkeypatch, status):
    from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "recorded_organization_plan", lambda *_: {"status": status})
    monkeypatch.setattr(bootstrap, "organization_plan_accepted", lambda *_: False)
    assert not turns._unaccepted_organization(turns.GroupTurn("session", "business", "old", "organization"))


def inbound(message_id):
    from openjiuwen.agent_teams.inbound_render import render_inbound

    return render_inbound(content='User request, not routing metadata', sender='user',
                          message_id=message_id, msg_type='broadcast', time_info='2026-11-09 16:05')


async def mailbox_backend(tmp_path, monkeypatch, rows):
    from unittest.mock import AsyncMock
    from openjiuwen.agent_teams.group_chat import handler

    source = backend(tmp_path, rows)
    conversation = await source.group_conversation()
    conversation.session_id = 'session'
    source.group_conversation = AsyncMock(return_value=conversation)
    source.team_name = 'outer-team'
    messages = [SimpleNamespace(
        message_id=r['message_id'], team_name=source.team_name, from_member_name='user',
        broadcast=True, content='User request', timestamp=100 + i,
        meta=json.dumps(dict(type='group_chat', client_message_id=r['client_message_id'],
                             mentions=r['mentions'], sender_name='user', attachments=[])),
    ) for i, r in enumerate(rows)]
    source.db = SimpleNamespace(message=SimpleNamespace(
        get_message=AsyncMock(side_effect=lambda key: next((m for m in messages if m.message_id == key), None)),
        get_broadcast_read_at=AsyncMock(return_value=0),
    ))
    source.message_manager = object()
    source.group_chat_spec = SimpleNamespace(language='cn')
    monkeypatch.setattr(handler, 'sync_history', AsyncMock(return_value=[
        handler.conversation_message(m, 'session') for m in messages
    ]))
    return source, messages


@pytest.mark.asyncio
@pytest.mark.parametrize('mentions,expected', [
    (['business', 'architecture'], 'organization'),
    (['business'], 'direct'), (['writer'], 'direct'),
    (['business', 'writer'], 'direct'),
    (['business', 'architecture', 'writer'], 'direct'),
])
async def test_public_mailbox_envelope_uses_verified_mentions(tmp_path, monkeypatch, mentions, expected):
    source, _ = await mailbox_backend(tmp_path, monkeypatch, [row('a', mentions), row('later')])
    member = mentions[0]
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[inbound('a')]))
    await turns.bind_consumed_group_turn(ctx, source, 'session', member, GROUP)
    turn = ctx.extra[turns.TURN_KEY]
    assert (turn.client_message_id, turn.route) == ('client-a', expected)
    assert turns.is_group_notice(ctx.inputs.parts[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'team', 'sender', 'broadcast', 'meta', 'mentions', 'client'])
async def test_unverified_mailbox_cannot_reuse_old_binding(tmp_path, monkeypatch, bad):
    source, messages = await mailbox_backend(tmp_path, monkeypatch, [row('a')])
    message = messages[0]
    if bad == 'team':
        message.team_name = 'other-team'
    elif bad == 'sender':
        message.from_member_name = 'peer'
    elif bad == 'broadcast':
        message.broadcast = False
    elif bad in {'meta', 'mentions', 'client'}:
        meta = json.loads(message.meta)
        if bad == 'meta':
            meta['type'] = 'ordinary'
        elif bad == 'mentions':
            meta['mentions'] = ['writer']
        else:
            meta['client_message_id'] = None
        message.meta = json.dumps(meta)
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[inbound('missing' if bad == 'missing' else 'a')]))
    ctx.extra[turns.TURN_KEY] = turns.GroupTurn('session', 'business', 'old', 'organization')
    await turns.bind_consumed_group_turn(ctx, source, 'session', 'business', GROUP)
    assert ctx.extra[turns.TURN_KEY].route == 'unknown'
    assert ctx.extra[turns.TURN_KEY].client_message_id is None


@pytest.mark.asyncio
async def test_mailbox_organizations_keep_batch_order_and_do_not_parse_body(tmp_path, monkeypatch):
    source, _ = await mailbox_backend(tmp_path, monkeypatch, [row('a'), row('b')])
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[inbound('a'), notice('b')]))
    await turns.bind_consumed_group_turn(ctx, source, 'session', 'business', GROUP)
    assert ctx.extra[turns.TURN_KEY].client_message_id == 'client-a'
    assert ctx.drain_steering() == [notice('b')]
    ctx.inputs = UserMessageInputs(parts=['An internal reminder quoting:\n' + inbound('b')])
    await turns.bind_consumed_group_turn(ctx, source, 'session', 'business', GROUP)
    assert ctx.extra[turns.TURN_KEY].client_message_id == 'client-a'
