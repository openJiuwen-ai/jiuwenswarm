# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical configuration and provider rules for KV cache affinity."""

from __future__ import annotations

import logging
import os
from typing import Any

from openjiuwen.core.kv_cache import KVCacheAffinityConfig

ASCEND_AFFINITY_PROVIDER = "AscendAffinity"
APPLICATION_KV_CACHE_CONFIG_KEY = "kv_cache_affinity_config"
KV_CACHE_AFFINITY_ENABLED_KEY = "enable_kv_cache_affinity"
logger = logging.getLogger(__name__)
KVC_CONFIG_KEYS = frozenset(
    {
        "kv_cache_affinity_enabled",
        "model_provider",
    }
)


def _kv_cache_mode(config_like: Any) -> str:
    """读 model_client_config 的 extensions.kv_cache.mode(归一小写)。

    兼容 dict 与对象：dict 取 ``extensions`` 子键；对象取属性链。
    KV 亲和的正式声明是 ``extensions.kv_cache.mode``
    (affinity/none)；旧 ``client_provider=AscendAffinity`` 由统一能力
    判定函数兼容，不在这里混入 provider 规则。
    """
    try:
        if isinstance(config_like, dict):
            kv = (config_like.get("extensions") or {}).get("kv_cache") or {}
            return str(kv.get("mode") or "").strip().lower()
        extensions = getattr(config_like, "extensions", None)
        kv = getattr(extensions, "kv_cache", None)
        return str(getattr(kv, "mode", "") or "").strip().lower()
    except Exception:
        return ""


def is_kv_cache_affinity_config(config_like: Any) -> bool:
    """新式判别：extensions.kv_cache.mode == 'affinity'。"""
    return _kv_cache_mode(config_like) == "affinity"


def normalize_provider(provider: Any) -> str:
    """Return one stable provider name from enums, strings or missing values."""

    value = getattr(provider, "value", provider)
    return str(value or "").strip()


def _model_client_provider(config: Any) -> str:
    """Resolve the service identity before transport normalization.

    ``agent-core`` may normalize legacy providers such as DeepSeek or
    OpenRouter to the OpenAI-compatible transport while retaining the
    original value in ``legacy_client_provider``.  The original provider is
    the value callers need for policy and diagnostics.
    """

    if isinstance(config, dict):
        legacy_provider = config.get("legacy_client_provider")
        provider = config.get("client_provider")
    else:
        legacy_provider = getattr(config, "legacy_client_provider", None)
        provider = getattr(config, "client_provider", None)
    if legacy_provider is not None:
        normalized_legacy = normalize_provider(legacy_provider)
        if normalized_legacy:
            return normalized_legacy
    return normalize_provider(provider)


def has_kv_cache_affinity_capability(
    model_client_config: Any = None,
    *,
    provider: Any = None,
) -> bool:
    """Return whether one endpoint declares affinity capability.

    ``extensions.kv_cache.mode=affinity`` is the canonical declaration.
    ``AscendAffinity`` remains a presentation and legacy configuration alias.
    """

    effective_provider = normalize_provider(provider) or _model_client_provider(
        model_client_config
    )
    return (
        is_kv_cache_affinity_config(model_client_config)
        or effective_provider == ASCEND_AFFINITY_PROVIDER
    )


def model_provider(model: Any | None) -> str:
    """Resolve the effective provider from an OpenJiuwen Model or its client."""

    for owner in (model, getattr(model, "_client", None)):
        provider = _model_client_provider(
            getattr(owner, "model_client_config", None)
        )
        if provider:
            return provider
    return ""


def build_kv_cache_affinity_config(
    application_config: dict[str, Any] | None,
    *,
    provider: str,
    model_client_config: Any = None,
) -> KVCacheAffinityConfig:
    """Build the shared Agent/Team KVC policy and fail closed by mode.

    新声明下 KV 亲和由 ``extensions.kv_cache.mode=affinity`` 表达。
    本函数以 mode 为正式能力声明，同时兼容旧 provider 名
    ``AscendAffinity``。
    """

    affinity_enabled = is_affinity_enabled(application_config)

    normalized_provider = normalize_provider(provider)
    if affinity_enabled and not has_kv_cache_affinity_capability(
        model_client_config,
        provider=normalized_provider,
    ):
        logger.warning(
            "KV cache affinity failed closed: model provider=%s mode=%s "
            "requires extensions.kv_cache.mode=affinity (or legacy AscendAffinity)",
            normalized_provider or "<empty>",
            _kv_cache_mode(model_client_config) or "<empty>",
        )
        affinity_enabled = False
    return KVCacheAffinityConfig(
        enable_kv_cache_affinity=affinity_enabled,
    )


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def select_default_model_entry(
    models: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for entry in models:
        if isinstance(entry, dict) and entry.get("is_default") is True:
            return entry
    return next((entry for entry in models if isinstance(entry, dict)), None)


def default_model_client_config_from_entries(
    models: list[dict[str, Any]],
) -> dict[str, Any] | None:
    entry = select_default_model_entry(models)
    if entry is None:
        return None
    model_client_config = entry.get("model_client_config")
    return model_client_config if isinstance(model_client_config, dict) else None


def set_default_model_provider_in_entries(
    models: list[dict[str, Any]],
    provider: str,
) -> bool:
    entry = select_default_model_entry(models)
    if entry is None:
        return False
    model_client_config = entry.setdefault("model_client_config", {})
    if not isinstance(model_client_config, dict):
        model_client_config = {}
        entry["model_client_config"] = model_client_config
    if model_client_config.get("client_provider") == provider:
        return False
    model_client_config["client_provider"] = provider
    return True


def get_default_model_client_config(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the effective default ModelClientConfig without creating a Model."""

    config = config if isinstance(config, dict) else {}
    models = config.get("models")
    models = models if isinstance(models, dict) else {}
    entries = models.get("defaults")
    if isinstance(entries, list) and entries:
        return default_model_client_config_from_entries(entries) or {}
    else:
        target = models.get("default")
        if isinstance(target, dict):
            model_client_config = target.get("model_client_config")
            if isinstance(model_client_config, dict):
                return model_client_config

    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    model_client_config = react.get("model_client_config")
    if isinstance(model_client_config, dict):
        return model_client_config
    return {"client_provider": str(os.getenv("MODEL_PROVIDER", "")).strip()}


def get_default_model_provider(config: dict[str, Any] | None) -> str:
    """Return the effective default provider without constructing a Model."""

    return _model_client_provider(get_default_model_client_config(config))


def is_affinity_enabled(config: dict[str, Any] | None) -> bool:
    kv_config = get_kv_cache_affinity_application_config(config)
    return bool(kv_config.get(KV_CACHE_AFFINITY_ENABLED_KEY, False))


def get_kv_cache_affinity_application_config(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return Application KVC policy, with legacy ReAct fallback."""
    config = config if isinstance(config, dict) else {}
    canonical = config.get(APPLICATION_KV_CACHE_CONFIG_KEY)
    if isinstance(canonical, dict) and KV_CACHE_AFFINITY_ENABLED_KEY in canonical:
        return canonical

    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    legacy = react.get(APPLICATION_KV_CACHE_CONFIG_KEY)
    return legacy if isinstance(legacy, dict) else {}


def validate_affinity_invariant(
    config: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    config = config if isinstance(config, dict) else {}
    if not is_affinity_enabled(config):
        return True, []

    failures: list[str] = []
    model_client_config = get_default_model_client_config(config)
    provider = _model_client_provider(model_client_config)
    if not has_kv_cache_affinity_capability(model_client_config):
        failures.append(
            "default model must declare extensions.kv_cache.mode=affinity "
            f"or use legacy provider {ASCEND_AFFINITY_PROVIDER}; "
            f"got provider={provider or '<empty>'} "
            f"mode={_kv_cache_mode(model_client_config) or '<empty>'}"
        )
    return not failures, failures


def normalize_affinity_request(params: dict[str, Any]) -> None:
    """Enforce switch/provider consistency on one mutable request payload."""
    affinity_enabled = parse_bool(params.get("kv_cache_affinity_enabled"))
    requested_provider = str(params.get("model_provider") or "").strip()
    if affinity_enabled:
        if requested_provider and requested_provider != ASCEND_AFFINITY_PROVIDER:
            params["kv_cache_affinity_enabled"] = "false"
        else:
            params["model_provider"] = ASCEND_AFFINITY_PROVIDER
    elif requested_provider and requested_provider != ASCEND_AFFINITY_PROVIDER:
        params.setdefault("kv_cache_affinity_enabled", "false")
