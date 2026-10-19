# coding: utf-8
from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from urllib.parse import urlparse


ReasoningProviderKind = Literal["deepseek_official", "dashscope_bailian"]
ReasoningLevel = Literal["off", "low", "medium", "high"]
ReasoningEffort = Literal["off", "high"]

OPENAI_SDK_REASONING_PROVIDERS = {
    "openai",
    "deepseek",
    "dashscope",
}

SUPPORTED_DEEPSEEK_V4_MODELS = {
    "deepseek-v4-pro",
    "deepseek-v4-flash",
}

LEVEL_MAPPING: dict[ReasoningLevel, ReasoningEffort] = {
    "off": "off",
    "low": "high",
    "medium": "high",
    "high": "high",
}


def _normalize_provider(provider: Any) -> str:
    if isinstance(provider, Enum):
        provider = provider.value
    return str(provider or "").strip().lower()


def _parse_api_base(api_base: str | None):
    value = str(api_base or "").strip()
    if value and "://" not in value:
        value = f"https://{value}"
    return urlparse(value)


def resolve_endpoint_profile_override(api_base: str | None) -> str | None:
    """Return the endpoint profile configured for a gateway host."""
    host = (_parse_api_base(api_base).hostname or "").lower()
    if not host:
        return None
    try:
        from jiuwenswarm.common.config import get_endpoint_profile_overrides

        return get_endpoint_profile_overrides().get(host)
    except Exception:
        return None


SAMPLING_OVERRIDE_RULES: dict[str, dict[str, float]] = {
    "api.moonshot.cn": {"temperature": 1.0, "top_p": 0.95},
    "api.kimi.com": {"temperature": 1.0, "top_p": 0.95},
}


def resolve_sampling_override(
    api_base: str | None,
    model_name: str | None = None,
) -> dict[str, float] | None:
    """Return required Kimi sampling values, including aggregator routes."""
    host = (_parse_api_base(api_base).hostname or "").lower()
    model = str(model_name or "").strip().lower().rsplit("/", 1)[-1]
    if host in SAMPLING_OVERRIDE_RULES or model.startswith("kimi-"):
        return {"temperature": 1.0, "top_p": 0.95}
    return None


def resolve_reasoning_provider_kind(
    api_base: str | None,
) -> ReasoningProviderKind | None:
    parsed = _parse_api_base(api_base)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")

    if host == "api.deepseek.com":
        return "deepseek_official"

    if host == "dashscope.aliyuncs.com" and path.startswith("/compatible-mode"):
        return "dashscope_bailian"

    return None


def normalize_reasoning_level(raw: Any) -> ReasoningLevel | None:
    if raw is None:
        return None

    key = str(raw).strip().lower()
    if key == "":
        return None
    if key in {"off", "none", "false", "disable", "disabled"}:
        return "off"
    if key in {"on", "true", "enable", "enabled", "low"}:
        return "low"
    if key in {"medium", "med", "mid"}:
        return "medium"
    if key == "high":
        return "high"
    return None


def resolve_reasoning_target(
    *,
    client_provider: Any,
    api_base: str | None,
    model_name: str | None,
) -> tuple[ReasoningProviderKind, str] | None:
    provider = _normalize_provider(client_provider)
    if provider not in OPENAI_SDK_REASONING_PROVIDERS:
        return None

    provider_kind = resolve_reasoning_provider_kind(api_base)
    if provider_kind is None:
        return None

    model = str(model_name or "").strip().lower()
    if model not in SUPPORTED_DEEPSEEK_V4_MODELS:
        return None
    return provider_kind, model


__all__ = [
    "LEVEL_MAPPING",
    "OPENAI_SDK_REASONING_PROVIDERS",
    "SUPPORTED_DEEPSEEK_V4_MODELS",
    "ReasoningEffort",
    "ReasoningLevel",
    "ReasoningProviderKind",
    "normalize_reasoning_level",
    "resolve_reasoning_provider_kind",
    "resolve_reasoning_target",
]
