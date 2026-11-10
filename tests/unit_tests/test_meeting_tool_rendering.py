"""Model-facing results for meeting controls."""

import json
from unittest.mock import Mock

import pytest
from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.agent_teams.meeting.member import MeetingMemberClient
from openjiuwen.agent_teams.schema.meeting import (
    MeetingScope,
    MeetingState,
    SpeechRequest,
    SpeechRequestStatus,
)

from jiuwenswarm.extensions.meeting.publication import create_meeting_publication_rail


@pytest.mark.asyncio
async def test_meeting_get_speaker_renders_active_lease_and_idle_state():
    scope = MeetingScope(session_id="session", team_name="team", meeting_id="meeting")
    request = SpeechRequest(
        scope=scope,
        request_id="speech-1",
        member_name="alice",
        source_message_id="private-message",
        speech_text="Private speech",
        sequence=1,
        status=SpeechRequestStatus.GRANTED,
        lease_token="active-lease",
        created_at=1,
        granted_at=2,
        content="Private message",
    )
    coordinator = Mock(spec=MeetingCoordinator)
    coordinator.for_member.side_effect = lambda name: MeetingMemberClient(coordinator, name)
    coordinator.get_state.return_value = MeetingState(scope, "leader", (), "speech-1", "active-lease", None, 1)
    coordinator.get_speaker.side_effect = [request, None]
    rail = await create_meeting_publication_rail(coordinator, "leader", create_send_tool=Mock())
    tool = rail._tools["meeting_get_speaker"]

    result = await tool.invoke({})
    assert result.success
    assert result.data["content"] is None
    rendered = tool.render_for_llm(result)
    speaker = json.loads(rendered)
    assert speaker["member_name"] == "alice"
    assert speaker["lease_token"] == "active-lease"
    assert "Private" not in rendered

    idle = await tool.invoke({})
    assert idle.success
    assert tool.render_for_llm(idle) == "No active meeting speaker."
