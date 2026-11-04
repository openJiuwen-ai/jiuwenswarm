# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Git identity policy, persistence, and live member environment tests."""

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.rails.team_context import TeamHandleKey
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.core.sys_operation.execution_environment import (
    execution_environment_scope,
)

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.server.runtime.session import execution_settings as policy


@pytest.mark.parametrize("value", [None, {"independent_git_identity": "false"}, {"unknown": True}, {"environment": {"X": 1}}])
def test_reject_invalid_settings(value: object) -> None:
    with pytest.raises(ValueError):
        policy.validate_settings(value)


@pytest.mark.parametrize("name", [None, "", " x", "bad\nname", "bad<name"])
def test_enabled_policy_requires_member_identity(name: str | None) -> None:
    with pytest.raises(ValueError):
        policy.member_environment({"independent_git_identity": True}, name)


def test_disabled_policy_and_unicode_email() -> None:
    assert policy.member_environment({}, None).merge() == {}
    first = policy.member_environment({"independent_git_identity": True}, "审查员").merge()
    second = policy.member_environment({"independent_git_identity": True}, "开发员").merge()
    assert first["GIT_AUTHOR_NAME"] == "审查员"
    assert first["GIT_AUTHOR_EMAIL"].isascii()
    assert first["GIT_AUTHOR_EMAIL"].endswith("@jiuwenswarm.com")
    assert first["GIT_AUTHOR_EMAIL"] != second["GIT_AUTHOR_EMAIL"]


@pytest.mark.asyncio
async def test_read_does_not_erase_first_send_draft_and_cas_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "get_session_metadata", lambda *args, **kwargs: {})
    writes = []
    monkeypatch.setattr(policy, "persist_settings_mirror", lambda sid, settings: writes.append(settings))
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    try:
        before = await policy._request_with_database("session", db, {})
        assert before["version"] == -1
        assert await db.session_settings.get("session") is None
        await policy.initialize_settings("session", db, {"independent_git_identity": True})
        current = await policy._request_with_database("session", db, {})
        assert current["settings"]["independent_git_identity"] is True
        saved = await policy._request_with_database("session", db, {
            "expected_version": current["version"], "settings": {"independent_git_identity": False},
        })
        assert saved["version"] == 1
        with pytest.raises(ValueError, match="refresh"):
            await policy._request_with_database("session", db, {
                "expected_version": 0, "settings": {"independent_git_identity": True},
            })
        assert writes[-1]["independent_git_identity"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_members_read_live_policy_and_seed_preserves_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "get_session_metadata", lambda *args, **kwargs: {})
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    try:
        await policy.initialize_settings("session", db, {"independent_git_identity": True})
        base = SwarmBuildContext(session_id="session", execution_settings_managed=True)
        base.extras[TeamHandleKey.TEAM_BACKEND] = SimpleNamespace(db=db)
        first = base.derive(member_name="alice")
        second = base.derive(member_name="bob")
        snapshots = await asyncio.gather(first.resolve_execution_environment(), second.resolve_execution_environment())
        assert snapshots[0].merge()["GIT_AUTHOR_NAME"] == "alice"
        assert snapshots[1].merge()["GIT_AUTHOR_NAME"] == "bob"
        await db.session_settings.replace("session", {"independent_git_identity": False}, expected_version=0)
        assert (await first.resolve_execution_environment()).merge() == {}
        assert snapshots[0].merge()["GIT_AUTHOR_NAME"] == "alice"
        recovered = SwarmBuildContext.from_seed(base.to_seed(), config={}, trajectory_span_processor=None)
        assert recovered.execution_settings_managed
        assert recovered.session_id == "session"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_real_git_commits_have_member_authors_and_disabled_fallback(tmp_path: Path) -> None:
    from jiuwenswarm.agents.harness.common.tools.command_tools import _run_command_sync

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    base_environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "host",
        "GIT_AUTHOR_EMAIL": "host@example.test",
        "GIT_COMMITTER_NAME": "host",
        "GIT_COMMITTER_EMAIL": "host@example.test",
    }
    for member, enabled in [("alice", True), ("bob", True), ("bob", False)]:
        snapshot = policy.member_environment({
            "environment": base_environment, "independent_git_identity": enabled,
        }, member)
        with execution_environment_scope(snapshot):
            result, _ = await asyncio.to_thread(
                _run_command_sync,
                "git -c commit.gpgsign=false commit --allow-empty -qm identity",
                30, tmp_path, "auto",
            )
        assert result.returncode == 0, result.stderr
        record = subprocess.check_output([
            "git", "-C", str(tmp_path), "log", "-1", "--format=%an|%ae|%cn|%ce",
        ], text=True).strip()
        name = member if enabled else "host"
        email = f"{member}@jiuwenswarm.com" if enabled else "host@example.test"
        assert record == f"{name}|{email}|{name}|{email}"


@pytest.mark.asyncio
async def test_preparation_seeds_policy_and_cold_recovery_preserves_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "get_session_metadata", lambda *args, **kwargs: {"work_mode": "code"})
    monkeypatch.setattr(policy, "persist_settings_mirror", lambda sid, settings: None)
    config = DatabaseConfig(connection_string=str(tmp_path / "team.db"))
    spec = SimpleNamespace(
        resolve_db_config=lambda: config,
        build_context=SwarmBuildContext(session_id="session"),
    )
    await policy.prepare_session_execution("session", spec, {"independent_git_identity": True})
    assert spec.build_context_seed["execution_settings_managed"] is True
    db = TeamDatabase(config)
    try:
        await db.initialize()
        await db.session_settings.replace("session", {"independent_git_identity": False}, expected_version=0)
        await policy.prepare_session_execution("session", spec, {"independent_git_identity": True})
        assert (await db.session_settings.get("session"))["settings"]["independent_git_identity"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_execution_settings_api_uses_live_member_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team import team_manager

    monkeypatch.setattr(policy, "get_session_metadata", lambda *args, **kwargs: {"work_mode": "code", "mode": "team"})
    monkeypatch.setattr(policy, "persist_settings_mirror", lambda sid, settings: None)
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "team.db")))
    manager = SimpleNamespace(get_team_agent=lambda sid: SimpleNamespace(team_backend=SimpleNamespace(db=db)))
    monkeypatch.setattr(team_manager, "get_team_manager", lambda channel: manager)
    try:
        initial = await policy.session_settings_request("session", {}, "web")
        assert initial["version"] == -1
        saved = await policy.session_settings_request("session", {
            "expected_version": -1, "settings": {"independent_git_identity": True},
        }, "web")
        assert saved["settings"]["independent_git_identity"] is True
        monkeypatch.setattr(policy, "get_session_metadata", lambda *args, **kwargs: {"work_mode": "work", "mode": "team"})
        with pytest.raises(ValueError, match="code team"):
            await policy.session_settings_request("session", {}, "web")
    finally:
        await db.close()
