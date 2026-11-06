"""Voice-started tasks must feed the chat's subagent panel like typed tasks (#5452)."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.backend import video_search


ROSTER = {
    "event_type": "chat.subtask_update", "session_id": "visible", "subagent_id": "cua-1",
    "parent_session_id": "visible", "subagent_type": "cua_agent", "display_name": "cua_agent",
    "task_description": "Open the settings page", "status": "running", "revision": 1,
}
THINKING = {
    "event_type": "chat.subagent_activity", "subagent_id": "cua-1", "parent_session_id": "visible",
    "task_id": "cua-1", "seq": 1, "kind": "thinking", "summary": "Looking for the window", "at_ms": 1000,
}
TOOL_CALL = {**THINKING, "seq": 2, "kind": "tool_call", "summary": "screenshot()", "tool_name": "screenshot"}


def test_subagent_frames_become_progress_carrying_the_original_event():
    # The Web panel normalizes the exact typed-path frame; a reshaped copy would drift.
    roster = video_search.core_agent_progress(ROSTER)
    activity = video_search.core_agent_progress(THINKING)
    assert roster["stage"] == activity["stage"] == "subagent"
    assert roster["subagent_event"] == ROSTER
    assert activity["subagent_event"] == THINKING
    assert "cua_agent" in roster["title"]
    assert activity["detail"] == "Looking for the window"


@pytest.mark.asyncio
async def test_every_subagent_frame_reaches_task_progress_in_stream_order():
    frames = [ROSTER, THINKING, TOOL_CALL, {**ROSTER, "status": "idle", "revision": 2},
              {"event_type": "chat.final", "content": "Done"}]

    class Client:
        async def send_request_stream(self, _env):
            for payload in frames:
                yield SimpleNamespace(payload=payload)

    progress = []

    async def record(entry):
        progress.append(entry)

    await video_search.execute_core_agent(
        Client(), question="q", query="q", visual_context="", search_session_id="voice", on_progress=record,
    )
    # Repeated roster/activity frames are distinct facts; none may be deduplicated away.
    forwarded = [entry["subagent_event"] for entry in progress if entry["stage"] == "subagent"]
    assert forwarded == frames[:4]
