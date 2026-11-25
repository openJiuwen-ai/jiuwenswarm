# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the sandbox shell env injection (MCP CLI connectors).

The proxy is exercised against a fake shell operation; no sandbox is started.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import cli_env_patch

_REAL_CREDENTIAL_ENV = cli_env_patch._credential_env


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    """Keep the tests off the real credential store unless they opt in."""
    monkeypatch.setattr(cli_env_patch, "_credential_env", lambda: {})


class _FakeShell:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.envs: list[dict | None] = []

    async def execute_cmd(self, command, *args, **kwargs):
        self.commands.append(command)
        self.envs.append(kwargs.get("environment"))
        return SimpleNamespace(data=SimpleNamespace(exit_code=0))

    async def execute_cmd_background(self, command, *args, **kwargs):
        self.commands.append(command)
        self.envs.append(kwargs.get("environment"))
        return SimpleNamespace(data=SimpleNamespace(pid=1))

    async def execute_cmd_stream(self, command, *args, **kwargs):
        self.commands.append(command)
        self.envs.append(kwargs.get("environment"))
        yield SimpleNamespace(data=SimpleNamespace(text="x"))


class _FakeSysOperation:
    def __init__(self, inner, *, mode="sandbox"):
        self._inner = inner
        self.mode = mode
        self._instances: dict[str, object] = {}

    def shell(self):
        if "shell" not in self._instances:
            self._instances["shell"] = self._inner
        return self._instances["shell"]


def _wrap(enabled: bool = True, mode="sandbox"):
    operation = _FakeSysOperation(_FakeShell(), mode=mode)
    with (
        patch.object(cli_env_patch, "_prefix", return_value='export HOME="/w"'),
        patch(
            "jiuwenswarm.server.runtime.mcp.cli_sandbox.sandbox_cli_enabled",
            return_value=enabled,
        ),
    ):
        wrapped = cli_env_patch.wrap_shell_operation(operation, operation.shell())
    return operation, wrapped


class TestWrapShellOperation:
    def test_sandbox_operation_is_wrapped_and_cached(self) -> None:
        operation, wrapped = _wrap()
        assert isinstance(wrapped, cli_env_patch._CliEnvShellOperation)
        assert operation._instances["shell"] is wrapped
        assert operation.shell() is wrapped

    def test_already_wrapped_is_returned_as_is(self) -> None:
        operation, wrapped = _wrap()
        with patch.object(cli_env_patch, "_prefix", return_value='export HOME="/w"'):
            assert cli_env_patch.wrap_shell_operation(operation, wrapped) is wrapped

    def test_local_operation_is_untouched(self) -> None:
        """A host shell must never receive sandbox paths or connector tokens."""
        operation, wrapped = _wrap(mode="local")
        assert wrapped is operation._inner

    def test_disabled_outside_agentos_is_untouched(self) -> None:
        operation, wrapped = _wrap(enabled=False)
        assert wrapped is operation._inner
        assert not isinstance(operation._instances["shell"], cli_env_patch._CliEnvShellOperation)

    def test_missing_internal_cache_does_not_crash(self) -> None:
        class _NoCache:
            mode = "sandbox"

        with patch(
            "jiuwenswarm.server.runtime.mcp.cli_sandbox.sandbox_cli_enabled",
            return_value=True,
        ):
            wrapped = cli_env_patch.wrap_shell_operation(_NoCache(), _FakeShell())
        assert isinstance(wrapped, cli_env_patch._CliEnvShellOperation)


class TestInstallShellPatch:
    def test_patches_once_and_is_idempotent(self, monkeypatch) -> None:
        calls: list[str] = []

        class _FakeSysOperation:
            def shell(self):
                calls.append("orig")
                return "raw"

        monkeypatch.setattr(cli_env_patch, "_PATCHED", False)
        monkeypatch.setattr(
            "jiuwenswarm.common.agentos_runtime.is_agentos_runtime",
            lambda: True,
        )
        monkeypatch.setattr(
            "openjiuwen.core.sys_operation.SysOperation", _FakeSysOperation,
        )
        monkeypatch.setattr(cli_env_patch, "wrap_shell_operation", lambda op, sh: f"wrapped:{sh}")
        cli_env_patch.install_shell_patch()
        patched_shell = _FakeSysOperation.shell
        cli_env_patch.install_shell_patch()  # second call must not patch again
        assert cli_env_patch._PATCHED is True
        assert _FakeSysOperation.shell is patched_shell
        assert _FakeSysOperation().shell() == "wrapped:raw"
        assert calls == ["orig"]


class TestInstallShellPatchAgentosOnly:
    def test_not_installed_outside_agentos(self, monkeypatch) -> None:
        """Non-AgentOS deployments must be left completely untouched."""

        class _FakeSysOperation:
            def shell(self):
                return "raw"

        original = _FakeSysOperation.shell
        monkeypatch.setattr(cli_env_patch, "_PATCHED", False)
        monkeypatch.setattr(
            "jiuwenswarm.common.agentos_runtime.is_agentos_runtime",
            lambda: False,
        )
        monkeypatch.setattr(
            "openjiuwen.core.sys_operation.SysOperation", _FakeSysOperation,
        )
        cli_env_patch.install_shell_patch()
        assert cli_env_patch._PATCHED is False
        assert _FakeSysOperation.shell is original


class TestProxy:
    async def test_execute_cmd_prefixes_the_command(self) -> None:
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with patch.object(cli_env_patch, "_prefix", return_value="export HOME=/w"):
            await proxy.execute_cmd("gitcode version")
        assert inner.commands == ["true; export HOME=/w; gitcode version"]

    async def test_background_and_stream_are_prefixed(self) -> None:
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with patch.object(cli_env_patch, "_prefix", return_value="export HOME=/w"):
            await proxy.execute_cmd_background("lark-cli auth login")
            _ = [item async for item in proxy.execute_cmd_stream("ls")]
        assert inner.commands == [
            "true; export HOME=/w; lark-cli auth login",
            "true; export HOME=/w; ls",
        ]

    async def test_empty_prefix_keeps_command_unchanged(self) -> None:
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with patch.object(cli_env_patch, "_prefix", return_value=""):
            await proxy.execute_cmd("gitcode version")
        assert inner.commands == ["gitcode version"]

    async def test_credentials_are_exported_inside_the_prefix(self) -> None:
        """The prefix must carry the token: the provider's own `export` is
        skipped when `cd <workdir>` fails (the cd short-circuit bug)."""
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with (
            patch(
                "jiuwenswarm.server.runtime.mcp.cli_sandbox.sandbox_shell_prefix",
                return_value="",
            ),
            patch.object(
                cli_env_patch, "_credential_env",
                return_value={"GITCODE_TOKEN": "stored"},
            ),
        ):
            await proxy.execute_cmd("gitcode repo list")
        assert inner.commands == ["true; export GITCODE_TOKEN=stored; gitcode repo list"]

    async def test_non_identifier_credential_key_is_skipped(self) -> None:
        """Keys come from a package's token-schema.json (marketplace data);
        only plain shell identifiers may reach the command text."""
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with (
            patch(
                "jiuwenswarm.server.runtime.mcp.cli_sandbox.sandbox_shell_prefix",
                return_value="",
            ),
            patch.object(
                cli_env_patch, "_credential_env",
                return_value={
                    "GITCODE_TOKEN": "ok",
                    "BAD; touch /tmp/pwned #": "x",
                    "TOKEN-KEY": "y",
                    "": "z",
                },
            ),
        ):
            await proxy.execute_cmd("gitcode repo list")

        assert inner.commands == ["true; export GITCODE_TOKEN=ok; gitcode repo list"]

    async def test_caller_environment_is_passed_through(self) -> None:
        """The proxy only prefixes the command; it no longer rewrites env."""
        inner = _FakeShell()
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        with patch.object(cli_env_patch, "_prefix", return_value=""):
            await proxy.execute_cmd(
                "gitcode repo list",
                environment={"OTHER": "1"},
            )
        assert inner.envs == [{"OTHER": "1"}]
        assert inner.commands == ["gitcode repo list"]

    def test_other_attributes_are_forwarded(self) -> None:
        inner = _FakeShell()
        inner.list_tools = lambda: ["t"]  # type: ignore[attr-defined]
        proxy = cli_env_patch._CliEnvShellOperation(inner)
        assert proxy.list_tools() == ["t"]


class TestCredentialSource:
    def test_reads_only_the_declared_schema_keys(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cli_env_patch, "_credential_env", _REAL_CREDENTIAL_ENV)
        (tmp_path / "mcp" / "credentials").mkdir(parents=True)
        (tmp_path / "mcp" / "credentials" / "gitcode.json").write_text("{}")
        store = SimpleNamespace(
            get_all=lambda name: {"GITCODE_TOKEN": "t", "UNRELATED": "o"},
        )
        with (
            patch(
                "jiuwenswarm.common.utils.get_agent_workspace_dir",
                return_value=str(tmp_path),
            ),
            patch(
                "jiuwenswarm.server.runtime.mcp.credential.CredentialStore",
                return_value=store,
            ),
            patch(
                "jiuwenswarm.server.runtime.mcp.credential.required_tokens_from_schema",
                return_value=["GITCODE_TOKEN"],
            ),
        ):
            assert cli_env_patch._credential_env() == {"GITCODE_TOKEN": "t"}

    def test_empty_credential_dir_yields_nothing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(cli_env_patch, "_credential_env", _REAL_CREDENTIAL_ENV)
        with patch(
            "jiuwenswarm.common.utils.get_agent_workspace_dir",
            return_value=str(tmp_path),
        ):
            assert cli_env_patch._credential_env() == {}
