# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.content.safety import (
    _ANCHORS_CN,
    _ANCHORS_EN,
    _SECTION_SAFETY,
    _transform_safety,
)

logger = logging.getLogger(__name__)


class FlashSafetyRail(DeepAgentRail):
    """拆分/改写 safety 巨石段为 030/100/110/120（§七 落地）。"""

    # safety 段由 SafetyPromptRail（priority 85）每轮注入；本 rail 需在其之后
    # 每轮重新拆分，故取任意 < 85 的优先级。与 FlashPersonaRail 同取 4。
    priority = 4
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
        """Apply the safety transform without owning Rail callback registration."""
        if builder is None:
            return
        self._system_prompt_builder = builder
        self._maybe_transform_safety(builder)

    def _maybe_transform_safety(self, builder: Any) -> None:
        """每轮拆分/改写：safety 段不存在（未装配/已拆）则跳过。

        SafetyPromptRail 每轮都会重新注入 safety 段，故本方法不设幂等 flag。
        """
        section = self._get_section(builder, _SECTION_SAFETY)
        if section is None:
            return

        content = self._extract_content(section, builder)
        language = self._resolve_language(builder)
        anchors = _ANCHORS_CN if language == "cn" else _ANCHORS_EN
        result = _transform_safety(content, anchors)
        if result is None:
            logger.warning(
                "[FlashSafetyRail] safety 拆分/改写命中守卫（锚点缺失/乱序），保留原生 safety 段原样"
            )
            return

        builder.remove_section(_SECTION_SAFETY)
        for name, priority, text in result:
            builder.add_section(PromptSection(
                name=name,
                content={language: text},
                priority=priority,
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
    def _extract_content(section: Any, builder: Any) -> str:
        content = getattr(section, "content", None)
        if isinstance(content, dict):
            language = getattr(builder, "language", None) or "cn"
            language = "cn" if language in ("cn", "zh") else "en"
            value = content.get(language)
            if not value:
                value = next((v for v in content.values() if v), "")
            return str(value)
        return str(content or "")

    @staticmethod
    def _resolve_language(builder: Any) -> str:
        language = getattr(builder, "language", None) or "cn"
        return "cn" if language in ("cn", "zh") else "en"


__all__ = ["FlashSafetyRail", "_transform_safety"]
