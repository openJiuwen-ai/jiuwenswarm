"""Compact Host-owned projection for Reviewer audit and UI metadata."""

from __future__ import annotations

from collections.abc import Mapping

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    _REVIEWER_UI_SHORT_TEXT_MAX_LENGTH,
    _REVIEWER_UI_TEXT_MAX_LENGTH,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
    redact_text,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.workspace_paths import sanitize_review_ui_value

_MANUAL_STATUSES = frozenset({"manual", "fallback", "timed_out", "aborted"})

_DISPLAY_MESSAGES = {
    "completed": ("宿主权限检查已完成。", "Host permission checks completed."),
    "denied": ("宿主权限检查拒绝了此操作。", "Host permission checks denied this operation."),
    "deterministic_allow": (
        "宿主权限规则已确认该操作满足自动执行条件。",
        "Host permission rules verified that this operation meets automatic execution conditions.",
    ),
    "execution_provider_contract_unverified": (
        "Host 无法验证当前工具的执行 provider 合同，因此需要人工确认；人工允许不会改变工具的实际执行环境。",
        "The Host cannot verify this tool's execution-provider contract, so manual "
        "confirmation is required. Manual approval does not change the tool's actual "
        "execution environment.",
    ),
    "low_confidence": (
        "自动审批结论的置信度低于当前阈值，已转人工审批。",
        "AutoReviewer confidence was below the configured threshold; manual review is required.",
    ),
    "manual_approval": (
        "用户已通过人工审批允许此操作。",
        "The user allowed this operation through manual approval.",
    ),
    "manual_required": (
        "当前权限证据不足以自动批准，请人工核对工具参数和风险。",
        "Current permission evidence is insufficient for automatic approval; review "
        "the tool parameters and risks manually.",
    ),
    "reviewer_fallback": (
        "自动审批响应无法通过宿主校验，未形成可信结论。",
        "The AutoReviewer response did not pass host validation and produced no trusted decision.",
    ),
    "reviewer_outcome_not_allowed": (
        "自动审批给出的结论超出宿主策略允许范围，已转人工审批。",
        "AutoReviewer returned an outcome outside the host policy allowance; manual review is required.",
    ),
    "reviewer_timeout": (
        "自动审批审查超时，未能形成可信结论。",
        "AutoReviewer timed out before producing a trusted decision.",
    ),
    "user_rejected": ("用户拒绝了本次工具调用。", "The user rejected this tool call."),
}

_MANUAL_HINTS = {
    "default": (
        "请核对工具参数是否符合用户意图，再决定是否允许。",
        "Verify that the tool parameters match the user's intent before approving.",
    ),
    "fallback": (
        "自动审批无法完成判断，已转人工审批。",
        "AutoReviewer could not complete the decision; review it manually.",
    ),
    "timed_out": (
        "自动审批审查超时，请人工核对工具参数和风险后决定。",
        "Review the tool parameters and risks manually because AutoReviewer timed out.",
    ),
}


def _configured_reviewer_display_language() -> str:
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
        if isinstance(config, Mapping):
            return (
                "en"
                if str(config.get("preferred_language") or "").strip().lower()
                == "en"
                else "zh"
            )
    except Exception:
        return "zh"
    return "zh"


def _localized(pair: tuple[str, str], language: str) -> str:
    return pair[1] if language == "en" else pair[0]


def _truncate(value: object, *, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    candidate = text[: max_length - 3].rstrip()
    boundary = max(candidate.rfind(" "), candidate.rfind("\t"))
    if boundary >= max_length // 2:
        candidate = candidate[:boundary].rstrip()
    return f"{candidate}..."


def _text(
    metadata: Mapping[str, object],
    *keys: str,
    default: str = "",
    max_length: int = _REVIEWER_UI_SHORT_TEXT_MAX_LENGTH,
) -> str:
    value: object = default
    for key in keys:
        candidate = metadata.get(key)
        if candidate is not None and candidate != "":
            value = candidate
            break
    return _truncate(
        redact_text(value, max_length=max_length * 2),
        max_length=max_length,
    )


def _reviewer_status(metadata: Mapping[str, object]) -> str:
    status = _text(metadata, "final_reviewer_status", "reviewer_status")
    if status:
        return status
    outcome = _text(metadata, "reviewer_outcome").lower()
    if outcome in {"deny", "denied"}:
        return "denied"
    lifecycle = _text(metadata, "reviewer_lifecycle")
    if lifecycle:
        return lifecycle
    return "fallback" if _text(metadata, "reviewer_fallback_reason", "fallback_reason") else "manual"


def _host_display_reason(
    reason: str,
    metadata: Mapping[str, object],
    *,
    status: str,
    language: str,
) -> str:
    reason_code = _text(metadata, "manual_reason_code", "reviewer_reason_code", default=reason)
    fallback = _text(metadata, "fallback_reason", "reviewer_fallback_reason")
    source = _text(metadata, "decision_source")
    if reason_code in {"execution_provider_contract_unverified", "user_rejected"}:
        key = reason_code
    elif status == "timed_out" or fallback == "reviewer_timeout":
        key = "reviewer_timeout"
    elif fallback == "low_confidence":
        key = "low_confidence"
    elif reason_code == "reviewer_outcome_not_allowed":
        key = "reviewer_outcome_not_allowed"
    elif fallback or reason_code == "reviewer_fallback":
        key = "reviewer_fallback"
    elif source.startswith("deterministic_"):
        key = "deterministic_allow"
    elif source == "manual_approval" and status in {"approved", "deterministic_allow"}:
        key = "manual_approval"
    elif status in _MANUAL_STATUSES:
        key = "manual_required"
    elif status in {"denied", "blocked"}:
        key = "denied"
    else:
        key = "completed"
    return _localized(_DISPLAY_MESSAGES[key], language)


def _reviewer_action_summary(facts: ToolDecisionFacts) -> str:
    """Describe the action without re-parsing paths, URLs, or shell syntax."""
    return f"{facts.tool_name} ({facts.tool_category}, {facts.capability.risk_tier} risk)"


def _reviewer_ui_metadata(
    facts: ToolDecisionFacts,
    *,
    reason: str,
    metadata: Mapping[str, object],
) -> dict[str, object]:
    """Return only the normalized decision fields consumed by audit/Web/TUI."""
    merged = dict(metadata)
    status = _reviewer_status(merged)
    language = _configured_reviewer_display_language()
    source = _text(
        merged,
        "decision_source",
        default="fallback" if status in {"fallback", "timed_out"} else "auto_reviewer",
    )
    natural_language = (
        source == "auto_reviewer"
        and status not in {"fallback", "timed_out", "aborted"}
        and not _text(merged, "fallback_reason", "reviewer_fallback_reason")
        and _text(merged, "reviewer_reason_code", "reason_code")
        not in {"reviewer_fallback", "reviewer_outcome_not_allowed"}
    )
    host_summary = _text(
        merged,
        "host_manual_reason_summary",
        max_length=_REVIEWER_UI_TEXT_MAX_LENGTH,
    )
    downgraded_allow = (
        status in _MANUAL_STATUSES
        and bool(host_summary)
        and _text(merged, "reviewer_reason_code", "reason_code")
        == "reviewer_outcome_not_allowed"
    )
    evidence = (
        host_summary
        if downgraded_allow
        else _text(
            merged,
            "evidence_summary",
            "reviewer_reason_summary",
            default=reason,
            max_length=_REVIEWER_UI_TEXT_MAX_LENGTH,
        )
        if natural_language
        else _host_display_reason(
            reason,
            merged,
            status=status,
            language=language,
        )
    )
    action_summary = _text(
        merged,
        "action_summary",
        default=_reviewer_action_summary(facts),
    )
    merged.update(
        {
            "action_summary": _truncate(
                sanitize_review_ui_value(action_summary, facts.workspace_root),
                max_length=_REVIEWER_UI_SHORT_TEXT_MAX_LENGTH,
            ),
            "decision_source": source,
            "evidence_summary": evidence,
            "final_reviewer_status": status,
            "reviewer_status": status,
            "risk_level": _text(
                merged,
                "risk_level",
                default=str(facts.capability.risk_tier or "").strip() or "unknown",
            ),
        }
    )
    merged.pop("user_authorization", None)
    for key in (
        "decision_digest",
        "latency_ms",
        "reviewer_raw_outcome",
        "reviewer_raw_status",
        "reviewer_user_review_hint",
        "search_query_digest",
        "search_query_preview",
        "search_source_tool",
    ):
        merged.pop(key, None)
    if status in _MANUAL_STATUSES:
        merged["manual_reason_code"] = _text(
            merged,
            "manual_reason_code",
            "reviewer_reason_code",
            default=reason,
        )
        merged["manual_reason_summary"] = (
            host_summary
            if downgraded_allow
            else _text(
                merged,
                "manual_reason_summary",
                "reviewer_reason_summary",
                default=reason,
                max_length=_REVIEWER_UI_TEXT_MAX_LENGTH,
            )
            if natural_language
            else evidence
        )
        if not bool(merged.get("preserve_reviewer_user_review_hint")):
            merged["user_review_hint"] = _localized(
                _MANUAL_HINTS.get(status, _MANUAL_HINTS["default"]), language
            )
    else:
        for key in (
            "manual_reason_code",
            "manual_reason_summary",
            "preserve_reviewer_user_review_hint",
            "reviewer_user_review_hint",
            "user_review_hint",
        ):
            merged.pop(key, None)
    return merged


def _with_host_owned_manual_review_display(
    facts: ToolDecisionFacts,
    route: DecisionRoute,
    metadata: Mapping[str, object],
) -> tuple[dict[str, object], str | None]:
    """Replace model wording when the Host route itself requires manual review."""
    if not route.requires_manual:
        return dict(metadata), None
    language = _configured_reviewer_display_language()
    if route.no_auto_allow_reason == "code_parse_error":
        summary = _localized(
            (
                "代码无法解析，静态读写和导入检查未执行，因此需要人工审批。",
                "The code could not be parsed, so static checks did not run; manual approval is required.",
            ),
            language,
        )
    elif route.no_auto_allow_reason == "code_payload_missing":
        summary = _localized(
            (
                "可执行代码内容缺失或为空，因此需要人工审批。",
                "The executable code payload is missing or empty; manual approval is required.",
            ),
            language,
        )
    else:
        summary = _localized(_DISPLAY_MESSAGES["manual_required"], language)
    reason_code = route.no_auto_allow_reason
    if facts.tool_category == "browser" and (
        _text(metadata, "manual_reason_code", "reviewer_reason_code")
        == "browser_network_guard_unverified"
        or reason_code == "browser_network_guard_unverified"
    ):
        reason_code = "browser_network_guard_unverified"
    merged = {
        **metadata,
        "evidence_summary": summary,
        "host_manual_reason_code": reason_code,
        "host_manual_reason_summary": summary,
        "manual_reason_summary": summary,
        "preserve_reviewer_user_review_hint": True,
        "user_review_hint": _localized(_MANUAL_HINTS["default"], language),
    }
    return merged, summary


def _localized_host_display_reason(
    reason: str,
    metadata: Mapping[str, object],
    *,
    reviewer_status: str,
) -> str:
    return _host_display_reason(
        reason,
        metadata,
        status=reviewer_status,
        language=_configured_reviewer_display_language(),
    )
