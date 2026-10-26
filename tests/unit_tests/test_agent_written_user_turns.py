"""A user-role turn another Session's Agent or a Heartbeat wrote is not the person's request.

The session is written by the production writers: the user rows carry exactly the fields
``_history_user_extra`` gives a Web chat, a mailbox delivery and a Heartbeat run.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.recommendation import topic_controls
from jiuwenswarm.agents.harness.common.recommendation.topic_history import rescan_history
from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.server.runtime.agent_adapter.interface import _history_user_extra
from jiuwenswarm.server.runtime.session import session_history, session_metadata

SID = "web_1a0f7acc572_4c3a3563ebed"
PERSON_FIRST = "整理本周的观察记录"
PERSON_SECOND = "把连翘的花期写进 phenology.md"
AGENT_MESSAGE = "不要联网，直接改 notes.md"
HEARTBEAT_PROMPT = "检查待办是否都已完成"


def _person(query: str) -> dict:
    return {"query": query, "mode": "agent", "input_origin": "manual_free_text", "assistance_origin": "none"}


def _mailbox_delivery(query: str) -> dict:
    # The params AgentServer builds for a claimed mailbox record (execute_internal_session_message).
    return {"query": query, "mode": "agent", SESSION_MESSAGE_INTERNAL_KEY: {
        "message_id": "sm-1", "source_session_id": "web_1a0f7afa412_50e95c15fa86",
        "source_request_id": "req_src_1", "source_tool_call_id": "call-1", "source_title": "整理笔记",
        "chain_id": "chain-1", "parent_message_id": None, "hop_count": 1, "language": "zh",
    }}


def _heartbeat_run(query: str) -> dict:
    # The params HeartbeatScheduler sends for a run that continues in the original session.
    return {"query": query, "content": query, "automation": {
        "kind": "heartbeat", "job_id": "hb-1", "run_id": "hb-run-1", "triggered_at": 30.0,
        "source": "agent_tool", "trigger": "scheduler",
    }}


def _write_turn(request_id: str, timestamp: float, params: dict, answer: str):
    session_history.append_history_record(
        session_id=SID, request_id=request_id, channel_id="web", role="user", content=params["query"],
        timestamp=timestamp, extra=_history_user_extra(params), mode="agent",
    )
    session_history.append_history_record(
        session_id=SID, request_id=request_id, channel_id="web", role="assistant", event_type="chat.final",
        content=answer, timestamp=timestamp + 1, mode="agent",
    )
    return session_history.enqueue_history_request_completion(SID, request_id, terminal_status="success")


@pytest.fixture
def history(tmp_path, monkeypatch) -> list[dict]:
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    _write_turn("req_person_1", 10.0, _person(PERSON_FIRST), "本周记录了连翘。")
    _write_turn("session-message-sm-1", 20.0, _mailbox_delivery(AGENT_MESSAGE), "已改好 notes.md。")
    _write_turn("hb-run-1", 30.0, _heartbeat_run(HEARTBEAT_PROMPT), "待办都已完成。")
    _write_turn("req_person_2", 40.0, _person(PERSON_SECOND), "已写入 phenology.md。").result(timeout=5)
    return session_history.load_history_records(SID)


def test_agent_written_user_rows_are_marked_agent_initiated(history) -> None:
    rows = {row["content"]: row for row in history if row.get("role") == "user"}

    assert session_history.is_agent_initiated_record(rows[AGENT_MESSAGE])
    assert session_history.is_agent_initiated_record(rows[HEARTBEAT_PROMPT])
    assert not session_history.is_agent_initiated_record(rows[PERSON_FIRST])


@pytest.mark.parametrize("history_after", [0.0, 5.0])
def test_topic_history_counts_only_the_persons_requests(history, history_after) -> None:
    rows = topic_controls.permitted_recommendation_history(
        history, {"enabled": True, "history_after": history_after})

    assert len(rows) == len(history)
    prefix = session_history.project_completed_session_prefix(SID, rows, allow_gaps=True)
    assert prefix.completed_request_ids == ("req_person_1", "req_person_2")
    projected = session_history.project_completed_session_records(SID, rows, allow_gaps=True)
    assert [row["content"] for row in projected if row["role"] == "user"] == [PERSON_FIRST, PERSON_SECOND]
    behaviours = rescan_history(None, {SID: rows}, {}, set()).new_behaviors
    assert [behaviour.content for behaviour in behaviours] == [PERSON_FIRST, PERSON_SECOND]
