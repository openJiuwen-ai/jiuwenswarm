"""Batch port discovery without changing residual-process safeguards."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.sandbox import jiuwenbox_runner as runner

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows port cleanup")


def test_empty_ports_need_only_one_query(monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout=b""))
    monkeypatch.setattr(subprocess, "run", run)
    runner._cleanup_stale_win_proxy_ports()
    run.assert_called_once()
    assert "Get-NetTCPConnection" in run.call_args.args[0][-1]
    assert "60080" in run.call_args.args[0][-1]
    assert "60089" in run.call_args.args[0][-1]
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_batch_groups_ports_and_preserves_unrelated_process(monkeypatch):
    run = Mock(side_effect=[
        SimpleNamespace(stdout=b"60080 123\r\n60081 123\r\n60082 456\r\n59999 789\r\ninvalid\r\n"),
        SimpleNamespace(stdout=b"python.exe\r\n"),
        SimpleNamespace(stdout=b""),
        SimpleNamespace(stdout=b"unrelated-app\r\n"),
    ])
    monkeypatch.setattr(subprocess, "run", run)
    runner._cleanup_stale_win_proxy_ports()
    commands = [call.args[0][-1] for call in run.call_args_list]
    assert len(commands) == 4  # one query, two name lookups, one stop
    assert sum("Get-NetTCPConnection" in command for command in commands) == 1
    assert [command for command in commands if "Stop-Process" in command] == ["Stop-Process -Id 123 -Force"]
    assert not any("789" in command for command in commands)


def test_query_failure_does_not_attempt_process_cleanup(monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired("powershell", 5))
    monkeypatch.setattr(subprocess, "run", run)
    runner._cleanup_stale_win_proxy_ports()
    run.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_runs_off_the_event_loop(monkeypatch, tmp_path):
    import threading
    from unittest.mock import AsyncMock

    owner_thread = threading.get_ident()
    cleanup_threads = []
    monkeypatch.setattr(runner, "_cleanup_stale_win_proxy_ports", lambda: cleanup_threads.append(threading.get_ident()))
    monkeypatch.setattr(runner, "_apply_soft_delete_env", Mock())
    box = runner.JiuwenBoxRunner()
    monkeypatch.setattr(box, "_prepare_windows_setup", AsyncMock(side_effect=RuntimeError("stop after cleanup")))
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    with pytest.raises(RuntimeError, match="stop after cleanup"):
        await box.ensure_running(policy_path=policy)
    assert len(cleanup_threads) == 1
    assert cleanup_threads[0] != owner_thread


@pytest.mark.asyncio
async def test_healthy_owned_service_reuses_without_another_port_query(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    cleanup = Mock()
    monkeypatch.setattr(runner, "_cleanup_stale_win_proxy_ports", cleanup)
    monkeypatch.setattr(runner, "_apply_soft_delete_env", Mock())
    box = runner.JiuwenBoxRunner()
    monkeypatch.setattr(box, "_prepare_windows_setup", AsyncMock())
    monkeypatch.setattr(box, "_wait_until_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(box, "health_check", AsyncMock(return_value=True))
    monkeypatch.setattr(box, "_bind_win_job", Mock())
    monkeypatch.setattr(box, "_register_atexit_once", Mock())
    monkeypatch.setattr(box, "_pump_stream", AsyncMock())
    spawn = AsyncMock(return_value=SimpleNamespace(returncode=None, stdout=None, stderr=None))
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    assert await box.ensure_running(policy_path=policy)
    assert await box.ensure_running(policy_path=policy)
    cleanup.assert_called_once()
    spawn.assert_awaited_once()
    box._prepare_windows_setup.assert_awaited_once()
    await box._stdout_pump_task
    await box._stderr_pump_task
