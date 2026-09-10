# coding: utf-8
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse


ReasoningLevel = Literal["off", "on", "minimal", "low", "medium", "high", "xhigh", "max"]
SUPPORTED_REASONING_LEVELS: tuple[ReasoningLevel, ...] = (
    "off",
    "on",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def _parse_api_base(api_base: str | None):
    value = str(api_base or "").strip()
    if value and "://" not in value:
        value = f"https://{value}"
    return urlparse(value)


# 按 api_base host 识别的 endpoint_profile 覆盖表,来源是用户配置 config.yaml
# 顶层的 endpoint_profile_overrides(host -> profile),源码不内置任何 host。
# 用途:自建网关(如 vLLM 起的 DashScope 风格服务,认 enable_thinking /
# thinking_budget、忽略官方 thinking.type)无法从域名推断方言,用户在配置里
# 声明后,保存/校验/运行时各路径自动补 endpoint_profile 透传给 core。
# 能力档位仍按模型名 fallback,不把端点压成纯开关;显式配置的
# endpoint_profile 始终优先,本表只补空缺。未配置时不推断,思考档位
# 对这类网关静默不生效(与其它未知方言的自定义端点一致)。
def resolve_endpoint_profile_override(api_base: str | None) -> str | None:
    """Return the endpoint_profile implied by a user-configured gateway host."""
    host = (_parse_api_base(api_base).hostname or "").lower()
    if not host:
        return None
    # Local import: keep this module importable in lightweight contexts that
    # do not load (or have) the user workspace config file.
    try:
        from jiuwenswarm.common.config import get_endpoint_profile_overrides

        overrides = get_endpoint_profile_overrides()
    except Exception:
        return None
    return overrides.get(host)


def effective_endpoint_profile(api_base: str | None, endpoint_profile: Any = None) -> str | None:
    """Explicit endpoint_profile wins; otherwise infer from known api_base hosts."""
    explicit = str(endpoint_profile or "").strip()
    if explicit:
        return explicit
    return resolve_endpoint_profile_override(api_base)


def normalize_reasoning_level(raw: Any) -> ReasoningLevel | None:
    if raw is None:
        return None

    key = str(raw).strip().lower()
    if key == "":
        return None
    if key in {"off", "none", "false", "disable", "disabled"}:
        return "off"
    if key in {"on", "true", "enable", "enabled"}:
        return "on"
    if key in {"minimum", "minimal"}:
        return "minimal"
    if key == "low":
        return "low"
    if key in {"medium", "med", "mid"}:
        return "medium"
    if key == "high":
        return "high"
    if key in {"xhigh", "extra_high"}:
        return "xhigh"
    if key in {"max", "maximum", "ultra"}:
        return "max"
    return None


def reasoning_config_for_level(level: ReasoningLevel) -> dict[str, str]:
    """Convert the UI compatibility field into core's provider-neutral config."""
    if level == "off":
        return {"mode": "disabled"}
    if level == "on":
        return {"mode": "enabled"}
    return {"mode": "enabled", "effort": level}


def reasoning_level_options(capability: Any) -> tuple[str, ...]:
    """Return canonical persisted values for a core ReasoningCapability."""
    return tuple(getattr(capability, "options", ()) or ())


def validate_reasoning_level_for_model(
    *,
    raw_level: Any,
    model_name: str,
    model_provider: Any,
    api_base: str | None,
    endpoint_profile: Any = None,
) -> str:
    """Normalize ``reasoning_level`` and validate it against model capability.

    Shared by all channels (web/tui) so every save path enforces the same
    core-backed rules. Returns the canonical level, or ``""`` when the raw
    value is empty (= use vendor default). Raises ``ValueError`` with a
    user-facing message for unknown or unsupported values.
    """
    # 仅 None/空白视为"未设置"。不能写 `raw_level or ""`：legacy 配置里裸写的
    # off 会被 YAML 1.1 加载器读成布尔 False，若按 falsy 短路会被当成未设置而
    # 静默清空；这里让 False 走 normalize 归一化为 "off"（True 同理为 "on"）。
    raw = "" if raw_level is None else str(raw_level).strip()
    if not raw:
        return ""
    level = normalize_reasoning_level(raw)
    if level is None:
        raise ValueError(
            f"reasoning_level is invalid; canonical values: {', '.join(SUPPORTED_REASONING_LEVELS)}"
        )
    provider_name = getattr(model_provider, "value", model_provider)
    protocol = "anthropic" if str(provider_name or "").strip().lower() == "anthropic" else "openai"
    try:
        # Local import: keep this module importable in lightweight contexts that
        # only need normalization helpers without loading the core LLM stack.
        from openjiuwen.core.foundation.llm import get_reasoning_capability

        capability = get_reasoning_capability(
            provider=model_provider,
            model=model_name,
            protocol=protocol,
            api_base=api_base or "",
            endpoint_profile=effective_endpoint_profile(api_base, endpoint_profile),
        )
    except Exception as exc:
        # 能力查询是静态表 lookup，正常不会抛；一旦抛（典型是 swarm 与 core
        # 版本漂移导致的 ImportError）统一转成 ValueError，让保存路径返回
        # 可读的校验错误（BAD_REQUEST）而不是 500。
        raise ValueError(
            f"reasoning_level cannot be validated for model '{model_name}': {exc}"
        ) from exc
    allowed = reasoning_level_options(capability)
    if level not in allowed:
        allowed_display = ", ".join(allowed) or "default only"
        raise ValueError(f"reasoning_level must be one of: {allowed_display}")
    return level


__all__ = [
    "ReasoningLevel",
    "SUPPORTED_REASONING_LEVELS",
    "effective_endpoint_profile",
    "normalize_reasoning_level",
    "reasoning_config_for_level",
    "reasoning_level_options",
    "resolve_endpoint_profile_override",
    "validate_reasoning_level_for_model",
]
