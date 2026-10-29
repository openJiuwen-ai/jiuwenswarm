"""Policy reload must preserve the Windows runner's bootstrap environment."""

import asyncio
import os
import subprocess
import sys
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.sandbox import jiuwenbox_runner as runner_module


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_retire_only_confirmed_owned_exit_after_job_teardown(monkeypatch, owned):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    runner = runner_module.JiuwenBoxRunner()
    runner.process = SimpleNamespace(returncode=1)
    runner.owns_process = owned
    runner.port = 54657
    events = []
    monkeypatch.setattr(runner, "_release_win_job", lambda: events.append("job"))
    retire = AsyncMock(side_effect=lambda url: events.append(url))
    monkeypatch.setattr(box, "retire_jiuwenbox_service", retire)
    await runner.stop()
    assert events == (["job", "http://127.0.0.1:54657"] if owned else ["job"])
    assert not runner._stopped_base_urls


@pytest.mark.asyncio
async def test_unconfirmed_stop_failure_does_not_retire_service(monkeypatch):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    runner = runner_module.JiuwenBoxRunner()
    runner.process = SimpleNamespace(returncode=None)
    runner.owns_process = True
    monkeypatch.setattr(runner, "_stop_process_no_lock", AsyncMock(side_effect=RuntimeError("kill failed")))
    retire = AsyncMock()
    monkeypatch.setattr(box, "retire_jiuwenbox_service", retire)
    with pytest.raises(RuntimeError, match="kill failed"):
        await runner.stop()
    retire.assert_not_awaited()
    assert not runner._stopped_base_urls


@pytest.mark.asyncio
async def test_failed_retirement_is_retried_before_next_service_start(monkeypatch):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    runner = runner_module.JiuwenBoxRunner()
    runner.process = SimpleNamespace(returncode=1)
    runner.owns_process = True
    retire = AsyncMock(side_effect=[RuntimeError("hook failed"), RuntimeError("hook failed"), []])
    monkeypatch.setattr(box, "retire_jiuwenbox_service", retire)
    spawn = AsyncMock()
    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="hook failed"):
        await runner.stop()
    with pytest.raises(RuntimeError, match="hook failed"):
        await runner.ensure_running(port=55981)
    spawn.assert_not_awaited()
    monkeypatch.setattr(runner, "health_check", AsyncMock(return_value=True))
    assert await runner.ensure_running(port=55981, startup_mode="external")
    assert retire.await_count == 3
    assert not runner._stopped_base_urls


@pytest.mark.asyncio
async def test_stop_preserves_pumps_until_process_exit(monkeypatch):
    runner = runner_module.JiuwenBoxRunner()
    exited = asyncio.Event()
    output = []

    async def pump():
        await exited.wait()
        output.append("shutdown complete")

    async def wait():
        assert not runner._stdout_pump_task.cancelled()
        exited.set()
        return 0

    runner.process = SimpleNamespace(pid=123, returncode=None, terminate=Mock(), wait=wait)
    runner.owns_process = True
    runner._stdout_pump_task = asyncio.create_task(pump())
    monkeypatch.setattr(runner_module, "sys", SimpleNamespace(platform="linux"))
    await runner.stop()
    assert output == ["shutdown complete"]
    assert runner._stdout_pump_task is None


def test_port_bind_failure_preserves_error(monkeypatch):
    from jiuwenswarm.server.sandbox import port_util
    error = OSError("permission denied")
    error.winerror = 10013
    sock = Mock()
    sock.bind.side_effect = error
    monkeypatch.setattr(port_util.socket, "socket", Mock(return_value=sock))
    warning = Mock()
    monkeypatch.setattr(port_util.logger, "warning", warning)
    assert not port_util.is_tcp_port_bindable("127.0.0.1", 8321)
    assert warning.call_args.args[-1] == 10013
    assert "cannot bind" in warning.call_args.args[0]
    sock.close.assert_called_once()


@pytest.mark.asyncio
async def test_stop_timeout_kills_before_draining_shutdown_logs(monkeypatch):
    import ctypes

    runner = runner_module.JiuwenBoxRunner()
    exited = asyncio.Event()
    output = []

    async def pump():
        await exited.wait()
        output.append("shutdown tail")

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(
        GenerateConsoleCtrlEvent=lambda *a: False,
    ), raising=False)
    proc = SimpleNamespace(pid=123, returncode=None, terminate=Mock(),
                           wait=AsyncMock(side_effect=[asyncio.TimeoutError(), 0]),
                           kill=Mock(side_effect=exited.set))
    runner.process = proc
    runner.owns_process = True
    runner._stdout_pump_task = asyncio.create_task(pump())
    await runner.stop()
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert output == ["shutdown tail"]
    assert runner.process is None


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("implicit_git", [False, True])
async def test_policy_reload_retains_bootstrap_env_until_explicitly_replaced(
    tmp_path, monkeypatch, platform, enabled, implicit_git,
):
    from jiuwenswarm.common import config

    for key in list(os.environ):
        if key.startswith("JIUWENBOX_"):
            monkeypatch.delenv(key)
    allowlist = ["HOST_TOOL_TOKEN", "HOST_PYTHON_EXE", "CUSTOM_TOOL_SETTING", "EMPTY_TOOL_SETTING"]
    paths = [str(tmp_path / "shared-dependencies")]
    tools = {"read_roots": [str(tmp_path / "tools")], "executable_dirs": [str(tmp_path / "tools")]}
    runtime = config.get_sandbox_runtime()
    runtime["enabled"] = enabled
    monkeypatch.setattr(config, "get_sandbox_runtime", lambda: runtime)
    warning = Mock()
    monkeypatch.setattr(runner_module.logger, "warning", warning)

    def empty_allowlist_warnings():
        return [call for call in warning.call_args_list if "sandbox.env_allowlist is empty" in call.args[0]]
    monkeypatch.setattr(config, "get_sandbox_env_allowlist", lambda: allowlist)
    monkeypatch.setattr(config, "get_sandbox_path_allowlist", lambda: paths)
    monkeypatch.setattr(runner_module, "sys", SimpleNamespace(platform=platform, executable=sys.executable))
    runner = runner_module.JiuwenBoxRunner()
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    monkeypatch.setattr(runner_module, "_cleanup_stale_win_proxy_ports", lambda: None)
    source = tmp_path / "jiuwenbox-src"
    source.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(runner_module, "_resolve_jiuwenbox_src_dir", lambda: source)
    monkeypatch.setattr(runner, "_register_atexit_once", Mock())
    monkeypatch.setattr(runner, "_bind_win_job", Mock())
    monkeypatch.setattr(runner, "_prepare_windows_setup", AsyncMock())
    monkeypatch.setattr(runner, "_wait_until_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "health_check", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_pump_stream", AsyncMock())
    monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)
    monkeypatch.delenv("JIUWENBOX_SKILLS_DIR", raising=False)
    monkeypatch.setenv("CUSTOM_TOOL_SETTING", "inherited-setting")
    monkeypatch.setenv("EMPTY_TOOL_SETTING", "")
    monkeypatch.setenv("HOST_TOOL_TOKEN", "test-tool-token")
    monkeypatch.setenv("HOST_PYTHON_EXE", "C:/managed/python.exe")
    if implicit_git:
        git = tmp_path / "git-win"
        for name in ("cmd/git.exe", "bin/bash.exe", "usr/bin/bash.exe"):
            path = git / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        tools = {"read_roots": [str(git)],
                 "executable_dirs": [str(git / name) for name in ("cmd", "bin", "usr/bin")]}
        monkeypatch.delenv("JIUWENBOX_TOOL_PATHS", raising=False)
        monkeypatch.setenv("PATH", str(git / "cmd"))
    else:
        monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", json.dumps(tools))
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "unlisted-product-token")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-forwarded")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    async def stop():
        runner.process = None
        runner.owns_process = False
        runner.spawned_policy_path = None
        runner._spawned_policy_fingerprint = None

    monkeypatch.setattr(runner, "_stop_no_lock", AsyncMock(side_effect=stop))
    spawn = AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(
        returncode=None, stdout=None, stderr=None,
    ))
    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", spawn)
    bootstrap_env = {
        "JIUWENBOX_RUNNER_PYTHON": str(tmp_path / "missing-runner" / "python.exe"),
        "JIUWENBOX_SKILLS_DIR": str(skills),
        "CUSTOM_TOOL_SETTING": "startup-override",
    }
    assert await runner.ensure_running(policy_path=policy, extra_env=bootstrap_env)
    # The caller must not be able to mutate retained startup parameters.
    bootstrap_env["JIUWENBOX_RUNNER_PYTHON"] = "C:/unexpected/python.exe"
    policy.write_text("version: 2", encoding="utf-8")
    assert await runner.ensure_running(policy_path=policy)
    assert spawn.await_count == 2
    for call in spawn.await_args_list:
        env = call.kwargs["env"]
        if platform == "win32":
            assert call.kwargs["stdin"] == subprocess.PIPE
            assert call.args[2] == "jiuwenbox.server.parent_control"
        assert env["JIUWENBOX_RUNNER_PYTHON"] == str(tmp_path / "missing-runner" / "python.exe")
        assert env["JIUWENBOX_SKILLS_DIR"] == str(skills)
        if platform == "win32":
            assert json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"]) == [str(source.resolve())]
        else:
            assert "JIUWENBOX_SERVICE_READ_ROOTS" not in env
        assert env["JIUWENBOX_POLICY_PATH"] == str(policy)
        assert env["HOST_TOOL_TOKEN"] == "test-tool-token"
        assert env["HOST_PYTHON_EXE"] == "C:/managed/python.exe"
        assert "CLAW_SKILL_TOKEN" not in env
        assert env["CUSTOM_TOOL_SETTING"] == "startup-override"
        assert env["EMPTY_TOOL_SETTING"] == ""
        assert os.environ["CUSTOM_TOOL_SETTING"] == "inherited-setting"
        assert "OPENAI_API_KEY" not in env
        assert "DATABASE_URL" not in env
        assert env["HTTPS_PROXY"] == "http://proxy.example:8080"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"
        assert json.loads(env["JIUWENBOX_ENV_ALLOWLIST"]) == allowlist
        assert json.loads(env["JIUWENBOX_PATH_ALLOWLIST"]) == paths
        if implicit_git and platform != "win32":
            assert "JIUWENBOX_TOOL_PATHS" not in env
        else:
            assert json.loads(env["JIUWENBOX_TOOL_PATHS"]) == tools

    if platform == "win32":
        assert runner._prepare_windows_setup.await_count == 2
        for call in runner._prepare_windows_setup.await_args_list:
            assert json.loads(call.args[0]["JIUWENBOX_TOOL_PATHS"]) == tools
            assert json.loads(call.args[0]["JIUWENBOX_SERVICE_READ_ROOTS"]) == [str(source.resolve())]

    policy.write_text("version: 3", encoding="utf-8")
    assert await runner.ensure_running(policy_path=policy, extra_env={})
    assert "JIUWENBOX_RUNNER_PYTHON" not in spawn.await_args.kwargs["env"]
    assert "JIUWENBOX_SKILLS_DIR" not in spawn.await_args.kwargs["env"]
    assert spawn.await_args.kwargs["env"]["CUSTOM_TOOL_SETTING"] == "inherited-setting"
    assert empty_allowlist_warnings() == []
    # A config-only allowlist change must restart the owned service too.
    allowlist.clear()
    assert await runner.ensure_running(policy_path=policy)
    assert spawn.await_count == 4
    env = spawn.await_args.kwargs["env"]
    assert "HOST_TOOL_TOKEN" not in env
    assert "CLAW_SKILL_TOKEN" not in env
    assert "CUSTOM_TOOL_SETTING" not in env
    assert "EMPTY_TOOL_SETTING" not in env
    assert json.loads(env["JIUWENBOX_ENV_ALLOWLIST"]) == []
    assert len(empty_allowlist_warnings()) == int(enabled)
    # Reusing the same service must not repeat startup warnings.
    assert await runner.ensure_running(policy_path=policy)
    assert spawn.await_count == 4
    assert len(empty_allowlist_warnings()) == int(enabled)
    # A path-only config change also restarts the service; an explicit empty
    # list cannot retain a stale bootstrap or inherited path allowlist.
    paths.clear()
    assert await runner.ensure_running(policy_path=policy)
    assert spawn.await_count == 5
    assert json.loads(spawn.await_args.kwargs["env"]["JIUWENBOX_PATH_ALLOWLIST"]) == []
    assert len(empty_allowlist_warnings()) == 2 * int(enabled)
    assert "test-tool-token" not in str(warning.call_args_list)
    await asyncio.sleep(0)  # Complete mocked stdout/stderr drain tasks.


_HARD_KILLED_AGENT = """
import asyncio, sys
from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

async def main():
    runner = JiuwenBoxRunner()
    runner.process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    runner._bind_win_job()
    print(runner.process.pid if runner._win_job else "unbound", flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_hard_killed_agent_server_takes_box_server_down():
    import ctypes

    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    agent = subprocess.Popen([sys.executable, "-c", _HARD_KILLED_AGENT], stdout=subprocess.PIPE, text=True, env=env)
    try:
        line = agent.stdout.readline().strip()
        assert line.isdigit(), line
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        child = kernel32.OpenProcess(0x00100000, False, int(line))  # SYNCHRONIZE
        assert child
        agent.kill()
        assert kernel32.WaitForSingleObject(child, 10000) == 0
        kernel32.CloseHandle(child)
    finally:
        agent.kill()
        agent.wait()
        agent.stdout.close()


def test_runner_probe_uses_startup_source_path(monkeypatch, tmp_path):
    source = tmp_path / "src"
    monkeypatch.setattr(runner_module, "_resolve_jiuwenbox_src_dir", lambda: source)
    monkeypatch.setenv("PYTHONPATH", "existing-source")
    monkeypatch.setenv("UNRELATED_TEST_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(subprocess, "run", run)

    assert runner_module.probe_runner_python("runner-python", {"SystemRoot": "C:/Windows"}) is None
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    env = run.call_args.kwargs["env"]
    assert env["PYTHONPATH"] == os.pathsep.join((str(source), "existing-source"))
    assert env["SystemRoot"] == "C:/Windows"
    assert "UNRELATED_TEST_SECRET" not in env
    assert os.environ["PYTHONPATH"] == "existing-source"


@pytest.mark.parametrize("result,reason", [
    (SimpleNamespace(returncode=1, stderr="Traceback\nModuleNotFoundError: missing"),
     "ModuleNotFoundError: missing"),
    (SimpleNamespace(returncode=7, stderr=""), "exit 7"),
    (subprocess.TimeoutExpired("runner-python", 15), "timed out"),
    (OSError("cannot start interpreter"), "cannot start interpreter"),
])
def test_runner_probe_reports_failure(monkeypatch, result, reason):
    run = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(subprocess, "run", run)
    assert reason in runner_module.probe_runner_python("runner-python", {})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited stdin pipe")
def test_runner_probe_with_desktop_stdin_reader():
    # Keep the desktop pipe open while its reader blocks, as Electron does.
    # CPython must run the probe with a separate stdin before any -c code runs.
    code = r'''
import os, sys, threading, time
from jiuwenswarm.server.sandbox.jiuwenbox_runner import probe_runner_python
reader = threading.Thread(target=lambda: sys.stdin.buffer.read(1), daemon=True)
reader.start()
time.sleep(0.1)
result = probe_runner_python(sys.executable, {}, timeout=3)
print(repr(result), flush=True)
# The intentionally blocked test reader must not enter interpreter teardown.
os._exit(0 if result is None else 1)
'''
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        proc.wait(timeout=12)
        stdout, stderr = proc.stdout.read(), proc.stderr.read()
        assert proc.returncode == 0, (stdout, stderr)
        assert stdout.strip() == b"None", (stdout, stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
