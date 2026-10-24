# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Organization replies must survive browser reloads and disconnected pushes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.runtime.session.history_io import run_history_io


@pytest.fixture
def history_records(monkeypatch):
    records = []

    def append_record(**record):
        records.append(record)

    monkeypatch.setattr(team_helpers, "append_history_record", append_record)
    return records


async def push(
    launcher: JiuwenExpertTeamLauncher,
    event_type: str,
    content: str = "",
    *,
    source: str = "org_expert_background",
    member: str = "team_leader",
    role: str = "leader",
    team: str = "expert",
    round_id: str = "round",
) -> bool:
    return await launcher.push_expert_payload(
        {"event_type": event_type, "content": content, "member_name": member, "role": role},
        team_id=team,
        session_id="session",
        channel_id="web",
        round_id=round_id,
        frame_sequence=0,
        source=source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("source", [
    "org_expert_direct", "org_expert_background", "org_summary_background",
    "org_root_background", "org_root_delivery",
])
async def test_replies_are_saved_before_push_even_when_disconnected(history_records, source, delivered):
    async def send_push(message):
        assert len(history_records) == 1
        return delivered

    launcher = JiuwenExpertTeamLauncher(push_transport=SimpleNamespace(send_push=send_push))
    if source == "org_expert_direct" and not delivered:
        with pytest.raises(RuntimeError, match="failed to deliver direct expert output"):
            await push(launcher, "chat.final", "saved reply", source=source)
    else:
        assert await push(launcher, "chat.final", "saved reply", source=source) is delivered
    assert len(history_records) == 1
    record = history_records[0]
    assert record["content"] == "saved reply"
    assert record["mode"] == "team"
    assert record["extra"]["team_id"] == "expert"
    assert record["extra"]["source"] == source
    assert record["extra"]["request_id"] == "round"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["org_expert_direct", "org_expert_background"])
async def test_transport_exception_does_not_lose_saved_reply(history_records, source):
    transport = SimpleNamespace(send_push=AsyncMock(side_effect=OSError("disconnected")))
    launcher = JiuwenExpertTeamLauncher(push_transport=transport)
    if source == "org_expert_direct":
        with pytest.raises(RuntimeError, match="failed to deliver direct expert output"):
            await push(launcher, "chat.final", "saved reply", source=source)
    else:
        assert await push(launcher, "chat.final", "saved reply", source=source) is False
    assert [record["content"] for record in history_records] == ["saved reply"]


@pytest.mark.asyncio
async def test_buffered_output_keeps_members_and_teams_separate(history_records):
    launcher = JiuwenExpertTeamLauncher(push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)))
    await push(launcher, "chat.delta", "leader ")
    await push(launcher, "chat.reasoning", "member reasoning", member="analyst", role="teammate")
    await push(launcher, "chat.delta", "member reply", member="analyst", role="teammate")
    await push(launcher, "chat.delta", "other team", team="summary")
    await push(launcher, "chat.delta", "reply")
    await push(launcher, "org.turn_completed")
    assert [record["content"] for record in history_records] == ["leader reply", "member reply"]
    leader, member = history_records
    assert leader["extra"]["member_name"] == "team_leader"
    assert member["extra"]["member_name"] == "analyst"
    assert member["extra"]["role"] == "teammate"
    assert member["extra"]["reasoning_content"] == "member reasoning"
    assert all(record["extra"]["team_id"] == "expert" for record in history_records)
    await push(launcher, "org.turn_completed", team="summary")
    assert history_records[-1]["content"] == "other team"
    assert history_records[-1]["extra"]["team_id"] == "summary"
    assert len({record["extra"]["id"] for record in history_records}) == 3
    assert launcher._history_writers == {}


@pytest.mark.asyncio
async def test_empty_finals_and_tool_boundaries_preserve_attribution_without_duplicates(history_records):
    launcher = JiuwenExpertTeamLauncher(push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)))
    await push(launcher, "chat.delta", "first segment")
    await push(launcher, "chat.tool_call", "tool")
    await push(launcher, "chat.delta", "second segment")
    await push(launcher, "chat.final")
    await push(launcher, "chat.final")
    await push(launcher, "chat.reasoning", "trailing reasoning")
    await push(launcher, "org.turn_completed")
    finals = [record for record in history_records if record["event_type"] == "chat.final"]
    assert [record["content"] for record in finals] == ["first segment", "second segment"]
    assert len({record["extra"]["id"] for record in history_records}) == len(history_records)
    assert all(record["extra"]["team_id"] == "expert" for record in history_records)
    assert history_records[-1]["extra"]["reasoning_content"] == "trailing reasoning"
    assert launcher._history_writers == {}


@pytest.mark.asyncio
async def test_finishing_one_round_does_not_flush_another_round(history_records):
    launcher = JiuwenExpertTeamLauncher(push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)))
    await push(launcher, "chat.delta", "first", round_id="first")
    await push(launcher, "chat.delta", "second", round_id="second")
    await push(launcher, "org.turn_completed", round_id="first")
    assert [record["content"] for record in history_records] == ["first"]
    await push(launcher, "org.turn_completed", round_id="second")
    assert [record["content"] for record in history_records] == ["first", "second"]
    assert launcher._history_writers == {}


@pytest.mark.asyncio
async def test_history_failure_does_not_interrupt_live_output(monkeypatch):
    def fail_append(**record):
        raise OSError("history unavailable")

    monkeypatch.setattr(team_helpers, "append_history_record", fail_append)
    transport = SimpleNamespace(send_push=AsyncMock(return_value=True))
    launcher = JiuwenExpertTeamLauncher(push_transport=transport)
    assert await push(launcher, "chat.final", "live reply") is True
    transport.send_push.assert_awaited_once()
    await push(launcher, "org.turn_completed")
    assert launcher._history_writers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_or_cancelled_turn_flushes_and_releases_history(history_records, cancelled):
    from openjiuwen.agent_teams.runtime.dispatch import RunActionKind
    from openjiuwen.agent_teams.runtime.pool import RuntimeState

    async def stream(inputs, session):
        yield SimpleNamespace(payload={"event_type": "chat.delta", "content": "partial reply"})
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("turn failed")

    agent = SimpleNamespace(spec=SimpleNamespace(metadata={"expert_team": True, "channel_id": "web"}), stream=stream)
    entry = SimpleNamespace(agent=agent, current_session_id="session", state=RuntimeState.PAUSED)
    activation = SimpleNamespace(
        agent=agent, action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
        session=SimpleNamespace(post_run=AsyncMock()),
    )
    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=AsyncMock(return_value=entry)),
        activate=AsyncMock(return_value=activation), finalize=AsyncMock(),
    )
    launcher = JiuwenExpertTeamLauncher(
        runtime_manager=runtime, push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)),
    )

    async def relay(chunk, **kwargs):
        await push(launcher, "chat.delta", chunk.payload["content"], round_id=kwargs["round_id"])
        return {"event_type": "chat.delta", "content": chunk.payload["content"], "role": "leader"}

    launcher._relay_expert_chunk = relay
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await launcher.run_organization_turn("expert", "session", {"query": "work"}, request_id="round")
    else:
        assert await launcher.run_organization_turn("expert", "session", {"query": "work"}, request_id="round") is False
    finals = [record for record in history_records if record["event_type"] == "chat.final"]
    assert [record["content"] for record in finals] == ["partial reply"]
    assert launcher._history_writers == {}
    runtime.finalize.assert_awaited_once()
    activation.session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_replies_can_be_read_after_recreating_the_launcher():
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    launcher = JiuwenExpertTeamLauncher(push_transport=SimpleNamespace(send_push=AsyncMock(return_value=False)))
    await push(launcher, "chat.final", "expert result")
    await push(launcher, "chat.final", "member result", member="analyst", role="teammate")
    await push(launcher, "org.turn_completed")
    assert await run_history_io(session_history.flush_pending_writes)
    # Reload from the history file, without relying on the old launcher's buffers.
    restored = JiuwenExpertTeamLauncher()
    assert restored._history_writers == {}
    records = await run_history_io(session_history.load_history_records, "session")
    assert [record["content"] for record in records] == ["expert result", "member result"]
    assert all(record["team_id"] == "expert" for record in records)
    assert len({record["id"] for record in records}) == 2
    page = await run_history_io(AgentWebSocketServer.get_conversation_history, "session", 1)
    assert {record["content"] for record in page["messages"]} == {"expert result", "member result"}
