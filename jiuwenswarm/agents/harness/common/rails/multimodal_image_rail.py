# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Multimodal image input adaptation for Core model calls."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails._multimodal import should_enable_read_image_multimodal

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
        self._turn_image_files: list[dict[str, Any]] | None = None
        self._turn_binding_token: Any = None

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    def bind_turn_image_files(self, image_files: list[dict[str, Any]]) -> Any:
        self._turn_image_files = list(image_files or [])
        self._turn_binding_token = token = object()
        return token

    def restore_turn_image_files(self, token: Any) -> None:
        if token is None or token is not self._turn_binding_token:
            return
        self._turn_binding_token = None
        self._turn_image_files = None

    def _current_turn_image_files(self) -> list[dict[str, Any]]:
        if self._turn_image_files is not None:
            return self._turn_image_files
        return current_multimodal_image_files()

    def _read_image_multimodal_enabled(self) -> bool:
        if self._enable_image_multimodal is not None:
            return self._enable_image_multimodal
        return should_enable_read_image_multimodal(self._deep_agent)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.context is None:
            return
        if self._read_image_multimodal_enabled():
            image_files = self._current_turn_image_files()
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
