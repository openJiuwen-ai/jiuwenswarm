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
import re
from pathlib import Path
from typing import Any

from openjiuwen.agent_evolving.trajectory import InMemoryTrajectoryRegistry
from openjiuwen.agent_teams.paths import team_home
from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.registry import (
    register_swarm_providers,
    TEAM_MEMBER_IDENTITY,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.mcp_config import build_enabled_mcp_server_configs
from jiuwenswarm.common.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)

# Member roles enriched in place, in deterministic order.
_MEMBER_ROLES: tuple[str, ...] = ("leader", "teammate")

_PROMPT_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


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


# ---------------------------------------------------------------------------
# 专家团（AgentGroup）组装：包驱动 → 编程式 TeamAgentSpec
# ---------------------------------------------------------------------------


def _check_agent_template_spec_support() -> None:
    """能力探针：openjiuwen 必须含快照导入能力。

    缺支持时专家团会半残（prompt 合并生效但包内 skills 不绑定）且无报错，
    这里显式失败，把部署契约问题暴露在 spec 构建时。
    """
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec

    if not hasattr(DeepAgent, "load_agent_template_spec") or (
        "agent_template_spec" not in DeepAgentSpec.model_fields
    ):
        raise RuntimeError(
            "当前 openjiuwen 缺少 AgentTemplate 快照导入能力"
            "（load_agent_template_spec / DeepAgentSpec.agent_template_spec）"
        )


def _template_skill_names(template: AgentTemplateSpec) -> list[str]:
    """模板技能目录名（去重保序），用于 spec.skills 可见性种子。"""
    names: list[str] = []
    seen: set[str] = set()
    for skill in template.skills:
        name = Path(skill.dir).name
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _select_prompt_content(content: dict[str, str], language: str) -> str:
    """按 core 回退序选一种语言的 prompt 文本。"""
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
    """渲染 AgentTemplate 的 ``{{ name }}`` 占位符（未知占位符原样保留）。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(params[key]) if key in params else match.group(0)

    return _PROMPT_TEMPLATE_PATTERN.sub(_replace, text)


def _template_team_prompt(template: AgentTemplateSpec, member_spec: Any) -> str:
    """把模板的 prompt sections 按 priority 拍平成 Team 层 prompt 文本。

    Team 层 prompt（``LeaderSpec.prompt`` / ``TeamMemberSpec.prompt``）是成员
    私有工作约定的持久化真相源（team_member.prompt 冷恢复通道）；persona 内容
    挪进 Team 层后，快照里必须清空 prompt_sections，避免 NativeHarness 热挂载时
    以 replace_existing=True 双写 identity section。
    """
    language = str(getattr(member_spec, "language", None) or "cn").strip() or "cn"
    workspace = getattr(getattr(member_spec, "workspace", None), "root_path", None)
    base_params: dict[str, Any] = {"language": language, "workspace": workspace or ""}
    rendered: list[str] = []
    for section in sorted(template.prompt_sections, key=lambda item: item.priority):
        params = {**base_params, **dict(section.render_params or {})}
        text = _render_prompt_text(
            _select_prompt_content(section.content, language), params
        ).strip()
        if text:
            rendered.append(text)
    return "\n\n".join(rendered)


def _merge_team_prompt(existing: str | None, expert_prompt: str) -> str:
    """追加专家 prompt，不丢弃模板原有 Team prompt。"""
    return "\n\n".join(
        part for part in (str(existing or "").strip(), expert_prompt.strip()) if part
    )


def _with_agent_template(member_spec: Any, template: AgentTemplateSpec) -> Any:
    """挂载"能力-only"快照（剥 prompt_sections）+ 技能可见性种子。"""
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


def _render_member_identity_text(
    template: AgentTemplateSpec,
    *,
    role: str,
    group_display: str,
    member_spec: Any,
) -> dict[str, str]:
    """渲染 member 身份块({cn, en} 双份),供 TEAM_MEMBER_IDENTITY rail params。

    内容:团名 + 主理人/成员称谓 + display_name + agent_card.description +
    模板 identity/persona 类 section 段落(双语选取 + 占位符渲染,与
    ``_template_team_prompt`` 同规)。Team 层 prompt 仍是冷恢复真相源;本块是独立 section 的身份锚点。
    团名与名字皆空时返回空 dict(provider 不挂载,行为零变化)。
    """
    display_name = str(template.agent_card.name or "").strip()
    if not display_name and not group_display:
        return {}
    persona: dict[str, str] = {}
    language = str(getattr(member_spec, "language", None) or "cn").strip() or "cn"
    workspace = getattr(getattr(member_spec, "workspace", None), "root_path", None)
    base_params: dict[str, Any] = {"language": language, "workspace": workspace or ""}
    for section in sorted(template.prompt_sections, key=lambda item: item.priority):
        name = str(section.name or "").lower()
        if "identity" not in name and "persona" not in name:
            continue
        params = {**base_params, **dict(section.render_params or {})}
        for lang in ("cn", "en"):
            if lang in persona:
                continue
            text = _render_prompt_text(
                _select_prompt_content(section.content, lang), params
            ).strip()
            if text:
                persona[lang] = text
    description = str(template.agent_card.description or "").strip()

    def _block(lang: str) -> str:
        if lang == "cn":
            title = "主理人" if role == "leader" else "成员"
            lines = ["# 你的身份", f"你是专家团「{group_display}」的{title} {display_name}。"]
        else:
            title = "leader" if role == "leader" else "member"
            lines = [
                "# Your identity",
                f'You are {display_name}, the {title} of the expert team "{group_display}".',
            ]
        if description:
            lines.append(description)
        if persona.get(lang):
            lines.append(persona[lang])
        return "\n".join(lines)

    return {"cn": _block("cn"), "en": _block("en")}


def _with_member_identity(
    member_spec: Any,
    *,
    role: str,
    display_name: str,
    group_display: str,
    identity_text: dict[str, str],
) -> Any:
    """把渲染好的身份块填进 member spec 的 TEAM_MEMBER_IDENTITY rail params。

    params 随 TeamAgentSpec 序列化(spawn/冷恢复/分布式重建不丢);非专家团
    不调用本函数,params 恒空,provider 返回 None 不挂载。
    """
    if not identity_text:
        return member_spec
    rails = list(getattr(member_spec, "rails", None) or [])
    for index, rail in enumerate(rails):
        if getattr(rail, "type", None) != TEAM_MEMBER_IDENTITY:
            continue
        params = dict(getattr(rail, "params", None) or {})
        params.update(
            {
                "role": role,
                "display_name": display_name,
                "group_display": group_display,
                "identity_text": identity_text,
            }
        )
        rails[index] = rail.model_copy(update={"params": params})
        return member_spec.model_copy(update={"rails": rails})
    return member_spec


def _apply_agent_group(spec: Any, agent_group_name: str, package_dir: Path | None = None) -> None:
    """把"本会话绑定的专家团包"覆写到 enrich 后的 TeamAgentSpec（组装七步）。

    1. 能力探针；
    2. 取 leader/teammate 基础 spec（缺一终止）；
    3. 定位包目录并严格加载：调用方预取的 ``package_dir`` 优先，缺省查
       expert_store 缓存——本函数是同步装配点不能自持 await，fetch 兜底
       （缓存优先契约见 ``get_cached_expert_package_dir``）已上移到
       async 调用方 ``TeamManager.get_swarm_enriched_team_spec``；
    4. leader prompt 合并（AGENT.md+persona+instruction 拍平后追加到模板原 prompt）；
    5. leader 快照；
    6. 成员覆写 + predefined_members 替换（TeamMemberSpec.prompt = persona+instruction，不含 AGENT.md）；
    7. 显式写 team_mode/dispatch_mode/enable_task_verification（不依赖版本默认值）。
    """
    _check_agent_template_spec_support()

    from jiuwenswarm.server.runtime.expert.agent_group import (
        load_agent_group_package,
        read_group_display,
    )
    from jiuwenswarm.server.runtime.expert.expert_store import (
        get_cached_expert_package_dir,
    )

    if package_dir is None:
        package_dir = get_cached_expert_package_dir(agent_group_name)
    if package_dir is None:
        raise FileNotFoundError(
            f"专家团包缓存缺失: {agent_group_name}（请重新 expert.load；"
            "本地调试模式 JIUWEN_EXPERT_LOCAL_DIRS 下本地源不落缓存，"
            "须由装配调用方预取）"
        )
    templates = load_agent_group_package(package_dir)

    leader_base = spec.agents.get("leader")
    teammate_base = spec.agents.get("teammate")
    if leader_base is None or teammate_base is None:
        raise ValueError("专家团组装要求基础 spec 同时含 'leader' 与 'teammate'")

    leader_template = templates["leader"]
    leader_prompt = _template_team_prompt(leader_template, leader_base)
    group_display = str(read_group_display(package_dir)["name"] or "").strip()
    # 团队版 switch notice（与单专家 expert.switch_notice 体验对齐）：
    # 绑定期间随 spec 构建写入 leader prompt 头部；退队后不再构建，自然摘除。
    group_notice = (
        f"当前会话由专家团「{group_display}」协作，"
        "你是该团的主理人。"
    )
    spec.leader = spec.leader.model_copy(
        update={
            "prompt": _merge_team_prompt(
                spec.leader.prompt, f"{group_notice}\n\n{leader_prompt}"
            ),
            # 运行时显示名跟随包的主理人花名（member_name 恒为 team-leader 不动——
            # 它是运行时身份键；display_name 纯展示）
            "display_name": leader_template.agent_card.name or spec.leader.display_name,
        }
    )
    leader_display = str(leader_template.agent_card.name or "").strip()
    leader_spec = _with_agent_template(leader_base, leader_template)
    leader_spec = _with_member_identity(
        leader_spec,
        role="leader",
        display_name=leader_display,
        group_display=group_display,
        identity_text=_render_member_identity_text(
            leader_template,
            role="leader",
            group_display=group_display,
            member_spec=leader_spec,
        ),
    )
    spec.agents["leader"] = leader_spec

    predefined_members: list[TeamMemberSpec] = []
    for agent_name, template in templates.items():
        if agent_name == "leader":
            continue
        member_prompt = _template_team_prompt(template, teammate_base)
        member_display = str(template.agent_card.name or agent_name).strip()
        member_spec = _with_agent_template(
            teammate_base.model_copy(deep=True), template
        )
        member_spec = _with_member_identity(
            member_spec,
            role="teammate",
            display_name=member_display,
            group_display=group_display,
            identity_text=_render_member_identity_text(
                template,
                role="teammate",
                group_display=group_display,
                member_spec=member_spec,
            ),
        )
        spec.agents[agent_name] = member_spec
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
    spec.dispatch_mode = "autonomous"
    spec.enable_task_verification = False


def enrich_team_spec_for_swarm(
    spec: Any,
    *,
    session_id: str,
    mode: str,
    project_dir: str | None = None,
    request_id: str | None = None,
    channel_id: str | None = None,
    request_metadata: dict[str, Any] | None = None,
    agent_group_name: str | None = None,
    agent_group_package_dir: Path | None = None,
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
        agent_group_name: 本会话绑定的专家团包名（expert_id）。非 None 时在基础
            enrich 之后调用 ``_apply_agent_group`` 覆写 roster/快照/模式字段——
            先基础 enrich 后团覆写，专家成员继承 jiuwenswarm 提供的
            rails/tools/MCP/workspace/权限。
        agent_group_package_dir: 调用方预取的专家团包目录（async 边界
            ``resolve_expert_package_dir`` 的结果，缓存优先 + fetch 兜底）。
            为 None 时 ``_apply_agent_group`` 只查缓存，miss 即抛——
            本地调试源不落缓存，生产路径需预取。
    """
    register_swarm_providers()

    config = get_config()
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
        trajectory_registry=InMemoryTrajectoryRegistry(),
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

    if agent_group_name:
        _apply_agent_group(spec, agent_group_name, package_dir=agent_group_package_dir)

    spec.build_context = base
    # Carry a serializable seed alongside the live context so members rebuilt
    # across a serialization boundary (spawned teammate, distributed remote,
    # cold recovery) can reconstruct the context via the registered factory.
    spec.build_context_seed = base.to_seed()
    logger.info(
        "[swarm.assembly] enriched team spec '%s' (roles=%s, session=%s, mcps=%d, agent_group=%s)",
        spec.team_name,
        [role for role in _MEMBER_ROLES if role in spec.agents],
        session_id,
        len(mcp_configs),
        agent_group_name,
    )


__all__ = ["enrich_team_spec_for_swarm"]
