# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: get_mcp_skills — list bundled skills with descriptions.

CLI-type MCPs (feishu/dingtalk/...) are NOT MCP servers — they expose
tools via bundled SKILL.md files that teach the agent to call CLI subcommands.

get_mcp_skills(name) returns [{name, description}] by reading each
bundled skill's SKILL.md frontmatter.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry


def _mk_pkg(workspace: Path, name: str, *, cli: dict | None = None, mcp: dict | None = None, skills: dict[str, str] | None = None) -> Path:
    pkg = workspace / "mcp" / "mcp_builtins" / name
    pkg.mkdir(parents=True, exist_ok=True)
    if cli:
        (pkg / "cli.json").write_text(json.dumps(cli), encoding="utf-8")
    if mcp:
        (pkg / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    if skills:
        sdir = pkg / "skills"
        sdir.mkdir(parents=True, exist_ok=True)
        for sname, body in skills.items():
            sp = sdir / sname
            sp.mkdir(parents=True, exist_ok=True)
            (sp / "SKILL.md").write_text(body, encoding="utf-8")
    return pkg


def test_get_connector_skills_returns_name_and_description(tmp_path: Path) -> None:
    """A CLI MCP's bundled skills surface with name + description."""
    _mk_pkg(tmp_path, "feishu", cli={"runtime": {"type": "node"}}, skills={
        "lark-doc": "---\nname: lark-doc\ndescription: 飞书云文档读写\n---\nbody",
        "lark-im": "---\nname: lark-im\ndescription: 飞书消息收发\n---\nbody",
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        skills = registry.get_mcp_skills("feishu")
    assert len(skills) == 2
    assert skills[0]["name"] == "lark-doc"
    assert skills[0]["description"] == "飞书云文档读写"
    assert skills[1]["name"] == "lark-im"


def test_get_connector_skills_empty_when_no_skills(tmp_path: Path) -> None:
    """An MCP with no skills/ dir returns []."""
    _mk_pkg(tmp_path, "plain", mcp={"mcpServers": {"plain": {"url": "https://x"}}})
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        assert registry.get_mcp_skills("plain") == []


def test_get_connector_skills_flat_layout(tmp_path: Path) -> None:
    """Flat layout: skills/SKILL.md is one skill named after the MCP."""
    _mk_pkg(tmp_path, "awesun", cli={"runtime": {"type": "node"}},
            skills={"SKILL.md": "---\nname: awesun\ndescription: 远程控制\n---\nbody"}).parent
    # fix: the helper writes skills/<key>/SKILL.md; for flat layout we need skills/SKILL.md directly
    pkg = tmp_path / "mcp" / "mcp_builtins" / "awesun"
    sdir = pkg / "skills"
    # remove the subdir the helper created, write flat
    import shutil
    shutil.rmtree(sdir)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text("---\nname: awesun\ndescription: 远程控制\n---\nbody", encoding="utf-8")
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        skills = registry.get_mcp_skills("awesun")
    assert len(skills) == 1
    assert skills[0]["name"] == "awesun"


def test_get_connector_skills_missing_description_field(tmp_path: Path) -> None:
    """A SKILL.md without a description frontmatter field yields empty string."""
    _mk_pkg(tmp_path, "x", cli={"runtime": {"type": "node"}}, skills={
        "s1": "---\nname: s1\n---\nbody",
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        skills = registry.get_mcp_skills("x")
    assert skills[0]["name"] == "s1"
    assert skills[0]["description"] == ""
