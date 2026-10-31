# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Multimodal image input adaptation for Core model calls."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    ensure_multimodal_image_filter_window_mutator,
    ensure_multimodal_image_window_mutator,
)
from jiuwenswarm.common.utils import logger


class MultimodalImageRail(DeepAgentRail):
    """Prepare image attachments for native multimodal model input."""

    priority = 75

    def __init__(
        self,
        enable_image_multimodal: bool | None = None,
        *,
        max_total_bytes: int = 0,
        max_visual_tokens: int = 0,
        max_count: int = 0,
    ) -> None:
        super().__init__()
        self._enable_image_multimodal = enable_image_multimodal
        self._max_total_bytes = max(0, int(max_total_bytes or 0))
        self._max_visual_tokens = max(0, int(max_visual_tokens or 0))
        self._max_count = max(0, int(max_count or 0))
        self._deep_agent: Any | None = None
        self._system_prompt_builder: Any | None = None

    def init(self, agent: Any) -> None:
        self._deep_agent = agent
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def _read_image_multimodal_enabled(self) -> bool:
        if self._enable_image_multimodal is not None:
            return self._enable_image_multimodal
        deep_config = (
            getattr(self._deep_agent, "deep_config", None)
            or getattr(self._deep_agent, "_deep_config", None)
        )
        return bool(getattr(deep_config, "enable_read_image_multimodal", False))

    def _sync_image_policy_section(self, enabled: bool) -> None:
        builder = self._system_prompt_builder
        if builder is None:
            return
        section_name = "multimodal_image_policy"
        try:
            builder.remove_section(section_name)
        except Exception:
            pass
        native_cn = (
            "本次请求可能包含用户上传的图片。当前模型支持原生图片理解，"
            "图片已作为图像内容直接提供；请直接使用你的视觉能力查看图片，"
            "不要调用 visual_question_answering 或 image_reading 工具。"
        )
        native_en = (
            "This request may contain user-uploaded images. The current model supports "
            "native image understanding and the images are provided as image content. "
            "Use your vision capability directly; do not call visual_question_answering "
            "or image_reading tools."
        )
        fallback_cn = (
            "本次请求可能包含用户上传的图片。当前模型不支持原生图片理解；"
            "如需理解图片内容，请调用 visual_question_answering 或 image_reading 工具。"
        )
        fallback_en = (
            "This request may contain user-uploaded images. The current model does not "
            "support native image understanding; to understand image content, call the "
            "visual_question_answering or image_reading tool."
        )
        builder.add_section(PromptSection(
            name=section_name,
            content={"cn": native_cn if enabled else fallback_cn,
                     "en": native_en if enabled else fallback_en},
            priority=96,
        ))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        enabled = self._read_image_multimodal_enabled()
        self._sync_image_policy_section(enabled)
        logger.info(
            "[MultimodalImageRail] before_model_call ctx_present=%s context_present=%s enabled=%s",
            ctx is not None,
            ctx is not None and ctx.context is not None,
            enabled,
        )
        if ctx.context is None:
            logger.info("[MultimodalImageRail] skip install: ctx.context is None")
            return
        if enabled:
            installed = ensure_multimodal_image_window_mutator(
                ctx.context,
                max_total_bytes=self._max_total_bytes,
                max_visual_tokens=self._max_visual_tokens,
                max_count=self._max_count,
            )
            logger.info("[MultimodalImageRail] inject mutator installed=%s", installed)
            if installed:
                logger.info(
                    "Installed multimodal image materialize window mutator "
                    "(writes pixels into history, budget_bytes=%s budget_visual_tokens=%s budget_count=%s)",
                    self._max_total_bytes,
                    self._max_visual_tokens,
                    self._max_count,
                )
            return

        installed = ensure_multimodal_image_filter_window_mutator(ctx.context)
        logger.info("[MultimodalImageRail] filter mutator installed=%s", installed)
        if installed:
            logger.info(
                "Installed multimodal image filter window mutator (view-level)"
            )
