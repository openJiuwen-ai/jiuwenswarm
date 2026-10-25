"""Real-file contracts for the sandbox-owned conditional publish operation."""

import hashlib
import multiprocessing
import os
import struct
import sys
from pathlib import Path

import pytest

from jiuwenbox.supervisor.sandbox_daemon import file_snapshot_operation


def request(path, content=None, **options):
    return file_snapshot_operation(
        {
            "operation": "read" if content is None else "compare_replace",
            "path": str(path),
            "options": options,
        },
        content or b"",
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX access ACL format")
@pytest.mark.parametrize("case", ["explicit", "inherited", "copy_failure"])
def test_snapshot_preserves_posix_access_acl(tmp_path, monkeypatch, case):
    def acl(owner):
        entries = [(1, owner, 0xFFFFFFFF), (2, 4, 65534), (4, 0, 0xFFFFFFFF),
                   (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
        return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)

    if case == "inherited":
        os.setxattr(tmp_path, "system.posix_acl_default", acl(7))
    path = tmp_path / "acl.json"
    path.write_bytes(b"old")
    os.setxattr(path, "system.posix_acl_access", acl(6))
    if case == "inherited":
        os.removexattr(path, "system.posix_acl_access")
    if case == "copy_failure":
        def deny_copy(*_args, **_kwargs):
            raise PermissionError("ACL copy denied")
        monkeypatch.setattr(os, "setxattr", deny_copy)
    result, _ = request(path, b"new", expected_exists=True)
    assert result["ok"] == (case != "copy_failure")
    assert path.read_bytes() == (b"old" if case == "copy_failure" else b"new")
    if case == "inherited":
        assert "system.posix_acl_access" not in os.listxattr(path)
    else:
        assert os.getxattr(path, "system.posix_acl_access") == acl(6)
    assert not list(tmp_path.glob(".acl.json-*.tmp"))


def _compete(path, ready, release, result, value):
    snapshot, _ = request(Path(path), missing_ok=True)
    ready.put(True)
    release.wait(5)
    response, _ = request(
        Path(path), value, expected_exists=True, expected_content_sha256=snapshot["content_sha256"]
    )
    result.put(response)


def test_create_read_and_stale_precondition(tmp_path):
    path = tmp_path / "nested/todo.json"
    absent, content = request(path, missing_ok=True)
    assert absent["ok"] and absent["exists"] is False and content == b""
    assert not path.parent.exists()
    empty_hash = hashlib.sha256(b"").hexdigest()
    created, _ = request(path, b'["A"]', expected_exists=False, expected_content_sha256=empty_hash)
    assert created["ok"] and created["atomic_replace"] is True
    snapshot, content = request(path)
    assert snapshot["exists"] is True and content == b'["A"]'
    assert snapshot["content_sha256"] == hashlib.sha256(content).hexdigest()
    conflict, _ = request(
        path, b'["lost"]', expected_exists=False, expected_content_sha256=empty_hash
    )
    assert conflict["error"] == "conflict" and path.read_bytes() == content


def test_two_processes_cannot_publish_from_the_same_snapshot(tmp_path):
    path = tmp_path / "todo.json"
    path.write_bytes(b"old")
    context = multiprocessing.get_context("spawn")
    ready, result, release = context.Queue(), context.Queue(), context.Event()
    children = [
        context.Process(target=_compete, args=(str(path), ready, release, result, value))
        for value in [b"one", b"two"]
    ]
    try:
        for child in children:
            child.start()
        for _ in children:
            assert ready.get(timeout=10)
        release.set()
        responses = [result.get(timeout=10) for _ in children]
        assert sum(r["ok"] for r in responses) == 1
        assert [r["error"] for r in responses if not r["ok"]] == ["conflict"]
        assert path.read_bytes() in [b"one", b"two"]
    finally:
        release.set()
        for child in children:
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "hardlink",
        pytest.param(
            "fifo",
            marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO validation"),
        ),
    ],
)
def test_unsafe_leaf_never_reads_or_overwrites(tmp_path, kind):
    import os

    path = tmp_path / "todo.json"
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    read, _ = request(path)
    written, _ = request(path, b"bad", expected_exists=True)
    assert not read["ok"] and not written["ok"]
    assert target.read_bytes() == b"keep"


def test_replace_failure_preserves_target_and_cleans_owned_temp(tmp_path, monkeypatch):
    import os

    path = tmp_path / "todo.json"
    path.write_bytes(b"old")

    def fail(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail)
    result, _ = request(
        path,
        b"new",
        expected_exists=True,
        expected_content_sha256=hashlib.sha256(b"old").hexdigest(),
    )
    assert not result["ok"] and path.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("fault", ["write", "flush", "close"])
def test_temporary_file_faults_preserve_original(tmp_path, monkeypatch, fault):
    import os

    path = tmp_path / "todo.json"
    path.write_bytes(b"old")
    original = os.fdopen

    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            if fault == "close":
                raise OSError("injected close failure")

        def write(self, content):
            if fault == "write":
                raise OSError("injected write failure")
            return self.stream.write(content)

        def flush(self):
            if fault == "flush":
                raise OSError("injected flush failure")
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(
        os,
        "fdopen",
        lambda fd, mode: FailingWriter(original(fd, mode)) if mode == "wb" else original(fd, mode),
    )
    result, _ = request(path, b"new", expected_exists=True)
    assert not result["ok"] and path.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp"))


def test_symlinked_parent_and_readonly_target_are_rejected(tmp_path):
    import os

    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    result, _ = request(alias / "todo.json", b"bad", expected_exists=False)
    assert not result["ok"] and not (actual / "todo.json").exists()
    path = actual / "readonly.json"
    path.write_bytes(b"keep")
    path.chmod(0o444)
    can_write = os.access(path, os.W_OK)
    result, _ = request(path, b"bad", expected_exists=True)
    assert result["ok"] is can_write
    assert path.read_bytes() == (b"bad" if can_write else b"keep")


def test_empty_existing_file_is_distinct_from_missing(tmp_path):
    path = tmp_path / "empty.json"
    path.write_bytes(b"")
    result, raw = request(path, missing_ok=True)
    assert result["ok"] and result["exists"] is True and raw == b""
    result, _ = request(path, b"bad", expected_exists=False)
    assert result["error"] == "conflict" and path.read_bytes() == b""


def test_reads_observe_only_complete_snapshots(tmp_path):
    import json
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "todo.json"
    path.write_text(json.dumps({"revision": 0, "value": "0" * 8192}))

    def write():
        for revision in range(1, 31):
            content = json.dumps({"revision": revision, "value": str(revision) * 8192}).encode()
            result, _ = request(path, content, expected_exists=True)
            assert result["ok"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(write)
        for _ in range(80):
            metadata, raw = request(path)
            assert metadata["ok"] and metadata["content_sha256"] == hashlib.sha256(raw).hexdigest()
            decoded = json.loads(raw)
            assert decoded["value"] == str(decoded["revision"]) * 8192
        work.result()


def test_lock_timeout_and_disabled_create_leave_target_unchanged(tmp_path):
    from jiuwenbox.supervisor.sandbox_daemon import _snapshot_lock

    path = tmp_path / "todo.json"
    path.write_bytes(b"keep")
    with _snapshot_lock(str(path), 1):
        result, _ = request(path, b"bad", expected_exists=True, lock_timeout=0.01)
        assert result["error"] == "lock_timeout" and path.read_bytes() == b"keep"
    result, _ = request(tmp_path / "missing.json", b"bad", create_if_not_exist=False)
    assert result["error"] == "not_found" and not (tmp_path / "missing.json").exists()
