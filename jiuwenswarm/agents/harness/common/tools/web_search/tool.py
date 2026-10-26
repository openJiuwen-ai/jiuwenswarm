# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.foundation.tool.base import ToolCard

from jiuwenswarm.agents.harness.common.tools.web_search.orchestrator import (
    is_valid_search_mode,
    normalize_search_mode,
    run_web_search,
)
from jiuwenswarm.agents.harness.common.tools.web_search.constants import KNOWN_PAID_PROVIDERS

logger = logging.getLogger(__name__)

_WEB_SEARCH_HARNESS_METADATA_REGISTERED = False

_HARNESS_STRIP_PARAMS = frozenset({
    "timeout_seconds",
    "overall_timeout",
    "mode",
    "search_tool",
    "provider",
    "search_source",
})


def _configured_providers_summary() -> str:
    try:
        from jiuwenswarm.agents.harness.common.tools.web_search.settings import load_web_search_settings
        order = load_web_search_settings().paid_provider_order
        if order:
            return ", ".join(order)
    except Exception:
        logger.debug("Failed to load web search settings, using default providers", exc_info=True)
    return "bocha, petal"


def _has_enabled_free_engines() -> bool:
    try:
        from jiuwenswarm.agents.harness.common.tools.web_search.free import _free_search_engines
        return len(_free_search_engines()) > 0
    except Exception:
        logger.debug("Failed to check free search engines", exc_info=True)
        return False


def _build_web_search_description() -> str:
    suffix = (
        "搜索结果默认仅返回摘要与 URL 列表；"
        "若需查看网页正文，请调用 fetch_webpage 工具（优先从内存缓存读取）。"
    )
    has_free = _has_enabled_free_engines()
    if has_free:
        return (
            "网页搜索统一入口。"
            "search_mode=default（默认）先付费后免费；"
            "search_mode=paid 仅付费，失败报错；"
            "search_mode=free 仅免费且不调用付费。"
            "配合 search_mode=paid 使用时优先使用指定源，不可用时返回明确错误。"
            "max_results 可选，限制单个 query 的最大返回条数。"
            "若用户指定使用某搜索引擎（如 bing、duckduckgo），请在 query 开头包含该引擎名称（如 'bing 今天的天气'），"
            "以便系统识别并在引擎不可用时向用户说明。"
            "default 模式已尝试所有可用搜索源（付费与免费），任何失败（all sources exhausted 或 no results）都不要再单独用 search_mode=paid 或 free 重试，"
            "也不要换 query 重试，直接告知用户搜索失败。" + suffix
        )
    else:
        return (
            "网页搜索统一入口。"
            "search_mode=default（默认）使用付费源；"
            "search_mode=paid 仅付费，失败报错。"
            "配合 search_mode=paid 使用时优先使用指定源，不可用时返回明确错误。"
            "max_results 可选，限制单个 query 的最大返回条数。"
            "若用户指定使用某搜索引擎（如 bing、duckduckgo），请在 query 开头包含该引擎名称（如 'bing 今天的天气'），"
            "以便系统识别并在引擎不可用时向用户说明。"
            "default 模式已尝试所有可用搜索源（付费），任何失败（all sources exhausted 或 no results）都不要再单独用 search_mode=paid 重试，"
            "也不要换 query 重试，直接告知用户搜索失败。" + suffix
        )


async def web_search_impl(
    query: str,
    search_mode: str = "default",
    search_source: str | None = None,
    max_results: int | None = None,
    cache: Any | None = None,
) -> str:
    query = (query or "").strip()
    if not query:
        logger.warning("[web_search] invoke rejected: empty query")
        return "[ERROR]: query cannot be empty."

    mode, extracted_source = normalize_search_mode(search_mode)
    if not is_valid_search_mode(mode):
        logger.warning(
            "[web_search] invalid search_mode=%r, using default; query=%r",
            search_mode,
            query[:120],
        )
        mode = "default"

    source = extracted_source
    if not source and search_source:
        raw = search_source.strip().lower()
        if raw in KNOWN_PAID_PROVIDERS:
            source = raw

    return await run_web_search(
        query, search_mode=mode, search_source=source, max_results=max_results, cache=cache,
    )


web_search = tool(
    name="web_search",
    description=_build_web_search_description(),
)(web_search_impl)


def _import_harness_web_schema():
    """Resolve openjiuwen harness web schema helpers across package layouts.

    Stable openjiuwen keeps tool schemas at ``harness.prompts.tools``;
    some enterprise builds nest them under ``harness.prompts.sections.tools``.
    """
    try:
        from openjiuwen.harness.prompts.tools.web_tools import _schema_free_search
    except ImportError:
        from openjiuwen.harness.prompts.sections.tools.web_tools import (  # type: ignore[no-redef]
            _schema_free_search,
        )
    from openjiuwen.harness.prompts import resolve_language

    return resolve_language, _schema_free_search


def _import_harness_tool_metadata():
    """Resolve ToolMetadataProvider registration APIs across package layouts."""
    try:
        from openjiuwen.harness.prompts.tools import register_tool_provider
        from openjiuwen.harness.prompts.tools.base import (
            ToolMetadataProvider as tool_metadata_provider_cls,
        )
    except ImportError:
        from openjiuwen.harness.prompts.sections.tools import (  # type: ignore[no-redef]
            register_tool_provider,
        )
        from openjiuwen.harness.prompts.sections.tools.base import (  # type: ignore[no-redef]
            ToolMetadataProvider as tool_metadata_provider_cls,
        )
    return register_tool_provider, tool_metadata_provider_cls


def _fallback_web_search_input_params(language: str) -> dict[str, Any]:
    has_free = _has_enabled_free_engines()
    search_mode_desc = "default | paid | free" if has_free else "default | paid"
    try:
        resolve_language, schema_free_search = _import_harness_web_schema()

        lang = resolve_language(language or "cn")
        schema = dict(schema_free_search(lang))
        props = dict(schema.get("properties") or {})
        for key in _HARNESS_STRIP_PARAMS:
            props.pop(key, None)
        props["search_mode"] = {
            "type": "string",
            "description": search_mode_desc,
            "default": "default",
        }
        if "max_results" in props:
            props["max_results"]["description"] = (
                "Optional result count; defaults to web_search.max_results in config."
            )
        schema["properties"] = props
        return schema
    except Exception as exc:
        logger.warning(
            "[web_search] harness schema fallback failed: %s", exc, exc_info=True
        )
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "search_mode": {
                    "type": "string",
                    "default": "default",
                    "description": search_mode_desc,
                },
                "max_results": {"type": "integer", "description": "Optional."},
            },
            "required": ["query"],
        }


def ensure_web_search_harness_metadata() -> None:
    global _WEB_SEARCH_HARNESS_METADATA_REGISTERED
    if _WEB_SEARCH_HARNESS_METADATA_REGISTERED:
        return
    try:
        register_tool_provider, tool_metadata_provider_cls = _import_harness_tool_metadata()
    except ImportError as exc:
        logger.debug("[web_search] harness metadata registration skipped: %s", exc)
        return

    class _WebSearchMetadataProvider(tool_metadata_provider_cls):
        def get_name(self) -> str:
            return "web_search"

        def get_description(self, language: str = "cn") -> str:
            return _build_web_search_description()

        def get_input_params(self, language: str = "cn") -> dict[str, Any]:
            return _fallback_web_search_input_params(language)

    register_tool_provider(_WebSearchMetadataProvider())
    _WEB_SEARCH_HARNESS_METADATA_REGISTERED = True


def build_web_search_tool_card(
    *,
    agent_id: str | None = None,
    language: str = "cn",
    id_prefix: str = "JiuwenHarnessWebSearch",
) -> ToolCard:
    ensure_web_search_harness_metadata()
    suffix = (agent_id or "").strip() or uuid.uuid4().hex

    return ToolCard(
        id=f"{id_prefix}_{suffix}",
        name="web_search",
        description=_build_web_search_description(),
        input_params=dict(_fallback_web_search_input_params(language)),
    )
