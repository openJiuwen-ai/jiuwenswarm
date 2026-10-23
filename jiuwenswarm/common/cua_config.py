# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared desktop preference semantics for config queries and agent assembly."""

from typing import Any

DEFAULT_CUA_AGENT_ENABLED = False


def is_cua_agent_enabled(subagent_config: Any) -> bool:
    """Resolve null/missing as the release default without changing saved preferences."""
    value = subagent_config.get("enabled") if isinstance(subagent_config, dict) else None
    return DEFAULT_CUA_AGENT_ENABLED if value is None else value is True
