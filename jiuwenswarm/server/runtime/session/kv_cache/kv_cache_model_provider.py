# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm configuration bridge for the shared KVC runtime."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.kv_cache_affinity_config import is_affinity_enabled
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs


def is_kv_cache_affinity_enabled(config: dict[str, Any] | None = None) -> bool:
    cfg = config if isinstance(config, dict) else get_config()
    return is_affinity_enabled(cfg)


def create_default_kv_cache_model():
    """Build the configured fallback model for a historical Session."""
    config = get_config()
    defaults = get_default_models(config)
    entry = next(
        (
            item
            for item in defaults
            if isinstance(item, dict) and item.get("is_default") is True
        ),
        defaults[0] if defaults and isinstance(defaults[0], dict) else None,
    )
    if entry is not None:
        client = dict(entry.get("model_client_config") or {})
        request = dict(entry.get("model_config_obj") or {})
    else:
        model_config = (config.get("models") or {}).get("default") or {}
        react_config = config.get("react") or {}
        client = dict(
            model_config.get("model_client_config")
            or react_config.get("model_client_config")
            or {}
        )
        request = dict(
            model_config.get("model_config_obj")
            or react_config.get("model_config_obj")
            or {}
        )
    if not client:
        return None

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
