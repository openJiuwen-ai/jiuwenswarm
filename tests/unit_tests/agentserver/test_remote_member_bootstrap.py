# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

_root = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "_jiuwen_remote_member_bootstrap_test",
    _root / "jiuwenswarm" / "agents" / "harness" / "team" / "remote_member_bootstrap.py",
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
remote_member_names = _mod.remote_member_names
remote_all_spawn_members = _mod.remote_all_spawn_members
parse_remote_bootstrap_ack_json = _mod.parse_remote_bootstrap_ack_json
build_bootstrap_ack_envelope = _mod.build_bootstrap_ack_envelope
attach_remote_bootstrap_ack_listener = _mod.attach_remote_bootstrap_ack_listener
attach_distributed_local_spawn_guard = _mod.attach_distributed_local_spawn_guard
attach_build_team_post_tool_registration_hook = _mod.attach_build_team_post_tool_registration_hook
attach_spawn_teammate_remote_bootstrap_wrapper = _mod.attach_spawn_teammate_remote_bootstrap_wrapper
attach_shutdown_member_remote_cleanup_wrapper = _mod.attach_shutdown_member_remote_cleanup_wrapper
attach_clean_team_distributed_teardown_wrapper = _mod.attach_clean_team_distributed_teardown_wrapper
release_a2x_reservations_for_session = _mod.release_a2x_reservations_for_session
resolve_team_lifecycle = _mod.resolve_team_lifecycle
remember_a2x_reservation = _mod.remember_a2x_reservation
build_team_destroy_envelope = _mod.build_team_destroy_envelope
apply_team_destroy_envelope_from_control_plane = _mod.apply_team_destroy_envelope_from_control_plane
apply_bootstrap_envelope_from_control_plane = _mod.apply_bootstrap_envelope_from_control_plane
apply_member_shutdown_envelope_from_control_plane = _mod.apply_member_shutdown_envelope_from_control_plane
finalize_remote_member_shutdown_on_teammate = _mod.finalize_remote_member_shutdown_on_teammate
notify_remote_member_shutdown_finalize = _mod.notify_remote_member_shutdown_finalize
REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE = _mod.REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE
precheck_and_reserve_remote_spawn = _mod.precheck_and_reserve_remote_spawn
RemoteSpawnPrecheck = _mod.RemoteSpawnPrecheck


def _distributed_leader_cfg(*, peers: list[dict] | None = None) -> dict:
    team: dict = {"runtime": {"mode": "distributed", "role": "leader"}}
    if peers:
        team["transport"] = {"type": "pyzmq", "params": {"known_peers": peers}}
    return {"team": team}


def _mock_a2x_precheck_reservation(monkeypatch) -> SimpleNamespace:
    reservation = SimpleNamespace(
        service_id="a2x-svc-test",
        endpoint="tcp://127.0.0.1:28612",
        dataset="team_pool",
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.reserve_blank_teammate_agent",
        AsyncMock(return_value=reservation),
    )
    return reservation


def test_remote_member_names_accepts_string():
    cfg = {"team": {"metadata": {"jiuwen_remote_member_names": "  t1  "}}}
    assert remote_member_names(cfg) == {"t1"}


def test_remote_member_names_accepts_list():
    cfg = {"team": {"metadata": {"jiuwen_remote_member_names": ["a", "b", ""]}}}
    assert remote_member_names(cfg) == {"a", "b"}


def test_remote_member_names_empty_when_missing():
    assert remote_member_names({"team": {}}) == set()


def test_remote_all_spawn_members_true_in_distributed_by_default():
    cfg = {"team": {"runtime": {"mode": "distributed"}}}
    assert remote_all_spawn_members(cfg) is True


def test_remote_all_spawn_members_honors_metadata_override():
    cfg = {
        "team": {
            "runtime": {"mode": "distributed"},
            "metadata": {"jiuwen_remote_all_spawn_members": False},
        },
    }
    assert remote_all_spawn_members(cfg) is False


def test_parse_remote_bootstrap_ack_json_accepts_valid():
    body = json.dumps(build_bootstrap_ack_envelope(member_name="m1", team_name="t1"))
    parsed = parse_remote_bootstrap_ack_json(body)
    assert parsed is not None
    assert parsed["member_name"] == "m1"
    assert parsed.get("team_name") == "t1"


def test_parse_remote_bootstrap_ack_json_rejects_non_json():
    assert parse_remote_bootstrap_ack_json("not json") is None


@pytest.mark.asyncio
async def test_ack_listener_updates_db_and_marks_read(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "team": {
                "runtime": {"mode": "distributed", "role": "leader"},
                "metadata": {"jiuwen_remote_member_names": ["remote1"]},
            }
        },
    )

    listeners: list = []
    db = MagicMock()
    db.get_message = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(build_bootstrap_ack_envelope(member_name="remote1", team_name="tn")),
            from_member_name="remote1",
            to_member_name="leader1",
        )
    )
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    mm = MagicMock()
    mm.mark_message_read = AsyncMock(return_value=True)

    ta = SimpleNamespace(
        role=TeamRole.LEADER,
        team_backend=SimpleNamespace(
            db=db,
            get_member=AsyncMock(return_value=SimpleNamespace(status="unstarted")),
        ),
        message_manager=mm,
        _member_name=lambda: "leader1",
        _team_name=lambda: "tn",
        add_event_listener=listeners.append,
    )

    attach_remote_bootstrap_ack_listener(ta, session_id="sid", channel_id=None)
    assert len(listeners) == 1

    ev = SimpleNamespace(
        event_type="message",
        payload={
            "message_id": "mid-1",
            "from_member_name": "remote1",
            "to_member_name": "leader1",
            "team_name": "tn",
        },
    )
    await listeners[0](ev)

    db.get_message.assert_awaited_once_with("mid-1")
    db.update_member_status.assert_awaited_once_with("remote1", "tn", "ready")
    mm.mark_message_read.assert_awaited_once_with("mid-1", "leader1")


@pytest.mark.asyncio
async def test_ack_listener_skips_ready_when_no_roster_row(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "team": {
                "runtime": {"mode": "distributed", "role": "leader"},
                "metadata": {"jiuwen_remote_member_names": ["remote1"]},
            }
        },
    )

    listeners: list = []
    db = MagicMock()
    db.get_message = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(build_bootstrap_ack_envelope(member_name="remote1", team_name="tn")),
            from_member_name="remote1",
            to_member_name="leader1",
        )
    )
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    mm = MagicMock()
    mm.mark_message_read = AsyncMock(return_value=True)

    ta = SimpleNamespace(
        role=TeamRole.LEADER,
        team_backend=SimpleNamespace(
            db=db,
            get_member=AsyncMock(return_value=None),
        ),
        message_manager=mm,
        _member_name=lambda: "leader1",
        _team_name=lambda: "tn",
        add_event_listener=listeners.append,
    )

    attach_remote_bootstrap_ack_listener(ta, session_id="sid", channel_id=None)
    ev = SimpleNamespace(
        event_type="message",
        payload={
            "message_id": "mid-no-row",
            "from_member_name": "remote1",
            "to_member_name": "leader1",
            "team_name": "tn",
        },
    )
    await listeners[0](ev)

    db.update_member_status.assert_not_awaited()
    mm.mark_message_read.assert_awaited_once_with("mid-no-row", "leader1")


@pytest.mark.asyncio
async def test_ack_listener_ignores_plain_text_message(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "team": {
                "runtime": {"mode": "distributed", "role": "leader"},
                "metadata": {"jiuwen_remote_member_names": ["remote1"]},
            }
        },
    )

    listeners: list = []
    db = MagicMock()
    db.get_message = AsyncMock(
        return_value=SimpleNamespace(
            content="hello leader",
            from_member_name="remote1",
            to_member_name="leader1",
        )
    )
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    mm = MagicMock()
    mm.mark_message_read = AsyncMock(return_value=True)

    ta = SimpleNamespace(
        role=TeamRole.LEADER,
        team_backend=SimpleNamespace(db=db),
        message_manager=mm,
        _member_name=lambda: "leader1",
        _team_name=lambda: "tn",
        add_event_listener=listeners.append,
    )

    attach_remote_bootstrap_ack_listener(ta, session_id="sid", channel_id=None)
    ev = SimpleNamespace(
        event_type="message",
        payload={
            "message_id": "mid-2",
            "from_member_name": "remote1",
            "to_member_name": "leader1",
        },
    )
    await listeners[0](ev)

    db.get_message.assert_awaited_once_with("mid-2")
    db.update_member_status.assert_not_awaited()
    mm.mark_message_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_ack_listener_accepts_any_sender_when_remote_all(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    listeners: list = []
    db = MagicMock()
    db.get_message = AsyncMock(
        return_value=SimpleNamespace(
            content=json.dumps(build_bootstrap_ack_envelope(member_name="calculator-1", team_name="tn")),
            from_member_name="calculator-1",
            to_member_name="leader1",
        )
    )
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    mm = MagicMock()
    mm.mark_message_read = AsyncMock(return_value=True)

    ta = SimpleNamespace(
        role=TeamRole.LEADER,
        team_backend=SimpleNamespace(
            db=db,
            get_member=AsyncMock(return_value=SimpleNamespace(status="ready")),
        ),
        message_manager=mm,
        _member_name=lambda: "leader1",
        _team_name=lambda: "tn",
        add_event_listener=listeners.append,
    )

    attach_remote_bootstrap_ack_listener(ta, session_id="sid", channel_id=None)
    ev = SimpleNamespace(
        event_type="message",
        payload={
            "message_id": "mid-3",
            "from_member_name": "calculator-1",
            "to_member_name": "leader1",
        },
    )
    await listeners[0](ev)

    db.update_member_status.assert_awaited_once_with("calculator-1", "tn", "ready")


@pytest.mark.asyncio
async def test_distributed_local_spawn_guard_disables_local_startup(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    send_message_tool = SimpleNamespace(_on_teammate_created=object())
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=send_message_tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)

    original_spawn = AsyncMock(return_value="local-handle")
    ta = SimpleNamespace(
        role=TeamRole.LEADER,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.send_message", name="send_message")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        spawn_teammate=original_spawn,
    )

    attach_distributed_local_spawn_guard(ta, session_id="sid", channel_id="web")

    assert getattr(send_message_tool, "_on_teammate_created") is None
    assert getattr(ta, "_jiuwen_distributed_local_spawn_guard_attached") is True
    result = await ta.spawn_teammate(SimpleNamespace(member_name="calculator-1"))
    assert result is None
    original_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_team_post_hook_defers_spawn_teammate_wrapper_until_build_team(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(),
    )

    attach_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        _mod,
        "attach_spawn_teammate_remote_bootstrap_wrapper",
        lambda team_agent, *, session_id, channel_id: attach_calls.append(
            (session_id, channel_id)
        ),
    )

    build_team = AsyncMock(return_value=None)
    team_backend = SimpleNamespace(build_team=build_team)
    team_agent = SimpleNamespace(role=TeamRole.LEADER, team_backend=team_backend)

    attach_build_team_post_tool_registration_hook(
        team_agent,
        session_id="sess-1",
        channel_id="web",
    )

    assert attach_calls == []
    await team_backend.build_team(
        display_name="team",
        desc="desc",
        leader_display_name="leader",
        leader_desc="leader desc",
    )
    build_team.assert_awaited_once()
    assert attach_calls == [("sess-1", "web")]


@pytest.mark.asyncio
async def test_spawn_teammate_wrapper_rebinds_reused_tool_to_latest_team(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(),
    )
    _mock_a2x_precheck_reservation(monkeypatch)

    class _Result:
        success = True

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _SpawnTeammateTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)

    bootstrap_calls = []

    async def _fake_send_bootstrap(team_agent, session_id, member_name, prompt, **kwargs):
        bootstrap_calls.append((team_agent, session_id, member_name, prompt))
        return True

    monkeypatch.setattr(_mod, "send_bootstrap_message", _fake_send_bootstrap)

    def _team(name):
        db = MagicMock()
        db.update_member_status = AsyncMock(return_value=True)
        db.member = SimpleNamespace(update_member_status=db.update_member_status)
        return SimpleNamespace(
            role=TeamRole.LEADER,
            deep_agent=SimpleNamespace(
                ability_manager=SimpleNamespace(
                    list=lambda: [SimpleNamespace(id="team.spawn_teammate", name="spawn_teammate")]
                ),
                card=SimpleNamespace(id="leader-card"),
            ),
            team_backend=SimpleNamespace(db=db),
            _team_name=lambda: name,
        )

    old_team = _team("old-team")
    new_team = _team("new-team")

    attach_spawn_teammate_remote_bootstrap_wrapper(old_team, session_id="old-sid", channel_id="web")
    attach_spawn_teammate_remote_bootstrap_wrapper(new_team, session_id="new-sid", channel_id="web")

    result = await tool.invoke(
        {
            "member_name": "calculator",
            "display_name": "Calculator",
            "desc": "calc",
            "prompt": "run calc",
        }
    )

    assert result.success is True
    assert bootstrap_calls == [(new_team, "new-sid", "calculator", "run calc")]
    old_team.team_backend.db.update_member_status.assert_not_awaited()
    new_team.team_backend.db.update_member_status.assert_any_await(
        "calculator", "new-team", MemberStatus.UNSTARTED.value
    )


@pytest.mark.asyncio
async def test_spawn_teammate_wrapper_forces_unstarted_after_bootstrap_delivered(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(),
    )
    _mock_a2x_precheck_reservation(monkeypatch)

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            return ToolOutput(
                success=True,
                data={
                    "member_name": inputs.get("member_name"),
                    "display_name": inputs.get("display_name"),
                    "role_type": "teammate",
                },
            )

    tool = _SpawnTeammateTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)
    monkeypatch.setattr(_mod, "send_bootstrap_message", AsyncMock(return_value=True))

    db = MagicMock()
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.spawn_teammate", name="spawn_teammate")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(db=db),
        _team_name=lambda: "active-team",
    )

    attach_spawn_teammate_remote_bootstrap_wrapper(team_agent, session_id="sid", channel_id="web")

    result = await tool.invoke(
        {
            "member_name": "calculator",
            "display_name": "Calculator",
            "desc": "Does math",
            "prompt": "run calc",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is True
    db.update_member_status.assert_any_await("calculator", "active-team", MemberStatus.UNSTARTED.value)


@pytest.mark.asyncio
async def test_spawn_teammate_wrapper_blocks_before_db_when_precheck_fails(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(),
    )

    orig_calls: list[str] = []

    class _Result:
        success = True

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            orig_calls.append(str((inputs or {}).get("member_name")))
            return _Result()

    tool = _SpawnTeammateTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)

    async def _fail_precheck(member_name: str, config_base=None, **kwargs):
        return RemoteSpawnPrecheck(error="precheck blocked")

    monkeypatch.setattr(_mod, "precheck_and_reserve_remote_spawn", _fail_precheck)

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.spawn_teammate", name="spawn_teammate")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(db=MagicMock()),
        _team_name=lambda: "dist-team",
    )

    attach_spawn_teammate_remote_bootstrap_wrapper(team_agent, session_id="sid", channel_id="web")

    result = await tool.invoke(
        {
            "member_name": "teammate-2",
            "display_name": "Teammate 2",
            "desc": "second",
            "prompt": "work",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is False
    assert "precheck blocked" in (result.error or "")
    assert orig_calls == []


@pytest.mark.asyncio
async def test_spawn_teammate_wrapper_fail_fast_when_bootstrap_not_delivered(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.tools.base_tool import ToolOutput

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: _distributed_leader_cfg(),
    )
    _mock_a2x_precheck_reservation(monkeypatch)

    class _Result:
        success = True

    class _SpawnTeammateTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _SpawnTeammateTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)
    monkeypatch.setattr(_mod, "send_bootstrap_message", AsyncMock(return_value=False))

    db = MagicMock()
    db.update_member_status = AsyncMock(return_value=True)
    db.member = SimpleNamespace(update_member_status=db.update_member_status)
    team_backend = SimpleNamespace(
        db=db,
        shutdown_member=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.spawn_teammate", name="spawn_teammate")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=team_backend,
        _team_name=lambda: "dist-team",
    )

    attach_spawn_teammate_remote_bootstrap_wrapper(team_agent, session_id="sid", channel_id="web")

    result = await tool.invoke(
        {
            "member_name": "teammate-2",
            "display_name": "Teammate 2",
            "desc": "second",
            "prompt": "work",
        }
    )

    assert isinstance(result, ToolOutput)
    assert result.success is False
    assert "not delivered" in (result.error or "").lower()
    assert "marked error" in (result.error or "").lower()
    db.update_member_status.assert_any_await(
        "teammate-2",
        "dist-team",
        MemberStatus.ERROR.value,
    )
    team_backend.shutdown_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_precheck_reserves_blank_from_a2x(monkeypatch):
    cfg = _distributed_leader_cfg()
    reservation = SimpleNamespace(
        service_id="a2x-svc-1",
        endpoint="tcp://10.0.0.5:18600",
        dataset="team_pool",
    )
    reserve_mock = AsyncMock(return_value=reservation)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.reserve_blank_teammate_agent",
        reserve_mock,
    )
    gate = await precheck_and_reserve_remote_spawn("any-logical-name", cfg)
    assert gate.error is None
    assert gate.registry_reservation is reservation
    reserve_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_precheck_fails_when_member_already_exists():
    team_backend = SimpleNamespace(
        get_member=AsyncMock(return_value=SimpleNamespace(status="ready")),
        team_name="dist-team",
    )
    team_agent = SimpleNamespace(team_backend=team_backend, _team_name=lambda: "dist-team")
    gate = await precheck_and_reserve_remote_spawn(
        "teammate-1",
        _distributed_leader_cfg(),
        team_agent=team_agent,
    )
    assert gate.registry_reservation is None
    assert gate.error is not None
    assert "already exists" in gate.error.lower()


@pytest.mark.asyncio
async def test_precheck_fails_when_no_blank_available(monkeypatch):
    cfg = _distributed_leader_cfg()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.reserve_blank_teammate_agent",
        AsyncMock(return_value=None),
    )
    gate = await precheck_and_reserve_remote_spawn("teammate-2", cfg)
    assert gate.registry_reservation is None
    assert gate.error is not None
    assert "blank teammate" in gate.error.lower()


@pytest.mark.asyncio
async def test_bootstrap_allows_later_kickoff_for_same_member_after_task_done(monkeypatch):
    kickoff_calls = []
    card_replace_calls = []
    member_status = AsyncMock(return_value=None)

    async def fake_ensure_dynamic_member_execution_loop(**kwargs):
        kickoff_calls.append(kwargs)
        return True, True

    async def fake_replace_card_after_direct_bootstrap(**kwargs):
        card_replace_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        _mod,
        "_ensure_dynamic_member_execution_loop",
        fake_ensure_dynamic_member_execution_loop,
    )
    monkeypatch.setattr(
        _mod,
        "_replace_teammate_card_after_direct_bootstrap",
        fake_replace_card_after_direct_bootstrap,
    )
    monkeypatch.setattr(_mod, "_member_status_for_session", member_status)

    processed = set()
    loop_kicked_members = set()
    kickoff_tasks = set()
    envelope = {
        "bootstrap_id": "boot-1",
        "team_name": "jiuwen_team_sess_1",
        "session_id": "sess_1",
        "member_name": "calculator",
        "leader_agent_id": "leader",
        "leader_direct_addr": "tcp://127.0.0.1:28555",
    }
    await apply_bootstrap_envelope_from_control_plane(
        processed_ids=processed,
        loop_kicked_members=loop_kicked_members,
        kickoff_tasks=kickoff_tasks,
        adopted_member="teammate_1",
        envelope=envelope,
        source_id="src-1",
    )
    await asyncio.gather(*list(kickoff_tasks))
    await asyncio.sleep(0)

    assert len(kickoff_calls) == 1
    assert card_replace_calls == [{"channel_id": "default", "member_name": "calculator"}]
    assert ("sess_1", "calculator") not in loop_kicked_members

    envelope["bootstrap_id"] = "boot-2"
    await apply_bootstrap_envelope_from_control_plane(
        processed_ids=processed,
        loop_kicked_members=loop_kicked_members,
        kickoff_tasks=kickoff_tasks,
        adopted_member="calculator",
        envelope=envelope,
        source_id="src-2",
    )
    await asyncio.gather(*list(kickoff_tasks))
    await asyncio.sleep(0)

    assert len(kickoff_calls) == 2
    assert card_replace_calls == [
        {"channel_id": "default", "member_name": "calculator"},
        {"channel_id": "default", "member_name": "calculator"},
    ]


@pytest.mark.asyncio
async def test_replace_teammate_card_after_direct_bootstrap_uses_local_a2x_state(monkeypatch):
    replace_calls = []
    client = SimpleNamespace()
    deep_agent = SimpleNamespace(
        _jiuwen_a2x_client=client,
        _jiuwen_a2x_blank_dataset="team_pool_local",
        _jiuwen_a2x_blank_service_id="sid-local",
    )
    async def _ensure_instance():
        return deep_agent

    agent = SimpleNamespace(get_instance=lambda: deep_agent, ensure_instance=_ensure_instance)
    agent_manager = SimpleNamespace(
        get_agent_nowait=lambda channel_id: agent,
        get_agent=AsyncMock(return_value=agent),
    )
    server = SimpleNamespace(get_agent_manager=lambda: agent_manager)

    async def fake_replace_teammate_agent_card_after_bootstrap(*args, **kwargs):
        replace_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        lambda: server,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime.replace_teammate_agent_card_after_bootstrap",
        fake_replace_teammate_agent_card_after_bootstrap,
    )
    replace_teammate_card = getattr(
        _mod,
        "_replace_teammate_card"
        "_after_direct_bootstrap",
    )

    replaced = await replace_teammate_card(channel_id="default", member_name="calculator")

    assert replaced is True
    assert len(replace_calls) == 1
    args, kwargs = replace_calls[0]
    assert args == (client,)
    assert kwargs == {
        "dataset": "team_pool_local",
        "service_id": "sid-local",
        "member_name": "calculator",
        "source": "teammate-direct-bootstrap",
    }


def test_retarget_teammate_direct_addr_allocates_non_default_port(monkeypatch):
    class _Config:
        direct_addr = "tcp://127.0.0.1:16000"

        @staticmethod
        def model_copy(update):
            copied = _Config()
            copied.direct_addr = update["direct_addr"]
            return copied

    class _Context:
        messager_config = _Config()

        @staticmethod
        def model_copy(update):
            copied = _Context()
            copied.messager_config = update["messager_config"]
            return copied

    monkeypatch.setattr(_mod, "_allocate_loopback_direct_addr", lambda: "tcp://127.0.0.1:32123")
    retarget_teammate_direct_addr = getattr(
        _mod,
        "_retarget_teammate"
        "_direct_addr",
    )

    retargeted = retarget_teammate_direct_addr(
        _Context(),
        session_id="sid",
        member_name="calculator",
    )

    assert retargeted.messager_config.direct_addr == "tcp://127.0.0.1:32123"


def test_apply_leader_route_uses_agent_core_infra_messager():
    register_peer = MagicMock()
    team_agent = SimpleNamespace(
        infra=SimpleNamespace(
            messager=SimpleNamespace(register_peer=register_peer),
        ),
    )
    apply_route = getattr(
        _mod,
        "_apply_leader"
        "_route_from_envelope",
    )

    applied = apply_route(
        team_agent,
        {
            "leader_agent_id": "team_leader",
            "leader_direct_addr": "tcp://0.0.0.0:28565",
        },
    )

    assert applied is True
    peer = register_peer.call_args.args[0]
    assert peer.agent_id == "team_leader"
    assert peer.addrs == ["tcp://127.0.0.1:28565"]


@pytest.mark.asyncio
async def test_teammate_direct_bootstrap_ack_sent_via_message_manager():
    send_message = AsyncMock(return_value="ack-msg-1")
    team_agent = SimpleNamespace(
        message_manager=SimpleNamespace(send_message=send_message),
        team_backend=SimpleNamespace(team_name="team-demo"),
    )
    send_ack = getattr(
        _mod,
        "_send_bootstrap"
        "_ack_from_teammate",
    )

    sent = await send_ack(
        team_agent,
        session_id="sess-ack",
        member_name="calc-expert",
        team_name="team-demo",
        leader_member_name="team_leader",
        leader_agent_id="team_leader_id",
        leader_direct_addr="tcp://127.0.0.1:28565",
        handshake_applied=True,
    )

    assert sent is True
    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert kwargs["to_member_name"] == "team_leader"
    payload = json.loads(kwargs["content"])
    assert payload["type"] == "jiuwen.remote_bootstrap_ack"
    assert payload["member_name"] == "calc-expert"
    assert payload["handshake_applied"] is True


@pytest.mark.asyncio
async def test_discard_auxiliary_team_agent_removes_cache_and_stops_runtime():
    stop_coordination = AsyncMock()
    stop_messager = AsyncMock()
    helper = SimpleNamespace(
        member_name="team_leader",
        _stop_coordination=stop_coordination,
        infra=SimpleNamespace(
            messager=SimpleNamespace(
                _config=SimpleNamespace(direct_addr="tcp://127.0.0.1:28555"),
                stop=stop_messager,
            ),
        ),
    )
    team_agents_attr = "_team" "_agents"
    team_manager = SimpleNamespace()
    setattr(team_manager, team_agents_attr, {"sid": helper})
    discard_auxiliary_team_agent = getattr(
        _mod,
        "_discard_auxiliary"
        "_team_agent",
    )

    await discard_auxiliary_team_agent(team_manager, "sid", helper)

    assert getattr(team_manager, team_agents_attr) == {}
    stop_coordination.assert_awaited_once()
    stop_messager.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_a2x_reservations_notifies_remote_teammate_and_does_not_release_from_leader(
    monkeypatch,
):
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )
    send = AsyncMock()
    register_peer = MagicMock()
    messager = SimpleNamespace(register_peer=register_peer, send=send)
    reservation = SimpleNamespace(
        dataset="team_pool",
        service_id="blank-agent-1",
        endpoint="tcp://127.0.0.1:28610",
        release=AsyncMock(),
        close=AsyncMock(),
    )
    ta = SimpleNamespace(
        spec=SimpleNamespace(
            team_name="jiuwen_team_sess_destroy_1",
            leader=SimpleNamespace(member_name="team_leader"),
        ),
        runtime_context=None,
        _messager=messager,
    )

    remember_a2x_reservation(
        ta,
        session_id="sess_destroy_1",
        member_name="math-calc-1",
        reservation=reservation,
    )

    await release_a2x_reservations_for_session("sess_destroy_1", team_agent=ta)

    send.assert_awaited_once()
    peer_agent_id, event = send.await_args.args
    assert peer_agent_id == "blank-agent-1"
    assert event.event_type == REMOTE_TEAM_DESTROY_DIRECT_EVENT_TYPE
    assert event.payload["envelope"]["type"] == "jiuwen.remote_team_destroy"
    assert event.payload["envelope"]["member_name"] == "math-calc-1"
    assert event.payload["envelope"]["session_id"] == "sess_destroy_1"
    assert event.payload["envelope"]["registry"] == {
        "dataset": "team_pool",
        "service_id": "blank-agent-1",
        "endpoint": "tcp://127.0.0.1:28610",
    }
    reservation.release.assert_not_awaited()
    reservation.close.assert_awaited_once()
    send.reset_mock()
    await release_a2x_reservations_for_session("sess_destroy_1", team_agent=ta)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_member_wrapper_schedules_cleanup_when_all_teammates_closed(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)

    scheduled = []
    monkeypatch.setattr(
        _mod,
        "".join(["_schedule", "_shutdown_cleanup"]),
        lambda session_id, channel_id: scheduled.append((session_id, channel_id)),
    )

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="persistent"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.shutdown_member", name="shutdown_member")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(
            list_members=AsyncMock(
                return_value=[
                    SimpleNamespace(member_name="teammate-1", status=MemberStatus.SHUTDOWN_REQUESTED.value),
                    SimpleNamespace(member_name="teammate-2", status=MemberStatus.SHUTDOWN.value),
                ]
            )
        ),
    )

    attach_shutdown_member_remote_cleanup_wrapper(team_agent, session_id="sid-1", channel_id="web")
    await tool.invoke({"member_name": "teammate-1"})

    assert scheduled == [("sid-1", "web")]


@pytest.mark.asyncio
async def test_shutdown_member_wrapper_skips_delete_for_temporary_lifecycle(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )

    scheduled = []
    monkeypatch.setattr(
        _mod,
        "".join(["_schedule", "_shutdown_cleanup"]),
        lambda session_id, channel_id: scheduled.append((session_id, channel_id)),
    )

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
                return_value=[
                    SimpleNamespace(member_name="teammate-1", status=MemberStatus.SHUTDOWN.value),
                ]
            )
        ),
    )

    attach_shutdown_member_remote_cleanup_wrapper(team_agent, session_id="sid-temp", channel_id="web")
    await tool.invoke({"member_name": "teammate-1"})

    assert scheduled == []


@pytest.mark.asyncio
async def test_shutdown_member_wrapper_waits_until_every_teammate_is_closed(monkeypatch):
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    resource_mgr = MagicMock()
    resource_mgr.get_tool = MagicMock(return_value=tool)
    monkeypatch.setattr(Runner, "resource_mgr", resource_mgr)

    scheduled = []
    monkeypatch.setattr(
        _mod,
        "".join(["_schedule", "_shutdown_cleanup"]),
        lambda session_id, channel_id: scheduled.append((session_id, channel_id)),
    )

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="persistent"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.shutdown_member", name="shutdown_member")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(
            list_members=AsyncMock(
                return_value=[
                    SimpleNamespace(member_name="teammate-1", status=MemberStatus.SHUTDOWN_REQUESTED.value),
                    SimpleNamespace(member_name="teammate-2", status=MemberStatus.READY.value),
                ]
            )
        ),
    )

    attach_shutdown_member_remote_cleanup_wrapper(team_agent, session_id="sid-1", channel_id="web")
    await tool.invoke({"member_name": "teammate-1"})

    assert scheduled == []


@pytest.mark.asyncio
async def test_clean_team_wrapper_releases_a2x_after_success(monkeypatch):
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = True

    class _CleanTeamTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _CleanTeamTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )

    released: list[tuple[str, Any]] = []

    async def fake_release(session_id: str, *, team_agent=None):
        released.append((session_id, team_agent))

    monkeypatch.setattr(_mod, "release_a2x_reservations_for_session", fake_release)

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="temporary"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.clean_team", name="clean_team")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
    )

    attach_clean_team_distributed_teardown_wrapper(
        team_agent,
        session_id="sid-clean",
        channel_id="web",
    )
    await tool.invoke({})

    assert released == [("sid-clean", team_agent)]


def test_resolve_team_lifecycle_prefers_spec_then_property() -> None:
    agent = SimpleNamespace(
        spec=SimpleNamespace(lifecycle="temporary"),
        lifecycle="persistent",
    )
    assert resolve_team_lifecycle(agent) == "temporary"

    agent2 = SimpleNamespace(spec=None, lifecycle="Temporary")
    assert resolve_team_lifecycle(agent2) == "temporary"

    agent3 = SimpleNamespace(spec=SimpleNamespace(lifecycle=""), lifecycle="")
    assert resolve_team_lifecycle(agent3) == "persistent"


@pytest.mark.asyncio
async def test_clean_team_wrapper_skips_release_on_failure(monkeypatch) -> None:
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = False

    class _CleanTeamTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _CleanTeamTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )

    released: list[str] = []

    async def fake_release(session_id: str, *, team_agent=None) -> None:
        released.append(session_id)

    monkeypatch.setattr(_mod, "release_a2x_reservations_for_session", fake_release)

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="temporary"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.clean_team", name="clean_team")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
    )
    attach_clean_team_distributed_teardown_wrapper(
        team_agent,
        session_id="sid-fail",
        channel_id="web",
    )
    await tool.invoke({})
    assert released == []


@pytest.mark.asyncio
async def test_clean_team_wrapper_not_wrapped_for_persistent_lifecycle(monkeypatch) -> None:
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _CleanTeamTool:
        async def invoke(self, inputs, **kwargs):
            return SimpleNamespace(success=True)

    tool = _CleanTeamTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )

    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="persistent"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.clean_team", name="clean_team")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
    )
    attach_clean_team_distributed_teardown_wrapper(
        team_agent,
        session_id="sid-persist",
        channel_id="web",
    )
    assert not getattr(tool, "_jiuwen_clean_team_distributed_teardown_wrapped", False)


@pytest.mark.asyncio
async def test_apply_member_shutdown_cancels_pending_kickoff(monkeypatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus

    update_status = AsyncMock(return_value=True)
    monkeypatch.setattr(_mod, "_member_status_for_session",
        AsyncMock(return_value=MemberStatus.SHUTDOWN_REQUESTED.value),
    )
    monkeypatch.setattr(_mod, "_update_member_status_for_session", update_status)
    monkeypatch.setattr(_mod, "_stop_dynamic_member_agent", AsyncMock(return_value=False))

    kickoff_tasks: set[asyncio.Task[Any]] = set()
    loop_kicked_members = {("sid-shutdown", "calc-expert")}

    async def _slow_kickoff() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_slow_kickoff(), name="remote-bootstrap-kickoff:sid-shutdown:calc-expert")
    kickoff_tasks.add(task)

    finalized = await apply_member_shutdown_envelope_from_control_plane(
        kickoff_tasks=kickoff_tasks,
        loop_kicked_members=loop_kicked_members,
        envelope={
            "session_id": "sid-shutdown",
            "member_name": "calc-expert",
            "force": False,
        },
        source_id="shutdown-1",
    )

    assert finalized is True
    assert task.cancelled() or task.done()
    assert ("sid-shutdown", "calc-expert") not in loop_kicked_members
    update_status.assert_awaited_once_with(
        "sid-shutdown",
        "calc-expert",
        MemberStatus.SHUTDOWN.value,
        channel_id="default",
    )


@pytest.mark.asyncio
async def test_apply_bootstrap_skips_kickoff_when_shutdown_requested(monkeypatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus

    monkeypatch.setattr(
        _mod,
        "_member_status_for_session",
        AsyncMock(return_value=MemberStatus.SHUTDOWN_REQUESTED.value),
    )
    monkeypatch.setattr(
        _mod,
        "_replace_teammate_card_after_direct_bootstrap",
        AsyncMock(return_value=True),
    )

    kickoff_tasks: set[asyncio.Task[Any]] = set()
    adopted = await apply_bootstrap_envelope_from_control_plane(
        processed_ids=set(),
        loop_kicked_members=set(),
        kickoff_tasks=kickoff_tasks,
        adopted_member="blank",
        envelope={
            "bootstrap_id": "bs-1",
            "team_name": "team_sid",
            "session_id": "sid-1",
            "member_name": "calc-expert",
            "leader_agent_id": "leader-1",
            "leader_direct_addr": "tcp://127.0.0.1:29111",
        },
        source_id="bs-1",
    )

    assert adopted == "blank"
    assert kickoff_tasks == set()


@pytest.mark.asyncio
async def test_shutdown_member_wrapper_notifies_remote_finalize_for_temporary(monkeypatch) -> None:
    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )

    notified: list[tuple[str, str, bool]] = []
    wait_polls: list[str] = []

    async def fake_notify(team_agent, session_id, member_name, *, force=False):
        notified.append((session_id, member_name, force))
        return True

    async def fake_existing(team_agent, member_name):
        wait_polls.append(member_name)
        return SimpleNamespace(status=MemberStatus.SHUTDOWN.value)

    monkeypatch.setattr(_mod, "notify_remote_member_shutdown_finalize", fake_notify)
    monkeypatch.setattr(_mod, "_existing_team_member", fake_existing)
    monkeypatch.setattr(
        _mod,
        "".join(["_schedule", "_shutdown_cleanup"]),
        lambda session_id, channel_id: None,
    )

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
                return_value=[
                    SimpleNamespace(member_name="calc-expert", status=MemberStatus.SHUTDOWN.value),
                ]
            )
        ),
    )

    attach_shutdown_member_remote_cleanup_wrapper(team_agent, session_id="sid-temp", channel_id="web")
    await tool.invoke({"member_name": "calc-expert", "force": True})

    assert notified == [("sid-temp", "calc-expert", True)]
    assert wait_polls == ["calc-expert"]
