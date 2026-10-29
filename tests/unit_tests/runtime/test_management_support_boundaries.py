"""Shared Runtime rules do not import management transport implementations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.runtime import project_activity


def test_shared_helpers_import_without_gateway_adapter_ui_or_sdk():
    root = Path(__file__).resolve().parents[3]
    source = r"""
import importlib.abc
import sys

blocked = (
    "openjiuwen", "jiuwenswarm.server.runtime.gateway_adapter",
    "jiuwenswarm.channels", "gateway",
)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Runtime helper imported management code: " + fullname)
sys.meta_path.insert(0, Guard())
from jiuwenswarm.runtime.config_reload import reload_runtime_after_config_save
from jiuwenswarm.runtime.project_activity import project_busy_sessions, project_conversation_state
assert callable(reload_runtime_after_config_save)
assert callable(project_busy_sessions)
assert callable(project_conversation_state)
assert not any(name == prefix or name.startswith(prefix + ".")
    for name in sys.modules for prefix in blocked)
print("NEUTRAL_RUNTIME_HELPERS_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "NEUTRAL_RUNTIME_HELPERS_OK" in result.stdout


def test_project_activity_counts_web_but_checks_every_channel_without_cron(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata

    sessions = [
        {"session_id": "web-idle", "channel_id": "web", "project_id": "project"},
        {"session_id": "web-heartbeat", "channel_id": "web", "project_id": "project"},
        {"session_id": "feishu-busy", "channel_id": "feishu", "project_id": "project"},
        {"session_id": "tui-idle", "channel_id": "tui", "project_id": "project"},
        {"session_id": "cron_legacy", "channel_id": "web", "project_id": "project"},
        {
            "session_id": "cron-bound",
            "channel_id": "web",
            "project_id": "project",
            "cron_id": "job",
        },
        {"session_id": "other-busy", "channel_id": "web", "project_id": "other"},
    ]
    monkeypatch.setattr(
        session_metadata, "collect_all_sessions_metadata", lambda: sessions
    )
    queried = []

    def running(session_id, *, ignore_heartbeats):
        queried.append((session_id, ignore_heartbeats))
        return session_id == "feishu-busy" or (
            session_id == "web-heartbeat" and not ignore_heartbeats
        )

    runtime = SimpleNamespace(is_session_running=running)
    assert project_activity.project_conversation_state("project") == (2, [])
    assert queried == []
    assert project_activity.project_conversation_state("project", runtime) == (
        2,
        ["web-heartbeat", "feishu-busy"],
    )
    assert queried == [
        (name, False)
        for name in ("web-idle", "web-heartbeat", "feishu-busy", "tui-idle")
    ]
    queried.clear()
    assert project_activity.project_busy_sessions("project", runtime) == ["feishu-busy"]
    assert queried == [
        (name, True)
        for name in ("web-idle", "web-heartbeat", "feishu-busy", "tui-idle")
    ]


def test_legacy_adapters_export_the_same_runtime_implementations():
    from jiuwenswarm.runtime.config_reload import reload_runtime_after_config_save
    from jiuwenswarm.server.runtime.gateway_adapter import (
        config_adapter,
        project_adapter,
    )

    assert (
        config_adapter.reload_runtime_after_config_save
        is reload_runtime_after_config_save
    )
    assert (
        project_adapter._project_conversation_state
        is project_activity.project_conversation_state
    )
    assert (
        project_adapter._project_busy_sessions is project_activity.project_busy_sessions
    )
