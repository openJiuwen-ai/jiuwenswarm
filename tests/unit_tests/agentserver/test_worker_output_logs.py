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
from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.server.supervisor import worker_process as process_module
from jiuwenswarm.server.worker import output_logs


@pytest.fixture(autouse=True)
def _isolated_worker_data(tmp_path, monkeypatch):
    from jiuwenswarm.common import paths

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setattr(paths, "_workspace_base_dir", None)


def test_worker_stderr_follows_overridden_socket_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("JIUWEN_WORKER_LOG_DIR", raising=False)
    owner = "a" * 32
    monkeypatch.setattr(output_logs, "_local_log_owner", lambda: owner)
    socket = tmp_path / "custom-ipc" / "worker.pipe"

    process = process_module.WorkerProcess(str(socket), host="127.0.0.1", port=18092)

    assert process.stderr_path.parent == socket.parent / "worker-logs"
    assert process.diagnostic_path.parent == process.stderr_path.parent
    assert output_logs._LOG_NAME.fullmatch(process.stderr_path.name).group("owner") == owner


def _stderr(directory: Path, number: int = 0) -> Path:
    owner = output_logs._local_log_owner() or "a" * 32
    return directory / f"runtime-worker-{number:032x}-{owner}.stderr.log"


def _age(path: Path, seconds: int = 120) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _process_exited(process: psutil.Process) -> bool:
    """Orphaned zombies have exited even when the container init has not reaped them."""
    try:
        return not process.is_running() or process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return True


def _wait_for_process_exit(process: psutil.Process, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not _process_exited(process):
        if time.monotonic() >= deadline:
            raise psutil.TimeoutExpired(timeout, pid=process.pid)
        time.sleep(0.01)


@pytest.mark.parametrize("status", [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD])
def test_orphan_exit_wait_accepts_terminated_process_records(status):
    process = SimpleNamespace(pid=1, is_running=lambda: True, status=lambda: status)
    _wait_for_process_exit(process, timeout=0)


def test_orphan_exit_wait_does_not_accept_a_live_worker():
    process = SimpleNamespace(pid=1, is_running=lambda: True, status=lambda: psutil.STATUS_SLEEPING)
    with pytest.raises(psutil.TimeoutExpired):
        _wait_for_process_exit(process, timeout=0)


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


@pytest.mark.parametrize("repeats", [6, 100])
def test_utf8_archives_keep_complete_characters_and_recent_bytes(tmp_path, repeats):
    path = _stderr(tmp_path)
    payload = ("中🙂文" * repeats).encode("utf-8")
    path.write_bytes(payload)
    rotation = output_logs.WorkerLogRotation(path, max_bytes=32, backup_count=2)
    rotation.rotate()
    archives = sorted(tmp_path.glob("*.stderr_*.log"))
    assert len(archives) == 2
    content = b""
    for archive in archives:
        raw = archive.read_bytes()
        assert 0 < len(raw) <= 32
        assert raw.decode("utf-8").encode("utf-8") == raw
        content += raw
    assert content == payload[-len(content):]
    if repeats == 6:
        assert content == payload
    assert path.read_bytes() == b""


def test_archive_names_do_not_overwrite_when_clock_ticks_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(output_logs.time, "time_ns", lambda: 123)
    path = _stderr(tmp_path)
    payload = b"a" * 16 + b"b" * 16 + b"c" * 16
    path.write_bytes(payload)
    rotation = output_logs.WorkerLogRotation(path, max_bytes=16, backup_count=4)
    rotation.rotate()
    path.write_bytes(b"d" * 16)
    rotation.rotate()
    archives = sorted(tmp_path.glob("*.stderr_*.log"))
    assert len(archives) == 4
    assert b"".join(archive.read_bytes() for archive in archives) == payload + b"d" * 16
    assert path.read_bytes() == b""


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
    legacy_stdout = _stderr(tmp_path, 99)
    legacy_stdout = legacy_stdout.with_name(legacy_stdout.name.replace(".stderr.", ".stdout."))
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


@pytest.mark.parametrize("partial_copies", [1, 8])
def test_retention_prunes_closed_temporary_files_left_by_interrupted_rotation(tmp_path, monkeypatch, partial_copies):
    monkeypatch.setattr(output_logs, "_active_log_paths", lambda _directory: set())
    orphan = _stderr(tmp_path, 99)
    orphan = orphan.with_name(f"{orphan.stem}_20261120_120000_123.tmp")
    orphan.write_bytes(b"partial archive" * partial_copies)
    _age(orphan, 100)
    retained = _stderr(tmp_path)
    retained.write_bytes(b"recent output")
    _age(retained, 200)
    unrelated = tmp_path / "unrelated.tmp"
    unrelated.write_bytes(b"keep")
    _age(unrelated)
    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
    assert not orphan.exists()
    assert retained.read_bytes() == b"recent output"
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows temporary-file sharing")
def test_retention_protects_an_open_temporary_archive(tmp_path):
    active = tmp_path / f"runtime-worker-{'a' * 32}.stderr_20261120_120000_123.tmp"
    with active.open("wb") as stream:
        stream.write(b"incomplete" * 16)
        stream.flush()
        _age(active, 200)
        output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
        assert active.exists()
        stream.write(b" continues")
    assert active.read_bytes().endswith(b" continues")


def test_unknown_process_access_protects_base_logs_but_prunes_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(output_logs, "_active_log_paths", lambda _directory: None)
    monkeypatch.setattr(output_logs, "_windows_log_is_closed", lambda _path: False)
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


@pytest.mark.parametrize("inherited_handle", ["legacy", "append_only"])
def test_retention_protects_another_process_inherited_stderr(tmp_path, monkeypatch, inherited_handle):
    active = _stderr(tmp_path)
    retired = [_stderr(tmp_path, number) for number in (1, 2, 3)]
    for path in retired:
        path.write_bytes(b"old")
        _age(path)
    opener = output_logs.open_worker_stderr if inherited_handle == "append_only" else lambda path: path.open("ab")
    with opener(active) as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", "import os; os.write(2,b'active'); input()"],
            stdin=subprocess.PIPE, stderr=stream,
        )
    try:
        deadline = time.monotonic() + 5
        while not active.stat().st_size and time.monotonic() < deadline:
            time.sleep(0.01)
        _age(active, 200)
        if os.name != "nt":
            child = psutil.Process(process.pid)
            child.info = {"pid": process.pid, "uids": child.uids()}
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


def _posix_processes(monkeypatch, processes):
    # Execute POSIX retention on any test host without changing pathlib's OS.
    owner = output_logs._local_log_owner() or "a" * 32
    monkeypatch.setattr(output_logs, "_local_log_owner", lambda: owner)
    monkeypatch.setattr(output_logs, "os", SimpleNamespace(name="posix", path=os.path, geteuid=lambda: 1000))
    monkeypatch.setattr(psutil, "process_iter", lambda *_args: iter(processes))


def test_shared_volume_retention_keeps_foreign_and_unattributed_mutable_files(tmp_path, monkeypatch):
    retired = [_stderr(tmp_path, number) for number in range(5)]
    for number, path in enumerate(retired):
        path.write_bytes(b"closed local output")
        _age(path, 130 - number)
    owner = output_logs._local_log_owner() or "a" * 32
    foreign = "b" * 32 if owner != "b" * 32 else "c" * 32
    protected = []
    for suffix in (f"-{foreign}", ""):
        base = tmp_path / f"runtime-worker-{'f' * 32}{suffix}.stderr.log"
        temporary = base.with_name(f"{base.stem}_20261120_120000_123.tmp")
        for path in (base, temporary):
            path.write_bytes(b"remote Worker may still be writing")
            _age(path, 300)
            protected.append(path)
    archive = protected[0].with_name(f"{protected[0].stem}_20261120_120000_123.log")
    archive.write_bytes(b"completed foreign archive")
    _age(archive, 200)
    _posix_processes(monkeypatch, [])

    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

    assert all(path.read_bytes() == b"remote Worker may still be writing" for path in protected)
    assert [path for path in retired if path.exists()] == retired[-2:]
    assert not archive.exists()


def test_missing_local_ownership_keeps_mutable_files_but_prunes_archives(tmp_path, monkeypatch):
    bases = [_stderr(tmp_path, number) for number in range(3)]
    for path in bases:
        path.write_bytes(b"possibly remote")
        _age(path)
    temporary = bases[0].with_name(f"{bases[0].stem}_20261120_120000_123.tmp")
    temporary.write_bytes(b"possibly copying")
    _age(temporary)
    archives = [base.with_name(f"{base.stem}_20261120_120000_123.log") for base in bases]
    for number, path in enumerate(archives):
        path.write_bytes(b"closed")
        _age(path, 200 - number)
    _posix_processes(monkeypatch, [])
    monkeypatch.setattr(output_logs, "_local_log_owner", lambda: None)

    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)

    assert all(path.exists() for path in bases)
    assert temporary.exists()
    assert [path for path in archives if path.exists()] == archives[-2:]


def _linux_owner_sources(monkeypatch):
    sources = {
        "boot": "12345678-1234-1234-1234-123456789abc", "pid": 10, "user": 20, "start": 100, "uid": 1000,
    }

    def proc_path(name):
        if name.endswith("boot_id"):
            return SimpleNamespace(read_text=lambda **_kwargs: sources["boot"])
        if name.endswith("/stat"):
            return SimpleNamespace(
                read_text=lambda **_kwargs: "1 (init (with spaces)) S " + "0 " * 18 + str(sources["start"]),
            )
        namespace = "pid" if name.endswith("/pid") else "user"
        return SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=1, st_ino=sources[namespace]))

    monkeypatch.setattr(output_logs, "Path", proc_path)
    monkeypatch.setattr(output_logs, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(output_logs, "os", SimpleNamespace(name="posix", geteuid=lambda: sources["uid"]))
    return sources


@pytest.mark.parametrize("identity", ["boot", "pid", "user", "start", "uid"])
def test_linux_owner_is_stable_and_distinguishes_process_views(monkeypatch, identity):
    sources = _linux_owner_sources(monkeypatch)
    owner = output_logs._local_log_owner()
    assert owner is not None and len(owner) == 32
    assert output_logs._local_log_owner() == owner
    sources[identity] = "87654321-1234-1234-1234-123456789abc" if identity == "boot" else sources[identity] + 1
    assert output_logs._local_log_owner() != owner


def test_missing_kernel_ownership_does_not_fall_back_to_hostname(monkeypatch, caplog):
    sources = _linux_owner_sources(monkeypatch)
    sources["boot"] = "not a boot UUID"
    monkeypatch.setenv("HOSTNAME", "same-name-on-both-hosts")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_INSTANCE", "same-instance")
    assert output_logs._local_log_owner() is None
    assert "ownership unavailable" in caplog.text
    assert output_logs.worker_stderr_name("0" * 32) == f"runtime-worker-{'0' * 32}.stderr.log"


def test_darwin_owner_uses_boot_session_and_uid(monkeypatch):
    monkeypatch.setattr(output_logs, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000))
    monkeypatch.setattr(output_logs, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(output_logs, "_darwin_boot_id", lambda: "a" * 32)
    owner = output_logs._local_log_owner()
    assert owner is not None and output_logs._local_log_owner() == owner
    assert output_logs.worker_stderr_name("0" * 32) == f"runtime-worker-{'0' * 32}-{owner}.stderr.log"
    monkeypatch.setattr(output_logs, "_darwin_boot_id", lambda: "b" * 32)
    assert output_logs._local_log_owner() != owner


@pytest.mark.parametrize("failure", ["unavailable", "invalid_name", "mismatch", "no_fileno", "closed", "missing"])
def test_configured_rotation_validation_failure_is_observable(tmp_path, monkeypatch, caplog, failure):
    path = _stderr(tmp_path)
    path.write_bytes(b"keep original stderr")
    monkeypatch.setattr(output_logs, "_rotation", None)
    monkeypatch.setenv(output_logs.STDERR_LOG_ENV, str(path))
    with path.open("ab") as stream, (tmp_path / "another.log").open("ab") as other:
        stderr = stream
        if failure == "unavailable":
            stderr = None
        elif failure == "invalid_name":
            monkeypatch.setenv(output_logs.STDERR_LOG_ENV, str(other.name))
        elif failure == "mismatch":
            stderr = other
        elif failure == "no_fileno":
            stderr = object()
        elif failure == "closed":
            stream.close()
        else:
            monkeypatch.setenv(output_logs.STDERR_LOG_ENV, str(_stderr(tmp_path, 999)))
        monkeypatch.setattr(output_logs, "sys", SimpleNamespace(stderr=stderr))
        assert output_logs.start_worker_log_rotation() is None
    assert output_logs._rotation is None
    assert "output log rotation could not start" in caplog.text


def test_rotation_without_parent_configuration_is_silent(monkeypatch, caplog):
    monkeypatch.setattr(output_logs, "_rotation", None)
    monkeypatch.delenv(output_logs.STDERR_LOG_ENV, raising=False)
    assert output_logs.start_worker_log_rotation() is None
    assert not caplog.records


def test_posix_retention_ignores_unresolvable_names_and_protects_inherited_output(tmp_path, monkeypatch):
    active = _stderr(tmp_path, 10)
    active.write_bytes(b"active output")
    _age(active, 200)
    retired = [_stderr(tmp_path, number) for number in range(5)]
    for number, path in enumerate(retired):
        path.write_bytes(b"closed output")
        _age(path, 150-number)

    def forbidden_files():
        raise psutil.AccessDenied(10)

    foreign = SimpleNamespace(pid=10, info={"uids": (2000, 2000, 2000), "username": None}, open_files=forbidden_files)
    own = SimpleNamespace(
        pid=11, info={"uids": (1000, 1000, 1000), "username": None},
        open_files=lambda: [SimpleNamespace(path=str(active))],
    )
    _posix_processes(monkeypatch, [foreign, own])
    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
    assert active.read_bytes() == b"active output"
    assert [path for path in retired if path.exists()] == retired[-2:]


@pytest.mark.parametrize("uids", [(1000, 2000, 2000), (2000, 1000, 2000), (2000, 2000, 1000)])
def test_posix_retention_protects_real_effective_and_saved_user_handles(tmp_path, monkeypatch, uids):
    active = _stderr(tmp_path)
    active.write_bytes(b"inherited output")
    own = SimpleNamespace(pid=11, info={"uids": uids}, open_files=lambda: [SimpleNamespace(path=str(active))])
    _posix_processes(monkeypatch, [own])
    assert output_logs._active_log_paths(tmp_path) == {os.path.normcase(str(active))}


@pytest.mark.parametrize("failure", [psutil.NoSuchProcess, psutil.ZombieProcess])
def test_posix_retention_skips_exited_process_with_unavailable_cached_uid(tmp_path, monkeypatch, failure):
    def vanished_uid():
        raise failure(10)

    vanished = SimpleNamespace(pid=10, info={"uids": None}, uids=vanished_uid)
    _posix_processes(monkeypatch, [vanished])
    assert output_logs._active_log_paths(tmp_path) == set()


@pytest.mark.parametrize("known_uid", [True, False])
def test_posix_retention_keeps_base_logs_when_process_access_is_unknown(tmp_path, monkeypatch, caplog, known_uid):
    def denied():
        raise psutil.AccessDenied(10)

    uncertain = SimpleNamespace(
        pid=10, info={"uids": (1000, 1000, 1000) if known_uid else None},
        uids=denied, open_files=denied,
    )
    bases = [_stderr(tmp_path, number) for number in range(3)]
    for path in bases:
        path.write_bytes(b"possibly active")
        _age(path)
    _posix_processes(monkeypatch, [uncertain])
    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
    assert all(path.exists() for path in bases)
    assert "process access denied (pid=10)" in caplog.text


def test_posix_retention_timeout_does_not_delete_unscanned_active_output(tmp_path, monkeypatch, caplog):
    bases = [_stderr(tmp_path, number) for number in range(3)]
    for path in bases:
        path.write_bytes(b"possibly active")
        _age(path)
    own = [
        SimpleNamespace(pid=10+number, info={"uids": (1000, 1000, 1000)}, open_files=lambda: [])
        for number in range(2)
    ]
    _posix_processes(monkeypatch, own)
    clock = iter([0.0, 0.0, 3.0])
    monkeypatch.setattr(output_logs, "time", SimpleNamespace(monotonic=lambda: next(clock), time=time.time))
    output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
    assert all(path.exists() for path in bases)
    assert "process scan timed out" in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing protection")
def test_windows_closed_logs_prune_without_system_process_access(tmp_path, monkeypatch):
    def inaccessible_processes(_directory):
        raise AssertionError("Windows retention must not inspect system processes")

    monkeypatch.setattr(output_logs, "_active_log_paths", inaccessible_processes)
    live = [_stderr(tmp_path, 10), _stderr(tmp_path, 11)]
    retired = [_stderr(tmp_path, number) for number in range(5)]
    for number, path in enumerate(retired):
        path.write_bytes(b"closed traceback")
        _age(path, 140-number)
    with live[0].open("ab") as legacy, output_logs.open_worker_stderr(live[1]) as current:
        legacy.write(b"legacy\n")
        legacy.flush()
        current.write(b"current\n")
        for path in live:
            _age(path, 200)
            assert not output_logs._windows_log_is_closed(path)
        output_logs.prune_worker_logs(tmp_path, max_bytes=32, backup_count=2)
        assert [path for path in retired if path.exists()] == retired[-2:]
        assert all(path.exists() for path in live)
        legacy.write(b"after\n")
        current.write(b"after\n")
    assert live[0].read_bytes() == b"legacy\nafter\n"
    assert live[1].read_bytes() == b"current\nafter\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle lifecycle")
def test_windows_sharing_probe_does_not_leak_handles(tmp_path):
    closed = _stderr(tmp_path)
    live = _stderr(tmp_path, 1)
    closed.write_bytes(b"closed")
    assert output_logs._windows_log_is_closed(closed)
    with output_logs.open_worker_stderr(live):
        process = psutil.Process()
        before = process.num_handles()
        for _ in range(1000):
            assert output_logs._windows_log_is_closed(closed)
            assert not output_logs._windows_log_is_closed(live)
        assert process.num_handles() <= before + 4


@pytest.mark.asyncio
async def test_slow_retention_does_not_delay_worker_spawn(tmp_path, monkeypatch):
    from jiuwenswarm.server.supervisor import identity

    entered = threading.Event()
    release = threading.Event()

    def slow_retention(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(output_logs, "prune_worker_logs", slow_retention)
    monkeypatch.setattr(identity, "code_identity", lambda: "0" * 64)
    monkeypatch.setattr(
        process_module, "build_worker_command",
        lambda *_args, **_kwargs: [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
    )
    process = process_module.WorkerProcess(str(tmp_path / "worker.pipe"), host="127.0.0.1", port=18092)
    try:
        await process.spawn()
        assert not entered.is_set()
        assert await process.wait(timeout=5) == 0
    finally:
        release.set()
        await process.stop()


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
    while rotation.path.stat().st_size >= rotation.max_bytes and time.monotonic() < deadline:
        time.sleep(0.01)
    if number == 0:
        os.write(2, b'maintenance diagnostic after rotation\\n')
    assert rotation.path.stat().st_size < rotation.max_bytes
rotation.stop()
faulthandler.enable()
faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
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
        assert b"Current thread" in stderr and b"File " in stderr
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
    while path.stat().st_size >= 20 * 1024 * 1024 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert path.stat().st_size < 20 * 1024 * 1024
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
gate = Path(sys.argv[2])
deadline = time.monotonic() + 20
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert gate.exists(), 'Front exit was not confirmed'
for number in range(8):
    os.write(2, b'x' * 5000)
    deadline = time.monotonic() + 5
    while rotation.path.stat().st_size >= rotation.max_bytes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rotation.path.stat().st_size < rotation.max_bytes
rotation.stop()
os.write(2, b'completed after Front exit\\n')
Path(sys.argv[1]).write_text('done', encoding='utf-8')
"""
    info = tmp_path / "worker.json"
    done = tmp_path / "done.txt"
    gate = tmp_path / "front-exited"
    front_script = """
import asyncio, json, sys
from pathlib import Path
import psutil
from jiuwenswarm.server.supervisor import worker_process as module
async def run():
    module.build_worker_command = lambda *args, **kwargs: [
        sys.executable, '-c', sys.argv[1], sys.argv[3], sys.argv[4],
    ]
    process = module.WorkerProcess(str(Path(sys.argv[2]).with_suffix('.pipe')), host='127.0.0.1', port=18092)
    await process.spawn()
    state = {
        'pid': process.pid, 'created': psutil.Process(process.pid).create_time(),
        'stderr': str(process.stderr_path),
    }
    Path(sys.argv[2]).write_text(json.dumps(state), encoding='utf-8')
asyncio.run(run())
"""
    front = subprocess.Popen(
        [sys.executable, "-c", front_script, child_script, str(info), str(done), str(gate)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    child = None
    try:
        _, error = front.communicate(timeout=20)
        assert front.returncode == 0, error.decode(errors="replace")
        state = json.loads(info.read_text(encoding="utf-8"))
        try:
            child = psutil.Process(state["pid"])
            if child.create_time() != state["created"]:
                child = None
        except psutil.NoSuchProcess:
            pass
        # The Worker starts producing output only after its actual Front exits.
        gate.touch()
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert done.read_text(encoding="utf-8") == "done"
        if child is not None:
            _wait_for_process_exit(child)
        path = Path(state["stderr"])
        archives = list(path.parent.glob("*.stderr_*.log"))
        assert len(archives) == 2
        assert all(item.stat().st_size <= 4096 for item in archives)
        assert b"completed after Front exit\n" in path.read_bytes()
    finally:
        if front.poll() is None:
            front.kill()
        front.wait(timeout=5)
        if child is not None and not _process_exited(child):
            child.kill()
            _wait_for_process_exit(child)
