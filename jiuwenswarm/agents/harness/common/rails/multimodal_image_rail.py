# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Multimodal image input adaptation for Core model calls."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    current_multimodal_image_files,
    ensure_multimodal_image_window_mutator,
    strip_image_content_from_model_context,
)
from jiuwenswarm.common.utils import logger


class MultimodalImageRail(DeepAgentRail):
    """Prepare image attachments for native multimodal model input."""

    priority = 75

    def __init__(self, enable_image_multimodal: bool | None = None) -> None:
        super().__init__()
        self._enable_image_multimodal = enable_image_multimodal
        self._deep_agent: Any | None = None

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    def _read_image_multimodal_enabled(self) -> bool:
        if self._enable_image_multimodal is not None:
            return self._enable_image_multimodal
        deep_config = (
            getattr(self._deep_agent, "deep_config", None)
            or getattr(self._deep_agent, "_deep_config", None)
        )
        return bool(getattr(deep_config, "enable_read_image_multimodal", False))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.context is None:
            return
        if self._read_image_multimodal_enabled():
            image_files = current_multimodal_image_files()
            installed = ensure_multimodal_image_window_mutator(
                ctx.context,
                image_files,
            )
            if installed:
                logger.info(
                    "Installed multimodal image context-window adapter for %d image attachment(s)",
                    len(image_files),
                )
            return

        removed = strip_image_content_from_model_context(ctx.context)
        if removed:
            logger.info(
                "Removed %d image content block(s) from chat-model context",
                removed,
            )
