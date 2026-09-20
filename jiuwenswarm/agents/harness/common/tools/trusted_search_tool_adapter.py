# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host integration adapters for search tools and permission provenance.

The neutral search implementation lives in ``search_tools``. This module is the
single owner of the MCP search Tool objects and bridges successful structured
provider results to the optional one-shot trusted-search provenance lease.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy

from openjiuwen.core.foundation.tool import LocalFunction, tool
from openjiuwen.harness.prompts.tools import get_tool_description, get_tool_input_params

from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    complete_trusted_search_producer,
)
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    DEFAULT_SEARCH_MAX_RESULTS,
    configured_paid_search_providers,
    normalize_search_max_results,
    render_free_search_result,
    render_paid_search_result,
    run_free_search_structured,
    run_paid_search_structured,
)


@tool(
    name="mcp_free_search",
    description="Free search via DuckDuckGo. Input query and return ranked URLs with snippets.",
)
async def mcp_free_search(
    query: str,
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
    timeout_seconds: int = 20,
) -> str:
    """Run free search and commit validated structured URLs before rendering."""

    try:
        query = (query or "").strip()
        if not query:
            return "[ERROR]: query cannot be empty."
        max_results = normalize_search_max_results(max_results)
        timeout_seconds = max(5, min(timeout_seconds, 60))
        try:
            engine_used, rows = await asyncio.to_thread(
                run_free_search_structured, query, max_results, timeout_seconds
            )
        except Exception as exc:
            return f"[ERROR]: free search failed: {exc}"
        if not rows:
            return f"No search results for: {query}"
        complete_trusted_search_producer(
            tool_name="mcp_free_search",
            success=True,
            urls=(str(row.get("url", "") or "") for row in rows),
        )
        return render_free_search_result(
            query=query,
            engine_used=engine_used,
            rows=rows,
        )
    finally:
        complete_trusted_search_producer(
            tool_name="mcp_free_search",
            success=False,
        )


async def _run_paid_search(
    query: str,
    provider: str = "auto",
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
    timeout_seconds: int = 45,
) -> str:
    """Run paid search and commit validated structured URLs before rendering."""

    try:
        query = (query or "").strip()
        if not query:
            return "[ERROR]: query cannot be empty."
        provider = (provider or "auto").strip().lower()
        if provider not in {"auto", "bocha", "jina", "serper", "perplexity"}:
            return "[ERROR]: provider must be auto or a configured search provider."
        max_results = normalize_search_max_results(max_results)
        timeout_seconds = max(10, min(timeout_seconds, 120))
        try:
            provider_used, answer, urls = await run_paid_search_structured(
                query=query,
                provider=provider,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            return f"[ERROR]: {exc}"
        complete_trusted_search_producer(
            tool_name="mcp_paid_search",
            success=True,
            urls=urls,
        )
        return render_paid_search_result(
            provider=provider_used,
            answer=answer,
            urls=urls,
        )
    finally:
        complete_trusted_search_producer(
            tool_name="mcp_paid_search",
            success=False,
        )


class _ConfiguredPaidSearchTool(LocalFunction):
    """Normalize queued provider choices before LocalFunction schema validation."""

    async def invoke(self, inputs, **kwargs):
        provider = str(inputs.get("provider", "auto") or "auto").strip().lower()
        if provider in {"auto", "bocha", "perplexity", "serper", "jina"}:
            if provider != "auto" and provider not in configured_paid_search_providers():
                provider = "auto"
            inputs = {**inputs, "provider": provider}
        return await super().invoke(inputs, **kwargs)


mcp_paid_search = _ConfiguredPaidSearchTool(
    card=tool(
        name="mcp_paid_search",
        description=get_tool_description("paid_search", "en"),
    )(_run_paid_search).card,
    func=_run_paid_search,
)


def refresh_paid_search_metadata() -> None:
    """Refresh the shared MCP card after environment configuration is loaded."""
    mcp_paid_search.card.description = get_tool_description("paid_search", "en")
    params = deepcopy(mcp_paid_search.card.input_params)
    params["properties"]["provider"] = get_tool_input_params("paid_search", "en")["properties"]["provider"]
    mcp_paid_search.card.input_params = params


refresh_paid_search_metadata()

__all__ = ["mcp_free_search", "mcp_paid_search"]
