"""The standing-mandate registry (PR2b, D2/D3/D9).

One entry per (document, agent-connection): the deployment owner's standing
delegation for that document. There is exactly **one** level -- ``apply_scoped``
(bounded direct edits) -- so a watch is a binary: off, or on at that level. No
entry means no delegation: adoption alone dispatches nothing (D2).

``reply_only`` used to sit below it as a "safer" tier. It was not one. Replying
in a thread needs no mandate at all: an unauthorised @ still gets an answer
("I have no mandate for this document"), written with the service account's own
platform rights. So that tier delegated not *replying* but *running an
unattended turn for this document*, named after its toolset rather than its
purpose -- and with the propose-then-approve loop gone (the unified edit flow),
a turn that can only write prose into a thread had nowhere to go. The strictest
fallback is **not dispatching**, not dispatching with a narrower toolset.

``suspend`` used to sit beside them as a third state. It was a pause nobody
could name from the outside: a suspended watch answered a collaborator with
"recorded, not scheduled this round" forever, and the owner had two ways to say
no with different memories. The state space is now **off / on**, plus the one
state the system produces on its own, **expired**. Entries left suspended by an
earlier release become tombstones (see ``_retire_suspended``) -- deleting the
check without them would have turned every paused watch back on in silence.

Storage follows the cursor-store precedent: one JSON file under the workspace
config dir, guarded by a portalocker file lock, so the gateway (dispatch gate)
and the agentserver (pre-write checkpoint, IC-3) read the same truth without a
private channel. Terms are snapshotted into the turn payload at dispatch; the
pre-write checkpoint reads revocation and expiry live, and holds the entry to
the tier the turn was dispatched under, so a change intercepts an in-flight
write rather than draining. Budget and ``from`` still drain.

Every lifecycle event — grant / modify / revoke / expire / dispatch / deny —
appends one line to the audit journal (D3: one event, one audit line). The
panel's off switch is a revocation, not an event of its own: turning a
document's watch off *is* revoking it, so it lands in the same place under the
same name.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import portalocker

logger = logging.getLogger(__name__)

# E1: standing authority decays by default. A watch issued without an explicit
# term expires after this many seconds; "permanent" exists but only as the
# owner's explicit word (expires_at=None passed on purpose), never as the
# silence of a default parameter.
DEFAULT_WATCH_TTL_SECONDS = 30 * 24 * 3600

# The rolling-window loop brake (§16.14). Generous enough that a lively document with
# several live threads never trips it, tight enough that a runaway pointer loop stops
# within one poll cycle rather than exhausting a day's budget first.
DEFAULT_DISPATCH_RATE_MAX = 10
DEFAULT_DISPATCH_RATE_WINDOW_SECONDS = 120.0

# Distinguishes "caller said nothing" (default term) from "caller said None"
# (permanent). A plain None default cannot carry both meanings.
_UNSET: Any = object()

MODES = ("apply_scoped",)

# Levels that existed in an earlier release and no longer do. A ledger on disk
# outlives the code that wrote it, so entries at a retired level are still out
# there. They are retired as **tombstones** (see ``_retire_unknown_modes``);
# promoting them to the surviving level would be a silent widening of authority
# the owner never signed for.
RETIRED_MODES = ("reply_only",)

# Why an entry was tombstoned by a migration rather than by a person. Both are
# ordinary revocations everywhere else -- the reason is what lets the audit view
# say "this turned off because the release changed", not "you turned it off".
RETIRED_TIER_REASON = "retired_tier"
SUSPEND_RETIRED_REASON = "suspend_retired"

_LOCK_TIMEOUT_S = 10.0


def get_watch_registry_path() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir() / "config" / "clouddoc-watches.json"


@dataclass(frozen=True)
class WatchVerdict:
    """The dispatch gate's answer for one document at one instant."""

    dispatchable: bool
    mode: str | None
    reason: str  # "ok" | "no_watch" | "expired" | "over_budget" | "rate_limited"


class WatchRegistry:
    def __init__(
        self,
        path: Path | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
        rate_max: int = DEFAULT_DISPATCH_RATE_MAX,
        rate_window_seconds: float = DEFAULT_DISPATCH_RATE_WINDOW_SECONDS,
    ) -> None:
        self._path = path or get_watch_registry_path()
        self._audit_path = self._path.with_name(self._path.stem + "-audit.jsonl")
        # Audit lines raised inside a mutation are held here and appended only
        # after the state file has been replaced; None outside a mutation.
        self._audit_buffer: list[dict] | None = None
        self._now = now_fn
        # The rolling-window loop brake, distinct from the per-watch daily budget: the
        # daily cap is a ceiling, but a tight A@B / B@A pointer loop would burn a whole
        # day's budget in seconds before it trips. This bounds dispatches per document
        # within a short window, so a runaway stops in one cycle rather than a day.
        # A deployment default, not a per-watch grant: it is a safety floor, not a
        # policy the owner tunes per document (§16.14, the liveness brake).
        self._rate_max = int(rate_max)
        self._rate_window = float(rate_window_seconds)
        # The retired-level migration writes once per process; the read paths
        # migrate in memory every time regardless, so the flag is a lock-churn
        # guard, never the thing correctness rests on.
        self._retirements_persisted = False

    # ------------------------------------------------------------------ storage

    def _read(self) -> dict:
        """The file as written, before the migrations.

        ``global.suspended`` is a legacy field: nothing sets it any more and
        nothing but ``_retire_suspended`` reads it. It is kept in the default
        shape so a file written by this release and read by an older one still
        parses, and so the migration has a place to write the cleared flag back.
        """
        if not self._path.is_file():
            return {"version": 1, "global": {"suspended": False}, "watches": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("[clouddoc] watch registry unreadable; treating as empty")
            return {"version": 1, "global": {"suspended": False}, "watches": {}}
        data.setdefault("global", {"suspended": False})
        data.setdefault("watches", {})
        return data

    def _load(self) -> dict:
        data = self._read()
        self._retire_unknown_modes(data)
        self._retire_suspended(data)
        return data

    @staticmethod
    def _is_retired(entry: Any) -> bool:
        """True for a live entry whose level this release no longer has."""
        return (
            isinstance(entry, dict)
            and not entry.get("revoked")
            and entry.get("mode") not in MODES
        )

    @staticmethod
    def _is_suspended(entry: Any) -> bool:
        """True for a live entry a previous release left paused."""
        return (
            isinstance(entry, dict)
            and not entry.get("revoked")
            and bool(entry.get("suspended"))
        )

    def _pending_retirements(self, data: dict) -> dict[str, str]:
        """Every entry this release must tombstone, and why.

        Two migrations share one shape because they share one hazard: a state
        this release stopped checking must never read as *dispatchable* on the
        next load. Deleting the check alone would silently widen authority the
        owner had narrowed -- the ledger says paused, the code no longer asks.
        """
        out: dict[str, str] = {}
        frozen = bool((data.get("global") or {}).get("suspended"))
        for doc_id, entry in (data.get("watches") or {}).items():
            if self._is_retired(entry):
                out[doc_id] = RETIRED_TIER_REASON
            elif self._is_suspended(entry) or (
                frozen and isinstance(entry, dict) and not entry.get("revoked")
            ):
                # The global pause is the same fact written once instead of per
                # entry, so it retires the same way -- per document, one line
                # each, since the audit view is per document.
                out[doc_id] = SUSPEND_RETIRED_REASON
        return out

    def _retire_unknown_modes(self, data: dict) -> list[str]:
        """Turn every entry at a retired level into a tombstone, in memory.

        A ledger written by an earlier release still carries ``reply_only``
        entries. They must not be **upgraded** to ``apply_scoped``: that would
        hand the agent write authority over a document whose owner only ever
        signed for the narrower thing -- a silent widening, the exact failure the
        closed-set family exists to prevent. They must not be honoured either:
        the level no longer has a contract, a toolset, or a dispatch path. So
        they become tombstones, the same shape revocation and expiry use, and
        the owner re-grants deliberately if they want the surviving level.

        Idempotent: a retired entry gains ``revoked``, so the next pass skips it.
        Returns the ids retired on this pass, for the audit line.
        """
        retired: list[str] = []
        for doc_id, entry in (data.get("watches") or {}).items():
            if not self._is_retired(entry):
                continue
            entry["revoked"] = True
            entry["revoked_at"] = self._now()
            entry["revoked_reason"] = RETIRED_TIER_REASON
            retired.append(doc_id)
        return retired

    def _retire_suspended(self, data: dict) -> list[str]:
        """Turn every entry a previous release left paused into a tombstone.

        Suspension is gone from the state space, and that is the whole danger:
        the file still says ``suspended: true`` (or ``global.suspended``), and a
        release that simply stopped reading the flag would hand those documents
        back to the dispatcher without anyone deciding to. So a paused watch
        lands where the owner's "not now" already pointed -- **off** -- and the
        owner turns it back on deliberately if that is what they meant. The
        ``mode`` is left as written, so the audit view can still say what the
        grant had been.

        The global flag is cleared in the same pass. Left set, it would retire
        every *new* grant on the next load -- the migration would eat the watches
        it exists to protect.
        """
        retired: list[str] = []
        frozen = bool((data.get("global") or {}).get("suspended"))
        for doc_id, entry in (data.get("watches") or {}).items():
            if not (self._is_suspended(entry) or (
                frozen and isinstance(entry, dict) and not entry.get("revoked")
            )):
                continue
            entry["revoked"] = True
            entry["revoked_at"] = self._now()
            entry["revoked_reason"] = SUSPEND_RETIRED_REASON
            retired.append(doc_id)
        if frozen:
            data.setdefault("global", {})["suspended"] = False
        return retired

    def _persist_retirements(self) -> None:
        """The on-disk half of the migration, run at most once per process.

        Read paths migrate in memory, which is enough for correctness (the gate
        already reads such an entry as no watch). This makes it durable and
        leaves the audit line, so the owner can see *why* a watch they remember
        granting now reads as off. Best effort: a read-only or full disk must not
        turn a query into an exception.
        """
        if getattr(self, "_retirements_persisted", False):
            return
        self._retirements_persisted = True
        on_disk = self._read()
        stale = self._pending_retirements(on_disk)
        frozen = bool((on_disk.get("global") or {}).get("suspended"))
        if not stale and not frozen:
            return
        try:
            self._mutate(lambda data: None)
        except Exception:  # noqa: BLE001 -- a query must not fail on a write error
            logger.exception("[clouddoc] watch-state migration could not be persisted")
            self._retirements_persisted = False
            return
        for doc_id, reason in stale.items():
            self._audit("revoke", doc_id, reason=reason, by="migration")
        tiers = sorted(d for d, r in stale.items() if r == RETIRED_TIER_REASON)
        paused = sorted(d for d, r in stale.items() if r == SUSPEND_RETIRED_REASON)
        if tiers:
            logger.warning(
                "[clouddoc] %d 条档位已不存在的 watch（本版仅 %s；已退役：%s）"
                "按墓碑处理，视同无授权；需要的话请在面板重新签发：%s",
                len(tiers), "/".join(MODES), "/".join(RETIRED_MODES),
                ", ".join(tiers),
            )
        if paused:
            logger.warning(
                "[clouddoc] %d 条处于「暂停」的 watch 按墓碑处理：暂停档已取消，"
                "值守只有 关 / 开 两态。这些文档现在是「关」，需要的话请在面板重新开启：%s",
                len(paused), ", ".join(paused),
            )

    def _mutate(self, fn: Callable[[dict], Any]) -> Any:
        # Writes migrate too, and they must leave the audit line. Without this,
        # a lifecycle call that happened to run first would persist the tombstone
        # as a side effect of ``_load`` and the "why did my watch turn off" line
        # would never be written. Re-entrant by the flag: the nested call this
        # makes returns immediately.
        self._persist_retirements()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock), timeout=_LOCK_TIMEOUT_S):
            data = self._load()
            # Audit lines raised by ``fn`` are buffered and written only once the
            # state file is in place: the journal must never claim a grant or a
            # revocation whose state write failed. Both land under the one lock,
            # so two processes cannot interleave their lines.
            self._audit_buffer = []
            try:
                out = fn(data)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self._path)
                lines, self._audit_buffer = self._audit_buffer, None
            except BaseException:
                self._audit_buffer = None
                raise
            for line in lines:
                self._append_audit(line)
        return out

    def _audit(self, event: str, doc_id: str | None, **extra: Any) -> None:
        line = {"ts": self._now(), "event": event, "doc_id": doc_id, **extra}
        if self._audit_buffer is not None:
            self._audit_buffer.append(line)
            return
        # Outside a mutation (a denial, a migration note): appended under the same
        # lock the state writes take, so concurrent writers serialise their lines.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock = self._path.with_suffix(self._path.suffix + ".lock")
            with portalocker.Lock(str(lock), timeout=_LOCK_TIMEOUT_S):
                self._append_audit(line)
        except (OSError, portalocker.exceptions.BaseLockException):
            logger.exception("[clouddoc] watch audit line lost: %s", line)

    def _append_audit(self, line: dict) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("[clouddoc] watch audit line lost: %s", line)

    # ---------------------------------------------------------------- lifecycle

    def issue(
        self,
        doc_id: str,
        mode: str,
        *,
        issued_by: str = "manual",
        expires_at: float | None = _UNSET,
        from_list: tuple[str, ...] = (),
        budget: dict | None = None,
    ) -> dict:
        """Grant, or modify by re-issuance (D3: a change terminates the old watch and
        issues a new one; in-flight turns keep the terms snapshotted at dispatch)."""
        if mode not in MODES:
            raise ValueError(f"unknown watch mode: {mode!r}")
        if expires_at is _UNSET:
            expires_at = self._now() + DEFAULT_WATCH_TTL_SECONDS

        def fn(data: dict) -> dict:
            prior = data["watches"].get(doc_id)
            # A revoked entry is a tombstone, not a watch: issuing over it is a fresh
            # grant (the manual act that outranks the owner's earlier termination),
            # not a modification of terms that no longer exist.
            if prior is not None and prior.get("revoked"):
                prior = None
            entry = {
                "mode": mode,
                "issued_at": self._now(),
                "issued_by": issued_by,
                "expires_at": expires_at,
                "from": list(from_list),
                "budget": dict(budget or {}),
                "dispatch_day": "",
                "dispatch_count": 0,
            }
            data["watches"][doc_id] = entry
            self._audit(
                "modify" if prior else "grant", doc_id, mode=mode, issued_by=issued_by
            )
            return entry

        return self._mutate(fn)

    def revoke(self, doc_id: str, *, reason: str | None = None) -> bool:
        """End the delegation and keep the entry, flagged, as its own tombstone.

        Deleting the entry left the journal as the only record of the revocation,
        and the journal's write failure is swallowed (a full disk logs and moves
        on). With no entry and no line, the adoption policy re-issued at the next
        startup. The entry now carries the flag itself; the gate and the pre-write
        checkpoint read it as no watch at all, and only a manual re-issue replaces
        it. Same shape as expiry, for the same reason.
        """

        def fn(data: dict) -> bool:
            entry = data["watches"].get(doc_id)
            if entry is None or entry.get("revoked"):
                return False
            entry["revoked"] = True
            entry["revoked_at"] = self._now()
            self._audit("revoke", doc_id, **({"reason": reason} if reason else {}))
            return True

        return self._mutate(fn)

    def revoke_all(self) -> int:
        """The kill switch (D8): one action, every standing mandate gone. Each
        entry stays as a flagged tombstone, like a single revocation."""

        def fn(data: dict) -> int:
            n = 0
            for doc_id, entry in data["watches"].items():
                if entry.get("revoked"):
                    continue
                entry["revoked"] = True
                entry["revoked_at"] = self._now()
                self._audit("revoke", doc_id, by="kill_switch")
                n += 1
            return n

        return self._mutate(fn)

    def _expire(self, doc_id: str) -> None:
        """Expiry ends the delegation but keeps the entry as its own tombstone.

        Deleting here is what turned expiry into a no-op: the adoption policy
        re-issues for any document without an entry, so a deleted-on-expiry
        watch came back silently at the next startup. The entry stays, flagged,
        until the owner renews (re-issuance) or revokes; the audit line lands
        exactly once."""

        def fn(data: dict) -> None:
            entry = data["watches"].get(doc_id)
            if entry is not None and not entry.get("expired"):
                entry["expired"] = True
                self._audit("expire", doc_id, mode=entry.get("mode"))

        self._mutate(fn)

    def terminated_by_owner(self, doc_id: str) -> bool:
        """True when the audit journal's last word on this document is revoke.

        The adoption policy consults this before issuing: a revoked watch whose
        entry is gone must not come back as a policy grant at the next startup --
        only a manual grant outranks the owner's own termination."""
        last: str | None = None
        try:
            with self._audit_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    if line.get("doc_id") != doc_id:
                        continue
                    ev = line.get("event")
                    if ev in ("grant", "modify", "revoke"):
                        last = ev
        except OSError:
            return False
        return last == "revoke"

    # ------------------------------------------------------------------ queries

    def get(self, doc_id: str) -> dict | None:
        self._persist_retirements()
        return self._load()["watches"].get(doc_id)

    def snapshot(self) -> dict:
        self._persist_retirements()
        return self._load()

    def is_on(self, doc_id: str) -> bool:
        """Is this document's watch **on** right now -- the panel's own question.

        Deliberately not ``check().dispatchable``: over-budget and rate-limited
        are momentary, and a bulk "turn the selected ones on" that re-issued a
        watch because today's budget happened to be spent would quietly restart
        the thirty-day term on a grant already in use. On/off is the standing
        fact; the gate's transient refusals are not part of it.
        """
        return self.is_write_live(doc_id)

    def check(self, doc_id: str) -> WatchVerdict:
        """The dispatch gate: one live conjunction, never a cached boolean (D3).

        Remote conditions (document reachable, connection alive) are judged by the
        tick's own API calls succeeding — probing them here would double the quota
        bill for nothing (the admission precedent).
        """
        self._persist_retirements()
        data = self._load()
        entry = data["watches"].get(doc_id)
        if entry is None or entry.get("revoked"):
            return WatchVerdict(False, None, "no_watch")
        if entry.get("mode") not in MODES:
            # A level this release no longer has. ``_load`` already retired it in
            # memory, so this is belt-and-braces for an entry written by a newer
            # or older peer: an unrecognised level is no mandate, never the one
            # surviving level.
            return WatchVerdict(False, None, "no_watch")
        exp = entry.get("expires_at")
        if exp is not None and self._now() >= float(exp):
            self._expire(doc_id)
            return WatchVerdict(False, entry["mode"], "expired")
        cap = (entry.get("budget") or {}).get("max_dispatches_per_day")
        if cap is not None and self._today_count(entry) >= int(cap):
            return WatchVerdict(False, entry["mode"], "over_budget")
        # The rolling-window loop brake, checked last: it is the backstop the roster
        # and the self/other-agent filter aim to make unnecessary, not the first line.
        if self._rate_max > 0 and self._recent_count(entry) >= self._rate_max:
            return WatchVerdict(False, entry["mode"], "rate_limited")
        return WatchVerdict(True, entry["mode"], "ok")

    def is_write_live(self, doc_id: str, *, mode: str | None = None) -> bool:
        """The pre-write checkpoint (IC-3): revocation and expiry intercept
        in-flight writes. With ``mode`` given, the entry must still be
        at that level: a turn dispatched under ``apply_scoped`` whose entry no
        longer reads ``apply_scoped`` (revoked, or left at a level this release
        retired) is intercepted here, at the last point before the platform call,
        rather than draining to completion. The
        budget and the ``from`` list stay snapshotted at dispatch."""
        data = self._load()
        entry = data["watches"].get(doc_id)
        if entry is None or entry.get("revoked"):
            return False
        if entry.get("mode") not in MODES:
            return False
        if entry.get("expired"):
            return False
        exp = entry.get("expires_at")
        if exp is not None and self._now() >= float(exp):
            return False
        if mode is not None and entry.get("mode") != mode:
            return False
        return True

    # ------------------------------------------------------------------- budget

    def _day_key(self) -> str:
        t = time.gmtime(self._now())
        return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"

    def _today_count(self, entry: dict) -> int:
        return entry["dispatch_count"] if entry.get("dispatch_day") == self._day_key() else 0

    def _recent_count(self, entry: dict) -> int:
        """Dispatches on this document within the rolling window ending now."""
        cutoff = self._now() - self._rate_window
        return sum(1 for ts in (entry.get("recent_dispatches") or []) if ts >= cutoff)

    def note_dispatch(self, doc_id: str) -> None:
        """Count a dispatched turn against the day's budget. Persisted, so a restart
        does not refill the budget (a refresh applies to new days, not new processes)."""

        def fn(data: dict) -> None:
            entry = data["watches"].get(doc_id)
            if entry is None:
                return
            day = self._day_key()
            if entry.get("dispatch_day") != day:
                entry["dispatch_day"] = day
                entry["dispatch_count"] = 0
            entry["dispatch_count"] += 1
            # The rolling-window brake's own record: timestamps within the window,
            # pruned on each write so the list cannot grow without bound. Kept separate
            # from the daily counter because the two answer different questions (a day's
            # total vs a burst).
            now = self._now()
            recent = [ts for ts in (entry.get("recent_dispatches") or [])
                      if ts >= now - self._rate_window]
            recent.append(now)
            entry["recent_dispatches"] = recent
            # The daily counter serves the budget; the audit line is what lets
            # the panel show cumulative use against the standing grant (E1's
            # audit view: granted minus used).
            self._audit("dispatch", doc_id, mode=entry.get("mode"))

        self._mutate(fn)

    def note_denied(self, doc_id: str, reason: str) -> None:
        """A refused dispatch, on the record. Frequent denials are the friction
        signal the audit view surfaces (the owner may want to widen or renew);
        without the line the signal does not exist."""
        self._audit("deny", doc_id, reason=reason)

    def usage_summary(self, doc_id: str) -> dict:
        """What the audit journal knows about this document's standing grant:
        issuance history, cumulative dispatches, denials by reason. Receipts --
        the writes themselves -- are the other half and live with the ledger."""
        grants = 0
        dispatches = 0
        last_grant_at: float | None = None
        denials: dict[str, int] = {}
        try:
            with self._audit_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    if line.get("doc_id") != doc_id:
                        continue
                    ev = line.get("event")
                    if ev in ("grant", "modify"):
                        grants += 1
                        last_grant_at = line.get("ts")
                    elif ev == "dispatch":
                        dispatches += 1
                    elif ev == "deny":
                        r = str(line.get("reason") or "unknown")
                        denials[r] = denials.get(r, 0) + 1
        except OSError:
            pass
        return {
            "grants": grants,
            "last_grant_at": last_grant_at,
            "dispatches": dispatches,
            "denials": denials,
        }

    # -------------------------------------------------------------------- audit

    def audit_for(self, doc_id: str, limit: int = 50) -> list[dict]:
        """This document's own lifecycle, newest first -- the authority lineage.

        ``audit_tail`` is the deployment's tail and answers a different question:
        opened per document it shows mostly other documents' lines, and the ones
        for this document may have scrolled off it entirely. This filters
        instead, so "why is this watch off, and who said so" is answerable for a
        document that has been quiet for a month.

        Lines with no ``doc_id`` -- deployment-wide events -- are skipped rather
        than attributed here: a per-document list that shows an event which did
        not name this document invents an association the journal never made.
        """
        if not self._audit_path.is_file():
            return []
        try:
            raw = self._audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict] = []
        for ln in reversed(raw):
            try:
                line = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("doc_id") != doc_id:
                continue
            out.append(line)
            if len(out) >= limit:
                break
        return out

    def audit_tail(self, limit: int = 100) -> list[dict]:
        if not self._audit_path.is_file():
            return []
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict] = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out
