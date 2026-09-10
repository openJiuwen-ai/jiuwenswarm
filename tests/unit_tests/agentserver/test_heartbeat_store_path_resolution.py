"""Heartbeat persistence must survive the legacy workspace migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as common_utils
from jiuwenswarm.common.utils import (
    _migrate_legacy_heartbeat_jobs,
    _migrate_legacy_workspace,
    get_heartbeat_jobs_path,
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: tmp_path)
    return tmp_path


def _write_store(path: Path, job_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "jobs": [{"id": job_id}]}),
        encoding="utf-8",
    )
    return path


def test_fresh_workspace_uses_agent_root(workspace: Path) -> None:
    assert get_heartbeat_jobs_path() == workspace / "agent" / "heartbeat_jobs.json"


def test_legacy_store_remains_readable_before_migration(workspace: Path) -> None:
    legacy = _write_store(
        workspace / "agent" / "home" / "heartbeat_jobs.json",
        "legacy",
    )
    assert get_heartbeat_jobs_path() == legacy


def test_current_store_wins_when_both_exist(workspace: Path) -> None:
    _write_store(workspace / "agent" / "home" / "heartbeat_jobs.json", "legacy")
    current = _write_store(workspace / "agent" / "heartbeat_jobs.json", "current")
    assert get_heartbeat_jobs_path() == current


def test_real_workspace_migration_preserves_heartbeat_jobs(
    workspace: Path,
) -> None:
    legacy = workspace / "agent" / "home" / "heartbeat_jobs.json"
    legacy.parent.mkdir(parents=True)
    payload = {
        "version": 1,
        "jobs": [
            {
                "id": "hb_persist",
                "name": "persist across restart",
                "channel_id": "web",
                "session_id": "session-1",
                "prompt": "continue",
                "schedule": {"type": "interval", "interval_seconds": 120},
                "status": "scheduled",
                "enabled": True,
                "next_run_at": 123456.0,
                "run_count": 2,
            }
        ],
    }
    legacy.write_text(json.dumps(payload), encoding="utf-8")

    _migrate_legacy_workspace(workspace)

    assert not (workspace / "agent" / "home").exists()
    assert get_heartbeat_jobs_path() == workspace / "agent" / "heartbeat_jobs.json"
    migrated = json.loads(get_heartbeat_jobs_path().read_text(encoding="utf-8"))
    assert migrated == payload


def test_existing_current_store_removes_legacy_without_backup(
    workspace: Path,
) -> None:
    legacy = _write_store(
        workspace / "agent" / "home" / "heartbeat_jobs.json",
        "legacy",
    )
    current = _write_store(
        workspace / "agent" / "heartbeat_jobs.json",
        "current",
    )

    assert _migrate_legacy_heartbeat_jobs(workspace) is True

    assert json.loads(current.read_text(encoding="utf-8"))["jobs"] == [
        {"id": "current"}
    ]
    assert not legacy.exists()
    assert not list(
        (workspace / "agent").glob("heartbeat_jobs.json.legacy-backup.*")
    )


def test_invalid_legacy_store_blocks_old_home_deletion(workspace: Path) -> None:
    legacy = workspace / "agent" / "home" / "heartbeat_jobs.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("not-json", encoding="utf-8")

    _migrate_legacy_workspace(workspace)

    assert legacy.exists()
    assert not (workspace / "agent" / "heartbeat_jobs.json").exists()


def test_existing_current_store_removes_invalid_legacy(
    workspace: Path,
) -> None:
    legacy = workspace / "agent" / "home" / "heartbeat_jobs.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("not-json", encoding="utf-8")
    current = _write_store(
        workspace / "agent" / "heartbeat_jobs.json",
        "current",
    )

    _migrate_legacy_workspace(workspace)

    assert not (workspace / "agent" / "home").exists()
    assert json.loads(current.read_text(encoding="utf-8"))["jobs"] == [
        {"id": "current"}
    ]
    assert not list(
        (workspace / "agent").glob("heartbeat_jobs.json.legacy-backup.*")
    )


def test_runtime_preparation_routes_legacy_home_through_workspace_migration(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _write_store(
        workspace / "agent" / "home" / "heartbeat_jobs.json",
        "legacy",
    )
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "config.yaml").write_text("{}", encoding="utf-8")
    (workspace / "agent" / "workspace" / "mcp" / "mcp_builtins").mkdir(
        parents=True
    )
    monkeypatch.setattr(common_utils, "cleanup_team_files", lambda _workspace: None)
    monkeypatch.setattr(
        common_utils,
        "mcp_builtins_seed_update_needed",
        lambda _workspace: False,
    )
    prepare_calls: list[dict[str, object]] = []

    def _prepare_workspace(**kwargs: object) -> None:
        prepare_calls.append(kwargs)
        _migrate_legacy_workspace(workspace)

    monkeypatch.setattr(common_utils, "prepare_workspace", _prepare_workspace)
    monkeypatch.setattr(
        common_utils,
        "ensure_config_migrated_from_template",
        lambda _workspace: None,
    )
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert prepare_calls == [{"overwrite": False, "workspace_dir": workspace}]
    assert not legacy.exists()
    assert (workspace / "agent" / "heartbeat_jobs.json").exists()
