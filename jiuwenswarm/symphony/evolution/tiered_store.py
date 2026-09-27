# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tiered evolution store for Symphony experience reuse.

Before the September refactor the repository kept reusable experience in
``symphony/experience/bank.py`` (``ExperienceBank`` + ``ExperienceItem``) with
a single FAISS index.  Upstream removed that package: the runtime evolution
layer now lives in :mod:`jiuwenswarm.symphony.evolution` and no longer exposes
those classes.  This module re-implements the *tiered* idea as a
self-contained store that depends only on the standard library, with an
optional SQLite mirror for the durable (long-term) tier.

Three tiers model how experience ages and is reused:

``short``
    Session-scoped, cheapest to hit, ages out after ``SHORT_TTL_SEC``.
``mid``
    Task-scoped, ages out after ``MID_TTL_SEC``; promoted to ``long`` once it
    has been retrieved ``PROMOTION_HIT_THRESHOLD`` times.
``long``
    Cross-task durable tier; the only tier optionally persisted to SQLite.

Records are plain ``dict`` payloads keyed by ``record_id``.  The store never
touches the network and never imports ``openjiuwen`` so it can run inside any
evolution hook or unit test.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

LOGGER = logging.getLogger(__name__)

# -- tiers ------------------------------------------------------------------
TIER_SHORT = "short"  # session-scoped, TTL < 1h
TIER_MID = "mid"  # task-scoped, TTL < 7d
TIER_LONG = "long"  # cross-task durable
TIERS: tuple[str, ...] = (TIER_SHORT, TIER_MID, TIER_LONG)

# -- defaults ---------------------------------------------------------------
SHORT_TTL_SEC = 3600  # 1 hour
MID_TTL_SEC = 7 * 86400  # 7 days
PROMOTION_HIT_THRESHOLD = 3  # mid records retrieved this often become long
DEFAULT_TASK_TYPE = "default"

# Text keys consulted (in order) when matching a record against a query.
_SEARCH_KEYS: tuple[str, ...] = ("query_pattern", "query", "text", "title", "summary")

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiered_records (
    record_id    TEXT PRIMARY KEY,
    tier         TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    deposited_at REAL NOT NULL,
    last_hit_at  REAL NOT NULL
)
"""


@dataclass
class TieredRecord:
    """A stored experience record together with its tier bookkeeping."""

    record_id: str
    payload: dict[str, Any]
    tier: str = TIER_SHORT
    task_type: str = DEFAULT_TASK_TYPE
    hit_count: int = 0
    deposited_at: float = field(default_factory=time.time)
    last_hit_at: float = field(default_factory=time.time)

    def age_sec(self, now: float) -> float:
        """Seconds elapsed since this record was deposited."""

        return max(0.0, now - self.deposited_at)

    def touch(self, now: float) -> None:
        """Record one retrieval hit."""

        self.hit_count += 1
        self.last_hit_at = now

    def searchable_text(self) -> str:
        """Return the lower-cased text used for substring matching."""

        for key in _SEARCH_KEYS:
            value = self.payload.get(key)
            if isinstance(value, str) and value:
                return value.lower()
        return ""

    def matches(self, query_text: str) -> bool:
        """Whether ``query_text`` (case-insensitive) occurs in the record."""

        needle = (query_text or "").strip().lower()
        if not needle:
            return False
        return needle in self.searchable_text()

    def is_promotable(self, threshold: int = PROMOTION_HIT_THRESHOLD) -> bool:
        """Whether a ``mid`` record has earned promotion to ``long``."""

        return self.tier == TIER_MID and self.hit_count >= threshold

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot (used by the SQLite mirror)."""

        return {
            "record_id": self.record_id,
            "tier": self.tier,
            "task_type": self.task_type,
            "payload": self.payload,
            "hit_count": self.hit_count,
            "deposited_at": self.deposited_at,
            "last_hit_at": self.last_hit_at,
        }


class TieredEvolutionStore:
    """Three-tier experience store with TTL aging and hit-based promotion.

    Args:
        sqlite_path: Optional path for a durable SQLite mirror of the ``long``
            tier.  When ``None`` (the default) the store is memory-only.
        short_ttl_sec: Age after which a ``short`` record degrades to ``mid``.
        mid_ttl_sec: Age after which a non-promotable ``mid`` record is dropped.
        promotion_hit_threshold: Hits required to promote ``mid`` -> ``long``.
        clock: Injectable time source (seconds); defaults to :func:`time.time`.
            Tests pass a fake clock to exercise TTL boundaries deterministically.
    """

    def __init__(
        self,
        *,
        sqlite_path: str | Path | None = None,
        short_ttl_sec: float = SHORT_TTL_SEC,
        mid_ttl_sec: float = MID_TTL_SEC,
        promotion_hit_threshold: int = PROMOTION_HIT_THRESHOLD,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._short_ttl_sec = float(short_ttl_sec)
        self._mid_ttl_sec = float(mid_ttl_sec)
        self._promotion_hit_threshold = int(promotion_hit_threshold)

        self._tiers: dict[str, dict[str, TieredRecord]] = {
            TIER_SHORT: {},
            TIER_MID: {},
            TIER_LONG: {},
        }
        self._total_deposits = 0
        self._total_hits = 0

        # The agent framework touches the store from multiple worker threads, so
        # every public operation is serialised by a single lock (the SQLite
        # connection is opened with ``check_same_thread=False`` to match).
        self._lock = threading.Lock()

        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._conn: sqlite3.Connection | None = None
        if self._sqlite_path is not None:
            self._open_sqlite()

    # -- core API -----------------------------------------------------------

    def upsert(
        self,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        tier: str = TIER_SHORT,
        task_type: str = DEFAULT_TASK_TYPE,
    ) -> TieredRecord:
        """Insert or replace a record, moving it to ``tier`` if it moved.

        Args:
            record_id: Stable identifier; re-using one replaces the record.
            payload: Arbitrary JSON-serialisable experience payload.
            tier: One of :data:`TIERS`.
            task_type: Logical grouping used to scope ``mid`` lookups.

        Returns:
            The stored :class:`TieredRecord`.

        Raises:
            ValueError: If ``tier`` is not a known tier.
        """

        if tier not in TIERS:
            raise ValueError(f"unknown tier: {tier!r}")

        with self._lock:
            now = self._clock()
            record = TieredRecord(
                record_id=str(record_id),
                payload=dict(payload),
                tier=tier,
                task_type=str(task_type or DEFAULT_TASK_TYPE),
                hit_count=0,
                deposited_at=now,
                last_hit_at=now,
            )
            self._remove(record.record_id)
            self._tiers[tier][record.record_id] = record
            self._total_deposits += 1
            if tier == TIER_LONG:
                self._persist_long(record)
            return record

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 3,
        task_type: str | None = None,
    ) -> list[TieredRecord]:
        """Retrieve records by substring match, short -> mid -> long.

        Results are returned in tier-priority order; each returned record has
        its hit counter incremented (which is what eventually promotes ``mid``
        records to ``long``).

        Args:
            query_text: Case-insensitive substring to match.
            top_k: Maximum number of records to return.
            task_type: When given, restricts the ``mid`` tier to this group.

        Returns:
            Up to ``top_k`` matching records (possibly empty).
        """

        if top_k <= 0 or not (query_text or "").strip():
            return []

        with self._lock:
            now = self._clock()
            results: list[TieredRecord] = []
            for tier in (TIER_SHORT, TIER_MID, TIER_LONG):
                for record in self._tiers[tier].values():
                    if (
                        task_type is not None
                        and tier == TIER_MID
                        and record.task_type != task_type
                    ):
                        continue
                    if not record.matches(query_text):
                        continue
                    record.touch(now)
                    self._total_hits += 1
                    if record.tier == TIER_LONG:
                        self._persist_long(record)
                    results.append(record)
                    if len(results) >= top_k:
                        return results
            return results

    def promote(self) -> list[str]:
        """Move every promotable ``mid`` record into the ``long`` tier.

        Returns:
            The ids that were promoted.
        """

        with self._lock:
            return self._promote_locked()

    def _promote_locked(self) -> list[str]:
        """``promote`` body; the caller must already hold :attr:`_lock`."""

        promoted: list[str] = []
        for record_id, record in list(self._tiers[TIER_MID].items()):
            if not record.is_promotable(self._promotion_hit_threshold):
                continue
            del self._tiers[TIER_MID][record_id]
            record.tier = TIER_LONG
            self._tiers[TIER_LONG][record_id] = record
            self._persist_long(record)
            promoted.append(record_id)
        return promoted

    def decay(self) -> dict[str, list[str]]:
        """Age records: degrade expired ``short``, drop expired ``mid``.

        ``mid`` records that are expired *but* promotable are promoted instead
        of dropped.

        Returns:
            Mapping with ``degraded`` (short -> mid), ``promoted`` (mid ->
            long) and ``dropped`` (expired mid) id lists.
        """

        with self._lock:
            now = self._clock()
            degraded: list[str] = []
            dropped: list[str] = []

            for record_id, record in list(self._tiers[TIER_SHORT].items()):
                if record.age_sec(now) > self._short_ttl_sec:
                    del self._tiers[TIER_SHORT][record_id]
                    record.tier = TIER_MID
                    self._tiers[TIER_MID][record_id] = record
                    degraded.append(record_id)

            for record_id, record in list(self._tiers[TIER_MID].items()):
                if record.age_sec(now) <= self._mid_ttl_sec:
                    continue
                if record.is_promotable(self._promotion_hit_threshold):
                    continue  # handled by the promotion pass below
                del self._tiers[TIER_MID][record_id]
                dropped.append(record_id)

            promoted = self._promote_locked()
            return {"degraded": degraded, "promoted": promoted, "dropped": dropped}

    # -- introspection ------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return per-tier counts and deposit/hit totals."""

        with self._lock:
            return {
                "short_term_count": len(self._tiers[TIER_SHORT]),
                "mid_term_count": len(self._tiers[TIER_MID]),
                "long_term_count": len(self._tiers[TIER_LONG]),
                "total_deposits": self._total_deposits,
                "total_hits": self._total_hits,
                "promotion_threshold": self._promotion_hit_threshold,
            }

    def get(self, record_id: str) -> TieredRecord | None:
        """Return a record by id from any tier, or ``None``."""

        with self._lock:
            for tier in TIERS:
                record = self._tiers[tier].get(str(record_id))
                if record is not None:
                    return record
            return None

    # -- internals ----------------------------------------------------------

    def _remove(self, record_id: str) -> None:
        for tier in TIERS:
            if self._tiers[tier].pop(record_id, None) is not None:
                if tier == TIER_LONG:
                    self._delete_long(record_id)
                return

    # -- optional SQLite mirror (long tier only) ----------------------------

    def _open_sqlite(self) -> None:
        assert self._sqlite_path is not None
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the connection be shared across the
        # agent framework's worker threads; concurrent access is serialised by
        # ``self._lock`` instead of SQLite's per-thread guard.  WAL mode gives
        # better cross-thread read/write concurrency than the default rollback
        # journal.
        self._conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SQLITE_SCHEMA)
        self._conn.commit()
        self._load_long()

    def _persist_long(self, record: TieredRecord) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO tiered_records "
            "(record_id, tier, task_type, payload, hit_count, deposited_at, last_hit_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.tier,
                record.task_type,
                json.dumps(record.payload, ensure_ascii=False, default=str),
                record.hit_count,
                record.deposited_at,
                record.last_hit_at,
            ),
        )
        self._conn.commit()

    def _delete_long(self, record_id: str) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "DELETE FROM tiered_records WHERE record_id = ?", (record_id,)
        )
        self._conn.commit()

    def _load_long(self) -> None:
        if self._conn is None:
            return
        rows = self._conn.execute(
            "SELECT record_id, task_type, payload, hit_count, deposited_at, last_hit_at "
            "FROM tiered_records"
        ).fetchall()
        for record_id, task_type, payload, hit_count, deposited_at, last_hit_at in rows:
            try:
                decoded = json.loads(payload)
            except (TypeError, ValueError):
                continue
            self._tiers[TIER_LONG][record_id] = TieredRecord(
                record_id=record_id,
                payload=decoded if isinstance(decoded, dict) else {},
                tier=TIER_LONG,
                task_type=task_type,
                hit_count=int(hit_count),
                deposited_at=float(deposited_at),
                last_hit_at=float(last_hit_at),
            )

    def close(self) -> None:
        """Close the SQLite mirror if one is open."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "MID_TTL_SEC",
    "PROMOTION_HIT_THRESHOLD",
    "SHORT_TTL_SEC",
    "TIERS",
    "TIER_LONG",
    "TIER_MID",
    "TIER_SHORT",
    "TieredEvolutionStore",
    "TieredRecord",
]
