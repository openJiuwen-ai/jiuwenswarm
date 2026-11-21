# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.content.response import (
    _FINAL_CN,
    _FINAL_EN,
    _LANGUAGE_CN,
    _LANGUAGE_EN,
    _MODEL_CN,
    _MODEL_EN,
    _NAME_FINAL,
    _NAME_LANGUAGE,
    _NAME_MODEL,
    _NAME_RESPONSE,
    _NAME_THINKING,
    _PRIO_FINAL,
    _PRIO_LANGUAGE,
    _PRIO_MODEL,
    _PRIO_RESPONSE,
    _PRIO_THINKING,
    _RESPONSE_CN,
    _RESPONSE_EN,
    _THINKING_CN,
    _THINKING_EN,
)

logger = logging.getLogger(__name__)


class FlashResponseRail(DeepAgentRail):
    """响应/交互/输出域迁号 + 改写（§七 落地）。"""

    # 源 rail（ResponsePromptRail=5 / RuntimePromptRail=5）每轮注入输出段；本 rail 需在其
    # 之后每轮重迁号/重写，故取 < 5 的优先级。与 FlashPersonaRail 同取 4。
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
        """Apply response renumbering and rewrites without a Rail callback."""
        if builder is None:
            return
        self._system_prompt_builder = builder
        self._migrate(builder)

    def _migrate(self, builder: Any) -> None:
        # 410：三矛盾修正（修复.md ①-⑤）→ 迁号 + 改写为 06 目标正文。
        self._rewrite(builder, _NAME_THINKING, _PRIO_THINKING, _THINKING_CN, _THINKING_EN)
        # 500/510/520/540：迁号 + 06 目标改写（单语言注入，按当前语言取 CN/EN）。
        self._rewrite(builder, _NAME_LANGUAGE, _PRIO_LANGUAGE, _LANGUAGE_CN, _LANGUAGE_EN)
        self._rewrite(builder, _NAME_RESPONSE, _PRIO_RESPONSE, _RESPONSE_CN, _RESPONSE_EN)
        self._rewrite(builder, _NAME_FINAL, _PRIO_FINAL, _FINAL_CN, _FINAL_EN)
        self._rewrite(builder, _NAME_MODEL, _PRIO_MODEL, _MODEL_CN, _MODEL_EN)

    def _rewrite(self, builder: Any, name: str, priority: int, cn: str, en: str) -> None:
        """迁号 + 改写为 06 目标正文。源段缺失则跳过（不凭空新增段）。"""
        if self._get_section(builder, name) is None:
            return
        language = self._resolve_language(builder)
        text = cn if language == "cn" else en
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
    def _resolve_language(builder: Any) -> str:
        language = getattr(builder, "language", None) or "cn"
        return "cn" if language in ("cn", "zh") else "en"


__all__ = ["FlashResponseRail"]
