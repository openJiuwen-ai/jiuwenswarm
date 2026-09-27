"""Unit tests for the tiered evolution store.

Covers the four core operations (upsert/query/promote/decay), tier priority,
TTL boundaries via an injected clock, error handling and the optional SQLite
mirror.
"""

from __future__ import annotations

import threading

import pytest

from jiuwenswarm.symphony.evolution.tiered_store import (
    MID_TTL_SEC,
    PROMOTION_HIT_THRESHOLD,
    SHORT_TTL_SEC,
    TIER_LONG,
    TIER_MID,
    TIER_SHORT,
    TieredEvolutionStore,
    TieredRecord,
)


class _FakeClock:
    """Deterministic, advanceable clock for TTL boundary tests."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _record(text: str) -> dict:
    return {"query_pattern": text}


def test_upsert_and_query_increments_hit_count():
    store = TieredEvolutionStore()
    record = store.upsert("a", _record("paper writing"), tier=TIER_SHORT)

    assert isinstance(record, TieredRecord)
    assert record.tier == TIER_SHORT

    hits = store.query("paper")
    assert [item.record_id for item in hits] == ["a"]
    assert hits[0].hit_count == 1

    store.query("PAPER")  # case-insensitive
    stored = store.get("a")
    assert stored is not None
    assert stored.hit_count == 2


def test_query_returns_tier_priority_order_and_respects_top_k():
    store = TieredEvolutionStore()
    store.upsert("s", _record("alpha"), tier=TIER_SHORT)
    store.upsert("m", _record("alpha"), tier=TIER_MID)
    store.upsert("l", _record("alpha"), tier=TIER_LONG)

    assert [r.record_id for r in store.query("alpha", top_k=3)] == ["s", "m", "l"]
    assert [r.record_id for r in store.query("alpha", top_k=1)] == ["s"]


def test_query_boundaries_return_empty():
    store = TieredEvolutionStore()
    store.upsert("a", _record("alpha"), tier=TIER_SHORT)

    assert store.query("") == []
    assert store.query("   ") == []
    assert store.query("alpha", top_k=0) == []
    assert store.query("alpha", top_k=-1) == []
    assert store.query("missing") == []


def test_upsert_rejects_unknown_tier():
    store = TieredEvolutionStore()
    with pytest.raises(ValueError):
        store.upsert("x", _record("nope"), tier="forever")


def test_upsert_replaces_and_moves_between_tiers():
    store = TieredEvolutionStore()
    store.upsert("a", _record("one"), tier=TIER_SHORT)
    store.upsert("a", _record("two"), tier=TIER_LONG)

    record = store.get("a")
    assert record is not None
    assert record.tier == TIER_LONG
    assert record.payload["query_pattern"] == "two"
    assert store.stats()["short_term_count"] == 0
    assert store.stats()["long_term_count"] == 1


def test_promote_moves_hot_mid_records_to_long():
    store = TieredEvolutionStore()
    store.upsert("m", _record("beta"), tier=TIER_MID)

    for _ in range(PROMOTION_HIT_THRESHOLD):
        store.query("beta")

    stored = store.get("m")
    assert stored is not None
    assert stored.is_promotable(PROMOTION_HIT_THRESHOLD)
    assert store.promote() == ["m"]
    assert stored.tier == TIER_LONG
    # a second promote is a no-op
    assert store.promote() == []


def test_decay_degrades_expired_short_to_mid():
    clock = _FakeClock()
    store = TieredEvolutionStore(clock=clock)
    store.upsert("s", _record("gamma"), tier=TIER_SHORT)

    clock.advance(SHORT_TTL_SEC + 1)
    result = store.decay()

    assert result["degraded"] == ["s"]
    degraded = store.get("s")
    assert degraded is not None
    assert degraded.tier == TIER_MID


def test_decay_drops_expired_cold_mid_records():
    clock = _FakeClock()
    store = TieredEvolutionStore(clock=clock)
    store.upsert("m", _record("delta"), tier=TIER_MID)

    clock.advance(MID_TTL_SEC + 1)
    result = store.decay()

    assert result["dropped"] == ["m"]
    assert store.get("m") is None


def test_decay_promotes_expired_but_hot_mid_records():
    clock = _FakeClock()
    store = TieredEvolutionStore(clock=clock)
    store.upsert("m", _record("epsilon"), tier=TIER_MID)

    for _ in range(PROMOTION_HIT_THRESHOLD):
        store.query("epsilon")

    clock.advance(MID_TTL_SEC + 1)
    result = store.decay()

    assert result["promoted"] == ["m"]
    assert result["dropped"] == []
    promoted = store.get("m")
    assert promoted is not None
    assert promoted.tier == TIER_LONG


def test_stats_reports_tier_counts():
    store = TieredEvolutionStore()
    store.upsert("a", _record("a"), tier=TIER_SHORT)
    store.upsert("b", _record("b"), tier=TIER_MID)
    store.upsert("c", _record("c"), tier=TIER_LONG)

    stats = store.stats()
    assert stats["short_term_count"] == 1
    assert stats["mid_term_count"] == 1
    assert stats["long_term_count"] == 1
    assert stats["total_deposits"] == 3


def test_sqlite_mirror_round_trips_long_tier(tmp_path):
    db_path = tmp_path / "tiered.sqlite3"

    with TieredEvolutionStore(sqlite_path=db_path) as store:
        store.upsert("l", _record("zeta"), tier=TIER_LONG)
        # short-tier records are not persisted
        store.upsert("s", _record("zeta"), tier=TIER_SHORT)

    reopened = TieredEvolutionStore(sqlite_path=db_path)
    try:
        record = reopened.get("l")
        assert record is not None
        assert record.tier == TIER_LONG
        assert record.payload["query_pattern"] == "zeta"
        assert reopened.get("s") is None
        assert [r.record_id for r in reopened.query("zeta")] == ["l"]
    finally:
        reopened.close()


def test_cross_thread_access_does_not_raise(tmp_path):
    # The agent framework touches the store from worker threads; the SQLite
    # connection is opened with check_same_thread=False and every operation is
    # serialised by a lock, so concurrent upsert/query must not raise.
    store = TieredEvolutionStore(sqlite_path=tmp_path / "threaded.sqlite3")
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for i in range(20):
                store.upsert(f"r{worker_id}-{i}", _record("paper"), tier=TIER_LONG)
                store.query("paper")
        except Exception as exc:  # noqa: BLE001 - collect any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == []
        assert store.stats()["long_term_count"] == 80
    finally:
        store.close()
