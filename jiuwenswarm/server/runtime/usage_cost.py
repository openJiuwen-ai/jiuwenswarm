from __future__ import annotations

from contextvars import ContextVar, Token
import math
from threading import Lock
from typing import Any, Callable


UsageSink = Callable[[dict[str, Any], str | None], dict[str, Any] | None]

_SUBAGENT_USAGE_SINK: ContextVar[UsageSink | None] = ContextVar(
    "jiuwenswarm_subagent_usage_sink",
    default=None,
)
_SESSION_COST_LOCK = Lock()
_SESSION_COST_TOTALS: dict[str, dict[str, float | bool]] = {}
_SESSION_COST_LIMITS: dict[str, float] = {}


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def get_usage_cost_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
        except Exception:
            config = {}
    raw = config.get("usage_cost", {}) if isinstance(config, dict) else {}
    section = raw if isinstance(raw, dict) else {}
    mode = str(section.get("mode") or "provider_reported").strip() or "provider_reported"
    currency_raw = section.get("currency")
    currency = str(currency_raw).strip().upper() if currency_raw is not None else ""
    enabled = _bool_value(section.get("enabled"), False) and mode == "provider_reported"
    enforce_limits = enabled and _bool_value(section.get("enforce_limits"), False)
    unsupported = str(section.get("unsupported_provider_behavior") or "refuse_limit").strip().lower()
    return {
        "enabled": enabled,
        "mode": mode,
        "enforce_limits": enforce_limits,
        "unsupported_provider_behavior": unsupported or "refuse_limit",
        "currency": currency or None,
    }


def is_usage_cost_enabled(config: dict[str, Any] | None = None) -> bool:
    return bool(get_usage_cost_settings(config).get("enabled"))


def is_usage_cost_limit_enforcement_enabled(config: dict[str, Any] | None = None) -> bool:
    return bool(get_usage_cost_settings(config).get("enforce_limits"))


class CostLimitExceededError(RuntimeError):
    def __init__(self, summary: dict[str, Any]):
        self.summary = summary
        total_cost = float(summary.get("total_cost", 0.0) or 0.0)
        cost_limit = float(summary.get("cost_limit", 0.0) or 0.0)
        currency = str(summary.get("currency") or "").strip().upper()
        total_text = f"{total_cost:.4f} {currency}" if currency else f"{total_cost:.4f}"
        limit_text = f"{cost_limit:.4f} {currency}" if currency else f"{cost_limit:.4f}"
        super().__init__(f"Cost limit exceeded: {total_text} > {limit_text}.")


def set_subagent_usage_sink(sink: UsageSink | None) -> Token[UsageSink | None]:
    return _SUBAGENT_USAGE_SINK.set(sink)


def reset_subagent_usage_sink(token: Token[UsageSink | None]) -> None:
    _SUBAGENT_USAGE_SINK.reset(token)


def get_subagent_usage_sink() -> UsageSink | None:
    return _SUBAGENT_USAGE_SINK.get()


def _session_key(session_id: str | None) -> str:
    return (session_id or "default").strip() or "default"


def _cost_value(usage: dict[str, Any], name: str) -> float | None:
    value = usage.get(name)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    return None


def add_session_usage(session_id: str | None, usage: dict[str, Any]) -> dict[str, Any]:
    """Accumulate provider cost metadata for a session and return the summary."""
    key = _session_key(session_id)
    settings = get_usage_cost_settings()
    input_tokens = _token_value(usage, "input_tokens") or 0.0
    output_tokens = _token_value(usage, "output_tokens") or 0.0
    total_tokens_raw = _token_value(usage, "total_tokens")
    total_tokens = total_tokens_raw if total_tokens_raw is not None else input_tokens + output_tokens
    input_cost_raw = _cost_value(usage, "input_cost")
    output_cost_raw = _cost_value(usage, "output_cost")
    total_cost_raw = _cost_value(usage, "total_cost")
    cost_available = bool(settings.get("enabled")) and any(
        v is not None for v in (input_cost_raw, output_cost_raw, total_cost_raw)
    )
    input_cost = input_cost_raw or 0.0
    output_cost = output_cost_raw or 0.0
    total_cost = total_cost_raw if total_cost_raw is not None else input_cost + output_cost
    with _SESSION_COST_LOCK:
        current = _SESSION_COST_TOTALS.setdefault(
            key,
            {
                "input_tokens": 0.0,
                "output_tokens": 0.0,
                "total_tokens": 0.0,
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": 0.0,
                "cost_available": False,
            },
        )
        current["input_tokens"] = float(current.get("input_tokens", 0.0)) + input_tokens
        current["output_tokens"] = float(current.get("output_tokens", 0.0)) + output_tokens
        current["total_tokens"] = float(current.get("total_tokens", 0.0)) + total_tokens
        if cost_available:
            current["cost_available"] = True
            current["input_cost"] = float(current.get("input_cost", 0.0)) + input_cost
            current["output_cost"] = float(current.get("output_cost", 0.0)) + output_cost
            current["total_cost"] = float(current.get("total_cost", 0.0)) + total_cost
    return get_session_cost_summary(key)


def get_session_cost_summary(session_id: str | None) -> dict[str, Any]:
    key = _session_key(session_id)
    settings = get_usage_cost_settings()
    with _SESSION_COST_LOCK:
        current = dict(_SESSION_COST_TOTALS.get(key) or {})
        limit = _SESSION_COST_LIMITS.get(key)
    feature_enabled = bool(settings.get("enabled"))
    cost_available = feature_enabled and bool(current.get("cost_available"))
    total_cost = float(current.get("total_cost", 0.0) or 0.0) if cost_available else 0.0
    enforcement_enabled = bool(settings.get("enforce_limits"))
    return {
        "session_id": key,
        "input_tokens": int(current.get("input_tokens", 0.0) or 0.0),
        "output_tokens": int(current.get("output_tokens", 0.0) or 0.0),
        "total_tokens": int(current.get("total_tokens", 0.0) or 0.0),
        "cost_feature_enabled": feature_enabled,
        "cost_available": cost_available,
        "cost_source": "provider_reported" if cost_available else "unavailable",
        "currency": settings.get("currency"),
        "input_cost": round(float(current.get("input_cost", 0.0) or 0.0), 6) if cost_available else 0.0,
        "output_cost": round(float(current.get("output_cost", 0.0) or 0.0), 6) if cost_available else 0.0,
        "total_cost": round(total_cost, 6),
        "cost_limit": round(limit, 6) if limit is not None else None,
        "limit_supported": cost_available and enforcement_enabled,
        "limit_enforcement_enabled": enforcement_enabled,
        "cost_limit_exceeded": cost_available and enforcement_enabled and limit is not None and total_cost > limit,
    }


def raise_if_session_cost_limit_exceeded(session_id: str | None) -> dict[str, Any]:
    """Return the current summary, or raise when the session is already over limit."""
    summary = get_session_cost_summary(session_id)
    if bool(summary.get("cost_limit_exceeded")):
        raise CostLimitExceededError(summary)
    return summary


def clear_session_cost(session_id: str | None) -> None:
    """Clear accumulated cost totals and limits for a finished session."""
    key = _session_key(session_id)
    with _SESSION_COST_LOCK:
        _SESSION_COST_TOTALS.pop(key, None)
        _SESSION_COST_LIMITS.pop(key, None)


def _token_value(usage: dict[str, Any], name: str) -> float | None:
    value = usage.get(name)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    return None


def set_session_cost_limit(session_id: str | None, limit: float | None) -> dict[str, Any]:
    key = _session_key(session_id)
    summary = get_session_cost_summary(key)
    with _SESSION_COST_LOCK:
        if limit is None:
            _SESSION_COST_LIMITS.pop(key, None)
        else:
            if not bool(summary.get("cost_feature_enabled")):
                raise ValueError("usage cost is disabled")
            if not bool(summary.get("limit_enforcement_enabled")):
                raise ValueError("cost limit enforcement is disabled")
            if not bool(summary.get("cost_available")):
                raise ValueError(
                    "cost limit unavailable: provider has not reported supported cost metadata"
                )
            value = float(limit)
            if not math.isfinite(value):
                raise ValueError("cost limit must be finite")
            _SESSION_COST_LIMITS[key] = max(0.0, value)
    return get_session_cost_summary(key)
