# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Record provenance from Core's structured free-search results."""

from typing import Any

from openjiuwen.harness.tools import WebFreeSearchTool

from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import complete_trusted_search_producer


class TrustedWebFreeSearchTool(WebFreeSearchTool):
    """Keep Core's search behavior while recording its successful result URLs."""

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> str:
        try:
            return await super().invoke(inputs, **kwargs)
        finally:
            complete_trusted_search_producer(tool_name="free_search", success=False)

    def _on_search_results(self, rows: list[dict[str, str]]) -> None:
        complete_trusted_search_producer(
            tool_name="free_search", success=True,
            urls=(str(row.get("url", "") or "") for row in rows),
        )
