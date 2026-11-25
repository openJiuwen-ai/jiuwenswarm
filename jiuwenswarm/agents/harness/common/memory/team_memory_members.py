# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm-managed enrollment for the shared document runtime."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


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
    from jiuwen_memory.api import Member, Scope, dimensions_of

    security = _admin_context(org, http=http)
    subjects = tuple(dict.fromkeys(agents))
    members = [Member(Scope(org=org, space=space, user=user, agent=name)) for name in subjects]
    for dimension in dimensions_of(
        Scope(org=org, space=space), {kind: key for kind, key in coordinates.items() if key},
    ):
        roster = members
        if dimension.kind == "project" and project_agent not in subjects:
            roster = [*members, Member(Scope(org=org, space=space, user=user, agent=project_agent))]
        await client.enroll_collective(dimension, roster, security)


async def _remove_collective_members(client, dimension, targets, admin):
    remover = getattr(client, "_remove_collective", None)
    if remover is None:
        raise AttributeError("Collective client does not expose _remove_collective")
    return await remover(dimension, targets, admin)


@asynccontextmanager
async def _membership_client(config=None, *, create=False, sdk_media_globals=None):
    """Open a host-only management lease through the configured transport."""
    from pathlib import Path

    from . import jiuwen_rail_builder as builder

    config = builder.load_memory_config() if config is None else config
    if not builder.is_jiuwen_builtin_memory(config):
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
        if client is None:
            yield None
            return
        yield client, _admin_context(org, http=http), (org, space, user)
    finally:
        await provider.shutdown()


async def _prepare_members(config, coordinates, agents, *, sdk_media_globals=None) -> bool:
    from . import jiuwen_rail_builder as builder

    project_agent = builder.get_jiuwen_memory_scope(config)["agent"]
    try:
        async with asyncio.timeout(10), _membership_client(
            config, create=True, sdk_media_globals=sdk_media_globals,
        ) as lease:
            if lease is None:
                return False
            client, _admin, (org, space, user) = lease
            if builder.get_jiuwen_builtin_mode(config) == "http" and not client.collective_document:
                return False
            await enroll_team_memory(
                client, org=org, space=space, user=user, agents=agents or (project_agent,),
                project_agent=project_agent, coordinates=coordinates,
                http=builder.get_jiuwen_builtin_mode(config) == "http",
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
    return await _prepare_members(config, {"project": project}, (), sdk_media_globals=sdk_media_globals)


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


async def revoke_expert_group_memory(group: str) -> None:
    """Revoke durable Team memberships after package uninstall."""
    from jiuwen_memory.api import Dimension
    from jiuwenswarm.common.team_memory_binding import team_memory_key

    if not group:
        return
    async with _membership_client() as session:
        if session is None:
            return
        client, admin, owner = session
        org, space, user = owner
        team = team_memory_key(org, space, user, group)
        dimension = Dimension(org, space, "team", team)
        targets = await client.active_collective_members(dimension, admin)
        if targets:
            await _remove_collective_members(client, dimension, targets, admin)


async def revoke_expert_template_memory(template: str) -> None:
    """Revoke memberships for a deleted standalone expert template."""
    from jiuwen_memory.api import Scope
    from jiuwenswarm.common.team_memory_binding import expert_memory_subject

    if not template:
        return
    async with _membership_client() as session:
        if session is None:
            return
        client, admin, owner = session
        org, space, user = owner
        target = Scope(org=org, space=space, user=user, agent=expert_memory_subject(None, template))
        await _remove_collective_members(client, None, [target], admin)



async def change_expert_membership(ctx, member_name: str, template_ref: dict, added: bool) -> None:
    """Mirror a deliberate roster change through the trusted management lease."""
    from jiuwen_memory.api import Member, Scope, dimensions_of
    from jiuwenswarm.common.team_memory_binding import resolve_expert_memory_subject

    agent = resolve_expert_memory_subject(ctx.memory_group, ctx.memory_experts, member_name, template_ref)
    if agent is None:
        return

    async with _membership_client(ctx.config, create=added) as session:
        if session is None:
            return
        client, admin, (org, space, user) = session
        scope = Scope(org=org, space=space, user=user, agent=agent)
        coordinates = {"team": ctx.memory_team}
        if ctx.memory_project:
            coordinates["project"] = ctx.memory_project
        for dimension in dimensions_of(scope, coordinates):
            if added:
                await client.attach_collective(dimension, [Member(scope)], admin)
            else:
                await _remove_collective_members(client, dimension, [scope], admin)
