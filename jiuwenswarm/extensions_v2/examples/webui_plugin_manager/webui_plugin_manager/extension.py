# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extension factory referenced by the package manifest."""

from __future__ import annotations

from .handlers import PluginManagerHandlers


class PluginManagerExtension:
    """Own manager state for exactly one package activation."""

    def __init__(self) -> None:
        self._handlers = PluginManagerHandlers()

    def register(self, registrar) -> None:
        for event_type, handler, match_pattern in self._handlers.handlers():
            registrar.register_event_handler(event_type, handler, match_pattern, "user")


def create_extension() -> PluginManagerExtension:
    """Create fresh state on install or reload."""
    return PluginManagerExtension()
