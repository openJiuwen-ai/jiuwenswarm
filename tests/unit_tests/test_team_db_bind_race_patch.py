"""Unit tests for the team DB bind drop-race patch (GitCode issue #4956)."""

import pytest
from sqlalchemy.exc import NoSuchTableError

from jiuwenswarm.agents.harness.team import db_bind_race_patch


def _make_scan(failures: int):
    """Build a stub backfill scan raising NoSuchTableError for its first ``failures`` calls."""
    state = {"calls": 0}

    def scan(sync_conn):
        del sync_conn
        state["calls"] += 1
        if state["calls"] <= failures:
            raise NoSuchTableError("team_message_b205e0dea956cda7")
        return "migrated"

    return scan, state


def test_wrapper_retries_once_after_concurrent_drop():
    scan, state = _make_scan(failures=1)
    wrapper = db_bind_race_patch._tolerate_concurrent_drop(scan, "_ensure_message_protocol_column")

    assert wrapper("conn") == "migrated"
    assert state["calls"] == 2


def test_wrapper_gives_up_quietly_after_repeated_drops():
    scan, state = _make_scan(failures=99)
    wrapper = db_bind_race_patch._tolerate_concurrent_drop(scan, "_ensure_message_meta_column")

    assert wrapper("conn") is None
    assert state["calls"] == db_bind_race_patch._MAX_SCAN_ATTEMPTS


def test_wrapper_calls_original_once_when_no_race():
    scan, state = _make_scan(failures=0)
    wrapper = db_bind_race_patch._tolerate_concurrent_drop(scan, "_ensure_message_protocol_column")

    assert wrapper("conn") == "migrated"
    assert state["calls"] == 1


def test_wrapper_propagates_unrelated_errors():
    def broken(sync_conn):
        del sync_conn
        raise ValueError("boom")

    wrapper = db_bind_race_patch._tolerate_concurrent_drop(broken, "_ensure_message_meta_column")

    with pytest.raises(ValueError, match="boom"):
        wrapper("conn")


def test_apply_wraps_both_scans_and_is_idempotent(monkeypatch):
    from openjiuwen.agent_teams.tools.database import engine as team_db_engine

    scan_proto, _ = _make_scan(failures=0)
    scan_meta, _ = _make_scan(failures=0)
    monkeypatch.setattr(team_db_engine, "_ensure_message_protocol_column", scan_proto)
    monkeypatch.setattr(team_db_engine, "_ensure_message_meta_column", scan_meta)

    db_bind_race_patch.apply_team_db_bind_race_patch()

    wrapped_proto = team_db_engine._ensure_message_protocol_column
    wrapped_meta = team_db_engine._ensure_message_meta_column
    assert getattr(wrapped_proto, db_bind_race_patch._PATCH_MARKER, False) is True
    assert getattr(wrapped_meta, db_bind_race_patch._PATCH_MARKER, False) is True
    assert wrapped_proto("conn") == "migrated"

    db_bind_race_patch.apply_team_db_bind_race_patch()
    assert team_db_engine._ensure_message_protocol_column is wrapped_proto
    assert team_db_engine._ensure_message_meta_column is wrapped_meta


def test_apply_skips_missing_targets_without_raising(monkeypatch):
    from openjiuwen.agent_teams.tools.database import engine as team_db_engine

    monkeypatch.delattr(team_db_engine, "_ensure_message_meta_column")

    db_bind_race_patch.apply_team_db_bind_race_patch()

    assert getattr(
        team_db_engine._ensure_message_protocol_column,
        db_bind_race_patch._PATCH_MARKER,
        False,
    ) is True


class _VanishingTableInspector:
    """Snapshot lists two tables; the first probe hits a concurrently dropped table."""

    def __init__(self):
        self.snapshots = 0

    def get_table_names(self):
        self.snapshots += 1
        if self.snapshots == 1:
            return ["team_message_a", "team_message_b"]
        return ["team_message_b"]

    def get_columns(self, table_name):
        if table_name == "team_message_a":
            raise NoSuchTableError(table_name)
        return [{"name": "id"}, {"name": "content"}]


class _RecordingConn:
    def __init__(self):
        self.statements = []

    def exec_driver_sql(self, sql):
        self.statements.append(sql)


def test_patched_real_backfill_survives_vanished_table(monkeypatch):
    from openjiuwen.agent_teams.tools.database import engine as team_db_engine

    inspector = _VanishingTableInspector()
    conn = _RecordingConn()
    monkeypatch.setattr(team_db_engine, "inspect", lambda _conn: inspector)

    current = getattr(team_db_engine, "_ensure_message_protocol_column")
    original = getattr(current, "__wrapped__", current)
    wrapper = db_bind_race_patch._tolerate_concurrent_drop(original, "_ensure_message_protocol_column")

    wrapper(conn)

    assert inspector.snapshots == 2
    assert conn.statements == [
        "ALTER TABLE team_message_b ADD COLUMN protocol TEXT NOT NULL DEFAULT 'plain'"
    ]


def test_runtime_entry_patches_install_team_db_bind_race_patch(monkeypatch):
    from jiuwenswarm.agents.harness.common.tools import bash_tool_safety
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common import llm_provider_compat_patch as compat_patch
    from jiuwenswarm.common.auth import login_credentials
    from jiuwenswarm.server.worker import runtime_backend
    import jiuwenswarm.llm_sse_patch as sse_patch

    calls = []
    monkeypatch.setattr(bash_tool_safety, "install_shell_tool_safety_hooks", lambda: None)
    monkeypatch.setattr(compat_patch, "apply_provider_compat_patches", lambda: None)
    monkeypatch.setattr(sse_patch, "apply_openai_sse_invoke_patch", lambda: None)
    monkeypatch.setattr(login_credentials, "apply_login_credential_patch", lambda: None)
    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {}})
    monkeypatch.setattr(
        db_bind_race_patch,
        "apply_team_db_bind_race_patch",
        lambda: calls.append(1),
    )

    runtime_backend._apply_runtime_entry_patches()

    assert calls == [1]
