# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry point declared by manifest.json."""

from __future__ import annotations

from .handlers import FsProbeHandlers


class FsProbeExtension:
    """Register the FS Probe handlers as USER-level event handlers."""

    def __init__(self) -> None:
        self._handlers = FsProbeHandlers()

    def register(self, registrar) -> None:
        for event_type, handler, match_pattern in self._handlers.handlers():
            registrar.register_event_handler(event_type, handler, match_pattern, "user")


def create_extension() -> FsProbeExtension:
    return FsProbeExtension()
