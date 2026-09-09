# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime characterization tests for permission integration contracts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.rails.team_permission_rail import TeamPermissionRail
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
)
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import (
    PermissionInterruptRail,
)
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.models import PermissionLevel

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)


class _ReplaceablePermissionRail(AgentRail):
    """A distinct rail type used to characterize exact-type hot replacement."""

    def __init__(self, *, fail_initialization: bool = False) -> None:
        self._fail_initialization = fail_initialization
        self.callback_calls = 0

    def init(self, agent: DeepAgent) -> None:
        if self._fail_initialization:
            raise RuntimeError("candidate initialization failed")

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        self.callback_calls += 1


@pytest.mark.asyncio
async def test_openjiuwen_hot_reload_replaces_same_type_rail_once() -> None:
    """The pinned SDK retires the old rail before registering its replacement."""
    old_rail = _ReplaceablePermissionRail()
    replacement_rail = _ReplaceablePermissionRail()
    agent = DeepAgent(AgentCard(name="permission-runtime-contract"))
    callback_event = agent._agent_callback_manager._get_agent_event(
        AgentCallbackEvent.BEFORE_INVOKE
    )

    try:
        agent.configure(
            DeepAgentConfig(
                rails=[old_rail],
                auto_create_workspace=False,
            )
        )
        await agent.ensure_initialized()
        assert agent._registered_rails == [old_rail]
        assert len(Runner.callback_framework.list_callbacks(callback_event)) == 1

        agent.configure(
            DeepAgentConfig(
                rails=[replacement_rail],
                auto_create_workspace=False,
            )
        )
        assert agent._registered_rails == []
        assert agent._stale_rails == [old_rail]
        assert agent._pending_rails == [replacement_rail]

        await agent.ensure_initialized()
        assert agent._registered_rails == [replacement_rail]
        assert agent._stale_rails == []
        assert agent._pending_rails == []
        assert len(Runner.callback_framework.list_callbacks(callback_event)) == 1

        await agent.ensure_initialized()
        assert agent._registered_rails == [replacement_rail]
        assert len(Runner.callback_framework.list_callbacks(callback_event)) == 1
    finally:
        await agent._agent_callback_manager.clear()


@pytest.mark.asyncio
async def test_openjiuwen_initialization_failure_blocks_public_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed candidate does not mark the agent initialized for invocation."""
    old_rail = _ReplaceablePermissionRail()
    failing_rail = _ReplaceablePermissionRail(fail_initialization=True)
    agent = DeepAgent(AgentCard(name="permission-runtime-failure-contract"))
    execute_round = AsyncMock()
    monkeypatch.setattr(agent, "_run_single_round_invoke", execute_round)

    try:
        agent.configure(
            DeepAgentConfig(
                rails=[old_rail],
                auto_create_workspace=False,
            )
        )
        await agent.ensure_initialized()

        agent.configure(
            DeepAgentConfig(
                rails=[failing_rail],
                auto_create_workspace=False,
            )
        )
        with pytest.raises(RuntimeError, match="candidate initialization failed"):
            await agent.invoke({"query": "must not execute"})

        assert agent._initialized is False
        assert old_rail not in agent._registered_rails
        assert failing_rail not in agent._registered_rails
        assert failing_rail in agent._pending_rails
        execute_round.assert_not_awaited()
    finally:
        await agent._agent_callback_manager.clear()




@pytest.mark.asyncio
async def test_manual_resume_identity_survives_permission_rail_replacement() -> None:
    """The replacement rail resolves only the answer for the original tool-call id."""
    old_rail = build_permission_rail({"permissions": {"enabled": True}})
    replacement_rail = build_permission_rail({"permissions": {"enabled": True}})
    assert old_rail is not None
    assert replacement_rail is not None
    agent = DeepAgent(AgentCard(name="permission-manual-resume"))

    try:
        agent.configure(
            DeepAgentConfig(
                rails=[old_rail],
                auto_create_workspace=False,
            )
        )
        await agent.ensure_initialized()
        agent.configure(
            DeepAgentConfig(
                rails=[replacement_rail],
                auto_create_workspace=False,
            )
        )
        await agent.ensure_initialized()

        resume_input = InteractiveInput()
        resume_input.update(
            "tool-call-reload",
            {
                "approved": True,
                "auto_confirm": False,
                "persist_allow": False,
            },
        )
        ctx = AgentCallbackContext(
            agent=agent,
            extra={RESUME_USER_INPUT_KEY: resume_input},
        )
        tool_call = SimpleNamespace(
            id="tool-call-reload",
            name="bash",
            arguments={"command": "pwd"},
        )

        tool_call_id = replacement_rail._resolve_tool_call_id(tool_call)
        user_input = replacement_rail._get_user_input(ctx, tool_call_id)
        decision = await replacement_rail.resolve_interrupt(
            ctx,
            tool_call,
            user_input,
        )

        assert tool_call_id == "tool-call-reload"
        assert isinstance(decision, ApproveResult)
        assert replacement_rail._get_user_input(ctx, "different-call") is None
    finally:
        await agent._agent_callback_manager.clear()
        if agent._react_agent is not None:
            await agent._react_agent.agent_callback_manager.clear()


def test_team_permission_rail_keeps_leader_scoped_confirmation_contract() -> None:
    """Current TeamPermissionRail does not persist and records the leader owner."""
    rail = TeamPermissionRail(config={"enabled": True})

    response = rail.parse_confirm_payload(
        {
            "approved": True,
            "auto_confirm": True,
            "persist_allow": True,
        }
    )

    assert rail._persist_allow_always("bash", {"command": "pwd"}) is False
    assert response is not None
    assert response.approved is True
    assert response.decided_by == "leader"


def _native_file_guard_config(
    *,
    workspace_root: Path,
    denied_root: Path | None = None,
    allowed_root: Path | None = None,
) -> dict[str, object]:
    path_rules: list[dict[str, str]] = []
    if denied_root is not None:
        path_rules.append(
            {
                "path": denied_root.as_posix(),
                "read": "deny",
                "write": "deny",
                "exec": "deny",
                "match": "prefix",
            }
        )
    if allowed_root is not None:
        path_rules.append(
            {
                "path": allowed_root.as_posix(),
                "read": "allow",
                "write": "allow",
                "exec": "ask",
                "match": "prefix",
            }
        )
    return {
        "enabled": True,
        "schema": "tiered_policy",
        "defaults": {"*": "allow"},
        "tools": {"write_file": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "allow", "exec": "ask"},
            "paths": path_rules,
        },
    }


@pytest.mark.asyncio
async def test_latest_openjiuwen_file_guard_combines_native_path_matrix(
    tmp_path: Path,
) -> None:
    """Pipeline B keeps workspace allow, external ask, and deny prefix terminal."""
    workspace_root = tmp_path / "workspace"
    denied_root = tmp_path / "external" / "denied"
    config = _native_file_guard_config(
        workspace_root=workspace_root,
        denied_root=denied_root,
    )
    engine = PermissionEngine(config=config, workspace_root=workspace_root)

    workspace_result = await engine.check_permission(
        "write_file",
        {"path": (workspace_root / "report.md").as_posix(), "content": "ok"},
    )
    external_result = await engine.check_permission(
        "write_file",
        {"path": (tmp_path / "external" / "report.md").as_posix(), "content": "ask"},
    )
    denied_result = await engine.check_permission(
        "write_file",
        {"path": (denied_root / "report.md").as_posix(), "content": "deny"},
    )

    assert workspace_result.permission == PermissionLevel.ALLOW
    assert external_result.permission == PermissionLevel.ASK
    assert denied_result.permission == PermissionLevel.DENY
    assert "file_guard:defaults" in str(external_result.matched_rule)
    assert f"file_guard:prefix:{denied_root.as_posix()}" in str(
        denied_result.matched_rule
    )


@pytest.mark.asyncio
async def test_same_permission_rail_reads_updated_host_snapshot(
    tmp_path: Path,
) -> None:
    """The next check on one rail rebuilds file_guard from the host snapshot."""
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    snapshot = _native_file_guard_config(workspace_root=workspace_root)
    host = ToolPermissionHost(
        get_permissions_snapshot=lambda: deepcopy(snapshot),
        resolve_workspace_dir=lambda: workspace_root,
    )
    rail = PermissionInterruptRail(config=deepcopy(snapshot), host=host)
    tool_call = SimpleNamespace(
        id="tool-call-file-guard-refresh",
        name="write_file",
        arguments={
            "path": (external_root / "report.md").as_posix(),
            "content": "updated",
        },
    )
    ctx = SimpleNamespace(session=None)

    before = await rail.resolve_interrupt(ctx, tool_call, None)
    snapshot = _native_file_guard_config(
        workspace_root=workspace_root,
        allowed_root=external_root,
    )
    after = await rail.resolve_interrupt(ctx, tool_call, None)

    assert isinstance(before, InterruptResult)
    assert isinstance(after, ApproveResult)


@pytest.mark.asyncio
async def test_failed_file_guard_persistence_rolls_back_live_engine(
    tmp_path: Path,
) -> None:
    """A failed host persistence callback restores the prior path decision."""
    workspace_root = tmp_path / "workspace"
    external_path = tmp_path / "external" / "report.md"
    snapshot = _native_file_guard_config(workspace_root=workspace_root)
    persisted_candidates: list[dict[str, object]] = []

    def _reject_persistence(candidate: dict[str, object]) -> bool:
        persisted_candidates.append(deepcopy(candidate))
        return False

    rail = PermissionInterruptRail(
        config=deepcopy(snapshot),
        host=ToolPermissionHost(
            get_permissions_snapshot=lambda: deepcopy(snapshot),
            persist_allow_rule=_reject_persistence,
            resolve_workspace_dir=lambda: workspace_root,
        ),
    )
    args = {"path": external_path.as_posix(), "content": "blocked"}
    before = await rail._engine.check_permission("write_file", args)

    persisted = rail._persist_allow_always("write_file", args)
    after = await rail._engine.check_permission("write_file", args)

    assert before.permission == PermissionLevel.ASK
    assert persisted is False
    assert persisted_candidates
    assert persisted_candidates[0]["file_guard"] != snapshot["file_guard"]
    assert after.permission == PermissionLevel.ASK
