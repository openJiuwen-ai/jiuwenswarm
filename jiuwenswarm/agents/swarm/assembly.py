# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm team-spec enrichment entry point.

``enrich_team_spec_for_swarm`` is the single seam between the platform and the
provider-based assembly. Given a ``TeamAgentSpec`` it:

* registers all swarm providers / rail types (idempotent),
* points openjiuwen's single Skill library at the platform-owned directory and
  resolves where the team's Skill visibility document lives (it never writes
  it: ``TeamWorkspaceManager.initialize`` is that document's only seeder),
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
import os
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import (
    SKILL_VISIBILITY_FILENAME,
    configure_global_skills_dir,
    team_home,
)
from openjiuwen.agent_teams.schema.blueprint import TransportSpec

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.registry import register_swarm_providers
from jiuwenswarm.agents.harness.observability_runtime import get_trajectory_span_processor
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import build_enabled_mcp_server_configs
from jiuwenswarm.common.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)

# Member roles enriched in place, in deterministic order.
_MEMBER_ROLES: tuple[str, ...] = ("leader", "teammate")


def _external_team_publish_url(channel_id: str | None) -> str:
    """Resolve the Gateway relay used by external CLI team members."""
    configured_url = os.getenv("TEAM_EVENT_GATEWAY_WS_URL", "").strip()
    if configured_url:
        return configured_url

    channel = str(channel_id or "web").strip().lower()
    if channel == "tui":
        port = os.getenv("GATEWAY_PORT", "19001")
        return f"ws://127.0.0.1:{port}/tui"

    port = os.getenv("WEB_PORT", "19000")
    path = os.getenv("WEB_PATH", "/ws").strip() or "/ws"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"ws://127.0.0.1:{port}{path}"


def _ensure_external_team_transport(spec: Any, channel_id: str | None) -> None:
    """Give cross-process CLI members a live event path back to Gateway."""
    if spec.external_transport is not None:
        return

    publish_url = _external_team_publish_url(channel_id)
    spec.external_transport = TransportSpec(
        type="hybrid",
        params={
            "team_name": spec.team_name,
            "external_publish_url": publish_url,
        },
    )
    logger.info(
        "[swarm.assembly] configured external team relay "
        "(team=%s, channel=%s, url=%s)",
        spec.team_name,
        channel_id or "web",
        publish_url,
    )


def _with_project_cwd(member_spec: Any, project_dir: str | None) -> Any:
    """Point a member's cwd / project root at the request project directory.

    Only the working directory moves: the member keeps its own workspace for
    artifacts (memory, Skill visibility metadata, ``.team`` mount). When
    worktree isolation is on, ``AgentConfigurator`` overrides cwd again with
    the member worktree, which is why this is unconditional here.
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
    trusted_dirs: list[str] | None = None,
    request_id: str | None = None,
    channel_id: str | None = None,
    request_metadata: dict[str, Any] | None = None,
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
        trusted_dirs: Directories the client declared as trusted for this request.
        request_id: Originating request id, if any.
        channel_id: Raw channel id from the request, if any.
        request_metadata: Request metadata mapping (carries ``mode`` etc.).
    """
    register_swarm_providers()
    _ensure_external_team_transport(spec, channel_id)

    # Point openjiuwen's single Skill library at the platform-owned directory
    # before any team / member / rail resolves it. Without this openjiuwen falls
    # back to ``~/.openjiuwen/workspace/skills`` and the two sides read
    # different libraries. The call is idempotent.
    skills_library = get_agent_skills_dir()
    configure_global_skills_dir(skills_library)

    config = get_config()
    workspace = spec.workspace
    team_ws_root = (
        workspace.root_path
        if workspace and workspace.root_path
        else str(team_home(spec.team_name) / "team-workspace")
    )
    # Derived from the actual team workspace root rather than from
    # ``paths.team_skill_visibility_path`` so a relocated team workspace keeps
    # its metadata next to the workspace it really uses. For the default layout
    # the two are the same path.
    #
    # Resolved only, never written: the team document has exactly one seeder,
    # ``TeamWorkspaceManager.initialize``, which runs when the team workspace
    # comes up. A missing document reads back as "no restriction", so a team
    # without a workspace needs no file here.
    team_visibility_path = str(Path(team_ws_root) / SKILL_VISIBILITY_FILENAME)
    global_skills_dir = str(skills_library)

    base = SwarmBuildContext(
        session_id=session_id,
        request_id=request_id,
        channel_id=channel_id,
        channel=channel_id or "default",
        request_metadata=request_metadata,
        mode=mode,
        project_dir=project_dir,
        trusted_dirs=trusted_dirs,
        disable_teammate_worktree=str(channel_id or "").strip().lower() == "web",
        team_id=spec.team_name,
        team_ws_root=team_ws_root,
        team_skill_visibility_path=team_visibility_path,
        global_skills_dir=global_skills_dir,
        trajectory_span_processor=get_trajectory_span_processor(),
        config=config,
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

    spec.build_context = base
    # Carry a serializable seed alongside the live context so members rebuilt
    # across a serialization boundary (spawned teammate, distributed remote,
    # cold recovery) can reconstruct the context via the registered factory.
    spec.build_context_seed = base.to_seed()
    logger.info(
        "[swarm.assembly] enriched team spec '%s' (roles=%s, session=%s, mcps=%d)",
        spec.team_name,
        [role for role in _MEMBER_ROLES if role in spec.agents],
        session_id,
        len(mcp_configs),
    )


__all__ = ["enrich_team_spec_for_swarm"]
