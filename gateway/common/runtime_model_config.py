# Gateway 侧副本，源文件 jiuwenswarm/common/runtime_model_config.py；改动请同步两侧。
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-memory model connection scope, independent of Channels and persistence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_MODEL_ENTRY: ContextVar[dict[str, Any] | None] = ContextVar(
    "runtime_model_entry", default=None
)


@contextmanager
def runtime_model_config(entry: dict[str, Any] | None) -> Iterator[None]:
    """Bind a connection for this invocation and its child async tasks only.

    The entry may contain credentials. It must never be serialized into an
    Agent request or passed to a configuration writer.
    """
    token = _MODEL_ENTRY.set(deepcopy(entry))
    try:
        yield
    finally:
        _MODEL_ENTRY.reset(token)


def apply_runtime_model_config(config: dict[str, Any]) -> dict[str, Any]:
    """Overlay a parsed read without changing the parse cache or disk config."""
    entry = _MODEL_ENTRY.get()
    if entry is None:
        return config
    result = dict(config)
    # A scoped connection is authoritative, including for child Agents.
    # Default groups and AgentOS fallback entries must not reroute it.
    # Keep independent multimodal connections and future model settings. Copy
    # the section so scoped reads cannot mutate the shared parsed config.
    models = deepcopy(config.get("models") or {})
    models.update(defaults=[deepcopy(entry)], groups=[], agentos=[])
    result["models"] = models
    return result
