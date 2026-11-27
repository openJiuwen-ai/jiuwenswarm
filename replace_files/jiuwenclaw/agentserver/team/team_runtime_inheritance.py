# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team 成员运行时继承模块.

TeamMember 专用 Rail、Ability 继承逻辑，不依赖主 agent adapter。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.harness.rails.filesystem_rail import FileSystemRail
from openjiuwen.harness.rails.heartbeat_rail import HeartbeatRail
from openjiuwen.harness.rails.security_rail import SecurityRail
from openjiuwen.harness.rails.skill_use_rail import SkillUseRail
from openjiuwen.harness.rails.subagent_rail import SubagentRail
from openjiuwen.harness.rails.task_planning_rail import TaskPlanningRail

from jiuwenclaw.agentserver.deep_agent.rails.avatar_rail import AvatarPromptRail
from jiuwenclaw.agentserver.deep_agent.rails.response_prompt_rail import ResponsePromptRail
from jiuwenclaw.agentserver.deep_agent.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import SkillComplianceRail
from jiuwenclaw.agentserver.deep_agent.rails.skill_prompt_rail import SkillProtocolPromptRail
from jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail import JiuClawStreamEventRail
from jiuwenclaw.utils import format_session_log
from jiuwenclaw.utils import get_agent_registered_skill_dirs

logger = logging.getLogger(__name__)

RAIL_WHITELIST = frozenset({
    "RuntimePromptRail",
    "ResponsePromptRail",
    "JiuClawStreamEventRail",
    "TaskPlanningRail",
    "SecurityRail",
    "HeartbeatRail",
    "AvatarPromptRail",
    "FileSystemRail",
    "SkillUseRail",
    "SkillProtocolPromptRail",
    "SkillComplianceRail",
    "JiuClawContextEngineeringRail",
    "SubagentRail",
})

TOOL_WHITELIST = frozenset({
    "free_search",
    "fetch_webpage",
    "paid_search",
    "vision",
    "audio",
    "image_ocr",
    "visual_question_answering",
    "audio_transcription",
    "audio_question_answering",
    "audio_metadata",
    "video_understanding",
    "search_skill",
    "install_skill",
    "uninstall_skill",
    "task_tool",
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
})


@dataclass
class MemberRailConfig:
    """``build_member_rails`` 的构造参数集合。

    Attributes:
        skills_dir: 兼容保留参数，当前不参与 skill rail 构造。
        language: 语言设置。
        channel: 渠道设置（使用真实 channel_id）。
        agent_name: 成员名称。
        model_name: 模型名称。
        session_id: team 会话 id，注入给 SkillComplianceRail 以对齐 skill_step facade 的
            skill_step.md 路径；未传入时 rail 内部会回退到 ctx.conversation_id 或 "default"。
        member_id: team 成员 id，用于流事件 rail 与日志标识。
        context_engine_enabled: 是否挂载上下文工程 rail；Team 默认开启。
        react_config: 与主 agent 一致的 ``react`` 配置片段；默认预置链 B
            （见 ``session_memory``），可与主 agent 一致地改为链 A。
    """

    skills_dir: str
    language: str = "cn"
    channel: str = "default"
    agent_name: str = "team_member"
    model_name: str = "gpt-4"
    session_id: str | None = None
    member_id: str | None = None
    context_engine_enabled: bool = True
    react_config: dict[str, Any] | None = None


def build_member_rails(config: MemberRailConfig) -> list[Any]:
    """为 Team 成员创建 rails 列表。参数通过 ``MemberRailConfig`` 传入。"""
    language = config.language
    channel = config.channel
    agent_name = config.agent_name
    model_name = config.model_name
    session_id = config.session_id
    member_id = config.member_id
    context_engine_enabled = config.context_engine_enabled
    react_config = config.react_config

    rails_list = []

    try:
        rail = RuntimePromptRail(
            language=language,
            channel=channel,
            agent_name=agent_name,
            model_name=model_name,
        )
        rails_list.append(rail)
        logger.info("[TeamRuntime] RuntimePromptRail created: channel=%s", channel)
    except Exception as exc:
        logger.warning("[TeamRuntime] RuntimePromptRail failed: %s", exc)

    try:
        rail = ResponsePromptRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] ResponsePromptRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] ResponsePromptRail failed: %s", exc)

    try:
        rail = FileSystemRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] FileSystemRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] FileSystemRail failed: %s", exc)

    try:
        rail = JiuClawStreamEventRail()
        rails_list.append(rail)
        logger.info(
            "[TeamRuntime] JiuClawStreamEventRail created: member=%s session=%s",
            member_id, session_id,
        )
    except Exception as exc:
        logger.warning("[TeamRuntime] JiuClawStreamEventRail failed: %s", exc)

    try:
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
        rail = HeartbeatRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] HeartbeatRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] HeartbeatRail failed: %s", exc)

    try:
        rail = AvatarPromptRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] AvatarPromptRail created")
    except Exception as exc:
        logger.warning("[TeamRuntime] AvatarPromptRail failed: %s", exc)

    try:
        rail = SubagentRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] SubagentRail created (task_tool when deep_config.subagents set)")
    except Exception as exc:
        logger.warning("[TeamRuntime] SubagentRail failed: %s", exc)

    # Team 成员的技能清单（"skills" section）由 SkillUseRail 注入，与主 agent 一致。
    # include_tools=False：read_file/code/bash 由成员 TOOL_WHITELIST 继承，不在此重复注册。
    # include_skill_body_tools=True：仍注册 skill_tool/skill_complete，与「技能」段及主站行为一致。
    try:
        skill_dirs = [str(p) for p in get_agent_registered_skill_dirs()]
        rail = SkillUseRail(
            skills_dir=skill_dirs,
            skill_mode=SkillUseRail.SKILL_MODE_ALL,
            include_tools=False,
            include_skill_body_tools=True,
        )
        rails_list.append(rail)
        logger.info("[TeamRuntime] SkillUseRail created: skills_dir=%s", skill_dirs)
    except Exception as exc:
        logger.warning("[TeamRuntime] SkillUseRail failed: %s", exc)

    try:
        rail = SkillProtocolPromptRail()
        rails_list.append(rail)
        logger.info("[TeamRuntime] SkillProtocolPromptRail created (protocol section only)")
    except Exception as exc:
        logger.warning("[TeamRuntime] SkillProtocolPromptRail failed: %s", exc)

    try:
        rail = SkillComplianceRail(session_id=session_id)
        rails_list.append(rail)
        logger.info(
            format_session_log(session_id, "[TeamRuntime] SkillComplianceRail created"),
        )
    except Exception as exc:
        logger.warning("[TeamRuntime] SkillComplianceRail failed: %s", exc)

    if context_engine_enabled:
        try:
            # 与 JiuWenClawDeepAdapter agent.plan 一致（默认预置链 B，见 react.context_engine_config.session_memory）
            from jiuwenclaw.agentserver.deep_agent.interface_deep import _build_context_engineering_rail

            react = react_config
            if react is None:
                try:
                    from jiuwenclaw.config import get_config

                    react = (get_config() or {}).get("react", {}) or {}
                except Exception:
                    react = {}
            rail = _build_context_engineering_rail(react, mode="agent.plan")
            if rail is not None:
                rails_list.append(rail)
                logger.info(
                    format_session_log(
                        session_id,
                        "[TeamRuntime] Context engineering rail mounted "
                    "(MicroCompact/FullCompact/offload preset): "
                    "agent_name=%s member_id=%s session_id=%s. "
                    "Runtime: search logs for 'trigger context processor' / "
                    "'MicroCompactProcessor' / '[FullCompact]'. "
                    "Optional NDJSON dumps: JIUWENCLAW_PROMPT_DUMP_DIR or "
                    "~/.jiuwenclaw/logs/logs/sessions/<session_id>/compact_<member_id>.log",
                    ),
                    agent_name,
                    member_id,
                    session_id,
                )
        except Exception as exc:
            logger.warning("[TeamRuntime] Context engineering rail failed: %s", exc)

    logger.info("[TeamRuntime] Total rails built: %d", len(rails_list))
    if not context_engine_enabled:
        logger.info(
            "[TeamRuntime] context_engine_enabled=False: team member will not run CE compact/offload chain "
            "(react.context_engine_config.enabled is false)",
        )
    return rails_list


def filter_inheritable_ability_cards(
    main_agent: Any,
    *,
    exclude_tool_names: frozenset[str] | None = None,
) -> list[ToolCard]:
    """从主 agent 获取可继承的 ToolCard 白名单.

    Args:
        main_agent: 主 DeepAgent 实例
        exclude_tool_names: 不继承的工具名（如 ``task_tool``，由成员侧 ``SubagentRail`` 自行注册）

    Returns:
        白名单内的 ToolCard 列表
    """
    result = []
    skip = exclude_tool_names or frozenset()
    try:
        abilities = main_agent.ability_manager.list()
        for ability in abilities:
            if isinstance(ability, ToolCard):
                if ability.name in TOOL_WHITELIST and ability.name not in skip:
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
            from jiuwenclaw.agentserver.config import get_config
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
