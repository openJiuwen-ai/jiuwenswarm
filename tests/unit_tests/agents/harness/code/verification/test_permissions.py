# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""run_tests is visible to the permission policy like bash."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
    compose_host_effective_permissions,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    classify_tool,
)

_RESOURCES = Path(__file__).resolve().parents[6] / "jiuwenswarm" / "resources"
_TEMPLATES = ("config.yaml", "config.team.distributed.leader.yaml", "config.team.distributed.teammate.yaml")


def test_run_tests_is_classified_as_dynamic_execution():
    cap = classify_tool("run_tests")
    assert cap.category == "code" and cap.operation_family == "dynamic_execution"
    assert cap.risk_tier == "high" and "dynamic_exec" in cap.static_side_effects


@pytest.mark.parametrize("name", _TEMPLATES)
def test_templates_list_run_tests_next_to_bash(name):
    perms = yaml.safe_load((_RESOURCES / name).read_text(encoding="utf-8"))["permissions"]
    assert perms["tools"]["run_tests"] == perms["tools"]["bash"] == "allow"


def test_policy_composition_sees_run_tests_and_honours_ask():
    perms = yaml.safe_load((_RESOURCES / "config.yaml").read_text(encoding="utf-8"))["permissions"]
    effective = compose_host_effective_permissions(global_permissions=perms)
    assert effective["tools"]["run_tests"] == "allow" and "run_tests" in effective["allow_tools"]
    strict = compose_host_effective_permissions(
        global_permissions=perms, user_permissions={"tools": {"run_tests": "ask"}}
    )
    assert strict["tools"]["run_tests"] == "ask" and "run_tests" in strict["ask_tools"]


def test_run_tests_is_an_unverified_execution_provider_in_auto_mode() -> None:
    """run_tests runs on the host, so auto mode must require manual review for it."""
    from jiuwenswarm.agents.harness.common.rails.permissions.execution_provider_contract import (
        JIUWENBOX_SANDBOX_EXECUTION_TOOLS,
        UNKNOWN_PROVIDER_EXECUTION_TOOLS,
        requires_manual_execution_provider_review,
    )

    assert "run_tests" in UNKNOWN_PROVIDER_EXECUTION_TOOLS
    assert "run_tests" not in JIUWENBOX_SANDBOX_EXECUTION_TOOLS
    assert requires_manual_execution_provider_review("run_tests", tool_category="code")
