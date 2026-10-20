# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.prompts import resolve_language

from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import mcp_fetch_webpage, mcp_fetch_webpage_impl
from jiuwenswarm.agents.harness.common.tools.web_search.harness import JiuwenHarnessWebSearchTool
from jiuwenswarm.common.utils import logger


def _build_fetch_webpage_tool_card(*, agent_id: Optional[str], language: str) -> ToolCard:
    """Build a harness ``fetch_webpage`` card from ``mcp_fetch_webpage`` metadata."""
    _ = language
    base = mcp_fetch_webpage.card
    card_id = "JiuwenHarnessFetch"
    if agent_id:
        card_id = f"{card_id}:{agent_id}"
    return ToolCard(
        id=card_id,
        name="fetch_webpage",
        description=base.description or "Fetch webpage text content.",
        input_params=dict(base.input_params or {}),
    )


def _override_fetch_card_url_array(base_card: ToolCard, lang: str) -> ToolCard:
    """Override the harness fetch_webpage card so ``url`` is an array.

    The upstream ``FetchWebpageMetadataProvider`` (openjiuwen package) still
    declares ``url`` as a string. We rebuild the card with an array schema and
    a multi-URL description so the LLM is told to pass several URLs at once.
    """
    ud = {
        "cn": "一个或多个要抓取的网页 URL。",
        "en": "One or more webpage URLs to fetch.",
    }[lang]
    description = {
        "cn": (
            "抓取网页文本，返回列表，每个元素含一个 URL 的状态码、标题和正文。"
            "可一次传入多个 URL 并行抓取。通常配合 web_search 使用：先搜索，"
            "再抓取前几个结果页。可设置 max_chars=0 关闭截断，"
            "也可调大 timeout_seconds 处理慢站点。"
        ),
        "en": (
            "Fetch webpage text and return a list; each item holds status, title and plain "
            "text of one URL. Accepts multiple URLs at once for parallel fetch. "
            "Usually used after web_search. Set max_chars=0 to disable clipping "
            "and use a larger timeout_seconds for slow pages."
        ),
    }[lang]
    input_params = {
        "type": "object",
        "properties": {
            "url": {"type": "array", "items": {"type": "string"}, "description": ud},
            "max_chars": {"type": "integer", "description": "返回内容最大字符数；设置为 0 表示不截断。", "default": 20000},
            "timeout_seconds": {"type": "integer", "description": "请求超时时间（秒）；慢站点可适当调大。", "default": 45},
            "use_cache": {
                "type": "boolean",
                "description": "是否优先从内存缓存读取（默认 true）。若需要最新数据，设为 false 从原站重新抓取。",
                "default": True,
            },
        },
        "required": ["url"],
    }
    return ToolCard(
        id=base_card.id,
        name=base_card.name,
        description=description,
        input_params=input_params,
    )


class JiuwenHarnessFetchWebpageTool(Tool):
    """Fetch webpage; body from ``mcp_fetch_webpage``."""

    def __init__(
        self,
        language: str = "cn",
        agent_id: Optional[str] = None,
        card: Optional[ToolCard] = None,
        cache: Any | None = None,
    ) -> None:
        lang = resolve_language(language or "cn")
        base_card = _build_fetch_webpage_tool_card(agent_id=agent_id, language=lang)
        fetch_card = card or _override_fetch_card_url_array(base_card, lang)
        super().__init__(fetch_card)
        self._cache = cache

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.info(
            "[JiuwenHarnessWebTools] LLM tool name=fetch_webpage (card_id=%s)",
            self.card.id,
        )
        # The auto-extracted LLM schema leaks the DI ``cache`` param from
        # ``mcp_fetch_webpage_impl``; drop it from LLM inputs before
        # injecting the harness cache, or both land in the call as
        # ``cache`` and the invoke fails with a duplicate keyword error.
        payload = {k: v for k, v in inputs.items() if k != "cache"}
        if self._cache is not None:
            kwargs["cache"] = self._cache
        return await mcp_fetch_webpage_impl(
            **payload, **{k: v for k, v in kwargs.items() if k in ("cache",)}
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        yield "Stream is not supported for this tool."
        raise build_error(StatusCode.TOOL_STREAM_NOT_SUPPORTED, card=self._card)


def build_jiuwen_harness_named_web_tools(
    *,
    agent_id: Optional[str],
    language: str = "cn",
    cache: Any | None = None,
) -> List[Tool]:
    """Build ``web_search`` + ``fetch_webpage`` for a scoped ``agent_id``."""
    lang = resolve_language(language or "cn")
    return [
        JiuwenHarnessWebSearchTool(language=lang, agent_id=agent_id, cache=cache),
        JiuwenHarnessFetchWebpageTool(language=lang, agent_id=agent_id, cache=cache),
    ]


__all__ = [
    "JiuwenHarnessFetchWebpageTool",
    "JiuwenHarnessWebSearchTool",
    "build_jiuwen_harness_named_web_tools",
]
