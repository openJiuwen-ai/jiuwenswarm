# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.content.tools import (
    _BASH_CN,
    _BASH_EN,
    _BROWSER_ROUTING_CN,
    _BROWSER_ROUTING_EN,
    _NAME_BASH,
    _NAME_BROWSER_ROUTING,
    _NAME_RESULT,
    _NAME_SELECTION,
    _PRIO_BASH,
    _PRIO_BROWSER_ROUTING,
    _PRIO_RESULT,
    _PRIO_SELECTION,
    _RESULT_BASE_CN,
    _RESULT_BASE_EN,
    _SECTION_EXEC,
    _SECTION_TOOLS,
    _SELECTION_CN,
    _SELECTION_EN,
    _TOOLS_BASH_CN,
    _TOOLS_BASH_EN,
    _TOOLS_BASH_BULLETS_CN,
    _TOOLS_BASH_BULLETS_EN,
    _TOOLS_DEDUP_CN,
    _TOOLS_DEDUP_EN,
    _TOOLS_DEDUP_BULLETS_CN,
    _TOOLS_DEDUP_BULLETS_EN,
    _TOOLS_PRODUCED_NAMES,
    _TOOLS_TASKTOOL_CN,
    _TOOLS_TASKTOOL_EN,
    _strip_tools_guide,
)

logger = logging.getLogger(__name__)


class FlashToolGuideRail(DeepAgentRail):
    """拆分/改写 tools 段 → 300/330，从 400 拆出 340（内容以 06 目标为准）。"""

    # 需在 ContextAssembleRail(85) 之后拿到 fresh tools 段、FlashPersonaRail(4) 之后拿到 400，
    # 且在 audit(2) 之前；取 3。
    priority = 3
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        self._subagent_types: frozenset[str] = frozenset()
        self._ability_manager: Any = None
        # init 时绑定的外层 DeepAgent（有 deep_config.subagents）；BEFORE_MODEL_CALL 会
        # 派发给内层 ReActAgent（无 deep_config），子代理类型须从外层读（评审 #3）。
        self._config_agent: Any = None
        # 首次剥离前保存的源 tools 段快照（name, priority, content），供域禁用回退（评审 #6）。
        # tools 源段剥离后上游 ContextAssembleRail 不重注入（段名恒在、fingerprint 不变），
        # 必须靠快照恢复完整原文，否则去重/bash 纪律静默丢失。
        self._original_tools: tuple[str, int, Any] | None = None
        self._stripped_builder_id: int | None = None

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._config_agent = agent
        self._subagent_types = self._available_subagent_types(agent)
        self._ability_manager = getattr(agent, "ability_manager", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None
        self._config_agent = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        # 子代理类型从 init 绑定的外层 agent 读（内层 ReActAgent 无 deep_config）；
        # 工具面从本轮回调的 ctx.agent（内层）读（评审 #3）。
        self._subagent_types = self._available_subagent_types(self._config_agent or ctx.agent)
        self._ability_manager = getattr(ctx.agent, "ability_manager", None)
        self.apply(builder)

    def apply(self, builder: Any) -> None:
        """Apply the tool-guide transform without owning Rail callback registration."""
        if builder is None:
            return
        self._system_prompt_builder = builder

        language = self._resolve_language(builder)
        is_cn = language == "cn"

        # ① tools → 目录 + 300 + 330
        self._restructure_tools(builder, language, is_cn)

        # ② 400 → 340（340 自包含，不再从 400 剥离 bullet）
        self._extract_tool_result(builder, language, is_cn)

    def restore(self, builder: Any) -> None:
        """撤销 tools 域装配（评审 #6）：清派生段 + 恢复源 tools 段原文 + 重置快照。

        派生段（300/330/340/345）域禁用时恒清，无论源段是否被剥离过；源段恢复仅在快照
        存在时进行（源段未剥离则无需恢复）。快照与 builder 绑定：热重载重建 builder 后旧
        快照属于旧 builder，不能写回。恢复前先判断当前源段是否已完整（含去重子标题，上游
        重注入/漂移），若是则不覆盖、避免用旧快照覆盖带 drift 的完整原文。
        """
        if builder is None:
            return
        # 清派生段（300/330/340/345）。
        for name in _TOOLS_PRODUCED_NAMES:
            if self._get_section(builder, name) is not None:
                builder.remove_section(name)
        if self._original_tools is None:
            return
        if self._stripped_builder_id is not None and self._stripped_builder_id != id(builder):
            self._stripped_builder_id = None
            self._original_tools = None
            return
        # 当前源段已完整（含去重子标题）则不覆盖，否则恢复快照原文。
        already_complete = False
        current = self._get_section(builder, _SECTION_TOOLS)
        if current is not None:
            lang = self._resolve_language(builder)
            dedup = _TOOLS_DEDUP_CN if lang == "cn" else _TOOLS_DEDUP_EN
            if dedup in self._extract_content(current, lang):
                already_complete = True
        if not already_complete:
            name, priority, content = self._original_tools
            builder.add_section(PromptSection(name=name, content=content, priority=priority))
        self._stripped_builder_id = None
        self._original_tools = None

    # ── ① ───────────────────────────────────────────────
    def _restructure_tools(self, builder: Any, language: str, is_cn: bool) -> None:
        dedup = _TOOLS_DEDUP_CN if is_cn else _TOOLS_DEDUP_EN
        bash = _TOOLS_BASH_CN if is_cn else _TOOLS_BASH_EN
        tasktool = _TOOLS_TASKTOOL_CN if is_cn else _TOOLS_TASKTOOL_EN
        dedup_bullets = _TOOLS_DEDUP_BULLETS_CN if is_cn else _TOOLS_DEDUP_BULLETS_EN
        bash_bullets = _TOOLS_BASH_BULLETS_CN if is_cn else _TOOLS_BASH_BULLETS_EN

        section = self._get_section(builder, _SECTION_TOOLS)
        if section is None:
            return
        content = self._extract_content(section, language)
        stripped = _strip_tools_guide(content, dedup, bash, tasktool, dedup_bullets, bash_bullets)
        if stripped is None:
            # 「去重子标题缺失」有两种可能：① 本 rail 已剥过（ContextAssembleRail 的
            # fingerprint 命中后不再重建 tools 段，正常态）；② 上游改文案。二者无法可靠区分，
            # 且行为都应是「不盲删」，故 debug 级记录、静默跳过。
            logger.debug("[FlashToolGuideRail] tools 段无「工具调用去重规则」子标题，跳过剥离（已剥或上游变更）")
        else:
            # 首次剥离前保存源 tools 段快照（完整原文 + 原 priority），域禁用时 restore 恢复。
            self._original_tools = (
                str(section.name),
                int(getattr(section, "priority", 0) or 0),
                getattr(section, "content", None),
            )
            self._stripped_builder_id = id(builder)
            self._replace_section(builder, _SECTION_TOOLS, language, stripped)

        self._upsert(builder, _NAME_SELECTION, _PRIO_SELECTION, language,
                     _SELECTION_CN if is_cn else _SELECTION_EN)
        self._upsert(builder, _NAME_BASH, _PRIO_BASH, language,
                     _BASH_CN if is_cn else _BASH_EN)

    # ── ② ───────────────────────────────────────────────
    def _extract_tool_result(self, builder: Any, language: str, is_cn: bool) -> None:
        # 340 工具结果与失败：自 400「工具结果优先」拆出后已成为自包含段（正文按名引用
        # 400「拒绝盲信写入」）。400 已由 FlashPersonaRail 改写为「通用+场景化」两组准则，
        # 「工具结果优先」末条不再存在于 400，故这里不再从 400 剥离任何 bullet；仍以 400
        # 存在为前提（persona 先跑、400 恒在），避免 340 引用悬空的 400。
        if self._get_section(builder, _SECTION_EXEC) is None:
            return

        # 与注册表 _render_tools 同一个判断：浏览器任务强制路由只在 task_tool 存在且
        # browser_agent 子代理可用时注入——否则会强制模型调用不存在的 browser_agent
        # （Subagent spec not found）。340 基础段无条件注入；路由拆为独立动态段
        # browser_routing@345（评审 C1），子代理/工具面变化时不影响静态 340。
        result = _RESULT_BASE_CN if is_cn else _RESULT_BASE_EN
        self._upsert(builder, _NAME_RESULT, _PRIO_RESULT, language, result)
        if self._browser_agent_available(builder):
            self._upsert(
                builder,
                _NAME_BROWSER_ROUTING,
                _PRIO_BROWSER_ROUTING,
                language,
                _BROWSER_ROUTING_CN if is_cn else _BROWSER_ROUTING_EN,
            )
        else:
            # 路由从「可用」翻「不可用」（如 task_tool 被 disabled_tools 禁用）时，
            # 上一轮写入的 browser_routing@345 会残留、模型继续被要求调用已不可用的
            # browser_agent——显式清除（评审 C1 收尾）。
            if self._get_section(builder, _NAME_BROWSER_ROUTING) is not None:
                builder.remove_section(_NAME_BROWSER_ROUTING)

    def _browser_agent_available(self, builder: Any) -> bool:
        """与注册表 _render_tools 同一判断：task_tool 在工具面 且 browser_agent 在子代理类型里。"""
        return self._has_tool("task_tool") and "browser_agent" in self._subagent_types

    def _has_tool(self, name: str) -> bool:
        """判断工具名是否在 ability_manager 的工具面（cards/tools/list_tool_names）。"""
        manager = self._ability_manager
        if manager is None:
            return False
        for attr in ("cards", "_tools", "tools"):
            value = getattr(manager, attr, None)
            if isinstance(value, dict):
                return name in {str(k) for k in value}
        names = getattr(manager, "list_tool_names", None)
        if callable(names):
            try:
                result = names()
                if isinstance(result, (list, set, tuple)):
                    return name in {str(n) for n in result}
            except Exception:
                logger.debug("[FlashToolGuideRail] list_tool_names() failed", exc_info=True)
        return False

    @staticmethod
    def _available_subagent_types(agent: Any) -> frozenset[str]:
        """从 agent.deep_config.subagents 提取可用子代理类型名。"""
        subagents = getattr(getattr(agent, "deep_config", None), "subagents", None) or []
        names: set[str] = set()
        for spec in subagents:
            card = getattr(spec, "agent_card", None)
            name = getattr(card, "name", None)
            if not name:
                card = getattr(spec, "card", None)
                name = getattr(card, "name", None)
            if name:
                names.add(str(name))
        return frozenset(names)

    # ── 工具方法 ──────────────────────────────────────────
    def _upsert(self, builder: Any, name: str, priority: int, language: str, text: str) -> None:
        builder.add_section(PromptSection(name=name, content={language: text}, priority=priority))

    @staticmethod
    def _replace_section(builder: Any, name: str, language: str, text: str) -> None:
        """同名覆盖（add_section 按 name 去重）。"""
        priority = None
        existing = getattr(builder, "get_section", None)
        if callable(existing):
            try:
                sec = existing(name)
                if sec is not None:
                    priority = getattr(sec, "priority", None)
            except Exception:  # noqa: BLE001
                priority = None
        # 保留原 priority（tools=30）
        builder.add_section(PromptSection(
            name=name,
            content={language: text},
            priority=priority if priority is not None else 0,
        ))

    @staticmethod
    def _get_section(builder: Any, name: str) -> Any | None:
        get_section = getattr(builder, "get_section", None)
        if not callable(get_section):
            return None
        try:
            return get_section(name)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _extract_content(section: Any, language: str) -> str:
        content = getattr(section, "content", None)
        if isinstance(content, dict):
            value = content.get(language)
            if not value:
                value = next((v for v in content.values() if v), "")
            return str(value)
        return str(content or "")

    @staticmethod
    def _resolve_language(builder: Any) -> str:
        language = getattr(builder, "language", None) or "cn"
        return "cn" if language in ("cn", "zh") else "en"


__all__ = [
    "FlashToolGuideRail",
    "_strip_tools_guide",
]
