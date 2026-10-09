"""Offline tests for CloudDocStore.

Every clock is injected through now_fn and driven by the test. The repository's cron
precedent for now_fn has no test using it, whereas the time windows, backoff and GC
here all depend on time -- leaving the clock undriven would be the same as not testing
them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import (
    MAX_TRIGGERED_IDS,
    TRIGGERED_TTL_SEC,
    CloudDocStore,
)

DOC = "doc-1"


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = CloudDocStore(tmp_path / "state.json", now_fn=clock)
    s._clock = clock  # handle for the test to advance
    return s


@pytest.mark.asyncio
async def test_triggered_roundtrip_and_atomicity(store, tmp_path):
    assert await store.is_triggered(DOC, "k1") is False
    await store.mark_triggered(DOC, ["k1", "k2"])
    assert await store.is_triggered(DOC, "k1") is True
    # Writing is an atomic replace, so no .tmp should be left behind
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads((tmp_path / "state.json").read_text())[DOC]["triggered_ids"].keys() >= {"k1", "k2"}


@pytest.mark.asyncio
async def test_time_window_prunes_before_count(store):
    """Time window first, count second. Reversed, it becomes a re-triggering loop that
    never converges.

    Evicting by count first drops keys still valid inside the window; those are then
    rediscovered as new, and the new keys push yet more valid ones out.
    """
    await store.mark_triggered(DOC, [f"old-{i}" for i in range(5)])
    store._clock.advance(TRIGGERED_TTL_SEC + 1)
    await store.mark_triggered(DOC, ["fresh"])

    assert await store.is_triggered(DOC, "fresh") is True
    assert await store.is_triggered(DOC, "old-0") is False


@pytest.mark.asyncio
async def test_count_bound_keeps_newest(store):
    await store.mark_triggered(DOC, [f"k{i}" for i in range(MAX_TRIGGERED_IDS + 50)])
    snap = await store.snapshot()
    assert len(snap[DOC]["triggered_ids"]) <= MAX_TRIGGERED_IDS


@pytest.mark.asyncio
async def test_rate_limit_never_sets_failed(store):
    """Quota is shared across every document. Feed throttling into a per-document state
    machine and one person hammering one document freezes all of them until somebody
    edits the config."""
    for _ in range(10):
        b = await store.note_failure(DOC, "rate_limited")
    assert b["failed"] is False
    assert b["failures"] == 0


@pytest.mark.asyncio
async def test_permission_failures_do_set_failed(store):
    for _ in range(3):
        b = await store.note_failure(DOC, "forbidden")
    assert b["failed"] is True
    await store.clear_failure(DOC)
    assert (await store.snapshot())[DOC]["backoff"]["failed"] is False


@pytest.mark.asyncio
async def test_session_persist_and_turn_count(store):
    assert await store.get_session(DOC) is None
    await store.set_session(DOC, "web_abc")
    assert (await store.get_session(DOC))["session_id"] == "web_abc"
    assert await store.bump_turn_count(DOC) == 1
    assert await store.bump_turn_count(DOC) == 2


@pytest.mark.asyncio
async def test_gc_drops_unwatched_documents(store):
    await store.mark_triggered("keep", ["k"])
    await store.mark_triggered("drop", ["k"])
    removed = await store.gc(["keep"])
    assert removed == ["drop"]
    snap = await store.snapshot()
    assert "keep" in snap and "drop" not in snap


@pytest.mark.asyncio
async def test_corrupt_state_file_does_not_crash(store, tmp_path):
    """A corrupt state file must not stop the watcher from starting; handling something
    twice is the better failure."""
    (tmp_path / "state.json").write_text("{ this is not json")
    assert await store.is_triggered(DOC, "k") is False
    await store.mark_triggered(DOC, ["k"])
    assert await store.is_triggered(DOC, "k") is True


@pytest.mark.asyncio
async def test_concurrent_coroutines_do_not_lose_updates(store):
    """Concurrent coroutines in one process: asyncio.Lock must serialize the
    read-modify-write.

    Measured without a lock, 78% of updates were lost across processes; coroutines are
    no different.
    """
    await asyncio.gather(*[store.mark_triggered(DOC, [f"k{i}"]) for i in range(50)])
    snap = await store.snapshot()
    assert len(snap[DOC]["triggered_ids"]) == 50


@pytest.mark.asyncio
async def test_legacy_dedup_keys_are_migrated_not_orphaned(tmp_path):
    """Changing the key format requires a migration, or every historical comment is
    re-dispatched as a new trigger.

    Measured cost: the first tick after an upgrade re-ran five historical comments and
    posted into a user's document. This class of defect appears only when a stateful
    component is upgraded -- test state files are always freshly created, so it never
    shows up locally.
    """
    import json

    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "doc-1": {"triggered_ids": {"c1:-": 1000.0, "c2:r9": 1000.0}}
    }), encoding="utf-8")

    store = CloudDocStore(path, now_fn=lambda: 1000.0)
    assert await store.is_triggered("doc-1", "clouddoc:doc-1:c1:-") is True
    assert await store.is_triggered("doc-1", "clouddoc:doc-1:c2:r9") is True


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """An already-migrated key must not be given a second prefix."""
    import json

    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "doc-1": {"triggered_ids": {"clouddoc:doc-1:c1:-": 1000.0}}
    }), encoding="utf-8")
    store = CloudDocStore(path, now_fn=lambda: 1000.0)
    await store.is_triggered("doc-1", "x")
    keys = list((await store.snapshot())["doc-1"]["triggered_ids"])
    assert keys == ["clouddoc:doc-1:c1:-"], keys


@pytest.mark.asyncio
async def test_a_corrupt_state_file_is_survivable_and_audible(tmp_path, caplog):
    """Survivable was already true and is the right call. Silent was not.

    Everything goes with the file -- dedup keys, which documents were seeded, the
    per-document failure counts. Nothing re-dispatches, because a document with no state
    reads as newly watched and is seeded rather than replayed, so the damage is to the
    record rather than to the documents. But a file that quietly becomes empty leaves
    whoever investigates later with nothing to find.

    The logging call itself is the second half of this test: ``cursor_store`` had no
    ``logger`` defined, exactly as ``google_provider`` did not, and a warning against a
    name that does not exist raises ``NameError`` from inside the handler meant to
    absorb the failure."""
    import logging

    path = tmp_path / "state.json"
    path.write_text("{truncated", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert await CloudDocStore(path=path).snapshot() == {}
    assert any("状态文件不可读" in r.message for r in caplog.records)

    caplog.clear()
    path.write_text("[]", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert await CloudDocStore(path=path).snapshot() == {}
    assert any("顶层不是对象" in r.message for r in caplog.records)
