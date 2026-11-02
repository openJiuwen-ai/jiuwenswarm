"""TTSE rail 构建、同步、注册与后台清理。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import (
    _get_ttse_config,
    coerce_config_bool,
    get_ttse_embedding_config,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _ttse_consult_knobs,
)

logger = logging.getLogger(__name__)


def _merge_ttse_config(runtime_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Yaml ``react.ttse`` plus runtime overlay (runtime keys win).

    Same merge used by TTSERail mount and ``ttse_consult`` eager gating so a
    sparse runtime cache (OfficeAce sync snapshot omitting ``ttse``) still
    inherits the on-disk default. An explicit runtime ``enabled: false`` wins.
    """
    # Resolve get_config via interface_deep so unit tests can monkeypatch it.
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    merged: dict[str, Any] = {}
    try:
        yaml_ttse = _get_ttse_config(deep_mod.get_config())
    except Exception:
        yaml_ttse = {}
    if isinstance(yaml_ttse, dict):
        merged.update(yaml_ttse)
    runtime = _get_ttse_config(runtime_config)
    if isinstance(runtime, dict):
        merged.update(runtime)
    return merged


def _ttse_bank_path(adapter) -> str:
    """FACT/TIP bank is always ``workspace/.ttse/bank.json``; not a user knob.

    Uses ``adapter._workspace_dir`` (same tenant root as memory rails). Sync
    and reload do not bind ``_TENANT_JIUWENCLAW_WS_CV``, so
    :func:`get_agent_workspace_dir` would fall back to the default-tenant
    shared workspace and mix FACT/TIP banks across tenants.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    root = (
        Path(adapter._workspace_dir)
        if adapter._workspace_dir
        else deep_mod.get_agent_workspace_dir()
    )
    return str(root / ".ttse" / "bank.json")


def _resolved_ttse_config(adapter, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """User yaml ``react.ttse`` plus adapter cache (runtime cache wins).

    File yaml supplies defaults. The adapter cache / passed config overlays
    it so a runtime ``enabled: false`` is not clobbered by the on-disk
    default. ``store_path`` is not a user setting; the bank is always under
    workspace. FACT/TIP disclosure is always catalog + ``ttse_consult``.
    """
    return _merge_ttse_config(config if config is not None else adapter._config_cache)


def _build_ttse_rail(adapter, config: dict[str, Any]) -> Any | None:
    """Build TTSERail for FACT/TIP dual-track self-evolution.

    Returns None when agent-core lacks TTSE, construction fails, or
    the feature is unavailable. Does not register the rail.
    """
    # Prefer interface_deep symbols so monkeypatched TTSERail/TTSEConfig apply.
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    TTSERail = deep_mod.TTSERail
    TTSEConfig = deep_mod.TTSEConfig
    if TTSERail is None or TTSEConfig is None:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] TTSERail unavailable: agent-core missing ttse"
        )
        return None
    try:
        from openjiuwen.core.memory.lite.embeddings import OpenAICompatibleEmbeddingProvider

        ttse_cfg = _resolved_ttse_config(adapter, config)
        store_path = _ttse_bank_path(adapter)
        evolve_enabled = coerce_config_bool(ttse_cfg.get("evolve_enabled"), True)
        inject_enabled = coerce_config_bool(ttse_cfg.get("inject_enabled"), True)
        trajectory_export_enabled = coerce_config_bool(
            ttse_cfg.get("trajectory_export_enabled"), False
        )
        trajectory_export_path = str(ttse_cfg.get("trajectory_export_path") or "").strip()
        dream_enabled = coerce_config_bool(ttse_cfg.get("dream_enabled"), True)
        try:
            dream_interval = int(ttse_cfg.get("dream_interval", 50))
        except (TypeError, ValueError):
            dream_interval = 50
        try:
            dream_min_hours = float(ttse_cfg.get("dream_min_hours", 24.0))
        except (TypeError, ValueError):
            dream_min_hours = 24.0
        try:
            dream_ttl_days = int(ttse_cfg.get("dream_ttl_days", 90))
        except (TypeError, ValueError):
            dream_ttl_days = 90
        consult_top_k, consult_retrieve_mode = _ttse_consult_knobs(ttse_cfg)
        try:
            consult_rrf_k = int(ttse_cfg.get("consult_rrf_k", 60) or 60)
        except (TypeError, ValueError):
            consult_rrf_k = 60
        logger.info(
            "[JiuWenSwarmDeepAdapter] TTSEConfig: store_path=%s evolve_enabled=%s "
            "inject_enabled=%s trajectory_export_enabled=%s dream_enabled=%s "
            "dream_interval=%s dream_min_hours=%s dream_ttl_days=%s",
            store_path,
            evolve_enabled,
            inject_enabled,
            trajectory_export_enabled,
            dream_enabled,
            dream_interval,
            dream_min_hours,
            dream_ttl_days,
        )
        emb_cfg = get_ttse_embedding_config({"react": {"ttse": ttse_cfg}})
        embedding = None
        if emb_cfg:
            embedding = OpenAICompatibleEmbeddingProvider(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
        from openjiuwen.extensions.observability.demand import (
            get_trajectory_span_processor,
        )

        trajectory_span_processor = get_trajectory_span_processor()
        if trajectory_span_processor is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] TTSERail create skipped: "
                "TrajectorySpanProcessor unavailable"
            )
            return None
        # TTSERail defaults to SignalBasedSuccessDetector(llm=..., model=..., config=...).
        # Do not construct the detector without those required kwargs.
        ttse_rail = TTSERail(
            llm=adapter._model,
            model=adapter._default_model_name or config.get("model_name", "gpt-4"),
            ttse_config=TTSEConfig(
                store_path=store_path,
                evolve_enabled=evolve_enabled,
                inject_enabled=inject_enabled,
                trajectory_export_enabled=trajectory_export_enabled,
                trajectory_export_path=trajectory_export_path,
                embedding=embedding,
                dream_enabled=bool(dream_enabled),
                dream_interval=dream_interval,
                dream_min_hours=dream_min_hours,
                dream_ttl_days=dream_ttl_days,
                consult_top_k=consult_top_k,
                consult_retrieve_mode=consult_retrieve_mode,
                consult_rrf_k=consult_rrf_k,
            ),
            trajectory_span_processor=trajectory_span_processor,
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] TTSERail create success, "
            "store_path=%s evolve_enabled=%s inject_enabled=%s has_embedding=%s",
            store_path,
            evolve_enabled,
            inject_enabled,
            embedding is not None,
        )
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] TTSERail create failed: %s", exc)
        ttse_rail = None
    return ttse_rail


def _sync_ttse_rail_config(adapter, config: dict[str, Any] | None = None) -> None:
    """Refresh live TTSERail flags/store from current react.ttse (no remount)."""
    rail = adapter._ttse_rail
    if rail is None:
        return
    ttse_cfg = _resolved_ttse_config(adapter, config)
    store_path = _ttse_bank_path(adapter)
    evolve_enabled = coerce_config_bool(ttse_cfg.get("evolve_enabled"), True)
    inject_enabled = coerce_config_bool(ttse_cfg.get("inject_enabled"), True)
    trajectory_export_enabled = coerce_config_bool(
        ttse_cfg.get("trajectory_export_enabled"), False
    )
    trajectory_export_path = str(ttse_cfg.get("trajectory_export_path") or "").strip()
    apply_config = getattr(rail, "apply_runtime_config", None)
    if callable(apply_config):
        # Older agent-core apply_runtime_config only accepts store/evolve/inject.
        # Trajectory export flags are synced via _ttse_config below.
        apply_config(
            store_path=store_path,
            evolve_enabled=evolve_enabled,
            inject_enabled=inject_enabled,
        )
    cfg_obj = getattr(rail, "_ttse_config", None)
    if cfg_obj is not None:
        cfg_obj.trajectory_export_enabled = trajectory_export_enabled
        cfg_obj.trajectory_export_path = trajectory_export_path
        consult_top_k, consult_retrieve_mode = _ttse_consult_knobs(ttse_cfg)
        for attr, value in (
            ("consult_top_k", consult_top_k),
            ("consult_retrieve_mode", consult_retrieve_mode),
        ):
            if hasattr(cfg_obj, attr):
                setattr(cfg_obj, attr, value)
        store = getattr(rail, "_ttse_store", None)
        store_cfg = getattr(store, "_config", None) if store is not None else None
        if store_cfg is not None and store_cfg is not cfg_obj:
            if hasattr(store_cfg, "consult_top_k"):
                store_cfg.consult_top_k = consult_top_k
            if hasattr(store_cfg, "consult_retrieve_mode"):
                store_cfg.consult_retrieve_mode = consult_retrieve_mode
        emb_cfg = get_ttse_embedding_config({"react": {"ttse": ttse_cfg}})
        embedding = None
        if emb_cfg:
            from openjiuwen.core.memory.lite.embeddings import (
                OpenAICompatibleEmbeddingProvider,
            )

            embedding = OpenAICompatibleEmbeddingProvider(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
        if embedding is not None:
            if store is not None:
                attach = getattr(store, "attach_embedding", None)
                if callable(attach):
                    attach(embedding)
            if hasattr(cfg_obj, "embedding"):
                cfg_obj.embedding = embedding
    else:
        logger.debug(
            "[JiuWenSwarmDeepAdapter] TTSERail._ttse_config missing; "
            "trajectory export flags not synced"
        )
    logger.info(
        "[JiuWenSwarmDeepAdapter] TTSERail config synced: "
        "store_path=%s evolve_enabled=%s inject_enabled=%s "
        "trajectory_export_enabled=%s",
        store_path,
        evolve_enabled,
        inject_enabled,
        trajectory_export_enabled,
    )


async def _ensure_ttse_rail_registered(adapter) -> None:
    """Build and register TTSERail when missing; else refresh flags from yaml."""
    if adapter._instance is None:
        return
    if adapter._ttse_rail is not None:
        # Prefer instance methods so monkeypatches on the adapter apply.
        adapter._sync_ttse_rail_config(adapter._config_cache)
        return
    rail = adapter._build_ttse_rail(adapter._config_cache)
    if rail is None:
        return
    await adapter._instance.register_rail(rail)
    adapter._ttse_rail = rail
    logger.info("[JiuWenSwarmDeepAdapter] TTSERail registered for agent mode")


async def _unconfigure_ttse_rail(adapter) -> None:
    """Unregister TTSERail if it is currently mounted."""
    rail = adapter._ttse_rail
    adapter._ttse_rail = None
    if adapter._instance is None or rail is None:
        return
    unregister = getattr(adapter._instance, "unregister_rail", None)
    if not callable(unregister):
        return
    try:
        await unregister(rail)
        logger.info("[JiuWenSwarmDeepAdapter] TTSERail unregistered")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] TTSERail unregister failed: %s", exc)


async def _cleanup_ttse_background_tasks(adapter, rid: str, session_id: str) -> None:
    """Wait for TTSE background induction without draining approval events."""
    rail = adapter._ttse_rail
    if rail is None:
        return
    try:
        cleanup = getattr(rail, "cleanup_background_tasks", None)
        if cleanup is not None:
            await cleanup()
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] TTSE cleanup failed: request_id=%s "
            "session_id=%s error=%s",
            rid,
            session_id,
            exc,
        )


def _on_ttse_cleanup_done(adapter, task: asyncio.Task) -> None:
    """Drop a finished TTSE cleanup task and log a normal failure."""
    adapter._ttse_cleanup_tasks.discard(task)
    try:
        task.result()
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] TTSE cleanup task exception: %s", exc)
