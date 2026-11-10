"""Team meeting acceptance scenarios with a real queue and simulated browser audio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio
from openjiuwen.agent_teams.messager import InProcessMessager
from openjiuwen.agent_teams.schema.events import EventMessage, TeamEvent
from openjiuwen.agent_teams.schema.meeting import MeetingRoute, SpeechRequestStatus
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend

from jiuwenswarm.common.voice_matching import load_tts_voice_catalog
from jiuwenswarm.extensions.meeting import meeting_manager
from jiuwenswarm.extensions.meeting.meeting_manager import MeetingManager
from jiuwenswarm.extensions.meeting.publication import MeetingSetAddressedMembersTool


SESSION = "meeting-lifecycle-session"
TEAM = "meeting-lifecycle-team"
LEADER = "leader"


@pytest_asyncio.fixture
async def meeting(monkeypatch, tmp_path):
    experimental = {"team_meeting_enabled": True, "team_meeting_default_enabled": False}
    monkeypatch.setattr(meeting_manager, "get_config", lambda: {"experimental": experimental})
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    backend = TeamBackend(TEAM, LEADER, True, db, InProcessMessager())
    backend.bind_group_session(SESSION)
    listeners = set()
    agent = SimpleNamespace(team_backend=backend, add_event_listener=listeners.add, remove_event_listener=listeners.remove)
    teams = Mock()
    teams.get_active_team_name.return_value = TEAM
    teams.get_team_agent.return_value = agent
    teams.is_runtime_active.return_value = True
    states = asyncio.Queue()
    speech = asyncio.Queue()
    browser = None

    async def emit_state(wire: dict) -> bool:
        states.put_nowait(wire["payload"])
        return True

    async def emit_audio(event: dict) -> bool:
        if event["event"] == "meeting.speech.ready":
            speech.put_nowait(event)
        elif event["event"] == "meeting.playback.stop":
            browser.confirm_stop(event["stop_id"])
        return True

    manager = MeetingManager(teams, emit=emit_state)

    async def create_team():
        await db.team.create_team(TEAM, "Meeting team", LEADER)
        for name in (LEADER, "alice", "bob", "carol"):
            await db.member.create_member(name, TEAM, name.title(), "{}", "ready")
        event = EventMessage(event_type=TeamEvent.CREATED, payload={"team_name": TEAM})
        for listener in tuple(listeners):
            await listener(event)

    async def connect(owner="first"):
        nonlocal browser
        await manager.request_start(SESSION, owner, owner)
        state = await manager.start(SESSION)
        meeting_id = state.scope.meeting_id
        catalog = load_tts_voice_catalog("cosyvoice")
        result = await manager.prepare_browser_playback(
            SESSION, meeting_id, owner_id=owner, startup_id=owner, route_id=owner,
            model=catalog.default_model, config_revision="test-revision",
            default_voice=catalog.default_voice, emit=emit_audio,
        )
        assert result == {"ready": True, "meeting_id": meeting_id}
        browser = await manager.browser_playback(SESSION, meeting_id, owner)
        return browser

    try:
        await db.initialize()
        await manager.team_runtime_ready(SESSION)
        yield SimpleNamespace(
            manager=manager, backend=backend, experimental=experimental, states=states,
            speech=speech, create_team=create_team, connect=connect,
        )
    finally:
        await manager.aclose()
        await db.close()


async def queue_reply(coordinator, member: str):
    return await coordinator.for_member(member).enqueue_message(
        f"Full reply from {member}", speech_text=f"Short reply from {member}",
        route=MeetingRoute(kind="broadcast"),
    )


async def complete_speech(browser, event):
    receipt = {**event, "outcome": "completed", "completed_chunks": event["chunk_count"]}
    assert await browser.result(receipt) == {"accepted": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_start", [False, True])
async def test_team_creation_applies_the_auto_meeting_setting(meeting, auto_start):
    meeting.experimental["team_meeting_default_enabled"] = auto_start
    before = await meeting.manager.lifecycle_status(SESSION)
    assert not before["team_exists"]
    assert not before["enabled"]
    with pytest.raises(RuntimeError, match="Team has not been created"):
        await meeting.manager.request_start(SESSION, "first", "first")

    await meeting.create_team()

    expected_state = "active" if auto_start else "idle"
    async with asyncio.timeout(5):
        while True:
            status = await meeting.states.get()
            if status["team_exists"] and status["state"] == expected_state:
                break
    assert status["enabled"] is auto_start
    assert (await meeting.backend.get_meeting() is not None) is auto_start


@pytest.mark.asyncio
async def test_ending_meeting_drains_queued_text_and_reconnect_preserves_voice(meeting):
    await meeting.create_team()
    browser = await meeting.connect()
    coordinator = await meeting.backend.get_meeting()
    meeting_id = coordinator.scope.meeting_id
    alice = await queue_reply(coordinator, "alice")
    first = await asyncio.wait_for(meeting.speech.get(), 5)
    assert first["request_id"] == alice.request_id
    assert first["chunks"] == ["Short reply from alice"]
    assert (await coordinator.get_request(alice.request_id)).message.content == "Full reply from alice"
    pending = [await queue_reply(coordinator, member) for member in ("bob", "carol")]

    assert await meeting.manager.end(SESSION, meeting_id, "first")
    status = await meeting.manager.lifecycle_status(SESSION)
    assert status["state"] == "idle" and status["team_exists"] and not status["enabled"]
    assert meeting.manager.publication_for_session(SESSION, TEAM) is None
    # End lets the current utterance finish but must drain later messages without audio.
    await complete_speech(browser, first)
    for queued in pending:
        async with asyncio.timeout(5):
            while True:
                published = await coordinator.get_request(queued.request_id)
                if published.status == SpeechRequestStatus.TEXT_ONLY:
                    break
                await asyncio.sleep(0.01)
        assert published.message.content == f"Full reply from {queued.member_name}"
        assert published.lease_token is None
    assert meeting.speech.empty()

    assert await meeting.manager.release(
        SESSION, "first", "first", transport_loss={"meeting_id": meeting_id, "route_id": "first"},
    )
    new_browser = await meeting.connect("second")
    assert new_browser.scope == browser.scope
    with pytest.raises(ValueError, match="not owned"):
        await meeting.manager.browser_playback(SESSION, meeting_id, "first")
    next_reply = await queue_reply(coordinator, "alice")
    resumed = await asyncio.wait_for(meeting.speech.get(), 5)
    assert resumed["request_id"] == next_reply.request_id
    assert resumed["voice"] == first["voice"]
    await complete_speech(new_browser, resumed)


@pytest.mark.asyncio
async def test_leader_and_addressed_member_get_next_turns_without_preempting_speaker(meeting):
    await meeting.create_team()
    browser = await meeting.connect()
    coordinator = await meeting.backend.get_meeting()
    alice = await queue_reply(coordinator, "alice")
    first = await asyncio.wait_for(meeting.speech.get(), 5)
    bob, carol, leader = [await queue_reply(coordinator, member) for member in ("bob", "carol", LEADER)]
    set_addressed = MeetingSetAddressedMembersTool(coordinator, LEADER)
    result = await set_addressed.invoke({"member_names": ["carol"]})

    assert result.success, result.error
    assert (await meeting.manager.lifecycle_status(SESSION))["addressed_members"] == ["carol"]
    assert (await coordinator.get_speaker()).request_id == alice.request_id
    assert meeting.speech.empty()
    await complete_speech(browser, first)

    for queued in (leader, carol, bob):
        turn = await asyncio.wait_for(meeting.speech.get(), 5)
        assert turn["request_id"] == queued.request_id
        assert turn["speaker_id"] == queued.member_name
        assert turn["voice"] != first["voice"]
        await complete_speech(browser, turn)
