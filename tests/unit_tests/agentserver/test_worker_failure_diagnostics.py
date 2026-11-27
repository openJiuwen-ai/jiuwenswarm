# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Worker failures remain diagnosable when startup closes IPC before HELLO."""

import asyncio
import builtins
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import worker_process
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker import main as worker_main
from jiuwenswarm.server.worker.diagnostics import write_worker_failure


def test_same_failure_is_written_once(monkeypatch, tmp_path):
    target = tmp_path / "latest-worker-error.log"
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))
    failure = RuntimeError("repeated failure probe")

    write_worker_failure(failure)
    write_worker_failure(failure, path=target)

    assert target.read_text(encoding="utf-8").count("stage=worker failure=RuntimeError:") == 1


@pytest.mark.parametrize("scope", ["stage", "target", "exception"])
def test_distinct_failure_records_are_preserved(tmp_path, scope):
    target = tmp_path / "latest-worker-error.log"
    second_target = tmp_path / "other-worker-error.log" if scope == "target" else target
    failure = RuntimeError("same message probe")
    second_failure = RuntimeError(str(failure)) if scope == "exception" else failure
    second_stage = "HELLO" if scope == "stage" else "worker"

    write_worker_failure(failure, path=target)
    write_worker_failure(second_failure, stage=second_stage, path=second_target)

    texts = [path.read_text(encoding="utf-8") for path in {target, second_target}]
    assert sum(text.count("failure=RuntimeError: same message probe") for text in texts) == 2
    if scope == "stage":
        assert "stage=HELLO" in texts[0] and "stage=worker" in texts[0]


def test_failed_diagnostic_write_can_be_retried(tmp_path):
    blocked = tmp_path / "diagnostic"
    blocked.write_text("not a directory", encoding="utf-8")
    target = blocked / "latest-worker-error.log"
    failure = RuntimeError("retry diagnostic probe")

    write_worker_failure(failure, path=target)
    blocked.unlink()
    write_worker_failure(failure, path=target)
    write_worker_failure(failure, path=target)

    assert target.read_text(encoding="utf-8").count("stage=worker failure=RuntimeError:") == 1


@pytest.fixture
def diagnostic_log(monkeypatch, tmp_path):
    target = tmp_path / "diagnostic/latest-worker-error.log"
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))
    handler = worker_main._configure_worker_diagnostic_logging()
    assert handler is not None
    try:
        yield target
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger("jiuwenswarm").removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("setting", [None, "", "configured"])
def test_log_directory_routing(monkeypatch, tmp_path, setting):
    if setting is None:
        monkeypatch.delenv("JIUWEN_WORKER_LOG_DIR", raising=False)
    else:
        value = str(tmp_path / "configured") if setting else ""
        monkeypatch.setenv("JIUWEN_WORKER_LOG_DIR", value)
    socket = tmp_path / "custom ipc" / "worker"
    process = worker_process.WorkerProcess(str(socket), host="127.0.0.1", port=0)
    expected = tmp_path / "configured" if setting else socket.parent / "worker-logs"
    assert process.stderr_path.parent == expected
    assert process.diagnostic_path == expected / "latest-worker-error.log"
    assert process.stderr_path.name.startswith("runtime-worker-")
    assert process.stderr_path.name.endswith(".stderr.log")


def test_error_records_include_package_and_asyncio_failures(diagnostic_log):
    logger = logging.getLogger("jiuwenswarm.server.worker.test")
    logger.info("normal info is not an error diagnostic")
    try:
        raise ValueError("worker error log probe")
    except ValueError:
        logger.exception("Worker error recorded")
    logging.getLogger("asyncio").error("unhandled task failure recorded")
    text = diagnostic_log.read_text(encoding="utf-8")
    assert "normal info is not an error diagnostic" not in text
    assert "Worker error recorded" in text and "ValueError: worker error log probe" in text
    assert "Traceback" in text and f"pid={os.getpid()}" in text
    assert "unhandled task failure recorded" in text


@pytest.mark.parametrize("blocked", [False, True])
def test_top_level_failure_keeps_original_exception(monkeypatch, tmp_path, blocked):
    target = tmp_path / "diagnostic/latest-worker-error.log"
    if blocked:
        target.parent.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))
    original = RuntimeError("failure before Worker logging setup")
    monkeypatch.setattr(worker_main, "_run_worker", Mock(side_effect=original))
    with pytest.raises(RuntimeError) as caught:
        worker_main.main()
    assert caught.value is original
    if not blocked:
        text = target.read_text(encoding="utf-8")
        assert "failure before Worker logging setup" in text
        assert "Traceback" in text and f"pid={os.getpid()}" in text


def test_bootstrap_import_failure_is_recorded(monkeypatch, tmp_path):
    target = tmp_path / "latest-worker-error.log"
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))
    real_import = builtins.__import__

    def import_with_bootstrap_failure(name, *args, **kwargs):
        if name == "jiuwenswarm.common.utils":
            raise ModuleNotFoundError("injected bootstrap dependency failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_bootstrap_failure)
    with pytest.raises(ModuleNotFoundError, match="bootstrap dependency failure"):
        worker_main.main()
    assert "ModuleNotFoundError" in target.read_text(encoding="utf-8")


def test_unwritable_error_handler_is_optional(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(blocked / "error.log"))
    assert worker_main._configure_worker_diagnostic_logging() is None


@pytest.mark.asyncio
async def test_failed_hello_is_persisted_before_connection_closes(monkeypatch, tmp_path):
    from jiuwenswarm.server.worker import service

    target = tmp_path / "latest-worker-error.log"
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))

    async def close_after_diagnostic():
        assert "stage=HELLO" in target.read_text(encoding="utf-8")

    connection = SimpleNamespace(close=AsyncMock(side_effect=close_after_diagnostic))
    source = SimpleNamespace(
        start=AsyncMock(), accept=AsyncMock(return_value=connection),
        close=AsyncMock(), generation=1, reconnectable=False,
    )
    monkeypatch.setattr(service, "_send_hello", AsyncMock(side_effect=RuntimeError("HELLO send probe")))
    monkeypatch.setattr(service, "_close_worker", AsyncMock())
    await service.serve_worker(
        "unused", host="127.0.0.1", port=0,
        code_identity_value="a" * 64, session_source=source,
    )
    text = target.read_text(encoding="utf-8")
    assert "stage=HELLO" in text and "RuntimeError: HELLO send probe" in text
    assert "Traceback" in text and f"pid={os.getpid()}" in text


@pytest.mark.asyncio
async def test_stub_hello_failure_is_persisted_before_unwinding(monkeypatch, tmp_path):
    from jiuwenswarm.common import process_identity
    from jiuwenswarm.server.worker import stub

    target = tmp_path / "latest-worker-error.log"
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(target))
    failure = psutil.AccessDenied(pid=os.getpid(), msg="stub HELLO diagnostic probe")
    monkeypatch.setattr(process_identity, "process_identity_fields", Mock(side_effect=failure))
    with pytest.raises(RuntimeError, match="process identity failed"):
        await stub._serve_one(
            object(), generation=1, ready_delay=0, exit_on_request=False,
            runtime_loaded=False, instance="diagnostic", code_identity_value="a" * 64,
        )
    text = target.read_text(encoding="utf-8")
    assert "stage=HELLO" in text and "stub HELLO diagnostic probe" in text
    assert "Traceback" in text and f"pid={os.getpid()}" in text


def test_supervisor_reports_only_bounded_diagnostic_tail(tmp_path, caplog):
    target = tmp_path / "latest-worker-error.log"
    target.write_bytes(b"OLD_PREFIX" + b"x" * (32 * 1024) + b"\nRuntimeError: bounded tail probe\n")
    process = SimpleNamespace(pid=123, diagnostic_path=target)
    RuntimeSupervisor._log_worker_startup_diagnostic(process)
    assert "OLD_PREFIX" not in caplog.text
    assert "RuntimeError: bounded tail probe" in caplog.text
    assert str(target) in caplog.text
    record = next(r for r in caplog.records if "startup diagnostic path=" in r.getMessage())
    assert len(record.getMessage()) < 17 * 1024


def test_missing_diagnostic_does_not_mask_startup_failure(tmp_path, caplog):
    process = SimpleNamespace(pid=123, diagnostic_path=tmp_path / "missing.log")
    RuntimeSupervisor._log_worker_startup_diagnostic(process)
    assert "diagnostic unavailable" in caplog.text
    assert "missing.log" in caplog.text


_FAILURE_SCRIPT = (
    "import logging, sys\n"
    "sys.stdin.buffer.read()\n"
    "from jiuwenswarm.server.worker.diagnostics import write_worker_failure\n"
    "print('stdout remains inherited', flush=True)\n"
    "logging.basicConfig(level=logging.INFO)\n"
    "logging.getLogger('common').info('ordinary stderr info probe')\n"
    "try:\n"
    "    raise RuntimeError('real child startup diagnostic probe')\n"
    "except RuntimeError as exc:\n"
    "    write_worker_failure(exc)\n"
    "raise SystemExit(1)\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, "", "configured"])
async def test_spawn_routes_paths_resets_stale_error_and_inherits_stdout(monkeypatch, tmp_path, capfd, setting):
    socket = tmp_path / "custom ipc" / "worker"
    directory = tmp_path / "configured logs" if setting else socket.parent / "worker-logs"
    directory.mkdir(parents=True)
    target = directory / "latest-worker-error.log"
    target.write_text("STALE_ERROR_DO_NOT_REUSE", encoding="utf-8")
    wrong_target = tmp_path / "parent-diagnostic.log"
    if setting is None:
        monkeypatch.delenv("JIUWEN_WORKER_LOG_DIR", raising=False)
    else:
        monkeypatch.setenv("JIUWEN_WORKER_LOG_DIR", str(directory) if setting else "")
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(wrong_target))
    monkeypatch.setattr(
        worker_process, "build_worker_command",
        lambda *args, **kwargs: [sys.executable, "-c", _FAILURE_SCRIPT],
    )
    process = worker_process.WorkerProcess(str(socket), host="127.0.0.1", port=0)
    try:
        await process.spawn()
        assert await process.wait(timeout=8) == 1
        assert "stdout remains inherited" in capfd.readouterr().out
        assert "ordinary stderr info probe" in process.stderr_path.read_text(encoding="utf-8")
        text = target.read_text(encoding="utf-8")
        assert "STALE_ERROR_DO_NOT_REUSE" not in text
        assert "Traceback" in text and "real child startup diagnostic probe" in text
        assert not wrong_target.exists()
        assert not list(directory.glob("*.stdout.log"))
    finally:
        await process.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resident", "front_loopback"])
@pytest.mark.parametrize("setting", [None, "", "configured"])
async def test_log_rotation_preserves_failure_diagnostic(monkeypatch, tmp_path, mode, setting):
    script = """
import os, sys, time
from pathlib import Path
from jiuwenswarm.server.worker.diagnostics import write_worker_failure
from jiuwenswarm.server.worker.output_logs import prune_worker_logs, start_worker_log_rotation
sys.stdin.buffer.read()
diagnostic = Path(os.environ['JIUWEN_WORKER_DIAGNOSTIC_PATH'])
assert not diagnostic.exists(), 'stale diagnostic was not cleared'
rotation = start_worker_log_rotation(max_bytes=1024, backup_count=2, interval=0.01)
assert rotation is not None, 'stderr configuration did not reach the child'
assert rotation.path.parent == diagnostic.parent
try:
    os.write(2, b'x' * 2048)
    deadline = time.monotonic() + 5
    while rotation.path.stat().st_size >= rotation.max_bytes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rotation.path.stat().st_size < rotation.max_bytes
finally:
    rotation.stop()
write_worker_failure(RuntimeError('diagnostic after rotation'))
stamp = time.time() - 120
os.utime(diagnostic, (stamp, stamp))
prune_worker_logs(rotation.path.parent, max_bytes=1024, backup_count=1, protected=rotation.path)
assert 'diagnostic after rotation' in diagnostic.read_text(encoding='utf-8')
"""
    socket = tmp_path / "custom ipc" / "worker"
    directory = tmp_path / "configured logs" if setting else socket.parent / "worker-logs"
    directory.mkdir(parents=True)
    diagnostic = directory / "latest-worker-error.log"
    diagnostic.write_text("STALE_ERROR_DO_NOT_REUSE", encoding="utf-8")
    if setting is None:
        monkeypatch.delenv("JIUWEN_WORKER_LOG_DIR", raising=False)
    else:
        monkeypatch.setenv("JIUWEN_WORKER_LOG_DIR", str(directory) if setting else "")
    monkeypatch.setenv("JIUWEN_WORKER_DIAGNOSTIC_PATH", str(tmp_path / "wrong-diagnostic.log"))
    monkeypatch.setenv("JIUWENSWARM_WORKER_STDERR_LOG", str(tmp_path / "wrong-stderr.log"))
    monkeypatch.setattr(
        worker_process, "build_worker_command",
        lambda *args, **kwargs: [sys.executable, "-c", script],
    )
    listener = worker_process.FrontLoopbackListener() if mode == "front_loopback" else None
    process = worker_process.WorkerProcess(
        str(socket), host="127.0.0.1", port=0, ipc_listener=listener,
    )
    try:
        await process.spawn()
        assert await process.wait(timeout=10) == 0, process.stderr_path.read_text(encoding="utf-8", errors="replace")
        assert process.stderr_path.parent == directory
        assert list(directory.glob("*.stderr_*.log"))
        text = diagnostic.read_text(encoding="utf-8")
        assert "diagnostic after rotation" in text
        assert "STALE_ERROR_DO_NOT_REUSE" not in text
        assert not (tmp_path / "wrong-diagnostic.log").exists()
        assert not (tmp_path / "wrong-stderr.log").exists()
    finally:
        await process.stop()
        if listener is not None:
            await listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resident", "front_loopback"])
async def test_real_failed_bringup_reports_diagnostic_after_cleanup(monkeypatch, tmp_path, caplog, capfd, mode):
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", mode)
    monkeypatch.setenv("JIUWEN_WORKER_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        worker_process, "build_worker_command",
        lambda *args, **kwargs: [sys.executable, "-c", _FAILURE_SCRIPT],
    )
    supervisor = RuntimeSupervisor(
        Readiness(), socket_path=str(tmp_path / "worker"),
        stub=True, max_restarts=0, connect_timeout=2,
    )
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert supervisor._process is None
        assert "Worker startup diagnostic path=" in caplog.text
        assert "RuntimeError: real child startup diagnostic probe" in caplog.text
        assert "stdout remains inherited" in capfd.readouterr().out
        assert "[Worker stderr pid=" not in caplog.text
        assert "[Worker stdout pid=" not in caplog.text
    finally:
        await supervisor.stop(terminate_worker=True)
