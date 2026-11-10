"""Regression coverage for the web meeting's leader interruption boundary."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.agent_teams.schema.meeting import MeetingScope, SpeechRequest, SpeechRequestStatus
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

from jiuwenswarm.extensions.meeting.grant_driver import MeetingGrantDriver
from jiuwenswarm.extensions.meeting.playback import BrowserPlaybackConsumer
from jiuwenswarm.extensions.meeting.publication import create_meeting_publication_rail


@pytest.mark.asyncio
async def test_recorded_leader_can_interrupt_with_default_member_role():
    scope = MeetingScope("session", "team", "meeting")
    db = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    coordinator = MeetingCoordinator(db, scope)
    pushes = []

    async def emit(payload: dict) -> bool:
        pushes.append(payload)
        if payload["event"] == "meeting.speech.stop":
            state = await coordinator.get_state()
            assert state.interruption_id == payload["stop_id"]
            consumer.confirm_stop(payload["stop_id"])
        return True

    async def publish_granted(
        request_id: str, lease_token: str, playback_ready: bool, _retry: bool,
    ) -> SpeechRequest | None:
        return await coordinator.finish_publication(request_id, lease_token, playback_ready=playback_ready)

    voices = Mock()
    voices.get_voice.return_value = Mock(provider_voice_id="voice", model="model", catalog_version="v1")
    consumer = BrowserPlaybackConsumer(scope, voices, owner_id="owner", route_id="route", config_revision="v1", emit=emit)
    driver = MeetingGrantDriver(coordinator, consumer, is_current=AsyncMock(return_value=True), publish_granted=publish_granted)
    consumer.driver = driver

    try:
        await db.initialize()
        await db.team.create_team(scope.team_name, "Team", "team-leader")
        # Team creation currently leaves the leader's member role at the teammate default.
        await db.member.create_member("team-leader", scope.team_name, "Leader", "{}", "ready")
        await db.member.create_member("alice", scope.team_name, "Alice", "{}", "ready")
        await db.meeting.enable_team_meeting(scope.session_id, scope.team_name, meeting_id=scope.meeting_id)
        request = await coordinator.for_member("alice").request_speech(speech_text="Testing playback")
        await driver._step()
        ready, = pushes

        rail = await create_meeting_publication_rail(
            coordinator, "team-leader", create_send_tool=Mock(),
            on_interrupt=lambda member, token: driver.interrupt(token, leader=member),
        )
        lookup = rail._tools["meeting_get_speaker"]
        token = json.loads(lookup.render_for_llm(await lookup.invoke({})))["lease_token"]
        assert token == ready["lease_token"]

        with pytest.raises(ValueError, match="Only the meeting Leader"):
            await driver.interrupt(token, leader="alice")
        assert await driver.interrupt("stale-token", leader="team-leader") is None
        assert len(pushes) == 1

        result = await rail._tools["meeting_interrupt_speech"].invoke({"expected_token": token})
        assert result.success, result.error
        stop = pushes[1]
        assert stop["event"] == "meeting.speech.stop"
        assert stop["playback_id"] == ready["playback_id"]
        assert stop["stop_id"] == result.data["interruption_id"]
        assert (await coordinator.get_request(request.request_id)).status == SpeechRequestStatus.INTERRUPTED
        assert (await coordinator.get_state()).interruption_id is None
    finally:
        await db.close()
