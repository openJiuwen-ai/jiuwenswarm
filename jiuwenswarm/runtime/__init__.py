# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lazy public surface for the transport-independent JiuwenSwarm Runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentRuntime": ("jiuwenswarm.runtime.service", "AgentRuntime"),
    "RuntimeStateError": ("jiuwenswarm.runtime.service", "RuntimeStateError"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
