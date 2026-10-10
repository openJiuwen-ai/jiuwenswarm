# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: skill-only MCP (form D) — ctrip-wendao / netease-mail.

Three gaps fixed:
  1. _detect_integration_type now returns "skill-only" (was mis-detected as
     "remote-mcp") for packages with token-schema.json + skills/ but no
     cli.json / mcp.json.
  2. skill_installer rewrites ``@skills/connector-<name>`` path placeholders
     in bundled SKILL.md files to the installed skill dir's absolute path,
     so BashTool can resolve ``node <abs path>``.
  3. _sync_mcp_credentials_environment injects connected MCPs' tokens into
     os.environ so skill scripts (run via BashTool, which inherits
     os.environ) see their token env vars.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Gap 1: _detect_integration_type
# ---------------------------------------------------------------------------

def _write_pkg(root: Path, *, files: dict[str, str] | None = None,
               dirs: list[str] | None = None) -> Path:
    pkg = root / "ctrip-wendao"
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        p = pkg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for d in dirs or []:
        (pkg / d).mkdir(parents=True, exist_ok=True)
    return pkg


def test_detect_skill_only_when_token_schema_and_skills(tmp_path: Path) -> None:
    """token-schema.json + skills/ + no cli.json/mcp.json → skill-only."""
    from jiuwenswarm.server.runtime.mcp.registry import _detect_integration_type
    pkg = _write_pkg(tmp_path, files={"token-schema.json": '{"fields": []}'},
                    dirs=["skills"])
    assert _detect_integration_type(pkg) == "skill-only"


def test_detect_skill_only_when_skills_only_no_schema(tmp_path: Path) -> None:
    """skills/ dir + no cli.json/mcp.json/token-schema → skill-only.

    A credentialless skill package (no token-schema.json) still classifies as
    skill-only — a skills/ dir with no MCP host is the defining trait, not the
    credential file. token-schema.json is one possible cred source, not the
    only one.
    """
    from jiuwenswarm.server.runtime.mcp.registry import _detect_integration_type
    pkg = _write_pkg(tmp_path, dirs=["skills"])
    assert _detect_integration_type(pkg) == "skill-only"


def test_detect_remote_when_no_skills_no_mcp_no_cli(tmp_path: Path) -> None:
    """No cli.json, no mcp.json, no skills/ → remote-mcp fallback."""
    from jiuwenswarm.server.runtime.mcp.registry import _detect_integration_type
    pkg = _write_pkg(tmp_path)  # empty package
    assert _detect_integration_type(pkg) == "remote-mcp"


def test_connect_skill_only_credentialless_connects(tmp_path: Path, monkeypatch) -> None:
    """A skill-only package with NO token-schema connects straight through
    (no credentials_required) — token-schema is one cred source, not the
    only one. Regression for the connect path's skill-only branch."""
    from jiuwenswarm.server.runtime.mcp import registry
    from jiuwenswarm.server.runtime.mcp import state_store
    pkg = _write_pkg(tmp_path, dirs=["skills"])  # no token-schema
    monkeypatch.setattr(registry, "_packages_dir", lambda: tmp_path)
    monkeypatch.setattr(registry, "_install_bundled_skills_safe",
                        lambda n: ["ctrip-wendao"])
    monkeypatch.setattr(state_store, "upsert_mcp_record", lambda *a, **kw: None)
    # credential.required_tokens_from_schema reads the real workspace (not
    # tmp_path); patch it to return [] to model a credentialless skill package.
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.required_tokens_from_schema",
        lambda n: [],
    )
    result = registry.connect_mcp("ctrip-wendao")
    assert result.get("integration_type") == "skill-only"
    assert result.get("installed_skills") == ["ctrip-wendao"]
    assert "credentials_required" not in result


def test_connect_empty_package_raises(tmp_path: Path, monkeypatch) -> None:
    """A package with no cli.json/mcp.json AND no skills/ raises — there is
    nothing to connect. The skill-only branch's empty-package guard."""
    from jiuwenswarm.server.runtime.mcp import registry
    pkg = _write_pkg(tmp_path)  # truly empty
    monkeypatch.setattr(registry, "_packages_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="no skills directory"):
        registry.connect_mcp("ctrip-wendao")


def test_detect_skill_only_ignored_when_mcp_present(tmp_path: Path) -> None:
    """mcp.json takes precedence over skills/ — an MCP with both an MCP
    server AND bundled skills is an MCP-type MCP, not skill-only."""
    from jiuwenswarm.server.runtime.mcp.registry import _detect_integration_type
    pkg = _write_pkg(tmp_path, files={
        "mcp.json": '{"mcpServers": {"x": {"url": "https://y"}}}',
    }, dirs=["skills"])
    assert _detect_integration_type(pkg) == "remote-mcp"


def test_detect_cli_takes_precedence_over_skill_only(tmp_path: Path) -> None:
    """cli.json present → cli, even if skills/ + token-schema.json present."""
    from jiuwenswarm.server.runtime.mcp.registry import _detect_integration_type
    pkg = _write_pkg(tmp_path, files={
        "cli.json": '{"runtime": {}}',
        "token-schema.json": '{"fields": []}',
    }, dirs=["skills"])
    assert _detect_integration_type(pkg) == "cli"


# ---------------------------------------------------------------------------
# Gap 2: skill_installer @skills/ placeholder rewrite
# ---------------------------------------------------------------------------

def test_rewrite_skill_paths_replaces_self_placeholder(tmp_path: Path) -> None:
    """@skills/connector-<self> in SKILL.md → installed absolute path."""
    from jiuwenswarm.server.runtime.mcp.skill_installer import (
        _rewrite_skill_paths,
    )
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        '```bash\nnode @skills/connector-ctrip-wendao/scripts/wendao_query.js "q"\n```',
        encoding="utf-8",
    )
    with patch(
        "jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir",
        return_value=tmp_path,
    ):
        # ctrip-wendao is flat layout, normalized at install time to
        # mcp/skills/<name>/<name>/ (install_mcp_skills copies pkg/skills/ to
        # dest_root/skill_name). _rewrite_skill_paths receives that dest dir.
        installed_dir = tmp_path / "mcp" / "skills" / "ctrip-wendao" / "ctrip-wendao"
        _rewrite_skill_paths(skill_md, "ctrip-wendao", installed_dir)
    text = skill_md.read_text(encoding="utf-8")
    # Placeholder replaced with the installed skill dir's absolute path.
    expected = str(installed_dir)
    assert "@skills/" not in text
    assert expected in text


def test_rewrite_skill_paths_leaves_cross_connector_placeholder(tmp_path: Path) -> None:
    """@skills/connector-<other> is left as-is (can't resolve at install time)."""
    from jiuwenswarm.server.runtime.mcp.skill_installer import _rewrite_skill_paths
    skill_md = tmp_path / "SKILL.md"
    original = 'node @skills/connector-other/scripts/x.js'
    skill_md.write_text(original, encoding="utf-8")
    installed_dir = tmp_path / "mcp" / "skills" / "ctrip-wendao"
    _rewrite_skill_paths(skill_md, "ctrip-wendao", installed_dir)
    assert skill_md.read_text(encoding="utf-8") == original


def test_rewrite_skill_paths_noop_without_placeholder(tmp_path: Path) -> None:
    """SKILL.md without @skills/ is untouched."""
    from jiuwenswarm.server.runtime.mcp.skill_installer import _rewrite_skill_paths
    skill_md = tmp_path / "SKILL.md"
    original = "# no placeholder here\njust text"
    skill_md.write_text(original, encoding="utf-8")
    installed_dir = tmp_path / "mcp" / "skills" / "ctrip-wendao"
    _rewrite_skill_paths(skill_md, "ctrip-wendao", installed_dir)
    assert skill_md.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Gap 3: _sync_connector_credentials_environment / clear
# ---------------------------------------------------------------------------

def _make_bare_adapter():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    a = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    return a


def test_sync_writes_connected_connector_tokens_to_environ(tmp_path: Path,
                                                           monkeypatch) -> None:
    """sync writes each connected MCP's stored tokens to os.environ."""
    adapter = _make_bare_adapter()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.state_store.list_connected_mcps",
        lambda: [{"name": "ctrip-wendao"}, {"name": "netease-mail"}],
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        lambda self, n: {"WENDAO_API_KEY": "tok-w"} if n == "ctrip-wendao"
        else {"NETEASE_EMAIL_USER": "u@x", "NETEASE_EMAIL_PASS": "p"} if n == "netease-mail"
        else {},
    )
    for k in ("WENDAO_API_KEY", "NETEASE_EMAIL_USER", "NETEASE_EMAIL_PASS"):
        monkeypatch.delenv(k, raising=False)

    adapter._sync_mcp_credentials_environment()

    assert os.environ.get("WENDAO_API_KEY") == "tok-w"
    assert os.environ.get("NETEASE_EMAIL_USER") == "u@x"
    assert os.environ.get("NETEASE_EMAIL_PASS") == "p"


def test_clear_removes_connector_env_keys(tmp_path: Path, monkeypatch) -> None:
    """clear pops the MCP's token env keys (from store + schema)."""
    adapter = _make_bare_adapter()
    monkeypatch.setenv("WENDAO_API_KEY", "tok")
    monkeypatch.setenv("OTHER_KEY", "keep")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        lambda self, n: {} if n == "ctrip-wendao" else {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.required_tokens_from_schema",
        lambda n: ["WENDAO_API_KEY"] if n == "ctrip-wendao" else [],
    )

    adapter._clear_mcp_credentials_environment("ctrip-wendao")

    assert "WENDAO_API_KEY" not in os.environ
    assert os.environ.get("OTHER_KEY") == "keep"


def test_mcp_env_keys_from_store_and_schema(monkeypatch) -> None:
    """_mcp_env_keys merges stored keys + schema required keys."""
    adapter = _make_bare_adapter()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        lambda self, n: {"STORED_KEY": "v"} if n == "x" else {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.credential.required_tokens_from_schema",
        lambda n: ["SCHEMA_KEY"] if n == "x" else [],
    )
    keys = adapter._mcp_env_keys("x")
    assert keys == ["SCHEMA_KEY", "STORED_KEY"]


def test_sync_skips_when_no_store_module(monkeypatch) -> None:
    """sync is a no-op (not a crash) if the mcp modules are absent."""
    adapter = _make_bare_adapter()
    # Force the import to fail by hiding the state_store module.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if "state_store" in name:
            raise ImportError("simulated")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Should not raise.
    adapter._sync_mcp_credentials_environment()
