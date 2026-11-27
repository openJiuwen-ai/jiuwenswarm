# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuClawContextEngineeringRail — Extend CE Rail with offload/context hints.

Subclasses the SDK ContextEngineeringRail to inject an independent 'offload'
PromptSection, replacing the old approach of appending to the 'context' section.

F-REDUCE: Added `minimal` mode for subagent — skips tools/context injection,
only keeps context compression processors and offload hint.
"""
from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.context_engineering_rail import ContextEngineeringRail


class JiuClawContextEngineeringRail(ContextEngineeringRail):
    """扩展 CE Rail，注入独立的 offload/上下文压缩 section。

    Args:
        processors: 透传给父类的用户级 processor 配置（增量合并/替换预置）。
        preset: 是否启用预置的上下文压缩 processor 配置。
        minimal: F-REDUCE — 是否跳过 tools/context section 注入（用于 subagent）。
        session_memory: SessionMemoryConfig 配置（传递给父类）。
    """

    OFFLOAD_HINT_CN = (
        "# 上下文压缩\n\n"
        "你的上下文在过长时会被自动压缩，"
        "并标记为[OFFLOAD: handle=<id>, type=<type>]。\n\n"
        "如果你认为需要读取隐藏的内容，"
        "可随时调用reload_original_context_messages工具。\n\n"
        "请勿猜测或编造缺失的内容。\n\n"
        '存储类型："in_memory"（会话缓存）'
    )

    OFFLOAD_HINT_EN = (
        "# Context Compression\n\n"
        "Your context will be automatically compressed when it becomes too long "
        "and marked with [OFFLOAD: handle=<id>, type=<type>].\n\n"
        'Call reload_original_context_messages(offload_handle="<id>", '
        'offload_type="<type>"), using the exact values from the marker.\n\n'
        "Do not guess or fabricate missing content.\n\n"
        'Storage types: "in_memory" (session cache)'
    )

    def __init__(
        self,
        processors=None,
        preset: bool = True,
        minimal: bool = False,
        session_memory=None,
    ) -> None:
        """Initialize with optional minimal mode for subagent.

        Args:
            processors: 透传给父类的用户级 processor 配置（增量合并/替换预置）。
            preset: Enable preset context compression processors.
            minimal: Skip tools/context section injection (for subagent).
            session_memory: SessionMemoryConfig for session memory (passed to parent).
        """
        super().__init__(
            processors=processors,
            preset=preset,
            session_memory=session_memory,
        )
        self._minimal = minimal

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Inject workspace + context (if not minimal), then offload section."""
        # F-REDUCE: minimal mode skips parent's tools/context injection
        if not self._minimal:
            await super().before_model_call(ctx)

        if not self.system_prompt_builder:
            return

        lang = self.system_prompt_builder.language or "cn"
        hint = self.OFFLOAD_HINT_CN if lang == "cn" else self.OFFLOAD_HINT_EN

        self.system_prompt_builder.add_section(
            PromptSection(
                name="offload",
                content={lang: hint},
                priority=90,
            )
        )
