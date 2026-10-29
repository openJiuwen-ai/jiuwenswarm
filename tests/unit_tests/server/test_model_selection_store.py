# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict model-selection reads share the metadata writer's process lock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import ctypes
import json
import multiprocessing
from pathlib import Path
import sys
import threading

import pytest

from jiuwenswarm.common.model_errors import MODEL_GROUP_INVALID, ModelSelectionError
from gateway.common.model_selection import ModelSelection
from jiuwenswarm.common import external_providers, model_selection_store as store
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session import session_metadata


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "agent" / "sessions"
    for module in (store, lifecycle, session_metadata):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setitem(external_providers._providers, "session_metadata", session_metadata)
    return root


def write_metadata(sessions, value):
    path = sessions / "session-a" / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@contextmanager
def windows_exclusive_metadata(path):
    """Keep the real Windows sharing-violation window open deterministically."""
    if sys.platform != "win32":
        yield
        return
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close(handle)


def replace_selection_with_writer_locks(sessions, entered, release):
    """Exercise the real metadata writer, including its in-process file lock."""
    root = Path(sessions)
    original_replace = session_metadata._atomic_replace
    original_lifecycle_root = lifecycle.get_agent_sessions_dir
    original_metadata_root = session_metadata.get_agent_sessions_dir
    lifecycle.get_agent_sessions_dir = lambda: root
    session_metadata.get_agent_sessions_dir = lambda: root

    def held_replace(source, destination, **kwargs):
        held = lifecycle._HELD_LOCKS.paths
        assert lifecycle.resource_path("project", "default") in held
        assert lifecycle.resource_path("session", "session-a") in held
        assert session_metadata._FILE_LOCK._is_owned()
        with windows_exclusive_metadata(destination):
            entered.set()
            assert release.wait(10), "reader did not release the held replacement"
        original_replace(source, destination, **kwargs)

    session_metadata._atomic_replace = held_replace
    try:
        session_metadata.update_session_model_selection("session-a", {"type": "model", "id": "mdl_new"})
    finally:
        session_metadata._atomic_replace = original_replace
        lifecycle.get_agent_sessions_dir = original_lifecycle_root
        session_metadata.get_agent_sessions_dir = original_metadata_root


@pytest.mark.parametrize("writer_location", ["thread", "process"])
def test_read_waits_for_atomic_replacement_and_returns_committed_selection(sessions, writer_location):
    write_metadata(sessions, {
        "session_id": "session-a", "project_id": "default", "work_mode": "work",
        "model_selection": {"type": "model", "id": "mdl_old"},
    })
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    writer = None
    writer_future = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            if writer_location == "process":
                writer = context.Process(target=replace_selection_with_writer_locks, args=(sessions, entered, release))
                writer.start()
            else:
                writer_future = executor.submit(replace_selection_with_writer_locks, sessions, entered, release)
            assert entered.wait(10), "writer never reached its protected atomic replacement"
            reader_started = threading.Event()

            def read():
                reader_started.set()
                return store.get_session_model_selection(" session-a ")

            reader = executor.submit(read)
            assert reader_started.wait(2)
            # The old reader raises PermissionError on Windows (or sees stale
            # bytes elsewhere) before the writer has committed its new value.
            with pytest.raises(FutureTimeout):
                reader.result(timeout=0.05)
            release.set()
            assert reader.result(timeout=5) == ModelSelection(type="model", id="mdl_new")
        finally:
            release.set()
            if writer is not None:
                writer.join(10)
                if writer.is_alive():
                    writer.terminate()
                    writer.join(5)
                assert writer.exitcode == 0
            if writer_future is not None:
                writer_future.result(timeout=5)


def test_missing_metadata_returns_none_without_creating_session(sessions):
    assert store.get_session_model_selection("session-a") is None
    assert not (sessions / "session-a").exists()


@pytest.mark.parametrize("session_id", ["", "../escape", "a/b", "a\\b", "a:b", "a" * 81])
def test_invalid_id_has_no_filesystem_side_effects(sessions, session_id):
    with pytest.raises(ValueError, match="invalid session_id"):
        store.get_session_model_selection(session_id)
    assert not sessions.parent.exists()


@pytest.mark.parametrize("content", ["{", "null", "[]", '"text"', '{"model_selection":{"type":"model","id":""}}'])
def test_corrupt_metadata_is_a_typed_error(sessions, content):
    path = write_metadata(sessions, {})
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ModelSelectionError) as caught:
        store.get_session_model_selection("session-a")
    assert caught.value.code == MODEL_GROUP_INVALID
    assert isinstance(caught.value.__cause__, ValueError)


def test_metadata_permission_error_is_not_treated_as_missing(sessions, monkeypatch):
    path = write_metadata(sessions, {})
    original_read = Path.read_text

    def deny_metadata(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError(13, "metadata access denied", str(path))
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_metadata)
    with pytest.raises(ModelSelectionError) as caught:
        store.get_session_model_selection("session-a")
    assert caught.value.code == MODEL_GROUP_INVALID
    assert isinstance(caught.value.__cause__, PermissionError)


def test_lock_file_failure_is_not_treated_as_missing_metadata(sessions, monkeypatch):
    @contextmanager
    def unavailable_lock(*_args):
        raise FileNotFoundError("lock directory unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(lifecycle, "resource_lock", unavailable_lock)
    with pytest.raises(ModelSelectionError) as caught:
        store.get_session_model_selection("session-a")
    assert caught.value.code == MODEL_GROUP_INVALID
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_read_releases_resource_lock_before_parsing(sessions, monkeypatch):
    write_metadata(sessions, {"model_selection": {"type": "model", "id": "mdl_a"}})
    original_loads = json.loads

    def unlocked_loads(text, *args, **kwargs):
        assert lifecycle.resource_path("session", "session-a") not in lifecycle._HELD_LOCKS.paths
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", unlocked_loads)
    assert store.get_session_model_selection("session-a").id == "mdl_a"


def test_reader_is_reentrant_under_existing_session_lock(sessions):
    write_metadata(sessions, {"model_selection": {"type": "model", "id": "mdl_a"}})
    with lifecycle.resource_lock("session", "session-a"):
        assert store.get_session_model_selection("session-a").id == "mdl_a"


def test_reader_and_writer_normalize_the_same_session_id(sessions):
    selection = ModelSelection(type="model", id="mdl_a")
    store.set_session_model_selection(" session-a ", selection)
    assert store.get_session_model_selection(" session-a ") == selection
    assert not (sessions / " session-a ").exists()
