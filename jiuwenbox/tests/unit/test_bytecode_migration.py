"""Portable guards for failure-triggered bytecode migration, without Windows."""

from types import SimpleNamespace
from unittest.mock import Mock
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jiuwenbox.supervisor import win_acl


def cache_file(root, name="worker.cpython-313.pyc", backed=True):
    package = root / "arbitrary-package"
    cache = package / "__pycache__" / name
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"derived data")
    if backed:
        (package / (name.split('.')[0] + ".py")).write_text("VALUE = 1\n")
    return cache


@pytest.mark.parametrize("name", ["worker.cpython-312.pyc", "__init__.cpython-313.pyc",
                                  "worker.cpython-313.opt-1.pyc"])
def test_backed_cache_in_any_declared_root_is_recognized(tmp_path, name):
    root = tmp_path / "plugin"
    cache = cache_file(root, name)
    assert win_acl._bytecode_source(cache, root) == cache.parent.parent / (name.split('.')[0] + ".py")


@pytest.mark.parametrize("name", ["worker.pyc", "worker.cpython-313.log", "worker.cpython-313.opt-9.pyc"])
def test_other_artifacts_are_not_cache_candidates(tmp_path, name):
    cache = cache_file(tmp_path, name)
    assert win_acl._bytecode_source(cache, tmp_path) is None


def test_unbacked_or_outside_cache_is_rejected(tmp_path):
    cache = cache_file(tmp_path / "outside", backed=False)
    assert win_acl._bytecode_source(cache, tmp_path / "outside") is None
    (cache.parent.parent / "worker.py").touch()
    assert win_acl._bytecode_source(cache, tmp_path / "another-root") is None


@pytest.mark.parametrize("dot_root,dot_path", [(True, False), (False, True), (True, True)])
def test_root_and_cache_use_the_same_canonical_path(tmp_path, dot_root, dot_path):
    (tmp_path / "detour").mkdir()
    cache = cache_file(tmp_path)
    root = tmp_path / "detour" / ".." if dot_root else tmp_path
    supplied = tmp_path / "detour" / ".." / cache.relative_to(tmp_path) if dot_path else cache
    assert win_acl._bytecode_source(supplied, root) == (cache.parent.parent / "worker.py").resolve()


def test_short_root_alias_is_compared_using_its_resolved_identity(tmp_path, monkeypatch):
    root = tmp_path / "long-runtime-name"
    alias = tmp_path / "LONGRU~1"
    cache = cache_file(root)
    resolve, lstat = Path.resolve, Path.lstat
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw:
                        resolve(root if path == alias else path, *a, **kw))
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw:
                        lstat(root if path == alias else path, *a, **kw))
    assert win_acl._bytecode_source(cache, alias) == (cache.parent.parent / "worker.py").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 8.3 paths")
def test_real_short_root_alias_accepts_long_and_short_cache_routes(tmp_path):
    import ctypes

    root = tmp_path / "long-runtime-directory-name"
    cache = cache_file(root)
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
    if not length or "~" not in buffer.value:
        pytest.skip("8.3 aliases are unavailable on this volume")
    short_root = Path(buffer.value)
    short_cache = short_root / cache.relative_to(root)
    expected = (cache.parent.parent / "worker.py").resolve()
    assert win_acl._bytecode_source(cache, short_root) == expected
    assert win_acl._bytecode_source(short_cache, root) == expected


@pytest.mark.parametrize("linked", ["cache", "source", "directory", "root"])
def test_linked_cache_source_or_parent_is_rejected(tmp_path, monkeypatch, linked):
    cache = cache_file(tmp_path)
    target = {"cache": cache, "source": cache.parent.parent / "worker.py",
              "directory": cache.parent, "root": tmp_path}[linked]
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    assert win_acl._bytecode_source(cache, tmp_path) is None


@pytest.mark.parametrize("owner,removed", [("sandbox-current", True), ("host", False), ("orphan-old", False)])
def test_only_current_sandbox_owner_can_be_migrated(tmp_path, monkeypatch, caplog, owner, removed):
    caplog.set_level(logging.DEBUG, logger=win_acl.__name__)
    cache = cache_file(tmp_path)
    security = SimpleNamespace(OWNER_SECURITY_INFORMATION=1,
        GetFileSecurity=Mock(return_value=SimpleNamespace(GetSecurityDescriptorOwner=lambda: owner)),
        ConvertSidToStringSid=lambda value: value)
    monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (security, None, None))
    assert win_acl.migrate_sandbox_bytecode(str(cache), str(tmp_path), "sandbox-current") is removed
    assert cache.exists() is not removed
    assert (cache.parent.parent / "worker.py").read_text() == "VALUE = 1\n"
    if not removed:
        assert f"owner_sid={owner} eligible=false reason=owner_mismatch" in caplog.text


def test_owner_read_failure_does_not_delete_cache(tmp_path, monkeypatch, caplog):
    cache = cache_file(tmp_path)
    security = SimpleNamespace(OWNER_SECURITY_INFORMATION=1, GetFileSecurity=Mock(side_effect=PermissionError("owner")))
    monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (security, None, None))
    assert not win_acl.migrate_sandbox_bytecode(str(cache), str(tmp_path), "sandbox")
    assert cache.exists()
    assert "eligible=unknown reason=owner_read_failed" in caplog.text


def test_eligible_cache_delete_failure_is_distinct_from_ineligible(tmp_path, monkeypatch, caplog):
    cache = cache_file(tmp_path)
    security = SimpleNamespace(OWNER_SECURITY_INFORMATION=1,
        GetFileSecurity=lambda *a: SimpleNamespace(GetSecurityDescriptorOwner=lambda: "sandbox"),
        ConvertSidToStringSid=str)
    monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (security, None, None))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError("cannot delete cache")))
    assert not win_acl.migrate_sandbox_bytecode(str(cache), str(tmp_path), "sandbox")
    assert cache.exists()
    assert "eligible=true reason=delete_failed" in caplog.text


@pytest.mark.parametrize("empty", [True, False])
def test_cache_directory_is_diagnosed_without_deletion(tmp_path, monkeypatch, caplog, empty):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    if not empty:
        (cache / "keep.txt").write_text("keep")
    monkeypatch.setattr(win_acl, "_ensure_pywin32", Mock(side_effect=AssertionError("not a cache file")))
    caplog.set_level(logging.DEBUG, logger=win_acl.__name__)
    assert not win_acl.migrate_sandbox_bytecode(str(cache), str(tmp_path), "sandbox")
    assert cache.is_dir()
    assert "reason=directory_not_cache" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_python_import_caches_outside_the_source_tree(tmp_path):
    from jiuwenbox.supervisor.win_exec import configure_python_cache_env

    source = tmp_path / "source"
    source.mkdir()
    (source / "cache_probe.py").write_text("VALUE = 1\n")
    env = dict(os.environ, PYTHONPATH=str(source))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    configure_python_cache_env(env, str(tmp_path / "sandbox-temp"))
    result = subprocess.run([sys.executable, "-c", "import cache_probe,json; print(json.dumps(cache_probe.__cached__))"],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=True)
    cached = Path(json.loads(result.stdout))
    assert cached.is_relative_to(tmp_path / "sandbox-temp" / "python-cache") and cached.is_file()
    assert not (source / "__pycache__").exists()
