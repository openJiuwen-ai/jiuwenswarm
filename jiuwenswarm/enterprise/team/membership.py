# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human-member membership service for enterprise teams (phase 1).

Bridges ``TeamControlStore`` (who joined which team as which seat) with the
agent-core HITT runtime (``TeamBackend.spawn_human_agent`` /
``HumanAgentInbox``). All entry points are usable from AgentServer handlers
and from the ``team.runtime_ready`` hook in ``team_helpers``.

Lifecycle::

    join (runtime active)   -> seat spawned now   -> membership ACTIVE
    join (runtime inactive) -> membership PENDING -> materialized on runtime_ready
    leave                   -> membership LEFT    (seat row is kept; leader may shutdown)

Seat naming: callers may pass ``member_name``; otherwise ``human-<hash6>`` is
derived from the user id. Names must match agent-core's member-name pattern.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.enterprise.team.control_store import (
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_LEFT,
    MEMBERSHIP_STATUS_PENDING,
    OwnerIdentity,
    TeamControlStoreError,
    TeamEntityRecord,
    TeamMembershipRecord,
    get_team_control_store,
)

logger = logging.getLogger(__name__)

# Mirrors openjiuwen.agent_teams.tools.tool_permissions._MEMBER_NAME_PATTERN.
MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
HUMAN_AGENT_ROLE = "human_agent"


class TeamMembershipError(ValueError):
    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JoinResult:
    membership: TeamMembershipRecord
    entity: TeamEntityRecord
    seat_ready: bool
    reused_seat: bool = False


def default_member_name(user_id: str) -> str:
    digest = hashlib.sha1(str(user_id or "").encode("utf-8")).hexdigest()[:6]
    return f"human-{digest}"


def validate_member_name(member_name: str) -> str:
    name = str(member_name or "").strip()
    if not name:
        raise TeamMembershipError("member_name is required")
    if not MEMBER_NAME_RE.match(name):
        raise TeamMembershipError(
            "member_name must start with a lowercase letter and contain only "
            "lowercase letters, digits or hyphen"
        )
    return name


async def _active_team_backend(runtime_team_name: str) -> Any | None:
    """Return the live ``TeamBackend`` for a Runner-owned leader, if any."""
    if not runtime_team_name:
        return None
    try:
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            _current_pool_team_agent,
        )

        agent = await _current_pool_team_agent(runtime_team_name)
    except Exception:  # noqa: BLE001
        logger.debug("[TeamMembership] pool lookup failed", exc_info=True)
        return None
    if agent is None:
        return None
    return getattr(agent, "team_backend", None)


async def _db_member_roles(runtime_team_name: str) -> dict[str, str]:
    """``member_name -> role`` from team.db (works without a live runtime)."""
    if not runtime_team_name:
        return {}
    try:
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )

        members = await TeamMonitorHandler.get_member_list_from_db(runtime_team_name)
    except Exception:  # noqa: BLE001
        logger.debug("[TeamMembership] db member lookup failed", exc_info=True)
        return {}
    roles: dict[str, str] = {}
    for item in members or []:
        if isinstance(item, dict) and item.get("member_id"):
            roles[str(item["member_id"])] = str(item.get("role") or "")
    return roles


async def _spawn_seat(
    backend: Any,
    *,
    member_name: str,
    display_name: str,
    existing_roles: dict[str, str],
) -> tuple[bool, bool]:
    """Ensure a HITT seat exists on a live backend.

    Returns ``(ready, reused)``. Raises ``TeamMembershipError`` when the seat
    name is taken by a non-human member.
    """
    role = existing_roles.get(member_name)
    if role:
        if role != HUMAN_AGENT_ROLE:
            raise TeamMembershipError(
                f"member_name '{member_name}' belongs to a non-human member", code="CONFLICT"
            )
        return True, True
    spawn = getattr(backend, "spawn_human_agent", None)
    if not callable(spawn):
        return False, False
    result = await spawn(member_name=member_name, display_name=display_name or None)
    ok = bool(getattr(result, "ok", result))
    if not ok:
        reason = str(getattr(result, "reason", "") or "spawn_human_agent failed")
        if "already exists" in reason:
            return True, True
        raise TeamMembershipError(reason, code="SPAWN_FAILED")
    return True, False


def _broadcast_membership(
    channel_id: str | None,
    session_id: str,
    membership: TeamMembershipRecord,
    action: str,
) -> None:
    if not session_id:
        return
    try:
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _broadcast_event

        _broadcast_event(
            channel_id,
            session_id,
            {
                "event_type": "team.membership",
                "action": action,
                "session_id": session_id,
                "team_id": membership.team_id,
                "member_name": membership.member_name,
                "display_name": membership.display_name or membership.member_name,
                "user_id": membership.identity.user_id,
                "status": membership.status,
                "mode": "human",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[TeamMembership] broadcast failed", exc_info=True)


def resolve_entity(
    *, team_id: str = "", session_id: str = "", team_name: str = ""
) -> TeamEntityRecord:
    store = get_team_control_store()
    entity = None
    if team_id:
        entity = store.get_entity(team_id)
    if entity is None and session_id:
        entity = store.get_entity_by_session(session_id)
    if entity is None and team_name:
        entity = store.get_entity_by_name(team_name)
    if entity is None:
        raise TeamMembershipError("team not found", code="NOT_FOUND")
    return entity


def check_join_allowed(entity: TeamEntityRecord, identity: OwnerIdentity) -> None:
    """Phase-1 authorization: owner, or same routing scope (group + bot)."""
    if entity.status == "deleted":
        raise TeamMembershipError("team has been deleted", code="NOT_FOUND")
    if entity.owner.user_id == identity.user_id and entity.owner.same_scope(identity):
        return
    if not entity.owner.same_scope(identity):
        raise TeamMembershipError(
            "cross-tenant join requires an invitation token", code="FORBIDDEN"
        )


async def join_team(
    *,
    identity: OwnerIdentity,
    team_id: str = "",
    session_id: str = "",
    team_name: str = "",
    member_name: str = "",
    display_name: str = "",
    channel_id: str | None = None,
) -> JoinResult:
    entity = resolve_entity(team_id=team_id, session_id=session_id, team_name=team_name)
    check_join_allowed(entity, identity)
    store = get_team_control_store()

    existing = store.get_membership(entity.team_id, identity.user_id)
    if existing is not None and existing.status in {MEMBERSHIP_STATUS_ACTIVE, MEMBERSHIP_STATUS_PENDING}:
        if member_name and member_name != existing.member_name:
            raise TeamMembershipError(
                f"you already hold seat '{existing.member_name}'", code="CONFLICT"
            )
        member_name = existing.member_name
    seat = validate_member_name(member_name or (existing.member_name if existing else "") or default_member_name(identity.user_id))

    # Seat must not be held by another user.
    holder = store.get_membership_by_member_name(entity.team_id, seat)
    if holder is not None and holder.identity.user_id != identity.user_id and holder.status in {
        MEMBERSHIP_STATUS_ACTIVE, MEMBERSHIP_STATUS_PENDING,
    }:
        raise TeamMembershipError(f"seat '{seat}' is already taken", code="CONFLICT")

    runtime_team_name = entity.runtime_team_name
    backend = await _active_team_backend(runtime_team_name)
    seat_ready = False
    reused = False
    if backend is not None:
        roles = await _db_member_roles(runtime_team_name)
        seat_ready, reused = await _spawn_seat(
            backend, member_name=seat, display_name=display_name, existing_roles=roles,
        )
    else:
        # Runtime idle: seat may already exist in team.db from a previous round.
        roles = await _db_member_roles(runtime_team_name)
        role = roles.get(seat)
        if role and role != HUMAN_AGENT_ROLE:
            raise TeamMembershipError(
                f"member_name '{seat}' belongs to a non-human member", code="CONFLICT"
            )
        seat_ready = bool(role)
        reused = bool(role)

    status = MEMBERSHIP_STATUS_ACTIVE if seat_ready else MEMBERSHIP_STATUS_PENDING
    try:
        membership = store.upsert_membership(
            team_id=entity.team_id,
            identity=identity,
            member_name=seat,
            display_name=display_name,
            status=status,
        )
    except TeamControlStoreError as exc:
        raise TeamMembershipError(str(exc), code=exc.code) from exc

    logger.info(
        "[TeamMembership] join: team_id=%s user=%s seat=%s status=%s seat_ready=%s reused=%s",
        entity.team_id, identity.user_id, seat, status, seat_ready, reused,
    )
    _broadcast_membership(channel_id, entity.team_session_id or session_id, membership, "joined")
    return JoinResult(membership=membership, entity=entity, seat_ready=seat_ready, reused_seat=reused)


async def leave_team(
    *,
    identity: OwnerIdentity,
    team_id: str = "",
    session_id: str = "",
    team_name: str = "",
    channel_id: str | None = None,
) -> TeamMembershipRecord:
    entity = resolve_entity(team_id=team_id, session_id=session_id, team_name=team_name)
    store = get_team_control_store()
    membership = store.get_membership(entity.team_id, identity.user_id)
    if membership is None or membership.status == MEMBERSHIP_STATUS_LEFT:
        raise TeamMembershipError("you are not a member of this team", code="NOT_FOUND")
    updated = store.set_membership_status(membership.membership_id, MEMBERSHIP_STATUS_LEFT)
    assert updated is not None
    logger.info(
        "[TeamMembership] leave: team_id=%s user=%s seat=%s",
        entity.team_id, identity.user_id, membership.member_name,
    )
    _broadcast_membership(channel_id, entity.team_session_id or session_id, updated, "left")
    return updated


async def materialize_pending_memberships(
    *,
    runtime_team_name: str,
    session_id: str,
    channel_id: str | None = None,
) -> list[TeamMembershipRecord]:
    """On ``team.runtime_ready``: spawn seats for PENDING memberships."""
    store = get_team_control_store()
    entity = store.get_entity_by_session(session_id)
    if entity is None:
        return []
    pending = store.list_memberships(entity.team_id, statuses=[MEMBERSHIP_STATUS_PENDING])
    if not pending:
        return []
    backend = await _active_team_backend(runtime_team_name)
    if backend is None:
        logger.debug(
            "[TeamMembership] materialize skipped (no backend): team=%s session=%s",
            runtime_team_name, session_id,
        )
        return []
    roles = await _db_member_roles(runtime_team_name)
    activated: list[TeamMembershipRecord] = []
    for membership in pending:
        try:
            ready, _reused = await _spawn_seat(
                backend,
                member_name=membership.member_name,
                display_name=membership.display_name,
                existing_roles=roles,
            )
        except TeamMembershipError as exc:
            logger.warning(
                "[TeamMembership] materialize failed: team=%s seat=%s reason=%s",
                runtime_team_name, membership.member_name, exc,
            )
            continue
        if not ready:
            continue
        updated = store.set_membership_status(membership.membership_id, MEMBERSHIP_STATUS_ACTIVE)
        if updated is not None:
            activated.append(updated)
            _broadcast_membership(channel_id, session_id, updated, "joined")
    if activated:
        logger.info(
            "[TeamMembership] materialized %d pending seat(s): team=%s session=%s",
            len(activated), runtime_team_name, session_id,
        )
    return activated


def resolve_sender_member_name(
    *, session_id: str, user_id: str, requested_member_name: str = ""
) -> str:
    """Server-side guard for ``$member`` prefixes.

    Returns the seat the caller legitimately holds (ACTIVE membership) or ``""``.
    A requested name that does not match the caller's seat is dropped, so a
    client cannot impersonate another human member. Owner requests without a
    membership are plain GodView input (empty string).
    """
    uid = str(user_id or "").strip()
    sid = str(session_id or "").strip()
    if not uid or not sid:
        return ""
    try:
        store = get_team_control_store()
        entity = store.get_entity_by_session(sid)
        if entity is None:
            return ""
        membership = store.get_membership(entity.team_id, uid)
    except Exception:  # noqa: BLE001
        logger.debug("[TeamMembership] resolve_sender_member_name failed", exc_info=True)
        return ""
    if membership is None or not membership.is_active:
        return ""
    wanted = str(requested_member_name or "").strip()
    if wanted and wanted != membership.member_name:
        logger.warning(
            "[TeamMembership] member_name mismatch dropped: session=%s user=%s wanted=%s held=%s",
            sid, uid, wanted, membership.member_name,
        )
    return membership.member_name


__all__ = [
    "JoinResult",
    "TeamMembershipError",
    "check_join_allowed",
    "default_member_name",
    "join_team",
    "leave_team",
    "materialize_pending_memberships",
    "resolve_entity",
    "resolve_sender_member_name",
    "validate_member_name",
]
