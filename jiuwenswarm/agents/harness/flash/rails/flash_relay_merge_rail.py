# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRelayMergeRail —— relay 注入内容的 flash 侧去重归并 + 静态末位收口（§七 relay 涉点）。

relay TS 层（RelayClawAgentService.ts buildRequest）把产品内容包经 ``system_prompt``
字段注入引擎，RuntimePromptRail 以 priority 110 装进 ``request_system_prompt`` 段。
本 rail 每轮**无条件**把该段 remove + 重加到静态末位 **999**（`_PRIO_REQUEST`），
一石二鸟：

- **撞号解除**：110 段位回归 refusal_behavior 独占（request_system_prompt 从 110
  挪走），消除此前两个段同号拼进最终提示词的问题；
- **位置收口**：relay 产品包恒在静态段（0xx–7xx）之后、动态段（1000+）之前渲染。

同时剥除段内与 flash 域 rail 重复的部分：

- ``[文件输出约束]``（220 已接管）——按锚点剥除；
- ``[记忆纠正规则]`` 的第 6 条（删除=回收站）——120 已完整覆盖且含大文件回退例外，删 relay 简版；
- ``[记忆纠正规则]`` 里的旧记忆工具名（read_memory / edit_memory / write_memory）——按
  ``prompt.content.memory_names.MEMORY_NAME_REPLACEMENTS``（与 FlashMemoryRail 共用）改写为
  统一入口 ``memory(mode=…)``。

剥除机制：``[文件输出约束]`` 按锚点定位，锚点缺失即**整体回退**（用原内容重加），不盲切；
其余改写均为独立锚点的 best-effort，锚点缺失只跳过该项、不影响其它项（带守卫原则）。

安全性说明：RuntimePromptRail 每轮 ``before_model_call`` 都重新注入
``request_system_prompt``（``add_section`` 按名覆盖于 110），故本 rail **不设幂等
flag**，每轮重新收口偏移到 999——与 FlashSafetyRail 每轮重拆同构。

**本期不做**：``[Personality Override]``（人格）段——010 人格需 flash.persona.override
槽位 + 1030 动态填充，延期；本 rail 只剥文件输出约束一块，不碰人格段。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.content.memory_names import (
    MEMORY_NAME_REPLACEMENTS,
)
from jiuwenswarm.agents.harness.flash.prompt.content.relay import (
    _PRIO_REQUEST,
    _SECTION_REQUEST,
    _rewrite_memory_tool_names,
    _strip_memory_rule6,
    _strip_relay_duplicates,
)

logger = logging.getLogger(__name__)


class FlashRelayMergeRail(DeepAgentRail):
    """剥除 relay 注入的重复块（220 / 120 的 relay 侧收口）。"""

    # request_system_prompt 由 RuntimePromptRail（priority 5）每轮注入；本 rail 需在
    # 其后每轮重新剥除，故取 < 5 的优先级。与 FlashToolGuideRail / FlashPressureRail
    # 同取 3（域 rail=4 之后、audit=2 之前），不依赖任何域 rail 的输出。
    priority = 3
    inherit_to_subagents = False

    def __init__(self, config_provider: Any = None) -> None:
        super().__init__()
        self._config_provider = config_provider
        self._system_prompt_builder: Any = None
        self._warned_guard_miss = False

    def _enabled_by_config(self) -> bool:
        """每轮现读 flash.relay_merge.enabled（热重载同步）。provider 缺省时默认启用。"""
        provider = self._config_provider
        if provider is None:
            return True
        try:
            config = provider()
        except Exception:
            return True
        return bool(config.get("enabled", True)) if isinstance(config, dict) else True

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        if builder is None:
            return
        self._system_prompt_builder = builder
        if not self._enabled_by_config():
            # 禁用：不再把 request_system_prompt 从 110 挪到 999，保留原生 110 段。
            return
        self._maybe_strip_duplicates(builder)

    def _maybe_strip_duplicates(self, builder: Any) -> None:
        """每轮收口 request_system_prompt -> 静态末位（_PRIO_REQUEST=999）并剥重复块。

        RuntimePromptRail 每轮以 110 重新注入 request_system_prompt，本方法每轮
        **无条件** remove + 重加到 999：保证它恒在静态段（0xx–7xx）之后、动态段
        （1000+）之前渲染，同时解除与 refusal_behavior@110 的撞号，且不设幂等 flag
        （RuntimePromptRail 每轮重注入，本 rail 每轮重收口）。

        剥除守卫语义保留：锚点缺失/乱序（_strip_relay_duplicates 返回 None）时用
        原内容重加、不盲切。
        """
        section = self._get_section(builder, _SECTION_REQUEST)
        if section is None:
            return

        content = self._extract_content(section, builder)
        stripped = _strip_relay_duplicates(content)
        if stripped is None:
            # 命中守卫（锚点缺失）——用原 content 重加，不盲切。
            stripped = content
            if not self._warned_guard_miss:
                self._warned_guard_miss = True
                logger.warning(
                    "[FlashRelayMergeRail] relay 去重剥除命中守卫（锚点缺失），原样重加到段号 %s",
                    _PRIO_REQUEST,
                )

        # 以下两步各自独立锚点、best-effort：某一项没命中只跳过该项，不影响其它项。
        # 删除=回收站：120 已覆盖（含大文件回退例外），删 relay 简版。
        stripped, _ = _strip_memory_rule6(stripped)
        # 旧记忆工具名 → 统一入口（映射表与 FlashMemoryRail 共用同一常量）。
        stripped, _ = _rewrite_memory_tool_names(stripped, MEMORY_NAME_REPLACEMENTS)

        builder.remove_section(_SECTION_REQUEST)
        builder.add_section(PromptSection(
            name=_SECTION_REQUEST,
            content={"cn": stripped, "en": stripped},
            priority=_PRIO_REQUEST,
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


__all__ = ["FlashRelayMergeRail", "_strip_relay_duplicates"]
