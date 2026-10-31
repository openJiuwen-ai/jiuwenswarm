# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register JiuwenSwarm event types, CORE handlers and BUILTIN handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types.session_end import register_session_end_event
from jiuwenswarm.extensions_v2.event_types.session_history import register_session_history_events
from jiuwenswarm.extensions_v2.event_types.session_start import register_session_start_event
from jiuwenswarm.extensions_v2.handler_ops.session_ops import SessionFileService, register_session_ops

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus

    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    """Register event types and their CORE handlers."""

    webui.register_event_types(bus)
    register_context_usage_events(bus)
    register_session_history_events(bus)
    register_session_start_event(bus)
    register_session_end_event(bus)


def register_handlers(
    bus: ExtensionEventBus,
    webui: WebUIRuntime,
    *,
    file_service: SessionFileService | None = None,
) -> None:
    """Register BUILTIN handlers for existing event types."""

    webui.register_handlers(bus)
    register_session_ops(bus, file_service=file_service)
