# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise team control-plane handlers.

Methods: ``team.create`` / ``team.get`` / ``team.list`` / ``team.stop`` /
``team.member.join`` / ``team.member.leave`` / ``team.members.list``.

These sit on top of the existing ``handlers/team.py`` binding/entity flows:
``team.create`` = binding create (template or generated) + session bind +
``TeamEntityRecord(status=creating)`` in one call, so the web client no longer
has to orchestrate three RPCs.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.request_identity import web_routing_identity
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.team import _server_global
from jiuwenswarm.enterprise.team.control_store import (
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_PENDING,
    OwnerIdentity,
    TEAM_STATUS_CREATING,
    TEAM_STATUS_DELETED,
    TEAM_STATUS_STOPPED,
    TeamControlStoreError,
    TeamEntityRecord,
    get_team_control_store,
)
from jiuwenswarm.enterprise.team.membership import (
    TeamMembershipError,
    join_team,
    leave_team,
    resolve_entity,
)

logger = logging.getLogger(__name__)


async def _wait_for_startup_sweep() -> None:
    """Read the control store only after the one startup sweep has finished."""
    try:
        from jiuwenswarm.enterprise.team.session_cleanup import wait_for_startup_sweep

        await wait_for_startup_sweep()
    except Exception:  # noqa: BLE001
        logger.warning("[TeamControl] startup sweep wait failed", exc_info=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _params(ctx: RequestContext) -> dict[str, Any]:
    params = ctx.request.params
    return params if isinstance(params, dict) else {}


def request_identity(request: AgentRequest) -> OwnerIdentity:
    """Routing identity of the caller (``user_id`` + ``metadata.routing``)."""
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    identity = web_routing_identity(metadata)
    params = request.params if isinstance(request.params, dict) else {}
    user_id = str(identity.get("user_id") or params.get("user_id") or metadata.get("user_id") or "").strip()
    group_id = str(identity.get("group_id") or params.get("group_id") or "").strip()
    bot_id = str(identity.get("bot_id") or params.get("bot_id") or "").strip()
    return OwnerIdentity(user_id=user_id, group_id=group_id, bot_id=bot_id)


async def _reply(ctx: RequestContext, *, ok: bool, payload: dict[str, Any]) -> None:
    request = ctx.request
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
        metadata=request.metadata,
    )
    await ctx.sink.send_response(resp)


async def _fail(
    ctx: RequestContext,
    error: str,
    code: str = "BAD_REQUEST",
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"error": error, "code": code}
    if extra:
        payload.update(extra)
    await _reply(ctx, ok=False, payload=payload)


def _runtime_state(entity: TeamEntityRecord) -> dict[str, Any]:
    """Derive live runtime facts (no persistence) for a response payload."""
    state: dict[str, Any] = {"runtime_active": False, "runtime_pending": False}
    session_id = entity.team_session_id
    if not session_id:
        return state
    try:
        from jiuwenswarm.agents.harness.team import get_all_team_managers

        for manager in get_all_team_managers():
            if manager.is_runtime_active(session_id):
                state["runtime_active"] = True
            if manager.is_runtime_pending(session_id):
                state["runtime_pending"] = True
    except Exception:  # noqa: BLE001
        logger.debug("[TeamControl] runtime state lookup failed", exc_info=True)
    return state


def _entity_payload(entity: TeamEntityRecord, *, with_members: bool = False) -> dict[str, Any]:
    payload = entity.to_dict()
    payload.update(_runtime_state(entity))
    if with_members:
        store = get_team_control_store()
        payload["memberships"] = [m.to_dict() for m in store.list_memberships(entity.team_id)]
    return payload


def _is_owner(entity: TeamEntityRecord, identity: OwnerIdentity) -> bool:
    return entity.owner.user_id == identity.user_id and entity.owner.same_scope(identity)


def _can_view(entity: TeamEntityRecord, identity: OwnerIdentity) -> bool:
    if _is_owner(entity, identity):
        return True
    store = get_team_control_store()
    membership = store.get_membership(entity.team_id, identity.user_id)
    return membership is not None and membership.status in {
        MEMBERSHIP_STATUS_ACTIVE,
        MEMBERSHIP_STATUS_PENDING,
    }


# ---------------------------------------------------------------------------
# team.create
# ---------------------------------------------------------------------------


async def handle_team_create(ctx: RequestContext) -> None:
    """Create a team binding (+entity) and bind it to ``session_id`` in one step.

    params: ``session_id`` (required), one of ``team_name + template_id`` or
    ``description`` (LLM-generated name), optional ``mode``.
    """
    request = ctx.request
    params = _params(ctx)
    from jiuwenswarm.agents.harness.team import TeamManager, get_team_template_snapshot
    from jiuwenswarm.agents.harness.team.config_loader import TeamTemplateNotFoundError
    from jiuwenswarm.runtime.session_delete import TEAM_DELETION_GATE
    from jiuwenswarm.server.handlers.team import (
        _create_generated_team_binding,
        _create_team_binding_from_template,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata
    from jiuwenswarm.server.runtime.team_binding_store import (
        TeamBindingStoreError,
        get_team_binding_store,
    )
    from jiuwenswarm.server.runtime.team_entity_store import (
        TeamEntityStoreError,
        get_team_entity_store,
    )

    session_id = str(params.get("session_id") or request.session_id or "").strip()
    team_name = str(params.get("team_name") or "").strip()
    template_id = str(params.get("template_id") or "").strip()
    description = str(params.get("description") or params.get("prompt") or "").strip()
    _, _, canonical_mode = _server_global("resolve_agent_request_mode")(params.get("mode", "team"))
    identity = request_identity(request)

    if not session_id:
        await _fail(ctx, "session_id is required")
        return
    if not identity.user_id:
        await _fail(ctx, "user identity is required to create a team", "FORBIDDEN")
        return
    if not (team_name and template_id) and not description:
        await _fail(ctx, "either team_name+template_id or description is required")
        return

    session_dir = get_agent_sessions_dir() / session_id
    if not session_id or session_id in {".", ".."} or "/" in session_id or "\\" in session_id:
        await _fail(ctx, "invalid session_id")
        return
    if not session_dir.is_dir():
        await _fail(ctx, "session not found", "NOT_FOUND")
        return

    await _wait_for_startup_sweep()

    control = get_team_control_store()
    binding_store = get_team_binding_store()
    entity_store = get_team_entity_store()
    binding = None
    control_entity: TeamEntityRecord | None = None
    quota_lease = None
    try:
        async with TEAM_DELETION_GATE.mutation_lock(session_id):
            config_base = _server_global("get_config")()
            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store, use_team_mysql
            from jiuwenswarm.enterprise.team.quota import (
                QUOTA_EXCEEDED,
                SessionAlreadyHasTeam,
                TeamQuotaExceeded,
                count_predefined_non_leaders,
                get_team_quota_service,
                team_quota_user_message,
                teammate_spawn_slots,
            )

            def _begin_team_create():
                quota = get_team_quota_service()
                if use_team_mysql():
                    return quota.reserve_team_create(
                        identity,
                        session_id,
                        pending_non_leaders=count_predefined_non_leaders(
                            config_base, template_id or None,
                        ),
                        on_existing="reject",
                    )
                existing = control.get_entity_by_session(session_id)
                if existing is not None and existing.status != TEAM_STATUS_DELETED:
                    raise SessionAlreadyHasTeam(session_id)
                limits = quota.resolve_limits(identity)
                pending_y = count_predefined_non_leaders(config_base, template_id or None)
                n = limits.max_teammates_per_team
                if pending_y > teammate_spawn_slots(n):
                    raise TeamQuotaExceeded(
                        f"team quota exceeded: {pending_y + 1}/{n} members including leader",
                        kind="teammate_spawn",
                        used=pending_y + 1,
                        limit=n,
                        default=limits.default_teammates,
                    )
                return quota.reserve_team_create(identity, session_id)

            try:
                quota_lease = await run_team_store(_begin_team_create)
            except SessionAlreadyHasTeam:
                await _fail(ctx, "session already has a team", "CONFLICT")
                return
            except TeamQuotaExceeded as exc:
                await _fail(ctx, team_quota_user_message(exc), QUOTA_EXCEEDED, extra={"quota": exc.as_quota()})
                return
            if team_name and template_id:
                binding = await _create_team_binding_from_template(
                    team_name=team_name, template_id=template_id, config_base=config_base,
                )
                template_summary: dict[str, Any] | None = None
            else:
                binding, template_summary = await _create_generated_team_binding(
                    description=description, config_base=config_base,
                )

            runtime_team_name = TeamManager.build_session_scoped_team_name(
                binding.team_name, session_id,
            )
            control_entity = await run_team_store(
                control.create_entity,
                team_name=binding.team_name,
                owner=identity,
                template_id=binding.template_id,
                team_session_id=session_id,
                runtime_team_name=runtime_team_name,
                status=TEAM_STATUS_CREATING,
                extra={"channel_id": request.channel_id or "", "mode": canonical_mode},
            )

            try:
                get_team_template_snapshot(config_base, template_id=binding.template_id)
            except TeamTemplateNotFoundError:
                logger.debug("[TeamControl] template snapshot missing: %s", binding.template_id)
            update_session_metadata(
                session_id=session_id,
                channel_id=str(request.channel_id or "").strip() or None,
                mode=canonical_mode,
                team_name=binding.team_name,
                team_template_id=binding.template_id,
                user_id=identity.user_id or None,
                sync=True,
            )
            binding = binding_store.bind_session(team_name=binding.team_name, session_id=session_id)
    except (TeamBindingStoreError, TeamEntityStoreError, TeamControlStoreError) as exc:
        from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

        await run_team_store(
            _rollback_create, control, binding_store, entity_store, binding, control_entity, quota_lease,
        )
        await _fail(ctx, str(exc), getattr(exc, "code", "BAD_REQUEST"))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TeamControl] team.create failed: session=%s", session_id)
        from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

        await run_team_store(
            _rollback_create, control, binding_store, entity_store, binding, control_entity, quota_lease,
        )
        await _fail(ctx, f"team create failed: {exc}", "INTERNAL_ERROR")
        return

    if quota_lease is not None and control_entity is not None:
        try:
            from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store
            from jiuwenswarm.enterprise.team.quota import get_team_quota_service

            await run_team_store(
                get_team_quota_service().commit_lease,
                quota_lease,
                team_id=control_entity.team_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[TeamControl] quota commit failed: team_id=%s", control_entity.team_id)

    payload = {
        "team": _entity_payload(control_entity),
        "binding": binding.to_dict(),
        "session_id": session_id,
        "mode": canonical_mode,
    }
    if template_summary:
        payload["template"] = template_summary
    await _reply(ctx, ok=True, payload=payload)


def _rollback_create(control, binding_store, entity_store, binding, control_entity, quota_lease=None) -> None:
    if quota_lease is not None:
        try:
            from jiuwenswarm.enterprise.team.quota import get_team_quota_service

            get_team_quota_service().release_lease(quota_lease)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamControl] rollback quota lease failed", exc_info=True)
    if control_entity is not None:
        try:
            from jiuwenswarm.enterprise.team.quota import get_team_quota_service

            get_team_quota_service().release_team(control_entity.team_id)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamControl] rollback quota team failed", exc_info=True)
        try:
            control.delete_entity(control_entity.team_id)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamControl] rollback control entity failed", exc_info=True)
    if binding is not None:
        for step in (
            lambda: binding_store.delete(binding.team_name),
            lambda: entity_store.delete_team_directory(binding.team_name),
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                logger.debug("[TeamControl] rollback binding failed", exc_info=True)


# ---------------------------------------------------------------------------
# team.get / team.list
# ---------------------------------------------------------------------------


async def handle_team_get(ctx: RequestContext) -> None:
    params = _params(ctx)
    identity = request_identity(ctx.request)
    try:
        entity = resolve_entity(
            team_id=str(params.get("team_id") or ""),
            session_id=str(params.get("session_id") or ctx.request.session_id or ""),
            team_name=str(params.get("team_name") or ""),
        )
    except TeamMembershipError as exc:
        await _fail(ctx, str(exc), exc.code)
        return
    if identity.user_id and not _can_view(entity, identity):
        await _fail(ctx, "not a member of this team", "FORBIDDEN")
        return
    payload = {"team": _entity_payload(entity, with_members=True)}
    try:
        from jiuwenswarm.enterprise.team.quota import get_team_quota_service

        payload["teammate_quota"] = get_team_quota_service().snapshot_team(entity.team_id, entity.owner)
    except Exception:  # noqa: BLE001
        logger.debug("[TeamControl] teammate quota snapshot skipped", exc_info=True)
    await _reply(ctx, ok=True, payload=payload)


async def handle_team_list(ctx: RequestContext) -> None:
    """Teams owned by the caller plus teams the caller has joined."""
    await _wait_for_startup_sweep()
    identity = request_identity(ctx.request)
    store = get_team_control_store()
    include_deleted = bool(_params(ctx).get("include_deleted"))
    if identity.user_id:
        owned = store.list_entities(owner=identity, include_deleted=include_deleted)
        seen = {e.team_id for e in owned}
        joined: list[TeamEntityRecord] = []
        for entity in store.list_entities(include_deleted=include_deleted):
            if entity.team_id in seen or not entity.owner.same_scope(identity):
                continue
            membership = store.get_membership(entity.team_id, identity.user_id)
            if membership is not None and membership.status in {
                MEMBERSHIP_STATUS_ACTIVE, MEMBERSHIP_STATUS_PENDING,
            }:
                joined.append(entity)
        teams = owned + joined
    else:
        teams = store.list_entities(include_deleted=include_deleted)
    payload: dict[str, Any] = {"teams": [_entity_payload(e) for e in teams], "count": len(teams)}
    if identity.user_id:
        try:
            from jiuwenswarm.enterprise.team.quota import get_team_quota_service

            payload["quota"] = get_team_quota_service().snapshot_user(identity)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamControl] user quota snapshot skipped", exc_info=True)
    await _reply(ctx, ok=True, payload=payload)


# ---------------------------------------------------------------------------
# team.stop
# ---------------------------------------------------------------------------


async def handle_team_stop(ctx: RequestContext) -> None:
    params = _params(ctx)
    identity = request_identity(ctx.request)
    try:
        entity = resolve_entity(
            team_id=str(params.get("team_id") or ""),
            session_id=str(params.get("session_id") or ctx.request.session_id or ""),
            team_name=str(params.get("team_name") or ""),
        )
    except TeamMembershipError as exc:
        await _fail(ctx, str(exc), exc.code)
        return
    if identity.user_id and not _is_owner(entity, identity):
        await _fail(ctx, "only the owner can stop the team", "FORBIDDEN")
        return
    stopped = False
    if entity.team_session_id:
        try:
            from jiuwenswarm.agents.harness.team import stop_team_session_runtime_across_managers

            stopped = bool(
                await stop_team_session_runtime_across_managers(
                    session_id=entity.team_session_id, reason="team.stop: ",
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("[TeamControl] team.stop runtime stop failed: team_id=%s", entity.team_id)
    updated = get_team_control_store().set_status(entity.team_id, TEAM_STATUS_STOPPED) or entity
    try:
        from jiuwenswarm.enterprise.team.quota import get_team_quota_service

        get_team_quota_service().release_team(entity.team_id)
    except Exception:  # noqa: BLE001
        logger.exception("[TeamControl] quota release on stop failed: team_id=%s", entity.team_id)
    await _reply(
        ctx,
        ok=True,
        payload={"team": _entity_payload(updated), "runtime_stopped": stopped},
    )


# ---------------------------------------------------------------------------
# team.member.join / leave / members.list
# ---------------------------------------------------------------------------


async def handle_team_member_join(ctx: RequestContext) -> None:
    request = ctx.request
    params = _params(ctx)
    identity = request_identity(request)
    if not identity.user_id:
        await _fail(ctx, "user identity is required to join a team", "FORBIDDEN")
        return
    try:
        result = await join_team(
            identity=identity,
            team_id=str(params.get("team_id") or ""),
            session_id=str(params.get("session_id") or request.session_id or ""),
            team_name=str(params.get("team_name") or ""),
            member_name=str(params.get("member_name") or ""),
            display_name=str(params.get("display_name") or ""),
            channel_id=request.channel_id,
        )
    except TeamMembershipError as exc:
        await _fail(ctx, str(exc), exc.code)
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TeamControl] team.member.join failed")
        await _fail(ctx, str(exc), "INTERNAL_ERROR")
        return
    await _reply(
        ctx,
        ok=True,
        payload={
            "membership": result.membership.to_dict(),
            "team": _entity_payload(result.entity),
            "session_id": result.entity.team_session_id,
            "seat_ready": result.seat_ready,
            "reused_seat": result.reused_seat,
        },
    )


async def handle_team_member_leave(ctx: RequestContext) -> None:
    request = ctx.request
    params = _params(ctx)
    identity = request_identity(request)
    if not identity.user_id:
        await _fail(ctx, "user identity is required", "FORBIDDEN")
        return
    try:
        membership = await leave_team(
            identity=identity,
            team_id=str(params.get("team_id") or ""),
            session_id=str(params.get("session_id") or request.session_id or ""),
            team_name=str(params.get("team_name") or ""),
            channel_id=request.channel_id,
        )
    except TeamMembershipError as exc:
        await _fail(ctx, str(exc), exc.code)
        return
    await _reply(ctx, ok=True, payload={"membership": membership.to_dict()})


async def handle_team_members_list(ctx: RequestContext) -> None:
    params = _params(ctx)
    identity = request_identity(ctx.request)
    try:
        entity = resolve_entity(
            team_id=str(params.get("team_id") or ""),
            session_id=str(params.get("session_id") or ctx.request.session_id or ""),
            team_name=str(params.get("team_name") or ""),
        )
    except TeamMembershipError as exc:
        await _fail(ctx, str(exc), exc.code)
        return
    if identity.user_id and not _can_view(entity, identity):
        await _fail(ctx, "not a member of this team", "FORBIDDEN")
        return
    store = get_team_control_store()
    memberships = store.list_memberships(entity.team_id)
    mine = store.get_membership(entity.team_id, identity.user_id) if identity.user_id else None
    await _reply(
        ctx,
        ok=True,
        payload={
            "team_id": entity.team_id,
            "session_id": entity.team_session_id,
            "memberships": [m.to_dict() for m in memberships],
            "my_membership": mine.to_dict() if mine else None,
            "is_owner": _is_owner(entity, identity) if identity.user_id else False,
        },
    )


def _occupy_session_mysql(
    identity: Any,
    *,
    session_id: str,
    team_name: str,
    template_id: str,
    channel_id: str,
) -> None:
    """Reserve, insert the entity, and commit the lease in one MySQL call."""
    import json
    import time
    import uuid

    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.enterprise.team.control_store import (
        TEAM_STATUS_CREATING,
        get_team_control_store,
    )
    from jiuwenswarm.enterprise.team.quota import (
        KIND_TEAM_CREATE,
        get_team_quota_service,
        team_create_key,
        team_quota_enabled,
    )

    runtime_team_name = TeamManager.build_session_scoped_team_name(team_name, session_id)
    now = time.time()
    record = {
        "team_id": uuid.uuid4().hex,
        "team_name": team_name,
        "owner_user_id": identity.user_id,
        "owner_group_id": identity.group_id,
        "owner_bot_id": identity.bot_id,
        "status": TEAM_STATUS_CREATING,
        "runtime_mode": "local",
        "template_id": template_id,
        "team_session_id": session_id,
        "runtime_team_name": runtime_team_name,
        "agent_service_id": "",
        "gateway_id": "",
        "created_at": now,
        "updated_at": now,
        "extra": json.dumps({"channel_id": channel_id}, ensure_ascii=False),
    }
    if not team_quota_enabled():
        get_team_control_store().create_entity(
            team_name=team_name,
            owner=identity,
            template_id=template_id,
            team_session_id=session_id,
            runtime_team_name=runtime_team_name,
            status=TEAM_STATUS_CREATING,
            team_id=record["team_id"],
            extra={"channel_id": channel_id},
        )
        return
    quota = get_team_quota_service()
    quota._reserve_mysql(
        kind=KIND_TEAM_CREATE,
        key=team_create_key(session_id),
        identity=identity,
        team_id="",
        member_name="",
        limit=0,
        default=0,
        lock="account",
        on_existing="skip",
        record_entity=record,
    )


def ensure_session_team_occupancy(
    request: AgentRequest,
    *,
    session_id: str,
    team_name: str,
    template_id: str,
) -> None:
    """Count a session-bound team against X/Z quota.

    ``team.create`` records the entity itself. ``team.session.bind`` is the
    existing enterprise entry, so it uses the same reservation.
    """
    from jiuwenswarm.agents.harness.team import TeamManager
    from jiuwenswarm.enterprise.team.quota import (
        TeamQuotaExceeded,
        get_team_quota_service,
    )

    identity = request_identity(request)
    if not identity.user_id:
        raise TeamQuotaExceeded(
            "user identity is required to create a team",
            kind="team_create",
            used=0,
            limit=0,
            default=0,
        )
    from jiuwenswarm.infrastructure.db.team_mysql_actor import use_team_mysql

    if use_team_mysql():
        _occupy_session_mysql(
            identity,
            session_id=session_id,
            team_name=team_name,
            template_id=template_id,
            channel_id=request.channel_id or "",
        )
        return
    control = get_team_control_store()
    existing = control.get_entity_by_session(session_id)
    if existing is not None and existing.status != TEAM_STATUS_DELETED:
        return
    quota = get_team_quota_service()
    lease = quota.reserve_team_create(identity, session_id)
    entity = None
    try:
        entity = control.create_entity(
            team_name=team_name,
            owner=identity,
            template_id=template_id,
            team_session_id=session_id,
            runtime_team_name=TeamManager.build_session_scoped_team_name(team_name, session_id),
            status=TEAM_STATUS_CREATING,
            extra={"channel_id": request.channel_id or ""},
        )
        quota.commit_lease(lease, team_id=entity.team_id)
    except Exception:
        if lease is not None:
            quota.release_lease(lease)
        if entity is not None:
            quota.release_team(entity.team_id)
            control.delete_entity(entity.team_id)
        raise


async def handle_team_quota_get(ctx: RequestContext) -> None:
    identity = request_identity(ctx.request)
    if not identity.user_id:
        await _fail(ctx, "user identity is required", "FORBIDDEN")
        return
    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

    from jiuwenswarm.infrastructure.db.team_mysql_actor import run_team_store

    await _reply(
        ctx,
        ok=True,
        payload=await run_team_store(get_team_quota_service().snapshot_user, identity),
    )


__all__ = [
    "handle_team_create",
    "handle_team_get",
    "handle_team_list",
    "handle_team_member_join",
    "handle_team_member_leave",
    "handle_team_members_list",
    "handle_team_quota_get",
    "handle_team_stop",
    "request_identity",
]
