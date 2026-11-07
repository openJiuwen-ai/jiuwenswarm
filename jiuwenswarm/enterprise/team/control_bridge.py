# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team control-plane bridge for the harness layer.

The runtime (``TeamManager`` / ``team_helpers``) knows *when* a team changes
lifecycle state but must not depend on the server-side persistence module
directly. This module is the thin seam: it maps runtime events onto
``TeamControlStore`` updates and is deliberately best-effort — a control-plane
write failure never breaks a running team.

Session-scoped runtime team names look like ``<team_name>_<session_suffix>``;
callers may pass either the entity ``team_name`` or the runtime name — both
are resolved by ``_resolve_entity``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _store():
    from jiuwenswarm.enterprise.team.control_store import get_team_control_store

    return get_team_control_store()


def _resolve_entity(*, team_name: str = "", session_id: str = "") -> Any | None:
    """Find the entity by session first (authoritative), then by team name."""
    store = _store()
    entity = None
    sid = str(session_id or "").strip()
    if sid:
        entity = store.get_entity_by_session(sid)
    name = str(team_name or "").strip()
    if entity is None and name:
        entity = store.get_entity_by_name(name)
        if entity is None and "_" in name:
            # runtime team name `<team>_<suffix>` → strip suffix progressively
            parts = name.split("_")
            for cut in range(len(parts) - 1, 0, -1):
                candidate = "_".join(parts[:cut])
                entity = store.get_entity_by_name(candidate)
                if entity is not None:
                    break
    return entity


def record_team_status(
    status: str,
    *,
    team_name: str = "",
    session_id: str = "",
    runtime_team_name: str = "",
) -> str | None:
    """Persist a lifecycle transition. Returns the ``team_id`` touched, if any."""
    try:
        entity = _resolve_entity(team_name=team_name, session_id=session_id)
        if entity is None:
            logger.debug(
                "[TeamControl] no entity for status update: status=%s team=%s session=%s",
                status, team_name, session_id,
            )
            return None
        fields: dict[str, Any] = {"status": status}
        if session_id and not entity.team_session_id:
            fields["team_session_id"] = session_id
        if runtime_team_name and entity.runtime_team_name != runtime_team_name:
            fields["runtime_team_name"] = runtime_team_name
        _store().update_entity(entity.team_id, **fields)
        logger.info(
            "[TeamControl] team status: team_id=%s team=%s session=%s %s -> %s",
            entity.team_id, entity.team_name, session_id or entity.team_session_id,
            entity.status, status,
        )
        return entity.team_id
    except Exception:  # noqa: BLE001 - best effort
        logger.warning(
            "[TeamControl] failed to record team status: status=%s team=%s session=%s",
            status, team_name, session_id, exc_info=True,
        )
        return None


def lookup_team_id(*, team_name: str = "", session_id: str = "") -> str | None:
    try:
        entity = _resolve_entity(team_name=team_name, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.debug("[TeamControl] lookup_team_id failed", exc_info=True)
        return None
    return entity.team_id if entity is not None else None


def entity_payload(*, team_name: str = "", session_id: str = "") -> dict[str, Any] | None:
    try:
        entity = _resolve_entity(team_name=team_name, session_id=session_id)
    except Exception:  # noqa: BLE001
        return None
    return entity.to_dict() if entity is not None else None


__all__ = ["entity_payload", "lookup_team_id", "record_team_status"]
