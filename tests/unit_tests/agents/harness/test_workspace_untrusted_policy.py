# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""关闭信任工作空间后，审批跟安全策略表走（开=ask，关=allow）。"""

from __future__ import annotations

from types import SimpleNamespace

from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

from jiuwenswarm.agents.harness.common.rails.permissions.workspace_untrusted_policy import (
    reconcile_tool_policy_when_workspace_untrusted,
    workspace_rw_trusted,
)


class _StubEngine:
    def __init__(self, policy: PermissionLevel, rule: str = "tools.read_file") -> None:
        self._policy = policy
        self._rule = rule

    def evaluate_global_policy_directly(self, tool_name, tool_args, include_external_directory=True):
        assert include_external_directory is False
        return self._policy, self._rule


def _ask_result() -> PermissionResult:
    return PermissionResult(
        permission=PermissionLevel.ASK,
        matched_rule="tools.read_file|file_guard:defaults",
        reason="file_guard requires approval",
        external_paths=["C:/tmp/uploads/a.docx"],
    )


def test_workspace_rw_trusted_follows_read_axis() -> None:
    assert workspace_rw_trusted({"read": "allow", "write": "allow", "exec": "allow"}) is True
    assert workspace_rw_trusted({"read": "ask", "write": "ask", "exec": "ask"}) is False


def test_untrusted_policy_off_allow_wins_over_file_guard_ask() -> None:
    result = reconcile_tool_policy_when_workspace_untrusted(
        _StubEngine(PermissionLevel.ALLOW),  # type: ignore[arg-type]
        "read_file",
        {"file_path": "C:/tmp/uploads/a.docx"},
        _ask_result(),
        workspace_trusted=False,
    )
    assert result.permission == PermissionLevel.ALLOW
    assert "workspace_untrusted:policy" in (result.matched_rule or "")


def test_untrusted_policy_on_stays_ask() -> None:
    result = reconcile_tool_policy_when_workspace_untrusted(
        _StubEngine(PermissionLevel.ASK),  # type: ignore[arg-type]
        "read_file",
        {"file_path": "C:/tmp/uploads/a.docx"},
        _ask_result(),
        workspace_trusted=False,
    )
    assert result.permission == PermissionLevel.ASK


def test_untrusted_file_guard_deny_is_kept() -> None:
    denied = PermissionResult(
        permission=PermissionLevel.DENY,
        matched_rule="file_guard:defaults",
        reason="denied",
    )
    result = reconcile_tool_policy_when_workspace_untrusted(
        _StubEngine(PermissionLevel.ALLOW),  # type: ignore[arg-type]
        "read_file",
        {},
        denied,
        workspace_trusted=False,
    )
    assert result.permission == PermissionLevel.DENY


def test_trusted_workspace_keeps_file_guard_ask() -> None:
    result = reconcile_tool_policy_when_workspace_untrusted(
        SimpleNamespace(),  # type: ignore[arg-type]
        "read_file",
        {},
        _ask_result(),
        workspace_trusted=True,
    )
    assert result.permission == PermissionLevel.ASK
    assert result.matched_rule == "tools.read_file|file_guard:defaults"
