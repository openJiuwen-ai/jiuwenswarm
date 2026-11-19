# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for cli_sandbox (AgentOS CLI lifecycle execution).

The sandbox itself is never started: the SysOperation, the npm prefix, the
sandbox config and the detached-auth plumbing are all injected, so these tests
cover the routing decisions (which commands leave the AgentServer), the command
wrapping, the mapping from a sandbox exec result to ``CommandResult`` and the
detached auth handle that stands in for ``subprocess.Popen``.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import cli_driver, cli_sandbox
from jiuwenswarm.server.runtime.mcp.cli_driver import (
    ERR_BINARY_NOT_FOUND,
    CliDriver,
    CliManifest,
    CommandResult,
)


def _endpoint(url: str = "http://127.0.0.1:18901", sandbox_type: str = "jiuwenbox") -> dict:
    return {"url": url, "type": sandbox_type}


def _runtime(enabled: bool = True) -> dict:
    return {"enabled": enabled, "files": {"allow": [], "deny": []}}


class TestSandboxCliEnabled:
    def test_disabled_outside_agentos(self, monkeypatch) -> None:
        monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE", raising=False)
        # The sandbox config must not even be consulted outside AgentOS.
        with patch(
            "jiuwenswarm.common.config.get_sandbox_endpoint",
            side_effect=AssertionError("config read outside AgentOS"),
        ):
            assert cli_sandbox.sandbox_cli_enabled() is False

    def test_enabled_on_agentos_with_configured_sandbox(self, monkeypatch) -> None:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
        with (
            patch("jiuwenswarm.common.config.get_sandbox_endpoint", return_value=_endpoint()),
            patch("jiuwenswarm.common.config.get_sandbox_runtime", return_value=_runtime()),
        ):
            assert cli_sandbox.sandbox_cli_enabled() is True

    def test_disabled_when_tool_sandbox_off(self, monkeypatch) -> None:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
        with (
            patch("jiuwenswarm.common.config.get_sandbox_endpoint", return_value=_endpoint()),
            patch(
                "jiuwenswarm.common.config.get_sandbox_runtime",
                return_value=_runtime(enabled=False),
            ),
        ):
            assert cli_sandbox.sandbox_cli_enabled() is False

    def test_disabled_without_endpoint(self, monkeypatch) -> None:
        monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
        with (
            patch(
                "jiuwenswarm.common.config.get_sandbox_endpoint",
                return_value=_endpoint(url=""),
            ),
            patch("jiuwenswarm.common.config.get_sandbox_runtime", return_value=_runtime()),
        ):
            assert cli_sandbox.sandbox_cli_enabled() is False


class _FakeShell:
    """Records the exec request and returns a canned ExecuteCmdResult shape."""

    def __init__(self, result) -> None:
        self._result = result
        self.request: dict | None = None

    async def execute_cmd(self, command: str, *, timeout=None, environment=None):
        self.request = {
            "command": command,
            "timeout": timeout,
            "environment": environment,
        }
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _exec_result(exit_code=0, stdout="", stderr=""):
    return SimpleNamespace(
        data=SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr),
        message="ok",
    )


def _run_in_fake_sandbox(result, *, extra_env=None):
    shell = _FakeShell(result)
    # Real SysOperation exposes ``shell`` as a method (``def shell(self)``);
    # fake it the same way so a missing call fails here like it does in prod.
    operation = SimpleNamespace(shell=lambda: shell)
    with (
        patch.object(cli_sandbox, "_get_sys_operation", return_value=operation),
        patch.object(cli_sandbox, "_npm_prefix", return_value=None),
        patch.object(cli_sandbox, "_cli_home", return_value=None),
    ):
        return cli_sandbox.run_in_sandbox("npm install -g x", extra_env=extra_env), shell


class TestRunInSandbox:
    def test_success_maps_exit_code_and_streams(self) -> None:
        res, shell = _run_in_fake_sandbox(
            _exec_result(stdout="added 1 package", stderr=""),
        )
        assert res.returncode == 0
        assert res.succeeded is True
        assert res.stdout == "added 1 package"
        assert shell.request["timeout"] == 300

    def test_missing_binary_maps_to_binary_not_found(self) -> None:
        """bash reports an unknown command as 127; CliDriver needs the same
        binary_not_found signal it derives from OSError/ENOENT on the host."""
        res, _ = _run_in_fake_sandbox(
            _exec_result(exit_code=127, stderr="bash: lark-cli: command not found"),
        )
        assert res.error_kind == ERR_BINARY_NOT_FOUND
        assert res.succeeded is False

    def test_timeout_exit_code_maps_to_timed_out(self) -> None:
        res, _ = _run_in_fake_sandbox(_exec_result(exit_code=124))
        assert res.timed_out is True
        assert res.succeeded is False

    def test_exec_error_is_reported_as_failure(self) -> None:
        res, _ = _run_in_fake_sandbox(RuntimeError("boom"))
        assert res.succeeded is False
        assert "boom" in res.stderr

    def test_credential_env_is_forwarded(self) -> None:
        _, shell = _run_in_fake_sandbox(
            _exec_result(), extra_env={"GITCODE_TOKEN": "t"},
        )
        assert shell.request["environment"] == {"GITCODE_TOKEN": "t"}

    def test_unavailable_sandbox_fails_instead_of_running_locally(self) -> None:
        with patch.object(cli_sandbox, "_get_sys_operation", return_value=None):
            res = cli_sandbox.run_in_sandbox("npm install -g x")
        assert res.succeeded is False
        assert "unavailable" in res.stderr


class TestWrapCommand:
    def test_npm_prefix_is_on_path(self, tmp_path) -> None:
        prefix = tmp_path / "npm"
        with (
            patch.object(cli_sandbox, "_npm_prefix", return_value=prefix),
            patch.object(cli_sandbox, "_cli_home", return_value=None),
        ):
            wrapped = cli_sandbox._wrap_command("npm install -g x")
        assert wrapped.endswith("npm install -g x")
        assert f"NPM_CONFIG_PREFIX={shlex.quote(str(prefix))}" in wrapped
        assert 'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"' in wrapped

    def test_writable_home_is_exported(self, tmp_path) -> None:
        """A non-root sandbox with HOME=/ cannot write /.local; the wrapper
        must point HOME at the workspace and put pip's bin dir on PATH."""
        home = tmp_path / "home"
        with (
            patch.object(cli_sandbox, "_cli_home", return_value=home),
            patch.object(cli_sandbox, "_npm_prefix", return_value=None),
        ):
            wrapped = cli_sandbox._wrap_command("python3 -m pip install x")
        quoted_home = shlex.quote(str(home))
        assert f"export HOME={quoted_home}" in wrapped
        assert f"export PIP_CACHE_DIR={quoted_home}/.cache/pip" in wrapped
        assert f"export PYTHONUSERBASE={quoted_home}/.local" in wrapped
        assert 'export PATH="$HOME/.local/bin:$PATH"' in wrapped
        assert wrapped.endswith("python3 -m pip install x")

    def test_cli_home_is_created_under_runtime_dir(self, tmp_path) -> None:
        runtime = tmp_path / "mcp" / ".cli_runtime"
        with patch.object(cli_sandbox, "_runtime_dir", return_value=runtime):
            home = cli_sandbox._cli_home()
        assert home == runtime / "home"
        assert home.is_dir()

    def test_unchanged_without_prefixes(self) -> None:
        with (
            patch.object(cli_sandbox, "_npm_prefix", return_value=None),
            patch.object(cli_sandbox, "_cli_home", return_value=None),
        ):
            assert cli_sandbox._wrap_command("npm install -g x") == "npm install -g x"


class TestSandboxEnv:
    """Prefix shared with the agent's own shell (credentials stay in memory)."""

    def _runtime(self, tmp_path):
        runtime = tmp_path / "mcp" / ".cli_runtime"
        runtime.mkdir(parents=True)
        return runtime

    def test_shell_prefix_is_empty_without_connectors(self, tmp_path) -> None:
        runtime = self._runtime(tmp_path)
        with patch.object(cli_sandbox, "_runtime_dir", return_value=runtime):
            assert cli_sandbox.sandbox_shell_prefix() == ""

    def test_home_and_npm_prefixes_appear_once_used(self, tmp_path) -> None:
        runtime = self._runtime(tmp_path)
        (runtime / "home" / ".local" / "bin").mkdir(parents=True)
        (runtime / "home" / ".local" / "bin" / "gitcode").write_text("x")
        (runtime / "npm" / "bin").mkdir(parents=True)
        with patch.object(cli_sandbox, "_runtime_dir", return_value=runtime):
            prefix = cli_sandbox.sandbox_shell_prefix()
        assert f"export HOME={shlex.quote(str(runtime / 'home'))}" in prefix
        assert 'export PATH="$HOME/.local/bin:$PATH"' in prefix
        assert 'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"' in prefix

    def test_prefix_keeps_empty_npm_dir_quiet(self, tmp_path) -> None:
        runtime = self._runtime(tmp_path)
        (runtime / "npm").mkdir(parents=True)
        with patch.object(cli_sandbox, "_runtime_dir", return_value=runtime):
            assert cli_sandbox.sandbox_shell_prefix() == ""


class TestDetachedAuth:
    def _paths(self, tmp_path):
        return tmp_path / "feishu.pid", tmp_path / "feishu.log"

    def test_start_detached_returns_pid_and_detaches(self, tmp_path) -> None:
        pid_path, log_path = self._paths(tmp_path)
        with patch.object(
            cli_sandbox, "run_in_sandbox",
            return_value=CommandResult("x", 0, stdout="4242\n"),
        ) as run:
            pid = cli_sandbox.start_detached(
                "lark-cli auth login", pid_path=pid_path, log_path=log_path,
            )
        assert pid == 4242
        script = run.call_args[0][0]
        # The login must outlive the exec and its output must not hold the
        # sandbox pipe open, otherwise execute_cmd blocks until OAuth ends.
        assert "setsid" in script
        assert "< /dev/null &" in script
        assert shlex.quote(str(log_path)) in script

    def test_start_detached_wraps_home_inside_login_shell(self, tmp_path) -> None:
        """The sandbox image's login shell resets HOME, so the export has to
        be part of the command handed to ``bash -lc``, not a prefix."""
        pid_path, log_path = self._paths(tmp_path)
        # Sandbox paths are POSIX; a Windows tmp_path would need a level of
        # nested quoting that the real deployment never sees.
        home = PurePosixPath("/home/agentos/.jiuwenswarm/agent/workspace/mcp/.cli_runtime/home")
        with (
            patch.object(cli_sandbox, "_cli_home", return_value=home),
            patch.object(cli_sandbox, "_npm_prefix", return_value=None),
            patch.object(
                cli_sandbox, "run_in_sandbox",
                return_value=CommandResult("x", 0, stdout="4242\n"),
            ) as run,
        ):
            cli_sandbox.start_detached(
                "lark-cli auth login", pid_path=pid_path, log_path=log_path,
            )
        script = run.call_args[0][0]
        assert "bash -lc " in script
        assert f"export HOME={shlex.quote(str(home))}" in script

    def test_start_detached_without_pid_returns_none(self, tmp_path) -> None:
        pid_path, log_path = self._paths(tmp_path)
        with patch.object(
            cli_sandbox, "run_in_sandbox",
            return_value=CommandResult("x", 1, stderr="setsid: not found"),
        ):
            assert cli_sandbox.start_detached(
                "lark-cli auth login", pid_path=pid_path, log_path=log_path,
            ) is None

    def test_process_alive_maps_probe_output(self) -> None:
        cases = {
            "alive\n": True,
            "dead\n": False,
            # Unrecognised output (e.g. a truncated probe) is unknown, never
            # "exited": a false exit ends the auth wait early.
            "": None,
        }
        for stdout, expected in cases.items():
            with patch.object(
                cli_sandbox, "run_in_sandbox",
                return_value=CommandResult("x", 0, stdout=stdout),
            ):
                assert cli_sandbox.process_alive(7) is expected
        with patch.object(
            cli_sandbox, "run_in_sandbox", return_value=CommandResult("x", -1),
        ):
            # Sandbox unreachable.
            assert cli_sandbox.process_alive(7) is None

    def test_auth_paths_sanitizes_name(self, tmp_path) -> None:
        with patch.object(cli_sandbox, "_runtime_dir", return_value=tmp_path):
            pid_path, log_path = cli_sandbox.auth_paths("../feishu/x")
        assert pid_path.parent == tmp_path / "auth"
        assert pid_path.name == ".._feishu_x.pid"
        assert log_path.name == ".._feishu_x.log"

    def test_detached_process_poll_mirrors_popen(self, tmp_path) -> None:
        proc = cli_sandbox.DetachedAuthProcess(11, tmp_path / "x.log")
        with patch.object(cli_sandbox, "process_alive", return_value=True):
            assert proc.poll() is None
        with patch.object(cli_sandbox, "process_alive", return_value=False):
            assert proc.poll() == 0
        with patch.object(cli_sandbox, "process_alive", return_value=None):
            assert proc.poll() is None

    def test_detached_process_kill_runs_in_sandbox(self, tmp_path) -> None:
        proc = cli_sandbox.DetachedAuthProcess(11, tmp_path / "x.log")
        with patch.object(cli_sandbox, "kill_process") as kill:
            proc.kill()
        kill.assert_called_once_with(11)


def _cli_manifest() -> CliManifest:
    return CliManifest(
        runtime_type="node",
        init_cmd="npm install -g @larksuite/cli",
        version_cmd="lark-cli --version",
        min_version="1.0.79",
        status_cmd="lark-cli auth status",
        unauth_cmd="lark-cli auth logout",
    )


def _wait_manifest() -> CliManifest:
    return CliManifest(
        init_cmd="npm install -g @larksuite/cli",
        version_cmd="lark-cli --version",
        min_version="1.0.79",
        auth_steps=[
            {
                "command": {"linux": "lark-cli auth login --recommend"},
                "authWaitForExit": True,
                "authUrlDomain": "accounts.feishu.cn",
            }
        ],
        status_cmd="lark-cli auth status",
    )


@pytest.fixture(autouse=True)
def _clear_pending_auth():
    cli_driver._PENDING_AUTH_PROCS.clear()
    yield
    cli_driver._PENDING_AUTH_PROCS.clear()


class TestLifecycleRunnerRouting:
    def _driver(self, host_calls: list[str], manifest: CliManifest | None = None) -> CliDriver:
        return CliDriver(
            "feishu",
            manifest or _cli_manifest(),
            lambda cmd: host_calls.append(cmd) or CommandResult(
                cmd, 1, stderr="not found",
            ),
        )

    def test_install_runs_version_and_init_in_sandbox(self) -> None:
        """versionCheck, init and the post-init re-check all stay in the
        sandbox: the binary the init produced only exists there."""
        host_calls: list[str] = []
        sandbox_calls: list[str] = []
        installed = {"done": False}

        def sandbox_runner(command: str) -> CommandResult:
            sandbox_calls.append(command)
            if command == "npm install -g @larksuite/cli":
                installed["done"] = True
                return CommandResult(command, 0)
            if command == "lark-cli --version":
                if installed["done"]:
                    return CommandResult(command, 0, stdout="lark-cli 1.0.79")
                return CommandResult(
                    command, -1, stderr="bash: lark-cli: command not found",
                    error_kind=ERR_BINARY_NOT_FOUND,
                )
            return CommandResult(command, 1, stderr="unknown")

        driver = self._driver(host_calls)
        driver._runner = sandbox_runner

        res = driver.install()

        assert res.version_ok is True
        assert res.version == "1.0.79"
        assert sandbox_calls == [
            "lark-cli --version",
            "npm install -g @larksuite/cli",
            "lark-cli --version",
        ]
        assert host_calls == []

    def test_status_and_unauth_follow_the_sandbox_runner(self) -> None:
        """The login state lives with the binary, so status/logout must target
        the same sandbox the install ran in."""
        host_calls: list[str] = []
        sandbox_calls: list[str] = []
        driver = self._driver(host_calls)
        driver._runner = lambda cmd: sandbox_calls.append(cmd) or CommandResult(cmd, 0)

        driver.status()
        driver.unauth()

        assert sandbox_calls == ["lark-cli auth status", "lark-cli auth logout"]
        assert host_calls == []

    def test_build_sandbox_runner_disabled_returns_none(self) -> None:
        driver = self._driver([])
        with patch.object(
            cli_sandbox, "sandbox_cli_enabled", return_value=False,
        ):
            assert driver._build_sandbox_runner(None) is None
            assert driver._runner is not None
            assert driver._sandbox_runner is None

    def test_build_sandbox_runner_enabled_forwards_credentials(self) -> None:
        driver = self._driver([])
        driver._cred_keys = ["MCP_TEST_TOKEN"]
        calls: list[tuple[str, dict]] = []

        def fake_run(command: str, **kwargs) -> CommandResult:
            calls.append((command, kwargs))
            return CommandResult(command, 0)

        with (
            patch.object(cli_sandbox, "sandbox_cli_enabled", return_value=True),
            patch.object(cli_sandbox, "run_in_sandbox", side_effect=fake_run),
        ):
            runner = driver._build_sandbox_runner({"MCP_TEST_TOKEN": "secret"})
            assert runner is not None
            runner("gitcode version")

        assert calls[0][0] == "gitcode version"
        assert calls[0][1]["extra_env"] == {"MCP_TEST_TOKEN": "secret"}

    def test_sandbox_env_never_carries_host_only_keys(self) -> None:
        """``__init__`` rewrites the host PATH after the env snapshot, so the
        sandbox must be fed the token-schema keys — not everything that
        differs from ``os.environ``."""
        driver = self._driver([])
        driver._cred_keys = ["GITCODE_TOKEN"]
        env = {"PATH": "/host/bin", "HOME": "/host/home", "GITCODE_TOKEN": "t"}
        calls: list[tuple[str, dict]] = []

        def fake_run(command: str, **kwargs) -> CommandResult:
            calls.append((command, kwargs))
            return CommandResult(command, 0)

        with (
            patch.object(cli_sandbox, "sandbox_cli_enabled", return_value=True),
            patch.object(cli_sandbox, "run_in_sandbox", side_effect=fake_run),
        ):
            runner = driver._build_sandbox_runner(env)
            assert runner is not None
            runner("gitcode version")

        assert calls[0][1]["extra_env"] == {"GITCODE_TOKEN": "t"}

    def test_constructor_routes_every_command_into_sandbox(self) -> None:
        calls: list[str] = []

        def fake_run(command: str, **kwargs) -> CommandResult:
            calls.append(command)
            return CommandResult(command, 0, stdout='{"identity": "user"}')

        with (
            patch.object(CliDriver, "_build_cred_env", return_value=None),
            patch.object(cli_driver, "_binary_dir_from_package", return_value=None),
            patch.object(cli_sandbox, "sandbox_cli_enabled", return_value=True),
            patch.object(cli_sandbox, "run_in_sandbox", side_effect=fake_run),
        ):
            driver = CliDriver("feishu", _cli_manifest())
            driver.status()
            driver.unauth()

        assert calls == ["lark-cli auth status", "lark-cli auth logout"]


class TestSandboxAuthStep:
    def _driver(self) -> CliDriver:
        driver = CliDriver(
            "feishu", _wait_manifest(),
            lambda cmd: CommandResult(cmd, 1, stderr="host must not run this"),
        )
        driver._sandbox_runner = lambda cmd: CommandResult(cmd, 0)
        driver._sandbox_env = {"MCP_TEST_TOKEN": "secret"}
        return driver

    def test_auth_wait_for_exit_starts_detached_in_sandbox(self, tmp_path) -> None:
        pid_path, log_path = tmp_path / "feishu.pid", tmp_path / "feishu.log"
        driver = self._driver()
        with (
            patch.object(cli_sandbox, "auth_paths", return_value=(pid_path, log_path)),
            patch.object(cli_sandbox, "start_detached", return_value=9001) as start,
            patch.object(
                cli_sandbox, "read_text",
                return_value="open https://accounts.feishu.cn/login?token=abc",
            ),
            patch.object(cli_sandbox, "process_alive", return_value=True),
        ):
            result = driver.auth_step(0)

        assert result.succeeded is True
        assert result.needs_user_action is True
        assert result.auth_url == "https://accounts.feishu.cn/login?token=abc"
        start.assert_called_once_with(
            "lark-cli auth login --recommend",
            pid_path=pid_path,
            log_path=log_path,
            extra_env={"MCP_TEST_TOKEN": "secret"},
        )
        pending = cli_driver._PENDING_AUTH_PROCS["feishu"]
        assert pending.pid == 9001

    def test_auth_proc_done_uses_sandbox_liveness(self, tmp_path) -> None:
        driver = self._driver()
        with (
            patch.object(
                cli_sandbox, "auth_paths",
                return_value=(tmp_path / "a.pid", tmp_path / "a.log"),
            ),
            patch.object(cli_sandbox, "start_detached", return_value=9002),
            patch.object(cli_sandbox, "read_text", return_value=""),
            patch.object(cli_sandbox, "process_alive", return_value=True),
            patch.object(cli_driver, "_AUTH_URL_WAIT_S", 0.05),
        ):
            driver.auth_step(0)
        assert driver.auth_proc_done() is False

        with patch.object(cli_sandbox, "process_alive", return_value=False):
            assert driver.auth_proc_done() is True
        assert "feishu" not in cli_driver._PENDING_AUTH_PROCS

    def test_cancel_kills_the_sandbox_login(self, tmp_path) -> None:
        driver = self._driver()
        with (
            patch.object(
                cli_sandbox, "auth_paths",
                return_value=(tmp_path / "a.pid", tmp_path / "a.log"),
            ),
            patch.object(cli_sandbox, "start_detached", return_value=9003),
            patch.object(cli_sandbox, "read_text", return_value=""),
            patch.object(cli_sandbox, "process_alive", return_value=True),
            patch.object(cli_driver, "_AUTH_URL_WAIT_S", 0.05),
        ):
            driver.auth_step(0)

        with patch.object(cli_sandbox, "kill_process") as kill:
            cli_driver.cancel_pending_auth_proc("feishu")

        kill.assert_called_once_with(9003)
        assert "feishu" not in cli_driver._PENDING_AUTH_PROCS

    def test_unstartable_login_fails_instead_of_falling_back(self, tmp_path) -> None:
        driver = self._driver()
        with (
            patch.object(
                cli_sandbox, "auth_paths",
                return_value=(tmp_path / "a.pid", tmp_path / "a.log"),
            ),
            patch.object(cli_sandbox, "start_detached", return_value=None),
        ):
            result = driver.auth_step(0)

        assert result.succeeded is False
        assert "tool sandbox" in result.error

    def test_missing_cli_fails_the_step_instead_of_reporting_auth(self, tmp_path) -> None:
        """``start_detached`` starts a shell even when the CLI is absent: the
        pid exists, but the login is gone and the log only holds "command not
        found". That must fail the step like the host runner's ENOENT."""
        driver = self._driver()
        with (
            patch.object(
                cli_sandbox, "auth_paths",
                return_value=(tmp_path / "a.pid", tmp_path / "a.log"),
            ),
            patch.object(cli_sandbox, "start_detached", return_value=9004),
            patch.object(
                cli_sandbox, "read_text",
                return_value="bash: line 1: lark-cli: command not found\n",
            ),
            patch.object(cli_sandbox, "process_alive", return_value=False),
            patch.object(cli_driver, "_AUTH_URL_WAIT_S", 0.05),
        ):
            result = driver.auth_step(0)

        assert result.succeeded is False
        assert result.needs_user_action is False
        assert result.error_kind == ERR_BINARY_NOT_FOUND
        assert "command not found" in result.error
        assert "feishu" not in cli_driver._PENDING_AUTH_PROCS

    def test_login_that_opened_the_browser_itself_still_succeeds(self, tmp_path) -> None:
        """authSuppressBrowser CLIs open the browser and exit without printing
        a URL; that must not be mistaken for a missing binary."""
        driver = self._driver()
        with (
            patch.object(
                cli_sandbox, "auth_paths",
                return_value=(tmp_path / "a.pid", tmp_path / "a.log"),
            ),
            patch.object(cli_sandbox, "start_detached", return_value=9005),
            patch.object(
                cli_sandbox, "read_text",
                return_value="opened the default browser\n",
            ),
            patch.object(cli_sandbox, "process_alive", return_value=False),
            patch.object(cli_driver, "_AUTH_URL_WAIT_S", 0.05),
        ):
            result = driver.auth_step(0)

        assert result.succeeded is True
        assert result.needs_user_action is True
        assert result.auth_url is None
