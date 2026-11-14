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

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.agent_teams.experts import LoadedAgentGroupPackage

from openjiuwen.agent_teams.paths import (
    SKILL_VISIBILITY_FILENAME,
    configure_global_skills_dir,
    team_home,
)
from openjiuwen.agent_teams.experts import compose_template_member
from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template
from openjiuwen.agent_teams.schema.blueprint import TransportSpec
from openjiuwen.agent_teams.schema.team import (
    ExternalCliMemberSpec,
    TeamMemberSpec,
    TeamRole,
)
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
from agent_gateway.common.mode_matrix import (
    NEW_TEAM_DESIGN,
    is_code_profile_mode,
    is_team_mode,
)

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
    artifacts (memory, Skill visibility metadata). When worktree isolation is
    on, ``AgentConfigurator`` overrides cwd again with the member worktree,
    which is why this is unconditional here.
    """
    project_root = str(project_dir or "").strip()
    if not project_root:
        return member_spec
    return member_spec.model_copy(update={"cwd": project_root, "project_root": project_root})


def _merge_team_prompt(existing: str | None, expert_prompt: str) -> str:
    """Append an expert prompt without discarding an existing Team prompt."""
    return "\n\n".join(
        part for part in (str(existing or "").strip(), expert_prompt.strip()) if part
    )


def _apply_agent_group(
    spec: Any,
    agent_group_name: str,
    package: LoadedAgentGroupPackage | None = None,
) -> None:
    """Seed the team from one resolved AgentGroup package.

    The leader always takes the group's leader template. Under scheduled
    dispatch the team starts with an empty roster: the group's other members
    are not assembled here, and the leader spawns the ones a task needs, which
    core loads from the installed package on demand (``spawn_expert``, inline
    ``create_task`` members, swarmflow ``agent_template``). Otherwise they
    become a hybrid predefined roster.
    """
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_member_display_name,
    )
    if package is None:
        from openjiuwen.agent_teams.experts import (
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
    # The metadata key stays for existing readers; ``agent_group_name`` is the
    # field core's spawn_expert reads to spawn more instances of a member.
    spec.metadata = {
        **(spec.metadata or {}),
        "agent_group_name": agent_group_name,
        "capabilities": list(package.capabilities),
    }
    spec.agent_group_name = agent_group_name

    leader_base = spec.agents.get("leader")
    teammate_base = spec.agents.get("teammate")
    if leader_base is None or teammate_base is None:
        raise ValueError(
            "AgentGroup assembly requires both base 'leader' and 'teammate' specs"
        )
    leader_member = compose_template_member(leader_base, templates["leader"])
    spec.leader = spec.leader.model_copy(
        update={
            "prompt": _merge_team_prompt(spec.leader.prompt, leader_member.persona),
        }
    )
    spec.agents["leader"] = leader_member.spec
    if spec.dispatch_mode == "scheduled":
        return

    predefined_members: list[TeamMemberSpec] = []
    for agent_name, template in templates.items():
        if agent_name == "leader":
            continue
        assembled = compose_template_member(teammate_base.model_copy(deep=True), template)
        member_prompt = assembled.persona
        external_config = external_cli_agent_spec_from_template(template)
        if external_config is None:
            spec.agents[agent_name] = assembled.spec
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
    "全新的创作需求用 designer_graph_autorun 自动建图，小改动用 designer_graph_patch；执行入口是 swarmflow 和画布运行按钮。"
    "\n\n画布硬规则（每次编辑必须遵守）：\n1. 全新创作需求（用户提出\"帮我做一个关于X的视频/广告/故事\"）必须调 designer_graph_autorun，"
    "把用户的完整需求原文传给 prompt 参数——导演链（分析→简报→审核→分镜→审核→建图→校验）在服务端完成，产出带完整锁体系的图。不要自己用 graph_patch 从零拼整图。"
    "designer_graph_autorun 返回 error 时，把错误原因转告用户并等用户决定，不要改用 graph_patch 手动拼图。"
    "\n2. designer_graph_patch 只用于用户要求的小改：加减个别节点、改 config.prompt、连线/断连、删节点。"
    "用户修改故事层面的事实（人物的性别/身份/外貌/服装、地点、时间、情节、出现在镜头里的道具或场景细节等，"
    "如\"把全片的女店员改成男店员\"\"把白色杯子改成红色杯子\"）时，"
    "先调 designer_story_edit（edit=用户的修改要求原话）同步修订大纲（Brief）、角色设定和分镜表——只 patch 下游节点改不到它们，"
    "镜头（clip）的视频提示词每次运行都按分镜表对应行重新生成；"
    "再用 graph_patch 改仍写着旧事实的角色/场景/镜头节点 config.prompt（改过的镜头提示词按用户编辑对待，运行时不会被分镜覆盖）；"
    "最后从大纲节点起重跑：swarmflow(node_id=大纲节点 id, cascade=true)（用户明确说先不要跑时除外）。"
    "patch 字段是 {\"upsert_nodes\": [...], \"upsert_edges\": [{\"id\",\"source\",\"target\"}], \"remove_node_ids\": [], "
    "\"remove_edge_ids\": []}。\n3. 节点 type 只能是五种模态之一：text、table、image、video、"
    "audio——outline/character/storyboard/script/scene/clip/film 等叙事角色绝不是 type，一律 type=\"text\"。"
    "config.role 合法值：text、table、image、video、audio；"
    "config.pipeline 合法值：brief、character、scene、storyboard、clip、compose、music、speech。不要自创其他取值。"
    "\n4. 不要重建整张图，只 patch 用户要求改动的部分；保留既有节点 ID 与无关内容。"
    "插入、删除或调换镜头时，镜头节点 ID 照旧保留，但要把所有镜头节点的 config.clip_index 按新顺序重排为 1..N"
    "（每个镜头按这个序号读取分镜表的对应行，成片也按它排序），并把上一镜→下一镜的连线改成新顺序；"
    "用户给某一镜指定了时长时，写到该镜节点的 config.duration_sec。graph_patch 返回 clip_order_warning 时按提示再 patch 一次。"
    "\n5. designer_graph_autorun 建图成功后直接调用 swarmflow 开始执行（用户明确说先不要跑时除外）；graph_patch 小改后询问用户是否重跑（等用户确认或点运行按钮）。"
    "执行完成后如果用户反馈不满意，给出具体优化建议（哪个节点改什么提示词），询问是否按建议调整后重跑。"
    "\n6. 上传参考节点（id 以 n_ref_ 开头，config 带 user_reference_path）是用户上传的素材，"
    "系统已创建并连好线：不要删除、不要重建、不要改它们的连线。只有图片参考可以被角色/场景节点引用"
    "（参考图是视觉权威，从中提取可兼容的可见特征）；音频和视频参考只连创意简报（brief），不要把它们连到分镜或其他节点。"
    "\nuser_canvas_edits 是用户的画布操作日志（add/remove/connect/disconnect/replace）——把这些当作已发生的事实，不要恢复被删除的节点或被断开的边。"
    "\n7. 背景音乐（BGM）：整部片最多一个 BGM 节点，即 type=\"audio\"、config.pipeline=\"music\" 的节点（规划生成的 id 是 n_music）。"
    "用户用自然语言要求加 BGM / 配乐 / 背景音乐时：已有 BGM 节点就只改它；没有就 upsert 一个"
    "{\"id\":\"n_music\",\"type\":\"audio\",\"label\":\"BGM\",\"config\":"
    "{\"role\":\"audio\",\"pipeline\":\"music\",\"inputs\":[\"n_brief\"]}}，"
    "加 n_brief→n_music 和 n_music→成片节点（config.pipeline=compose）两条边，并把 n_music 加进成片节点的 config.inputs。"
    "用户对音乐的描述（风格、情绪、乐器、节奏、调性）原样写进 BGM 节点的 config.prompt，不要写成片时长或镜头时间点，工具会按剪辑点自动对齐。"
    "BGM 以创意简报为先：用户要求生成或重新生成 BGM 时，先用 designer_graph_get 看 BGM 的上游（n_brief）是否已有产出；"
    "还没有产出就只 patch 不运行，告诉用户 BGM 要等创意简报生成后才能生成，请先运行画布等简报生成完再来生成 BGM；"
    "designer_node_run 返回 upstream not ready 也按这条如实告诉用户，不要说已启动。"
    "上游已有产出时，patch 完直接 designer_node_run 这个 BGM 节点（只跑它，不必再问）；"
    "BGM 要混进成片还需重跑成片节点，跑完 BGM 后询问用户是否重跑成片。"
    "节点结果是静音占位（placeholder / no music API）说明音频生成没开，提示用户在 设置→实验功能→设计模式 打开「音频生成」并配置模型。"
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
    from openjiuwen.agent_teams.experts import load_agent_group_package
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
    leader_member = compose_template_member(leader_base, templates["leader"])
    spec.leader = spec.leader.model_copy(
        update={
            "prompt": _merge_team_prompt(spec.leader.prompt, leader_member.persona),
        }
    )
    leader_spec = leader_member.spec

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
        spec.agents[agent_name] = compose_template_member(teammate_base.model_copy(deep=True), template).spec
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
        from jiuwenswarm.agents.swarm.group_execution import install_execution_policy

        install_execution_policy(teammate)
        install_execution_policy(spec.agents["leader"], include_tools=False)
        teammate.rails = [*(teammate.rails or []), RailSpec(type=GROUP_MEMBER_TEMPLATE,
            params={"initial_names": [member["member_name"] for member in group_members]})]
    # P-B entry-level sandbox injection (design D-20261105-1041 KD1/KD2, §4.0).
    # Runs after every named entry exists (role bases, AgentGroup roster, group
    # members) so the whole spec.agents dict is covered by one pass.
    _build_member_sandbox_specs(config, spec, session_id=session_id, project_dir=project_dir)
    logger.info(
        "[swarm.assembly] enriched team spec '%s' "
        "(roles=%s, session=%s, mcps=%d, agent_group=%s)",
        spec.team_name,
        [role for role in _MEMBER_ROLES if role in spec.agents],
        session_id,
        len(mcp_configs),
        agent_group_name,
    )


# Teams already covered by the one-time enterprise known-boundary note (KD7).
# Per-process (deliberate): the leader and a remote teammate are separate
# processes, so each logs the note once on its own side; sharing state across
# processes is out of scope for an info-level note.
_ENTERPRISE_BOUNDARY_NOTED: set[str] = set()


def _entry_slug(value: str) -> str:
    """Sanitize one name fragment for use inside a sandbox id / isolation key.

    jiuwenbox validates custom sandbox ids against ``^[0-9a-z_-]+$``
    (models/sandbox.py), so the slug is lowercased and every stray character
    (notably ``#`` from binder directory names) degrades to ``-``.

    Anti-collision (key/id bijection, KD2) — two distinct raw names must never
    map to the same slug. Lossy folds keep a 12-hex digest suffix derived
    from the raw name:
      - oversize: names longer than 80 chars use ``slug[:67]-<digest12>``
        so truncation cannot merge two names;
      - degenerate fold: names whose characters all fold away (pure CJK,
        pure punctuation, ...) would ALL fall back to the same constant and
        share one isolation key — Chinese member names are common in this
        user base — so they get ``entry-<digest12>`` instead;
      - partial fold: a name that keeps some characters but lost others
        (e.g. ``a#1`` vs ``a-1`` fold identically) also keeps the digest.
      A lossless fold (safe charset, case-folded only) needs no digest.
    """
    raw = str(value or "")
    slug = re.sub(r"[^0-9a-zA-Z_-]", "-", raw).lower().strip("-_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else ""
    if not slug:
        # Everything folded away (e.g. a pure-CJK name): distinct raw names
        # stay distinct via the digest; the constant prefix keeps ids readable.
        return f"entry-{digest}" if digest else "entry"
    if len(slug) <= 80 and slug == raw.lower():
        return slug  # lossless fold: no information lost, no digest needed
    if len(slug) <= 80:
        return f"{slug[:67]}-{digest}"  # partial fold: digest guards merges
    return f"{slug[:67]}-{digest}"  # oversize truncation: digest guards merges


def _entry_sandbox_slug(team_name: str, entry_name: str) -> str:
    """Deterministic isolation-key suffix for one spec entry (P-B granularity)."""
    return f"entry_{_entry_slug(team_name)}_{_entry_slug(entry_name)}"


def _build_member_sandbox_specs(
    config: Any, spec: Any, *, session_id: str, project_dir: str | None = None
) -> None:
    """Give every ``spec.agents`` entry its own sandbox SysOperationSpec (P-B).

    Injection condition matrix (design D-20261105-1041 KD7): all branches that
    leave members on LOCAL are loud except ``sandbox.enabled=false``, which is
    the expected configuration and stays silent. Each entry gets a distinct
    deterministic id (``sandbox.entry_<team>_<entry>``) so the openjiuwen
    registry's one-key-one-id invariant holds without any workaround; runtime
    derived members (spawn_expert / reviewer / swarmflow worker) inherit their
    base entry's sysop through openjiuwen's own ``model_copy`` paths.

    Args:
        config: The resolved ``config.yaml`` mapping (unused here; kept for
            symmetry with the other assembly helpers and future knobs).
        spec: The ``TeamAgentSpec`` being enriched (mutated in place).
        session_id: Active session id, threaded into the team mount context.
        project_dir: The request's project directory, mounted rw into every
            member container (KD3). Without it the policy builder falls back
            to the agent-server cwd, which is not what team members work on.
    """
    from jiuwenswarm.common.config import get_sandbox_endpoint, get_sandbox_runtime
    from jiuwenswarm.enterprise.edition import is_enterprise
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec

    from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
        _team_context_from_spec,
        create_sandbox_sysop_card,
    )

    runtime = get_sandbox_runtime()
    if not runtime.get("enabled"):
        return  # expected configuration: silent
    if is_enterprise():
        # Known boundary (design §3 non-goal): enterprise team members stay
        # LOCAL until multi-tenant isolation-key semantics exist. Info-level,
        # logged once per team (KD7) so the note is auditable without
        # repeating on every enrich.
        if spec.team_name not in _ENTERPRISE_BOUNDARY_NOTED:
            _ENTERPRISE_BOUNDARY_NOTED.add(spec.team_name)
            logger.info(
                "[swarm.assembly] team members stay LOCAL: enterprise sandbox "
                "isolation is a known boundary (team=%s)",
                spec.team_name,
            )
        return

    endpoint = get_sandbox_endpoint()
    sandbox_url = str(endpoint.get("url") or "").strip()
    sandbox_type = str(endpoint.get("type") or "").strip().lower()
    if not sandbox_url or not sandbox_type:
        logger.warning(
            "[swarm.assembly] team members stay LOCAL: sandbox endpoint "
            "unresolved (type=%s, url=%s) (team=%s)",
            sandbox_type,
            sandbox_url,
            spec.team_name,
        )
        return
    if sandbox_type != "jiuwenbox":
        logger.warning(
            "[swarm.assembly] team members stay LOCAL: sandbox type %s "
            "not supported for team members yet (team=%s)",
            sandbox_type,
            spec.team_name,
        )
        return

    team_ctx = _team_context_from_spec(spec)
    if team_ctx is None:
        # An unresolvable team context must NOT cancel sandboxing: dropping
        # members back to the host is strictly worse than sandboxing them
        # without the 4 team mounts (project_dir / agent workspace / files.*
        # still mount). Loud, but injection continues with team mounts off.
        logger.warning(
            "[swarm.assembly] team mounts skipped: team context "
            "unresolvable, members stay sandboxed without team mounts "
            "(team=%s)",
            spec.team_name,
        )
        team_ctx = None  # explicit: intent(None) == [] (no team mounts)
    else:
        team_ctx["session_id"] = str(session_id or "")

    injected: list[str] = []
    failed_entries: list[str] = []
    for entry_name, agent_spec in list(spec.agents.items()):
        entry_slug = _entry_sandbox_slug(spec.team_name, entry_name)
        card = create_sandbox_sysop_card(
            sandbox_url,
            "jiuwenbox",
            files_runtime=runtime.get("files"),
            excluded_commands=runtime.get("excluded_commands"),
            idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
            idle_check_interval=runtime.get("idle_check_interval"),
            fallback_on_failure=bool(runtime.get("fallback_on_failure", False)),
            project_dir=project_dir,
            isolation_custom_id=entry_slug,
            team_context=team_ctx,
        )
        if card is None:
            failed_entries.append(entry_name)
            continue
        spec.agents[entry_name] = agent_spec.model_copy(
            update={
                "sys_operation": SysOperationSpec(
                    id=f"sandbox.{entry_slug}",
                    mode=OperationMode.SANDBOX,
                    gateway_config=card.gateway_config,
                ),
            }
        )
        injected.append(entry_name)

    # The reviewer spec is built before injection from the teammate entry, so
    # it does not carry the sysop automatically: hand it the teammate's spec
    # object so temp reviewers share the teammate container (read-only toolset,
    # design KD2). Untouched when the teammate entry itself failed to inject.
    teammate_sysop = (spec.agents.get("teammate").sys_operation
                      if spec.agents.get("teammate") is not None else None)
    if teammate_sysop is not None and spec.reviewer_agent is not None:
        spec.reviewer_agent = spec.reviewer_agent.model_copy(
            update={"sys_operation": teammate_sysop}
        )

    logger.info(
        "[swarm.assembly] team entries share sandbox config: entries=%d "
        "injected=%s (team=%s)",
        len(injected),
        injected,
        spec.team_name,
    )
    if failed_entries:
        # KD7: partial injection is loud at the assembly layer too — the
        # builder's warning names the cause, this names WHICH entries stay
        # LOCAL so partial-injection is auditable from one log line.
        logger.warning(
            "[swarm.assembly] team members stay LOCAL: entry card build "
            "failed for %s (team=%s)",
            failed_entries,
            spec.team_name,
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
            if key and key in verdict:
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

    # AgentGroup 展开出来的其他成员同样带着团队 MCP；只裁剪 leader/teammate
    # 两个键会漏掉它们，坏 MCP 仍可能让整个团队启动失败。
    for member_name, member in list(spec.agents.items()):
        if member_name in _MEMBER_ROLES or not getattr(member, "mcps", None):
            continue
        kept_other = []
        for cfg in member.mcps:
            sid = str(getattr(cfg, "server_id", "") or "").strip()
            nm = str(getattr(cfg, "server_name", "") or "").strip()
            if verdict.get(sid or nm, True):
                kept_other.append(cfg)
        spec.agents[member_name] = member.model_copy(
            update={"mcps": kept_other}
        )
    return dropped


# The v1 prototype ``assign_shared_sandbox_sys_operation`` (reusing the main
# agent's single sysop id for every member) was removed per design
# D-20261105-1041 §6: it overwrote the P-B per-entry injection with one shared
# id, silently restoring the superseded shared-container behavior on the
# primary team path. Per-entry injection now lives in
# ``_build_member_sandbox_specs`` at enrich time.


__all__ = [
    "enrich_team_spec_for_swarm",
    "preflight_team_mcps",
]
