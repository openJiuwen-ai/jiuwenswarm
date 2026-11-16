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
    _NAME_BASH,
    _NAME_RESULT,
    _NAME_SELECTION,
    _PRIO_BASH,
    _PRIO_RESULT,
    _PRIO_SELECTION,
    _RESULT_CN,
    _RESULT_EN,
    _SECTION_EXEC,
    _SECTION_TOOLS,
    _SELECTION_CN,
    _SELECTION_EN,
    _TOOLS_BASH_CN,
    _TOOLS_BASH_EN,
    _TOOLS_DEDUP_CN,
    _TOOLS_DEDUP_EN,
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

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
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

    # ── ① ───────────────────────────────────────────────
    def _restructure_tools(self, builder: Any, language: str, is_cn: bool) -> None:
        dedup = _TOOLS_DEDUP_CN if is_cn else _TOOLS_DEDUP_EN
        bash = _TOOLS_BASH_CN if is_cn else _TOOLS_BASH_EN
        tasktool = _TOOLS_TASKTOOL_CN if is_cn else _TOOLS_TASKTOOL_EN

        section = self._get_section(builder, _SECTION_TOOLS)
        if section is None:
            return
        content = self._extract_content(section, language)
        stripped = _strip_tools_guide(content, dedup, bash, tasktool)
        if stripped is None:
            # 「去重子标题缺失」有两种可能：① 本 rail 已剥过（ContextAssembleRail 的
            # fingerprint 命中后不再重建 tools 段，正常态）；② 上游改文案。二者无法可靠区分，
            # 且行为都应是「不盲删」，故 debug 级记录、静默跳过。
            logger.debug("[FlashToolGuideRail] tools 段无「工具调用去重规则」子标题，跳过剥离（已剥或上游变更）")
        else:
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

        self._upsert(builder, _NAME_RESULT, _PRIO_RESULT, language,
                     _RESULT_CN if is_cn else _RESULT_EN)

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
