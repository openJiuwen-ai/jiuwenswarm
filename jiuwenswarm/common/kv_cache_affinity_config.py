# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical configuration and provider rules for KV cache affinity."""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openjiuwen.core.kv_cache import KVCacheAffinityConfig

ASCEND_AFFINITY_PROVIDER = "AscendAffinity"
APPLICATION_KV_CACHE_CONFIG_KEY = "kv_cache_affinity_config"
KV_CACHE_AFFINITY_ENABLED_KEY = "enable_kv_cache_affinity"
NO_AFFINITY_MODEL_ERROR = "No configured model declares KV cache affinity support"
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
        provider = _model_client_provider(getattr(owner, "model_client_config", None))
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

    from openjiuwen.core.kv_cache import KVCacheAffinityConfig

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
    return {
        "client_provider": str(os.getenv("MODEL_PROVIDER", "")).strip(),
        "model_name": str(os.getenv("MODEL_NAME", "")).strip(),
    }


def get_default_model_provider(config: dict[str, Any] | None) -> str:
    """Return the effective default provider without constructing a Model."""

    return _model_client_provider(get_default_model_client_config(config))


def _get_agentos_model_client_configs(
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    config = config if isinstance(config, dict) else {}
    models = config.get("models")
    models = models if isinstance(models, dict) else {}
    agentos_entries = models.get("agentos")
    agentos_configs: list[dict[str, Any]] = []
    if isinstance(agentos_entries, list):
        for entry in agentos_entries:
            if not isinstance(entry, dict):
                continue
            model_client_config = entry.get("model_client_config")
            if isinstance(model_client_config, dict) and model_client_config.get(
                "model_name"
            ):
                agentos_configs.append(model_client_config)
    return agentos_configs


def get_kv_cache_control_model_client_configs(
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return configured inference clients, including non-default and AgentOS entries.

    Only named models count; empty template entries cannot admit the switch.
    Keep this read-only helper free of runtime imports for the control service.
    """
    config = config if isinstance(config, dict) else {}
    models = config.get("models")
    models = models if isinstance(models, dict) else {}
    entries = models.get("defaults")
    if isinstance(entries, list) and entries:
        clients = [
            entry.get("model_client_config")
            for entry in entries if isinstance(entry, dict)
        ]
    else:
        clients = [get_default_model_client_config(config)]
    clients.extend(_get_agentos_model_client_configs(config))
    return [
        client for client in clients
        if isinstance(client, dict) and str(client.get("model_name") or "").strip()
    ]


def get_kv_cache_control_model_client_config(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the first configured client for legacy callers; not an admission gate."""

    clients = get_kv_cache_control_model_client_configs(config)
    return clients[0] if clients else {}


def get_kv_cache_control_model_provider(config: dict[str, Any] | None) -> str:
    """Return the first configured provider for legacy callers."""

    return _model_client_provider(get_kv_cache_control_model_client_config(config))


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

    if any(
        has_kv_cache_affinity_capability(client)
        for client in get_kv_cache_control_model_client_configs(config)
    ):
        return True, []
    return False, [NO_AFFINITY_MODEL_ERROR]


def build_application_kv_cache_affinity_config(
    config: dict[str, Any] | None,
) -> KVCacheAffinityConfig:
    """Allow capable calls without letting one selected cloud model veto the app."""
    from openjiuwen.core.kv_cache import KVCacheAffinityConfig

    valid, _ = validate_affinity_invariant(config)
    return KVCacheAffinityConfig(
        enable_kv_cache_affinity=is_affinity_enabled(config) and valid,
    )


def normalize_affinity_request(params: dict[str, Any]) -> None:
    """Normalize the switch without changing the model's connection settings."""
    if "kv_cache_affinity_enabled" in params:
        params["kv_cache_affinity_enabled"] = str(
            parse_bool(params["kv_cache_affinity_enabled"])
        ).lower()


def set_affinity_enabled(config: dict[str, Any], enabled: bool) -> None:
    """Set canonical policy on a candidate; callers own persistence and locking."""
    policy = config.get(APPLICATION_KV_CACHE_CONFIG_KEY)
    if not isinstance(policy, dict):
        policy = {}
        config[APPLICATION_KV_CACHE_CONFIG_KEY] = policy
    policy[KV_CACHE_AFFINITY_ENABLED_KEY] = enabled
    react = config.get("react")
    if isinstance(react, dict):
        react.pop(APPLICATION_KV_CACHE_CONFIG_KEY, None)


def get_affinity_status(config: dict[str, Any]) -> dict[str, Any]:
    """Describe admission, not remote protocol detection or live-task activation."""
    candidate = deepcopy(config)
    set_affinity_enabled(candidate, True)
    valid, _ = validate_affinity_invariant(candidate)
    clients = get_kv_cache_control_model_client_configs(config)
    requested = is_affinity_enabled(config)
    return {
        "requested_enabled": requested,
        "enabled": requested and valid,
        "can_enable": valid,
        "scope": "configured",
        "models": [
            {"name": str(client.get("model_name") or ""),
             "capable": has_kv_cache_affinity_capability(client)}
            for client in clients
        ],
    }


def prepare_model_affinity_update(
    merged: dict[str, Any], current: dict[str, Any], update: dict[str, Any],
) -> None:
    """Validate explicit declarations and revoke inherited endpoint capability."""
    client = merged.get("model_client_config") or {}
    old = current.get("model_client_config") or {}
    patch = update.get("model_client_config") or {}
    for owner in (client, patch):
        extensions = owner.get("extensions")
        if extensions is not None and not isinstance(extensions, dict):
            raise ValueError("model extensions must be an object")
        kv = (extensions or {}).get("kv_cache")
        if kv is not None and not isinstance(kv, dict):
            raise ValueError("model extensions.kv_cache must be an object")
    explicit_mode = _kv_cache_mode(patch)
    changed = bool(current) and any(
        client.get(key) != old.get(key)
        for key in ("api_base", "model_name", "client_provider", "vendor_key", "plan")
    )
    provider = normalize_provider(client.get("client_provider"))
    if changed or explicit_mode == "none":
        client.pop("legacy_client_provider", None)
    if (changed or explicit_mode == "none") and provider == ASCEND_AFFINITY_PROVIDER:
        client["client_provider"] = "OpenAI"
        provider = "OpenAI"
    if changed and not explicit_mode and has_kv_cache_affinity_capability(old):
        extensions = client.get("extensions") or {}
        kv = extensions.get("kv_cache") or {}
        kv["mode"] = "none"
        extensions["kv_cache"] = kv
        client["extensions"] = extensions
    preserving_affinity = (
        not changed and merged.get("source") == current.get("source")
        and has_kv_cache_affinity_capability(old)
    )
    if explicit_mode != "affinity" or preserving_affinity:
        return
    supports_protocol = provider in {"OpenAI", ASCEND_AFFINITY_PROVIDER}
    has_vendor_identity = bool(client.get("vendor_key") or client.get("plan"))
    is_user_model = merged.get("source") in (None, "", "defaults")
    if not supports_protocol or has_vendor_identity or not is_user_model:
        raise ValueError("Affinity requires a custom OpenAI-compatible model endpoint")
