# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm configuration bridge for the shared KVC runtime."""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from jiuwenswarm.common.config import get_agentos_models, get_config, get_default_models
from jiuwenswarm.common.kv_cache_affinity_config import (
    has_kv_cache_affinity_capability,
    is_affinity_enabled,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs


def is_kv_cache_affinity_enabled(config: dict[str, Any] | None = None) -> bool:
    cfg = config if isinstance(config, dict) else get_config()
    return is_affinity_enabled(cfg)


def _stable_config_value(value: Any) -> str:
    """Return a comparable representation for nested routing configuration."""

    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return repr(value)


class _KVCacheTargetIdentity(NamedTuple):
    """Stable identity of one physical KVC control target."""

    provider: str
    api_base: str
    model_name: str
    endpoint_profile: str
    api_key: str
    custom_headers: str
    kv_cache: str


def _kv_cache_target_identity(
    client: dict[str, Any],
) -> _KVCacheTargetIdentity:
    """Identify one physical KVC control target, independent of config source."""

    extensions = client.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    return _KVCacheTargetIdentity(
        provider=str(
            client.get("legacy_client_provider") or client.get("client_provider") or ""
        ).strip(),
        api_base=str(client.get("api_base") or "").strip().rstrip("/"),
        model_name=str(client.get("model_name") or "").strip(),
        endpoint_profile=str(client.get("endpoint_profile") or "").strip(),
        api_key=str(client.get("api_key") or ""),
        custom_headers=_stable_config_value(client.get("custom_headers") or {}),
        kv_cache=_stable_config_value(extensions.get("kv_cache") or {}),
    )


def _select_unique_affinity_entry(
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select one physical affinity target without guessing between targets."""

    targets: dict[_KVCacheTargetIdentity, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        client = entry.get("model_client_config") or {}
        if not isinstance(client, dict) or not has_kv_cache_affinity_capability(client):
            continue
        targets.setdefault(_kv_cache_target_identity(client), entry)
    if len(targets) != 1:
        return None
    return next(iter(targets.values()))


def create_default_kv_cache_model():
    """Build a fallback from the one unambiguous KVC control target.

    AgentOS model configuration is authoritative for KVC control. Generic
    defaults are considered only when no valid ``models.agentos`` entry exists.
    This does not change which model normal inference selects.
    """
    config = get_config()
    agentos_entries = get_agentos_models(config)
    candidates = agentos_entries or get_default_models(config)
    entry = _select_unique_affinity_entry(candidates)
    if entry is None:
        return None
    client = dict(entry.get("model_client_config") or {})
    request = dict(entry.get("model_config_obj") or {})

    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )

    model_name = str(client.pop("model_name", "") or "").strip()
    client.setdefault("client_provider", "OpenAI")
    model = Model(
        model_client_config=ModelClientConfig(**client),
        model_config=ModelRequestConfig(
            **build_reasoning_model_request_kwargs(
                model_client_config=client,
                model_config_obj=request,
                model_name=model_name,
            )
        ),
    )
    supports = getattr(model, "supports_kv_cache_affinity", None)
    return model if callable(supports) and supports() else None


__all__ = ["create_default_kv_cache_model", "is_kv_cache_affinity_enabled"]
