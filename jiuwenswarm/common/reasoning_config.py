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

# 新声明下 client_provider 只剩 OpenAI/Anthropic，DeepSeek/DashScope 改由
# endpoint_profile 表达。推理注入的第一道门控改为认 endpoint_profile；
# 下方集合与 OPENAI_SDK_REASONING_PROVIDERS 同义，保留旧名供 client_provider 兼容。
OPENAI_SDK_REASONING_PROFILES = {
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


# 部分厂商对采样参数有硬性约束(传错值直接 400)。Moonshot(api.moonshot.cn)
# 的 kimi-k2.6 实测要求 temperature=1、top_p=0.95(报错 "only 1 is allowed for
# this model" / "only 0.95 is allowed for this model")。core 的 ModelRequestConfig
# 默认 temperature=0.95 正好踩雷,故在此按 api_base 识别后强制覆盖,且无视用户
# 填值——因为传任何其它值都必失败,无协商余地。Moonshot 无专属 endpoint_profile,
# 与 deepseek_official 一样靠 api_base host 识别。约束带 "for this model" 字样
# 可能按模型配置,但对无约束的 moonshot 模型固定到安全值不会报错,故按厂商识别即可。
SAMPLING_OVERRIDE_RULES: dict[str, dict[str, float]] = {
    "api.moonshot.cn": {"temperature": 1.0, "top_p": 0.95},
}


def resolve_sampling_override(api_base: str | None) -> dict[str, float] | None:
    """Return forced sampling params for hosts that reject defaults, else None.

    Some vendors reject the SDK's default sampling values with HTTP 400.
    Moonshot's kimi-k2.6 requires exactly temperature=1 and top_p=0.95; the
    core default temperature=0.95 trips this. Identify by api_base host (no
    dedicated endpoint_profile exists) and force the values, overriding
    anything the user supplied — any other value is guaranteed to fail.
    """
    host = (_parse_api_base(api_base).hostname or "").lower()
    return SAMPLING_OVERRIDE_RULES.get(host)


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
    client_provider: Any = None,
    api_base: str | None,
    model_name: str | None,
    endpoint_profile: Any = None,
) -> tuple[ReasoningProviderKind, str] | None:
    # 新声明下优先认 endpoint_profile(deepseek/dashscope/openai)；
    # 兼容旧 client_provider 名(DeepSeek/DashScope/OpenAI)。
    profile = str(endpoint_profile or "").strip().lower() if endpoint_profile is not None else ""
    if profile:
        if profile not in OPENAI_SDK_REASONING_PROFILES:
            return None
    else:
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
    "OPENAI_SDK_REASONING_PROFILES",
    "SAMPLING_OVERRIDE_RULES",
    "SUPPORTED_DEEPSEEK_V4_MODELS",
    "ReasoningEffort",
    "ReasoningLevel",
    "ReasoningProviderKind",
    "normalize_reasoning_level",
    "resolve_reasoning_provider_kind",
    "resolve_reasoning_target",
    "resolve_sampling_override",
]
