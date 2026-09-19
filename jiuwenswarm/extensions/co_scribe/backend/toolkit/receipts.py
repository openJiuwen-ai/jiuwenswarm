"""The receipt store (PR2c, D8): every write's after-the-fact instrument.

A receipt is recorded **mechanically at the write primitive** — the model can
neither skip nor disable it (the receipt invariant). Each receipt carries what was
written (old and new over the exact located spans), the many-to-many edit↔comment
map (D5), and the highlight flag, so one instrument serves audit, the panel's
history, and un-highlight-on-resolve. Undoing a change is the platform's version
history, not the ledger's job: a receipt records, it does not reverse.

Write-ahead discipline (IC-3): the receipt lands as ``pending`` **before** the
platform write and is marked ``applied`` after it — a crash between the two
leaves a pending receipt for the sweep to adjudicate, never an untracked write.

Storage follows the watch-registry precedent: one JSON file under the workspace
config dir, portalocker-guarded, readable from both processes.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import portalocker

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_S = 10.0


class ReceiptStoreUnavailable(RuntimeError):
    """The ledger file exists but cannot be read.

    Raised on the write path only. A missing file is an empty ledger (first run);
    an unreadable one is not -- it holds receipts this process cannot see, and a
    write recorded against an empty stand-in would overwrite them. IC-2: a write
    the ledger cannot record is refused, so this propagates to the write primitive.
    """


def get_receipts_path() -> Path:
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.deployment import (
        workspace_dir as get_user_workspace_dir,
    )

    return get_user_workspace_dir() / "config" / "clouddoc-receipts.json"


class ReceiptStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
        max_receipts: int = 500,
    ) -> None:
        self._path = path or get_receipts_path()
        self._now = now_fn
        self._max = max_receipts

    def _load(self, *, strict: bool = False) -> dict:
        """Read the ledger. Reads degrade (an unreadable file reads as empty, so the
        panel still opens); writes do not (``strict`` raises), because writing through
        an empty stand-in would replace the unreadable file and lose every receipt in
        it. ``_mutate`` is the only strict caller."""
        if not self._path.is_file():
            return {"version": 1, "receipts": {}}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if strict:
                raise ReceiptStoreUnavailable(
                    f"回执账本不可读（{self._path.name}）：{exc}"
                ) from exc
            logger.exception("[clouddoc] receipt store unreadable; reading as empty")
            return {"version": 1, "receipts": {}}

    def _mutate(self, fn: Callable[[dict], Any]) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock), timeout=_LOCK_TIMEOUT_S):
            data = self._load(strict=True)
            out = fn(data)
            # Retention: oldest settled receipts fall off past the cap (500 by
            # default, across every document -- release §10 says so), and pending
            # ones are never dropped -- a
            # pending receipt is the only evidence that a write may have landed, and
            # discarding it would discard the crash window itself.
            #
            # That exemption is why ``sweep_stale`` exists. Without it pending receipts
            # accumulate past the cap and stay for good, and this method rewrites the
            # whole file on every write.
            recs = data.setdefault("receipts", {})
            if len(recs) > self._max:
                aged = sorted(
                    (r for r in recs.values() if r.get("status") != "pending"),
                    key=lambda r: r.get("ts", 0),
                )
                for r in aged[: len(recs) - self._max]:
                    recs.pop(r["receipt_id"], None)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        return out

    # ---------------------------------------------------------------- lifecycle

    def begin(
        self,
        doc_id: str,
        edits: list[dict],
        *,
        highlight: bool,
        executor: str = "",
        source: str = "",
        op: str = "edit",
        subject: dict | None = None,
    ) -> str:
        """The write-ahead half: record intent before the platform write.

        ``op`` names what kind of act the receipt records. ``edit`` (the default, and
        what every receipt written before the field existed means) is a content
        change; ``edits`` holds the exact before and after text, which is what a
        person needs to put the old wording back by hand. The lifecycle acts --
        ``create``, ``share``, ``trash`` -- carry no edits; ``subject`` names what
        was acted on (the document, and for a share the address and role).
        """
        receipt_id = uuid.uuid4().hex[:12]

        def fn(data: dict) -> str:
            data["receipts"][receipt_id] = {
                "receipt_id": receipt_id,
                "ts": self._now(),
                "doc_id": doc_id,
                "status": "pending",
                # Each edit: {old, new, for_comment_ids: [...]} — `old` is what the
                # person reads to restore the previous wording by hand.
                "edits": edits,
                "op": op,
                "subject": dict(subject or {}),
                "highlight": bool(highlight),
                "executor": executor,
                "source": source,
                "revision_after": None,
            }
            return receipt_id

        return self._mutate(fn)

    def commit(
        self,
        receipt_id: str,
        *,
        revision_after: str | None,
        highlighted: bool | None = None,
    ) -> None:
        """Close a pending receipt as applied.

        ``highlighted`` corrects what ``begin`` recorded. ``begin`` writes the intent,
        which is what the caller **asked for**; only the platform knows whether it
        happened. Left as the request, a platform with no highlighting primitive
        produced a receipt claiming a highlight, and the panel then offered an
        "unhighlight" button that could not do anything.

        ``None`` leaves the recorded value alone, for a caller that has nothing to
        correct.
        """

        def fn(data: dict) -> None:
            r = data["receipts"].get(receipt_id)
            if r is not None:
                r["status"] = "applied"
                r["revision_after"] = revision_after
                if highlighted is not None:
                    r["highlight"] = bool(highlighted)

        self._mutate(fn)

    def abort(self, receipt_id: str, *, reason: str) -> None:
        """The platform write never landed: the pending receipt closes as aborted —
        an audit fact, not a revertible instrument."""

        def fn(data: dict) -> None:
            r = data["receipts"].get(receipt_id)
            if r is not None:
                r["status"] = "aborted"
                r["abort_reason"] = reason

        self._mutate(fn)

    def mark_unhighlighted(self, receipt_id: str) -> None:
        """The highlight this receipt left is gone from the document.

        ``highlight`` is what the row displays and what the un-highlight control
        keys off, so it flips here: leaving it True kept the row reading
        "highlighted" and kept offering a button whose work was already done.
        ``unhighlighted`` stays as the audit fact -- this batch *was* highlighted
        and someone cleared it, which a receipt that never highlighted anything
        cannot claim.
        """

        def fn(data: dict) -> None:
            r = data["receipts"].get(receipt_id)
            if r is not None:
                r["unhighlighted"] = True
                r["highlight"] = False

        self._mutate(fn)


    def mark_unverified(self, receipt_id: str, *, detail: str) -> None:
        """The write committed, but the read-back does not match what was asked
        (D19 tier 3b). The platform said applied and the document disagrees --
        recorded as its own status rather than left as a clean ``applied``, because
        a receipt is what the person's acceptance reads, and it must not claim more
        than the document shows. The detail says what differed."""

        def fn(data: dict) -> None:
            r = data["receipts"].get(receipt_id)
            if r is not None and r.get("status") == "applied":
                r["status"] = "applied_unverified"
                r["unverified_detail"] = detail

        self._mutate(fn)

    # ------------------------------------------------------------------ queries

    def get(self, receipt_id: str) -> dict | None:
        return self._load()["receipts"].get(receipt_id)

    def list_for(self, doc_id: str, *, limit: int = 50) -> list[dict]:
        recs = [
            r for r in self._load()["receipts"].values() if r["doc_id"] == doc_id
        ]
        recs.sort(key=lambda r: -r.get("ts", 0))
        return recs[:limit]

    def pending(self) -> list[dict]:
        """Crash-window candidates for the startup sweep (IC-3)."""
        return [
            r for r in self._load()["receipts"].values() if r["status"] == "pending"
        ]

    def sweep_stale(self, *, older_than_seconds: float = 900.0) -> list[str]:
        """Adjudicate crash-window receipts, and return the ids that were settled.

        A pending receipt says a write was begun and never closed. Adjudicating it
        honestly means saying **the outcome is unknown** -- not guessing. The document
        cannot be consulted for an answer: the text may match because the write landed,
        or because it never did and someone else wrote the same words, and re-reading
        would turn a known unknown into a confident wrong answer.

        So it becomes ``unknown``, which is a settled status: the panel can show it, the
        un-highlight still refuses it (that needs ``applied``), and the retention cap
        can finally reclaim it. Nothing here was reclaimable before, and this was the
        only status the cap skipped.

        **The age test is not a nicety.** The receipts file is one path shared by every
        process on this machine, locked with portalocker, and the design has several
        watchers sharing one state file. A sweep with no age test, run at startup, would
        settle a receipt another process wrote seconds ago and is still writing against
        -- destroying the very evidence the crash window depends on. The default is far
        longer than any single platform write, so a receipt this old belongs to a
        process that is not coming back.
        """
        cutoff = self._now() - float(older_than_seconds)
        settled: list[str] = []

        def fn(data: dict) -> None:
            for r in data["receipts"].values():
                if r.get("status") != "pending" or r.get("ts", 0) > cutoff:
                    continue
                r["status"] = "unknown"
                r["abort_reason"] = (
                    "进程在写入过程中结束，这次写入是否落地无从判断；"
                    "请对照文档确认。"
                )
                settled.append(r["receipt_id"])

        self._mutate(fn)
        return settled
