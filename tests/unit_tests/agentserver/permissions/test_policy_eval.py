# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for policy-only permission evaluation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from openjiuwen.harness.security import PermissionLevel, PermissionResult

from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import (
    OpenJiuwenPolicyEvaluator,
)
from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import (
    SendFilePathGuardResult,
)


class FakeEngine:
    """Minimal OpenJiuwen PermissionEngine double."""

    def __init__(
        self, result: PermissionResult | None = None, *, raise_on_check: bool = False
    ) -> None:
        self.result = result or PermissionResult(permission=PermissionLevel.ASK)
        self.raise_on_check = raise_on_check
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.config_updates: list[dict[str, Any]] = []
        self.trusted_dirs: list[Any] = []

    def update_config(self, config: dict[str, Any]) -> None:
        """Record config updates."""
        self.config_updates.append(config)

    async def check_permission(
        self, *, tool_name: str, tool_args: dict[str, Any]
    ) -> PermissionResult:
        """Return the configured policy result."""
        self.calls.append((tool_name, tool_args))
        if self.raise_on_check:
            raise RuntimeError("policy failed")
        return self.result


class FakeRail:
    """Minimal OpenJiuwen PermissionInterruptRail double."""

    def __init__(
        self,
        *,
        engine: FakeEngine | None = None,
        scene_hook: Any | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        self._engine = engine
        self._static_config = {"defaults": {"*": "ask"}}
        self._host = SimpleNamespace(
            permission_scene_hook=scene_hook,
            get_permissions_snapshot=(lambda: snapshot)
            if snapshot is not None
            else None,
        )
        self.config_updates: list[dict[str, Any]] = []
        self.confirmation_calls = 0

    def _normalize_tool_name(self, tool_name: str) -> str:
        return "mcp_exec_command" if tool_name == "exec_command" else tool_name

    @staticmethod
    def _parse_tool_args(tool_call: object | None) -> dict[str, Any]:
        args = getattr(tool_call, "arguments", {})
        return dict(args) if isinstance(args, dict) else {}

    def update_config(self, config: dict[str, Any]) -> None:
        self.config_updates.append(config)

    async def before_tool_call(self, *_args: object, **_kwargs: object) -> None:
        self.confirmation_calls += 1
        return None


class FakePathGuardEvaluator:
    """Record adapter inputs and return one fixed path decision."""

    def __init__(self, result: SendFilePathGuardResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def evaluate(self, **kwargs: Any) -> SendFilePathGuardResult:
        self.calls.append(kwargs)
        return self.result


def _invocation(
    tool_name: str = "read_file", tool_args: dict[str, Any] | None = None
) -> SimpleNamespace:
    args = tool_args or {"path": "README.md"}
    return SimpleNamespace(
        ctx=SimpleNamespace(session=None, extra={}),
        tool_call=SimpleNamespace(name=tool_name, arguments=args),
        tool_name=tool_name,
        tool_args=args,
    )


async def test_policy_only_returns_deny_without_triggering_confirmation() -> None:
    engine = FakeEngine(
        PermissionResult(
            permission=PermissionLevel.DENY,
            matched_rule="deny_readme",
            reason="blocked by rule",
        )
    )
    base = FakeRail(engine=engine)
    evaluator = OpenJiuwenPolicyEvaluator(base)

    result = await evaluator.evaluate(_invocation())

    assert result.level == "deny"
    assert result.reason == "blocked by rule"
    assert result.matched_rule == "deny_readme"
    assert base.confirmation_calls == 0
    assert engine.calls == [("read_file", {"path": "README.md"})]


async def test_policy_only_canonicalizes_permission_tool_aliases() -> None:
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(
        _invocation("free_search", {"query": "Agentic AI security news"})
    )

    assert result.level == "allow"
    assert engine.calls == [("mcp_free_search", {"query": "Agentic AI security news"})]


async def test_policy_only_falsy_tool_names_use_empty_policy_key() -> None:
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ASK))
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(_invocation(None, {"query": "public news"}))

    assert result.level == "ask"
    assert engine.calls == [("", {"query": "public news"})]


async def test_policy_only_scene_hook_rejects_before_engine() -> None:
    async def scene_hook(_inp: object) -> tuple[str, str]:
        return ("reject", "[PERMISSION_DENIED] scene")

    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine, scene_hook=scene_hook)
    )

    result = await evaluator.evaluate(_invocation())

    assert result.level == "deny"
    assert result.reason == "[PERMISSION_DENIED] scene"
    assert result.source == "scene_hook"
    assert engine.calls == []


async def test_policy_only_scene_hook_allow_cannot_override_engine_deny() -> None:
    async def scene_hook(_inp: object) -> tuple[str]:
        return ("approve",)

    engine = FakeEngine(PermissionResult(permission=PermissionLevel.DENY))
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine, scene_hook=scene_hook)
    )

    result = await evaluator.evaluate(_invocation())

    assert result.level == "deny"
    assert result.source == "permission_engine"
    assert engine.calls == [("read_file", {"path": "README.md"})]


async def test_send_path_deny_is_stricter_than_engine_allow(tmp_path) -> None:
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    path = (tmp_path / "blocked.md").resolve().as_posix()
    path_result = SendFilePathGuardResult(
        status="evaluated",
        level="deny",
        reason="file_guard denied",
        canonical_paths=(path,),
        matched_rules=("file_guard:deny",),
    )
    path_evaluator = FakePathGuardEvaluator(path_result)
    runtime_config = {"tools": {"send_file_to_user": "allow"}}
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine),
        permission_config_getter=lambda: runtime_config,
        workspace_root=tmp_path,
        path_guard_evaluator=path_evaluator,
    )

    result = await evaluator.evaluate(
        _invocation("send_file_to_user", {"abs_file_path_list": path})
    )

    assert result.level == "deny"
    assert result.source == "file_guard_adapter"
    assert result.path_result is path_result
    assert result.matched_rule == "file_guard:deny"
    assert path_evaluator.calls == [
        {
            "permissions": runtime_config,
            "workspace_root": tmp_path,
            "trusted_dirs": [],
            "raw_paths": path,
        }
    ]


async def test_scene_allow_cannot_override_send_path_ask(tmp_path) -> None:
    async def scene_hook(_inp: object) -> tuple[str]:
        return ("approve",)

    path = (tmp_path / "external.md").resolve().as_posix()
    path_result = SendFilePathGuardResult(
        status="evaluated",
        level="ask",
        reason="file_guard requires approval",
        canonical_paths=(path,),
        matched_rules=("file_guard:defaults",),
        ask_accesses=((path, "read"),),
    )
    path_evaluator = FakePathGuardEvaluator(path_result)
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine, scene_hook=scene_hook),
        permission_config_getter=lambda: {},
        workspace_root=tmp_path,
        path_guard_evaluator=path_evaluator,
    )

    result = await evaluator.evaluate(
        _invocation("send_file_to_user", {"abs_file_path_list": path})
    )

    assert result.level == "ask"
    assert result.source == "file_guard_adapter"
    assert result.path_result is path_result
    assert engine.calls == [
        ("send_file_to_user", {"abs_file_path_list": path})
    ]


async def test_path_allow_does_not_downgrade_engine_ask(tmp_path) -> None:
    path = (tmp_path / "trusted.md").resolve().as_posix()
    path_result = SendFilePathGuardResult(
        status="evaluated",
        level="allow",
        reason="send_file_path_guard_allowed",
        canonical_paths=(path,),
    )
    path_evaluator = FakePathGuardEvaluator(path_result)
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ASK))
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine),
        permission_config_getter=lambda: {},
        workspace_root=tmp_path,
        path_guard_evaluator=path_evaluator,
    )

    result = await evaluator.evaluate(
        _invocation("send_file_to_user", {"abs_file_path_list": path})
    )

    assert result.level == "ask"
    assert result.source == "permission_engine"
    assert result.path_result is path_result


async def test_policy_only_returns_ask_without_interrupt() -> None:
    engine = FakeEngine(
        PermissionResult(permission=PermissionLevel.ASK, reason="needs approval")
    )
    base = FakeRail(engine=engine)
    evaluator = OpenJiuwenPolicyEvaluator(base)

    result = await evaluator.evaluate(_invocation())

    assert result.level == "ask"
    assert result.reason == "needs approval"
    assert base.confirmation_calls == 0


async def test_policy_only_scene_hook_failure_falls_back_to_engine() -> None:
    async def scene_hook(_inp: object) -> tuple[str]:
        raise RuntimeError("scene hook failed")

    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    evaluator = OpenJiuwenPolicyEvaluator(
        FakeRail(engine=engine, scene_hook=scene_hook)
    )

    result = await evaluator.evaluate(_invocation())

    assert result.level == "allow"
    assert engine.calls == [("read_file", {"path": "README.md"})]


async def test_policy_only_engine_failure_fails_closed_to_ask() -> None:
    engine = FakeEngine(raise_on_check=True)
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(_invocation())

    assert result.level == "ask"
    assert result.source == "fail_closed"
    assert result.reason == "policy_engine_check_failed"


async def test_policy_only_preserves_valid_engine_external_paths() -> None:
    engine = FakeEngine(
        PermissionResult(
            permission=PermissionLevel.ASK,
            reason="needs approval",
            external_paths=["/tmp/input.md", "/tmp/output.md"],
        )
    )
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(_invocation())

    assert result.source == "permission_engine"
    assert result.external_paths == ("/tmp/input.md", "/tmp/output.md")


async def test_policy_only_malformed_external_paths_fail_closed_to_ask() -> None:
    engine = FakeEngine()
    engine.result = SimpleNamespace(
        permission=PermissionLevel.ALLOW,
        reason="allow",
        matched_rule="builtin",
        external_paths=("/tmp/not-a-list",),
    )
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(_invocation())

    assert result.level == "ask"
    assert result.source == "fail_closed"
    assert result.reason == "malformed_policy_external_paths"
    assert result.external_paths == ()


async def test_policy_only_deny_remains_terminal_with_malformed_external_paths() -> None:
    engine = FakeEngine()
    engine.result = SimpleNamespace(
        permission=PermissionLevel.DENY,
        reason="blocked",
        matched_rule="deny-rule",
        external_paths={"path": "/tmp/value"},
    )
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=engine))

    result = await evaluator.evaluate(_invocation())

    assert result.level == "deny"
    assert result.reason == "blocked"
    assert result.external_paths == ()


async def test_policy_only_refreshes_config_snapshot() -> None:
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ALLOW))
    snapshot = {"defaults": {"*": "deny"}}
    base = FakeRail(engine=engine, snapshot=snapshot)
    evaluator = OpenJiuwenPolicyEvaluator(base)

    result = await evaluator.evaluate(_invocation())

    assert result.level == "allow"
    assert base.config_updates == [snapshot]
    assert engine.config_updates == []


async def test_policy_only_prefers_runtime_config_over_host_snapshot() -> None:
    engine = FakeEngine(PermissionResult(permission=PermissionLevel.ASK))
    snapshot = {"enabled": False, "defaults": {"*": "allow"}}
    runtime_config = {"enabled": True, "defaults": {"*": "ask"}}
    base = FakeRail(engine=engine, snapshot=snapshot)
    evaluator = OpenJiuwenPolicyEvaluator(
        base,
        permission_config_getter=lambda: runtime_config,
    )

    result = await evaluator.evaluate(_invocation("bash", {"cmd": "pytest tests"}))

    assert result.level == "ask"
    assert base.config_updates == [runtime_config]
    assert engine.config_updates == []


async def test_policy_only_missing_engine_fails_closed_to_ask() -> None:
    evaluator = OpenJiuwenPolicyEvaluator(FakeRail(engine=None))

    result = await evaluator.evaluate(_invocation())

    assert result.level == "ask"
    assert result.source == "fail_closed"
    assert result.reason == "policy_engine_unavailable"
