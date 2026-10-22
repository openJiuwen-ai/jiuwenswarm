# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry point declared by manifest.yaml."""

from __future__ import annotations

from .handlers import TokenWeatherHandlers


class TokenWeatherExtension:
    """Register the Token Weather handlers as USER-level event handlers."""

    def __init__(self) -> None:
        self._handlers = TokenWeatherHandlers()

    def register(self, registrar) -> None:
        for event_type, handler, match_pattern in self._handlers.handlers():
            registrar.register_event_handler(event_type, handler, match_pattern, "user")


def create_extension() -> TokenWeatherExtension:
    return TokenWeatherExtension()
