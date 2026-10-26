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
    "format_web_search_response",
    "is_valid_search_mode",
    "load_web_search_settings",
    "normalize_search_mode",
    "resolve_web_search_settings",
    "run_web_search",
    "web_search",
]
