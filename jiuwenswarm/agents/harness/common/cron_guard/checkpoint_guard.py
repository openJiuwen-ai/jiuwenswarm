# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""L2: checkpoint restore governance for cron runs (issue #5018, design §3.3).

Rules (ledger-driven, reversible):

* Interactive requests are never rejected, quarantined, or cleared.
* A ``scheduled_run`` whose *prior* ledger row for the same sid is in
  ``{tripped, orphaned}`` is an abnormal resurrection: the checkpoint blobs
  are copied into a ``quarantine:{ts}:{sid}:`` namespace and the original
  keys are cleared (copy verified first — never destroy without backup).
* ``finished`` priors are allowed (stable cron sids legitimately reuse
  context across runs).
* Guard-internal failures are fail-open (allow + warn).  Quarantine failures
  (backup write failed) never clear the original keys.

The checkpoint store is the upstream SQLite ``kv_store`` table at
``~/.jiuwenswarm/agent/.checkpoint/checkpoint.db`` with keys of the form
``{sid}:agent:{entity}:{suffix}``.  We operate on the key namespace
directly (prefix copy + delete), which is storage-format agnostic.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from jiuwenswarm.common.utils import logger

from .ledger import (
    STATE_ORPHANED,
    STATE_TRIPPED,
    default_ledger_path,
    get_run_ledger,
)

_ABNORMAL_PRIOR_STATES = frozenset({STATE_TRIPPED, STATE_ORPHANED})


class CronCheckpointQuarantined(Exception):
    """Raised when a scheduled run is refused because its checkpoint was quarantined."""


def checkpoint_db_path() -> Path:
    base = default_ledger_path().parent
    return base / "checkpoint.db"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def has_checkpoint_blob(sid: str, db_path: Path | None = None) -> bool:
    path = db_path or checkpoint_db_path()
    if not path.exists():
        return False
    try:
        conn = _connect(path)
        try:
            cur = conn.execute(
                "SELECT 1 FROM kv_store WHERE key LIKE ? LIMIT 1",
                (f"{sid}:agent:%",),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] checkpoint probe failed (fail-open): %s", exc)
        return False


def quarantine_checkpoint(sid: str, db_path: Path | None = None) -> tuple[bool, str | None]:
    """Copy ``{sid}:agent:*`` keys into ``quarantine:{ts}:{sid}:*`` then clear.

    Returns ``(success, quarantine_prefix)``.  On any failure before the copy
    completes, the original keys are left untouched.  If the copy succeeded
    but the delete failed, returns success with a ``quarantine_partial``
    note in the prefix so operators can reconcile.
    """
    path = db_path or checkpoint_db_path()
    if not path.exists():
        return False, None
    ts = int(time.time())
    prefix = f"quarantine:{ts}:{sid}"
    try:
        conn = _connect(path)
        try:
            cur = conn.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE ?",
                (f"{sid}:agent:%",),
            )
            rows = cur.fetchall()
            if not rows:
                return False, None
            conn.executemany(
                "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                [(f"{prefix}:{k}", v) for k, v in rows],
            )
            conn.commit()
            # Verify the copy before deleting anything.
            verify = conn.execute(
                "SELECT COUNT(*) FROM kv_store WHERE key LIKE ?",
                (f"{prefix}:%",),
            ).fetchone()[0]
            if int(verify) < len(rows):
                conn.rollback()
                return False, None
            conn.executemany(
                "DELETE FROM kv_store WHERE key = ?",
                [(k,) for k, _ in rows],
            )
            conn.commit()
            remaining = conn.execute(
                "SELECT COUNT(*) FROM kv_store WHERE key LIKE ?",
                (f"{sid}:agent:%",),
            ).fetchone()[0]
            if int(remaining) > 0:
                return True, prefix + ":quarantine_partial"
            return True, prefix
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] quarantine failed for sid=%s: %s", sid, exc)
        return False, None


def restore_quarantined(sid: str, quarantine_prefix: str, db_path: Path | None = None) -> bool:
    """Operator-side reversible restore: copy quarantined keys back.

    Not called by the agent automatically (design §3.3.5).
    """
    path = db_path or checkpoint_db_path()
    try:
        conn = _connect(path)
        try:
            cur = conn.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE ?",
                (f"{quarantine_prefix}:%",),
            )
            rows = cur.fetchall()
            if not rows:
                return False
            conn.executemany(
                "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                [(k[len(quarantine_prefix) + 1:], v) for k, v in rows],
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cron_guard] quarantine restore failed: %s", exc)
        return False


def govern_restore_at_entry(ctx) -> None:
    """L2 decision at scheduled-run entry (design §3.3.3/§3.3.4).

    Raises :class:`CronCheckpointQuarantined` only for
    ``quarantine_and_fail`` with a successful quarantine.  Every
    other path returns normally (allow / fresh / warn / fail-open).
    """
    from .config import get_cron_guard_config

    cfg = get_cron_guard_config()
    cp_cfg = cfg.get("checkpoint") or {}
    if not cfg.get("enabled") or not cp_cfg.get("enabled"):
        return
    if not ctx.trusted:
        # Untrusted identity: state-changing actions disabled (log only).
        logger.info(
            "[cron_guard] untrusted scheduled_run sid=%s — checkpoint governance log-only",
            ctx.sid,
        )
        return

    ledger = get_run_ledger()
    prior = ledger.latest_for_sid(ctx.sid, exclude_run_id=ctx.run_id)
    if prior is None:
        return  # first run for this sid
    if prior.get("state") not in _ABNORMAL_PRIOR_STATES:
        return  # finished (or same-run) — legitimate reuse

    on_trip = str(cp_cfg.get("on_trip", "quarantine_and_fail"))
    # Aliases from v1 keep the quarantine semantics.
    if on_trip == "reset_and_fail":
        on_trip = "quarantine_and_fail"
    elif on_trip == "reset_and_fresh":
        on_trip = "quarantine_and_fresh"

    if not has_checkpoint_blob(ctx.sid):
        logger.debug("[cron_guard] no checkpoint blob for sid=%s, allow", ctx.sid)
        return

    if on_trip == "warn":
        logger.error(
            "[cron_guard] (warn) abnormal checkpoint resurrection sid=%s prior_state=%s",
            ctx.sid, prior.get("state"),
        )
        return

    ok, prefix = quarantine_checkpoint(ctx.sid)
    if not ok:
        # Backup failed → never clear; fail the run (design §3.3.4).
        logger.error(
            "[cron_guard] quarantine backup failed for sid=%s — failing run without clearing",
            ctx.sid,
        )
        raise CronCheckpointQuarantined(
            f"checkpoint quarantine failed for sid={ctx.sid}"
        )
    logger.warning(
        "[cron_guard] quarantined checkpoint sid=%s prior_state=%s -> %s (on_trip=%s)",
        ctx.sid, prior.get("state"), prefix, on_trip,
    )
    if on_trip == "quarantine_and_fail":
        raise CronCheckpointQuarantined(
            f"checkpoint quarantined ({prefix}); run refused due to prior "
            f"{prior.get('state')} state"
        )
    # quarantine_and_fresh: fall through — execution continues with empty state.
