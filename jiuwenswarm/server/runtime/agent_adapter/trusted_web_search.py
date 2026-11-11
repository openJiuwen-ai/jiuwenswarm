# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record search provenance without duplicating SDK quality or budget policy."""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.tools import WebFreeSearchTool

from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    complete_trusted_search_producer,
)


class TrustedWebFreeSearchTool(WebFreeSearchTool):
    """Record only the structured rows accepted by the SDK search pipeline."""

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> str:
        try:
            if not callable(getattr(WebFreeSearchTool, "_on_search_results", None)):
                return "[ERROR]: openjiuwen search-result hook unavailable; install the matching agent-core version."
            return await super().invoke(inputs, **kwargs)
        finally:
            complete_trusted_search_producer(tool_name="free_search", success=False)

    def _on_search_results(self, rows: list[dict[str, str]]) -> None:
        complete_trusted_search_producer(
            tool_name="free_search",
            success=True,
            urls=(str(row.get("url", "") or "") for row in rows),
        )


__all__ = ["TrustedWebFreeSearchTool"]
