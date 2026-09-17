# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Core system tests for distributed temporary team flow."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    "test_distributed_temporary_team_st_bootstrap",
    _BOOTSTRAP_PATH,
)
assert _BOOTSTRAP_SPEC is not None and _BOOTSTRAP_SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(bootstrap)


def _distributed_leader_cfg(*, remote_names: list[str] | None = None) -> dict:
    metadata: dict[str, object] = {}
    if remote_names is not None:
        metadata["jiuwen_remote_member_names"] = remote_names
    return {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
            "metadata": metadata,
        }
    }


def _install_fake_openjiuwen_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    import openjiuwen.agent_teams.schema.events as real_events
    import openjiuwen.agent_teams.schema.status as real_status
    import openjiuwen.agent_teams.schema.team as real_team

    events_module = ModuleType("openjiuwen.agent_teams.schema.events")
    events_module.__dict__.update(real_events.__dict__)
    status_module = ModuleType("openjiuwen.agent_teams.schema.status")
    status_module.__dict__.update(real_status.__dict__)
    team_module = ModuleType("openjiuwen.agent_teams.schema.team")
    team_module.__dict__.update(real_team.__dict__)
    monkeypatch.setitem(sys.modules, "openjiuwen.agent_teams.schema.events", events_module)
    monkeypatch.setitem(sys.modules, "openjiuwen.agent_teams.schema.status", status_module)
    monkeypatch.setitem(sys.modules, "openjiuwen.agent_teams.schema.team", team_module)


@pytest.mark.asyncio
async def test_temporary_shutdown_skips_session_delete_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: _distributed_leader_cfg())
    scheduled: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        bootstrap,
        "_schedule_shutdown_cleanup",
        lambda session_id, channel_id: scheduled.append((session_id, channel_id)),
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(get_tool=lambda *_a, **_k: tool))
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="temporary"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.shutdown_member", name="shutdown_member")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(
            list_members=AsyncMock(
                return_value=[SimpleNamespace(member_name="remote-1", status=MemberStatus.SHUTDOWN.value)]
            )
        ),
    )
    bootstrap.attach_shutdown_member_remote_cleanup_wrapper(team_agent, session_id="sess-a", channel_id="web")
    await tool.invoke({"member_name": "remote-1"})
    assert scheduled == []
    assert not await bootstrap.wait_for_pending_shutdown_cleanup_for_session(
        "sess-a",
        timeout=0.01,
    )


@pytest.mark.asyncio
async def test_shutdown_member_triggers_shutdown_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    _install_fake_openjiuwen_schema(monkeypatch)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: _distributed_leader_cfg())
    send = AsyncMock()
    messager = SimpleNamespace(register_peer=MagicMock(), send=send)
    reservation = SimpleNamespace(
        dataset="team_pool",
        service_id="blank-remote-1",
        endpoint="tcp://127.0.0.1:28611",
        release=AsyncMock(),
        close=AsyncMock(),
    )
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(
            lifecycle="temporary",
            team_name="jiuwen_team_sess_shutdown_st",
            leader=SimpleNamespace(member_name="team_leader"),
        ),
        runtime_context=None,
        _messager=messager,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.shutdown_member", name="shutdown_member")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(
            list_members=AsyncMock(
                return_value=[
                    SimpleNamespace(member_name="remote-1", status=MemberStatus.SHUTDOWN_REQUESTED.value)
                ]
            ),
            get_member=AsyncMock(
                return_value=SimpleNamespace(
                    member_name="remote-1",
                    status=MemberStatus.SHUTDOWN.value,
                )
            ),
        ),
    )
    bootstrap.remember_a2x_reservation(
        team_agent,
        session_id="sess-shutdown-st",
        member_name="remote-1",
        reservation=reservation,
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(get_tool=lambda *_a, **_k: tool))
    bootstrap.attach_shutdown_member_remote_cleanup_wrapper(
        team_agent,
        session_id="sess-shutdown-st",
        channel_id="web",
    )
    await tool.invoke({"member_name": "remote-1", "force": True})

    send.assert_awaited_once()
    peer_agent_id, event = send.await_args.args
    assert peer_agent_id == "blank-remote-1"
    assert event.event_type == bootstrap.REMOTE_MEMBER_SHUTDOWN_DIRECT_EVENT_TYPE


@pytest.mark.asyncio
async def test_shutdown_direct_finalizes_teammate_and_cancels_kickoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus

    _install_fake_openjiuwen_schema(monkeypatch)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "teammate"}}},
    )
    monkeypatch.setattr(bootstrap, "_member_status_for_session",
        AsyncMock(return_value=MemberStatus.SHUTDOWN_REQUESTED.value))
    monkeypatch.setattr(bootstrap, "_update_member_status_for_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bootstrap, "_stop_dynamic_member_agent", AsyncMock(return_value=True))
    kickoff_tasks: set[asyncio.Task] = set()
    loop_kicked_members = {("sess-shutdown-st", "remote-1")}

    async def _slow_kickoff() -> None:
        await asyncio.sleep(60)

    pending = asyncio.create_task(_slow_kickoff(), name="remote-bootstrap-kickoff:sess-shutdown-st:remote-1")
    kickoff_tasks.add(pending)
    try:
        finalized = await bootstrap.apply_member_shutdown_envelope_from_control_plane(
            kickoff_tasks=kickoff_tasks,
            loop_kicked_members=loop_kicked_members,
            envelope={
                "session_id": "sess-shutdown-st",
                "member_name": "remote-1",
                "force": False,
            },
            source_id="shutdown-st-1",
        )
    finally:
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
    assert finalized is True
    assert pending.cancelled() or pending.done()
    assert ("sess-shutdown-st", "remote-1") not in loop_kicked_members


@pytest.mark.asyncio
async def test_clean_team_triggers_destroy_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    _install_fake_openjiuwen_schema(monkeypatch)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: _distributed_leader_cfg())
    send = AsyncMock()
    messager = SimpleNamespace(register_peer=MagicMock(), send=send)
    reservation = SimpleNamespace(
        dataset="team_pool",
        service_id="blank-remote-1",
        endpoint="tcp://127.0.0.1:28610",
        release=AsyncMock(),
        close=AsyncMock(),
    )
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(
            lifecycle="temporary",
            team_name="jiuwen_team_sess_temp_st",
            leader=SimpleNamespace(member_name="team_leader"),
        ),
        runtime_context=None,
        _messager=messager,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(list=lambda: [SimpleNamespace(id="team.clean_team", name="clean_team")]),
            card=SimpleNamespace(id="leader-card"),
        ),
    )
    bootstrap.remember_a2x_reservation(team_agent, session_id="sess-temp-st",
        member_name="remote-1", reservation=reservation)

    class _Result:
        success = True

    class _CleanTeamTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _CleanTeamTool()
    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(get_tool=lambda *_a, **_k: tool))
    bootstrap.attach_clean_team_distributed_teardown_wrapper(
        team_agent,
        session_id="sess-temp-st",
        channel_id="web",
    )
    await tool.invoke({})
    send.assert_awaited_once()



