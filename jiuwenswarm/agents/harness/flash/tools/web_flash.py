# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash-only web tool combining search and webpage fetch behind one card."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
)
from jiuwenswarm.agents.harness.common.tools.web_search.harness import (
    JiuwenHarnessWebSearchTool,
)


def _description(language: str) -> str:
    if language == "en":
        return (
            "Web tool for Flash mode. Fill exactly one action object: search returns "
            "URLs and snippets; fetch retrieves the full text of one or more webpages. "
            "Usually search first, then fetch relevant result pages."
        )
    return (
        "Flash 模式联网工具。只填一个操作对象：search 返回 URL 和摘要；"
        "fetch 抓取一个或多个网页的正文。通常先搜索，再抓取相关结果页。"
    )


def _input_params(language: str) -> dict[str, Any]:
    if language == "en":
        search_desc = "Search the web. The system selects the available provider."
        query_desc = "Search query."
        fetch_desc = "Fetch full text from one or more webpage URLs."
        urls_desc = "One or more webpage URLs."
    else:
        search_desc = "联网搜索；可用搜索服务由系统自动选择。"
        query_desc = "搜索查询。查询当前信息时应使用当前年份或日期。"
        fetch_desc = "抓取一个或多个网页的正文。"
        urls_desc = "一个或多个网页 URL。"

    search = {
        "type": "object",
        "properties": {
            "search": {
                "type": "object",
                "description": search_desc,
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": query_desc},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        "required": ["search"],
        "additionalProperties": False,
    }
    fetch = {
        "type": "object",
        "properties": {
            "fetch": {
                "type": "object",
                "description": fetch_desc,
                "properties": {
                    "url": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "description": urls_desc,
                    },
                    "max_chars": {"type": "integer", "minimum": 0, "default": 20000},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "default": 45},
                    "use_cache": {"type": "boolean", "default": True},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
        "required": ["fetch"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": _description(language),
        "properties": {
            **search["properties"],
            **fetch["properties"],
        },
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    }


def build_web_flash_tool(
    *,
    agent_id: str | None,
    language: str = "cn",
    cache: Any | None = None,
) -> LocalFunction:
    """Build the Flash-only card while reusing the mainline web backends."""
    search_tool = JiuwenHarnessWebSearchTool(
        language=language,
        agent_id=agent_id,
        cache=cache,
    )
    fetch_tool = JiuwenHarnessFetchWebpageTool(
        language=language,
        agent_id=agent_id,
        cache=cache,
    )

    async def _invoke(**kwargs: Any) -> Any:
        actions = [
            name
            for name in ("search", "fetch")
            if isinstance(kwargs.get(name), dict)
        ]
        if len(actions) != 1:
            raise ValueError("fill exactly one web action object: search or fetch")
        action = actions[0]
        inputs = kwargs.get(action)
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError(f"{action} must be a non-empty object")
        if action == "search":
            return await search_tool.invoke(inputs)
        return await fetch_tool.invoke(inputs)

    suffix = (agent_id or "merged").strip() or "merged"
    card = ToolCard(
        id=f"web_flash_{suffix}",
        name="web_flash",
        description=_description(language),
        input_params=_input_params(language),
    )
    return LocalFunction(card=card, func=_invoke)


__all__ = ["build_web_flash_tool"]
