# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: disconnect_mcp uninstalls bundled skills for non-cli MCPs.

Regression: previously only the ``cli`` integration-type branch in
``disconnect_mcp`` called ``uninstall_mcp_skills``. skill-only (form D,
e.g. netease-mail) and remote-mcp (form A) MCPs left orphaned
``mcp/skills/<name>/`` dirs and stale ``skill_configs`` entries behind, so
the agent could still read those skills after disconnect. CLI MCPs (form C,
e.g. feishu) worked because their branch deleted the dir, masking the bug.

These tests pin the fix: disconnect removes the bundled skills dir and
skill_configs entry for every marketplace MCP type that ships skills, and
is a no-op for remote MCPs without bundled skills.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry
from jiuwenswarm.server.runtime.mcp import state_store


class _ws_ctx:
    """Patch get_workspace_dir at its source (common.utils) so every module
    that imports it (registry/credential/state_store/skill_installer/...) sees
    the tmp_path workspace. Also patch the per-module bound copies that some
    modules keep, for safety."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        ws_targets = [
            "jiuwenswarm.common.utils.get_workspace_dir",
            "jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir",
            "jiuwenswarm.server.runtime.mcp.credential.get_workspace_dir",
            "jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir",
            "jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir",
        ]
        skills_targets = [
            "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
            "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
            "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
        ]
        self._patches = [patch(t, return_value=self.path) for t in ws_targets]
        self._patches += [
            patch(t, return_value=self.path / "skills") for t in skills_targets if t.endswith("get_agent_skills_dir")
        ]
        self._patches += [
            patch(
                "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
                return_value=self.path / "skills" / "skills_state.json",
            )
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _ws(path: Path) -> _ws_ctx:
    return _ws_ctx(path)


def _mk_skill_only_pkg(workspace: Path, name: str, skill: str = "mail") -> Path:
    """A skill-only (form D) MCP package: skills/ dir, no cli.json/mcp.json."""
    pkg = workspace / "mcp" / "mcp_builtins" / name
    skill_dir = pkg / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: x\n---\nbody", encoding="utf-8"
    )
    return pkg


def _mk_remote_mcp_pkg(workspace: Path, name: str) -> Path:
    """A remote-mcp (form A) package: mcp.json with url, no skills/."""
    pkg = workspace / "mcp" / "mcp_builtins" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "mcp.json").write_text(
        json.dumps({"mcpServers": {name: {"url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    return pkg


def _install_skill_marker(workspace: Path, mcp_name: str, skill: str) -> Path:
    """Simulate connect-time install_mcp_skills: drop mcp/skills/<mcp>/<skill>/."""
    installed = workspace / "mcp" / "skills" / mcp_name / skill
    installed.mkdir(parents=True, exist_ok=True)
    body = "---\nname: %s\ndescription: x\n---\nbody" % skill
    (installed / "SKILL.md").write_text(body, encoding="utf-8")
    return installed


def _seed_skill_config(workspace: Path, skill: str, enabled: bool = True) -> None:
    """Seed skills_state.json with a skill_configs entry (as connect does)."""
    skills_dir = workspace / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    state_file = skills_dir / "skills_state.json"
    state = {}
    if state_file.is_file():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    state.setdefault("skill_configs", {})[skill] = {"enabled": enabled}
    state_file.write_text(json.dumps(state), encoding="utf-8")


def _skill_configs(workspace: Path) -> dict:
    state_file = workspace / "skills" / "skills_state.json"
    if not state_file.is_file():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8")).get("skill_configs", {})


def test_disconnect_skill_only_removes_bundled_skills_dir(tmp_path: Path) -> None:
    """skill-only MCP (form D) disconnect must delete mcp/skills/<name>/.

    Previously only the cli branch called uninstall_mcp_skills, so the
    skill-only dir survived disconnect and the agent could still read it.
    """
    _mk_skill_only_pkg(tmp_path, "netease-mail", "mail")
    installed = _install_skill_marker(tmp_path, "netease-mail", "mail")
    assert installed.is_dir()
    with _ws(tmp_path), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
        return_value={"name": "netease-mail", "removed": True},
    ):
        registry.disconnect_mcp("netease-mail")
    # The whole per-MCP skills dir must be gone.
    assert not (tmp_path / "mcp" / "skills" / "netease-mail").is_dir()


def test_disconnect_skill_only_clears_skill_configs(tmp_path: Path) -> None:
    """skill-only MCP disconnect must drop the skill_configs entry too."""
    _mk_skill_only_pkg(tmp_path, "netease-mail", "mail")
    _install_skill_marker(tmp_path, "netease-mail", "mail")
    _seed_skill_config(tmp_path, "mail", enabled=True)
    assert "mail" in _skill_configs(tmp_path)
    with _ws(tmp_path), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
        return_value={"name": "netease-mail", "removed": True},
    ):
        registry.disconnect_mcp("netease-mail")
    assert "mail" not in _skill_configs(tmp_path)


def test_disconnect_remote_mcp_without_skills_is_noop(tmp_path: Path) -> None:
    """A remote-mcp (form A) without bundled skills: disconnect must not
    error and must not create any skills dir. Confirms the fix has no
    side effects on pure-remote MCPs."""
    _mk_remote_mcp_pkg(tmp_path, "tyc-mcp")
    with _ws(tmp_path), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
        return_value={"name": "tyc-mcp", "removed": True},
    ):
        result = registry.disconnect_mcp("tyc-mcp")
    assert result["removed"] is True
    assert not (tmp_path / "mcp" / "skills" / "tyc-mcp").is_dir()


def test_disconnect_cli_still_uninstalls_skills(tmp_path: Path) -> None:
    """CLI MCP (form C) disconnect keeps uninstalling skills (regression
    guard on the fix moving uninstall_mcp_skills out of the cli branch)."""
    pkg = tmp_path / "mcp" / "mcp_builtins" / "feishu"
    (pkg / "skills").mkdir(parents=True, exist_ok=True)
    (pkg / "skills" / "lark-doc" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (pkg / "skills" / "lark-doc" / "SKILL.md").write_text(
        "---\nname: lark-doc\ndescription: x\n---\nbody", encoding="utf-8"
    )  # noqa: E501
    (pkg / "cli.json").write_text(json.dumps({"binary": "lark-cli"}), encoding="utf-8")
    _install_skill_marker(tmp_path, "feishu", "lark-doc")
    _seed_skill_config(tmp_path, "lark-doc", enabled=True)
    with _ws(tmp_path), patch(
        "jiuwenswarm.server.runtime.mcp.cli_driver.CliDriver.unauth", lambda self: None
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
        return_value={"name": "feishu", "removed": True},
    ):
        registry.disconnect_mcp("feishu")
    assert not (tmp_path / "mcp" / "skills" / "feishu").is_dir()
    assert "lark-doc" not in _skill_configs(tmp_path)
