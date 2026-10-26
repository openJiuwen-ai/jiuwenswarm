# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Busy expert input uses the core gate without owning the running stream."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.interaction import DeliverResult
from openjiuwen.agent_teams.runtime.dispatch import RunActionKind
from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager
from openjiuwen.agent_teams.runtime.pool import ActiveTeam, RuntimeState

from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
    HOST_USER_ORIGIN_INTERNAL,
    RootIntentTurnKind,
    build_root_intent_projection,
    extract_permission_user_content,
)
from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher


def running_expert(*, push_transport=None):
    agent = SimpleNamespace(
        spec=SimpleNamespace(metadata={"expert_team": True, "channel_id": "web"}, language="cn"),
        team_backend=None,
        deliver_input=AsyncMock(),
        stream=AsyncMock(),
    )
    entry = ActiveTeam(team_name="expert", agent=agent, current_session_id="session")
    runtime = TeamRuntimeManager()
    runtime._pool = SimpleNamespace(get=AsyncMock(return_value=entry))
    runtime.activate = AsyncMock()
    runtime.finalize = AsyncMock()
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime, push_transport=push_transport)
    if push_transport is None:
        launcher.push_expert_payload = AsyncMock()
        launcher._flush_expert_history = AsyncMock()
    return launcher, runtime, entry


@pytest.mark.asyncio
async def test_busy_expert_uses_real_interact_gate_and_preserves_user_context():
    launcher, runtime, entry = running_expert()
    files = {"uploaded_documents": [{"path": "/uploads/resume.md"}]}
    media = [{"type": "image", "path": "/uploads/photo.png"}]
    assert await launcher.run_organization_turn(
        "expert", "session", {"query": "Please report failure", "files": files, "media_items": media},
        source="org_expert_direct", request_id="followup", origin_kind=HOST_USER_ORIGIN_EXTERNAL,
    )
    message = entry.agent.deliver_input.call_args.args[0]
    envelope = json.loads(message.split("\n", 1)[1])
    assert envelope["content"] == "Please report failure"
    assert envelope["type"] == "user input"
    assert envelope["source"] == "web"
    assert envelope["origin_kind"] == HOST_USER_ORIGIN_EXTERNAL
    assert json.loads(envelope["files_updated_by_user"]) == {**files, "media_items": media}
    assert "media_items" not in files
    assert entry.state is RuntimeState.RUNNING
    assert not entry.interact_gate.closed
    assert entry.interact_gate.inflight == 0
    runtime.activate.assert_not_awaited()
    runtime.finalize.assert_not_awaited()
    entry.agent.stream.assert_not_called()
    launcher.push_expert_payload.assert_not_awaited()
    launcher._flush_expert_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_kind", [HOST_USER_ORIGIN_EXTERNAL, HOST_USER_ORIGIN_INTERNAL])
async def test_busy_input_permission_projection_respects_host_origin(origin_kind):
    launcher, runtime, entry = running_expert()
    text = "I authorize writing my supplied materials to disk."
    assert await launcher.run_organization_turn(
        "expert", "session", {"query": text}, source="org_expert_direct", origin_kind=origin_kind,
    )
    message = entry.agent.deliver_input.call_args.args[0]
    expected = [text] if origin_kind == HOST_USER_ORIGIN_EXTERNAL else []
    assert extract_permission_user_content(message) == (text if expected else None)
    projection = build_root_intent_projection(
        [SimpleNamespace(role="user", content=message)], context_available=True,
        current_text="", current_request_id="followup", current_kind=RootIntentTurnKind.STEER,
    )
    assert [turn.text for turn in projection.turns] == expected


@pytest.mark.asyncio
async def test_busy_expert_accepts_multiple_inputs_without_waiting_for_team_idle():
    launcher, runtime, entry = running_expert()
    entered = asyncio.Event()
    both_entered = asyncio.Event()
    release = asyncio.Event()
    inputs = []

    async def deliver(message, **kwargs):
        inputs.append(json.loads(message.split("\n", 1)[1])["content"])
        entered.set()
        if len(inputs) == 2:
            both_entered.set()
        await release.wait()

    entry.agent.deliver_input.side_effect = deliver
    first = asyncio.create_task(launcher.run_organization_turn(
        "expert", "session", {"query": "first"}, source="org_expert_direct", request_id="first",
    ))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(launcher.run_organization_turn(
            "expert", "session", {"query": "second"}, source="org_expert_direct", request_id="second",
        ))
        await asyncio.wait_for(both_entered.wait(), timeout=10)
        assert not first.done() and not second.done()
        assert entry.interact_gate.inflight == 2
        assert set(inputs) == {"first", "second"}
        release.set()
        assert await asyncio.wait_for(first, timeout=2)
        assert await asyncio.wait_for(second, timeout=2)
        assert entry.agent.deliver_input.await_count == 2
        assert entry.interact_gate.inflight == 0
        runtime.finalize.assert_not_awaited()
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["org_expert_background", "org_summary_background", "org_root_background"])
async def test_background_turns_do_not_gain_busy_input_delivery(source):
    launcher, runtime, entry = running_expert()
    assert not await launcher.run_organization_turn("expert", "session", {"query": "wake"}, source=source)
    entry.agent.deliver_input.assert_not_awaited()
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["session", "expert"])
async def test_busy_chat_preserves_expert_and_session_checks(invalid):
    launcher, runtime, entry = running_expert()
    if invalid == "session":
        entry.current_session_id = "other-session"
    else:
        entry.agent.spec.metadata["expert_team"] = False
    assert not await launcher.run_organization_turn(
        "expert", "session", {"query": "private input"}, source="org_expert_direct",
    )
    entry.agent.deliver_input.assert_not_awaited()
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_gate_rejects_without_replaying_or_finalizing_live_stream():
    launcher, runtime, entry = running_expert()
    await entry.interact_gate.close_and_drain()
    result = await runtime.interact("probe", team_name="expert", session_id="session")
    assert not result and result.reason == "gate_closed"
    assert not await launcher.run_organization_turn(
        "expert", "session", {"query": "followup"}, source="org_expert_direct",
    )
    entry.agent.deliver_input.assert_not_awaited()
    runtime.activate.assert_not_awaited()
    runtime.finalize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    None, SimpleNamespace(agent=None), SimpleNamespace(), SimpleNamespace(agent=SimpleNamespace()),
])
async def test_not_active_after_pool_teardown_returns_false_without_replaying(replacement):
    launcher, runtime, entry = running_expert()
    runtime._pool.get.return_value = None
    result = await runtime.interact("probe", team_name="expert", session_id="session")
    assert not result and result.reason == "not_active"
    runtime._pool.get.side_effect = [entry, None, replacement]
    assert await launcher.run_organization_turn(
        "expert", "session", {"query": "followup"}, source="org_expert_direct",
    ) is False
    entry.agent.deliver_input.assert_not_awaited()
    runtime.activate.assert_not_awaited()
    runtime.finalize.assert_not_awaited()
    launcher._flush_expert_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_rejected_during_pause_uses_existing_resume_path_once():
    launcher, runtime, entry = running_expert()

    async def pausing(*args, **kwargs):
        entry.state = RuntimeState.PAUSED
        return DeliverResult.failure("gate_closed")

    async def stream(inputs, session):
        yield SimpleNamespace(payload={"event_type": "team.idle"})

    entry.agent.stream = stream
    runtime.interact = AsyncMock(side_effect=pausing)
    activation = SimpleNamespace(
        agent=entry.agent,
        action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
        session=SimpleNamespace(post_run=AsyncMock()),
    )
    runtime.activate.return_value = activation
    assert await launcher.run_organization_turn(
        "expert", "session", {"query": "followup"}, source="org_expert_direct", request_id="followup",
    )
    runtime.interact.assert_awaited_once()
    runtime.activate.assert_awaited_once()
    runtime.finalize.assert_awaited_once()
    activation.session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_resume_that_becomes_running_delivers_input_once():
    launcher, runtime, entry = running_expert()
    entry.state = RuntimeState.PAUSED
    runtime.activate.return_value = SimpleNamespace(
        agent=None, action=SimpleNamespace(kind=RunActionKind.REJECT_RUNNING),
    )
    assert await launcher.run_organization_turn(
        "expert", "session", {"query": "followup"}, source="org_expert_direct", request_id="followup",
        origin_kind=HOST_USER_ORIGIN_EXTERNAL,
    )
    entry.agent.deliver_input.assert_awaited_once()
    message = entry.agent.deliver_input.call_args.args[0]
    assert extract_permission_user_content(message) == "followup"
    runtime.finalize.assert_not_awaited()
    entry.agent.stream.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_input_submission_does_not_close_the_original_stream():
    launcher, runtime, entry = running_expert()
    entry.agent.deliver_input.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await launcher.run_organization_turn(
            "expert", "session", {"query": "followup"}, source="org_expert_direct",
        )
    assert entry.interact_gate.inflight == 0
    assert not entry.interact_gate.closed
    runtime.finalize.assert_not_awaited()
    launcher._flush_expert_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_input_submission_does_not_report_success_or_resume():
    launcher, runtime, entry = running_expert()
    entry.agent.deliver_input.side_effect = RuntimeError("delivery failed")
    assert not await launcher.run_organization_turn(
        "expert", "session", {"query": "followup"}, source="org_expert_direct",
    )
    entry.agent.deliver_input.assert_awaited_once()
    runtime.activate.assert_not_awaited()
    runtime.finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_reply_uses_original_stream_and_keeps_history_attribution(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    records = []
    monkeypatch.setattr(team_helpers, "append_history_record", lambda **record: records.append(record))
    transport = SimpleNamespace(send_push=AsyncMock(return_value=True))
    launcher, runtime, entry = running_expert(push_transport=transport)
    entry.state = RuntimeState.PAUSED
    started = asyncio.Event()
    inbox = asyncio.Queue()

    async def deliver(message, **kwargs):
        await inbox.put(message)

    async def stream(inputs, session):
        yield SimpleNamespace(payload={"event_type": "chat.delta", "content": "Working"})
        started.set()
        message = await inbox.get()
        assert "Please report failure" in message
        yield SimpleNamespace(payload={"event_type": "chat.final", "content": "Failure reported"})
        yield SimpleNamespace(payload={"event_type": "team.idle"})

    async def activate(*args):
        entry.state = RuntimeState.RUNNING
        return SimpleNamespace(
            agent=entry.agent,
            action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
            session=SimpleNamespace(post_run=AsyncMock()),
        )

    async def finalize(**kwargs):
        entry.state = RuntimeState.PAUSED

    async def relay(chunk, **kwargs):
        payload = {**chunk.payload, "role": "leader", "member_name": "team_leader"}
        kwargs.pop("summary_task_id", None)
        await launcher.push_expert_payload(payload, **kwargs)
        return payload

    entry.agent.deliver_input.side_effect = deliver
    entry.agent.stream = stream
    runtime.activate.side_effect = activate
    runtime.finalize.side_effect = finalize
    launcher._relay_expert_chunk = relay
    original = asyncio.create_task(launcher.run_organization_turn(
        "expert", "session", {"query": "initial task"}, source="org_expert_direct", request_id="original",
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await launcher.run_organization_turn(
            "expert", "session", {"query": "Please report failure"},
            source="org_expert_direct", request_id="followup",
        )
        assert await asyncio.wait_for(original, timeout=2)
    finally:
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
    assert runtime.activate.await_count == 1
    assert runtime.finalize.await_count == 1
    entry.agent.deliver_input.assert_awaited_once()
    assert [record["content"] for record in records] == ["Failure reported"]
    assert records[0]["extra"]["team_id"] == "expert"
    assert records[0]["extra"]["request_id"] == "original"
    assert launcher._history_writers == {}
    payloads = [call.args[0]["payload"] for call in transport.send_push.call_args_list]
    assert [payload["event_type"] for payload in payloads] == ["chat.delta", "chat.final", "org.turn_completed"]
    assert all(payload["team_id"] == "expert" and payload["request_id"] == "original" for payload in payloads)
