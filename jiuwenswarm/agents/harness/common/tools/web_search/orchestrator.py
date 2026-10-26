# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import re
from typing import Any

from jiuwenswarm.agents.harness.common.tools.web_search.constants import KNOWN_PAID_PROVIDERS
from jiuwenswarm.agents.harness.common.tools.web_search.free import _free_search_engines
from jiuwenswarm.agents.harness.common.tools.web_search.providers import (
    any_paid_provider_available,
    paid_provider_available,
    run_free_chain,
    run_paid_chain,
)
from jiuwenswarm.agents.harness.common.tools.web_search.log_util import (
    paid_availability_report,
    paid_provider_skip_reason,
    provider_run_summary,
    settings_summary,
    truncate_query,
)
from jiuwenswarm.agents.harness.common.tools.web_search.response import format_success_response
from jiuwenswarm.agents.harness.common.tools.web_search.settings import resolve_web_search_settings
from jiuwenswarm.agents.harness.common.tools.web_search.types import ProviderRun, WebSearchRecord

logger = logging.getLogger(__name__)

_SEARCH_MODE_ALIASES: dict[str, str] = {
    "": "default",
    "default": "default",
    "auto": "default",
    "paid": "paid",
    "paid_search": "paid",
    "mcp_paid_search": "paid",
    "petal": "paid",
    "petal_search": "paid",
    "mcp_petal_search": "paid",
    "free": "free",
    "free_search": "free",
    "mcp_free_search": "free",
}

_SUPPORTED_SEARCH_MODES = frozenset({"default", "paid", "free"})

_ENGINE_ALIAS_MAP: dict[str, str] = {
    "duckduckgo": "duckduckgo", "ddg": "duckduckgo", "鸭子": "duckduckgo",
    "谷歌": "google", "google": "google",
    "必应": "bing", "bing": "bing",
    "百度": "baidu", "baidu": "baidu",
    "花瓣": "petal", "petal": "petal",
    "博查": "bocha", "bocha": "bocha",
    "360": "360", "好搜": "360", "so": "360",
    "搜狗": "sogou", "sogou": "sogou",
    "头条": "toutiao", "今日头条": "toutiao",
}

_PROVIDER_TO_ENGINE: dict[str, str] = {
    "petal": "petal",
    "bocha": "bocha",
    "tavily": "tavily",
    "perplexity": "perplexity",
    "serper": "serper",
    "jina": "jina",
    "duckduckgo": "duckduckgo",
    "duckduckgo-jina": "duckduckgo",
    "bing": "bing",
}


def _detect_requested_engine(query: str) -> str | None:
    query_lower = query.lower()
    for alias, engine in _ENGINE_ALIAS_MAP.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", query_lower):
            return engine
    return None


def _generate_engine_mismatch_warning(
    query: str,
    actual_provider: str,
) -> str | None:
    requested_engine = _detect_requested_engine(query)
    if not requested_engine:
        return None
    # actual_provider 形如 "paid:bocha" / "free:duckduckgo"，剥掉 "paid:"/"free:" 前缀
    # 再查 _PROVIDER_TO_ENGINE，否则带前缀查不到 → 误判与 requested_engine 不匹配。
    actual_base = actual_provider.split(":", 1)[-1] if ":" in actual_provider else actual_provider
    actual_engine = _PROVIDER_TO_ENGINE.get(actual_base, actual_base)
    if requested_engine == actual_engine:
        return None
    return (
        f"⚠️ 用户请求使用 {requested_engine} 搜索，但该引擎不可用，"
        f"已自动切换至 {actual_provider}。"
    )


def _resolve_engine_preferred_source(query: str) -> str | None:
    """从 query 文本里识别用户指定的引擎名；当该引擎是可用付费源时返回它作为 preferred。

    仅当识别到的引擎属于 KNOWN_PAID_PROVIDERS 且当前确实可用（API key 已配置）时，
    才把它作为优先 provider；否则返回 None，回退到默认 paid_order 顺序。
    免费引擎名（google/bing/baidu 等）不属于付费源，不在此注入。
    """
    requested_engine = _detect_requested_engine(query)
    if not requested_engine:
        return None
    if requested_engine not in KNOWN_PAID_PROVIDERS:
        return None
    if not paid_provider_available(requested_engine):
        return None
    return requested_engine


def _parse_search_source(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw in KNOWN_PAID_PROVIDERS:
        return raw
    return None


def normalize_search_mode(value: str | None) -> tuple[str, str | None]:
    key = (value or "").strip().lower()
    if not key:
        return "default", None
    if ":" in key:
        mode_part, source_part = key.split(":", 1)
        mode = _SEARCH_MODE_ALIASES.get(mode_part.strip(), mode_part.strip())
        source = _parse_search_source(source_part.strip())
        return mode, source
    return _SEARCH_MODE_ALIASES.get(key, key), None


def is_valid_search_mode(value: str | None) -> bool:
    mode, _source = normalize_search_mode(value)
    return mode in _SUPPORTED_SEARCH_MODES


def _has_usable_results(run: ProviderRun) -> bool:
    return bool(run.records or (run.answer or "").strip())


def _provider_status(run: ProviderRun) -> str:
    return "pass" if run.quality_passed else "low"


def _log_done(
    *,
    query: str,
    mode: str,
    provider: str,
    quality_passed: bool,
    providers_tried: list[str],
) -> None:
    logger.info(
        "[web_search] done query=%r search_mode=%s selected=%s quality=%s tried=%s",
        truncate_query(query),
        mode,
        provider,
        "pass" if quality_passed else "low",
        ", ".join(providers_tried),
    )


def _log_failed(
    *,
    query: str,
    mode: str,
    reason: str,
    providers_tried: list[str] | None = None,
    detail: str = "",
) -> None:
    tried = ", ".join(providers_tried) if providers_tried else "-"
    extra = f" detail={detail}" if detail else ""
    logger.warning(
        "[web_search] failed query=%r search_mode=%s reason=%s tried=%s%s",
        truncate_query(query),
        mode,
        reason,
        tried,
        extra,
    )


async def run_web_search(
    query: str,
    *,
    search_mode: str = "default",
    search_source: str | None = None,
    max_results: int | None = None,
    cache: Any | None = None,
) -> str:
    settings = resolve_web_search_settings(max_results)
    mode, extracted_source = normalize_search_mode(search_mode)
    preferred_source = extracted_source or _parse_search_source(search_source)
    # 显式入参优先；缺失时若 query 里识别出可用付费引擎则注入为 preferred_source，
    # 让 run_paid_chain 把该引擎提到 order 首位优先试（而非仅生成不匹配警告）。
    if not preferred_source:
        preferred_source = _resolve_engine_preferred_source(query)
    if mode != "free" and preferred_source and not paid_provider_available(preferred_source):
        reason = paid_provider_skip_reason(preferred_source)
        _log_failed(
            query=query,
            mode=mode,
            reason=f"requested source {preferred_source} unavailable: {reason}",
            detail=paid_availability_report(settings.paid_provider_order),
        )
        return (
            f"[ERROR]: requested paid source '{preferred_source}' unavailable ({reason}). "
            "You may retry with search_mode=free."
            if _free_search_engines()
            else f"[ERROR]: requested paid source '{preferred_source}' unavailable ({reason})."
        )
    logger.debug(
        "[web_search] start query=%r search_mode=%s search_source=%s %s",
        truncate_query(query),
        mode,
        preferred_source or "-",
        settings_summary(settings),
    )

    if mode == "free":
        free_run = await run_free_chain(query, settings)
        if free_run.error:
            _log_failed(
                query=query,
                mode=mode,
                reason=free_run.error,
                detail=provider_run_summary(free_run),
            )
            return f"[ERROR]: free search failed: {free_run.error}"
        if not _has_usable_results(free_run):
            _log_failed(
                query=query,
                mode=mode,
                reason="no results",
                detail=provider_run_summary(free_run),
            )
            return "[ERROR]: free search failed: no results"
        tried = [f"{free_run.provider}({_provider_status(free_run)})"]
        _log_done(
            query=query,
            mode=mode,
            provider=free_run.provider,
            quality_passed=free_run.quality_passed,
            providers_tried=tried,
        )
        return format_success_response(
            query,
            mode,
            provider=free_run.provider,
            quality_passed=free_run.quality_passed,
            records=free_run.records,
            answer=free_run.answer,
            providers_tried=tried,
            warning=_generate_engine_mismatch_warning(query, free_run.provider),
        )

    if mode == "paid":
        paid_run: ProviderRun | None = None
        tried: list[str] = []
        if preferred_source:
            paid_run, tried = await run_paid_chain(query, settings, preferred_provider=preferred_source, cache=cache)
        else:
            if not any_paid_provider_available(settings.paid_provider_order):
                availability = paid_availability_report(settings.paid_provider_order)
                _log_failed(
                    query=query,
                    mode=mode,
                    reason="paid unavailable",
                    detail=availability,
                )
                if _free_search_engines():
                    return "[ERROR]: paid search unavailable. Use search_mode=free instead."
                return "[ERROR]: paid search unavailable. No search source is available."
            paid_run, tried = await run_paid_chain(query, settings, cache=cache)
        if paid_run and paid_run.quality_passed:
            _log_done(
                query=query,
                mode=mode,
                provider=paid_run.provider,
                quality_passed=True,
                providers_tried=tried,
            )
            return format_success_response(
                query,
                mode,
                provider=paid_run.provider,
                quality_passed=True,
                records=paid_run.records,
                answer=paid_run.answer,
                providers_tried=tried,
                warning=_generate_engine_mismatch_warning(query, paid_run.provider),
            )
        _log_failed(
            query=query,
            mode=mode,
            reason="paid search failed",
            providers_tried=tried,
            detail=provider_run_summary(paid_run),
        )
        if _free_search_engines():
            return "[ERROR]: paid search failed. You may retry with search_mode=free."
        return "[ERROR]: paid search failed. No search source is available."

    paid_run, tried = await run_paid_chain(query, settings, preferred_provider=preferred_source)
    if paid_run and paid_run.quality_passed:
        _log_done(
            query=query,
            mode=mode,
            provider=paid_run.provider,
            quality_passed=True,
            providers_tried=tried,
        )
        return format_success_response(
            query,
            mode,
            provider=paid_run.provider,
            quality_passed=True,
            records=paid_run.records,
            answer=paid_run.answer,
            providers_tried=tried,
            warning=_generate_engine_mismatch_warning(query, paid_run.provider),
        )

    earlier: list[WebSearchRecord] = []
    if paid_run and paid_run.records:
        earlier.extend(paid_run.records)

    logger.debug(
        "[web_search] default fallback free query=%r paid_tried=%s last_paid=%s",
        truncate_query(query),
        ", ".join(tried),
        provider_run_summary(paid_run),
    )
    free_run = await run_free_chain(query, settings)
    if free_run.error:
        _log_failed(
            query=query,
            mode=mode,
            reason=free_run.error,
            providers_tried=tried,
            detail=provider_run_summary(free_run),
        )
        return (
            "[ERROR]: web search failed: all sources exhausted (configuration issue, not query-related). "
            "Do not retry with any search_mode or a different query; "
            "inform the user that web search is currently unavailable."
        )
    if not _has_usable_results(free_run):
        _log_failed(
            query=query,
            mode=mode,
            reason="no results",
            providers_tried=tried,
            detail=provider_run_summary(free_run),
        )
        return (
            "[ERROR]: web search failed: no results from any source. "
            "Do not retry with any search_mode or a different query; "
            "inform the user that web search returned no results."
        )
    tried.append(f"{free_run.provider}({_provider_status(free_run)})")
    _log_done(
        query=query,
        mode=mode,
        provider=free_run.provider,
        quality_passed=free_run.quality_passed,
        providers_tried=tried,
    )
    return format_success_response(
        query,
        mode,
        provider=free_run.provider,
        quality_passed=free_run.quality_passed,
        records=free_run.records,
        answer=free_run.answer,
        supplementary_records=earlier,
        providers_tried=tried,
        warning=_generate_engine_mismatch_warning(query, free_run.provider),
    )
