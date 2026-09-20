from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import (
    invocation_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.tools.command_runtime import (
    CommandRuntimePaths,
    current_command_runtime_paths,
    resolve_command_workdir,
)
from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
    AutoPermissionInterruptRail,
    AutoReviewer,
    FakeBaseRail,
    PolicyEvaluation,
    ReviewerOutcome,
    StaticPolicyEvaluator,
    StaticReviewerClient,
    _jiuwenbox_sys_operation,
)


def _runtime_paths(tmp_path: Path) -> CommandRuntimePaths:
    workspace = tmp_path / "workspace"
    cwd = workspace / "project"
    cwd.mkdir(parents=True)
    return CommandRuntimePaths(
        current_cwd=cwd,
        project_root=workspace,
        workspace_root=workspace,
        agent_workspace_root=workspace,
    )


@pytest.mark.parametrize(
    ("workdir", "suffix"),
    [
        (None, "project"),
        ("", "project"),
        (".", "project"),
        ("outputs", "project/outputs"),
    ],
)
def test_resolves_workdir_from_runtime_cwd(
    tmp_path: Path,
    workdir: object,
    suffix: str,
) -> None:
    paths = _runtime_paths(tmp_path)

    resolved = resolve_command_workdir(workdir, runtime_paths=paths)

    assert resolved == paths.workspace_root / suffix


def test_resolves_absolute_workdir_without_rebasing(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    target = paths.workspace_root / "other"

    assert resolve_command_workdir(str(target), runtime_paths=paths) == target


def test_required_runtime_cwd_does_not_guess_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjiuwen.core.sys_operation import cwd as cwd_module

    monkeypatch.setattr(
        cwd_module,
        "get_cwd",
        lambda: (_ for _ in ()).throw(RuntimeError("missing")),
    )

    with pytest.raises(ValueError, match="runtime cwd is unavailable"):
        current_command_runtime_paths(require_runtime_cwd=True)


@pytest.mark.parametrize("workdir", ["../../outside", 42, ["."]])
def test_rejects_invalid_or_external_workdir(
    tmp_path: Path,
    workdir: object,
) -> None:
    paths = _runtime_paths(tmp_path)

    with pytest.raises(ValueError):
        resolve_command_workdir(workdir, runtime_paths=paths)
def test_freezes_effective_workdir_into_host_execution_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _runtime_paths(tmp_path)
    tool_call = SimpleNamespace(
        name="mcp_exec_command",
        arguments={"command": "pwd", "workdir": "outputs"},
    )
    inputs = SimpleNamespace(
        tool_call=tool_call,
        tool_name="mcp_exec_command",
        tool_args=tool_call.arguments,
    )
    invocation = ToolInvocation(
        ctx=SimpleNamespace(inputs=inputs),
        tool_call=tool_call,
        tool_name="mcp_exec_command",
        tool_args={
            "command": "pwd",
            "workdir": "outputs",
            "call_goal": "display only",
        },
    )
    kwargs = {"tool_args": invocation.tool_args}
    monkeypatch.setattr(
        invocation_context,
        "current_command_runtime_paths",
        lambda **_kwargs: paths,
    )

    frozen, error = invocation_context._normalize_command_invocation_for_execution(
        invocation,
        kwargs,
    )

    expected = {
        "command": "pwd",
        "workdir": str(paths.current_cwd / "outputs"),
    }
    assert error == ""
    assert frozen.tool_args == expected
    assert tool_call.arguments == expected
    assert inputs.tool_args == expected
    assert kwargs["tool_args"] == expected
    facts = build_tool_decision_facts(
        frozen.tool_name,
        frozen.tool_args,
        workspace_root=paths.workspace_root,
        original_args_were_valid_object=True,
    )
    assert dict(facts.untrusted_args) == expected
    assert facts.command == "pwd"


def test_rejects_non_authoritative_cwd_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _runtime_paths(tmp_path)
    invocation = ToolInvocation(
        ctx=SimpleNamespace(inputs=None),
        tool_call=SimpleNamespace(arguments={}),
        tool_name="mcp_exec_command",
        tool_args={"command": "pwd", "cwd": str(paths.current_cwd)},
    )
    monkeypatch.setattr(
        invocation_context,
        "current_command_runtime_paths",
        lambda **_kwargs: paths,
    )

    frozen, error = invocation_context._normalize_command_invocation_for_execution(
        invocation,
        {},
    )

    assert frozen is invocation
    assert error == "command_workdir_contract_invalid"


def test_writeback_failure_does_not_publish_frozen_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _runtime_paths(tmp_path)
    invocation = ToolInvocation(
        ctx=SimpleNamespace(inputs=None),
        tool_call=MappingProxyType({"arguments": {"command": "pwd"}}),
        tool_name="mcp_exec_command",
        tool_args={"command": "pwd"},
    )
    monkeypatch.setattr(
        invocation_context,
        "current_command_runtime_paths",
        lambda **_kwargs: paths,
    )

    frozen, error = invocation_context._normalize_command_invocation_for_execution(
        invocation,
        {},
    )

    assert frozen is invocation
    assert error == "command_workdir_writeback_failed"


class _PermissionCallbacks:
    def __init__(self, rail: AutoPermissionInterruptRail) -> None:
        self.rail = rail

    async def execute(
        self,
        event: AgentCallbackEvent,
        ctx: AgentCallbackContext,
    ) -> None:
        if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
            await self.rail.before_tool_call(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("workdir", [None, ".", "outputs"])
async def test_real_ability_manager_uses_policy_frozen_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workdir: str | None,
) -> None:
    paths = _runtime_paths(tmp_path)
    sys_operation, _provider = _jiuwenbox_sys_operation()
    policy = StaticPolicyEvaluator(PolicyEvaluation(level="allow", reason="allow"))
    rail = AutoPermissionInterruptRail(
        base_rail=FakeBaseRail(),
        permission_config={"mode": "auto", "enabled": True},
        workspace_root=paths.workspace_root,
        sys_operation=sys_operation,
        policy_evaluator=policy,
        auto_reviewer=AutoReviewer(
            client=StaticReviewerClient(outcome=ReviewerOutcome.ALLOW_ONCE)
        ),
    )
    monkeypatch.setattr(
        invocation_context,
        "current_command_runtime_paths",
        lambda **_kwargs: paths,
    )
    manager = AbilityManager()
    executed: list[Any] = []

    async def execute_tool(**kwargs: Any) -> tuple[str, ToolMessage]:
        executed.append(json.loads(kwargs["tool_call"].arguments))
        return "ok", ToolMessage(content="ok", tool_call_id="tc-command")

    manager._execute_single_tool_call = execute_tool
    args = {"command": "pwd", "call_goal": "display only"}
    if workdir is not None:
        args["workdir"] = workdir
    tool_call = ToolCall(
        id="tc-command",
        type="function",
        name="mcp_exec_command",
        arguments=json.dumps(args),
    )
    parent = AgentCallbackContext(
        agent=SimpleNamespace(
            agent_callback_manager=_PermissionCallbacks(rail),
        )
    )

    await manager.execute(parent, tool_call, session=SimpleNamespace(session_id="s1"))

    expected_cwd = paths.current_cwd / ("outputs" if workdir == "outputs" else "")
    expected = {"command": "pwd", "workdir": str(expected_cwd)}
    assert executed == [expected]
    assert policy.calls
    assert all(call.tool_args == expected for call in policy.calls)
    facts = build_tool_decision_facts(
        "mcp_exec_command",
        executed[0],
        workspace_root=paths.workspace_root,
        original_args_were_valid_object=True,
    )
    assert dict(facts.untrusted_args) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["external", "writeback"])
async def test_real_ability_manager_fails_before_policy_reviewer_or_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    paths = _runtime_paths(tmp_path)
    policy = StaticPolicyEvaluator(PolicyEvaluation(level="allow", reason="allow"))
    reviewer = StaticReviewerClient(outcome=ReviewerOutcome.ALLOW_ONCE)
    rail = AutoPermissionInterruptRail(
        base_rail=FakeBaseRail(),
        permission_config={"mode": "auto", "enabled": True},
        workspace_root=paths.workspace_root,
        policy_evaluator=policy,
        auto_reviewer=AutoReviewer(client=reviewer),
    )
    monkeypatch.setattr(
        invocation_context,
        "current_command_runtime_paths",
        lambda **_kwargs: paths,
    )
    if failure == "writeback":
        monkeypatch.setattr(
            invocation_context,
            "_write_invocation_tool_args",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
        )
    manager = AbilityManager()
    executed: list[Any] = []

    async def execute_tool(**kwargs: Any) -> tuple[str, ToolMessage]:
        executed.append(kwargs["tool_call"].arguments)
        return "ok", ToolMessage(content="ok", tool_call_id="tc-command")

    manager._execute_single_tool_call = execute_tool
    tool_call = ToolCall(
        id="tc-command",
        type="function",
        name="mcp_exec_command",
        arguments=json.dumps(
            {
                "command": "pwd",
                "workdir": "../../outside" if failure == "external" else ".",
            }
        ),
    )
    parent = AgentCallbackContext(
        agent=SimpleNamespace(agent_callback_manager=_PermissionCallbacks(rail))
    )

    await manager.execute(parent, tool_call, session=SimpleNamespace(session_id="s1"))

    assert len(policy.calls) == (0 if failure == "writeback" else 1)
    assert reviewer.requests == []
    assert executed == []
