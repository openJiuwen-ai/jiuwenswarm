from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _with_web_agent_template_metadata,
    _with_web_agent_template_payload,
)
from jiuwenswarm.common.e2a.gateway_normalize import (
    e2a_response_from_agent_chunk,
    e2a_response_to_agent_chunk,
)
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel


def test_web_single_agent_identity_is_added_to_live_and_history_payloads() -> None:
    params = {"mode": "agent", "agent_template_name": "  expert-a  "}

    payload = _with_web_agent_template_payload(
        {"event_type": "chat.final", "content": "answer"},
        params,
        "web",
    )

    assert payload == {
        "event_type": "chat.final",
        "content": "answer",
        "agent_template_name": "expert-a",
    }
    assert _with_web_agent_template_metadata(
        None,
        params,
        "web",
        event_type="chat.final",
    ) == {"agent_template_name": "expert-a"}
    assert _with_web_agent_template_metadata(
        {"reasoning_content": "thinking"},
        params,
        "web",
        event_type="chat.reasoning",
    ) == {
        "reasoning_content": "thinking",
        "agent_template_name": "expert-a",
    }
    assert _with_web_agent_template_payload(
        {"event_type": "chat.reasoning", "content": "thinking"},
        params,
        "web",
    ) == {
        "event_type": "chat.reasoning",
        "content": "thinking",
        "agent_template_name": "expert-a",
    }
    assert _with_web_agent_template_payload(
        {"event_type": "chat.tool_call", "content": "tool"},
        params,
        "web",
    ) == {
        "event_type": "chat.tool_call",
        "content": "tool",
        "agent_template_name": "expert-a",
    }


def test_unary_final_without_event_type_gets_identity() -> None:
    payload = _with_web_agent_template_payload(
        {"content": "answer"},
        {"mode": "agent", "agent_template_name": "expert-a"},
        "web",
        event_type="chat.final",
    )

    assert payload == {"content": "answer", "agent_template_name": "expert-a"}


def test_e2a_stream_delta_preserves_agent_identity() -> None:
    chunk = AgentResponseChunk(
        request_id="req-1",
        channel_id="web",
        payload={
            "event_type": "chat.delta",
            "content": "answer",
            "agent_template_name": "expert-a",
        },
    )

    wire = e2a_response_from_agent_chunk(chunk, response_id="resp-1", sequence=0)
    assert wire.body["agent_template_name"] == "expert-a"

    restored = e2a_response_to_agent_chunk(wire)
    assert restored.payload["agent_template_name"] == "expert-a"


def test_non_web_team_harness_and_proactive_events_are_unchanged() -> None:
    cases = [
        ("tui", {"mode": "agent", "agent_template_name": "expert-a"}),
        ("web", {"mode": "team", "agent_template_name": "expert-a"}),
        ("web", {"mode": "agent", "team": True, "agent_template_name": "expert-a"}),
        ("web", {"mode": "auto_harness", "agent_template_name": "expert-a"}),
        (
            "web",
            {
                "mode": "agent",
                "agent_template_name": "expert-a",
                "source": "proactive_recommendation",
            },
        ),
        (
            "web",
            {
                "mode": "agent",
                "agent_template_name": "expert-a",
                "source": "session_task_summary",
            },
        ),
    ]

    for channel_id, params in cases:
        assert _with_web_agent_template_payload(
            {"event_type": "chat.final", "content": "answer"},
            params,
            channel_id,
        ) == {"event_type": "chat.final", "content": "answer"}


def test_eventless_stream_payload_does_not_get_agent_identity() -> None:
    assert _with_web_agent_template_payload(
        {"content": "answer"},
        {"mode": "agent", "agent_template_name": "expert-a"},
        "web",
    ) == {"content": "answer"}


def test_web_identity_is_forwarded_for_reasoning_text_and_tool_events() -> None:
    message = SimpleNamespace(
        payload={
            "event_type": "chat.reasoning",
            "content": "reasoning",
            "agent_template_name": "expert-a",
        },
        session_id="session-1",
        metadata={},
    )
    reasoning_payload = WebChannel._build_event_payload(  # pylint: disable=protected-access
        message, "chat.reasoning"
    )
    assert reasoning_payload["agent_template_name"] == "expert-a"

    for event_type in ("chat.delta", "chat.reasoning", "chat.final", "chat.tool_call"):
        text_message = SimpleNamespace(
            id="message-1",
            payload={
                "event_type": event_type,
                "content": "answer",
                "agent_template_name": "expert-a",
            },
            session_id="session-1",
            metadata={},
        )
        text_payload = WebChannel._build_event_payload(  # pylint: disable=protected-access
            text_message, event_type
        )
        assert text_payload["agent_template_name"] == "expert-a"


def test_non_target_event_types_and_heartbeat_are_unchanged() -> None:
    params = {"mode": "agent", "agent_template_name": "expert-a"}
    for event_type in ("chat.error",):
        assert _with_web_agent_template_payload(
            {"event_type": event_type, "content": "event"},
            params,
            "web",
        ) == {"event_type": event_type, "content": "event"}

    assert _with_web_agent_template_payload(
        {"event_type": "chat.final", "content": "heartbeat"},
        {**params, "automation": {"kind": "heartbeat", "run_id": "run-1"}},
        "web",
    ) == {"event_type": "chat.final", "content": "heartbeat"}
