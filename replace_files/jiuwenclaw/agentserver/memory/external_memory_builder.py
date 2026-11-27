# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Builder for ExternalMemoryRail — single entry point, config-driven.

Dispatches on `memory.external.provider`:
  - openjiuwen  -> OpenJiuwenMemoryProvider (builds its own KV/Vector/DB from config)
  - mem0        -> Mem0MemoryProvider
  - openviking  -> OpenVikingMemoryProvider
  - <plugin>    -> user-installed plugin from ~/.jiuwenclaw/plugins/memory/
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
) -> Optional[Any]:
    """Build an ExternalMemoryRail from config, or None if disabled/failed."""
    try:
        from openjiuwen.harness.rails.external_memory_rail import ExternalMemoryRail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] ExternalMemoryRail import failed: %s", exc)
        return None

    ext_cfg = get_external_memory_config(config)
    provider_name = ext_cfg.get("provider", "")
    if not provider_name:
        return None

    provider = None
    try:
        if provider_name == "openjiuwen":
            provider = _build_openjiuwen_provider(ext_cfg, config)
        elif provider_name == "mem0":
            provider = _build_mem0_provider(ext_cfg)
        elif provider_name == "openviking":
            provider = _build_openviking_provider(ext_cfg)
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
        )
        logger.info(
            "[ExternalMemoryBuilder] ExternalMemoryRail built (provider=%s)",
            provider_name,
        )
        return rail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] rail construction failed: %s", exc)
        return None


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
    user_id = mem0_cfg.get("user_id") or os.environ.get("MEM0_USER_ID", "jiuwenclaw-user")
    agent_id = mem0_cfg.get("agent_id") or os.environ.get("MEM0_AGENT_ID", "jiuwenclaw")
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
