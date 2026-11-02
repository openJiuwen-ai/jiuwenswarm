# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tear down control-plane team records when a Session is permanently deleted.

Develop's ``session.delete`` on a team Session stops execution and then
``Runner.delete_agent_team``. This process does the matching control-plane
work for the enterprise MySQL tables: entity, membership, route binding, and
quota leases. It is safe to call from Gateway when AgentServer no longer has
the session directory (pod recycled).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from jiuwenswarm.enterprise.team.control_store import OwnerIdentity

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _occupancy_owner_user_id(
    *,
    entity: Any = None,
    lease: dict[str, str] | None = None,
    binding: Any = None,
) -> str:
    if entity is not None:
        owner = getattr(entity, "owner", None)
        uid = _clean(getattr(owner, "user_id", ""))
        if uid:
            return uid
    if lease:
        uid = _clean(lease.get("user_id"))
        if uid:
            return uid
    if binding is not None:
        uid = _clean(getattr(binding, "owner_user_id", ""))
        if uid:
            return uid
    return ""


def _purge_mysql(
    *,
    session_id: str,
    team_name: str,
    owner_user_id: str,
    require_owner: bool,
) -> str | None:
    """Read and delete team rows in one MySQL call, then drop the local binding."""
    from sqlalchemy import text

    from jiuwenswarm.infrastructure.db.team_mysql_actor import get_team_mysql_actor, run_tx
    from jiuwenswarm.enterprise.team.control_store import TEAM_STATUS_DELETED
    from jiuwenswarm.enterprise.team.quota import KIND_TEAM_CREATE, team_create_key

    actor = get_team_mysql_actor()
    key = team_create_key(session_id) if session_id else ""

    async def _op() -> tuple[bool, str | None]:
        async def _tx(conn: Any) -> tuple[bool, str | None]:
            lease_uid = ""
            team_id = ""
            entity_uid = ""
            entity_name = team_name
            entity_id = ""
            binding_uid = ""
            binding_sid = ""
            binding_tid = ""

            if key:
                lease = (
                    await conn.execute(
                        text(
                            "SELECT user_id, team_id FROM team_quota_lease "
                            "WHERE kind=:kind AND idempotency_key=:key"
                        ),
                        {"kind": KIND_TEAM_CREATE, "key": key},
                    )
                ).mappings().first()
                if lease is not None:
                    lease_uid = _clean(lease.get("user_id"))
                    team_id = _clean(lease.get("team_id"))

            entity = None
            if session_id:
                entity = (
                    await conn.execute(
                        text(
                            "SELECT team_id, team_name, owner_user_id FROM team_entity "
                            "WHERE team_session_id=:sid ORDER BY updated_at DESC LIMIT 1"
                        ),
                        {"sid": session_id},
                    )
                ).mappings().first()
            if entity is None and team_name:
                entity = (
                    await conn.execute(
                        text(
                            "SELECT team_id, team_name, owner_user_id FROM team_entity "
                            "WHERE team_name=:name LIMIT 1"
                        ),
                        {"name": team_name},
                    )
                ).mappings().first()
            if entity is None and team_id:
                entity = (
                    await conn.execute(
                        text(
                            "SELECT team_id, team_name, owner_user_id FROM team_entity "
                            "WHERE team_id=:tid"
                        ),
                        {"tid": team_id},
                    )
                ).mappings().first()
            if entity is not None:
                entity_id = _clean(entity.get("team_id"))
                entity_name = _clean(entity.get("team_name")) or entity_name
                entity_uid = _clean(entity.get("owner_user_id"))
                team_id = entity_id or team_id

            if session_id:
                binding = (
                    await conn.execute(
                        text(
                            "SELECT team_id, team_session_id, owner_user_id "
                            "FROM team_route_binding WHERE team_session_id=:sid"
                        ),
                        {"sid": session_id},
                    )
                ).mappings().first()
                if binding is not None:
                    binding_uid = _clean(binding.get("owner_user_id"))
                    binding_sid = _clean(binding.get("team_session_id"))
                    binding_tid = _clean(binding.get("team_id"))
                    team_id = team_id or binding_tid
            if not binding_tid and team_id:
                binding = (
                    await conn.execute(
                        text(
                            "SELECT team_id, team_session_id, owner_user_id "
                            "FROM team_route_binding WHERE team_id=:tid "
                            "ORDER BY updated_at DESC LIMIT 1"
                        ),
                        {"tid": team_id},
                    )
                ).mappings().first()
                if binding is not None:
                    binding_uid = binding_uid or _clean(binding.get("owner_user_id"))
                    binding_sid = _clean(binding.get("team_session_id"))
                    binding_tid = _clean(binding.get("team_id"))

            class _Owner:
                def __init__(self, user_id: str) -> None:
                    self.user_id = user_id

            class _Entity:
                def __init__(self, user_id: str) -> None:
                    self.owner = _Owner(user_id)

            class _Binding:
                def __init__(self, user_id: str) -> None:
                    self.owner_user_id = user_id

            occupancy_owner = _occupancy_owner_user_id(
                entity=_Entity(entity_uid) if entity_uid else None,
                lease={"user_id": lease_uid} if lease_uid else None,
                binding=_Binding(binding_uid) if binding_uid else None,
            )
            if require_owner and (not owner_user_id or occupancy_owner != owner_user_id):
                logger.info(
                    "[TeamCleanup] skip purge, owner mismatch: session_id=%s "
                    "caller=%s occupancy_owner=%s",
                    session_id,
                    owner_user_id or "-",
                    occupancy_owner or "-",
                )
                return False, None

            if entity_id:
                await conn.execute(
                    text(
                        "UPDATE team_entity SET status=:status, updated_at=:now "
                        "WHERE team_id=:tid"
                    ),
                    {"status": TEAM_STATUS_DELETED, "now": time.time(), "tid": entity_id},
                )
                await conn.execute(
                    text("DELETE FROM team_membership WHERE team_id=:tid"),
                    {"tid": entity_id},
                )
                await conn.execute(
                    text("DELETE FROM team_entity WHERE team_id=:tid"),
                    {"tid": entity_id},
                )
            if team_id:
                await conn.execute(
                    text("DELETE FROM team_quota_lease WHERE team_id=:tid"),
                    {"tid": team_id},
                )
            if key:
                await conn.execute(
                    text(
                        "DELETE FROM team_quota_lease WHERE kind=:kind "
                        "AND idempotency_key=:key"
                    ),
                    {"kind": KIND_TEAM_CREATE, "key": key},
                )
            if session_id:
                await conn.execute(
                    text("DELETE FROM team_route_binding WHERE team_session_id=:sid"),
                    {"sid": session_id},
                )
            extra_sid = binding_sid if binding_sid and binding_sid != session_id else ""
            if extra_sid:
                await conn.execute(
                    text("DELETE FROM team_route_binding WHERE team_session_id=:sid"),
                    {"sid": extra_sid},
                )
            return True, (team_id or None)

        return await run_tx(actor.handler, _tx)

    try:
        allowed, team_id = actor.call(_op)
    except Exception:
        logger.warning(
            "[TeamCleanup] mysql purge failed: session_id=%s team_name=%s",
            session_id,
            team_name,
            exc_info=True,
        )
        return None
    if not allowed:
        return None

    resolved_name = team_name
    if session_id:
        try:
            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

            get_team_binding_store().unbind_session(
                session_id=session_id,
                team_name=resolved_name or None,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TeamCleanup] local team binding unbind skipped: session_id=%s",
                session_id,
                exc_info=True,
            )
    if team_id or session_id:
        logger.info(
            "[TeamCleanup] purged team for deleted session: session_id=%s team_id=%s team_name=%s",
            session_id,
            team_id,
            resolved_name,
        )
    return team_id or None


def purge_team_for_deleted_session(
    *,
    session_id: str = "",
    team_name: str = "",
    owner_user_id: str = "",
    require_owner: bool = False,
) -> str | None:
    """Remove team occupancy for a deleted Session. Idempotent.

    Returns the ``team_id`` that was purged, if one was found.

    When ``require_owner`` is true, occupancy is dropped only if the caller
    ``owner_user_id`` matches the entity / create-lease / route-binding owner.
    Gateway uses this for leftover rows after a PG ownership miss so knowing a
    ``session_id`` cannot wipe another user's team tables.
    """
    sid = _clean(session_id)
    name = _clean(team_name)
    caller = _clean(owner_user_id)
    if not sid and not name:
        return None

    from jiuwenswarm.infrastructure.db.team_mysql_actor import use_team_mysql
    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

    # Injected SQLite stores keep the original path. Production MySQL uses one call.
    quota_on_mysql = False
    try:
        quota_on_mysql = bool(get_team_quota_service()._use_mysql())
    except Exception:  # noqa: BLE001
        quota_on_mysql = False
    if use_team_mysql() and quota_on_mysql:
        return _purge_mysql(
            session_id=sid,
            team_name=name,
            owner_user_id=caller,
            require_owner=require_owner,
        )

    entity = None
    team_id = ""
    resolved_name = name
    lease: dict[str, str] | None = None
    binding = None

    try:
        from jiuwenswarm.enterprise.team.quota import get_team_quota_service

        if sid:
            lease = get_team_quota_service().peek_team_create_lease(sid)
            if lease:
                team_id = _clean(lease.get("team_id"))
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TeamCleanup] peek create-lease skipped: session_id=%s",
            sid,
            exc_info=True,
        )

    try:
        from jiuwenswarm.enterprise.team.control_store import get_team_control_store

        store = get_team_control_store()
        if sid:
            entity = store.get_entity_by_session(sid)
        if entity is None and name:
            entity = store.get_entity_by_name(name)
        if entity is None and team_id:
            entity = store.get_entity(team_id)
        if entity is not None:
            team_id = _clean(entity.team_id) or team_id
            resolved_name = _clean(entity.team_name) or name
    except Exception:  # noqa: BLE001 - control-plane purge must not fail the chat delete
        logger.warning(
            "[TeamCleanup] failed to load team entity: session_id=%s team_name=%s",
            sid,
            name,
            exc_info=True,
        )

    if sid or team_id:
        try:
            from jiuwenswarm.enterprise.team.route_binding_store import (
                get_team_route_binding_store,
            )

            route_store = get_team_route_binding_store()
            if sid:
                binding = route_store.get_by_session(sid)
            if binding is None and team_id:
                getter = getattr(route_store, "get_by_team_id", None)
                if callable(getter):
                    binding = getter(team_id)
            if binding is not None:
                team_id = team_id or _clean(getattr(binding, "team_id", ""))
                if not sid:
                    sid = _clean(getattr(binding, "team_session_id", ""))
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TeamCleanup] route binding lookup skipped: session_id=%s team_id=%s",
                sid,
                team_id,
                exc_info=True,
            )

    occupancy_owner = _occupancy_owner_user_id(
        entity=entity, lease=lease, binding=binding,
    )
    if require_owner:
        if not caller:
            return None
        if occupancy_owner != caller:
            logger.info(
                "[TeamCleanup] skip purge, owner mismatch: session_id=%s "
                "caller=%s occupancy_owner=%s",
                sid,
                caller,
                occupancy_owner or "-",
            )
            return None

    try:
        from jiuwenswarm.enterprise.team.control_store import (
            TEAM_STATUS_DELETED,
            get_team_control_store,
        )

        if entity is not None:
            store = get_team_control_store()
            store.set_status(entity.team_id, TEAM_STATUS_DELETED)
            store.delete_entity(entity.team_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[TeamCleanup] failed to delete team entity: session_id=%s team_name=%s",
            sid,
            name,
            exc_info=True,
        )

    try:
        from jiuwenswarm.enterprise.team.quota import get_team_quota_service

        get_team_quota_service().drop_occupancy(session_id=sid, team_id=team_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[TeamCleanup] failed to drop quota leases: session_id=%s team_id=%s",
            sid,
            team_id,
            exc_info=True,
        )

    try:
        from jiuwenswarm.enterprise.team.route_binding_store import (
            get_team_route_binding_store,
        )

        route_store = get_team_route_binding_store()
        if sid:
            route_store.delete(sid)
        if team_id:
            deleter = getattr(route_store, "delete_by_team_id", None)
            if callable(deleter):
                deleter(team_id)
            else:
                getter = getattr(route_store, "get_by_team_id", None)
                extra = getter(team_id) if callable(getter) else None
                extra_sid = _clean(getattr(extra, "team_session_id", ""))
                if extra_sid and extra_sid != sid:
                    route_store.delete(extra_sid)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[TeamCleanup] route binding delete skipped: session_id=%s team_id=%s",
            sid,
            team_id,
            exc_info=True,
        )

    if sid:
        try:
            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

            get_team_binding_store().unbind_session(
                session_id=sid,
                team_name=resolved_name or None,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TeamCleanup] local team binding unbind skipped: session_id=%s",
                sid,
                exc_info=True,
            )

    if team_id or sid:
        logger.info(
            "[TeamCleanup] purged team for deleted session: session_id=%s team_id=%s team_name=%s",
            sid,
            team_id,
            resolved_name,
        )
    return team_id or None


_SWEEP_LOCK_NAME = ".team-session-reconcile.lock"
_startup_sweep_task: asyncio.Task | None = None


def _session_id_is_safe(session_id: str) -> bool:
    sid = _clean(session_id)
    if not sid or sid in {".", ".."}:
        return False
    return "/" not in sid and "\\" not in sid


def _owner_session_dir(owner: OwnerIdentity, session_id: str) -> Path:
    """Session directory for this owner, using the same root as session writes."""
    from jiuwenswarm.common.utils import get_agent_root_dir
    from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key

    key = build_tenant_key(owner.group_id, owner.bot_id, owner.user_id)
    return get_agent_root_dir(key) / "sessions" / session_id


@contextmanager
def _sweep_lock(volume: Path) -> Iterator[None]:
    """One AgentServer replica sweeps at a time. The purge itself is idempotent."""
    handle = open(volume / _SWEEP_LOCK_NAME, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def reconcile_teams_with_sessions(*, identity: OwnerIdentity | None = None) -> int:
    """Drop team occupancy whose session directory is gone.

    A missing data-volume root means the volume is not mounted. In that case
    this returns 0 and deletes nothing. A mounted volume whose session directory
    is absent still purges, because that session is gone. ``identity`` limits
    the scan to that owner; ``None`` scans every team. Returns how many session
    ids were purged.
    """
    from jiuwenswarm.common.utils import get_user_workspace_dir

    volume = get_user_workspace_dir()
    if not volume.is_dir():
        logger.warning("[TeamCleanup] skip reconcile, data volume missing: %s", volume)
        return 0
    with _sweep_lock(volume):
        return _reconcile_unlocked(identity)


def _reconcile_unlocked(identity: OwnerIdentity | None) -> int:
    candidates: dict[str, OwnerIdentity] = {}
    try:
        from jiuwenswarm.enterprise.team.control_store import get_team_control_store

        for entity in get_team_control_store().list_entities(owner=identity):
            sid = _clean(getattr(entity, "team_session_id", ""))
            if _session_id_is_safe(sid):
                candidates[sid] = entity.owner
    except Exception:  # noqa: BLE001
        logger.warning("[TeamCleanup] reconcile entity scan failed", exc_info=True)

    try:
        from jiuwenswarm.enterprise.team.quota import get_team_quota_service

        for sid, owner in get_team_quota_service().list_occupying_team_create_sessions(identity):
            if _session_id_is_safe(sid) and sid not in candidates:
                candidates[sid] = owner
    except Exception:  # noqa: BLE001
        logger.warning("[TeamCleanup] reconcile lease scan failed", exc_info=True)

    purged = 0
    for sid in sorted(candidates):
        if _owner_session_dir(candidates[sid], sid).is_dir():
            continue
        try:
            purge_team_for_deleted_session(session_id=sid)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[TeamCleanup] reconcile purge failed: session_id=%s",
                sid,
                exc_info=True,
            )
            continue
        purged += 1
    if purged:
        logger.info("[TeamCleanup] reconciled %s team session(s) without a directory", purged)
    return purged


def bind_startup_sweep(task: asyncio.Task) -> None:
    """Remember the one startup sweep so list and create can wait for it."""
    global _startup_sweep_task
    _startup_sweep_task = task


def reset_startup_sweep() -> None:
    """Drop the startup sweep handle. Tests use this between cases."""
    global _startup_sweep_task
    _startup_sweep_task = None


async def wait_for_startup_sweep() -> None:
    """Wait for the startup sweep. Returns immediately once it has finished.

    When the process never started a sweep, this is a no-op so callers still
    read the control store.
    """
    task = _startup_sweep_task
    if task is None or task.done():
        return
    try:
        await asyncio.shield(task)
    except Exception:  # noqa: BLE001
        logger.warning("[TeamCleanup] startup sweep failed", exc_info=True)


async def run_startup_sweep() -> int:
    """Sweep once. There is no repeating interval."""
    from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

    try:
        purged = await run_team_store(reconcile_teams_with_sessions)
    except Exception:  # noqa: BLE001
        logger.warning("[TeamCleanup] startup reconcile failed", exc_info=True)
        return 0
    logger.info("[TeamCleanup] startup reconcile purged=%s", purged)
    return int(purged or 0)


__all__ = [
    "bind_startup_sweep",
    "purge_team_for_deleted_session",
    "reconcile_teams_with_sessions",
    "reset_startup_sweep",
    "run_startup_sweep",
    "wait_for_startup_sweep",
]
