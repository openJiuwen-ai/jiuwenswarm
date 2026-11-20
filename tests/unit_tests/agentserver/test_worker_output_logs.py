# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound Worker output while preserving inherited handles and native dumps."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from jiuwenswarm.server.supervisor import worker_process as process_module
from jiuwenswarm.server.worker import output_logs


def _stderr(directory: Path, number: int = 0) -> Path:
    return directory / f"runtime-worker-{number:032x}.stderr.log"


def _age(path: Path, seconds: int = 120) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_rotation_keeps_bounded_recent_archives_and_preserves_handle(tmp_path):
    path = _stderr(tmp_path)
    rotation = output_logs.WorkerLogRotation(path, max_bytes=32, backup_count=2)
    with output_logs.open_worker_stderr(path) as stream:
        for number in range(5):
            stream.write(bytes([65 + number]) * 48)
            rotation.rotate()
            assert path.read_bytes() == b""
        # The same handle must append at the new EOF, including on Windows.
        stream.write(b"after rotation\n")
    archives = sorted(tmp_path.glob("*.stderr_*.log"))
    assert [path.read_bytes() for path in archives] == [b"E" * 32, b"E" * 16]
    assert path.read_bytes() == b"after rotation\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_archive_keeps_original_output(tmp_path, monkeypatch):
    path = _stderr(tmp_path)
    path.write_bytes(b"important traceback\n" * 8)
    original = path.read_bytes()
    rotation = output_logs.WorkerLogRotation(path, max_bytes=32, backup_count=2)

    def fail_replace(*_args):
        raise OSError("archive volume is full")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="volume is full"):
        rotation.rotate()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_archive_delete_failure_does_not_stop_rotation(tmp_path, monkeypatch):
    path = _stderr(tmp_path)
    rotation = output_logs.WorkerLogRotation(path, max_bytes=32, backup_count=2)
    path.write_bytes(b'a' * 32)
    rotation.rotate()
    held = next(tmp_path.glob("*.stderr_*.log"))
    original_unlink = Path.unlink

    def deny_held_archive(target, *args, **kwargs):
        if target == held:
            raise PermissionError("archive is open in a viewer")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_held_archive)
    for _ in range(3):
        path.write_bytes(b'b' * 32)
        rotation.rotate()
        assert path.read_bytes() == b""
    assert held.exists()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    path.write_bytes(b'c' * 32)
    rotation.rotate()
    assert len(list(tmp_path.glob("*.stderr_*.log"))) == 2
    assert path.read_bytes() == b""


def test_blocked_log_maintenance_does_not_extend_worker_shutdown(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    rotation = output_logs.WorkerLogRotation(_stderr(tmp_path), max_bytes=32, backup_count=2)

    def blocked_rotate():
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(rotation, "rotate", blocked_rotate)
    rotation.start()
    try:
        assert entered.wait(timeout=2)
        began = time.monotonic()
        rotation.stop()
        assert time.monotonic() - began < 1
    finally:
        release.set()
        rotation._thread.join(timeout=2)
    assert not rotation._thread.is_alive()


def test_retention_bounds_retired_outputs_and_supervisor_dumps(tmp_path, monkeypatch):
    monkeypatch.setattr(output_logs, "_active_log_paths", lambda _directory: set())
    outputs = []
    dumps = []
    for number in range(5):
        path = _stderr(tmp_path, number)
        path.write_bytes(bytes([65 + number]) * 16)
        _age(path, 130 - number)
        outputs.append(path)
        dump = tmp_path / f"supervisor-1234-{number}.txt"
        dump.write_text("diagnostic snapshot", encoding="utf-8")
        _age(dump, 130 - number)
        dumps.append(dump)
    unrelated = tmp_path / "user-notes.log"
    unrelated.write_text("keep", encoding="utf-8")
    legacy_stdout = tmp_path / f"runtime-worker-{'f' * 32}.stdout.log"
    legacy_stdout.write_bytes(b"legacy stdout")
    _age(legacy_stdout, 140)

    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

    assert [path for path in outputs if path.exists()] == outputs[-2:]
    assert [path for path in dumps if path.exists()] == dumps[-2:]
    assert not legacy_stdout.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_retention_enforces_byte_budget_for_old_oversized_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(output_logs, "_active_log_paths", lambda _directory: set())
    oversized = _stderr(tmp_path)
    oversized.write_bytes(b"x" * 1024)
    _age(oversized)
    latest = _stderr(tmp_path, 1)
    latest.write_bytes(b"recent traceback")
    _age(latest, 100)

    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

    assert not oversized.exists()
    assert latest.read_bytes() == b"recent traceback"


def test_unknown_process_access_protects_base_logs_but_prunes_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(output_logs, "_active_log_paths", lambda _directory: None)
    bases = []
    archives = []
    for number in range(4):
        path = _stderr(tmp_path, number)
        path.write_bytes(b"potentially live")
        _age(path)
        bases.append(path)
        archive = path.with_name(f"{path.stem}_20261120_120000_{number}.log")
        archive.write_bytes(b"closed archive")
        _age(archive, 140 - number)
        archives.append(archive)

    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

    assert all(path.exists() for path in bases)
    assert [path for path in archives if path.exists()] == archives[-2:]


def test_retention_protects_another_process_inherited_stderr(tmp_path, monkeypatch):
    active = _stderr(tmp_path)
    retired = [_stderr(tmp_path, number) for number in (1, 2, 3)]
    for path in retired:
        path.write_bytes(b"old")
        _age(path)
    with output_logs.open_worker_stderr(active) as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", "import os; os.write(2,b'active'); input()"],
            stdin=subprocess.PIPE, stderr=stream,
        )
    try:
        deadline = time.monotonic() + 5
        while not active.stat().st_size and time.monotonic() < deadline:
            time.sleep(0.01)
        _age(active, 200)
        child = psutil.Process(process.pid)
        child.info = {"pid": process.pid, "username": child.username()}
        # Inspect real OS handles of the controlled child, independently of
        # unrelated applications' process permissions on the test machine.
        monkeypatch.setattr(psutil, "process_iter", lambda *_args: iter([child]))
        live = output_logs._active_log_paths(tmp_path)
        assert live is not None and os.path.normcase(str(active)) in live

        output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

        assert active.read_bytes() == b"active"
        assert sum(path.exists() for path in retired) == 2
        process.communicate(b"\n", timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["resident", "front_loopback"])
async def test_real_worker_stderr_rotates_without_losing_native_diagnostics(
    tmp_path, monkeypatch, capfd, transport,
):
    script = """
import faulthandler, os, sys, time
from jiuwenswarm.server.worker.output_logs import start_worker_log_rotation
sys.stdin.buffer.read()
rotation = start_worker_log_rotation(max_bytes=4096, backup_count=2, interval=0.01)
assert rotation is not None
print('inherited stdout survives rotation', flush=True)
for number in range(5):
    os.write(2, str(number).encode() * 5000)
    deadline = time.monotonic() + 5
    while rotation.path.stat().st_size and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rotation.path.stat().st_size == 0
faulthandler.enable()
faulthandler.dump_traceback_later(0.05)
time.sleep(0.15)
rotation.stop()
os.write(2, b'native stderr after rotation\\n')
raise RuntimeError('original exception after rotation')
"""
    monkeypatch.setattr(
        process_module, "build_worker_command",
        lambda *_args, **_kwargs: [sys.executable, "-c", script],
    )
    listener = process_module.FrontLoopbackListener() if transport == "front_loopback" else None
    process = process_module.WorkerProcess(
        str(tmp_path / "worker.pipe"), host="127.0.0.1", port=18092,
        ipc_listener=listener,
    )
    try:
        await process.spawn()
        assert await process.wait(timeout=20) == 1
        assert "inherited stdout survives rotation" in capfd.readouterr().out
        archives = list(process.stderr_path.parent.glob("*.stderr_*.log"))
        assert len(archives) == 2
        assert all(path.stat().st_size <= 4096 for path in archives)
        stderr = process.stderr_path.read_bytes()
        assert b"\x00" not in stderr
        assert b"Timeout (" in stderr and b"File " in stderr
        assert b"native stderr after rotation" in stderr
        assert b"RuntimeError: original exception after rotation" in stderr
        assert not list(process.stderr_path.parent.glob("*.stdout.log"))
    finally:
        await process.stop()
        if listener is not None:
            await listener.close()


@pytest.mark.asyncio
async def test_real_worker_entrypoint_rotates_at_application_default_limit(tmp_path, monkeypatch):
    script = """
import asyncio, os, sys
from pathlib import Path
from jiuwenswarm.server.worker import main, stub
from jiuwenswarm.server.worker.output_logs import STDERR_LOG_ENV
async def serve(*args, **kwargs):
    os.write(2, b'x' * (20 * 1024 * 1024 + 1024))
    path = Path(os.environ[STDERR_LOG_ENV])
    deadline = asyncio.get_running_loop().time() + 10
    while path.stat().st_size and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert path.stat().st_size == 0
    os.write(2, b'default rotation completed\\n')
stub.serve_stub = serve
sys.argv = ['worker', '--socket', 'unused.pipe', '--stub', *sys.argv[1:]]
main.main()
"""
    monkeypatch.setattr(
        process_module, "build_worker_command",
        lambda *_args, **_kwargs: [sys.executable, "-c", script],
    )
    process = process_module.WorkerProcess(str(tmp_path / "worker.pipe"), host="127.0.0.1", port=18092)
    try:
        await process.spawn()
        result = await process.wait(timeout=25)
        assert result == 0, process.stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        archives = list(process.stderr_path.parent.glob("*.stderr_*.log"))
        assert len(archives) == 2
        assert max(path.stat().st_size for path in archives) == 20 * 1024 * 1024
        assert all(path.stat().st_size <= 20 * 1024 * 1024 for path in archives)
        assert b'default rotation completed' in process.stderr_path.read_bytes()
    finally:
        await process.stop()


def test_rotation_continues_after_resident_front_process_exits(tmp_path):
    child_script = """
import json, os, sys, time
from pathlib import Path
from jiuwenswarm.server.worker.output_logs import start_worker_log_rotation
sys.stdin.buffer.read()
rotation = start_worker_log_rotation(max_bytes=4096, backup_count=2, interval=0.01)
assert rotation is not None
for number in range(8):
    os.write(2, b'x' * 5000)
    time.sleep(0.1)
rotation.stop()
os.write(2, b'completed after Front exit\\n')
Path(sys.argv[1]).write_text('done', encoding='utf-8')
"""
    info = tmp_path / "worker.json"
    done = tmp_path / "done.txt"
    front_script = """
import asyncio, json, sys
from pathlib import Path
from jiuwenswarm.server.supervisor import worker_process as module
async def run():
    module.build_worker_command = lambda *args, **kwargs: [sys.executable, '-c', sys.argv[1], sys.argv[3]]
    process = module.WorkerProcess(str(Path(sys.argv[2]).with_suffix('.pipe')), host='127.0.0.1', port=18092)
    await process.spawn()
    Path(sys.argv[2]).write_text(json.dumps({'pid': process.pid, 'stderr': str(process.stderr_path)}), encoding='utf-8')
asyncio.run(run())
"""
    front = subprocess.Popen(
        [sys.executable, "-c", front_script, child_script, str(info), str(done)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    child = None
    try:
        _, error = front.communicate(timeout=20)
        assert front.returncode == 0, error.decode(errors="replace")
        state = json.loads(info.read_text(encoding="utf-8"))
        try:
            child = psutil.Process(state["pid"])
        except psutil.NoSuchProcess:
            pass
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert done.read_text(encoding="utf-8") == "done"
        if child is not None:
            child.wait(timeout=5)
        path = Path(state["stderr"])
        archives = list(path.parent.glob("*.stderr_*.log"))
        assert len(archives) == 2
        assert all(item.stat().st_size <= 4096 for item in archives)
        assert path.read_bytes() == b"completed after Front exit\n"
    finally:
        if front.poll() is None:
            front.kill()
        front.wait(timeout=5)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=5)
