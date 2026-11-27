# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.prompts.sections.tools import build_tool_card

from jiuwenclaw.agentserver.tools.search_tools import mcp_free_search
from jiuwenclaw.agentserver.tools.web_fetch_tools import mcp_fetch_webpage
from jiuwenclaw.utils import logger


class JiuwenHarnessFreeSearchTool(Tool):
    """Free search; same contract as harness ``WebFreeSearchTool``, body from ``mcp_free_search``."""

    def __init__(
        self,
        language: str = "cn",
        agent_id: Optional[str] = None,
        card: Optional[ToolCard] = None,
    ) -> None:
        lang = resolve_language(language or "cn")
        super().__init__(
            card
            or build_tool_card(
                "free_search",
                "JiuwenHarnessFreeSearch",
                lang,
                agent_id=agent_id,
            )
        )

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.info(
            "[JiuwenHarnessWebTools] LLM tool name=free_search -> actual impl="
            "jiuwenclaw.agentserver.tools.search_tools.mcp_free_search "
            "(card_id=%s)",
            self.card.id,
        )
        return await mcp_free_search.invoke(inputs, **kwargs)

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


class JiuwenHarnessFetchWebpageTool(Tool):
    """Fetch webpage; same contract as harness ``WebFetchWebpageTool``, body from ``mcp_fetch_webpage``."""

    def __init__(
        self,
        language: str = "cn",
        agent_id: Optional[str] = None,
        card: Optional[ToolCard] = None,
    ) -> None:
        lang = resolve_language(language or "cn")
        super().__init__(
            card
            or build_tool_card(
                "fetch_webpage",
                "JiuwenHarnessFetch",
                lang,
                agent_id=agent_id,
            )
        )

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.info(
            "[JiuwenHarnessWebTools] LLM tool name=fetch_webpage -> actual impl="
            "jiuwenclaw.agentserver.tools.web_fetch_tools.mcp_fetch_webpage "
            "(card_id=%s)",
            self.card.id,
        )
        return await mcp_fetch_webpage.invoke(inputs, **kwargs)

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


def build_jiuwen_harness_named_web_tools(
    *,
    agent_id: Optional[str],
    language: str = "cn",
) -> List[Tool]:
    """Build ``free_search`` + ``fetch_webpage`` tools for a given scoped ``agent_id`` (unique per main/sub agent)."""
    lang = resolve_language(language or "cn")
    return [
        JiuwenHarnessFreeSearchTool(language=lang, agent_id=agent_id),
        JiuwenHarnessFetchWebpageTool(language=lang, agent_id=agent_id),
    ]


__all__ = [
    "JiuwenHarnessFreeSearchTool",
    "JiuwenHarnessFetchWebpageTool",
    "build_jiuwen_harness_named_web_tools",
]
