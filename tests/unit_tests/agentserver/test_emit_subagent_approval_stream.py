"""Regression: subagent approval cards must keep type=chat.ask_user_question."""

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
    SubagentApprovalKind,
    SubagentApprovalRequest,
)

from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
    emit_subagent_approval,
    emit_subagent_approval_expired,
)


class _CaptureSession:
    def __init__(self) -> None:
        self.writes: list[Any] = []

    async def write_stream(self, data: Any) -> None:
        # Mirror AgentSession.write_stream normalization so the test fails the
        # same way production did when ``index`` was missing from a raw dict.
        from openjiuwen.core.session.agent import Session as AgentSession

        normalized = AgentSession._normalize_output_stream(data)
        self.writes.append(normalized)


def _request() -> SubagentApprovalRequest:
    return SubagentApprovalRequest(
        approval_id="subagent_tool_permission_deadbeef",
        kind=SubagentApprovalKind.TOOL_PERMISSION,
        session_id="main-session",
        agent_scope_id="child-session",
        tool_call_id="call_1",
        payload={"tool_name": "read_file"},
    )


@pytest.mark.asyncio
async def test_emit_subagent_approval_keeps_ask_user_question_type():
    session = _CaptureSession()

    await emit_subagent_approval(
        session,
        _request(),
        header="权限审批: read_file",
        question="allow?",
        options=[{"label": "本次允许", "description": "once"}],
    )

    assert len(session.writes) == 1
    frame = session.writes[0]
    assert isinstance(frame, OutputSchema)
    assert frame.type == "chat.ask_user_question"
    assert frame.index == 0
    assert frame.payload["source"] == "subagent_tool_permission"
    assert frame.payload["request_id"] == "subagent_tool_permission_deadbeef"


@pytest.mark.asyncio
async def test_emit_subagent_approval_expired_keeps_expired_type():
    session = _CaptureSession()

    await emit_subagent_approval_expired(session, _request(), "timeout")

    assert len(session.writes) == 1
    frame = session.writes[0]
    assert isinstance(frame, OutputSchema)
    assert frame.type == "chat.ask_user_question_expired"
    assert frame.payload["reason"] == "timeout"


def test_normalize_bare_dict_without_index_collapses_to_message():
    """Document the Session contract that caused the production hang."""
    from openjiuwen.core.session.agent import Session as AgentSession

    collapsed = AgentSession._normalize_output_stream(
        {
            "type": "chat.ask_user_question",
            "payload": {"event_type": "chat.ask_user_question"},
        }
    )
    assert collapsed.type == "message"
