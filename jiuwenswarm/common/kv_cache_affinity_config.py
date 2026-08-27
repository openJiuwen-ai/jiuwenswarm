# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical configuration and provider rules for KV cache affinity."""

from __future__ import annotations

import logging
import os
from typing import Any

from openjiuwen.core.foundation.kv_cache import KVCacheAffinityConfig

ASCEND_AFFINITY_PROVIDER = "AscendAffinity"
logger = logging.getLogger(__name__)
KVC_CONFIG_KEYS = frozenset(
    {
        "kv_cache_affinity_enabled",
        "kv_cache_release_enabled",
        "model_provider",
    }
)


def _kv_cache_mode(config_like: Any) -> str:
    """读 model_client_config 的 extensions.kv_cache.mode(归一小写)。

    兼容 dict 与对象：dict 取 ``extensions`` 子键；对象取属性链。
    新声明下 KV 亲和/释放由 ``extensions.kv_cache.mode`` 表达
    (release/affinity/none)，不再用 client_provider=AscendAffinity 判别。
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


def is_kv_cache_release_config(config_like: Any) -> bool:
    """新式判别：extensions.kv_cache.mode == 'release'。"""
    return _kv_cache_mode(config_like) == "release"


def normalize_provider(provider: Any) -> str:
    """Return one stable provider name from enums, strings or missing values."""

    value = getattr(provider, "value", provider)
    return str(value or "").strip()


def model_provider(model: Any | None) -> str:
    """Resolve the effective provider from an OpenJiuwen Model or its client."""

    for owner in (model, getattr(model, "_client", None)):
        model_client_config = getattr(owner, "model_client_config", None)
        provider = getattr(model_client_config, "client_provider", None)
        if provider is not None:
            return normalize_provider(provider)
    return ""


def build_kv_cache_affinity_config(
    react_config: dict[str, Any] | None,
    *,
    provider: str,
    model_client_config: Any = None,
) -> KVCacheAffinityConfig:
    """Build the shared Agent/Team KVC policy and fail closed by mode.

    新声明下 KV 亲和由 ``extensions.kv_cache.mode=affinity`` 表达，不再用
    client_provider=AscendAffinity 判别。本函数优先认 mode，同时兼容旧
    provider 名(AscendAffinity)配置。
    """

    react_config = react_config or {}
    raw = react_config.get("kv_cache_affinity_config")
    raw = raw if isinstance(raw, dict) else {}
    affinity_enabled = bool(raw.get("enable_kv_cache_affinity", False))

    # 新式判别：mcc 带 extensions.kv_cache.mode=affinity 视为具备亲和能力。
    mode_ok = is_kv_cache_affinity_config(model_client_config) if model_client_config is not None else False
    # 兼容旧配置：client_provider 仍是 AscendAffinity 别名(core 内部归一)。
    normalized_provider = normalize_provider(provider)
    legacy_ok = normalized_provider == ASCEND_AFFINITY_PROVIDER
    if affinity_enabled and not (mode_ok or legacy_ok):
        logger.warning(
            "KV cache affinity failed closed: model provider=%s mode=%s "
            "requires extensions.kv_cache.mode=affinity (or legacy AscendAffinity)",
            normalized_provider or "<empty>",
            _kv_cache_mode(model_client_config) or "<empty>",
        )
        affinity_enabled = False
    return KVCacheAffinityConfig(
        enable_kv_cache_release=bool(
            raw.get("enable_kv_cache_release", False)
        ),
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


def default_model_provider_from_entries(models: list[dict[str, Any]]) -> str:
    entry = select_default_model_entry(models)
    if entry is None:
        return ""
    model_client_config = entry.get("model_client_config") or {}
    return str(model_client_config.get("client_provider") or "").strip()


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


def get_default_model_provider(config: dict[str, Any] | None) -> str:
    """Return the effective default provider without constructing a Model."""
    config = config if isinstance(config, dict) else {}
    models = config.get("models")
    models = models if isinstance(models, dict) else {}
    entries = models.get("defaults")
    if isinstance(entries, list) and entries:
        return default_model_provider_from_entries(entries)
    else:
        target = models.get("default")
        if isinstance(target, dict):
            model_client_config = target.get("model_client_config")
            if isinstance(model_client_config, dict):
                return str(
                    model_client_config.get("client_provider") or ""
                ).strip()

    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    model_client_config = react.get("model_client_config")
    if isinstance(model_client_config, dict):
        provider = str(model_client_config.get("client_provider") or "").strip()
        if provider:
            return provider
    return str(os.getenv("MODEL_PROVIDER", "")).strip()


def is_affinity_enabled(config: dict[str, Any] | None) -> bool:
    config = config if isinstance(config, dict) else {}
    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    kv_config = react.get("kv_cache_affinity_config")
    kv_config = kv_config if isinstance(kv_config, dict) else {}
    return bool(kv_config.get("enable_kv_cache_affinity", False))


def validate_affinity_invariant(
    config: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    config = config if isinstance(config, dict) else {}
    react = config.get("react")
    react = react if isinstance(react, dict) else {}
    kv_config = react.get("kv_cache_affinity_config")
    kv_config = kv_config if isinstance(kv_config, dict) else {}
    if not bool(kv_config.get("enable_kv_cache_affinity", False)):
        return True, []

    failures: list[str] = []
    if bool(kv_config.get("enable_kv_cache_release", False)):
        failures.append("enable_kv_cache_release must be false")
    provider = get_default_model_provider(config)
    if provider != ASCEND_AFFINITY_PROVIDER:
        failures.append(
            f"default provider must be {ASCEND_AFFINITY_PROVIDER}, "
            f"got {provider or '<empty>'}"
        )
    return not failures, failures


def normalize_affinity_request(params: dict[str, Any]) -> None:
    """Enforce switch/provider consistency on one mutable request payload."""
    release_enabled = parse_bool(params.get("kv_cache_release_enabled"))
    affinity_enabled = parse_bool(params.get("kv_cache_affinity_enabled"))
    if release_enabled and affinity_enabled:
        raise ValueError(
            "kv_cache_release_enabled and kv_cache_affinity_enabled "
            "cannot both be true"
        )

    requested_provider = str(params.get("model_provider") or "").strip()
    if affinity_enabled:
        if requested_provider and requested_provider != ASCEND_AFFINITY_PROVIDER:
            params["kv_cache_affinity_enabled"] = "false"
        else:
            params["model_provider"] = ASCEND_AFFINITY_PROVIDER
    elif requested_provider and requested_provider != ASCEND_AFFINITY_PROVIDER:
        params.setdefault("kv_cache_affinity_enabled", "false")
