# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchRecord

# 内联正文展示上限，避免超大网页正文撑爆模型上下文
_MAX_DISPLAY_BODY = 6000


def _dedupe_records(records: list[WebSearchRecord]) -> list[WebSearchRecord]:
    seen: set[str] = set()
    out: list[WebSearchRecord] = []
    for rec in records:
        key = (rec.url or "").strip().rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _format_records_block(
    heading: str,
    records: list[WebSearchRecord],
    *,
    include_answer: str = "",
) -> list[str]:
    lines = [heading]
    if include_answer.strip():
        lines.extend(["Answer:", include_answer.strip(), ""])
    deduped = _dedupe_records(records)
    if not deduped and not include_answer.strip():
        lines.append("(no structured results)")
        return lines
    # petal 已将完整正文(content)内联返回，模型通常无需再 fetch
    lines.append("Hint: 结果已内联展示正文（Content）；仅在正文缺失时才需 fetch_webpage。")
    for idx, rec in enumerate(deduped, 1):
        lines.append(f"{idx}. {rec.title}")
        if rec.url:
            lines.append(f"   URL: {rec.url}")
        # 优先展示完整 content；content 缺失（非 petal 源）时回退 snippet(summary)
        body = (rec.content or "").strip() or (rec.snippet or "").strip()
        if body:
            label = "Content" if (rec.content and rec.content.strip()) else "Snippet"
            if len(body) > _MAX_DISPLAY_BODY:
                body = body[:_MAX_DISPLAY_BODY] + " ...(body truncated)"
            lines.append(f"   {label}: {body}")
        lines.append(f"   Source: {rec.source}")
    return lines


def format_web_search_response(
    query: str,
    *,
    search_mode: str,
    selected_provider: str,
    quality_passed: bool,
    primary_records: list[WebSearchRecord],
    supplementary_records: list[WebSearchRecord],
    primary_answer: str,
    providers_tried: list[str],
    warning: str | None = None,
) -> str:
    quality_tag = "pass" if quality_passed else "low"
    header = (
        f"[web_search search_mode={search_mode} "
        f"selected={selected_provider} quality={quality_tag}]"
    )
    lines = [header, "", f"Query: {query}", ""]
    if warning:
        lines.append(warning)
        lines.append("")
    lines.extend(
        _format_records_block(
            f"Results ({len(_dedupe_records(primary_records))}):",
            primary_records,
            include_answer=primary_answer,
        )
    )
    supplementary = _dedupe_records(supplementary_records)
    if supplementary:
        lines.extend(["", "---", ""])
        lines.extend(
            _format_records_block("Supplementary (earlier providers):", supplementary)
        )
    lines.extend(["", "---", f"providers_tried: {', '.join(providers_tried)}"])
    return "\n".join(lines)


def format_success_response(
    query: str,
    search_mode: str,
    *,
    provider: str,
    quality_passed: bool,
    records: list[WebSearchRecord],
    answer: str,
    supplementary_records: list[WebSearchRecord] | None = None,
    providers_tried: list[str],
    warning: str | None = None,
) -> str:
    return format_web_search_response(
        query,
        search_mode=search_mode,
        selected_provider=provider,
        quality_passed=quality_passed,
        primary_records=records,
        supplementary_records=supplementary_records or [],
        primary_answer=answer,
        providers_tried=providers_tried,
        warning=warning,
    )
