# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team 成员运行时继承模块.

TeamMember 专用 Rail、Ability 继承逻辑，不依赖主 agent adapter。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.harness.rails import (
    SysOperationRail,
    SecurityRail,
    TaskPlanningRail,
)
from openjiuwen.harness.rails import (
    EvolutionInterruptRail,
    SkillEvolutionRail,
    TeamSkillCreateRail,
    TeamSkillEvolutionRail,
)
from openjiuwen.harness.rails.evolution import EvolutionReviewRuntime
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.avatar_rail import AvatarPromptRail
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import ResponsePromptRail
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.agents.harness.team.rails.team_workspace_report_path_rail import TeamWorkspaceReportPathRail
from jiuwenswarm.common.config import (
    get_config,
    get_evolution_auto_save_enabled,
    get_skill_evolution_enabled,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.common.utils import get_agent_skills_dir
from jiuwenswarm.agents.harness.observability_runtime import (
    get_trajectory_span_processor,
)
from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

logger = logging.getLogger(__name__)


@dataclass
class MemberInfo:
    """成员身份信息."""
    agent_name: str = "team_member"
    model_name: str = "gpt-4"
    role: str | None = None


@dataclass
class RuntimeInfo:
    """运行时环境信息."""
    channel: str = "default"
    language: str = "cn"


@dataclass
class TeamWorkspaceInfo:
    """Team 共享 workspace 信息.

    Attributes:
        root_dir: Team shared workspace root.
        skills_dir: Legacy team skills view path. Skills now live in exactly
            one physical library, so this no longer locates any Skill and is
            kept only for callers that still thread it through.
        team_id: Team name.
        config: Resolved ``config.yaml`` mapping.
        trajectory_registry: Per-team in-memory trajectory registry.
    """
    root_dir: str | None = None
    skills_dir: str | None = None
    team_id: str | None = None
    config: dict[str, Any] | None = None
    trajectory_span_processor: Any | None = None
    project_dir: str | None = None


RAIL_WHITELIST = frozenset({
    "RuntimePromptRail",
    "ResponsePromptRail",
    "JiuSwarmStreamEventRail",
    "TaskPlanningRail",
    "SecurityRail",
    "AvatarPromptRail",
    "StructuredAskUserRail",
    "FileSystemRail",
    "SysOperationRail",
    "TeamSkillEvolutionRail",
    "TeamSkillCreateRail",
    "EvolutionInterruptRail",
    "SkillEvolutionRail",
    "TeamWorkspaceReportPathRail",
    "ContextProcessorRail",
})

TOOL_WHITELIST = frozenset({
    "free_search",
    "fetch_webpage",
    "paid_search",
    "vision",
    "audio",
    "image_ocr",
    "visual_question_answering",
    "generate_image",
    "generate_video",
    "audio_transcription",
    "audio_question_answering",
    "audio_metadata",
    "video_understanding",
    "search_skill",
    "install_skill",
    "uninstall_skill",
    "skill_index",
    "user_todos",
    "get_user_location",
    "create_note",
    "search_notes",
    "modify_note",
    "create_calendar_event",
    "search_calendar_event",
    "search_contact",
    "search_photo_gallery",
    "upload_photo",
    "search_file",
    "upload_file",
    "call_phone",
    "send_message",
    "search_message",
    "create_alarm",
    "search_alarms",
    "modify_alarm",
    "delete_alarm",
    "xiaoyi_collection",
    "image_reading",
    "xiaoyi_gui_agent",
    "web_free_search",
    "web_fetch_webpage",
    "web_paid_search",
    "skill_toolkit",
    "acp_chat",
})


def build_member_rails(
    member_info: MemberInfo | None = None,
    runtime: RuntimeInfo | None = None,
    team_workspace: TeamWorkspaceInfo | None = None,
) -> list[Any]:
    """为 Team 成员创建 rails 列表.

    Skill 相关 rail 一律指向唯一的全局 skill 实体库；team 不再拥有 skills 视图目录，
    因此这些 rail 的挂载不再依赖 ``team_workspace.skills_dir``.

    Args:
        member_info: 成员身份信息（agent_name, role）
        runtime: 运行时环境信息（channel, language）
        team_workspace: 团队共享 workspace 信息

    Returns:
        rail 实例列表
    """
    member_info = member_info or MemberInfo()
    runtime = runtime or RuntimeInfo()
    team_workspace = team_workspace or TeamWorkspaceInfo()

    role = member_info.role
    channel = runtime.channel
    language = runtime.language
    team_ws_root = team_workspace.root_dir
    team_id = team_workspace.team_id
    config = team_workspace.config
    team_trajectory_span_processor = team_workspace.trajectory_span_processor
    skill_evolution_enabled = get_skill_evolution_enabled(config)
    # Single physical Skill library shared by every agent; per-team and
    # per-member skill directories no longer exist.
    global_skills_root = str(get_agent_skills_dir())

    rails_list = []

    try:
        rail = RuntimePromptRail(
            language=language,
            channel=channel,
        )
        rails_list.append(rail)
        logger.info("[TeamRuntime] RuntimePromptRail created: channel=%s", channel)
    except Exception as exc:
        logger.warning("[TeamRuntime] RuntimePromptRail failed: %s", exc)

    try:
        rail = ResponsePromptRail()
        rail.set_channel(channel)
        rails_list.append(rail)
        logger.info("[TeamRuntime] ResponsePromptRail created: channel=%s", channel)
    except Exception as exc:
        logger.warning("[TeamRuntime] ResponsePromptRail failed: %s", exc)

    try:
        rail = SysOperationRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] FileSystemRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] FileSystemRail failed: %s", exc)

    try:
        rail = JiuSwarmStreamEventRail(
            member_name=member_info.agent_name,
            role=member_info.role,
        )
        rails_list.append(rail)
        logger.info("[TeamRuntime] JiuSwarmStreamEventRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] JiuSwarmStreamEventRail failed: %s", exc)

    if role == "leader":
        try:
            rail = StructuredAskUserRail(language=language)
            rails_list.append(rail)
            logger.info("[TeamRuntime] StructuredAskUserRail created for leader")
        except Exception as exc:
            logger.warning("[TeamRuntime] StructuredAskUserRail failed: %s", exc)

    try:
        if role != "leader":
            rail = TaskPlanningRail()
            rails_list.append(rail)
            logger.info("[TeamRuntime] TaskPlanningRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] TaskPlanningRail failed: %s", exc)

    try:
        rail = SecurityRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] SecurityRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] SecurityRail failed: %s", exc)

    try:
        rail = AvatarPromptRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] AvatarPromptRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] AvatarPromptRail failed: %s", exc)

    if team_ws_root:
        try:
            rail = TeamWorkspaceReportPathRail(
                root_dir=team_ws_root,
                project_dir=team_workspace.project_dir,
                team_id=team_id,
                language=language,
            )
            rails_list.append(rail)
            logger.info(
                "[TeamRuntime] TeamWorkspaceReportPathRail created: root_dir=%s",
                team_ws_root,
            )
        except Exception as exc:
            logger.warning("[TeamRuntime] TeamWorkspaceReportPathRail failed: %s", exc)

    # Leader-only: TeamSkillEvolutionRail for team skill evolution.
    if role == "leader" and skill_evolution_enabled:
        try:
            Path(global_skills_root).mkdir(parents=True, exist_ok=True)
            llm_model, actual_model_name = build_evolution_llm(config)
            evolution_auto_save = get_evolution_auto_save_enabled(config)
            review_runtime = EvolutionReviewRuntime()
            team_skill_rail = TeamSkillEvolutionRail(
                skills_dir=global_skills_root,
                llm=llm_model,
                model=actual_model_name,
                review_runtime=review_runtime,
                language=language,
                signal_trigger=False,
                review_trigger=True,
                member_role=role,
                auto_save=evolution_auto_save,
                team_id=team_id,
                disabled_skills=load_execution_disabled_skills(),
                trajectory_span_processor=(
                    team_trajectory_span_processor or get_trajectory_span_processor()
                ),
            )
            rails_list.append(
                EvolutionInterruptRail(
                    review_runtime=review_runtime,
                    submission_service=team_skill_rail.experience_manager.experience_submission_service,
                    auto_save=evolution_auto_save,
                    language=language,
                )
            )
            rails_list.append(team_skill_rail)
            logger.info(
                "[TeamRuntime] TeamSkillEvolutionRail created: skills_dir=%s, "
                "model=%s, auto_save=%s, trajectory_span_processor=%s",
                global_skills_root,
                actual_model_name,
                evolution_auto_save,
                bool(team_trajectory_span_processor),
            )
        except Exception as exc:
            logger.warning("[TeamRuntime] TeamSkillEvolutionRail failed: %s", exc, exc_info=True)

    # Leader-only: TeamSkillCreateRail for team skill creation proposals.
    # Skill creation follows the same canonical capability switch as evolution.
    # New skills are authored straight into the single library: there is no
    # team-scoped skill directory to stage them in.
    if role == "leader" and skill_evolution_enabled:
        try:
            team_skill_create_rail = TeamSkillCreateRail(
                skills_dir=global_skills_root,
                language=language,
                auto_trigger=True,
                trajectory_span_processor=(
                    team_trajectory_span_processor or get_trajectory_span_processor()
                ),
            )
            rails_list.append(team_skill_create_rail)
            logger.info(
                "[TeamRuntime] TeamSkillCreateRail created: skills_dir=%s, team_id=%s, member=%s",
                global_skills_root,
                team_id,
                member_info.agent_name,
            )
        except Exception as exc:
            logger.warning("[TeamRuntime] TeamSkillCreateRail failed: %s", exc, exc_info=True)

    # Non-leader: SkillEvolutionRail for member skill self-evolution.
    if role != "leader" and skill_evolution_enabled:
        review_runtime = EvolutionReviewRuntime()
        evo_rail = build_skill_evolution_rail(
            skills_dir=global_skills_root,
            config=config,
            trajectory_span_processor=team_trajectory_span_processor,
            team_id=team_id,
            review_runtime=review_runtime,
        )
        if evo_rail is not None:
            rails_list.append(evo_rail)

    # Context compression rail for all members (leader + teammates).
    # ``config`` here is the full config.yaml mapping (hot-reload path passes
    # ``get_config()``); strip the ``react`` outer key so the rail builder sees
    # the same ``{"context_engine_config": ...}`` shape the swarm provider path
    # passes (see member_rails._build_context_processor).
    if get_context_engine_enabled(config):
        react = config.get("react", {}) if isinstance(config, dict) else {}
        react = react if isinstance(react, dict) else {}
        rail = _build_context_processor_rail(
            {"context_engine_config": react.get("context_engine_config", {})}
        )
        if rail is not None:
            rails_list.append(rail)

    logger.info("[TeamRuntime] Total rails built: %d", len(rails_list))
    return rails_list


def filter_inheritable_ability_cards(main_agent: Any) -> list[ToolCard]:
    """从主 agent 获取可继承的 ToolCard 白名单.

    Args:
        main_agent: 主 DeepAgent 实例

    Returns:
        白名单内的 ToolCard 列表
    """
    result = []
    try:
        abilities = main_agent.ability_manager.list()
        for ability in abilities:
            if isinstance(ability, ToolCard):
                if ability.name in TOOL_WHITELIST:
                    result.append(ability)
                else:
                    logger.debug("[TeamRuntime] Tool '%s' not in whitelist, skipped", ability.name)
            else:
                logger.debug(
                    "[TeamRuntime] Skipping non-ToolCard ability: %s",
                    getattr(ability, "name", type(ability)),
                )
    except Exception as exc:
        logger.warning("[TeamRuntime] Failed to filter inheritable abilities: %s", exc)
    return result


def get_default_model_name(config: dict[str, Any] | None = None) -> str:
    """从配置获取默认 model_name.

    Args:
        config: 可选的配置字典

    Returns:
        model_name 字符串，默认为 "gpt-4"
    """
    if config is None:
        try:
            config = get_config()
        except Exception as exc:
            logger.warning("[TeamRuntime] Failed to load config for default model: %s", exc)
            return "gpt-4"

    try:
        model_name = config.get("models", {}).get("default", {}).get(
            "model_client_config", {}
        ).get("model_name")
        if model_name:
            return model_name
    except Exception as exc:
        logger.warning("[TeamRuntime] Failed to resolve default model name: %s", exc)

    return "gpt-4"


def resolve_model_config(
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """从配置字典解析 model 相关参数.

    优先从 models.defaults 列表中取 is_default=true 的条目，
    回退到 models.default 单对象，再回退到 react 段。

    Args:
        config: 配置字典.

    Returns:
        (model_client_config dict, model_config_obj dict, model_name str).
    """
    model_configs = config.get("models", {})

    # 优先从 models.defaults 列表取 is_default=true 的条目
    defaults_list = model_configs.get("defaults")
    if isinstance(defaults_list, list) and defaults_list:
        for entry in defaults_list:
            if isinstance(entry, dict) and entry.get("is_default") is True:
                mcc = (entry.get("model_client_config") or {}).copy()
                mco = (entry.get("model_config_obj") or {}).copy()
                model_name = mcc.get("model_name", "")
                if model_name:
                    return mcc, mco, model_name
        # 无 is_default=true 时取第一个
        first = defaults_list[0]
        if isinstance(first, dict):
            mcc = (first.get("model_client_config") or {}).copy()
            mco = (first.get("model_config_obj") or {}).copy()
            model_name = mcc.get("model_name", "")
            if model_name:
                return mcc, mco, model_name

    # 回退到旧格式
    default_model_config = model_configs.get("default", {}).copy()
    react_config = config.get("react", {}).copy()

    model_client_config = default_model_config.get("model_client_config") or {}
    if not model_client_config:
        model_client_config = react_config.get("model_client_config") or {}

    model_name = (
        model_client_config.get("model_name")
        or react_config.get("model_name")
        or "gpt-4"
    )

    model_config_obj = default_model_config.get("model_config_obj") or {}
    if not model_config_obj:
        model_config_obj = react_config.get("model_config_obj") or {}

    return model_client_config, model_config_obj, model_name


def build_evolution_llm(
    config: dict[str, Any] | None = None,
) -> tuple[Any, str]:
    """从配置构造 evolution 使用的 LLM Model 实例.

    Args:
        config: 可选配置字典，为 None 时自动加载.

    Returns:
        (Model 实例, model_name 字符串) 元组.
    """
    from openjiuwen.core.foundation.llm import (
        Model, ModelClientConfig, ModelRequestConfig,
    )

    if config is None:
        config = get_config()

    model_client_config, model_config_obj, model_name = resolve_model_config(config)

    from jiuwenswarm.common.openrouter_attribution import inject_attribution_headers
    inject_attribution_headers(model_client_config)

    request_config = ModelRequestConfig(
        **build_reasoning_model_request_kwargs(
            model_client_config=model_client_config,
            model_config_obj=model_config_obj,
            model_name=model_name,
        )
    )
    client_config = ModelClientConfig(**model_client_config)
    return Model(model_client_config=client_config, model_config=request_config), model_name


def build_skill_evolution_rail(
    skills_dir: str,
    config: dict[str, Any] | None = None,
    trajectory_span_processor: Any | None = None,
    team_id: str | None = None,
    review_runtime: EvolutionReviewRuntime | None = None,
) -> Any | None:
    """为 Team member 构造 SkillEvolutionRail.

    Args:
        skills_dir: 全局 skill 实体库根目录（唯一实体库，不再传 team 视图目录）.
        config: 可选配置字典.
        team_trajectory_sink: 团队轨迹注册表.
        team_id: 团队名，为空时不绑定团队轨迹.
        review_runtime: 复用的 review runtime.

    Returns:
        SkillEvolutionRail 实例，失败返回 None.
    """
    try:
        llm, model_name = build_evolution_llm(config)
        review_runtime = review_runtime or EvolutionReviewRuntime()

        rail = SkillEvolutionRail(
            skills_dir=skills_dir,
            llm=llm,
            model=model_name,
            review_runtime=review_runtime,
            signal_trigger=True,
            auto_save=True,
            review_trigger=False,
            disabled_skills=load_execution_disabled_skills(),
            trajectory_span_processor=(
                trajectory_span_processor or get_trajectory_span_processor()
            ),
        )
        logger.info(
            "[TeamRuntime] SkillEvolutionRail created: model=%s, auto_save=%s, "
            "trajectory_span_processor=%s",
            model_name,
            True,
            bool(trajectory_span_processor),
        )
        return rail
    except Exception as exc:
        logger.warning("[TeamRuntime] SkillEvolutionRail creation failed: %s", exc, exc_info=True)
        return None


def get_context_engine_enabled(config: dict[str, Any] | None) -> bool:
    """Check whether context compression is enabled in config.

    Reads ``react.context_engine_config.enabled`` (default True).
    """
    if not isinstance(config, dict):
        return True
    react = config.get("react", {})
    if isinstance(react, dict):
        ctx_cfg = react.get("context_engine_config", {})
        if isinstance(ctx_cfg, dict):
            return ctx_cfg.get("enabled", True)
    return True


def _build_context_processor_rail(config: dict[str, Any] | None) -> ContextProcessorRail | None:
    """Build a preset ContextProcessorRail for team members with user config thresholds.

    Expects a pre-extracted mapping of shape ``{"context_engine_config": {...}}`` —
    the ``react`` outer key must already be stripped by the caller. Both call sites
    pass this shape:

    * ``build_member_rails`` (hot-reload path, full config source) extracts
      ``react.context_engine_config`` before calling;
    * ``member_rails._build_context_processor`` (swarm provider) bakes the
      section into ``ContextProcessorInput`` at spec-build time.

    Mirrors :func:`interface_deep._build_context_processor_rail`, which takes the
    same shape (the ``react`` section itself).
    """
    try:
        from typing import List, Tuple

        user_processors: List[Tuple[str, dict]] = []
        ctx_cfg: dict[str, Any] = {}
        if isinstance(config, dict):
            raw = config.get("context_engine_config", {})
            ctx_cfg = raw if isinstance(raw, dict) else {}

        offloader_cfg = ctx_cfg.get("message_summary_offloader_config", {})
        if isinstance(offloader_cfg, dict) and offloader_cfg:
            user_processors.append(("MessageSummaryOffloader", offloader_cfg))

        compressor_cfg = ctx_cfg.get("dialogue_compressor_config", {})
        if isinstance(compressor_cfg, dict) and compressor_cfg:
            user_processors.append(("DialogueCompressor", compressor_cfg))

        current_round_cfg = ctx_cfg.get("current_round_compressor_config", {})
        if isinstance(current_round_cfg, dict) and current_round_cfg:
            user_processors.append(("CurrentRoundCompressor", current_round_cfg))

        round_level_cfg = ctx_cfg.get("round_level_compressor_config", {})
        if isinstance(round_level_cfg, dict) and round_level_cfg:
            user_processors.append(("RoundLevelCompressor", round_level_cfg))

        rail = ContextProcessorRail(
            processors=user_processors if user_processors else None,
            preset=True,
        )
        logger.info(
            "[TeamRuntime] ContextProcessorRail created (preset=True), "
            "user_processors=%s",
            [p[0] for p in user_processors] if user_processors else "none",
        )
        return rail
    except Exception as exc:
        logger.warning("[TeamRuntime] ContextProcessorRail creation failed: %s", exc, exc_info=True)
        return None
