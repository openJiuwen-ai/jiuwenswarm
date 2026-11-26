# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm-managed enrollment for the shared document runtime."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def supports_team_memory_membership() -> bool:
    """Whether the installed memory package exposes the collective management API."""
    from jiuwen_memory import api

    return hasattr(api, "COLLECTIVE_MANAGER_ID")


def _admin_context(org, *, http=False):
    from jiuwen_memory.api import COLLECTIVE_MANAGER_ID, Scope, Surface, new_request_context
    from jiuwen_memory.common.security.types import AuthContext, Role

    if http:
        return COLLECTIVE_MANAGER_ID
    return new_request_context(
        AuthContext(
            actor=Scope(org=org, user=COLLECTIVE_MANAGER_ID), role=Role.ADMIN,
            credential_type="internal", auth_method="internal", authenticated_at=datetime.now(UTC),
        ),
        surface=Surface.INTERNAL,
    )


async def enroll_team_memory(client, *, org, space, user, agents, project_agent, coordinates, http=False):
    await _enroll_members(
        client, org=org, space=space, user=user, agents=agents, project_agent=project_agent,
        coordinates=coordinates, security=_admin_context(org, http=http),
    )


async def _enroll_members(client, *, org, space, user, agents, project_agent, coordinates, security):
    from jiuwen_memory.api import Member, Scope, dimensions_of

    subjects = tuple(dict.fromkeys(agents))
    members = [Member(Scope(org=org, space=space, user=user, agent=name)) for name in subjects]
    for dimension in dimensions_of(
        Scope(org=org, space=space), {kind: key for kind, key in coordinates.items() if key},
    ):
        roster = members
        if dimension.kind == "project" and project_agent not in subjects:
            roster = [*members, Member(Scope(org=org, space=space, user=user, agent=project_agent))]
        await client.enroll_collective(dimension, roster, security)


class _ManagedMembershipClient:
    def __init__(self, manager):
        self._manager = manager

    async def _call(self, method, *args, **kwargs):
        return await asyncio.to_thread(self._manager.call_collective_management, method, *args, **kwargs)

    async def enroll_collective(self, dimension, members, admin):
        return await self._call("init_collective_dimension", dimension, members, security=admin)

    async def attach_collective(self, dimension, members, admin):
        return await self._call("add_collective_members", dimension, members, security=admin)


@asynccontextmanager
async def _membership_client(config=None, *, create=False, sdk_media_globals=None, managed_only=True):
    """Open a host-only management lease through the configured transport."""
    from pathlib import Path

    from jiuwenswarm.server.runtime.managed_memory_http import managed_memory_http

    from . import jiuwen_rail_builder as builder

    config = builder.load_memory_config() if config is None else config
    if not builder.is_jiuwen_builtin_memory(config) or not supports_team_memory_membership():
        yield None
        return
    owner = managed_memory_http.collective_owner()
    if owner is not None:
        yield _ManagedMembershipClient(managed_memory_http), _admin_context(owner[0]), owner
        return
    if managed_only:
        yield None
        return
    http = builder.get_jiuwen_builtin_mode(config) == "http"
    org, space, user = builder.get_collective_memory_owner(config)
    options, root = builder.memory_connection_options(config, sdk_media_globals=sdk_media_globals)
    has_members_db = bool(root) and (Path(root) / "memory" / ".shadow" / "members.db").is_file()
    if not create and not http and not has_members_db:
        yield None
        return
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import AgentMemoryMemoryProvider

    scope = builder.get_jiuwen_memory_scope(config)
    provider = AgentMemoryMemoryProvider(
        **options, portal={"enabled": False}, user_id=user, agent_id=scope["agent"], space=space,
    )
    try:
        await provider.initialize(scope_id=org, user_id=user, agent_id=scope["agent"], session_id="")
        client = getattr(provider, "_client", None)
        if client is None or (http and not client.collective_document):
            yield None
            return
        yield client, _admin_context(org, http=http), (org, space, user)
    finally:
        await provider.shutdown()


async def _prepare_members(config, coordinates, agents, *, sdk_media_globals=None, managed_only=True) -> bool:
    from . import jiuwen_rail_builder as builder

    project_agent = builder.get_jiuwen_memory_scope(config)["agent"]
    try:
        async with asyncio.timeout(10), _membership_client(
            config, create=True, sdk_media_globals=sdk_media_globals, managed_only=managed_only,
        ) as lease:
            if lease is None:
                return False
            client, admin, (org, space, user) = lease
            await _enroll_members(
                client, org=org, space=space, user=user, agents=agents or (project_agent,),
                project_agent=project_agent, coordinates=coordinates,
                security=admin,
            )
        return True
    except Exception as exc:
        logger.warning("Memory membership preparation failed: %s", type(exc).__name__)
        return False


async def prepare_project_memory(config, project: str, *, sdk_media_globals=None) -> bool:
    """Register the ordinary responder's host-bound project during rail assembly."""
    from .jiuwen_rail_builder import get_jiuwen_memory_scope

    if get_jiuwen_memory_scope(config)["session"]:
        return False
    return await _prepare_members(
        config, {"project": project}, (), sdk_media_globals=sdk_media_globals, managed_only=False,
    )


async def prepare_team_memory(ctx, *, seed: dict | None = None) -> bool:
    """Prepare the roster and sync the readiness result to the member build seed."""
    from jiuwenswarm.common.team_memory_binding import expert_memory_subject, uses_jiuwen_team_memory

    if ctx is None or not getattr(ctx, "memory_team", None):
        return False
    config = ctx.config or {}
    if not uses_jiuwen_team_memory(config, ctx.mode):
        return False
    roster = tuple(expert_memory_subject(ctx.memory_group, name) for name in ctx.memory_experts)
    ready = await _prepare_members(
        config, {"project": ctx.memory_project, "team": ctx.memory_team},
        ("main_agent", *roster),
    )
    ctx.extras["team.memory_members_ready"] = ready
    if seed is not None:
        seed.update(ctx.to_seed())
    return ready


def apply_membership_revocation(api: Any, org: str, user: str, kind: str, subject: str,
                                team: str, project: str) -> None:
    """Apply a journaled revocation against the listener's own store."""
    from jiuwen_memory.api import Dimension, Scope, dimensions_of
    from jiuwenswarm.common.team_memory_binding import team_memory_key

    admin = _admin_context(org)
    if kind == "group":
        dimension = Dimension(org, "", "team", team_memory_key(org, "", user, subject))
        targets = [member.scope for member in api.list_collective_members(dimension, status="active", security=admin)]
        if targets:
            api.remove_collective_members(dimension, targets, security=admin)
        return

    target = Scope(org=org, user=user, agent=subject)
    if kind == "template":
        api.remove_collective_members(None, [target], security=admin)
    elif kind == "member":
        coordinates = {key: value for key, value in (("team", team), ("project", project)) if value}
        for dimension in dimensions_of(target, coordinates):
            api.remove_collective_members(dimension, [target], security=admin)


async def _revoke_membership(kind: str, subject: str, *, config: dict | None = None,
                             team: str = "", project: str = "") -> None:
    from jiuwenswarm.enterprise import is_enterprise
    from jiuwenswarm.server.runtime.managed_memory_http import managed_memory_http

    from . import jiuwen_rail_builder as builder

    if is_enterprise():
        return
    config = builder.load_memory_config() if config is None else config
    if not builder.is_jiuwen_builtin_memory(config):
        return
    await asyncio.to_thread(managed_memory_http.revoke_membership, kind, subject, team=team, project=project)


async def revoke_expert_group_memory(group: str) -> None:
    """Revoke durable Team memberships after package uninstall."""
    if group:
        await _revoke_membership("group", group)


async def revoke_expert_template_memory(template: str) -> None:
    """Revoke memberships for a deleted standalone expert template."""
    from jiuwenswarm.common.team_memory_binding import expert_memory_subject

    if template:
        await _revoke_membership("template", expert_memory_subject(None, template))



async def change_expert_membership(ctx, member_name: str, template_ref: dict, added: bool) -> None:
    """Mirror a deliberate roster change through the trusted management lease."""
    from jiuwenswarm.common.team_memory_binding import resolve_expert_memory_subject

    agent = resolve_expert_memory_subject(ctx.memory_group, ctx.memory_experts, member_name, template_ref)
    if agent is None:
        return

    if not added:
        await _revoke_membership("member", agent, config=ctx.config, team=ctx.memory_team, project=ctx.memory_project)
        return

    async with _membership_client(ctx.config, create=True) as session:
        if session is None:
            return
        from jiuwen_memory.api import Member, Scope, dimensions_of

        client, admin, (org, space, user) = session
        scope = Scope(org=org, space=space, user=user, agent=agent)
        coordinates = {"team": ctx.memory_team}
        if ctx.memory_project:
            coordinates["project"] = ctx.memory_project
        for dimension in dimensions_of(scope, coordinates):
            await client.attach_collective(dimension, [Member(scope)], admin)
