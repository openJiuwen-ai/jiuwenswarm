# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI event types: one registration function per platform event family."""

from __future__ import annotations

from openjiuwen.core.events import EventHandler, ExtensionEventBus

from jiuwenswarm.gateway.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.gateway.extensions_v2.models import EVENT_UI_RENDER


def register_webui_events(
    bus: ExtensionEventBus,
    render_core: EventHandler,
) -> None:
    """Register the WebUI events with their CORE handlers."""

    bus.register_event_type(EVENT_UI_RENDER, match_pattern_subset, render_core)
