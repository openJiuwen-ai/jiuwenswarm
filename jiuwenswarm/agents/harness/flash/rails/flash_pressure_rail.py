# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashPressureRail —— 水位压力注入（M4 ⑥）。

在 ``ContextOverflowRecoveryRail``（90% 主动 force_compact 强制层）之下挂一层
提示层阶梯：上下文使用率（provider 真实上报 ``input_tokens`` / 真实模型窗口）
达到 70% / 85% 时，通过 PromptAttachment 注入一条瞬态提醒，引导模型主动精简
上下文（委派子代理 / 停止加载 / 摘要替换），在触到 90% 强制压缩前把水位压下去。

- 与 90% 强制层共用同一套水位口径（复用 ContextOverflowRecoveryRail 的
  ``_resolve_model_context_limit`` + ``_last_prompt_tokens``），保证 70/85/90
  三档在同一把尺子上，不会因 tokenizer 估算偏差（751K vs 1.047M）错位。
- turn 1 无 ``usage_metadata``（首轮无上一轮上报），静默跳过；turn 2+ 才有值。
- 解析不到真实模型窗口时跳过（同强制层，避免用 200000 默认值误触发）。
- 附件属瞬态（8xx 运行时区），不占段序表号位；水位回落（< warning）时清空提醒。

本 rail 是「行为」不是「内容」，正文为提示层提醒文案（非 06 目标静态正文），
由水位阶梯动态触发。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.context_overflow_recovery_rail import (
    ContextOverflowRecoveryRail,
)

logger = logging.getLogger(__name__)

# 水位阶梯默认值（70/85），挂 90% 强制层之下。
_DEFAULT_WARNING_RATIO = 0.70
_DEFAULT_CRITICAL_RATIO = 0.85

_ATTACHMENT_SECTION = "context_pressure"
_SOURCE = "jiuwenswarm.flash.pressure_rail"
_KIND = PromptAttachmentKind.RUNTIME
# 附件块内排序键（与静态段 priority 是独立命名空间）。压力提醒是瞬态建议，
# 排在 repeat_call_reminder(70) 之后、runtime 事实(94/95) 之前。
_ATTACHMENT_PRIORITY = 71

_WARNING_CN = (
    "当前上下文使用率约 70%。请开始精简上下文：大块独立工作优先委派给子代理"
    "（task_tool），避免继续加载大文件或长文档，并及时总结已完成部分、丢弃中间过程细节。"
)
_CRITICAL_CN = (
    "当前上下文使用率约 85%，接近上限。请立即压缩：所有大块独立工作一律委派给子代理"
    "（不要在主上下文执行），停止加载新内容，用简短摘要替换已完成的中间结果。"
)
_WARNING_EN = (
    "Context usage is around 70%. Start trimming: prefer delegating large independent "
    "work to a subagent (task_tool), avoid loading more large files or long documents, "
    "and summarize completed parts while discarding intermediate details."
)
_CRITICAL_EN = (
    "Context usage is around 85%, near the limit. Compress now: delegate ALL large "
    "independent work to a subagent (do not run it in the main context), stop loading "
    "new content, and replace completed intermediate results with brief summaries."
)


def _validate_ratios(warning_ratio: float, critical_ratio: float) -> None:
    if not (0.0 < warning_ratio < critical_ratio < 1.0):
        raise ValueError(
            f"flash pressure: 非法水位阈值 warning={warning_ratio} critical={critical_ratio} "
            "——必须满足 0 < warning < critical < 1"
        )


class FlashPressureRail(DeepAgentRail):
    """Advisory context-pressure injector; warns the model below the 90% force layer."""

    # before_model_call 注入，独立于段排序；与 FlashToolGuideRail 同级取 3。
    priority = 3
    inherit_to_subagents = False

    def __init__(
        self,
        warning_ratio: float = _DEFAULT_WARNING_RATIO,
        critical_ratio: float = _DEFAULT_CRITICAL_RATIO,
    ) -> None:
        super().__init__()
        _validate_ratios(float(warning_ratio), float(critical_ratio))
        self._warning_ratio = float(warning_ratio)
        self._critical_ratio = float(critical_ratio)
        self._attachment_manager: Any = None
        self._system_prompt_builder: Any = None

    def init(self, agent: Any) -> None:
        self._attachment_manager = getattr(agent, "prompt_attachment_manager", None)
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._attachment_manager = None
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """按当前水位注入或清空压力提醒。"""
        text = self._resolve_pressure_text(ctx)
        if text is None:
            await self._clear(ctx)
            return
        await self._inject(ctx, text)

    def _resolve_pressure_text(self, ctx: AgentCallbackContext) -> str | None:
        """纯函数：按上一轮真实 usage / 真实模型窗口算出应注入的提醒正文；无则 None。"""
        limit = ContextOverflowRecoveryRail._resolve_model_context_limit(ctx)
        if limit <= 0:
            return None
        used = ContextOverflowRecoveryRail._last_prompt_tokens(ctx)
        if used <= 0:
            return None  # turn 1：无上一轮真实 usage
        rate = used / limit
        if rate < self._warning_ratio:
            return None
        critical = rate >= self._critical_ratio
        language = self._resolve_language()
        if critical:
            return _CRITICAL_CN if language == "cn" else _CRITICAL_EN
        return _WARNING_CN if language == "cn" else _WARNING_EN

    def _resolve_language(self) -> str:
        lang = getattr(self._system_prompt_builder, "language", None) or "cn"
        return "cn" if lang in ("cn", "zh") else "en"

    async def _inject(self, ctx: AgentCallbackContext, text: str) -> None:
        manager = self._attachment_manager
        if manager is None:
            return
        try:
            writer = manager.bind_context(ctx)
            await writer.add_section(
                _ATTACHMENT_SECTION,
                text,
                _KIND,
                _SOURCE,
                priority=_ATTACHMENT_PRIORITY,
                content_kind="text/markdown",
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[FlashPressureRail] skip pressure injection: %s", exc)

    async def _clear(self, ctx: AgentCallbackContext) -> None:
        """水位回落时清空压力提醒（附件为 session 粘滞存储，需显式清除）。"""
        manager = self._attachment_manager
        if manager is None:
            return
        try:
            writer = manager.bind_context(ctx)
            await writer.clear_section(_ATTACHMENT_SECTION)
        except (ValueError, TypeError):
            logger.debug("[FlashPressureRail] clear pressure reminder skipped")


__all__ = ["FlashPressureRail", "_validate_ratios"]
