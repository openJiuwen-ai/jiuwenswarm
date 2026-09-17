# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for CLI MCP <bin> mcp stdio entry derivation.

Bug: ``_finalize_cli`` only registered a stdio entry when mcp.json had a
``command``. But pure CLI connectors (feishu/dingtalk/zsxq/awesun/lovrabet/
tmeet/wecom) have NO mcp.json — only cli.json. So none registered an MCP
server; tools stayed invisible even after a "successful" connect.

Fix: derive the stdio entry from cli.json — run ``<bin> mcp`` where <bin> is
extracted from the versionCheck (or init) command.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import cli_driver as cli_driver_mod
from jiuwenswarm.server.runtime.mcp.cli_driver import (
    CliManifest,
    CommandResult,
)


def _feishu_manifest() -> CliManifest:
    return CliManifest(
        runtime_type="node",
        runtime_version=">=18",
        init_cmd="npm install -g @larksuite/cli",
        version_cmd="lark-cli.cmd --version",
        min_version="1.0.77",
        auth_steps=[{"command": {"win32": "lark-cli.cmd auth login --recommend"}, "authWaitForExit": True, "authUrlDomain": "accounts.feishu.cn"}],
        unauth_cmd="lark-cli.cmd auth logout",
        status_cmd="lark-cli.cmd auth status",
        status_match={"identity": "user"},
    )


# --- _derive_bin_name: pure function under test ---

@pytest.mark.parametrize("cmd,expected", [
    ("lark-cli.cmd --version", "lark-cli"),
    ("dws.cmd --version", "dws"),
    ("zsxq-cli --version", "zsxq-cli"),
    ("awesun-cli --version", "awesun-cli"),
    ("lovrabet --version", "lovrabet"),
    ("tmeet.cmd --version", "tmeet"),
    ("wecom-cli.cmd --version", "wecom-cli"),
])
def test_derive_bin_name_from_command(cmd: str, expected: str) -> None:
    assert cli_driver_mod._derive_bin_name(cmd) == expected


def test_derive_bin_name_init_command_not_used() -> None:
    """npm package names don't map to bin names (@larksuite/cli -> lark-cli),
    so build_stdio_entry must NOT pass init_cmd to _derive_bin_name. The pure
    function returns the first token ('npm') which is correct-but-useless;
    the guard is at build_stdio_entry (only version_cmd is fed in)."""
    # _derive_bin_name is context-free; it returns the first token verbatim.
    assert cli_driver_mod._derive_bin_name("npm install -g @larksuite/cli") == "npm"
    # build_stdio_entry only consults version_cmd, never init_cmd, so a
    # manifest with only init_cmd (no versionCheck) yields no entry.
    m = CliManifest(init_cmd="npm install -g @larksuite/cli")
    assert cli_driver_mod.build_stdio_entry("x", m) is None


def test_derive_bin_name_empty_returns_none() -> None:
    assert cli_driver_mod._derive_bin_name("") is None
    assert cli_driver_mod._derive_bin_name("   ") is None


# --- build_stdio_entry: produces the config.yaml mcp.servers entry ---

def test_build_stdio_entry_from_feishu_manifest() -> None:
    entry = cli_driver_mod.build_stdio_entry("feishu", _feishu_manifest())
    assert entry is not None
    assert entry["name"] == "feishu"
    assert entry["transport"] == "stdio"
    assert entry["enabled"] is True
    assert entry["command"] == "lark-cli"
    assert entry["args"] == ["mcp"]
    assert entry["server_id_scope"] == "mcp:feishu"


def test_build_stdio_entry_dingtalk() -> None:
    m = CliManifest(version_cmd="dws.cmd --version")
    entry = cli_driver_mod.build_stdio_entry("dingtalk", m)
    assert entry is not None
    assert entry["command"] == "dws"
    assert entry["args"] == ["mcp"]


def test_build_stdio_entry_none_when_no_bin_derivable() -> None:
    m = CliManifest(version_cmd="", init_cmd="", min_version="")
    assert cli_driver_mod.build_stdio_entry("x", m) is None


# --- _finalize_cli end-to-end: pure CLI does NOT register a stdio MCP entry ---

def test_finalize_cli_no_mcp_entry_for_pure_cli(tmp_path: Path) -> None:
    """Pure CLI connectors (feishu: no mcp.json, no `mcp` subcommand) must NOT
    register a stdio MCP entry — the CLI exposes business subcommands
    (docs/im/...) that a SKILL.md teaches the agent to call via exec, not MCP.
    Registering a bogus ``lark-cli mcp`` stdio entry made stdio connect fail.

    Pure CLI still writes a state.json record (carrying skills, no MCP entry)
    so disconnect/enable/disable can find it — that's expected, not an MCP reg.
    """
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
    # NOTE: no mcp.json — pure CLI.

    upserts: list[dict] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.cli_driver.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, **e})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills", return_value={"installed": ["lark-doc"]}):
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli
        from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult

        inst = InstallResult(name="feishu", installed=True, version="1.0.80", min_version="1.0.77", version_ok=True)
        result = _finalize_cli("feishu", inst)

    # A state.json record IS written (carries skills, no MCP entry) — that's
    # the pure-CLI state record, not an MCP registration.
    assert len(upserts) == 1
    assert "command" not in upserts[0]  # no stdio command — not an MCP entry
    assert "server_id_scope" in upserts[0]
    # No stdio MCP entry registered (lark-cli has no `mcp` subcommand).
    assert result["mcp_entry"] is None
    assert result["installed_skills"] == ["lark-doc"]


def test_finalize_cli_registers_mcp_for_hybrid_package(tmp_path: Path) -> None:
    """Hybrid packages (cloudbase: cli.json for auth + mcp.json with command)
    DO register a stdio MCP entry from mcp.json's command field, written to
    state.json."""
    mp = tmp_path / "mcp" / "mcp_builtins" / "cloudbase"
    mp.mkdir(parents=True)
    (mp / "cli.json").write_text(
        '{"runtime":{"type":"node","version":">=18"},'
        '"init":{"win32":"npm install -g @cloudbase/cli@latest"},'
        '"versionCheck":{"command":{"win32":"tcb.cmd --version"},"minVersion":"3.6.4"}}',
        encoding="utf-8",
    )
    (mp / "mcp.json").write_text(
        '{"mcpServers":{"cloudbase":{"command":"npx","args":["-y","@cloudbase/cloudbase-mcp@latest"]}}}',
        encoding="utf-8",
    )

    upserts: list[dict] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, **e})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills", return_value={"installed": []}):
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli
        from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult

        inst = InstallResult(name="cloudbase", installed=True, version="3.6.4", min_version="3.6.4", version_ok=True)
        result = _finalize_cli("cloudbase", inst)

    # mcp.json declares command=npx -> stdio entry written to state.json.
    assert len(upserts) == 1
    assert upserts[0]["command"] == "npx"
    assert result["mcp_entry"] is not None
