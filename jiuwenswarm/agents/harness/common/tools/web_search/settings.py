# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging

from jiuwenswarm.agents.harness.common.tools.web_search.constants import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_PAID_PROVIDER_ORDER,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESULTS_CAP,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
)
from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchSettings

logger = logging.getLogger(__name__)


def load_web_search_settings() -> WebSearchSettings:
    timeout = DEFAULT_TIMEOUT_SECONDS
    max_results = DEFAULT_MAX_RESULTS
    order: list[str] = list(DEFAULT_PAID_PROVIDER_ORDER)
    try:
        from jiuwenswarm.common.config import get_config

        raw = get_config().get("web_search") or {}
        if isinstance(raw, dict):
            timeout = int(raw.get("timeout_seconds") or timeout)
            max_results = int(raw.get("max_results") or max_results)
            cfg_order = raw.get("paid_provider_order")
            if isinstance(cfg_order, list) and cfg_order:
                order = [
                    str(item).strip().lower()
                    for item in cfg_order
                    if str(item).strip()
                ]
    except Exception:
        logger.warning("[web_search] config load failed", exc_info=True)

    timeout = max(MIN_TIMEOUT_SECONDS, min(timeout, MAX_TIMEOUT_SECONDS))
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    settings = WebSearchSettings(
        timeout_seconds=timeout,
        max_results=max_results,
        paid_provider_order=tuple(order or DEFAULT_PAID_PROVIDER_ORDER),
    )
    logger.debug(
        "[web_search] loaded settings timeout=%ss max_results=%s paid_order=%s",
        settings.timeout_seconds,
        settings.max_results,
        ",".join(settings.paid_provider_order),
    )
    return settings


def resolve_web_search_settings(max_results: int | None = None) -> WebSearchSettings:
    """Load config defaults and apply optional LLM ``max_results`` override."""
    settings = load_web_search_settings()
    if max_results is None or max_results <= 0:
        return settings
    capped = max(1, min(int(max_results), MAX_RESULTS_CAP))
    return WebSearchSettings(
        timeout_seconds=settings.timeout_seconds,
        max_results=capped,
        paid_provider_order=settings.paid_provider_order,
    )
