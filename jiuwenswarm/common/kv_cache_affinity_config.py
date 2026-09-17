# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure configuration rules for Ascend KV cache affinity."""

from __future__ import annotations

import os
from typing import Any

ASCEND_AFFINITY_PROVIDER = "AscendAffinity"
KVC_CONFIG_KEYS = frozenset(
    {
        "kv_cache_affinity_enabled",
        "kv_cache_release_enabled",
        "model_provider",
    }
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
