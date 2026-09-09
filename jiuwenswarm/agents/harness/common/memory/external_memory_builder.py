# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Builder for ExternalMemoryRail — single entry point, config-driven.

Dispatches on `memory.external.provider`:
  - openjiuwen  -> OpenJiuwenMemoryProvider (builds its own KV/Vector/DB from config)
  - mem0        -> Mem0MemoryProvider
  - openviking  -> OpenVikingMemoryProvider
  - <plugin>    -> user-installed plugin from ~/.jiuwenswarm/plugins/memory/
  - ""          -> disabled (returns None)

Any failure returns None — the main flow is never blocked.
"""

import logging
import os
from typing import Any, Dict, Optional

from .external_memory_config import (
    build_openjiuwen_provider_config,
    get_external_memory_config,
)

logger = logging.getLogger(__name__)

_BUILTIN_PROVIDERS = {"openjiuwen", "mem0", "openviking"}


def build_external_memory_rail(
    config: Optional[Dict[str, Any]] = None,
    workspace_dir: str = ".",
    session_id: str = "__default__",
    request_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Build an ExternalMemoryRail from config, or None if disabled/failed."""
    try:
        from openjiuwen.harness.rails import ExternalMemoryRail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] ExternalMemoryRail import failed: %s", exc)
        return None

    ext_cfg = get_external_memory_config(config)
    provider_name = ext_cfg.get("provider", "")
    if not provider_name:
        return None

    provider = None
    try:
        if provider_name == "celia":
            celia_source_config = dict(config or {})
            celia_source_config["__celia_workspace_dir"] = workspace_dir
            return _build_celia_rail(
                celia_source_config, ext_cfg, session_id=session_id,
                request_metadata=request_metadata,
            )
        if provider_name == "openjiuwen":
            provider = _build_openjiuwen_provider(ext_cfg, config)
        elif provider_name == "mem0":
            provider = _build_mem0_provider(ext_cfg)
        elif provider_name == "openviking":
            provider = _build_openviking_provider(ext_cfg)
        elif provider_name == "lakebase":
            provider = _build_lakebase_provider(ext_cfg)
        else:
            provider = _load_plugin_provider(provider_name, ext_cfg.get("allowed_plugins") or None)
    except Exception as exc:
        logger.warning(
            "[ExternalMemoryBuilder] build provider '%s' failed: %s",
            provider_name, exc,
        )
        return None

    if provider is None:
        return None

    try:
        rail = ExternalMemoryRail(
            provider,
            user_id=ext_cfg.get("user_id", "__default__"),
            scope_id=ext_cfg.get("scope_id", "__default__"),
            session_id=session_id,
        )
        logger.info(
            "[ExternalMemoryBuilder] ExternalMemoryRail built (provider=%s)",
            provider_name,
        )
        return rail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] rail construction failed: %s", exc)
        return None


def _build_celia_rail(
    config: Dict[str, Any],
    ext_cfg: Dict[str, Any],
    *,
    session_id: str,
    request_metadata: Optional[Dict[str, Any]] = None,
):
    from .celia.config import build_celia_config
    from .celia.provider import CeliaMemoryProvider
    from .celia.rail import CeliaMemoryRail

    celia_config = build_celia_config(
        config,
        ext_cfg,
        workspace_dir=str(config.get("__celia_workspace_dir") or "."),
    )
    provider = CeliaMemoryProvider(
        celia_config,
        user_id=ext_cfg.get("user_id", celia_config.user_id),
        scope_id=ext_cfg.get("scope_id", celia_config.scope_id),
        session_id=session_id,
        request_metadata=request_metadata,
    )
    # Mount prompt and tools even when the backend cannot start. Optional
    # preflight belongs to provider initialization, whose failures the rail handles.
    return CeliaMemoryRail(
        provider,
        user_id=ext_cfg.get("user_id", celia_config.user_id),
        scope_id=ext_cfg.get("scope_id", celia_config.scope_id),
        session_id=session_id,
    )


def _build_openjiuwen_provider(ext_cfg: Dict[str, Any], full_config: Optional[Dict[str, Any]] = None):
    from openjiuwen.core.memory.external.openjiuwen_memory_provider import (
        OpenJiuwenMemoryProvider,
    )
    provider_config, scope_config = build_openjiuwen_provider_config(ext_cfg, full_config)
    return OpenJiuwenMemoryProvider(config=provider_config, scope_config=scope_config)


def _build_mem0_provider(ext_cfg: Dict[str, Any]):
    from openjiuwen.core.memory.external.mem0_provider import Mem0MemoryProvider

    mem0_cfg = ext_cfg.get("mem0") or {}
    api_key = mem0_cfg.get("api_key") or os.environ.get("MEM0_API_KEY", "")
    user_id = mem0_cfg.get("user_id") or os.environ.get("MEM0_USER_ID", "jiuwenswarm-user")
    agent_id = mem0_cfg.get("agent_id") or os.environ.get("MEM0_AGENT_ID", "jiuwenswarm")
    rerank = bool(mem0_cfg.get("rerank", True))

    provider = Mem0MemoryProvider(
        api_key=api_key,
        user_id=user_id,
        agent_id=agent_id,
        rerank=rerank,
    )
    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] Mem0 unavailable (no API key)")
        return None
    return provider


def _build_openviking_provider(ext_cfg: Dict[str, Any]):
    from openjiuwen.core.memory.external.openviking_memory_provider import (
        OpenVikingMemoryProvider,
    )

    vk_cfg = ext_cfg.get("openviking") or {}
    endpoint = vk_cfg.get("endpoint") or os.environ.get("OPENVIKING_ENDPOINT", "")
    api_key = vk_cfg.get("api_key") or os.environ.get("OPENVIKING_API_KEY", "")
    account = vk_cfg.get("account") or os.environ.get("OPENVIKING_ACCOUNT", "root")
    user = vk_cfg.get("user") or os.environ.get("OPENVIKING_USER", "default")

    provider = OpenVikingMemoryProvider(
        endpoint=endpoint,
        api_key=api_key,
        account=account,
        user=user,
    )
    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] OpenViking unavailable (no endpoint)")
        return None
    return provider


def _build_lakebase_provider(ext_cfg: Dict[str, Any]):
    """Build LakeBase (DBay) external memory provider.

    LakeBase provides:
    - Semantic memory storage and retrieval via pgvector
    - Multiple memory types (fact, episode, procedural, etc.)
    - Trait extraction via digest API
    - Multi-workspace support via base switching

    Config shape (memory.external.lakebase):
        api_key: str       # LakeBase API key (required)
        base_url: str      # LakeBase API endpoint (default: localhost:8080)
        base_id: str       # Memory base ID (workspace)
        database_id: str   # Database ID for branching
        timeout: float     # HTTP request timeout
    """
    from openjiuwen.core.memory.external.lakebase_memory_provider import (
        LakeBaseMemoryProvider,
    )

    lb_cfg = ext_cfg.get("lakebase") or {}
    api_key = lb_cfg.get("api_key") or os.environ.get("LAKEBASE_API_KEY", "")
    base_url = lb_cfg.get("base_url") or os.environ.get(
        "LAKEBASE_API_URL", "http://localhost:8080/api/v1"
    )
    base_id = lb_cfg.get("base_id") or os.environ.get("LAKEBASE_MEM_BASE_ID", "mem_default")
    database_id = lb_cfg.get("database_id") or os.environ.get(
        "LAKEBASE_DATABASE_ID", "db_agent_memory"
    )
    timeout = float(lb_cfg.get("timeout") or 60.0)

    if not api_key:
        logger.warning("[ExternalMemoryBuilder] LakeBase unavailable (no api_key)")
        return None

    provider = LakeBaseMemoryProvider(
        api_key=api_key,
        base_url=base_url,
        base_id=base_id,
        database_id=database_id,
        timeout=timeout,
    )

    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] LakeBase unavailable (config incomplete)")
        return None

    logger.info(
        "[ExternalMemoryBuilder] LakeBase provider built: base_url=%s, base_id=%s",
        base_url, base_id,
    )
    return provider


def _load_plugin_provider(name: str, allowed: Optional[list] = None):
    try:
        from .plugin_discovery import load_memory_plugin
    except ImportError:
        logger.warning(
            "[ExternalMemoryBuilder] plugin '%s' requested but plugin_discovery not yet available",
            name,
        )
        return None
    return load_memory_plugin(name, allowed_plugins=allowed)
