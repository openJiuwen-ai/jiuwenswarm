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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenswarm.agents.swarm.agent_group import LoadedAgentGroupPackage

from openjiuwen.agent_teams.paths import (
    SKILL_VISIBILITY_FILENAME,
    configure_global_skills_dir,
    team_home,
)
from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template
from openjiuwen.agent_teams.schema.blueprint import TransportSpec
from openjiuwen.agent_teams.schema.team import (
    ExternalCliMemberSpec,
    TeamMemberSpec,
    TeamRole,
)
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec

from jiuwenswarm.agents.swarm.config_specs import (
    build_member_capability_specs,
    build_member_deep_agent_spec,
    build_reviewer_deep_agent_spec,
)
from jiuwenswarm.agents.swarm.context import (
    SwarmBuildContext,
    get_heartbeat_job_service,
)
from jiuwenswarm.agents.swarm.registry import register_swarm_providers
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import (
    build_enabled_mcp_server_configs,
    preflight_mcp_server_reachable,
)
from jiuwenswarm.common.mode_matrix import (
    NEW_TEAM_DESIGN,
    is_code_profile_mode,
    is_team_mode,
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
    artifacts (memory, Skill visibility metadata). When worktree isolation is
    on, ``AgentConfigurator`` overrides cwd again with the member worktree,
    which is why this is unconditional here.
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


def _apply_agent_group(
    spec: Any,
    agent_group_name: str,
    package: LoadedAgentGroupPackage | None = None,
) -> None:
    """Seed a hybrid roster from one resolved AgentGroup package."""
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_member_display_name,
    )
    if package is None:
        from jiuwenswarm.agents.swarm.agent_group import (
            load_agent_group_package_bundle,
        )
        from jiuwenswarm.server.runtime.extension_package_manager import (
            resolve_agent_group_dir,
        )

        package = load_agent_group_package_bundle(
            resolve_agent_group_dir(agent_group_name)
        )
    templates = package.templates
    package_dir = package.package_dir
    spec.metadata = {
        **(spec.metadata or {}),
        "agent_group_name": agent_group_name,
        "capabilities": list(package.capabilities),
    }

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
        external_config = external_cli_agent_spec_from_template(template)
        if external_config is None:
            spec.agents[agent_name] = _with_agent_template(
                teammate_base.model_copy(deep=True),
                template,
            )
            member = TeamMemberSpec(
                member_name=agent_name,
                display_name=resolve_agent_group_member_display_name(
                    package_dir,
                    agent_name,
                    fallback=template.agent_card.name or agent_name,
                ),
                desc=template.agent_card.description or "",
                prompt=member_prompt,
                role_type=TeamRole.TEAMMATE,
            )
        else:
            spec.agents[agent_name] = teammate_base.model_copy(deep=True)
            member = ExternalCliMemberSpec(
                member_name=agent_name,
                display_name=resolve_agent_group_member_display_name(
                    package_dir,
                    agent_name,
                    fallback=template.agent_card.name or agent_name,
                ),
                desc=template.agent_card.description or "",
                prompt=member_prompt,
                external_cli=external_config,
            )
        predefined_members.append(member)

    spec.predefined_members = predefined_members
    spec.team_mode = "hybrid"


def _is_design_team_mode(mode: str) -> bool:
    """Whether this enrich pass serves a team.design (designer canvas) team."""
    return str(mode or "") == NEW_TEAM_DESIGN



# Non-negotiable canvas rules for the design leader. Carried as the
# DeepAgentSpec ``system_prompt`` so TEAM_POLICY mounts them as the static
# ``team.extra`` system-prompt section (present on every model call); the full
# persona still travels the member_prompt dynamic lane. Mirrors the old
# server-side prompt assembly where these rules were force-fed every turn.
_DESIGN_LEADER_SYSTEM_PROMPT = (
    "你是设计画布（Design Canvas）的主编，与用户对话并编辑设计图。"
    "全新的创作需求用 designer_graph_autorun 自动建图，小改动用 designer_graph_patch；执行入口是 designer_run_graph 和画布运行按钮。"
    "\n\n画布硬规则（每次编辑必须遵守）：\n1. 全新创作需求（用户提出\"帮我做一个关于X的视频/广告/故事\"）必须调 designer_graph_autorun，"
    "把用户的完整需求原文传给 prompt 参数——导演链（分析→简报→审核→分镜→审核→建图→校验）在服务端完成，产出带完整锁体系的图。不要自己用 graph_patch 从零拼整图。"
    "\n2. designer_graph_patch 只用于用户要求的小改：加减个别节点、改 config.prompt、连线/断连、删节点。"
    "patch 字段是 {\"upsert_nodes\": [...], \"upsert_edges\": [{\"id\",\"source\",\"target\"}], \"remove_node_ids\": [], "
    "\"remove_edge_ids\": []}。\n3. 节点 type 只能是五种模态之一：text、table、image、video、"
    "audio——outline/character/storyboard/script/scene/clip/film 等叙事角色绝不是 type，一律 type=\"text\"。"
    "config.role 合法值：text、table、image、video、audio；"
    "config.pipeline 合法值：brief、character、scene、storyboard、clip、compose、music、speech。不要自创其他取值。"
    "\n4. 不要重建整张图，只 patch 用户要求改动的部分；保留既有节点 ID 与无关内容。"
    "\n5. designer_graph_autorun 建图成功后直接调用 designer_run_graph 开始执行（用户明确说先不要跑时除外）；graph_patch 小改后询问用户是否重跑（等用户确认或点运行按钮）。"
    "执行完成后如果用户反馈不满意，给出具体优化建议（哪个节点改什么提示词），询问是否按建议调整后重跑。"
    "\n6. 上传参考图节点（id 以 n_ref_ 开头，config 带 user_reference_path）是用户上传的素材，"
    "系统已创建：不要删除、不要重建、不要改它们的连线——创作节点（角色/场景/分镜）应引用这些参考（参考图是视觉权威，从中提取可兼容的可见特征）。"
    "\nuser_canvas_edits 是用户的画布操作日志（add/remove/connect/disconnect/replace）——把这些当作已发生的事实，不要恢复被删除的节点或被断开的边。"
    "\n详细创作规则见你的成员设定（team-context）。"
)


def _apply_design_agent_group(spec: Any, agent_group_name: str) -> None:
    """Assemble a team.design roster from one resolved AgentGroup package.

    Design teams (``team.design``) apply the leader template only: the member
    templates stay in the package as worker bases consumed per node by
    ``TeamWorkerBackend`` — they never enter the spawn roster — and the
    leader's collaboration toolset is stripped by
    ``DesignerSurfaceSanitizerRail`` (execution is service-driven; the leader
    has no run-control surface).
    """
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_dir,
    )
    from openjiuwen.agent_teams.schema.deep_agent_spec import ProgressiveToolSpec

    templates = load_agent_group_package(resolve_agent_group_dir(agent_group_name))
    leader_base = spec.agents.get("leader")
    if leader_base is None or spec.agents.get("teammate") is None:
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
    leader_spec = _with_agent_template(leader_base, leader_template)

    # Design-team leader: a conversation surface over the canvas.
    # Rebuild the leader spec instead of extending the config.yaml base:
    # the base member's tools/rails/mcps (fs/shell/org/browser/MCP …)
    # are dropped wholesale and replaced by the design profile
    # (`_build_design_capability_specs`: vision/audio + designer graph
    # tools + structured_ask_user). Execution is service-side
    # (RunCoordinator); the leader has no run-control or collaboration
    # surface at all.
    #
    # sys_operation stays ENABLED (user decision 2026-10-10): the first
    # tools injected by the base team path (list_files/glob/bash/…) expect
    # a live sys_operation, and the skill-use rail reads SKILL.md front
    # matter through it — disabling it left "tools present, engine None"
    # and every such call crashed with 'NoneType' object has no attribute
    # 'fs'/'shell'. Skills remain a planned leader capability, so the
    # filesystem engine must be there even though the design profile does
    # not advertise fs tools itself.
    #
    # progressive_tool is pinned disabled with the base surface: the design
    # profile is a small curated toolset with no long tail, and the
    # platform-wide switch (#8145) would defer every design tool behind
    # tool_search. Pinned rather than None so a harness-side default flip
    # cannot re-enable it.
    leader_spec = leader_spec.model_copy(
        update={
            "tools": [],
            "rails": [],
            "mcps": [],
            "subagents": [],
            "enable_skill_discovery": False,
            "progressive_tool": ProgressiveToolSpec(enabled=False),
        }
    )
    # Hard canvas rules ride the spec-level system prompt (TEAM_POLICY's
    # base_prompt lane): the leader's static system-prompt sections only
    # carry [team_bootstrap] + [team.extra(base_prompt)] (progressive
    # disclosure, F_76), and the AgentGroup persona travels the
    # member_prompt dynamic lane instead — too weak for schema-level
    # constraints (first real run: 24 graph_patch attempts, all rejected
    # for invented node types, persona rule never surfaced). Putting the
    # non-negotiables here makes them a static system-prompt section that
    # every model call sees.
    leader_spec = leader_spec.model_copy(
        update={"system_prompt": _DESIGN_LEADER_SYSTEM_PROMPT}
    )
    rails_specs, tool_specs = build_member_capability_specs(
        get_config(),
        NEW_TEAM_DESIGN,
        "leader",
        enable_permissions=spec.enable_permissions,
    )
    leader_spec = leader_spec.model_copy(
        update={
            "rails": rails_specs,
            "tools": tool_specs,
        }
    )
    spec.agents["leader"] = leader_spec
    # SDD-0030 expert table (2026-10-14): write each member's capability
    # snapshot into spec.agents (same shape as the regular team path's
    # _apply_agent_group) so the design bridge can derive the swarmflow
    # expert table (agents[m] ⋈ synthesized roster). The members still never
    # enter the spawn roster (predefined_members stays [] below), so nothing
    # is spawned — the entries are data for derive_design_expert_specs only.
    # Pin the platform-wide progressive_tool flag (#8145) off on the derivation
    # base: worker node tools are the member's whole surface, and deferring
    # them behind tool_search breaks the single-shot worker protocol. The
    # write-back also covers the plain-worker fallback base (no agent_template).
    teammate_base = spec.agents["teammate"].model_copy(
        update={"progressive_tool": ProgressiveToolSpec(enabled=False)}
    )
    spec.agents["teammate"] = teammate_base
    for agent_name, template in templates.items():
        if agent_name in ("leader", "teammate"):
            continue
        spec.agents[agent_name] = _with_agent_template(
            teammate_base.model_copy(deep=True),
            template,
        )
    spec.enable_swarmflow = False
    spec.predefined_members = []
    # Team-level swarmflow progressive switch (swarmflow_progressive_tool):
    # derive_swarmflow_expert_specs re-adds ProgressiveToolSpec to the expert
    # table when this is on, bypassing the strips above — force it off too.
    spec.progressive_tool = False


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
    agent_group_package: LoadedAgentGroupPackage | None = None,
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
        agent_group_package: Optional preloaded package for this AgentGroup.
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
    # A projectless team member (no project_dir) still needs a shared, stable
    # place for final deliverables. It lives under the team workspace's own
    # ``artifacts/`` tree so it is inside the team-workspace git repository
    # (auto-commit / history work) and so all members of one team share one
    # dated ``chat-<n>`` directory, distinguished by filename. Members bound to a
    # project keep deliverables in the project, so no task workspace is
    # allocated and the runtime prompt rail stays on its else branch (matching
    # single-agent behaviour). The per-member ``task_work_dir`` (the member's
    # own workspace) is resolved in the rail per member, not here.
    task_workspace_root: str | None = None
    team_outputs_dir: str | None = None
    if not str(project_dir or "").strip():
        try:
            from jiuwenswarm.common.team_artifacts import (
                get_team_artifact_workspace,
            )

            artifact_ws = get_team_artifact_workspace(
                team_ws_root,
                session_id=session_id,
            )
            task_workspace_root = str(artifact_ws.root_dir)
            team_outputs_dir = str(artifact_ws.outputs_dir)
        except OSError as exc:
            logger.warning(
                "[swarm.assembly] team artifact workspace allocation "
                "failed (team=%s, session=%s): %s — members will keep "
                "deliverables in their own workspace",
                spec.team_name,
                session_id,
                exc,
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
        disable_teammate_worktree=(
            str(channel_id or "").strip().lower() == "web"
            and not (is_team_mode(mode) and is_code_profile_mode(mode))
        ),
        team_id=spec.team_name,
        team_ws_root=team_ws_root,
        task_workspace_root=task_workspace_root,
        team_outputs_dir=team_outputs_dir,
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
    if "teammate" in spec.agents:
        spec.reviewer_agent = build_reviewer_deep_agent_spec(mode, spec.agents["teammate"])

    if agent_group_name:
        if _is_design_team_mode(mode):
            _apply_design_agent_group(spec, agent_group_name)
        else:
            _apply_agent_group(spec, agent_group_name, agent_group_package)

    spec.build_context = base
    # Carry a serializable seed alongside the live context so members rebuilt
    # across a serialization boundary (spawned teammate, distributed remote,
    # cold recovery) can reconstruct the context via the registered factory.
    spec.build_context_seed = base.to_seed()
    group_members = (request_metadata or {}).get("group_members")
    if group_members is not None:
        if agent_group_name:
            raise ValueError("a public group roster cannot also select a single AgentGroup")
        from jiuwenswarm.agents.swarm.group_chat import assemble_group_members, GROUP_MEMBER_TEMPLATE
        from openjiuwen.harness.schema.deep_agent_spec import RailSpec

        assemble_group_members(spec, group_members)
        # Future members use the shared role without rewriting a running spec.
        teammate = spec.agents["teammate"]
        teammate.rails = [*(teammate.rails or []), RailSpec(type=GROUP_MEMBER_TEMPLATE,
            params={"initial_names": [member["member_name"] for member in group_members]})]
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
