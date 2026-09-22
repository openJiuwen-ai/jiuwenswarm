# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AgentCreateFailed",
    "AgentCreatingTimeout",
    "AgentInfo",
    "AgentManager",
    "AgentOSRouter",
    "AgentOSRouterClient",
    "AgentRuntime",
    "AgentStatus",
    "ImageInfo",
    "RegistryClient",
]


def __getattr__(name: str) -> Any:
    """Lazily preserve the package-level AgentOS Router exports."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(
        import_module("jiuwenswarm.extensions.agentos.agentos_router"),
        name,
    )
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
