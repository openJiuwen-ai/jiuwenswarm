# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted URL provenance adapter for OpenJiuwen's free-search tool.

OpenJiuwen commit ``fd30965b4e1622accef82d212716787c3fe36da9`` does not
expose structured search rows through a public hook. Keep the private seam in
this module and lock its behavior to the upstream tool with equivalence tests.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.tools import WebFreeSearchTool
from openjiuwen.harness.tools.web import free_search as openjiuwen_free_search
from openjiuwen.harness.tools.web._common import _safe_int

from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    complete_trusted_search_producer,
)


def _new_search_session() -> Any:
    """Return the pinned upstream transport seam or fail compatibility closed."""

    http_transport = getattr(openjiuwen_free_search, "_http", None)
    new_session = getattr(http_transport, "new_session", None)
    if not callable(new_session):
        raise RuntimeError("openjiuwen free-search transport contract unavailable")
    return new_session()


class TrustedWebFreeSearchTool(WebFreeSearchTool):
    """Preserve upstream behavior while recording its structured result URLs."""

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            query = str(inputs.get("query", "") or "").strip()
            max_results = _safe_int(inputs.get("max_results", 8) or 8, 8)
            timeout_seconds = _safe_int(inputs.get("timeout_seconds", 20) or 20, 20)

            if not query:
                return "[ERROR]: query cannot be empty."

            max_results = max(1, min(max_results, 20))
            timeout_seconds = max(5, min(timeout_seconds, 60))
            try:
                async with _new_search_session() as session:
                    engine_used, rows = await WebFreeSearchTool._search_free(
                        session, query, max_results, timeout_seconds
                    )
            except Exception as exc:  # noqa: BLE001
                return f"[ERROR]: free search failed: {exc}"

            if not rows:
                return f"No search results for: {query}"

            complete_trusted_search_producer(
                tool_name="free_search",
                success=True,
                urls=(str(row.get("url", "") or "") for row in rows),
            )

            lines = [
                "Free search results "
                f"({WebFreeSearchTool._engine_display_name(engine_used)}) for: {query}"
            ]
            for idx, row in enumerate(rows, 1):
                lines.append(f"{idx}. {row['title']}")
                lines.append(f"   URL: {row['url']}")
                if row.get("snippet"):
                    lines.append(f"   Snippet: {row['snippet']}")

            top_fetch_urls: list[str] = []
            for row in rows:
                url = str(row.get("url", "") or "")
                if not url:
                    continue
                top_fetch_urls.append(url)
                if len(top_fetch_urls) >= 3:
                    break
            lines.append("")
            lines.append(
                "Required next step: before reformulating the query, fetch at least 2 relevant URLs "
                "from the top results. If the first fetch fails, is a dynamic shell page, or is "
                "still incomplete, continue with the next recommended URLs instead of searching "
                "again."
            )
            if top_fetch_urls:
                lines.append("Recommended fetch targets:")
                for idx, url in enumerate(top_fetch_urls, 1):
                    lines.append(f"{idx}. {url}")
            return "\n".join(lines)
        finally:
            complete_trusted_search_producer(
                tool_name="free_search",
                success=False,
            )


__all__ = ["TrustedWebFreeSearchTool"]
