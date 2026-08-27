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
* optionally overlays an AgentGroup as Team-owned member prompts, serialized
  per-member capability templates, and a manifest-defined initial roster, and
* attaches the base context to ``spec.build_context`` so openjiuwen's
  ``setup_agent`` derives a per-member view through ``derive()``.

It never receives or inspects a pre-built ``DeepAgent``: members are assembled
purely from the config source plus provider name references.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import (
    SKILL_VISIBILITY_FILENAME,
    configure_global_skills_dir,
    team_home,
)
from openjiuwen.agent_teams.schema.blueprint import TransportSpec
from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.context import (
    SwarmBuildContext,
    get_heartbeat_job_service,
)
from jiuwenswarm.agents.swarm.registry import register_swarm_providers
from jiuwenswarm.agents.harness.observability_runtime import get_trajectory_span_processor
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import (
    build_enabled_mcp_server_configs,
    preflight_mcp_server_reachable,
)
from jiuwenswarm.common.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)

# Member roles enriched in place, in deterministic order.
_MEMBER_ROLES: tuple[str, ...] = ("leader", "teammate")
_PROMPT_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


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


def _template_skill_names(template: AgentTemplateSpec) -> list[str]:
    """Return stable, deduplicated skill directory names from a template."""
    names: list[str] = []
    seen: set[str] = set()
    for skill in template.skills:
        name = Path(skill.dir).name
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _select_prompt_content(content: dict[str, str], language: str) -> str:
    """Select one localized AgentTemplate prompt using core's fallback order."""
    if language in content:
        return content[language]
    if "en" in content:
        return content["en"]
    if "cn" in content:
        return content["cn"]
    if content:
        return next(iter(content.values()))
    return ""


def _render_prompt_text(text: str, params: dict[str, Any]) -> str:
    """Render the plain ``{{ name }}`` placeholders used by AgentTemplate."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(params[key]) if key in params else match.group(0)

    return _PROMPT_TEMPLATE_PATTERN.sub(_replace, text)


def _template_team_prompt(template: AgentTemplateSpec, member_spec: Any) -> str:
    """Flatten an AgentTemplate's prompt sections into the Team-owned prompt.

    ``TeamMemberSpec.prompt`` / ``LeaderSpec.prompt`` is the persisted source of
    truth for a member's private working agreement.  Preserve AgentTemplate
    priority and localization semantics while moving that content into the Team
    layer instead of letting ``NativeHarness`` replace prompt sections later.
    """
    language = str(getattr(member_spec, "language", None) or "cn").strip() or "cn"
    workspace = getattr(getattr(member_spec, "workspace", None), "root_path", None)
    base_params: dict[str, Any] = {
        "language": language,
        "workspace": workspace or "",
    }
    rendered: list[str] = []
    for section in sorted(template.prompt_sections, key=lambda item: item.priority):
        params = {**base_params, **dict(section.render_params or {})}
        text = _render_prompt_text(
            _select_prompt_content(section.content, language),
            params,
        ).strip()
        if text:
            rendered.append(text)
    return "\n\n".join(rendered)


def _merge_team_prompt(existing: str | None, expert_prompt: str) -> str:
    """Append an expert prompt without discarding an existing Team prompt."""
    return "\n\n".join(
        part for part in (str(existing or "").strip(), expert_prompt.strip()) if part
    )


def _with_agent_template(member_spec: Any, template: AgentTemplateSpec) -> Any:
    """Attach a capability-only template snapshot and seed Skill visibility."""
    skills: list[str] = []
    seen: set[str] = set()
    for name in [*(member_spec.skills or []), *_template_skill_names(template)]:
        if name and name not in seen:
            skills.append(name)
            seen.add(name)
    runtime_template = template.model_copy(update={"prompt_sections": []})
    return member_spec.model_copy(
        update={
            "agent_template_spec": runtime_template.model_dump(mode="json"),
            "skills": skills,
        }
    )


def _apply_agent_group(spec: Any, agent_group_name: str) -> None:
    """Seed a hybrid roster from one resolved AgentGroup package."""
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_dir,
    )

    package_dir = resolve_agent_group_dir(agent_group_name)
    templates = load_agent_group_package(package_dir)

    leader_base = spec.agents.get("leader")
    teammate_base = spec.agents.get("teammate")
    if leader_base is None or teammate_base is None:
        raise ValueError(
            "AgentGroup assembly requires both base 'leader' and 'teammate' specs"
        )
    leader_template = templates["leader"]
    leader_prompt = _template_team_prompt(leader_template, leader_base)
    spec.leader = spec.leader.model_copy(
        update={
            "prompt": _merge_team_prompt(spec.leader.prompt, leader_prompt),
        }
    )
    spec.agents["leader"] = _with_agent_template(
        leader_base,
        leader_template,
    )

    predefined_members: list[TeamMemberSpec] = []
    for agent_name, template in templates.items():
        if agent_name == "leader":
            continue
        member_prompt = _template_team_prompt(template, teammate_base)
        spec.agents[agent_name] = _with_agent_template(
            teammate_base.model_copy(deep=True),
            template,
        )
        predefined_members.append(
            TeamMemberSpec(
                member_name=agent_name,
                display_name=template.agent_card.name or agent_name,
                desc=template.agent_card.description or "",
                prompt=member_prompt,
                role_type=TeamRole.TEAMMATE,
            )
        )

    spec.predefined_members = predefined_members
    spec.team_mode = "hybrid"


def enrich_team_spec_for_swarm(
    spec: Any,
    *,
    session_id: str,
    mode: str,
    project_dir: str | None = None,
    trusted_dirs: list[str] | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
    channel_id: str | None = None,
    request_metadata: dict[str, Any] | None = None,
    agent_group_name: str | None = None,
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
        user_id: Authenticated request owner, if any.
        channel_id: Raw channel id from the request, if any.
        request_metadata: Request metadata mapping (carries ``mode`` etc.).
        agent_group_name: Optional AgentGroup package selected for this Team.
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
        user_id=user_id,
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
        heartbeat_job_service=get_heartbeat_job_service(),
        config=config,
    )
    mcp_configs = build_enabled_mcp_server_configs(
        config,
        server_id_scope=f"team:{spec.team_name}",
        resolve_credentials=True,
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

    if agent_group_name:
        _apply_agent_group(spec, agent_group_name)

    spec.build_context = base
    # Carry a serializable seed alongside the live context so members rebuilt
    # across a serialization boundary (spawned teammate, distributed remote,
    # cold recovery) can reconstruct the context via the registered factory.
    spec.build_context_seed = base.to_seed()
    logger.info(
        "[swarm.assembly] enriched team spec '%s' "
        "(roles=%s, session=%s, mcps=%d, agent_group=%s)",
        spec.team_name,
        [role for role in _MEMBER_ROLES if role in spec.agents],
        session_id,
        len(mcp_configs),
        agent_group_name,
    )


async def preflight_team_mcps(spec: Any) -> list[str]:
    """Probe each member's MCPs; drop unreachable ones and degrade state.

    Run after :func:`enrich_team_spec_for_swarm` so one bad MCP can't cancel
    the whole team via openjiuwen's fail-fast ``_register_pending_mcps`` raise.
    Probes each unique ``server_id`` once (leader and teammate typically share
    configs); unreachable MCPs are removed from every member's ``mcps`` list
    and degraded to ``state=disconnected`` in state.json. Returns the names of
    dropped MCPs. Never raises — a probe failure is a verdict, not an exception.
    """
    dropped: list[str] = []
    # Verdict cache keyed by server_id (or name fallback): True = keep,
    # False = drop. Leader and teammate typically share the same McpServerConfig
    # (same server_id), so probe once and apply the verdict to every role.
    verdict: dict[str, bool] = {}
    for role in _MEMBER_ROLES:
        member = spec.agents.get(role) if isinstance(spec.agents, dict) else None
        if member is None or not getattr(member, "mcps", None):
            continue
        kept: list[McpServerConfig] = []
        for cfg in member.mcps:
            name = str(getattr(cfg, "server_name", "") or "").strip()
            sid = str(getattr(cfg, "server_id", "") or "").strip()
            key = sid or name
            if key in verdict:
                # Already probed (shared config across roles) — reuse verdict.
                if verdict[key]:
                    kept.append(cfg)
                continue
            ok, reason = await preflight_mcp_server_reachable(cfg)
            if ok:
                verdict[key] = True
                kept.append(cfg)
                continue
            verdict[key] = False
            dropped.append(name)
            logger.warning(
                "[swarm.assembly] team MCP '%s' preflight failed, dropping "
                "from member '%s': %s",
                name, role, reason,
            )
            if name:
                try:
                    from jiuwenswarm.server.runtime.mcp.state_store import set_mcp_state
                    set_mcp_state(name, state="disconnected")
                except Exception as degr_exc:  # noqa: BLE001 — degrade is best-effort
                    logger.debug(
                        "[swarm.assembly] state degrade for '%s' failed: %s",
                        name, degr_exc,
                    )
        spec.agents[role] = member.model_copy(update={"mcps": kept})
    return dropped


__all__ = ["enrich_team_spec_for_swarm", "preflight_team_mcps"]
