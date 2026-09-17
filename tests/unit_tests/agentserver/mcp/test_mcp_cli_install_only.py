# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``install_only`` mode on CLI MCP connect.

``connect_mcp(name, install_only=True)`` runs the CLI install + auth steps
but skips bundled-skill copy/load — the state.json record carries no
``skills`` field so ``connected_mcp_skill_dirs`` derives no scan dir and the
agent never sees the MCP's skills. Used when another feature manages skills
itself. Default ``install_only=False`` keeps the full connect flow unchanged.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult


def _feishu_pkg(tmp_path: Path) -> Path:
    """Lay out a feishu-style pure-CLI marketplace package (cli.json only)."""
    mp = tmp_path / "mcp" / "mcp_builtins" / "feishu"
    mp.mkdir(parents=True)
    (mp / "cli.json").write_text(
        '{"runtime":{"type":"node","version":">=18"},'
        '"init":{"win32":"npm install -g @larksuite/cli"},'
        '"versionCheck":{"command":{"win32":"lark-cli.cmd --version"},"minVersion":"1.0.77"},'
        '"auth":[{"command":{"win32":"lark-cli.cmd auth login --recommend"},'
        '"authWaitForExit":true,"authUrlDomain":"accounts.feishu.cn"}],'
        '"unAuth":{"win32":"lark-cli.cmd auth logout"},'
        '"status":{"win32":"lark-cli.cmd auth status"},'
        '"statusMatchJson":{"identity":"user"}}',
        encoding="utf-8",
    )
    # A bundled skills dir (flat layout) — install_only must NOT copy it.
    (mp / "skills" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (mp / "skills" / "SKILL.md").write_text("---\ndescription: feishu skill\n---", encoding="utf-8")
    return mp


def test_finalize_cli_install_only_skips_skill_copy(tmp_path: Path) -> None:
    """``install_only=True`` → ``_finalize_cli`` must NOT call install_mcp_skills."""
    _feishu_pkg(tmp_path)

    upserts: list[dict] = []
    skill_calls: list[str] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.cli_driver.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, "entry": dict(e), "kw": kw})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills",
               side_effect=lambda n: skill_calls.append(n) or {"installed": ["feishu"]}):
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli
        inst = InstallResult(name="feishu", installed=True, version="1.0.80",
                             min_version="1.0.77", version_ok=True)
        result = _finalize_cli("feishu", inst, install_only=True)

    # Skill installer must NOT run in install_only mode.
    assert skill_calls == []
    # installed_skills is empty; state.json record carries no skills field.
    assert result["installed_skills"] == []
    assert len(upserts) == 1
    assert upserts[0]["kw"].get("skills") in (None, [], False)
    # Pure CLI → no stdio MCP entry.
    assert result["mcp_entry"] is None


def test_finalize_cli_default_still_copies_skills(tmp_path: Path) -> None:
    """``install_only=False`` (default) → install_mcp_skills runs as before."""
    _feishu_pkg(tmp_path)

    upserts: list[dict] = []
    skill_calls: list[str] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.cli_driver.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, "entry": dict(e), "kw": kw})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills",
               side_effect=lambda n: skill_calls.append(n) or {"installed": ["feishu"]}):
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli
        inst = InstallResult(name="feishu", installed=True, version="1.0.80",
                             min_version="1.0.77", version_ok=True)
        result = _finalize_cli("feishu", inst)

    # Default mode runs the skill installer.
    assert skill_calls == ["feishu"]
    assert result["installed_skills"] == ["feishu"]
    assert upserts[0]["kw"].get("skills") == ["feishu"]


def test_connect_mcp_install_only_threads_through_auth(tmp_path: Path) -> None:
    """``connect_mcp(name, install_only=True)`` threads the flag through
    ``_connect_cli`` → ``_finalize_cli`` (install_only still honors auth_required
    sentinel; finalize is reached only after auth completes)."""
    _feishu_pkg(tmp_path)

    from jiuwenswarm.server.runtime.mcp.cli_driver import (
        CliDriver, StatusResult,
    )

    skill_calls: list[str] = []
    upserts: list[dict] = []

    # Fake CliDriver.install → version_ok; auth_step(0) → needs user action
    # (auth_required sentinel). complete_cli_auth → status authenticated →
    # advance → finalize(install_only=True).
    def fake_install(self, *a, **kw):
        return InstallResult(name=self.name, installed=True, version="1.0.80",
                             min_version="1.0.77", version_ok=True)

    from jiuwenswarm.server.runtime.mcp.cli_driver import AuthStepResult

    def fake_auth_step(self, idx, *a, **kw):
        return AuthStepResult(name=self.name, step_index=idx,
                              command="lark-cli.cmd auth login --recommend",
                              succeeded=True, needs_user_action=True,
                              auth_url="https://accounts.feishu.cn/login?token=x",
                              auth_domain="accounts.feishu.cn")

    def fake_auth_steps_count(self, *a, **kw):
        return 1

    # status() is called twice across the two-phase connect→auth_complete
    # flow, on different CliDriver instances. Closure counter tracks which
    # call we're on: call 1 (connect_mcp pre-check) → not authenticated (miss
    # the fast path → auth_step(0) returns the auth_required sentinel); call 2
    # (complete_cli_auth) → authenticated (advance → finalize).
    status_calls = {"n": 0}

    def fake_status(self, *a, **kw):
        status_calls["n"] += 1
        authenticated = status_calls["n"] >= 2
        return StatusResult(name="feishu", authenticated=authenticated, matched={})

    def fake_auth_proc_done(self, *a, **kw):
        return True

    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.cli_driver.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, "kw": kw}) or dict(e)), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills",
               side_effect=lambda n: skill_calls.append(n) or {"installed": ["feishu"]}), \
         patch.object(CliDriver, "install", fake_install), \
         patch.object(CliDriver, "auth_step", fake_auth_step), \
         patch.object(CliDriver, "auth_steps_count", fake_auth_steps_count), \
         patch.object(CliDriver, "status", fake_status), \
         patch.object(CliDriver, "auth_proc_done", fake_auth_proc_done):
        from jiuwenswarm.server.runtime.mcp.registry import (
            connect_mcp, complete_cli_auth,
        )
        # First call hits auth_step(0) → needs_user_action → auth_required sentinel.
        r1 = connect_mcp("feishu", install_only=True)
        assert r1["auth_required"] is True
        assert r1["auth_url"].startswith("https://accounts.feishu.cn")
        # Skills must NOT have been copied yet (auth pending).
        assert skill_calls == []

        # Auth complete → status authenticated → advance → finalize(install_only=True).
        r2 = complete_cli_auth("feishu", r1["step_index"], install_only=True)
        assert r2["auth_required"] is False
        # finalize ran in install_only mode → no skill copy.
        assert skill_calls == []
        assert r2["installed_skills"] == []
        # state.json record carries no skills field.
        assert upserts and upserts[-1]["kw"].get("skills") in (None, [], False)
