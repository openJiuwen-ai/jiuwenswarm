# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System tests for distributed remote spawn fail-fast and scheme-B readiness.

Covers commit ``fix(teams): add a fail-fast check when distributed team member does not exist``:
- A2X blank reservation before roster DB write
- spawn_teammate wrapper: UNSTARTED after insert, READY only after MESSAGE ACK
- bootstrap delivery failure marks roster row ``error`` instead of silent success
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system]

_BOOTSTRAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "team"
    / "remote_member_bootstrap.py"
)
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "remote_member_bootstrap_st_module",
    _BOOTSTRAP_PATH,
)
assert _BOOTSTRAP_SPEC is not None and _BOOTSTRAP_SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(bootstrap)


def _distributed_leader_cfg(*, remote_names: list[str] | None = None) -> dict:
    metadata: dict = {}
    if remote_names is not None:
        metadata["jiuwen_remote_member_names"] = remote_names
    return {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
            "metadata": metadata,
        }
    }


def _mock_a2x_reservation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reservation: SimpleNamespace | None = None,
) -> SimpleNamespace:
    hold = reservation or SimpleNamespace(
        service_id="a2x-svc-st",
        endpoint="tcp://127.0.0.1:28612",
        dataset="team_pool",
        release=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.reserve_blank_teammate_agent",
        AsyncMock(return_value=hold),
    )
    return hold


def _leader_team_agent(
    *,
    team_name: str = "dist-team-st",
    get_member_return: object | None = None,
) -> SimpleNamespace:
    from openjiuwen.agent_teams.schema.team import TeamRole

    db = MagicMock()
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    db.get_message = AsyncMock()
    team_backend = SimpleNamespace(
        db=db,
        get_member=AsyncMock(return_value=get_member_return),
        shutdown_member=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    return SimpleNamespace(
        role=TeamRole.LEADER,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.spawn_teammate", name="spawn_teammate")]
            ),
            card=SimpleNamespace(id="leader-card-st"),
        ),
        team_backend=team_backend,
        message_manager=MagicMock(),
        _team_name=lambda: team_name,
        _member_name=lambda: "team_leader",
        add_event_listener=lambda cb: None,
    )


def _install_spawn_tool(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.tools.base_tool import ToolOutput

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            return ToolOutput(
                success=True,
                data={"member_name": (inputs or {}).get("member_name")},
            )

    tool = _SpawnTeammateTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_a, **_k: tool),
    )
    return tool


def _ack_message_event(*, message_id: str, from_member: str, to_member: str = "team_leader") -> SimpleNamespace:
    from openjiuwen.agent_teams.schema.events import TeamEvent

    return SimpleNamespace(
        event_type=TeamEvent.MESSAGE,
        payload={
            "message_id": message_id,
            "from_member_name": from_member,
            "to_member_name": to_member,
        },
    )


@pytest.mark.asyncio
async def test_leader_precheck_reserves_blank_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public precheck API must reserve A2X blank before any roster write."""
    cfg = _distributed_leader_cfg()
    reservation = _mock_a2x_reservation(monkeypatch)
    gate = await bootstrap.precheck_and_reserve_remote_spawn("teammate-st-1", cfg)
    assert gate.error is None
    assert gate.registry_reservation is reservation


@pytest.mark.asyncio
async def test_leader_spawn_blocked_when_no_blank_teammate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-fast: no idle blank in registry → spawn_teammate never hits DB tool."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(remote_names=["teammate-st-1"]),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.reserve_blank_teammate_agent",
        AsyncMock(return_value=None),
    )
    orig_calls: list[str] = []

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            orig_calls.append(str((inputs or {}).get("member_name")))
            return ToolOutput(success=True)

    from openjiuwen.core.runner import Runner

    tool = _SpawnTeammateTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_a, **_k: tool),
    )
    team_agent = _leader_team_agent()
    bootstrap.attach_spawn_teammate_remote_bootstrap_wrapper(
        team_agent, session_id="sess-no-blank", channel_id="web"
    )

    result = await tool.invoke(
        {
            "member_name": "teammate-st-1",
            "display_name": "T1",
            "desc": "remote",
            "prompt": "go",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is False
    assert "blank teammate" in (result.error or "").lower()
    assert orig_calls == []


@pytest.mark.asyncio
async def test_leader_spawn_blocked_when_member_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-fast: duplicate member_name in roster → no second DB insert."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(remote_names=["teammate-st-1"]),
    )
    _mock_a2x_reservation(monkeypatch)
    orig_calls: list[str] = []

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            orig_calls.append("db-write")
            return ToolOutput(success=True)

    from openjiuwen.core.runner import Runner

    tool = _SpawnTeammateTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_a, **_k: tool),
    )
    team_agent = _leader_team_agent(
        get_member_return=SimpleNamespace(member_name="teammate-st-1", status="ready"),
    )
    bootstrap.attach_spawn_teammate_remote_bootstrap_wrapper(
        team_agent, session_id="sess-dup", channel_id="web"
    )

    result = await tool.invoke(
        {
            "member_name": "teammate-st-1",
            "display_name": "T1",
            "desc": "remote",
            "prompt": "go",
        }
    )

    assert result.success is False
    assert "already exists" in (result.error or "").lower()
    assert orig_calls == []


@pytest.mark.asyncio
async def test_leader_spawn_unstarted_then_ack_sets_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheme B: bootstrap delivered keeps UNSTARTED; MESSAGE ACK listener sets ready."""
    from openjiuwen.agent_teams.schema.status import MemberStatus

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(remote_names=["teammate-st-1"]),
    )
    _mock_a2x_reservation(monkeypatch)
    _install_spawn_tool(monkeypatch)
    monkeypatch.setattr(bootstrap, "send_bootstrap_message", AsyncMock(return_value=True))

    team_agent = _leader_team_agent(team_name="team-scheme-b")
    db = team_agent.team_backend.db
    db.get_message = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(
                bootstrap.build_bootstrap_ack_envelope(
                    member_name="teammate-st-1",
                    team_name="team-scheme-b",
                    handshake_applied=True,
                )
            ),
            from_member_name="teammate-st-1",
            to_member_name="team_leader",
        )
    )
    mm = SimpleNamespace(mark_message_read=AsyncMock(return_value=True))
    team_agent.message_manager = mm

    ack_listeners: list = []

    def _add_listener(cb):
        ack_listeners.append(cb)

    team_agent.add_event_listener = _add_listener
    # Precheck must see no row; ACK listener must see the roster row.
    team_agent.team_backend.get_member = AsyncMock(
        side_effect=[
            None,
            SimpleNamespace(status=MemberStatus.UNSTARTED.value),
        ]
    )

    bootstrap.attach_spawn_teammate_remote_bootstrap_wrapper(
        team_agent, session_id="sess-scheme-b", channel_id="web"
    )
    bootstrap.attach_remote_bootstrap_ack_listener(
        team_agent, session_id="sess-scheme-b", channel_id="web"
    )
    assert len(ack_listeners) == 1

    from openjiuwen.core.runner import Runner

    tool = Runner.resource_mgr.get_tool("team.spawn_teammate")
    result = await tool.invoke(
        {
            "member_name": "teammate-st-1",
            "display_name": "T1",
            "desc": "remote",
            "prompt": "go",
        }
    )
    assert result.success is True
    db.update_member_status.assert_any_await(
        "teammate-st-1", "team-scheme-b", MemberStatus.UNSTARTED.value
    )
    ready_calls = [
        c
        for c in db.update_member_status.await_args_list
        if c.args[2] == MemberStatus.READY.value
    ]
    assert ready_calls == []

    await ack_listeners[0](
        _ack_message_event(message_id="msg-ack-1", from_member="teammate-st-1")
    )
    db.update_member_status.assert_any_await(
        "teammate-st-1", "team-scheme-b", MemberStatus.READY.value
    )
    mm.mark_message_read.assert_awaited_once_with("msg-ack-1", "team_leader")


@pytest.mark.asyncio
async def test_leader_spawn_fail_fast_when_bootstrap_not_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap not delivered: ToolOutput failure and roster row marked error (no delete rollback)."""
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(remote_names=["teammate-st-2"]),
    )
    _mock_a2x_reservation(monkeypatch)
    _install_spawn_tool(monkeypatch)
    monkeypatch.setattr(bootstrap, "send_bootstrap_message", AsyncMock(return_value=False))

    team_agent = _leader_team_agent(team_name="team-fail-deliver")
    bootstrap.attach_spawn_teammate_remote_bootstrap_wrapper(
        team_agent, session_id="sess-fail-deliver", channel_id="web"
    )

    from openjiuwen.core.runner import Runner

    tool = Runner.resource_mgr.get_tool("team.spawn_teammate")
    result = await tool.invoke(
        {
            "member_name": "teammate-st-2",
            "display_name": "T2",
            "desc": "remote",
            "prompt": "go",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is False
    assert "not delivered" in (result.error or "").lower()
    team_agent.team_backend.db.update_member_status.assert_any_await(
        "teammate-st-2",
        "team-fail-deliver",
        MemberStatus.ERROR.value,
    )
    team_agent.team_backend.shutdown_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_leader_spawn_post_hook_exception_marks_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-spawn hook exception must not return success while leaving a half-baked remote member."""
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(remote_names=["teammate-st-3"]),
    )
    _mock_a2x_reservation(monkeypatch)
    _install_spawn_tool(monkeypatch)
    monkeypatch.setattr(
        bootstrap,
        "send_bootstrap_message",
        AsyncMock(side_effect=RuntimeError("control plane down")),
    )

    team_agent = _leader_team_agent(team_name="team-hook-exc")
    bootstrap.attach_spawn_teammate_remote_bootstrap_wrapper(
        team_agent, session_id="sess-hook-exc", channel_id="web"
    )

    from openjiuwen.core.runner import Runner

    tool = Runner.resource_mgr.get_tool("team.spawn_teammate")
    result = await tool.invoke(
        {
            "member_name": "teammate-st-3",
            "display_name": "T3",
            "desc": "remote",
            "prompt": "go",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is False
    team_agent.team_backend.db.update_member_status.assert_any_await(
        "teammate-st-3",
        "team-hook-exc",
        MemberStatus.ERROR.value,
    )
