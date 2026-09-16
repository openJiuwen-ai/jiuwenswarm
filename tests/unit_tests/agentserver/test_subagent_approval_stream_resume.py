"""Regression: hosted subagent approval must resume via streaming chat.send."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_ask_user_payload_excludes_hosted_subagent_sources() -> None:
    """Hosted cards must not trip outer-loop HITL suppress / invocation_paused."""
    assert JiuWenSwarmDeepAdapter._is_ask_user_payload(
        {
            "event_type": "chat.ask_user_question",
            "source": "permission_interrupt",
            "request_id": "call_1",
        }
    )
    assert not JiuWenSwarmDeepAdapter._is_ask_user_payload(
        {
            "event_type": "chat.ask_user_question",
            "source": "subagent_tool_permission",
            "request_id": "subagent_tool_permission_abc",
        }
    )
    assert not JiuWenSwarmDeepAdapter._is_ask_user_payload(
        {
            "event_type": "chat.ask_user_question",
            "source": "subagent_skill_load",
            "request_id": "subagent_skill_load_abc",
        }
    )


def test_subagent_approval_answer_detection() -> None:
    assert JiuWenSwarmDeepAdapter._is_subagent_approval_answer(
        "subagent_tool_permission_abc",
        {"source": "subagent_tool_permission", "answers": ["本次允许"]},
    )
    assert JiuWenSwarmDeepAdapter._is_subagent_approval_answer(
        "subagent_skill_load_abc",
        {"answers": ["本次允许"]},
    )
    assert not JiuWenSwarmDeepAdapter._is_subagent_approval_answer(
        "call_1",
        {"source": "permission_interrupt", "answers": ["本次允许"]},
    )


@pytest.mark.asyncio
async def test_process_message_stream_resolves_subagent_approval_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OfficeClaw resumes via chat.send; must resolve Future before lease logic."""
    resolved: dict[str, object] = {}

    def _fake_resolve(request, request_id, answers):  # noqa: ANN001
        resolved["session_id"] = request.session_id
        resolved["request_id"] = request_id
        resolved["answers"] = answers
        return True

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_resolve_subagent_approval_answer",
        staticmethod(_fake_resolve),
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    # Early path must not touch session/MCP wiring.
    adapter._is_session_scoped_adapter = False

    request = AgentRequest(
        request_id="resume-stream-1",
        channel_id="officeclaw",
        session_id="officeclaw_sess",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "subagent_tool_permission_deadbeef",
            "answers": ["本次允许"],
            "source": "subagent_tool_permission",
        },
        is_stream=True,
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(request, {"query": ""})
    ]

    assert resolved == {
        "session_id": "officeclaw_sess",
        "request_id": "subagent_tool_permission_deadbeef",
        "answers": ["本次允许"],
    }
    assert len(chunks) == 2
    assert chunks[0].payload["event_type"] == "runtime.accepted"
    assert chunks[0].payload["resolved"] is True
    assert chunks[1].is_complete is True
    assert chunks[1].payload is None


@pytest.mark.asyncio
async def test_process_message_impl_resolves_subagent_approval_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-stream chat.send with answers must not fall into a normal agent round."""
    resolved: dict[str, object] = {}

    def _fake_resolve(request, request_id, answers):  # noqa: ANN001
        resolved["session_id"] = request.session_id
        resolved["request_id"] = request_id
        resolved["answers"] = answers
        return True

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_resolve_subagent_approval_answer",
        staticmethod(_fake_resolve),
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False

    request = AgentRequest(
        request_id="resume-nostream-1",
        channel_id="web",
        session_id="web_sess",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "subagent_tool_permission_cafebabe",
            "answers": [{"selected_options": ["拒绝"]}],
            "source": "subagent_tool_permission",
        },
        is_stream=False,
    )

    response = await adapter.process_message_impl(request, {"query": ""})

    assert resolved == {
        "session_id": "web_sess",
        "request_id": "subagent_tool_permission_cafebabe",
        "answers": [{"selected_options": ["拒绝"]}],
    }
    assert response.ok is True
    assert response.payload["resolved"] is True
    assert response.payload["accepted"] is True


def test_resolve_subagent_approval_works_without_agent_scope_id(monkeypatch):
    """OfficeClaw resume omits agent_scope_id; approval_id alone must resolve."""
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalKind,
        SubagentApprovalRequest,
        SubagentApprovalRegistry,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
        resolve_subagent_approval,
    )

    class _FakeRegistry:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def pending_requests(self):
            return (
                SubagentApprovalRequest(
                    approval_id="subagent_tool_permission_deadbeef",
                    kind=SubagentApprovalKind.TOOL_PERMISSION,
                    session_id="sess-1",
                    agent_scope_id="scope-child",
                    tool_call_id="call_1",
                    payload={},
                ),
            )

        def resolve(self, **kwargs):
            self.calls.append(kwargs)
            return True

    fake = _FakeRegistry()
    monkeypatch.setattr(
        "openjiuwen.harness.security.skill_authorization.subagent_approval_registry.get_subagent_approval_registry",
        lambda: fake,
    )
    # runtime imports get_subagent_approval_registry inside the function from
    # the same module path used above via get_subagent_approval_registry alias.
    import openjiuwen.harness.security.skill_authorization.subagent_approval_registry as reg_mod

    monkeypatch.setattr(reg_mod, "get_subagent_approval_registry", lambda: fake)

    ok = resolve_subagent_approval(
        request_id="subagent_tool_permission_deadbeef",
        session_id="sess-1",
        source="subagent_tool_permission",
        answers=["本次允许"],
        agent_scope_id="",
    )
    assert ok is True
    assert fake.calls[0]["agent_scope_id"] == "scope-child"
