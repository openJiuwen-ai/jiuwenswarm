# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Connector Source host runtime (Registry + config apply).

Mirrors ``server.runtime.skill`` ownership of ``SourceRegistry``: SPI types live in
gateway; this package owns process-local multi-source binding. MCP connect state
remains in ``server.runtime.mcp.registry``.
"""

from jiuwenswarm.server.runtime.connector.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)
from jiuwenswarm.server.runtime.connector.source_runtime import (
    SourceRuntime,
    get_source_runtime,
    reset_source_runtime,
)

__all__ = [
    "SourceRegistry",
    "SourceRegistryError",
    "SourceRuntime",
    "get_source_runtime",
    "reset_source_runtime",
]
