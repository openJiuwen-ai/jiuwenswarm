# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish saved configuration to process-owned Runtime agents."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def reload_runtime_after_config_save(
    agent_manager: Any,
    _updated_keys: Any = None,
    *,
    env_updates: dict[str, Any] | None = None,
    config_payload: dict[str, Any] | None = None,
    reload_options: dict[str, Any] | None = None,
) -> bool:
    """Publish a saved config to every agent in this process before acknowledging it.

    Permission changes share one config file across channels, so they are not
    narrowed to the channel that submitted the panel request.
    """
    options = reload_options if isinstance(reload_options, dict) else {}
    scopes = {
        str(scope)
        for scope in (options.get("reload_scopes") or [])
        if isinstance(scope, str) and scope
    }
    target_channel = str(options.get("target_channel_id") or "").strip() or None
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.runtime.memory_portal import get_memory_portal_service

    try:
        await get_memory_portal_service().configure(
            config_payload if isinstance(config_payload, dict) and "memory" in config_payload else get_config()
        )
    except Exception as exc:
        logger.warning("Memory Portal configuration update failed (%s)", type(exc).__name__)
    if set(_updated_keys or []) == {"memory_portal_enabled"} and not env_updates:
        return True
    if "permissions" in scopes:
        target_channel = None
    await agent_manager.reload_agents_config(
        config_payload if isinstance(config_payload, dict) else None,
        env_updates if isinstance(env_updates, dict) else None,
        target_channel_id=target_channel,
        reload_scopes=scopes or None,
    )
    return True
