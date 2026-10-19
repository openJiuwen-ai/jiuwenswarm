"""Language helpers for user-facing Symphony orchestration output."""

from __future__ import annotations

from typing import Any

DEFAULT_ORCHESTRATION_LANGUAGE = "cn"
SUPPORTED_ORCHESTRATION_LANGUAGES = frozenset({"cn", "en"})


def resolve_orchestration_language(value: Any = None) -> str:
    """Normalize runtime language to the Symphony ``cn | en`` contract."""

    raw = str(value or "").strip().lower()
    if raw == "zh":
        raw = "cn"
    if raw not in SUPPORTED_ORCHESTRATION_LANGUAGES:
        return DEFAULT_ORCHESTRATION_LANGUAGE
    return raw


def planner_language_instruction(language: str) -> str:
    """Build the shared LLM output-language instruction."""

    normalized = resolve_orchestration_language(language)
    target = "Simplified Chinese" if normalized == "cn" else "English"
    return (
        f"Write all user-visible natural-language fields in {target}. "
        "Keep Skill IDs, original Skill names, status values, enum values, and "
        "structured field names exactly as provided; do not translate them."
    )


def default_plan_title(language: str) -> str:
    """Return the localized presentation fallback title."""

    if resolve_orchestration_language(language) == "en":
        return "Symphony plan"
    return "Symphony 编排计划"


def default_fast_no_plan_title(language: str) -> str:
    """Return the localized fast-planner no-plan title."""

    if resolve_orchestration_language(language) == "en":
        return "No Symphony fast plan"
    return "未生成 Symphony 快速编排计划"


def default_beam_plan_title(language: str) -> str:
    """Return the localized Beam planner fallback title."""

    if resolve_orchestration_language(language) == "en":
        return "Symphony beam plan"
    return "Symphony 束搜索编排计划"
