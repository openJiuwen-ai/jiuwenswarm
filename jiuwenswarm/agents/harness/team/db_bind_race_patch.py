# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patch: tolerate concurrent session-table drops during team DB bind.

Every team DB bind runs openjiuwen's legacy column backfills
(``_ensure_message_protocol_column`` / ``_ensure_message_meta_column``), which
enumerate every ``team_message_*`` table in the file and probe each one with
``inspector.get_columns``. When a concurrent ``session.delete`` cleanup drops
another session's tables between the ``get_table_names()`` snapshot and the
probe, ``get_columns`` raises ``NoSuchTableError`` — whose ``str()`` is the
bare table name. That exception is not in ``create_cur_session_tables``
retry tuple (OperationalError / SATimeoutError / TimeoutError), so it escapes
the bind path, gets wrapped as ``task_failed`` by the chat round, and reaches
the user as a ``chat.error`` whose error text is the vanished table name,
killing this innocent session's stream (GitCode issue #4956).

The race is benign for the binding session: its own tables are created before
the scan starts, and the legacy backfill is idempotent (it reruns on every
bind), so a table vanishing mid-scan only means "migrate the rest later".

Deliberately patches openjiuwen private internals (same waiver as
``bash_tool_safety`` / ``llm_sse_patch``) because the repository cannot ship
an openjiuwen release; drop this module once openjiuwen's own backfill
tolerates the drop race or narrows its scan to the current session the way
``_ensure_dynamic_table_indexes`` already does.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from sqlalchemy.exc import NoSuchTableError

logger = logging.getLogger("jiuwenswarm.team_db_bind_race_patch")

# Idempotency marker set on the wrapper so re-installation never double-wraps.
_PATCH_MARKER = "_jiuwenswarm_drop_race_tolerant"

# The two bind-time full-file scans that probe other sessions' tables.
_BACKFILL_SCAN_FUNCTIONS = (
    "_ensure_message_protocol_column",
    "_ensure_message_meta_column",
)

# One re-scan after a concurrent drop (fresh get_table_names snapshot); a
# second drop during the re-scan gives up quietly — the backfill reruns on
# every bind, so nothing is lost permanently.
_MAX_SCAN_ATTEMPTS = 2


def _tolerate_concurrent_drop(original: Any, name: str) -> Any:
    """Wrap one backfill scan so tables dropped mid-scan are skipped."""

    @functools.wraps(original)
    def wrapper(sync_conn: Any) -> None:
        for attempt in range(_MAX_SCAN_ATTEMPTS):
            try:
                return original(sync_conn)
            except NoSuchTableError:
                # Another session's cleanup dropped a team_message_* table
                # between this scan's get_table_names() snapshot and its
                # get_columns() probe. Benign for this bind.
                logger.info(
                    "[TeamDbBindRacePatch] %s hit a concurrently dropped table "
                    "(attempt %d/%d)",
                    name,
                    attempt + 1,
                    _MAX_SCAN_ATTEMPTS,
                )
        return None

    return wrapper


def apply_team_db_bind_race_patch() -> None:
    """Make openjiuwen's bind-time message-table backfills drop-race tolerant.

    Never raises: a patch that fails to install must not take down worker
    startup. Missing target functions are logged and skipped — upstream may
    rename or already fix them.
    """
    try:
        from openjiuwen.agent_teams.tools.database import engine as team_db_engine
    except Exception as exc:  # pragma: no cover - openjiuwen unavailable
        logger.warning("[TeamDbBindRacePatch] openjiuwen team DB engine unavailable, skip: %s", exc)
        return

    patched_any = False
    for name in _BACKFILL_SCAN_FUNCTIONS:
        original = getattr(team_db_engine, name, None)
        if original is None:
            logger.info("[TeamDbBindRacePatch] %s not found, treat as fixed upstream", name)
            continue
        if getattr(original, _PATCH_MARKER, False):
            continue
        patched = _tolerate_concurrent_drop(original, name)
        setattr(patched, _PATCH_MARKER, True)
        setattr(team_db_engine, name, patched)
        patched_any = True

    if patched_any:
        logger.info("[TeamDbBindRacePatch] team DB bind drop-race patch applied")
