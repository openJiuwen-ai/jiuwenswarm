# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the send-file OpenJiuwen ``file_guard`` adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openjiuwen.harness.security import PermissionEngine, PermissionLevel, PermissionResult

from jiuwenswarm.agents.harness.common.rails.permissions import (
    send_file_path_guard as path_guard_module,
)
from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import (
    SEND_FILE_PATH_ARGUMENT,
    SEND_FILE_TOOL_NAME,
    SendFilePathGuardEvaluator,
)


def _native_config(
    *,
    denied_root: Path | None = None,
) -> dict[str, Any]:
    paths: list[dict[str, str]] = []
    if denied_root is not None:
        paths.append(
            {
                "path": denied_root.as_posix(),
                "read": "deny",
                "write": "deny",
                "exec": "deny",
                "match": "prefix",
            }
        )
    return {
        "enabled": True,
        "schema": "tiered_policy",
        "defaults": {"*": "allow"},
        "tools": {SEND_FILE_TOOL_NAME: "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "allow", "exec": "ask"},
            "paths": paths,
        },
    }


def _legacy_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "defaults": {"*": "allow"},
        "tools": {SEND_FILE_TOOL_NAME: "allow"},
        "external_directory": {"*": "ask"},
        "file_guard": {"enabled": True},
    }


def test_native_guard_aggregates_all_paths_with_strictest_precedence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external" / "report.md"
    denied = tmp_path / "denied" / "secret.md"
    workspace_file = workspace / "summary.md"
    raw_paths = json.dumps(
        [
            workspace_file.as_posix(),
            external.as_posix(),
            denied.as_posix(),
            external.as_posix(),
        ]
    )

    result = SendFilePathGuardEvaluator().evaluate(
        permissions=_native_config(denied_root=denied.parent),
        workspace_root=workspace,
        trusted_dirs=(),
        raw_paths=raw_paths,
    )

    assert result.status == "evaluated"
    assert result.level == "deny"
    assert result.canonical_paths == (
        workspace_file.resolve().as_posix(),
        external.resolve().as_posix(),
        denied.resolve().as_posix(),
    )
    assert result.ask_accesses == ((external.resolve().as_posix(), "read"),)
    assert "file_guard:defaults" in result.matched_rules
    assert f"file_guard:prefix:{denied.parent.resolve().as_posix()}" in (
        result.matched_rules
    )


def test_native_guard_trusts_develop_trusted_directory_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    trusted = tmp_path / "trusted"
    target = trusted / "report.md"

    result = SendFilePathGuardEvaluator().evaluate(
        permissions=_native_config(),
        workspace_root=workspace,
        trusted_dirs=(trusted,),
        raw_paths=target.as_posix(),
    )

    assert result.status == "evaluated"
    assert result.level == "allow"
    assert result.canonical_paths == (target.resolve().as_posix(),)
    assert result.ask_accesses == ()


def test_legacy_guard_uses_locked_read_file_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external" / "report.md"

    result = SendFilePathGuardEvaluator().evaluate(
        permissions=_legacy_config(),
        workspace_root=workspace,
        trusted_dirs=(),
        raw_paths=(external.as_posix(),),
    )

    assert result.status == "evaluated"
    assert result.level == "ask"
    assert result.ask_accesses == ((external.resolve().as_posix(), "read"),)
    assert result.matched_rules == ("external_directory.*",)


def test_disabled_guard_is_neutral_after_valid_path_normalization(
    tmp_path: Path,
) -> None:
    target = tmp_path / "report.md"

    result = SendFilePathGuardEvaluator().evaluate(
        permissions={"file_guard": {"enabled": False}},
        workspace_root=None,
        trusted_dirs=(),
        raw_paths=target.as_posix(),
    )

    assert result.status == "neutral"
    assert result.level == "neutral"
    assert result.canonical_paths == (target.resolve().as_posix(),)


@pytest.mark.parametrize(
    ("raw_paths", "reason"),
    [
        (None, "send_file_path_guard_paths_invalid_type"),
        ("", "send_file_path_guard_paths_empty"),
        ([], "send_file_path_guard_paths_empty"),
        (["/tmp/report.md", 7], "send_file_path_guard_path_not_string"),
        ('["/tmp/report.md", 7]', "send_file_path_guard_path_not_string"),
        ("[", "send_file_path_guard_paths_invalid_json"),
        ("/tmp/bad\x00path", "send_file_path_guard_path_contains_nul"),
    ],
)
def test_malformed_paths_fail_closed_before_disabled_guard(
    raw_paths: Any,
    reason: str,
) -> None:
    result = SendFilePathGuardEvaluator().evaluate(
        permissions={"file_guard": {"enabled": False}},
        workspace_root=None,
        trusted_dirs=(),
        raw_paths=raw_paths,
    )

    assert result.status == "unevaluable"
    assert result.level == "ask"
    assert result.reason == reason


def test_enabled_guard_without_workspace_fails_closed(tmp_path: Path) -> None:
    result = SendFilePathGuardEvaluator().evaluate(
        permissions=_native_config(),
        workspace_root=None,
        trusted_dirs=(),
        raw_paths=(tmp_path / "report.md").as_posix(),
    )

    assert result.status == "unevaluable"
    assert result.level == "ask"
    assert result.reason == "send_file_path_guard_workspace_unavailable"


class _RecordingChecker:
    mode = "native"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict[str, str],
    ) -> PermissionResult | None:
        self.calls.append((tool_name, tool_args))
        path = tool_args[SEND_FILE_PATH_ARGUMENT]
        if path.endswith("ask.md"):
            return PermissionResult(
                permission=PermissionLevel.ASK,
                reason="ask path",
                matched_rule="ask-rule",
            )
        if path.endswith("deny.md"):
            return PermissionResult(
                permission=PermissionLevel.DENY,
                reason="deny path",
                matched_rule="deny-rule",
            )
        return None


def test_each_ordered_unique_native_path_is_evaluated_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checker = _RecordingChecker()
    monkeypatch.setattr(
        path_guard_module,
        "build_file_guard_checker",
        lambda *_args, **_kwargs: checker,
    )
    monkeypatch.setattr(
        path_guard_module,
        "_native_send_file_schema_is_valid",
        lambda: True,
    )
    allow = tmp_path / "allow.md"
    ask = tmp_path / "ask.md"
    deny = tmp_path / "deny.md"

    result = SendFilePathGuardEvaluator().evaluate(
        permissions={},
        workspace_root=tmp_path,
        trusted_dirs=(),
        raw_paths=[allow.as_posix(), ask.as_posix(), allow.as_posix(), deny.as_posix()],
    )

    assert result.level == "deny"
    assert result.ask_accesses == ((ask.resolve().as_posix(), "read"),)
    assert checker.calls == [
        (
            SEND_FILE_TOOL_NAME,
            {SEND_FILE_PATH_ARGUMENT: allow.resolve().as_posix()},
        ),
        (
            SEND_FILE_TOOL_NAME,
            {SEND_FILE_PATH_ARGUMENT: ask.resolve().as_posix()},
        ),
        (
            SEND_FILE_TOOL_NAME,
            {SEND_FILE_PATH_ARGUMENT: deny.resolve().as_posix()},
        ),
    ]


def test_conflicting_native_schema_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checker = _RecordingChecker()
    monkeypatch.setattr(
        path_guard_module,
        "build_file_guard_checker",
        lambda *_args, **_kwargs: checker,
    )
    monkeypatch.setattr(
        path_guard_module,
        "_native_send_file_schema_is_valid",
        lambda: False,
    )

    result = SendFilePathGuardEvaluator().evaluate(
        permissions={},
        workspace_root=tmp_path,
        trusted_dirs=(),
        raw_paths=(tmp_path / "report.md").as_posix(),
    )

    assert result.status == "unevaluable"
    assert result.level == "ask"
    assert result.reason == "send_file_path_guard_native_schema_unavailable"
    assert checker.calls == []


def test_checker_construction_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broken config")

    monkeypatch.setattr(path_guard_module, "build_file_guard_checker", _raise)

    result = SendFilePathGuardEvaluator().evaluate(
        permissions={},
        workspace_root=tmp_path,
        trusted_dirs=(),
        raw_paths=(tmp_path / "report.md").as_posix(),
    )

    assert result.status == "unevaluable"
    assert result.level == "ask"
    assert result.reason == "send_file_path_guard_checker_failed"


async def test_locked_engine_does_not_extract_original_send_argument(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    denied = tmp_path / "denied" / "secret.md"
    config = _native_config(denied_root=denied.parent)
    engine = PermissionEngine(config=config, workspace_root=workspace)

    raw_result = await engine.check_permission(
        SEND_FILE_TOOL_NAME,
        {"abs_file_path_list": denied.as_posix()},
    )
    adapted_result = SendFilePathGuardEvaluator().evaluate(
        permissions=config,
        workspace_root=workspace,
        trusted_dirs=(),
        raw_paths=denied.as_posix(),
    )

    assert raw_result.permission == PermissionLevel.ALLOW
    assert raw_result.matched_rule == f"tools.{SEND_FILE_TOOL_NAME}"
    assert adapted_result.level == "deny"
