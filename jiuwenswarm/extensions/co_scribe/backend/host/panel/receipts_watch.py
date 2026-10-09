"""Push for the receipts ledger: tell every web client when it changed.

The ledger is a JSON file shared across processes -- chat and unattended writes
land from the agentserver, panel writes from the gateway -- so no in-process
hook sees them all. What every writer does do is touch the one file, so the
gateway watches its mtime (a stat every couple of seconds, no read) and
broadcasts ``clouddoc.receipts_changed``. The workbench refreshes on the event
instead of polling each open document every 15 seconds; its own poll survives
as a slow fallback for a dropped frame.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


async def watch_receipts_file(
    broadcast: Callable[[str, dict[str, Any]], Awaitable[None]],
    *,
    interval_s: float = 2.0,
    path: Any = None,
    max_ticks: int | None = None,
) -> None:
    """Broadcast once per observed change of the ledger file's mtime.

    ``path`` and ``max_ticks`` are seams for tests; the default path is the
    deployment's ledger. Fail-soft everywhere: a missing file is the quiet
    starting state, a failed broadcast is logged and the watch keeps going.
    """
    if path is None:
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import (
            get_receipts_path,
        )

        path = get_receipts_path()
    last: float | None = None
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime != last:
            # The very first observation is the baseline, not a change: clients
            # load their state on mount and an immediate ping would double that.
            if last is not None:
                try:
                    await broadcast("clouddoc.receipts_changed", {"ts": mtime})
                except Exception:  # noqa: BLE001 - the watch outlives one bad frame
                    logger.debug("[clouddoc] receipts broadcast failed", exc_info=True)
            last = mtime
        elif mtime is None and last is not None:
            last = None
        await asyncio.sleep(interval_s)
