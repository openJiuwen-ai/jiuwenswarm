# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified web search package: paid chain (petal first) then free fallback."""

from jiuwenswarm.agents.harness.common.tools.web_search.orchestrator import (
    is_valid_search_mode,
    normalize_search_mode,
    run_web_search,
)
from jiuwenswarm.agents.harness.common.tools.web_search.quality import evaluate_search_quality
from jiuwenswarm.agents.harness.common.tools.web_search.response import format_web_search_response
from jiuwenswarm.agents.harness.common.tools.web_search.settings import (
    load_web_search_settings,
    resolve_web_search_settings,
)
from jiuwenswarm.agents.harness.common.tools.web_search.harness import JiuwenHarnessWebSearchTool
from jiuwenswarm.agents.harness.common.tools.web_search.image_search import (
    extract_image_candidates,
    is_public_http_url,
    search_images,
)
from jiuwenswarm.agents.harness.common.tools.web_search.tool import (
    build_web_search_tool_card,
    ensure_web_search_harness_metadata,
    web_search,
)
from jiuwenswarm.agents.harness.common.tools.web_search.types import (
    ProviderRun,
    WebSearchRecord,
    WebSearchSettings,
)

__all__ = [
    "ProviderRun",
    "WebSearchRecord",
    "WebSearchSettings",
    "JiuwenHarnessWebSearchTool",
    "build_web_search_tool_card",
    "ensure_web_search_harness_metadata",
    "evaluate_search_quality",
    "extract_image_candidates",
    "format_web_search_response",
    "is_public_http_url",
    "is_valid_search_mode",
    "load_web_search_settings",
    "normalize_search_mode",
    "resolve_web_search_settings",
    "run_web_search",
    "search_images",
    "web_search",
]
