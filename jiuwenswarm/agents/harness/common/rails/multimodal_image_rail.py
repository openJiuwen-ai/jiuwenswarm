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
from jiuwenswarm.common.model_capabilities import ImageCapabilityPolicy


class MultimodalImageRail(DeepAgentRail):
    """Prepare image attachments for native multimodal model input."""

    priority = 75

    def __init__(self, enable_image_multimodal: bool | None = None) -> None:
        super().__init__()
        self._enable_image_multimodal = enable_image_multimodal
        self._deep_agent: Any | None = None
        self._image_capability_policy: ImageCapabilityPolicy | None = None

    def clone_for_subagent(self):
        cloned = type(self)(enable_image_multimodal=self._enable_image_multimodal)
        policy = self._image_capability_policy
        if policy is not None:
            cloned.configure_image_capabilities(policy.request_source, policy.config_source)
        return cloned

    def configure_image_capabilities(self, request_source, config_source=None) -> None:
        if self._image_capability_policy is None:
            self._image_capability_policy = ImageCapabilityPolicy(request_source, config_source)
        else:
            self._image_capability_policy.request_source = request_source
            if config_source is not None:
                self._image_capability_policy.config_source = config_source

    def _sync_image_capabilities(self) -> None:
        if self._image_capability_policy is not None:
            self._image_capability_policy.apply(self._deep_agent)

    def init(self, agent: Any) -> None:
        self._deep_agent = agent
        self._sync_image_capabilities()

    def _read_image_multimodal_enabled(self) -> bool:
        declared = getattr(self._deep_agent, "_jiuwenswarm_declared_image_support", None)
        if isinstance(declared, bool):
            return declared
        if self._enable_image_multimodal is not None:
            return self._enable_image_multimodal
        deep_config = (
            getattr(self._deep_agent, "deep_config", None)
            or getattr(self._deep_agent, "_deep_config", None)
        )
        return bool(getattr(deep_config, "enable_read_image_multimodal", False))

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        self._sync_image_capabilities()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        self._sync_image_capabilities()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._sync_image_capabilities()
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

        ensure_multimodal_image_window_mutator(ctx.context, [])
        removed = strip_image_content_from_model_context(ctx.context)
        if removed:
            logger.info(
                "Removed %d image content block(s) from chat-model context",
                removed,
            )
