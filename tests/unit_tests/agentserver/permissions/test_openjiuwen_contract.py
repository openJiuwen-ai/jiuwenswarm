# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the openjiuwen permission rail integration contract."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_denied_permission_response,
    build_manual_approval_required_response,
    build_rejected_permission_response,
    classify_permission_result,
    load_openjiuwen_permission_contract,
)
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
)

def test_openjiuwen_permission_contract_imports() -> None:
    contract = load_openjiuwen_permission_contract()
    assert contract.permission_rail_class is not None
    assert contract.permission_confirm_response_class is not None
    assert contract.before_tool_call_is_async is True
    assert contract.supports_update_config is True
    assert contract.supports_manual_approval_response is True
    assert contract.permission_confirm_response_class.__module__ == (
        "openjiuwen.harness.security.permission_engine.models"
    )
    assert contract.supports_permission_engine_factory is True


def test_manual_interrupt_helpers_uses_permission_engine_factory(monkeypatch) -> None:
    calls = []
    sentinel = object()

    def factory(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr("openjiuwen.harness.security.build_permission_interrupt_rail", factory)
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is sentinel
    assert len(calls) == 1
    assert calls[0]["permissions"]["enabled"] is True
    assert callable(calls[0]["host"].get_permissions_snapshot)


def test_build_denied_permission_response() -> None:
    response = build_denied_permission_response("blocked by auto permission")
    assert response.approved is False
    assert response.auto_confirm is False
    assert "blocked by auto permission" in response.feedback


def test_build_rejected_permission_response() -> None:
    response = build_rejected_permission_response("user rejected the request")

    assert response.approved is False
    assert response.auto_confirm is False
    assert response.feedback.startswith("[PERMISSION_REJECTED]")
    assert classify_permission_result(response) == "user_rejection"


def test_permission_result_classification() -> None:
    assert classify_permission_result(None) == "allow"
    assert (
        classify_permission_result(build_denied_permission_response("no")) == "denied"
    )
    assert classify_permission_result({"interrupt": True}) == "interrupt"
    assert (
        classify_permission_result({"feedback": "[PERMISSION_REJECTED] rejected"})
        == "user_rejection"
    )
    assert (
        classify_permission_result("[PERMISSION_REJECTED] rejected") == "user_rejection"
    )
    assert (
        classify_permission_result("tool execution cancelled by scheduler")
        == "interrupt"
    )
    assert (
        classify_permission_result({"feedback": "Permission request was cancelled."})
        == "user_rejection"
    )
    assert classify_permission_result("[PERMISSION_DENIED] blocked") == "denied"
    assert classify_permission_result({"unexpected": object()}) == "interrupt"
    assert classify_permission_result(object()) == "interrupt"
def test_manual_approval_response_builds_permission_interrupt() -> None:
    queue = RootPermissionQueue(id_factory=lambda: "invocation-1")
    card = queue.begin(
        root_session_id="session-1",
        request_id="request-1",
        execution_session_id="session-1",
        tool_call_id="tool-call-1",
        tool_name="bash",
    )
    queue.mark_pending(
        card.key,
        request=InterruptRequest(metadata={"tool_invocation_key": card.key.to_wire()}),
        auto_manual=False,
        root_context=None,
    )
    interaction = build_manual_approval_required_response(
        "manual approval required",
        {"tool_invocation_key": card.key.to_wire()},
        prompt_payload={"tool_name": "bash", "tool_call_id": "tool-call-1"},
    )
    interaction["id"] = "tool-call-1"
    assert classify_permission_result(interaction) == "interrupt"
    converted = convert_interactions_to_ask_user_question(
        [[interaction]], root_permission_queue=queue
    )
    assert converted is not None
    assert converted["source"] == "permission_interrupt"
    assert "manual approval required" in converted["questions"][0]["question"]
    assert converted["questions"][0]["card_id"] == card.key.invocation_id
    assert "tool_invocation_key" not in converted["questions"][0]
    assert "tool_call_id" not in converted["questions"][0]
