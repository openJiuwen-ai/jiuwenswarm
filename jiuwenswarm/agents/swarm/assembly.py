# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm team-spec enrichment entry point.

``enrich_team_spec_for_swarm`` is the single seam between the platform and the
provider-based assembly. Given a ``TeamAgentSpec`` it:

* registers all swarm providers / rail types (idempotent),
* builds the per-team base :class:`SwarmBuildContext` carrying the live runtime
  handles every provider needs,
* rewrites each present member spec ("leader" / "teammate") with its
  config-sourced rails and tools, and
* attaches the base context to ``spec.build_context`` so openjiuwen's
  ``setup_agent`` derives a per-member view through ``derive()``.

It never receives or inspects a pre-built ``DeepAgent``: members are assembled
purely from the config source plus provider name references.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import team_home
from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.harness.team.config_loader import _normalize_prompt_language
from jiuwenswarm.agents.swarm.registry import STREAM_EVENT, register_swarm_providers
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import build_enabled_mcp_server_configs
from jiuwenswarm.common.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)

# Member roles enriched in place, in deterministic order. Named predefined
# members (spec.agents keyed by member_name) are enriched too, after the role
# keys, so their capabilities come from the same config source — without this
# a predefined member builds from a raw spec with no skill rails/links and
# skill_tool reports "Skill not found" for its configured skills.
_MEMBER_ROLES: tuple[str, ...] = ("leader", "teammate")


def _collect_named_teammate_members(spec: Any) -> list[str]:
    """Return predefined LLM-teammate member names present in ``spec.agents``.

    Human-agent / bridge members are excluded: only role_type == "teammate"
    entries are assembled from the shared config source.
    """
    names: list[str] = []
    for member in getattr(spec, "predefined_members", None) or []:
        role_type = getattr(member, "role_type", None)
        if getattr(role_type, "value", role_type) != "teammate":
            continue
        name = str(getattr(member, "member_name", "") or "").strip()
        if not name or name not in spec.agents:
            continue
        if name not in names:
            names.append(name)
    return sorted(names)


def _mount_named_teammate_stream_events(spec: Any) -> list[str]:
    """Mount canonical UI stream events on named predefined LLM teammates."""
    mounted: list[str] = []
    for member in getattr(spec, "predefined_members", None) or []:
        role_type = getattr(member, "role_type", None)
        if getattr(role_type, "value", role_type) != "teammate":
            continue
        member_name = str(getattr(member, "member_name", "") or "").strip()
        if not member_name or member_name in _MEMBER_ROLES or member_name not in spec.agents:
            continue
        member_spec = spec.agents[member_name]
        rails = list(member_spec.rails or [])
        if not any(rail.type == STREAM_EVENT for rail in rails):
            rails.append(RailSpec(type=STREAM_EVENT))
            spec.agents[member_name] = member_spec.model_copy(update={"rails": rails})
        mounted.append(member_name)
    return mounted


def _with_project_cwd(member_spec: Any, project_dir: str | None) -> Any:
    """Point a member's cwd / project root at the request project directory.

    Only the working directory moves: the member keeps its own workspace for
    artifacts (memory, skills view, ``.team`` mount). When worktree isolation
    is on, ``AgentConfigurator`` overrides cwd again with the member worktree,
    which is why this is unconditional here.
    """
    project_root = str(project_dir or "").strip()
    if not project_root:
        return member_spec
    return member_spec.model_copy(update={"cwd": project_root, "project_root": project_root})


def enrich_team_spec_for_swarm(
    spec: Any,
    *,
    session_id: str,
    mode: str,
    project_dir: str | None = None,
    request_id: str | None = None,
    channel_id: str | None = None,
    request_metadata: dict[str, Any] | None = None,
    config_base: dict[str, Any] | None = None,
) -> None:
    """Enrich *spec* in place for provider-based swarm assembly.

    Registers swarm providers, builds the per-team base context, rewrites the
    present member specs with their config-sourced capabilities, and attaches the
    base context to the spec. Modifies *spec* in place and returns nothing.

    Args:
        spec: The ``TeamAgentSpec`` to enrich (mutated in place).
        session_id: Active session id.
        mode: Request mode (e.g. "team").
        project_dir: Resolved project directory, if any.
        request_id: Originating request id, if any.
        channel_id: Raw channel id from the request, if any.
        request_metadata: Request metadata mapping (carries ``mode`` etc.).
        config_base: Explicit effective config snapshot for the active tenant.
    """
    register_swarm_providers()

    config = config_base if config_base is not None else get_config()
    workspace = spec.workspace
    team_ws_root = (
        workspace.root_path
        if workspace and workspace.root_path
        else str(team_home(spec.team_name) / "team-workspace")
    )
    team_skills_dir = str(Path(team_ws_root) / "skills")
    global_skills_dir = str(get_agent_skills_dir())

    base = SwarmBuildContext(
        session_id=session_id,
        request_id=request_id,
        channel_id=channel_id,
        channel=channel_id or "default",
        request_metadata=request_metadata,
        mode=mode,
        project_dir=project_dir,
        disable_teammate_worktree=str(channel_id or "").strip().lower() == "web",
        team_id=spec.team_name,
        team_ws_root=team_ws_root,
        team_skills_dir=team_skills_dir,
        global_skills_dir=global_skills_dir,
        config=config,
        language=_normalize_prompt_language(
            getattr(spec, "language", None)
            or (config.get("preferred_language") if isinstance(config, dict) else None)
        ),
    )
    mcp_configs = build_enabled_mcp_server_configs(
        config,
        server_id_scope=f"team:{spec.team_name}",
    )

    for role in _MEMBER_ROLES:
        if role in spec.agents:
            member_spec = build_member_deep_agent_spec(
                config,
                mode,
                role,
                spec.agents[role],
                enable_permissions=spec.enable_permissions,
                mcp_configs=mcp_configs,
            )
            member_spec = _with_project_cwd(member_spec, project_dir)
            spec.agents[role] = member_spec

    named_members = _collect_named_teammate_members(spec)
    for member_name in named_members:
        member_spec = build_member_deep_agent_spec(
            config,
            mode,
            "teammate",
            spec.agents[member_name],
            enable_permissions=spec.enable_permissions,
            mcp_configs=mcp_configs,
        )
        member_spec = _with_project_cwd(member_spec, project_dir)
        spec.agents[member_name] = member_spec

    named_teammates = _mount_named_teammate_stream_events(spec)

    spec.build_context = base
    # Carry a serializable seed alongside the live context so members rebuilt
    # across a serialization boundary (spawned teammate, distributed remote,
    # cold recovery) can reconstruct the context via the registered factory.
    spec.build_context_seed = base.to_seed()
    logger.info(
        "[swarm.assembly] enriched team spec '%s' (roles=%s, named_members=%s, "
        "named_teammates=%s, session=%s, mcps=%d)",
        spec.team_name,
        [role for role in _MEMBER_ROLES if role in spec.agents],
        named_members,
        named_teammates,
        session_id,
        len(mcp_configs),
    )


__all__ = ["enrich_team_spec_for_swarm"]
