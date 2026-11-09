# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash-only web tool combining search and webpage fetch behind one card."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
)
from jiuwenswarm.agents.harness.common.tools.web_search.harness import (
    JiuwenHarnessWebSearchTool,
)
from jiuwenswarm.agents.harness.common.tools.web_search.constants import (
    KNOWN_PAID_PROVIDERS,
)
from jiuwenswarm.agents.harness.common.tools.web_search.image_search import (
    search_images,
)


def _description(language: str) -> str:
    if language == "en":
        return (
            "Web tool for Flash mode. Fill exactly one action object: search returns "
            "URLs and snippets; fetch retrieves the full text of one or more webpages; "
            "images searches the web and downloads candidate images to local paths for "
            "image/PPT tools. "
            "Usually search first, then fetch relevant result pages, and call images "
            "when the task needs real pictures."
        )
    return (
        "Flash 模式联网工具。只填一个操作对象：search 返回 URL 和摘要；"
        "fetch 抓取一个或多个网页的正文；images 联网检索并把配图下载到本地，"
        "返回本地路径供图片工具与 PPT 配图使用。"
        "通常先搜索，再抓取相关结果页；需要真实图片时调用 images。"
        "用户明确指定搜索引擎时，请在 search 中填写 search_source。"
    )


def _input_params(language: str) -> dict[str, Any]:
    if language == "en":
        search_desc = (
            "Search the web. Set search_source when the user explicitly requests "
            "a specific search engine; otherwise omit it for automatic selection."
        )
        query_desc = "Search query."
        search_source_desc = (
            "Optional explicit search engine, e.g. bocha, petal, tavily. "
            "Omit when the user does not request a specific engine."
        )
        fetch_desc = "Fetch full text from one or more webpage URLs."
        urls_desc = "One or more webpage URLs."
        images_desc = (
            "Search the web for images and download the best candidates to local paths "
            "for downstream tools."
        )
        images_query_desc = "What kind of image to find, e.g. 'Huawei campus signing ceremony'."
    else:
        search_desc = (
            "联网搜索；用户明确指定搜索引擎时填写 search_source，"
            "未指定时省略并由系统自动选择。"
        )
        query_desc = "搜索查询。查询当前信息时应使用当前年份或日期。"
        search_source_desc = (
            "可选。用户明确指定的搜索引擎，如 bocha、petal、tavily；"
            "未指定时省略，由系统自动选择。"
        )
        fetch_desc = "抓取一个或多个网页的正文。"
        urls_desc = "一个或多个网页 URL。"
        images_desc = (
            "联网检索图片，把最合适的候选图下载到本地；"
            "返回本地文件路径，供图片理解与 PPT 配图等后续工具使用。"
        )
        images_query_desc = "要找什么图，例如「华为东南大学 签约 现场」。"

    search = {
        "type": "object",
        "properties": {
            "search": {
                "type": "object",
                "description": search_desc,
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": query_desc,
                    },
                    "search_source": {
                        "type": "string",
                        "enum": sorted(KNOWN_PAID_PROVIDERS),
                        "description": search_source_desc,
                    },
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
    images = {
        "type": "object",
        "properties": {
            "images": {
                "type": "object",
                "description": images_desc,
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": images_query_desc},
                    "max_images": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "default": 6,
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "default": 3,
                    },
                    "search_mode": {
                        "type": "string",
                        "enum": ["default", "paid", "free"],
                    },
                    "output_dir": {
                        "type": "string",
                        "description": (
                            "保存目录（可选）。传入已有的工作目录时，图片会直接落到该目录；"
                            "不传则使用默认的检索图片目录。"
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        "required": ["images"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": _description(language),
        "properties": {
            **search["properties"],
            **fetch["properties"],
            **images["properties"],
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
            for name in ("search", "fetch", "images")
            if isinstance(kwargs.get(name), dict)
        ]
        if len(actions) != 1:
            raise ValueError("fill exactly one web action object: search, fetch or images")
        action = actions[0]
        inputs = kwargs.get(action)
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError(f"{action} must be a non-empty object")
        if action == "search":
            return await search_tool.invoke(inputs)
        if action == "fetch":
            return await fetch_tool.invoke(inputs)
        return await _invoke_images(inputs, cache=cache)

    async def _invoke_images(inputs: dict[str, Any], *, cache: Any | None) -> ToolOutput:
        outcome = await search_images(
            str(inputs.get("query") or "").strip(),
            max_images=inputs.get("max_images"),
            max_pages=inputs.get("max_pages"),
            search_mode=str(inputs.get("search_mode") or "default"),
            cache=cache,
            output_dir=inputs.get("output_dir"),
        )
        payload = outcome.as_dict()
        data = {
            "content": outcome.to_content(),
            "images": payload["images"],
            "stats": payload["stats"],
        }
        if outcome.error:
            return ToolOutput(success=False, error=outcome.error, data=data)
        return ToolOutput(success=True, data=data)

    suffix = (agent_id or "merged").strip() or "merged"
    card = ToolCard(
        id=f"web_flash_{suffix}",
        name="web_flash",
        description=_description(language),
        input_params=_input_params(language),
    )
    return LocalFunction(card=card, func=_invoke)


__all__ = ["build_web_flash_tool"]
