# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fresh memory Rail/Provider per Team responder; coordinates come from the entry."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from agent_gateway.common.mode_matrix import is_code_profile_mode
from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import _initial_sdk_media_globals
from jiuwenswarm.agents.harness.common.memory.plugin import (
    MEMORY_PLUGIN_ID,
    host_memory_context,
    team_memory_plugin_enabled,
)
from jiuwenswarm.agents.harness.common.rails.memory_forbidden_rail import MemoryForbiddenRail
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.team_memory_binding import (
    resolve_expert_memory_subject,
    uses_jiuwen_team_memory,
)
from jiuwenswarm.extensions.sdk.plugin_runtime import PluginRuntimeServices

logger = logging.getLogger(__name__)

TEAM_MEMORY = "swarm.team_memory"


def _team_identity(ctx: SwarmBuildContext) -> tuple[str, dict[str, str]]:
    member = ctx.member_name or ""
    subject = resolve_expert_memory_subject(
        ctx.memory_group, ctx.memory_experts, member, ctx.extras.get("team.agent_template_ref") or {},
        role=getattr(ctx, "role", ""),
    ) or "main_agent"
    return subject, {
        "team": ctx.memory_team,
        "member": member,
        "sidebar": "code" if is_code_profile_mode(ctx.mode) else "work",
    }


def _build_plugin_rail(ctx: SwarmBuildContext, agent: Any, enabled: Callable[[], bool]) -> Any:
    from openjiuwen.harness.manifest.meta_elements import _load_class_from_file
    from jiuwenswarm.server.runtime import extension_package_manager as packages

    package = packages.resolve_plugin_runtime_dir(MEMORY_PLUGIN_ID).resolve()
    manifest = packages.read_package_manifest(package) or {}
    entry, = manifest["rails"]
    path = (package / entry["file"]).resolve()
    if not path.is_relative_to(package):
        raise ValueError("Plugin Rail must stay inside the package")
    factory = _load_class_from_file(path, entry["class"], package_root=package)
    config = ctx.config or {}
    options = config.get("plugins", {}).get(MEMORY_PLUGIN_ID, {})
    host = host_memory_context(config, agent_name=ctx.member_name, project_dir=ctx.project_dir,
                               sdk_media_globals=_initial_sdk_media_globals)
    subject, binding = _team_identity(ctx)
    host = replace(host, team_project=ctx.memory_project, team_agent=subject,
                   team_enabled=enabled, team_binding=binding)
    runtime = PluginRuntimeServices(
        plugin_id=MEMORY_PLUGIN_ID, session_id=ctx.session_id, member_card_id=str(agent.card.id),
        profile="team", services={"memory_context": host}, host_config=config,
        package_config=packages.read_plugin_package_config(package), options=options,
    )
    runtime = runtime.with_options(factory.prepare_plugin_context(runtime))
    return factory.from_plugin_context(params={}, context=SimpleNamespace(extras={"plugin_runtime": runtime}))


class TeamMemoryPluginMount(DeepAgentRail):
    def __init__(self, context: SwarmBuildContext) -> None:
        super().__init__()
        self._context = context
        self._rail: Any = None
        self._lock = asyncio.Lock()

    def _enabled(self) -> bool:
        return team_memory_plugin_enabled(self._context.session_id, self._context.config or {})

    async def before_invoke(self, ctx: Any) -> None:
        async with self._lock:
            if not self._enabled():
                await self._close_rail()
                return
            try:
                if self._rail is None or self._rail.closed:
                    self._rail = _build_plugin_rail(self._context, ctx.agent, self._enabled)
                if ctx.agent not in self._rail.attached_agents:
                    await ctx.agent.register_rail(self._rail)
            except Exception as exc:
                logger.warning("[TeamMemory] plugin attachment failed: %s", type(exc).__name__)
                await self._close_rail()
                return
            rail = self._rail
        await rail.before_invoke(ctx)

    async def _close_rail(self) -> None:
        if self._rail is None:
            return
        try:
            await self._rail.aclose()
        except Exception as exc:
            logger.warning("[TeamMemory] plugin cleanup failed: %s", type(exc).__name__)
        else:
            self._rail = None


@harness_element(kind=ElementKind.RAIL, name=TEAM_MEMORY,
                 description="Jiuwen Team document memory with host-bound identity and coordinates.")
def build_team_memory(_params: dict[str, Any], ctx: SwarmBuildContext) -> Any:
    config = ctx.config or {}
    # Provider selection enables Team memory independently of legacy memory gates.
    if not uses_jiuwen_team_memory(config, ctx.mode):
        return None
    if not ctx.memory_team:
        logger.warning(
            "[TeamMemory] missing host-bound team coordinate; memory rail not created"
        )
        return None
    if ctx.extras.get("team.memory_members_ready") is False:
        return None
    rail = TeamMemoryPluginMount(ctx)
    # code_agent is part of this responder, not a separate Team member.
    ctx.extras["_coding_memory_rail"] = rail
    return [MemoryForbiddenRail(), rail]
