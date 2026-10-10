"""Policy reload must preserve the Windows runner's bootstrap environment."""

import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.sandbox import jiuwenbox_runner as runner_module


@pytest.mark.asyncio
async def test_policy_reload_retains_bootstrap_env_until_explicitly_replaced(tmp_path, monkeypatch):
    runner = runner_module.JiuwenBoxRunner()
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    monkeypatch.setattr(runner_module, "_cleanup_stale_win_proxy_ports", lambda: None)
    monkeypatch.setattr(runner_module, "_resolve_jiuwenbox_src_dir", lambda: None)
    monkeypatch.setattr(runner, "_register_atexit_once", Mock())
    monkeypatch.setattr(runner, "_wait_until_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_pump_stream", AsyncMock())
    monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)
    monkeypatch.delenv("JIUWENBOX_SKILLS_DIR", raising=False)
    monkeypatch.setenv("UNRELATED_TEST_SECRET", "must-not-be-forwarded")

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
        "JIUWENBOX_RUNNER_PYTHON": "C:/Python312/python.exe",
        "JIUWENBOX_SKILLS_DIR": "C:/workspace/skills",
    }
    assert await runner.ensure_running(policy_path=policy, extra_env=bootstrap_env)
    # The caller must not be able to mutate retained startup parameters.
    bootstrap_env["JIUWENBOX_RUNNER_PYTHON"] = "C:/unexpected/python.exe"
    policy.write_text("version: 2", encoding="utf-8")
    assert await runner.ensure_running(policy_path=policy)
    assert spawn.await_count == 2
    for call in spawn.await_args_list:
        env = call.kwargs["env"]
        assert env["JIUWENBOX_RUNNER_PYTHON"] == "C:/Python312/python.exe"
        assert env["JIUWENBOX_SKILLS_DIR"] == "C:/workspace/skills"
        assert env["JIUWENBOX_POLICY_PATH"] == str(policy)
        assert "UNRELATED_TEST_SECRET" not in env

    policy.write_text("version: 3", encoding="utf-8")
    assert await runner.ensure_running(policy_path=policy, extra_env={})
    assert "JIUWENBOX_RUNNER_PYTHON" not in spawn.await_args.kwargs["env"]
    assert "JIUWENBOX_SKILLS_DIR" not in spawn.await_args.kwargs["env"]
    await asyncio.sleep(0)  # Complete mocked stdout/stderr drain tasks.


def test_runner_probe_uses_startup_source_path(monkeypatch, tmp_path):
    source = tmp_path / "src"
    monkeypatch.setattr(runner_module, "_resolve_jiuwenbox_src_dir", lambda: source)
    monkeypatch.setenv("PYTHONPATH", "existing-source")
    monkeypatch.setenv("UNRELATED_TEST_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(subprocess, "run", run)

    assert runner_module.probe_runner_python("runner-python", {"SystemRoot": "C:/Windows"}) is None
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
