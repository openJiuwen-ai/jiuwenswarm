# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI event types: one registration function per platform event family."""

from __future__ import annotations

from openjiuwen.core.events import EventHandler, ExtensionEventBus

from jiuwenswarm.gateway.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_UI_APPEARANCE,
    EVENT_UI_DOWNLOAD,
    EVENT_UI_RENDER,
    EVENT_UI_SPLASH_PREVIEW,
)


def register_webui_events(
    bus: ExtensionEventBus,
    render_core: EventHandler,
) -> None:
    """Register the WebUI events with their CORE handlers."""

    bus.register_event_type(EVENT_UI_RENDER, match_pattern_subset, render_core)


def register_webui_action_events(
    bus: ExtensionEventBus,
    *,
    appearance_core: EventHandler,
    download_core: EventHandler,
    splash_preview_core: EventHandler,
) -> None:
    """Register the host action events that packages reach through Ops."""

    bus.register_event_type(EVENT_UI_APPEARANCE, match_pattern_subset, appearance_core)
    bus.register_event_type(EVENT_UI_DOWNLOAD, match_pattern_subset, download_core)
    bus.register_event_type(EVENT_UI_SPLASH_PREVIEW, match_pattern_subset, splash_preview_core)
