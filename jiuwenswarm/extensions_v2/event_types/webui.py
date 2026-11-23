# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI event types and their registration on the extension event bus."""

from __future__ import annotations

from openjiuwen.core.events import EventHandler, ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset
from jiuwenswarm.extensions_v2.webui.constants import UPSTREAM_EVENTS, WebUIEvent


def register_webui_events(
    bus: ExtensionEventBus,
    render_core: EventHandler,
    action_core: EventHandler,
) -> None:
    """Register ui.render and all upstream ui.* event types."""

    bus.register_event_type(WebUIEvent.RENDER.value, match_pattern_subset, render_core)
    for event_type in sorted(UPSTREAM_EVENTS):
        bus.register_event_type(event_type, match_pattern_subset, action_core)
