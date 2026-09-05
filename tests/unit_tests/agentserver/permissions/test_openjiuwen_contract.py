# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the openjiuwen permission rail integration contract."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_denied_permission_response,
    build_rejected_permission_response,
    classify_permission_result,
    load_openjiuwen_permission_contract,
)

_INTERRUPT_HELPERS = (
    Path(__file__).resolve().parents[4]
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "interrupt"
    / "interrupt_helpers.py"
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


def test_interrupt_helpers_uses_permission_engine_factory() -> None:
    source = _INTERRUPT_HELPERS.read_text(encoding="utf-8")
    assert "build_permission_interrupt_rail" in source
    assert "from openjiuwen.harness.security.host" not in source
    assert "PermissionInterruptRail(" not in source


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
