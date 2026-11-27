# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Component-based log routing filters."""

from __future__ import annotations

import logging


def log_component_from_logger_name(name: str) -> str:
    """Map logger name to gateway / channel / agent_server / permissions."""
    if name.startswith("jiuwenclaw.channel"):
        return "channel"
    if name.startswith("jiuwenclaw.agentserver.permissions.checker"):
        return "permissions"
    if name.startswith("jiuwenclaw.agentserver"):
        return "agent_server"
    return "gateway"


class ComponentNameFilter(logging.Filter):
    """Only pass log records for the given component (by logger name)."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "component"):
            return record.component == self.component
        return log_component_from_logger_name(record.name) == self.component


class CompositeFilter(logging.Filter):
    """Pass if any nested filter passes."""

    def __init__(self, filters: list[logging.Filter]) -> None:
        super().__init__()
        self.filters = filters

    def filter(self, record: logging.LogRecord) -> bool:
        return any(f.filter(record) for f in self.filters)
