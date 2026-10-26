# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.prompts import resolve_language

from jiuwenswarm.agents.harness.common.tools.web_search.tool import (
    build_web_search_tool_card,
    web_search_impl,
)
from jiuwenswarm.common.utils import logger


class JiuwenHarnessWebSearchTool(Tool):
    """Unified web search for harness registration."""

    def __init__(
        self,
        language: str = "cn",
        agent_id: Optional[str] = None,
        card: Optional[ToolCard] = None,
        cache: Any | None = None,
    ) -> None:
        lang = resolve_language(language or "cn")
        super().__init__(
            card
            or build_web_search_tool_card(agent_id=agent_id, language=lang),
        )
        self._cache = cache

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.debug(
            "[JiuwenHarnessWebTools] web_search card_id=%s",
            self.card.id,
        )
        if self._cache is not None:
            kwargs["cache"] = self._cache
        return await web_search_impl(**inputs, **{k: v for k, v in kwargs.items() if k in ("cache",)})

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


__all__ = ["JiuwenHarnessWebSearchTool"]
