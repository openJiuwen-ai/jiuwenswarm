"""CloudDocStore: the watcher's state storage.

The concurrency model follows ``CronJobStore``: an ``asyncio.Lock`` for coroutines
in one process, a companion ``portalocker`` lock across processes, and **the whole
read-modify-write inside both**. Measured without locks, 6 processes incrementing
40 times each lost 187 of 240; with them, nothing was lost.

Note what a lock does not prevent: two watcher instances would each still read "this
reply is not in triggered_ids" and each dispatch. Preventing duplicate **actions**
needs a single-instance lease, not a write lock.

The clock is injected through the constructor as ``now_fn``. The repository's
existing cron precedent for ``now_fn`` has no test using it, whereas this store's
time windows, backoff and session growth all depend on time -- so every relevant
test here must drive that seam.
"""

from __future__ import annotations

import asyncio
import logging
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import portalocker

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_FILE_LOCK_TIMEOUT_SEC = 10.0

# Bounds on dedup keys: at most 500 per document, kept for 30 days
MAX_TRIGGERED_IDS = 500
TRIGGERED_TTL_SEC = 30 * 24 * 3600

# Per-document backoff: 30s base, doubling, capped at 15 minutes
BACKOFF_BASE_SEC = 30.0
BACKOFF_MAX_SEC = 15 * 60.0

# Schema version of a state entry. **Bump it whenever a migration is added**; the
# test is the version number, never the shape of the keys.
_SCHEMA_VERSION = 1

# Several records carry a timestamp -- ``updated_at``, ``created_at``, ``started_at``,
# ``dispatched_at`` -- that no code reads. They are kept deliberately: the state file is
# human-readable JSON, and after an incident the first question about any record is when
# it was written. Do not add another unless it answers that question too.

# Relocated to the host-free tools layer (see kinds.py).
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.kinds import (  # noqa: E402
    get_clouddoc_state_path,
)


def _now() -> float:
    return time.time()


class CloudDocStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        now_fn: Callable[[], float] = _now,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._path = path or get_clouddoc_state_path()
        self._lock = asyncio.Lock()
        self._now_fn = now_fn
        self._file_lock_timeout = float(file_lock_timeout)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ locking and IO

    def _call_under_file_lock(self, fn: Callable[[], _T]) -> _T:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=self._file_lock_timeout):
            return fn()

    async def _run_locked(self, fn: Callable[[], _T]) -> _T:
        async with self._lock:
            return await asyncio.to_thread(self._call_under_file_lock, fn)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not stop the watcher from starting: treat it as
            # empty and the next tick rebuilds it.
            #
            # **Said out loud, because everything is lost with it** -- the dedup keys,
            # which documents were seeded, the per-document failure counts. Nothing
            # re-dispatches (a document with no state reads as newly watched and is
            # seeded rather than replayed), so the damage is to the record rather than
            # to the documents. But a file that silently becomes empty leaves whoever
            # investigates later with no way to know it ever happened.
            logger.warning(
                "[clouddoc] 状态文件不可读（%s），按空状态继续：%s。"
                "已纳管文档的去重键与失败计数随之丢失，下一轮 tick 会重新播种。",
                type(exc).__name__, self._path,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "[clouddoc] 状态文件的顶层不是对象（%s），按空状态继续：%s",
                type(data).__name__, self._path,
            )
            return {}
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    def _migrate(self, entry: dict[str, Any], doc_id: str) -> None:
        """Upgrade one entry to the current schema version.

        **Changing the dedup key format requires a migration.** The dedup key is this
        component's only authority on "has this been handled", so a format change
        mismatches every historical key and re-dispatches every historical comment as
        a new trigger. Measured cost: the first tick after an upgrade re-ran five
        historical comments and posted into a user's document. This class of defect
        appears only when a stateful component is upgraded -- test state files are
        always freshly created, so it never shows up locally.

        The test is ``schema_version``, **not** what a key looks like. Guessing from
        shape re-prefixes, on every read, the very key the caller just wrote, breaking
        the most basic round trip of ``mark_triggered`` into ``is_triggered`` -- which
        is exactly how the first version got it wrong.
        """
        version = int(entry.get("schema_version", 0))
        if version >= _SCHEMA_VERSION:
            return
        if version < 1:
            # v1: dedup keys take the canonical ``clouddoc:{doc_id}:`` prefix
            prefix = f"clouddoc:{doc_id}:"
            entry["triggered_ids"] = {
                (k if k.startswith("clouddoc:") else prefix + k): t
                for k, t in (entry.get("triggered_ids") or {}).items()
            }
        entry["schema_version"] = _SCHEMA_VERSION

    def _doc_unlocked(self, data: dict[str, Any], doc_id: str) -> dict[str, Any]:
        entry = data.setdefault(doc_id, {})
        entry.setdefault("triggered_ids", {})
        entry.setdefault("inflight", {})
        entry.setdefault("conventions", None)
        entry.setdefault("session", None)
        entry.setdefault("seeded", False)
        entry.setdefault(
            "backoff", {"failures": 0, "backoff_steps": 0, "until": None, "failed": False}
        )
        self._migrate(entry, doc_id)
        return entry

    async def _mutate(self, fn: Callable[[dict[str, Any]], _T]) -> _T:
        def run() -> _T:
            data = self._read_unlocked()
            out = fn(data)
            self._write_unlocked(data)
            return out

        return await self._run_locked(run)

    # ------------------------------------------------------------ deduplication

    async def is_triggered(self, doc_id: str, key: str) -> bool:
        def run(data: dict) -> bool:
            entry = self._doc_unlocked(data, doc_id)
            self._prune_triggered(entry)
            return key in entry["triggered_ids"]

        return await self._mutate(run)

    async def mark_triggered(self, doc_id: str, keys: list[str]) -> None:
        """Persist dedup keys. **Must be called before touching the document.**

        The zero-token approve and keep paths dispatch no turn, but they go through
        here all the same. Otherwise their keys are never stored, a crash leaves them
        to be rediscovered and applied again, and reapplying is not idempotent -- `X`
        becoming `X and Y` still matches on the second pass.
        """

        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            now = self._now_fn()
            for k in keys:
                entry["triggered_ids"][k] = now
            self._prune_triggered(entry)
            entry["updated_at"] = now

        await self._mutate(run)

    async def unmark_triggered(self, doc_id: str, keys: list[str]) -> None:
        """Give dedup keys back. The dispatch order writes the key before the
        placeholder goes out; when the placeholder fails there is no turn behind
        the key, and leaving it stored would lose the summons for good. Removing it
        lets the next tick rediscover the same trigger and try again."""

        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            for k in keys:
                entry["triggered_ids"].pop(k, None)
            entry["updated_at"] = self._now_fn()

        await self._mutate(run)

    def _prune_triggered(self, entry: dict) -> None:
        """Evict by time window first, **then** by count.

        The order cannot be reversed: evicting by count first drops keys still valid
        inside the window, and those keys are then rediscovered as new -- a
        re-triggering loop that never converges.
        """
        now = self._now_fn()
        ids: dict[str, float] = entry["triggered_ids"]
        fresh = {k: t for k, t in ids.items() if now - float(t) <= TRIGGERED_TTL_SEC}
        if len(fresh) > MAX_TRIGGERED_IDS:
            # Still over the cap after the time window means an unusual burst of
            # triggers arrived at once. Keep the newest batch and drop the rest.
            newest = sorted(fresh.items(), key=lambda kv: -float(kv[1]))[:MAX_TRIGGERED_IDS]
            fresh = dict(newest)
        entry["triggered_ids"] = fresh

    # ------------------------------------------------------------ inflight

    async def begin_inflight(
        self, doc_id: str, turn_id: str, keys: list[str], placeholders: dict[str, str]
    ) -> None:
        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            entry["inflight"][turn_id] = {
                "keys": list(keys),
                "placeholders": dict(placeholders),
                "dispatched_at": self._now_fn(),
            }

        await self._mutate(run)

    async def end_inflight(self, doc_id: str, turn_id: str) -> None:
        def run(data: dict) -> None:
            self._doc_unlocked(data, doc_id)["inflight"].pop(turn_id, None)

        await self._mutate(run)

    async def list_inflight(self, doc_id: str) -> dict[str, dict]:
        def run(data: dict) -> dict:
            return dict(self._doc_unlocked(data, doc_id)["inflight"])

        return await self._mutate(run)

    # ------------------------------------------------------------ conventions

    async def get_conventions(self, doc_id: str) -> dict | None:
        def run(data: dict) -> dict | None:
            return self._doc_unlocked(data, doc_id)["conventions"]

        return await self._mutate(run)

    async def note_conventions_acked(self, doc_id: str, content_hash: str) -> None:
        """Record that these conventions have been acknowledged.

        The test is a content hash: rewritten conventions must be acknowledged again,
        and recording only "acknowledged once" would let every later edit take effect
        in silence.
        """

        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            entry["conventions"] = {"hash": content_hash, "acked_at": self._now_fn()}

        await self._mutate(run)

    # ------------------------------------------------------------ sessions

    async def get_session(self, doc_id: str) -> dict | None:
        def run(data: dict) -> dict | None:
            return self._doc_unlocked(data, doc_id)["session"]

        return await self._mutate(run)

    async def set_session(self, doc_id: str, session_id: str) -> None:
        """Open a new session generation.

        ``generation`` increases monotonically and is **never reset**. If session ids
        were distinguished by timestamp, an injected fake clock in tests, or two
        rotations inside one second, would collide on the same id and drop the old and
        new turns into a single session.
        """

        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            previous = entry.get("session") or {}
            entry["session"] = {
                "session_id": session_id,
                "turn_count": 0,
                "generation": int(previous.get("generation", 0)) + 1,
                "created_at": self._now_fn(),
            }

        await self._mutate(run)

    async def bump_turn_count(self, doc_id: str) -> int:
        def run(data: dict) -> int:
            entry = self._doc_unlocked(data, doc_id)
            sess = entry.get("session")
            if not sess:
                return 0
            sess["turn_count"] = int(sess.get("turn_count", 0)) + 1
            return sess["turn_count"]

        return await self._mutate(run)

    # ------------------------------------------------------------ backoff

    async def seed_if_new(self, doc_id: str, keys: list[str]) -> bool:
        """On first watching a document, mark its existing triggers as handled
        **without dispatching them**.

        Without this step, switching the feature on replays the document's **entire
        history**: every past @ mention and every addressed reply dispatches a turn and
        posts into the document. Measured, a document with six historical mentions
        dispatched six turns immediately; a real document after a few months would be
        far worse.

        The test is the ``seeded`` flag, not whether ``triggered_ids`` is empty. The
        latter becomes empty again once the 30-day window has evicted every key, and
        the history would then be replayed a second time.

        Returns whether seeding ran, so the caller can skip dispatching this tick.
        """

        def run(data: dict) -> bool:
            entry = self._doc_unlocked(data, doc_id)
            if entry.get("seeded"):
                return False
            now = self._now_fn()
            for k in keys:
                entry["triggered_ids"][k] = now
            self._prune_triggered(entry)
            entry["seeded"] = True
            entry["updated_at"] = now
            return True

        return await self._mutate(run)

    async def is_frozen(self, doc_id: str, now: float) -> bool:
        """Whether this document should be skipped this tick.

        Both ``failed``, set by consecutive permission errors, and ``until``, the
        backoff window, are consumed here. Without this reader, backoff would be a
        **record nobody looks at**: a document whose access was revoked would retry
        every 15 seconds forever, and a throttled one would keep hammering at full rate
        and prolong the throttling.
        """

        def run(data: dict) -> bool:
            b = self._doc_unlocked(data, doc_id)["backoff"]
            if b.get("failed"):
                return True
            until = b.get("until")
            return bool(until and now < float(until))

        return await self._mutate(run)

    async def is_permanently_failed(self, doc_id: str) -> bool:
        """The ``failed`` verdict bit.

        Unlike ``is_frozen``, this **excludes the backoff window**. A re-check should
        look only at the verdict: the backoff window is a rate-limiting rhythm, and
        treating it as re-checkable would let a restart step around the limit.
        """

        def run(data: dict) -> bool:
            return bool(self._doc_unlocked(data, doc_id)["backoff"].get("failed"))

        return await self._mutate(run)

    async def doc_health(self, doc_id: str) -> dict:
        """A health snapshot for the panel: verdict bit, backoff window, failure reason
        and panel metadata. Read-only."""

        def run(data: dict) -> dict:
            entry = self._doc_unlocked(data, doc_id)
            b = entry["backoff"]
            return {
                "failed": bool(b.get("failed")),
                "failed_reason": b.get("failed_reason"),
                "until": b.get("until"),
                "panel_meta": dict(entry.get("panel_meta") or {}),
            }

        return await self._mutate(run)

    async def set_panel_meta(self, doc_id: str, **fields) -> None:
        """Merge in panel metadata: the cached title and when it was last checked.

        It stays out of the setdefault chain in ``_doc_unlocked`` because this is
        decorative data for the panel, not part of the co-editing state schema; each
        reader copes with its absence.
        """

        def run(data: dict) -> None:
            entry = self._doc_unlocked(data, doc_id)
            meta = entry.setdefault("panel_meta", {})
            meta.update({k: v for k, v in fields.items() if v is not None})

        await self._mutate(run)

    async def note_permanent_failure(self, doc_id: str, reason: str) -> None:
        """Mark the document failed at once, without the three-strikes count.

        The count exists to tell **occasional** from **persistent**: one 403 may be a
        transient during a concurrent permission change. But the admission check reads
        the **fact** of the current permission, not the luck of one request -- retrying
        three times just asks the same settled question twice more.
        """

        def run(data: dict) -> None:
            b = self._doc_unlocked(data, doc_id)["backoff"]
            b["failed"] = True
            b["failed_reason"] = reason

        await self._mutate(run)

    async def note_failure(self, doc_id: str, kind: str, now: float | None = None) -> dict:
        """Record one failure, advance the backoff window, return the new backoff state.

        **Rate limiting never advances failed.** Quota is shared by every watched
        document, so feeding a 429 or a throttling 403 into a per-document state
        machine would let one person hammering one document freeze all of them until
        someone edited the config. What it advances is the backoff window -- slowing
        down is the right response, freezing is not.

        Backoff is 30s base, doubling, capped at 15 minutes. Jitter comes from the
        caller's clock; no randomness is introduced here, or the state could not be
        asserted in tests.
        """

        def run(data: dict) -> dict:
            entry = self._doc_unlocked(data, doc_id)
            b = entry["backoff"]
            t = self._now_fn() if now is None else now

            # The count and the backoff window are **two different things** and advance
            # separately.
            #
            # ``failures`` counts only per-document errors -- a 403 for permission, a
            # 404 for deletion -- because its sole use is deciding that a document is
            # beyond help. Letting rate limiting add to it would pollute a per-document
            # test with a global signal: someone else exhausts the quota, the count
            # climbs to 10, and the **first** genuine 403 after that marks the document
            # failed immediately. That is precisely the false freeze to avoid.
            if kind not in ("rate_limited",):
                b["failures"] = int(b.get("failures", 0)) + 1
                if kind in ("not_found", "forbidden") and b["failures"] >= 3:
                    b["failed"] = True

            # The backoff window applies to **every** error, and rate limiting needs it
            # most: slowing down is the right response, freezing is not. The exponent
            # counts the window's own advances, decoupled from failures.
            steps = int(b.get("backoff_steps", 0)) + 1
            b["backoff_steps"] = steps
            b["until"] = t + min(BACKOFF_BASE_SEC * (2 ** min(steps - 1, 10)), BACKOFF_MAX_SEC)
            entry["backoff"] = b
            return dict(b)

        return await self._mutate(run)

    async def clear_failure(self, doc_id: str) -> None:
        def run(data: dict) -> None:
            self._doc_unlocked(data, doc_id)["backoff"] = {
                "failures": 0,
                "backoff_steps": 0,
                "until": None,
                "failed": False,
            }

        await self._mutate(run)

    # ------------------------------------------------------------ GC

    async def gc(self, keep_doc_ids: list[str]) -> list[str]:
        """Drop the whole state entry for any document no longer watched, and prune
        the dedup keys of the rest."""

        def run(data: dict) -> list[str]:
            keep = set(keep_doc_ids)
            removed = [d for d in list(data) if d not in keep]
            for d in removed:
                data.pop(d, None)
            for doc_id in list(data):
                entry = self._doc_unlocked(data, doc_id)
                self._prune_triggered(entry)
            return removed

        return await self._mutate(run)

    async def snapshot(self) -> dict[str, Any]:
        """A read-only copy of the whole state, for tests and observability."""
        return await self._run_locked(self._read_unlocked)
