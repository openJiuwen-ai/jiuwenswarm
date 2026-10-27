# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-scoped permissions overlay for 「会话内记住」."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_SESSION_ID


def _load_permissions_persist():
    """Load the module directly to avoid rails/__init__ pulling team deps."""
    mod_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "permissions"
        / "permissions_persist.py"
    )
    spec = importlib.util.spec_from_file_location(
        "permissions_persist_session_overlay_under_test", mod_path
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": {"enabled": True, "file_guard": {"paths": []}}},
    )
    return root


def _file_guard_exec_allow(path: str) -> dict:
    return {
        "enabled": True,
        "file_guard": {
            "enabled": True,
            "paths": [
                {
                    "path": path,
                    "read": "allow",
                    "write": "ask",
                    "exec": "allow",
                    "match": "prefix",
                }
            ],
        },
    }


def test_apply_session_overlay_merges_file_guard_exec_allow():
    pp = _load_permissions_persist()
    base = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [],
        },
    }
    overlay = {
        "file_guard": {
            "enabled": True,
            "paths": [
                {
                    "path": "D:/xiaoyi/test.py",
                    "read": "allow",
                    "write": "ask",
                    "exec": "allow",
                    "match": "prefix",
                }
            ],
        }
    }
    merged = pp.apply_session_permissions_overlay(base, overlay)
    paths = (merged.get("file_guard") or {}).get("paths") or []
    assert any(
        isinstance(p, dict)
        and str(p.get("path", "")).replace("\\", "/").rstrip("/") == "D:/xiaoyi/test.py"
        and p.get("exec") == "allow"
        for p in paths
    )
    defaults = (merged.get("file_guard") or {}).get("defaults") or {}
    assert defaults.get("exec") == "ask"


def test_composed_overlay_cannot_replace_cloud_deny(tmp_path):
    from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
        FileGuardChecker, normalize_path_guard_config,
    )
    from openjiuwen.harness.security.permission_engine.models import PermissionLevel

    pp = _load_permissions_persist()
    target = str(tmp_path / "private")
    base = {
        "composition": {"schema_version": 1, "revision": "sec-test", "source_order": ["user", "cloud"]},
        "file_guard": {"enabled": True, "paths": [{"path": target, "read": "deny", "layer": "cloud"}]},
    }
    overlay = {"file_guard": {"paths": [{"path": target, "read": "allow", "write": "allow"}]}}
    merged = pp.apply_session_permissions_overlay(base, overlay)
    paths = merged["file_guard"]["paths"]
    assert len(paths) == 2
    assert paths[0]["layer"] == "cloud" and paths[0]["read"] == "deny"
    assert paths[1]["layer"] == "user"
    checker = FileGuardChecker(normalize_path_guard_config(merged, workspace_root=tmp_path))
    assert checker._resolve_one(tmp_path / "private" / "data.txt", "read")[0] == PermissionLevel.DENY

    base["composition"]["source_order"] = ["cloud"]
    disabled = pp.apply_session_permissions_overlay(base, overlay)
    assert disabled["file_guard"]["paths"] == base["file_guard"]["paths"]
    base["composition"]["source_order"] = ["user", "cloud"]
    base["file_guard"]["enabled"] = False
    overlay["file_guard"]["enabled"] = True
    assert pp.apply_session_permissions_overlay(base, overlay)["file_guard"]["enabled"] is False


@pytest.mark.parametrize("overlay_enabled,with_path", [(True, False), (None, True), (True, True)])
def test_session_overlay_preserves_explicit_file_guard_disable(overlay_enabled, with_path):
    pp = _load_permissions_persist()
    base = {"enabled": True, "file_guard": {"enabled": False, "paths": []}}
    guard = {}
    if overlay_enabled is not None:
        guard["enabled"] = overlay_enabled
    if with_path:
        guard["paths"] = [{"path": "D:/remembered", "read": "allow"}]

    merged = pp.apply_session_permissions_overlay(base, {"file_guard": guard})

    assert merged["file_guard"]["enabled"] is False
    assert bool(merged["file_guard"]["paths"]) is with_path
    assert base["file_guard"] == {"enabled": False, "paths": []}


def test_persist_session_allow_rule_writes_overlay(sessions_root):
    pp = _load_permissions_persist()
    token = CURRENT_SESSION_ID.set("sess-abc")
    try:
        ok = pp.persist_session_allow_rule(_file_guard_exec_allow("D:/xiaoyi/test.py"))
        assert ok is True
        overlay_path = sessions_root / "sess-abc" / "session_permissions.yaml"
        assert overlay_path.is_file()
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_persist_session_allow_rule_uses_explicit_session_id_when_contextvar_empty(
    sessions_root,
):
    """Interrupt resume often has an empty ContextVar; explicit id must still write."""
    pp = _load_permissions_persist()
    token = CURRENT_SESSION_ID.set("")
    try:
        ok = pp.persist_session_allow_rule(
            _file_guard_exec_allow("D:/xiaoyi/test.py"),
            session_id="sess-explicit",
        )
        assert ok is True
        overlay_path = sessions_root / "sess-explicit" / "session_permissions.yaml"
        assert overlay_path.is_file()
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_session_batch_writes_only_supplied_approvals(sessions_root):
    pp = _load_permissions_persist()
    grants = [{"id": "first", "action": "allow"}, {"id": "second", "action": "allow"}]
    snapshot = {
        "approval_overrides": [*grants, {"id": "unrelated", "action": "allow"}],
        "file_guard": {"paths": [{"path": "D:/unrelated", "read": "allow"}]},
    }
    assert pp.persist_session_allow_rule(snapshot, session_id="batch", approval_grants=grants)
    stored = pp.load_session_permissions_overlay("batch")
    assert stored["approval_overrides"] == grants
    assert "file_guard" not in stored


def test_persist_session_allow_rule_rejects_empty_session_id(sessions_root):
    pp = _load_permissions_persist()
    token = CURRENT_SESSION_ID.set("")
    try:
        ok = pp.persist_session_allow_rule(_file_guard_exec_allow("D:/xiaoyi/test.py"))
        assert ok is False
        assert not any(sessions_root.rglob("session_permissions.yaml"))
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_persist_session_allow_rule_rejects_parent_dir_session_id(sessions_root):
    pp = _load_permissions_persist()
    token = CURRENT_SESSION_ID.set("..")
    try:
        ok = pp.persist_session_allow_rule(_file_guard_exec_allow("D:/xiaoyi/test.py"))
        assert ok is False
        assert not (sessions_root.parent / "session_permissions.yaml").exists()
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_persist_session_allow_rule_writes_only_delta(sessions_root, monkeypatch):
    """Full merged snapshot must not dump disk tools/paths/overrides into overlay."""
    pp = _load_permissions_persist()
    disk = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [
                {
                    "path": "C:/trusted",
                    "read": "allow",
                    "write": "allow",
                    "exec": "ask",
                    "match": "prefix",
                }
            ],
        },
        "approval_overrides": [
            {
                "id": "disk_ov",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "ls *",
                "action": "allow",
            }
        ],
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": disk},
    )
    merged = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [
                disk["file_guard"]["paths"][0],
                {
                    "path": "D:/xiaoyi/test.py",
                    "read": "allow",
                    "write": "ask",
                    "exec": "allow",
                    "match": "prefix",
                },
            ],
        },
        "approval_overrides": [
            disk["approval_overrides"][0],
            {
                "id": "sess_ov",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "python test.py",
                "action": "allow",
            },
        ],
    }
    token = CURRENT_SESSION_ID.set("sess-delta")
    try:
        assert pp.persist_session_allow_rule(merged) is True
        overlay = pp.load_session_permissions_overlay("sess-delta")
        assert "tools" not in overlay
        assert "enabled" not in overlay
        fg = overlay.get("file_guard") or {}
        assert "defaults" not in fg
        assert "enabled" not in fg
        paths = [
            str(p.get("path", "")).replace("\\", "/").rstrip("/")
            for p in (fg.get("paths") or [])
            if isinstance(p, dict)
        ]
        assert paths == ["D:/xiaoyi/test.py"]
        ov_ids = [
            str(i.get("id"))
            for i in (overlay.get("approval_overrides") or [])
            if isinstance(i, dict)
        ]
        assert ov_ids == ["sess_ov"]
        overlay_path = sessions_root / "sess-delta" / "session_permissions.yaml"
        assert overlay_path.is_file()
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_persist_session_allow_rule_accumulates_incremental_paths(
    sessions_root, monkeypatch
):
    pp = _load_permissions_persist()
    disk = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {"enabled": True, "paths": []},
        "approval_overrides": [],
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": disk},
    )
    token = CURRENT_SESSION_ID.set("sess-acc")
    try:
        assert pp.persist_session_allow_rule(
            _file_guard_exec_allow("D:/xiaoyi/a.py")
        ) is True
        second = {
            "file_guard": {
                "paths": [
                    {
                        "path": "D:/xiaoyi/a.py",
                        "read": "allow",
                        "write": "ask",
                        "exec": "allow",
                        "match": "prefix",
                    },
                    {
                        "path": "D:/xiaoyi/b.py",
                        "read": "allow",
                        "write": "ask",
                        "exec": "allow",
                        "match": "prefix",
                    },
                ]
            }
        }
        assert pp.persist_session_allow_rule(second) is True
        overlay = pp.load_session_permissions_overlay("sess-acc")
        paths = sorted(
            str(p.get("path", "")).replace("\\", "/").rstrip("/")
            for p in ((overlay.get("file_guard") or {}).get("paths") or [])
            if isinstance(p, dict)
        )
        assert paths == ["D:/xiaoyi/a.py", "D:/xiaoyi/b.py"]
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_extract_session_overlay_delta_skips_unchanged_disk_rules():
    pp = _load_permissions_persist()
    disk_path = {
        "path": "C:/trusted",
        "read": "allow",
        "write": "allow",
        "exec": "ask",
        "match": "prefix",
    }
    baseline = {
        "file_guard": {"paths": [disk_path]},
        "approval_overrides": [{"id": "disk_ov", "action": "allow", "pattern": "ls *"}],
    }
    merged = {
        "tools": {"bash": "ask"},
        "file_guard": {
            "defaults": {"exec": "ask"},
            "paths": [
                disk_path,
                {
                    "path": "D:/xiaoyi/test.py",
                    "read": "allow",
                    "write": "ask",
                    "exec": "allow",
                    "match": "prefix",
                },
            ],
        },
        "approval_overrides": [
            {"id": "disk_ov", "action": "allow", "pattern": "ls *"},
            {"id": "sess_ov", "action": "allow", "pattern": "python test.py"},
        ],
    }
    delta = pp.extract_session_overlay_delta(baseline, merged)
    paths = [
        str(p.get("path", "")).replace("\\", "/").rstrip("/")
        for p in ((delta.get("file_guard") or {}).get("paths") or [])
        if isinstance(p, dict)
    ]
    assert paths == ["D:/xiaoyi/test.py"]
    assert [i.get("id") for i in delta.get("approval_overrides") or []] == ["sess_ov"]
    assert "tools" not in delta
    assert "defaults" not in (delta.get("file_guard") or {})


def test_interrupt_helpers_wires_session_overlay():
    """Source-level check: avoid importing rails/__init__ (heavy / py3.13 pysbd)."""
    helpers = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "interrupt"
        / "interrupt_helpers.py"
    )
    text = helpers.read_text(encoding="utf-8")
    assert "persist_session_allow_rule=" in text
    assert "get_permissions_with_session_overlay" in text
    assert "session_id: str | None = None" in text


def test_get_permissions_with_session_overlay_includes_exec_allow(
    sessions_root, monkeypatch
):
    pp = _load_permissions_persist()
    disk = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [],
        },
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": disk},
    )
    token = CURRENT_SESSION_ID.set("sess-overlay")
    try:
        assert pp.persist_session_allow_rule(
            _file_guard_exec_allow("D:/xiaoyi/test.py")
        ) is True
        merged = pp.get_permissions_with_session_overlay()
        paths = (merged.get("file_guard") or {}).get("paths") or []
        assert any(
            isinstance(p, dict)
            and str(p.get("path", "")).replace("\\", "/").rstrip("/")
            == "D:/xiaoyi/test.py"
            and p.get("exec") == "allow"
            for p in paths
        )
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_get_permissions_overlay_uses_explicit_session_id_when_contextvar_empty(
    sessions_root, monkeypatch
):
    pp = _load_permissions_persist()
    disk = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "paths": [],
        },
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"permissions": disk},
    )
    assert pp.persist_session_allow_rule(
        _file_guard_exec_allow("D:/xiaoyi/test.py"),
        session_id="sess-overlay-arg",
    ) is True
    token = CURRENT_SESSION_ID.set("")
    try:
        empty = pp.get_permissions_with_session_overlay()
        empty_paths = (empty.get("file_guard") or {}).get("paths") or []
        assert not any(
            isinstance(p, dict)
            and str(p.get("path", "")).replace("\\", "/").rstrip("/")
            == "D:/xiaoyi/test.py"
            for p in empty_paths
        )
        merged = pp.get_permissions_with_session_overlay(session_id="sess-overlay-arg")
        paths = (merged.get("file_guard") or {}).get("paths") or []
        assert any(
            isinstance(p, dict)
            and str(p.get("path", "")).replace("\\", "/").rstrip("/")
            == "D:/xiaoyi/test.py"
            and p.get("exec") == "allow"
            for p in paths
        )
    finally:
        CURRENT_SESSION_ID.reset(token)
