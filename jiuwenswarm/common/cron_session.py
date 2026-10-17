# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron execution session identity helpers."""

from __future__ import annotations


def is_cron_execution_session(session_id: str | None) -> bool:
    """Return True for scheduled-job execution sessions (``cron_*``).

    Cron runs have no operator on the other end, so interactive permission
    interrupts cannot be answered and must not be attached.
    """
    return str(session_id or "").strip().startswith("cron")


def cron_session_matches_job(session_id: str | None, cron_id: str) -> bool:
    """Whether a session directory name follows the cron job naming convention.

    Two shapes are conventionally named after a job:
    - ``cron_<job_id>``: the stable proactive.tick session;
    - ``cron_<ts>_<job_id>``: per-run team execution sessions.

    Single-agent runs allocate ``__cron___<ts>_<rand>`` sessions whose suffix is
    random, so they never match here and rely on their persisted ``cron_id``
    metadata instead. Used as a fallback for legacy team execution sessions
    whose metadata never got the ``cron_id`` stamp (the implicit chat-admission
    sync was skipped), e.g. by ``project.get_cron_sessions``.
    """
    sid = str(session_id or "").strip()
    job = str(cron_id or "").strip()
    if not sid or not job:
        return False
    if sid == f"cron_{job}":
        return True
    return sid.startswith("cron_") and sid.endswith(f"_{job}")
