# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: gitcode environment-variable token authentication.

gitcode CLI's ``auth status`` reads the token from the ``GITCODE_TOKEN`` /
``GC_TOKEN`` environment variable (or local config) — NOT from an OAuth
flow. So gitcode's cli.json carries an empty ``auth`` array (no
``auth login`` step), and authentication is: the user provisions
``GITCODE_TOKEN`` via the credentials modal, CliDriver injects it into the
subprocess env, and ``auth status`` reports logged in.

Two layers are pinned here:

1. ``connect_mcp`` CLI branch surfaces a ``credentials_required`` prompt
   when the token-schema-declared env keys are not yet provisioned (so the
   user is asked for the token before install/auth run). This reuses the
   skill-only/form-B prompt path.

2. ``CliDriver._build_cred_env`` merges the MCP's stored tokens (keyed by
   its token-schema required fields) onto ``os.environ`` for the subprocess.
   None when the MCP has no token-schema (feishu/dingtalk — pure OAuth, env
   injection is a no-op so OAuth CLIs are unaffected).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_gitcode_pkg(tmp_path: Path) -> Path:
    """Materialize a gitcode marketplace package on disk (cli.json with an
    EMPTY auth array — env-token auth, no OAuth step) + a token-schema.

    Writes under <tmp>/mcp/mcp_builtins/gitcode so _packages_dir() (which is
    <workspace>/mcp/mcp_builtins) finds it when get_workspace_dir is patched
    to tmp_path."""
    pkg = tmp_path / "mcp" / "mcp_builtins" / "gitcode"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "cli.json").write_text(
        '{"init":{"win32":"pip install gitcode-cli"},'
        '"versionCheck":{"command":{"win32":"gitcode version"},"minVersion":"0.9.0"},'
        '"auth":[],'
        '"unAuth":{"win32":"gitcode auth logout --yes"},'
        '"status":{"win32":"gitcode auth status"},'
        '"statusMatch":"Logged in as"}',
        encoding="utf-8",
    )
    (pkg / "token-schema.json").write_text(
        '{"title":"GitCode","fields":[{"key":"GITCODE_TOKEN","type":"password","required":true}]}',
        encoding="utf-8",
    )
    return pkg


# ---------------------------------------------------------------------------
# Layer 1: connect_mcp CLI branch surfaces credentials_required for missing
#           token-schema-declared env keys.
# ---------------------------------------------------------------------------


def test_connect_cli_missing_token_returns_credentials_required(tmp_path: Path, monkeypatch) -> None:
    """gitcode (CLI + token-schema) with no stored token → connect_mcp returns
    credentials_required (NOT _connect_cli / OAuth). The user must provision
    GITCODE_TOKEN first."""
    from jiuwenswarm.server.runtime.mcp import credential, registry, state_store

    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    _write_gitcode_pkg(tmp_path)

    # CredentialStore empty (no token saved).
    with patch(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        return_value={},
    ):
        result = registry.connect_mcp("gitcode")

    assert result.get("credentials_required") is True
    assert "GITCODE_TOKEN" in result.get("required_tokens", [])
    assert result.get("integration_type") == "cli"


def test_connect_cli_token_provisioned_proceeds_to_connect_cli(tmp_path: Path, monkeypatch) -> None:
    """gitcode with the token already stored → connect_mcp does NOT surface
    credentials_required; it proceeds to _connect_cli. We mock _connect_cli
    to capture the call (no real pip/gitcode binary in the test env)."""
    from jiuwenswarm.server.runtime.mcp import credential, registry, state_store

    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    _write_gitcode_pkg(tmp_path)

    captured: dict = {}

    def fake_connect_cli(name, step, *, install_only=False):
        captured["called"] = True
        captured["name"] = name
        return {"name": name, "integration_type": "cli", "auth_required": False}

    with patch(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        return_value={"GITCODE_TOKEN": "tok_123"},
    ), patch(
        "jiuwenswarm.server.runtime.mcp.registry._connect_cli",
        side_effect=fake_connect_cli,
    ):
        result = registry.connect_mcp("gitcode")

    # Token was provisioned → no credentials_required prompt.
    assert "credentials_required" not in result
    # Proceeded into _connect_cli (the token check was bypassed).
    assert captured.get("called") is True
    assert captured.get("name") == "gitcode"


# ---------------------------------------------------------------------------
# Layer 2: CliDriver._build_cred_env merges stored tokens into the subprocess
#           env; None when the MCP has no token-schema (OAuth CLIs unaffected).
# ---------------------------------------------------------------------------


def test_cli_driver_cred_env_includes_token_schema_keys(tmp_path: Path, monkeypatch) -> None:
    """CliDriver for a CLI MCP WITH a token-schema merges the stored token
    into _cred_env (so `gitcode auth status` sees GITCODE_TOKEN in its env)."""
    from jiuwenswarm.server.runtime.mcp import cli_driver, credential

    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    _write_gitcode_pkg(tmp_path)

    with patch(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        return_value={"GITCODE_TOKEN": "tok_abc"},
    ):
        drv = cli_driver.CliDriver("gitcode", cli_driver.CliManifest())

    cred_env = drv._cred_env
    assert cred_env is not None
    assert cred_env.get("GITCODE_TOKEN") == "tok_abc"
    # os.environ base is preserved (PATH etc. still present).
    assert "PATH" in cred_env or any(k in cred_env for k in os.environ)


def test_cli_driver_cred_env_none_when_no_token_schema(tmp_path: Path, monkeypatch) -> None:
    """CliDriver for a pure-OAuth CLI (feishu — no token-schema) returns
    _cred_env=None → env injection is a no-op, OAuth flow is unaffected."""
    from jiuwenswarm.server.runtime.mcp import cli_driver, credential

    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    # feishu package with a cli.json but NO token-schema.json, under the
    # marketplace dir so _pkg_dir finds it.
    pkg = tmp_path / "mcp" / "mcp_builtins" / "feishu"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "cli.json").write_text(
        '{"init":{"win32":"npm i -g @larksuite/cli"},'
        '"versionCheck":{"command":{"win32":"lark-cli --version"},"minVersion":"1.0.79"},'
        '"auth":[{"command":{"win32":"lark-cli auth login"},"authUrlDomain":"accounts.feishu.cn"}],'
        '"status":{"win32":"lark-cli auth status"}}',
        encoding="utf-8",
    )

    drv = cli_driver.CliDriver("feishu", cli_driver.CliManifest())
    # No token-schema → _cred_env is None → env inherits parent (OAuth path).
    assert drv._cred_env is None


def test_cli_driver_cred_env_none_when_store_empty(tmp_path: Path, monkeypatch) -> None:
    """Even with a token-schema, an empty CredentialStore (token not yet
    provisioned) → _cred_env=None (nothing to inject). The connect_mcp
    prompt path catches this case first; CliDriver just defends in depth."""
    from jiuwenswarm.server.runtime.mcp import cli_driver, credential

    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    _write_gitcode_pkg(tmp_path)

    with patch(
        "jiuwenswarm.server.runtime.mcp.credential.CredentialStore.get_all",
        return_value={},
    ):
        drv = cli_driver.CliDriver("gitcode", cli_driver.CliManifest())

    assert drv._cred_env is None


# ---------------------------------------------------------------------------
# default_runner: env=None inherits parent (existing behavior unchanged).
# ---------------------------------------------------------------------------


def test_default_runner_env_none_inherits_parent(monkeypatch) -> None:
    """default_runner(cmd, env=None) must NOT pass env= to subprocess (so the
    child inherits the parent env — the pre-existing behavior). We patch
    subprocess.run to capture the kwargs."""
    from jiuwenswarm.server.runtime.mcp import cli_driver

    captured: dict = {}

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env", "__missing__")
        return _FakeProc()

    monkeypatch.setattr(cli_driver.subprocess, "run", fake_run)
    # _safe_split_command on win32 may resolve via shutil.which; patch to a
    # bare split so no PATH lookup happens in the test.
    monkeypatch.setattr(cli_driver, "_safe_split_command", lambda c: c.split())

    cli_driver.default_runner("echo hi", env=None)
    # env=None passed through → subprocess inherits parent env (existing behavior).
    assert captured["env"] is None
