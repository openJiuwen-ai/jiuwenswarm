# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validation helpers for DeepResearch SDK workflow token usage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "llm_call_count")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def normalize_workflow_llm_token_usage(value: Any) -> dict[str, Any] | None:
    """Return a bounded SDK usage mapping, or ``None`` for an invalid payload."""
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, Any] = {}
    for field in _TOKEN_FIELDS:
        token_value = _nonnegative_int(value.get(field))
        if token_value is None:
            return None
        normalized[field] = token_value

    agent_usage: list[dict[str, Any]] = []
    raw_agent_usage = value.get("agent_name_token_usage", [])
    if not isinstance(raw_agent_usage, list):
        return None
    for item in raw_agent_usage:
        if not isinstance(item, Mapping):
            return None
        agent_name = item.get("agent_name")
        if not isinstance(agent_name, str) or not agent_name.strip():
            return None
        normalized_item: dict[str, Any] = {"agent_name": agent_name.strip()}
        for field in _TOKEN_FIELDS:
            token_value = _nonnegative_int(item.get(field))
            if token_value is None:
                return None
            normalized_item[field] = token_value
        agent_usage.append(normalized_item)
    normalized["agent_name_token_usage"] = agent_usage
    return normalized


__all__ = ["normalize_workflow_llm_token_usage"]
