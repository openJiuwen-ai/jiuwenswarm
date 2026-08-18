# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session lifecycle hooks for Ascend KV cache affinity."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from openjiuwen.core.foundation.kv_cache import resolve_kvc_action_timeout

from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs

logger = logging.getLogger(__name__)

KVAction = Literal["evict", "offload", "prefetch"]
ASCEND_AFFINITY_PROVIDER = "AscendAffinity"


@dataclass(frozen=True)
class KVCacheLifecycleResult:
    status: Literal["ok", "failed", "skipped", "scheduled"]

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def scheduled(self) -> bool:
        return self.status == "scheduled"


def _result(
    status: Literal["ok", "failed", "skipped", "scheduled"],
) -> KVCacheLifecycleResult:
    return KVCacheLifecycleResult(status=status)


def is_kv_cache_affinity_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether jiuwenswarm should emit Ascend KV lifecycle calls."""
    cfg = config if isinstance(config, dict) else get_config()
    react_cfg = cfg.get("react") or {}
    kv_cfg = react_cfg.get("kv_cache_affinity_config") or {}
    return bool(kv_cfg.get("enable_kv_cache_affinity", False))


def _model_supports_kv_cache_affinity(model: Any) -> bool:
    supports = getattr(model, "supports_kv_cache_affinity", None)
    if callable(supports):
        return bool(supports())
    return all(
        callable(getattr(model, name, None))
        for name in ("evict_kvc", "offload_kvc", "prefetch_kvc")
    )


def _normalize_provider(provider: Any) -> str:
    if provider is None:
        return ""
    value = getattr(provider, "value", provider)
    return str(value or "").strip()


def _is_ascend_affinity_provider(provider: Any) -> bool:
    return _normalize_provider(provider).lower() == ASCEND_AFFINITY_PROVIDER.lower()


def _provider_from_model(model: Any) -> str:
    model_client_config = getattr(model, "model_client_config", None)
    return _normalize_provider(getattr(model_client_config, "client_provider", None))


def _default_model_provider(config: dict[str, Any]) -> str:
    entry = _default_model_entry(config)
    if entry is None:
        return ""
    model_client_config, _ = entry
    return _normalize_provider(model_client_config.get("client_provider"))


def _model_from_jiuwenswarm_agent(agent: Any) -> Any | None:
    if agent is None:
        return None

    adapter = getattr(agent, "_adapter", None)
    model = getattr(adapter, "_model", None) if adapter is not None else None
    if model is not None:
        return model

    get_instance = getattr(agent, "get_instance", None)
    if callable(get_instance):
        try:
            instance = get_instance()
        except Exception:
            instance = None
        if instance is not None:
            deep_config = getattr(instance, "deep_config", None)
            model = getattr(deep_config, "model", None) if deep_config is not None else None
            if model is not None:
                return model

            react_agent = getattr(instance, "react_agent", None)
            model = getattr(react_agent, "_llm", None) if react_agent is not None else None
            if model is not None:
                return model

    return None


def _default_model_entry(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    defaults = get_default_models(config)
    for entry in defaults:
        if isinstance(entry, dict) and entry.get("is_default") is True:
            return dict(entry.get("model_client_config") or {}), dict(entry.get("model_config_obj") or {})
    if defaults and isinstance(defaults[0], dict):
        return dict(defaults[0].get("model_client_config") or {}), dict(defaults[0].get("model_config_obj") or {})

    model_cfg = (config.get("models") or {}).get("default") or {}
    react_cfg = config.get("react") or {}
    mcc = dict(model_cfg.get("model_client_config") or react_cfg.get("model_client_config") or {})
    if not mcc:
        return None
    if "model_name" not in mcc:
        mcc["model_name"] = react_cfg.get("model_name") or "gpt-4"
    mco = dict(model_cfg.get("model_config_obj") or react_cfg.get("model_config_obj") or {})
    return mcc, mco


def _build_default_model(config: dict[str, Any]) -> Any | None:
    entry = _default_model_entry(config)
    if entry is None:
        return None

    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

    model_client_config, model_config_obj = entry
    model_name = str(model_client_config.get("model_name") or "").strip()
    mcc_fields = {k: v for k, v in model_client_config.items() if k != "model_name"}
    if not mcc_fields.get("client_provider"):
        mcc_fields["client_provider"] = "OpenAI"
    request_config = ModelRequestConfig(
        **build_reasoning_model_request_kwargs(
            model_client_config=mcc_fields,
            model_config_obj=model_config_obj,
            model_name=model_name,
        )
    )
    return Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=request_config)


def resolve_kv_cache_affinity_model(
    *,
    agent: Any = None,
    config: dict[str, Any] | None = None,
) -> Any | None:
    model = _model_from_jiuwenswarm_agent(agent)
    if model is not None:
        return model
    return _build_default_model(config if isinstance(config, dict) else get_config())


async def run_session_kv_cache_lifecycle(
    action: KVAction,
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    config: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    """Run a session-level KV lifecycle action when Ascend affinity is enabled."""
    normalized_session_id = ""
    try:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return _result("skipped")

        cfg = config if isinstance(config, dict) else get_config()
        if not is_kv_cache_affinity_enabled(cfg):
            return _result("skipped")

        model = resolve_kv_cache_affinity_model(agent=agent, config=cfg)
        provider = _provider_from_model(model) if model is not None else _default_model_provider(cfg)
        if provider and not _is_ascend_affinity_provider(provider):
            logger.warning(
                "[KVCacheAffinityLifecycle] %s blocked: session_id=%s provider=%s requires=%s",
                action,
                normalized_session_id,
                provider,
                ASCEND_AFFINITY_PROVIDER,
            )
            return _result("failed")
        if model is None:
            return _result("skipped")
        if not _model_supports_kv_cache_affinity(model):
            return _result("skipped")

        action_fn = getattr(model, f"{action}_kvc", None)
        if not callable(action_fn):
            return _result("skipped")

        if action == "evict":
            previous = _BACKGROUND_TAILS.get(normalized_session_id)
            if previous is not None and previous is not asyncio.current_task():
                await asyncio.gather(asyncio.shield(previous), return_exceptions=True)

        action_timeout = resolve_kvc_action_timeout(action, "session", timeout)
        action_call = action_fn(
            target="session",
            session_id=normalized_session_id,
            parent_session_id=parent_session_id or normalized_session_id,
            timeout=action_timeout,
        )
        # OpenAIModelClient owns the whole-action deadline when kv_cache.mode=affinity (including
        # retries/backoff). Product lifecycle code only selects and forwards
        # the shared budget so timeout ownership remains unambiguous.
        action_result = await action_call
        ok = bool(action_result)
    except Exception as exc:
        logger.warning(
            "[KVCacheAffinityLifecycle] %s failed: session_id=%s error=%s",
            action,
            normalized_session_id,
            exc,
        )
        return _result("failed")

    if not ok:
        logger.warning(
            "[KVCacheAffinityLifecycle] %s failed: session_id=%s reason=provider_returned_false",
            action,
            normalized_session_id,
        )
        return _result("failed")
    logger.info(
        "[KVCacheAffinityLifecycle] %s ok: session_id=%s parent_session_id=%s",
        action,
        normalized_session_id,
        parent_session_id or normalized_session_id,
    )
    return _result("ok")


_BACKGROUND_ACTIONS: set[asyncio.Task[KVCacheLifecycleResult]] = set()
_BACKGROUND_TAILS: dict[str, asyncio.Task[KVCacheLifecycleResult]] = {}


def dispatch_session_kv_cache_lifecycle(
    action: Literal["offload", "prefetch"],
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    config: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    """Schedule a root signal while retaining and observing its task."""
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return _result("skipped")
    try:
        cfg = config if isinstance(config, dict) else get_config()
        if not is_kv_cache_affinity_enabled(cfg):
            return _result("skipped")
    except Exception as exc:
        logger.warning(
            "[KVCacheAffinityLifecycle] %s dispatch failed: session_id=%s error=%s",
            action,
            normalized_session_id,
            exc,
        )
        return _result("failed")

    previous = _BACKGROUND_TAILS.get(normalized_session_id)

    async def _run_ordered() -> KVCacheLifecycleResult:
        if previous is not None:
            await asyncio.gather(asyncio.shield(previous), return_exceptions=True)
        return await run_session_kv_cache_lifecycle(
            action,
            session_id=normalized_session_id,
            parent_session_id=parent_session_id,
            agent=agent,
            config=cfg,
            timeout=timeout,
        )

    task = asyncio.create_task(
        _run_ordered(),
        name=f"kvc-{action}[{normalized_session_id}]",
    )
    _BACKGROUND_ACTIONS.add(task)
    _BACKGROUND_TAILS[normalized_session_id] = task

    def _consume(done: asyncio.Task[KVCacheLifecycleResult]) -> None:
        _BACKGROUND_ACTIONS.discard(done)
        if _BACKGROUND_TAILS.get(normalized_session_id) is done:
            _BACKGROUND_TAILS.pop(normalized_session_id, None)
        try:
            result = done.result()
            if result.failed:
                logger.warning(
                    "[KVCacheAffinityLifecycle] background %s failed: session_id=%s",
                    action,
                    normalized_session_id,
                )
        except asyncio.CancelledError:
            logger.debug(
                "[KVCacheAffinityLifecycle] background %s cancelled: session_id=%s",
                action,
                normalized_session_id,
            )
        except Exception as exc:
            logger.warning(
                "[KVCacheAffinityLifecycle] background %s raised: session_id=%s error=%s",
                action,
                normalized_session_id,
                exc,
            )

    task.add_done_callback(_consume)
    return _result("scheduled")


async def cancel_pending_kv_cache_lifecycle_tasks() -> None:
    tasks = tuple(_BACKGROUND_ACTIONS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _BACKGROUND_TAILS.clear()


async def prefetch_session_kv_cache(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    return await run_session_kv_cache_lifecycle(
        "prefetch",
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        timeout=timeout,
    )


def dispatch_prefetch_session_kv_cache(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    return dispatch_session_kv_cache_lifecycle(
        "prefetch",
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        timeout=timeout,
    )


async def offload_session_kv_cache(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    return await run_session_kv_cache_lifecycle(
        "offload",
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        timeout=timeout,
    )


def dispatch_offload_session_kv_cache(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    return dispatch_session_kv_cache_lifecycle(
        "offload",
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        timeout=timeout,
    )


async def evict_session_kv_cache(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    agent: Any = None,
    timeout: float | None = None,
) -> KVCacheLifecycleResult:
    return await run_session_kv_cache_lifecycle(
        "evict",
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        timeout=timeout,
    )
