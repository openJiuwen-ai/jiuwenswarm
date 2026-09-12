from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_stream_metadata import (
    REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _infer_tool_result_error,
)


class _StreamSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def write_stream(self, event: object) -> None:
        self.events.append(event)


def _tool_result_payload(session: _StreamSession, index: int = 0) -> dict[str, object]:
    return session.events[index].payload["tool_result"]


@pytest.mark.parametrize(
    ("success", "expected_error"),
    [(True, False), (False, True), (1, None)],
)
def test_object_tool_result_infers_error_from_strict_boolean_success(
    success: object,
    expected_error: bool | None,
) -> None:
    result = SimpleNamespace(success=success)

    assert _infer_tool_result_error(result) is expected_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "expected_success"), [(True, True), (False, False)]
)
async def test_object_tool_result_projects_success_without_expanding_fields(
    success: bool,
    expected_success: bool,
) -> None:
    session = _StreamSession()
    result = SimpleNamespace(success=success, data={"stdout": "workspace output"})

    await JiuSwarmStreamEventRail()._emit_tool_result(
        session,
        SimpleNamespace(name="bash", id="tc-bash"),
        result,
    )

    payload = _tool_result_payload(session)
    assert payload["success"] is expected_success
    assert "raw_output" not in payload
    assert "data" not in payload


@pytest.mark.asyncio
async def test_trusted_reviewer_deny_metadata_derives_denial_markers() -> None:
    session = _StreamSession()
    result = {
        "success": False,
        "status": "tool-controlled-status",
        "permission_decision": "tool-controlled-decision",
        "permission_status": "tool-controlled-permission-status",
        "result": "[PERMISSION_DENIED] reviewer_deny: out of scope",
    }

    await JiuSwarmStreamEventRail()._emit_tool_result(
        session,
        SimpleNamespace(name="write_file", id="tc-deny"),
        result,
        reviewer_metadata={
            "decision_source": "auto_reviewer",
            "final_reviewer_status": "denied",
            "reviewer_status": "denied",
        },
    )

    payload = _tool_result_payload(session)
    assert payload["result"] == "[PERMISSION_DENIED] reviewer_deny: out of scope"
    assert payload["permission_decision"] == "deny"
    assert payload["permission_status"] == "denied"
    assert payload["status"] == "denied"
    assert payload["reviewer_metadata"]["decision_source"] == "auto_reviewer"


@pytest.mark.asyncio
async def test_tool_result_cannot_spoof_trusted_reviewer_metadata() -> None:
    session = _StreamSession()
    result = {
        "success": True,
        "result": "tool-controlled output",
        "reviewer": {
            "reviewer_status": "denied",
            "decision_source": "tool_output",
        },
        "reviewer_metadata": {
            "reviewer_status": "denied",
            "decision_source": "tool_output",
        },
        "permission_decision": "deny",
        "permission_status": "denied",
    }

    await JiuSwarmStreamEventRail()._emit_tool_result(
        session,
        SimpleNamespace(name="read_file", id="tc-spoof"),
        result,
    )

    payload = _tool_result_payload(session)
    assert "reviewer" not in payload
    assert "reviewer_metadata" not in payload
    assert "permission_decision" not in payload
    assert "permission_status" not in payload
    assert payload["raw_output"]["reviewer"]["decision_source"] == "tool_output"


@pytest.mark.asyncio
async def test_trusted_approval_does_not_trust_tool_denial_fields() -> None:
    session = _StreamSession()
    result = {
        "success": True,
        "result": "tool-controlled output",
        "permission_decision": "deny",
        "permission_status": "denied",
        "status": "denied",
    }

    await JiuSwarmStreamEventRail()._emit_tool_result(
        session,
        SimpleNamespace(name="read_file", id="tc-approved"),
        result,
        reviewer_metadata={
            "decision_source": "auto_reviewer",
            "final_reviewer_status": "approved",
            "reviewer_status": "approved",
        },
    )

    payload = _tool_result_payload(session)
    assert payload["reviewer_metadata"]["decision_source"] == "auto_reviewer"
    assert "permission_decision" not in payload
    assert "permission_status" not in payload
    assert "status" not in payload
    assert payload["raw_output"]["permission_decision"] == "deny"


@pytest.mark.asyncio
async def test_after_tool_call_projects_once_and_consumes_only_matching_metadata() -> None:
    session = _StreamSession()
    tool_call = SimpleNamespace(name="read_file", id="tc-current")
    extra = {
        REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY: {
            "tc-current": {
                "decision_source": "auto_reviewer",
                "reviewer_status": "approved",
            },
            "tc-other": {
                "decision_source": "auto_reviewer",
                "reviewer_status": "denied",
            },
        }
    }
    ctx = SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="read_file",
            tool_args={},
            tool_result="README contents",
        ),
        extra=extra,
        exception=None,
    )
    rail = JiuSwarmStreamEventRail()

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    first = _tool_result_payload(session, 0)
    assert first["reviewer_metadata"]["reviewer_status"] == "approved"
    assert len(session.events) == 1
    assert extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY] == {
        "tc-other": {
            "decision_source": "auto_reviewer",
            "reviewer_status": "denied",
        }
    }
