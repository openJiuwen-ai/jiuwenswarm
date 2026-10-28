# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Source identity cache never substitutes timestamps for content identity."""
from __future__ import annotations

import concurrent.futures
import asyncio
import hashlib
import io
import json
import os
import subprocess
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.supervisor import identity, identity_cache as cache


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    package = tmp_path / "checkout" / "jiuwenswarm"
    package.mkdir(parents=True)
    (package / "a.py").write_text("value = 1\n", encoding="utf-8")
    (package.parent / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (package.parent / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    versions = {}
    monkeypatch.setattr(cache, "_ntfs_marker", lambda path: [str(path.resolve()), versions.get(str(path), 0)])
    monkeypatch.setattr(cache, "_cache_path", lambda root: tmp_path / "cache" / "identity.json")
    return package, versions


def write_source(path, value, versions):
    path.write_text(value, encoding="utf-8")
    versions[str(path)] = versions.get(str(path), 0) + 1


def test_validated_cache_hit_does_not_read_source_content(checkout, monkeypatch):
    package, _versions = checkout
    first = cache.source_fingerprint(package)
    real = Path.read_bytes

    def no_source_reads(path):
        if path.suffix == ".py" and path.is_relative_to(package):
            pytest.fail("an unchanged source digest must be reused")
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", no_source_reads)
    assert cache.source_fingerprint(package) == first
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 0


def test_dirty_source_add_delete_restore_and_lockfiles(checkout):
    package, versions = checkout
    source = package / "a.py"
    original = cache.source_fingerprint(package)
    write_source(source, "value = 2\n", versions)
    assert cache.source_fingerprint(package) != original
    write_source(source, "value = 1\n", versions)
    assert cache.source_fingerprint(package) == original
    added = package / "b.py"
    write_source(added, "other = True\n", versions)
    assert cache.source_fingerprint(package) != original
    added.unlink()
    assert cache.source_fingerprint(package) == original
    lock = package.parent / "uv.lock"
    lock.write_text("version = 2\n", encoding="utf-8")
    assert cache.source_fingerprint(package) != original
    lock.unlink()
    assert cache.source_fingerprint(package) != original


def test_source_walk_prunes_only_frontend_node_dependencies_before_entering(checkout, monkeypatch):
    package, _versions = checkout
    excluded = package / "channels" / "web" / "frontend" / "node_modules"
    paths = [
        excluded / "gyp" / "build.py",
        package / "channels" / "web" / "backend" / "server.py",
        package / "channels" / "web" / "frontend" / "runtime.py",
        package / "resources" / "runtime.py",
        package / "extensions" / "runtime.py",
        package / "another" / "node_modules" / "runtime.py",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
    original_scandir = os.scandir

    def guarded_scandir(path):
        assert not Path(path).is_relative_to(excluded), "build-only Node tree must never be entered"
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    scanned = cache._source_paths(package)
    assert paths[0] not in scanned
    assert all(path in scanned for path in paths[1:])


@pytest.mark.parametrize("fingerprint", [cache.source_fingerprint, cache.full_source_fingerprint])
def test_frontend_node_tool_edits_do_not_hide_backend_changes(checkout, fingerprint):
    package, versions = checkout
    frontend = package / "channels" / "web" / "frontend"
    tool = frontend / "node_modules" / "gyp" / "build.py"
    backend = package / "channels" / "web" / "backend" / "server.py"
    for path in (tool, backend):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_source(path, "value = 1\n", versions)
    original = fingerprint(package)
    write_source(tool, "value = 2\n", versions)
    assert fingerprint(package) == original
    write_source(backend, "value = 2\n", versions)
    updated = fingerprint(package)
    assert updated != original
    added = backend.parent / "new_backend.py"
    write_source(added, "value = 3\n", versions)
    assert fingerprint(package) != updated


@pytest.mark.parametrize("manifest", ["package.json", "package-lock.json"])
@pytest.mark.parametrize("fingerprint", [cache.source_fingerprint, cache.full_source_fingerprint])
def test_frontend_dependency_manifests_remain_part_of_identity(checkout, manifest, fingerprint):
    package, _versions = checkout
    path = package / "channels" / "web" / "frontend" / manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1}\n', encoding="utf-8")
    original = fingerprint(package)
    path.write_text('{"version": 2}\n', encoding="utf-8")
    assert fingerprint(package) != original
    path.unlink()
    assert fingerprint(package) != original


def test_full_build_fingerprint_has_same_scope_without_using_cached_digests(checkout, monkeypatch):
    package, _versions = checkout
    source = package / "a.py"
    cached = cache.source_fingerprint(package)
    assert cache.full_source_fingerprint(package) == cached
    source.write_text("value = 2\n", encoding="utf-8")
    # A controlled build hashes content even if no change-marker update exists.
    full = cache.full_source_fingerprint(package)
    assert full != cached
    monkeypatch.setattr(cache, "_ntfs_marker", lambda _path: None)
    assert cache.source_fingerprint(package) == full


def test_unreadable_runtime_subtree_cannot_be_omitted_from_identity(checkout, monkeypatch):
    package, _versions = checkout
    backend = package / "channels" / "web" / "backend"
    backend.mkdir(parents=True)
    original_scandir = os.scandir

    def blocked_scandir(path):
        if Path(path) == backend:
            raise PermissionError("Runtime subtree cannot be read")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", blocked_scandir)
    with pytest.raises(PermissionError, match="Runtime subtree"):
        cache._source_paths(package)


@pytest.mark.skipif(os.name != "nt", reason="Windows glob matches suffixes without case")
def test_source_walk_preserves_windows_python_suffix_matching(checkout):
    package, _versions = checkout
    source = package / "runtime.PY"
    source.write_text("value = 1\n", encoding="utf-8")
    assert source in cache._source_paths(package)


def test_changed_source_path_changes_identity(checkout):
    package, _versions = checkout
    before = cache.source_fingerprint(package)
    (package / "a.py").rename(package / "renamed.py")
    assert cache.source_fingerprint(package) != before


@pytest.mark.parametrize("corruption", ["{broken", "[]", '{"checksum": "bad", "files": {}}'])
def test_corrupt_cache_is_rebuilt_from_content(checkout, corruption):
    package, _versions = checkout
    expected = cache.source_fingerprint(package)
    path = cache._cache_path(package.parent)
    path.write_text(corruption, encoding="utf-8")
    assert cache.source_fingerprint(package) == expected
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 1
    json.loads(path.read_text(encoding="utf-8"))


def test_unavailable_change_marker_rehashes_all_source(checkout, monkeypatch):
    package, _versions = checkout
    source = package / "a.py"
    initial = cache.source_fingerprint(package)
    monkeypatch.setattr(cache, "_ntfs_marker", lambda _path: None)
    before = source.stat()
    source.write_text("value = 2\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert cache.source_fingerprint(package) != initial
    assert cache._LAST_DIAGNOSTICS["strategy"] == "full-content"
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 1


def test_cache_io_failure_uses_content_identity(checkout, monkeypatch):
    package, _versions = checkout
    expected = cache.source_fingerprint(package)
    monkeypatch.setattr(cache, "_cache_path", lambda _root: package / "a.py" / "unwritable.json")
    assert cache.source_fingerprint(package) == expected


def test_same_build_label_cannot_override_different_source(checkout, monkeypatch):
    package, versions = checkout
    # identity.py normally resolves parents[2] to its package directory.
    fake_module = package / "server" / "supervisor" / "identity.py"
    monkeypatch.setattr(identity, "__file__", str(fake_module))
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "explicit-same-label")
    before = identity.code_identity()
    write_source(package / "a.py", "value = 2\n", versions)
    assert identity.code_identity() != before
    write_source(package / "a.py", "value = 1\n", versions)
    assert identity.code_identity() == before
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "different-label")
    assert identity.code_identity() != before


def test_ntfs_marker_detects_same_length_restored_mtime(tmp_path):
    source = tmp_path / "same.py"
    source.write_text("value = 1\n", encoding="utf-8")
    first = cache._ntfs_marker(source)
    if first is None:
        pytest.skip("reliable NTFS per-file USN is unavailable")
    stat = source.stat()
    source.write_text("value = 2\n", encoding="utf-8")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_size == stat.st_size and source.stat().st_mtime_ns == stat.st_mtime_ns
    second = cache._ntfs_marker(source)
    assert second is not None and second != first


def test_real_ntfs_cache_rejects_restored_mtime_same_size_edit(tmp_path, monkeypatch):
    package = tmp_path / "actual-ntfs-checkout" / "jiuwenswarm"
    package.mkdir(parents=True)
    source = package / "a.py"
    source.write_bytes(b"value = 1\n")
    if cache._ntfs_marker(source) is None:
        pytest.skip("reliable NTFS per-file USN is unavailable")
    monkeypatch.setattr(cache, "_cache_path", lambda _root: tmp_path / "real-cache.json")
    before = cache.source_fingerprint(package)
    assert cache._LAST_DIAGNOSTICS["strategy"] == "ntfs-usn"
    stat = source.stat()
    source.write_bytes(b"value = 2\n")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_size == stat.st_size and source.stat().st_mtime_ns == stat.st_mtime_ns
    assert cache.source_fingerprint(package) != before
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 1


def test_two_checkouts_are_separate_even_with_identical_source(checkout, tmp_path, monkeypatch):
    package, _versions = checkout
    other = tmp_path / "other-checkout" / "jiuwenswarm"
    other.mkdir(parents=True)
    (other / "a.py").write_text("value = 1\n", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock"):
        (other.parent / name).write_bytes((package.parent / name).read_bytes())
    monkeypatch.setattr(cache, "_cache_path", lambda root: tmp_path / "cache" / (root.name + ".json"))
    assert cache.source_fingerprint(package) != cache.source_fingerprint(other)


def test_source_change_during_validation_retries_content(checkout, monkeypatch):
    package, versions = checkout
    source = package / "a.py"
    before = cache.source_fingerprint(package)
    marker = cache._ntfs_marker
    calls = 0

    def changing(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            write_source(source, "value = 2\n", versions)
        return marker(path)

    monkeypatch.setattr(cache, "_ntfs_marker", changing)
    assert cache.source_fingerprint(package) != before


def test_git_metadata_and_dirty_paths_follow_index_without_changing_same_code(checkout):
    package, versions = checkout
    git = package.parent / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/test\n", encoding="utf-8")
    (git / "refs/heads").mkdir(parents=True)
    (git / "refs/heads/test").write_text("a" * 40 + "\n", encoding="utf-8")
    source = package / "a.py"

    def publish_index():
        name = b"jiuwenswarm/a.py"
        entry = bytes(40) + bytes.fromhex(cache._content_hashes(source)["git_blob"]) + struct.pack(">H", len(name)) + name + b"\0"
        entry += bytes((-len(entry)) % 8)
        content = b"DIRC" + struct.pack(">II", 2, 1) + entry
        (git / "index").write_bytes(content + hashlib.sha1(content, usedforsecurity=False).digest())

    publish_index()
    before = cache.source_fingerprint(package)
    path = cache._cache_path(package.parent)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert not any(name.endswith("a.py") for name in state["dirty_paths"])
    write_source(source, "value = 2\n", versions)
    dirty = cache.source_fingerprint(package)
    assert dirty != before
    state = json.loads(path.read_text(encoding="utf-8"))
    assert any(name.endswith("a.py") for name in state["dirty_paths"])
    publish_index()
    (git / "refs/heads/test").write_text("b" * 40 + "\n", encoding="utf-8")
    assert cache.source_fingerprint(package) == dirty
    state = json.loads(path.read_text(encoding="utf-8"))
    assert not any(name.endswith("a.py") for name in state["dirty_paths"])


def test_unrecognized_index_version_uses_full_content(checkout):
    package, _versions = checkout
    git = package.parent / ".git"
    git.mkdir()
    (git / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    (git / "index").write_bytes(b"DIRC" + struct.pack(">II", 4, 0))
    cache.source_fingerprint(package)
    assert cache._LAST_DIAGNOSTICS["strategy"] == "full-content"


def test_git_autocrlf_diagnostics_do_not_hide_actual_source_changes(checkout):
    package, versions = checkout
    git = package.parent / ".git"
    git.mkdir()
    (git / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    (git / "config").write_text("[core]\n autocrlf = true\n", encoding="utf-8")
    source = package / "a.py"
    source.write_bytes(b"value = 1\n")
    clean_blob = cache._content_hashes(source)["git_blob"]
    name = b"jiuwenswarm/a.py"
    entry = bytes(40) + bytes.fromhex(clean_blob) + struct.pack(">H", len(name)) + name + b"\0"
    entry += bytes((-len(entry)) % 8)
    index = b"DIRC" + struct.pack(">II", 2, 1) + entry
    (git / "index").write_bytes(index + hashlib.sha1(index, usedforsecurity=False).digest())
    before = cache.source_fingerprint(package)
    source.write_bytes(b"value = 1\r\n")
    versions[str(source)] = 1
    # Runtime identity always hashes the actual bytes, even when Git considers
    # a text checkout clean after normalizing its line endings.
    assert cache.source_fingerprint(package) != before
    state = json.loads(cache._cache_path(package.parent).read_text(encoding="utf-8"))
    assert not any(name.endswith("a.py") for name in state["dirty_paths"])
    source.write_bytes(b"value = 2\r\n")
    versions[str(source)] = 2
    cache.source_fingerprint(package)
    state = json.loads(cache._cache_path(package.parent).read_text(encoding="utf-8"))
    assert any(name.endswith("a.py") for name in state["dirty_paths"])


def test_concurrent_real_processes_publish_one_valid_cache(tmp_path, monkeypatch):
    package = tmp_path / "concurrent" / "jiuwenswarm"
    package.mkdir(parents=True)
    (package / "a.py").write_text("value = 1\n", encoding="utf-8")
    if cache._ntfs_marker(package / "a.py") is None:
        pytest.skip("reliable NTFS per-file USN is unavailable")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache-root"))
    root = Path(__file__).resolve().parents[3]
    script = "from pathlib import Path; from jiuwenswarm.server.supervisor.identity_cache import source_fingerprint; import sys; print(source_fingerprint(Path(sys.argv[1])))"
    env = {**os.environ, "PYTHONPATH": str(root)}

    def compute(_index):
        return subprocess.check_output([sys.executable, "-c", script, str(package)], env=env, cwd=root,
                                       encoding="utf-8", timeout=20).strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(compute, range(4)))
    assert len(set(identities)) == 1
    value = json.loads(cache._cache_path(package.parent).read_text(encoding="utf-8"))
    assert value["fingerprint"] == identities[0]
    checksum = value.pop("checksum")
    assert checksum == cache._bytes_digest(cache._encoded(value))


def test_controlled_frozen_build_identity_is_path_independent(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "jiuwenswarm").mkdir(parents=True)
    (bundle / "jiuwenswarm/_worker_build_identity.json").write_text(
        json.dumps({"schema": 1, "fingerprint": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.delenv("JIUWENSWARM_BUILD_ID", raising=False)
    first = identity.code_identity()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "elsewhere.exe"))
    assert identity.code_identity() == first
    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "another-package")
    assert identity.code_identity() != first


def test_frozen_build_without_controlled_identity_is_rejected(tmp_path, monkeypatch):
    executable = tmp_path / "legacy.exe"
    executable.write_bytes(b"first")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("JIUWENSWARM_BUILD_ID", raising=False)
    with pytest.raises(RuntimeError, match="Invalid Worker build identity"):
        identity.code_identity()


@pytest.mark.parametrize("value", [b'\xef\xbb\xbf[core]\n autocrlf = true\n',
                                  b'autocrlf = true\n', b'[core]\n invalid syntax\n'])
@pytest.mark.parametrize("location", ["global", "repository"])
def test_invalid_gitconfig_falls_back_to_content_validation(checkout, monkeypatch, value, location):
    package, _versions = checkout
    git = package.parent / ".git"
    git.mkdir()
    (git / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    target = package.parent / "global.gitconfig" if location == "global" else git / "config"
    target.write_bytes(value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(package.parent / "global.gitconfig"))
    first = cache.source_fingerprint(package)
    assert cache._git_metadata(package.parent) == ({"unavailable": True}, None)
    assert cache._LAST_DIAGNOSTICS["strategy"] == "full-content"
    # A stale change marker must not let broken Git metadata reuse a digest.
    (package / "a.py").write_text("value = 2\n", encoding="utf-8")
    assert cache.source_fingerprint(package) != first


def test_identity_cache_directory_isolated_from_other_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "one"))
    first = cache._cache_path(tmp_path / "checkout")
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "two"))
    second = cache._cache_path(tmp_path / "checkout")
    assert first.parent != second.parent
    assert first.name == second.name


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS change-time fallback")
def test_ntfs_without_usn_persists_digests_and_detects_restored_mtime(tmp_path, monkeypatch):
    real = cache._windows_kernel()

    class WithoutUsn:
        def __getattr__(self, name):
            return getattr(real, name)

        def DeviceIoControl(self, *args):
            return False

    monkeypatch.setattr(cache, "_windows_kernel", lambda: WithoutUsn())
    package = tmp_path / "checkout" / "jiuwenswarm"
    package.mkdir(parents=True)
    source = package / "same.py"
    source.write_bytes(b"value = 1\n")
    marker = cache._ntfs_marker(source)
    if marker is None:
        pytest.skip("local filesystem has no trusted NTFS change time")
    assert marker[0] == "ntfs-change-time"
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "cache"))
    first = cache.source_fingerprint(package)
    assert cache._LAST_DIAGNOSTICS["strategy"] == "ntfs-change-time"
    assert cache.source_fingerprint(package) == first
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 0
    previous = source.stat()
    source.write_bytes(b"value = 2\n")
    os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert source.stat().st_size == previous.st_size
    assert source.stat().st_mtime_ns == previous.st_mtime_ns
    assert cache.source_fingerprint(package) != first
    assert cache._LAST_DIAGNOSTICS["rehashed_files"] == 1


@pytest.mark.parametrize("field,value", [("parent_pid", -1), ("package", "elsewhere"),
    ("data_scope", "other"), ("build_label", "forged"), ("code_identity", "bad")])
def test_parent_identity_pipe_rejects_wrong_deployment(monkeypatch, field, value):
    payload = json.loads(identity.launch_identity_payload("a" * 64))
    payload["parent_pid"] = os.getppid()
    payload[field] = value
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    with pytest.raises(RuntimeError, match="Invalid Worker parent identity"):
        identity.read_launch_identity()


def test_parent_identity_pipe_consumed_once(monkeypatch):
    monkeypatch.setattr(identity, "code_identity", lambda: "a" * 64)
    payload = json.loads(identity.launch_identity_payload("a" * 64))
    payload["parent_pid"] = os.getppid()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert identity.read_launch_identity() == "a" * 64
    with pytest.raises(RuntimeError, match="Invalid Worker parent identity"):
        identity.read_launch_identity()


def test_parent_source_observation_reuses_content_and_rejects_launch_edit(checkout, monkeypatch):
    package, versions = checkout
    monkeypatch.setattr(identity, "__file__", str(package / "server" / "supervisor" / "identity.py"))
    before = identity.code_identity()
    payload = json.loads(identity.launch_identity_payload(before))
    payload["parent_pid"] = os.getppid()
    assert payload["source_observation"] is not None

    def pipe():
        return io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))

    monkeypatch.setattr(sys, "stdin", pipe())
    real = Path.read_bytes

    def no_source_reads(path):
        if path.suffix == ".py" and path.is_relative_to(package):
            pytest.fail("parent's unchanged content should not be hashed again")
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", no_source_reads)
    assert identity.read_launch_identity() == before
    monkeypatch.setattr(Path, "read_bytes", real)
    write_source(package / "a.py", "value = 2\n", versions)
    monkeypatch.setattr(sys, "stdin", pipe())
    with pytest.raises(RuntimeError, match="Invalid Worker parent identity"):
        identity.read_launch_identity()


def test_launch_parent_accepts_only_the_known_windows_launcher_hop(monkeypatch):
    import psutil

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "getppid", lambda: 42)
    launcher = SimpleNamespace(ppid=lambda: 41, exe=lambda: sys.executable)
    monkeypatch.setattr(psutil, "Process", lambda _pid: launcher)
    assert identity._is_launch_parent(42)
    assert identity._is_launch_parent(41)
    launcher.exe = lambda: str(Path(sys.executable).parent / "unrelated.exe")
    assert not identity._is_launch_parent(41)
    launcher.exe = lambda: sys.executable
    launcher.ppid = lambda: 40
    assert not identity._is_launch_parent(41)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b'\xef\xbb\xbf[core]\n autocrlf = true\n', b'broken syntax\n'])
async def test_bad_global_gitconfig_does_not_block_real_worker_hello(tmp_path, monkeypatch, value):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.supervisor.worker_process import WorkerProcess

    config = tmp_path / "bad.gitconfig"
    config.write_bytes(value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / "data")
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identities"))
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", str(root))
    socket = str(tmp_path / ("runtime.pipe" if os.name == "nt" else "runtime.sock"))
    worker = WorkerProcess(socket, host="127.0.0.1", port=18092, stub=True)
    conn = None
    try:
        await worker.spawn()
        conn = await connect_with_retry(socket, timeout=15)
        hello = await asyncio.wait_for(conn.recv(), 5)
        assert hello["kind"] == "hello"
        assert hello["code_identity"] == worker.launch_identity
        assert (await asyncio.wait_for(conn.recv(), 5))["kind"] == "readiness"
        await conn.send({"kind": "shutdown"})
        assert (await asyncio.wait_for(conn.recv(), 5))["ok"] is True
        assert await worker.wait(timeout=5) == 0
    finally:
        if conn is not None:
            await conn.close()
        await worker.stop(timeout=5)
