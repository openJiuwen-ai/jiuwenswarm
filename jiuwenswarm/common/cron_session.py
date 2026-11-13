# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron execution session identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_cron_execution_session(session_id: str | None) -> bool:
    """Return True for scheduled-job execution sessions.

    Covers both id shapes a cron run can hold: the ``cron_*`` convention
    (proactive tick sessions ``cron_<job_id>`` and legacy team per-run
    sessions ``cron_<ts>_<job_id>``) and the ``__cron___<ts>_<rand>`` ids
    assigned by ``CronScheduler._allocate_execution_session`` through
    ``AgentManager.create_session`` on the internal ``__cron__`` channel.
    Aligned with the single-agent adapter's ``_is_cron_execution_session``
    prefix union so team members and single agents agree on the same signal.

    Cron runs have no operator on the other end, so interactive permission
    interrupts cannot be answered and must not be attached.
    """
    return str(session_id or "").strip().startswith(("cron", "__cron__"))


def is_cron_execution_context(
    session_id: str | None,
    *,
    channel_id: str | None = None,
    request_metadata: Mapping[str, Any] | None = None,
    session_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Whether a build/runtime context belongs to a cron execution session.

    Union of every durable and per-request signal:

    - ``request_metadata["cron"]``: the scheduler's marker on the request that
      actually runs the job (single-agent and team stream paths);
    - ``request_metadata["cron_session"]``: session-level cron ownership
      forwarded across a process boundary where the target cannot read the
      session's metadata. Deliberately distinct from ``cron`` so downstream
      consumers keep treating ``cron`` as "this round was scheduler-triggered"
      (interactive approvals, A4P scope, prompts) while tool gating still knows
      the session belongs to a cron run;
    - ``channel_id == "__cron__"``: the internal execution channel;
    - the ``cron_*`` / ``__cron___*`` session-id conventions;
    - the persisted ``cron_id`` in session metadata written by
      ``session.create`` for every scheduled run.

    The persisted ``cron_id`` is the only signal that survives outside the
    scheduler request itself: Web jobs execute in an ordinary ``web_<ts>_<rand>``
    session, so the id and channel checks cannot identify them. Without this
    signal a Team rebuilt while serving a later turn of the same execution
    session (user follow-up, cold recovery, idle teardown) re-exposes cron tools
    and allows nested task creation, while the single-agent adapter already
    stays gated through ``_is_cron_execution_session``.

    Args:
        session_id: Active session id.
        channel_id: Raw channel id from the request.
        request_metadata: Request metadata mapping (carries ``cron``).
        session_metadata: Session metadata the caller already read. This module
            is the ``common`` layer and must not import ``server`` (R1 layering
            rule), so resolving metadata from disk stays in the upper layer and
            is passed in as a mapping.

    Returns:
        True when the context is a cron execution and cron capabilities must
        not be exposed.
    """
    metadata = request_metadata if isinstance(request_metadata, Mapping) else {}
    if metadata.get("cron") or metadata.get("cron_session"):
        return True
    if str(channel_id or "").strip() == "__cron__":
        return True
    if is_cron_execution_session(session_id):
        return True
    if not isinstance(session_metadata, Mapping):
        return False
    return bool(str(session_metadata.get("cron_id") or "").strip())


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
