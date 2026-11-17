# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashStateRail —— ③ 运行时 820/830/880 收口（M2 ⑤）。

在 ``RuntimePromptRail``（priority=5）之后执行，把散落在静态系统提示词里的运行事实
挪到 PromptAttachment 通道，保持静态区（0xx–7xx，priority < 800）hash 会话内稳定。

本期只做 820（时间事实迁 attachment，每轮 upsert 刷新）：
- 830 会话事实（``runtime.setting``）已由 RuntimePromptRail 走 attachment（priority 95），
  flash 侧无需重复注入，本 rail 不碰；
- 880 动态规则与 020 存疑裁决（外部门禁/审批策略）耦合，延期到 M4 与存疑 7 段一起定稿。

执行顺序依赖：``RuntimePromptRail`` 每轮重建静态 ``time`` 段（priority 130），本 rail
必须在其后运行（priority < 5）才能把该段撤下并转成 attachment。rail 执行序 = 类自身
priority（大者先，见 framework.py:459 ``sort(reverse=True)``），故本 rail priority=4：
5（RuntimePromptRail）→ 4（本 rail）→ 2（FlashPromptAuditRail），撤段发生在审计之前，
审计看到的静态区不再含 ``time``，静态 hash 稳定。

「如何读运行时附件」前置说明段（01 §9 待定 #6 / 05 §七 8xx 前置）属内容设计项，
单独设计，本 rail 不代拟正文。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

# 静态 time 段名（RuntimePromptRail 注入的 section name）。
_SECTION_TIME = "time"

# 转 attachment 后的键名：与 830 的 "runtime.setting" 对齐，用 "runtime.time" 表明来源通道。
_ATTACHMENT_SECTION = "runtime.time"
_SOURCE = "jiuwenswarm.flash.state_rail"
_KIND = PromptAttachmentKind.RUNTIME
# 与 830（runtime.setting = 95）同属运行事实附件块，排在其前一格；附件块内排序键，
# 与静态段 priority 是两个独立命名空间，不撞号。
_ATTACHMENT_PRIORITY = 94


class FlashStateRail(DeepAgentRail):
    """收口 ③ 运行时运行事实到 attachment（本期：820 时间事实）。"""

    # 必须晚于 RuntimePromptRail(5) 运行，早于 FlashPromptAuditRail(2)。
    priority = 4
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        self._attachment_manager: Any = None

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._attachment_manager = getattr(agent, "prompt_attachment_manager", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None
        self._attachment_manager = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        if builder is None:
            return
        self._system_prompt_builder = builder

        # 820 时间事实：撤下静态段，转 attachment。
        await self._move_time_to_attachment(ctx, builder)

    async def _move_time_to_attachment(self, ctx: AgentCallbackContext, builder: Any) -> None:
        time_section = self._get_section(builder, _SECTION_TIME)
        if time_section is None:
            # 本 rail 之前已撤段（重复调用），或 RuntimePromptRail 未注入（非 flash 装配）。
            return

        content = self._extract_content(time_section, builder)
        manager = self._attachment_manager
        if manager is None:
            # 附件通道不可用：保留静态 time 段（不撤），避免运行事实静默丢失。
            logger.warning(
                "[FlashStateRail] prompt attachment manager unavailable; "
                "保留静态 time 段（不转 attachment），请检查装配"
            )
            return
        try:
            writer = manager.bind_context(ctx)
            await writer.add_section(
                _ATTACHMENT_SECTION,
                content,
                _KIND,
                _SOURCE,
                priority=_ATTACHMENT_PRIORITY,
                content_kind="text/markdown",
            )
        except (ValueError, TypeError) as exc:
            # 写入失败：同样保留静态 time 段，避免事实丢失。
            logger.warning(
                "[FlashStateRail] skip time attachment（保留静态 time 段）: %s", exc
            )
            return
        # 附件写入成功后才撤下静态 time 段（先写后删）。若 add_section 抛出未在
        # 上方捕获的异常，这里不会执行，静态 time 段同样保留，fail-safe。
        builder.remove_section(_SECTION_TIME)

    @staticmethod
    def _get_section(builder: Any, name: str) -> Any | None:
        get_section = getattr(builder, "get_section", None)
        if not callable(get_section):
            return None
        try:
            return get_section(name)
        except Exception:  # noqa: BLE001 — 拿不到就按「没有」处理，静默回退
            return None

    @staticmethod
    def _extract_content(time_section: Any, builder: Any) -> str:
        """按当前语言取 time 段正文；缺语言键时退回首个非空值。"""
        content = getattr(time_section, "content", None)
        if isinstance(content, dict):
            language = getattr(builder, "language", None) or "cn"
            language = "cn" if language in ("cn", "zh") else "en"
            value = content.get(language)
            if not value:
                value = next((v for v in content.values() if v), "")
            return str(value)
        return str(content or "")


__all__ = ["FlashStateRail"]
