"""Conditional file publication on real files, without starting a sandbox."""

import hashlib
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenbox.models.common import AuditEventType
from jiuwenbox.models.sandbox import SandboxPhase
from jiuwenbox.server.sandbox_manager import SandboxManager
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


def _write(path, ready, release, result, value):
    meta, _ = request(path)
    ready.put(True)
    release.wait(10)
    result.put(
        request(path, value, expected_exists=True, expected_content_sha256=meta["content_sha256"])[
            0
        ]
    )


def test_missing_empty_and_raw_hash(tmp_path):
    path = tmp_path / "nested/todo.json"
    meta, raw = request(path, missing_ok=True)
    assert meta["ok"] and not meta["exists"] and not path.parent.exists()
    options = {"expected_exists": False, "expected_content_sha256": meta["content_sha256"]}
    assert request(path, b"", **options)[0]["ok"]
    assert request(path, b"bad", **options)[0]["error"] == "conflict"
    path.write_bytes(b"a\r\nb\r\n")
    meta, raw = request(path)
    assert raw == b"a\r\nb\r\n"
    assert meta["content_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("failure", ["fdopen", "read", None])
def test_snapshot_read_always_closes_descriptor(tmp_path, monkeypatch, failure):
    from jiuwenbox.supervisor import sandbox_daemon

    path = tmp_path / "todo.json"
    path.write_bytes(b"old")
    original_open, original_fdopen = os.open, os.fdopen
    descriptors = []

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    def fail_fdopen(*args, **kwargs):
        if failure == "fdopen":
            raise OSError("injected stream creation failure")
        return original_fdopen(*args, **kwargs)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    if failure == "read":
        monkeypatch.setattr(sandbox_daemon, "MAX_FILE_BYTES", 1)
    response, content = request(path)
    if failure:
        assert not response["ok"]
        assert response["error"] == "io_error"
    else:
        assert response["ok"]
        assert content == b"old"
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_cross_process_compare_replace(tmp_path):
    path = tmp_path / "todo.json"
    path.write_bytes(b"old")
    ctx = multiprocessing.get_context("spawn")
    ready, release, result = ctx.Queue(), ctx.Event(), ctx.Queue()
    children = [
        ctx.Process(target=_write, args=(path, ready, release, result, value))
        for value in (b"A", b"B")
    ]
    try:
        for child in children:
            child.start()
        for _ in children:
            assert ready.get(timeout=15)
        release.set()
        results = [result.get(timeout=15) for _ in children]
        assert sum(r["ok"] for r in results) == 1
        assert [r["error"] for r in results if not r["ok"]] == ["conflict"]
        assert path.read_bytes() in (b"A", b"B")
    finally:
        release.set()
        for child in children:
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join()


@pytest.mark.parametrize("failure", ["replace", "permission"])
def test_failure_preserves_file(tmp_path, monkeypatch, failure):
    path = tmp_path / "todo.json"
    path.write_bytes(b"old")
    meta, _ = request(path)
    original = os.open

    def deny(*args, **kwargs):
        if failure == "replace" or (
            str(args[0]) in (str(path), path.name) and args[1] & os.O_WRONLY
        ):
            raise PermissionError("injected refusal")
        return original(*args, **kwargs)

    monkeypatch.setattr(os, "replace" if failure == "replace" else "open", deny)
    assert not request(
        path, b"new", expected_exists=True, expected_content_sha256=meta["content_sha256"]
    )[0]["ok"]
    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp"))


def test_permissions_and_independent_objects(tmp_path):
    paths = [tmp_path / f"todo-{i}" for i in range(2)]
    for path in paths:
        path.write_bytes(b"old")
        path.chmod(0o640)

    def replace(path):
        meta, _ = request(path)
        return request(
            path, b"new", expected_exists=True, expected_content_sha256=meta["content_sha256"]
        )[0]

    with ThreadPoolExecutor(2) as pool:
        assert all(r["ok"] for r in pool.map(replace, paths))
    assert all(p.stat().st_mode & 0o777 == 0o640 for p in paths)


def test_requires_both_preconditions(tmp_path):
    path = tmp_path / "todo.json"
    path.write_bytes(b"keep")
    assert request(path, b"bad", expected_exists=True)[0]["error"] == "bad_request"
    assert path.read_bytes() == b"keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,direction", [("read", "download"), ("compare_replace", "upload")])
@pytest.mark.parametrize("ok", [True, False])
async def test_snapshot_audit_direction(tmp_path, monkeypatch, operation, direction, ok):
    response = {"ok": ok, **({} if ok else {"error": "conflict"})}
    runtime = SimpleNamespace(file_snapshot=AsyncMock(return_value=(response, b"read")))
    audit = Mock()
    manager = SandboxManager(runtime=runtime, audit_logger=audit, state_dir=tmp_path, policy_reader=Mock())
    monkeypatch.setattr(manager, "_get_sandbox", Mock(return_value=SimpleNamespace(phase=SandboxPhase.READY)))
    monkeypatch.setattr(manager, "_mark_active", Mock())
    enforce = AsyncMock()
    monkeypatch.setattr(manager, "_enforce_path_policy", enforce)
    payload = {"operation": operation, "path": "/todo.json"}
    content = b"write" if operation == "compare_replace" else b""
    assert await manager.file_snapshot("box", payload, content) == (response, b"read")
    enforce.assert_awaited_once_with("box", "/todo.json", op="write" if content else "read")
    runtime.file_snapshot.assert_awaited_once_with("box", payload, content)
    args, data = audit.log.call_args
    assert args == (AuditEventType.FILE_TRANSFER, "box")
    assert data["direction"] == direction and data["operation"] == operation
    assert data["ok"] is ok and data["path"] == "snapshot_ipc"
