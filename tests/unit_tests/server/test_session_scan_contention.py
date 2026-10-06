"""Regression coverage for overlapping scans and low-priority migrations."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
import queue
import threading
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session import session_metadata as sm


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: root)
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(sm, "_build_project_lookup", lambda: ({}, {}))
    monkeypatch.setattr(sm, "_ensure_worker_started", lambda: None)
    monkeypatch.setattr(sm, "_METADATA_QUEUE", queue.Queue(maxsize=4))
    monkeypatch.setattr(sm, "_PENDING_MIGRATIONS", set())
    monkeypatch.setattr(sm, "_METADATA_CACHE", {})
    monkeypatch.setattr(sm, "_METADATA_CACHE_GENERATIONS", {})
    monkeypatch.setattr(sm, "_SESSION_REBIND_GEN", {})
    return root


def seed(root, sid, mode="agent.work.normal"):
    folder = root / sid
    folder.mkdir()
    data = dict(session_id=sid, mode=mode, work_mode="work", channel_id="web",
                project_id="default", last_user_message_at=1, created_at=1,
                pinned=True, pin_order=2, message_count=5)
    path = folder / "metadata.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_batch_reuses_lifecycle_and_project_state(isolated, monkeypatch):
    for sid in ("a", "b"):
        seed(isolated, sid)
    state = Mock(wraps=lc.state)
    paths = Mock(wraps=lc.session_paths)
    monkeypatch.setattr(lc, "state", state)
    monkeypatch.setattr(lc, "session_paths", paths)
    assert len(sm.collect_all_sessions_metadata()) == 2
    assert state.call_count == 3  # two sessions and one shared project
    assert paths.call_count == 2
    assert sm._METADATA_QUEUE.empty()


def test_migration_is_deduplicated_and_preserves_newer_fields(isolated):
    path, original = seed(isolated, "legacy", "agent")
    assert sm.collect_all_sessions_metadata()[0]["mode"] == "agent.work.normal"
    sm.collect_all_sessions_metadata()
    assert sm._METADATA_QUEUE.qsize() == 1
    sid, inferred, options = sm._METADATA_QUEUE.get_nowait()
    current = {**original, "mode": "agent.code.normal", "message_count": 99,
               "pinned": False, "pin_order": 0}
    path.write_text(json.dumps(current), encoding="utf-8")
    sm._write_metadata_sync(sid, inferred, options)
    assert json.loads(path.read_text(encoding="utf-8")) == current


def test_migration_persists_and_second_scan_needs_no_migration(isolated):
    path, _ = seed(isolated, "legacy", "agent")
    sm.collect_all_sessions_metadata()
    sid, inferred, options = sm._METADATA_QUEUE.get_nowait()
    sm._write_metadata_sync(sid, inferred, options)
    sm._PENDING_MIGRATIONS.clear()
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "agent.work.normal"
    sm.collect_all_sessions_metadata()
    assert sm._METADATA_QUEUE.empty()


def test_full_migration_queue_never_falls_back_to_sync_write(isolated, monkeypatch):
    path, original = seed(isolated, "legacy", "agent")
    full = queue.Queue(maxsize=1)
    full.put(None)
    monkeypatch.setattr(sm, "_METADATA_QUEUE", full)
    write = Mock(side_effect=AssertionError("must not write synchronously"))
    monkeypatch.setattr(sm, "_write_metadata_sync", write)
    assert sm.collect_all_sessions_metadata()[0]["mode"] == "agent.work.normal"
    assert not sm._PENDING_MIGRATIONS
    assert json.loads(path.read_text(encoding="utf-8")) == original
    full.get_nowait()
    sm.collect_all_sessions_metadata()
    assert full.qsize() == 1


def test_migration_does_not_recreate_removed_session(isolated):
    path, _ = seed(isolated, "removed", "agent")
    sm.collect_all_sessions_metadata()
    sid, inferred, options = sm._METADATA_QUEUE.get_nowait()
    path.unlink()
    path.parent.rmdir()
    with pytest.raises(FileNotFoundError):
        sm._write_metadata_sync(sid, inferred, options)
    assert not path.parent.exists()


def test_migrations_are_bounded_and_retry_on_next_scan(isolated, monkeypatch):
    seed(isolated, "one", "agent")
    seed(isolated, "two", "agent")
    monkeypatch.setattr(sm, "_MAX_PENDING_MIGRATIONS", 1)
    sm.collect_all_sessions_metadata()
    assert sm._METADATA_QUEUE.qsize() == 1
    sid, inferred, options = sm._METADATA_QUEUE.get_nowait()
    sm._write_metadata_sync(sid, inferred, options)
    sm._PENDING_MIGRATIONS.clear()
    sm.collect_all_sessions_metadata()
    assert sm._METADATA_QUEUE.qsize() == 1
    assert sm._METADATA_QUEUE.get_nowait()[0] != sid


@pytest.mark.parametrize("fail", [False, True])
def test_overlapping_scans_share_work_but_not_mutable_results(isolated, monkeypatch, fail):
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)
    def scan(_user_id):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        if fail:
            raise ValueError("scan failed")
        return [{"nested": {"value": 1}}]
    monkeypatch.setattr(sm, "Future", ObservedFuture)
    monkeypatch.setattr(sm, "_collect_all_sessions_metadata", scan)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(sm.collect_all_sessions_metadata)
        assert entered.wait(3)
        second = executor.submit(sm.collect_all_sessions_metadata)
        try:
            assert joined.wait(3)
        finally:
            release.set()
        if fail:
            for result in (first, second):
                with pytest.raises(ValueError, match="scan failed"):
                    result.result(3)
        else:
            a, b = first.result(3), second.result(3)
            a[0]["nested"]["value"] = 2
            assert b[0]["nested"]["value"] == 1
    assert len(calls) == 1
    assert not sm._COLLECT_INFLIGHT
    monkeypatch.setattr(sm, "_collect_all_sessions_metadata", lambda _: [{"fresh": True}])
    assert sm.collect_all_sessions_metadata() == [{"fresh": True}]
