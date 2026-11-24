# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Connector Source host (Registry + Manager for config apply).

Mirrors ``server.runtime.skill`` ownership of skill ``SourceRegistry``: SPI types
live in gateway; this package owns process / request-local multi-source binding.
MCP connect state remains in ``server.runtime.mcp.registry``.
"""

from jiuwenswarm.server.runtime.connector.manager import (
    ConnectorSourceManager,
    bind_connector_source_manager,
    get_connector_source_manager,
    reset_connector_source_manager,
    reset_connector_source_manager_binding,
)
from jiuwenswarm.server.runtime.connector.registry import (
    ConnectorSourceRegistry,
    ConnectorSourceRegistryError,
)

__all__ = [
    "ConnectorSourceManager",
    "ConnectorSourceRegistry",
    "ConnectorSourceRegistryError",
    "bind_connector_source_manager",
    "get_connector_source_manager",
    "reset_connector_source_manager",
    "reset_connector_source_manager_binding",
]
