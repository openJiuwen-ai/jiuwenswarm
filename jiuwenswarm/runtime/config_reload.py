# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish saved configuration to process-owned Runtime agents."""

from __future__ import annotations

from typing import Any


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
    if "permissions" in scopes:
        target_channel = None
    await agent_manager.reload_agents_config(
        config_payload if isinstance(config_payload, dict) else None,
        env_updates if isinstance(env_updates, dict) else None,
        target_channel_id=target_channel,
        reload_scopes=scopes or None,
    )
    return True
