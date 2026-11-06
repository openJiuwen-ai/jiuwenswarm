# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record trusted URL provenance through the pinned core's search-result hook.

Core owns search requests, quality filtering and rendering. The host only
records the structured URLs exposed by its successful-result callback.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.tools import WebFreeSearchTool

from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    complete_trusted_search_producer,
)


class TrustedWebFreeSearchTool(WebFreeSearchTool):
    """Preserve upstream behavior while recording its structured result URLs."""

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> str:
        try:
            return await super().invoke(inputs, **kwargs)
        finally:
            complete_trusted_search_producer(
                tool_name="free_search",
                success=False,
            )

    def _on_search_results(self, rows: list[dict[str, str]]) -> None:
        complete_trusted_search_producer(
            tool_name="free_search",
            success=True,
            urls=(str(row.get("url", "") or "") for row in rows),
        )


__all__ = ["TrustedWebFreeSearchTool"]
