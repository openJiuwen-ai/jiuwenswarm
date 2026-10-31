# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register JiuwenSwarm event types, CORE handlers and BUILTIN handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types.session_attach import register_session_attach_event
from jiuwenswarm.extensions_v2.event_types.session_detach import register_session_detach_event
from jiuwenswarm.extensions_v2.event_types.session_end import register_session_end_event
from jiuwenswarm.extensions_v2.event_types.session_history import register_session_history_events
from jiuwenswarm.extensions_v2.event_types.session_queries import register_session_query_events
from jiuwenswarm.extensions_v2.event_handlers.session_queries import register_session_query_handlers
from jiuwenswarm.extensions_v2.event_types.session_start import register_session_start_event
from jiuwenswarm.extensions_v2.handler_ops.session_ops import SessionFileService, register_session_ops

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus

    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime

logger = logging.getLogger(__name__)


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    """Register event types and their CORE handlers."""

    webui.register_event_types(bus)
    register_context_usage_events(bus)
    register_session_history_events(bus)
    register_session_query_events(bus)
    _register_team_event_types(bus)
    register_session_start_event(bus)
    register_session_end_event(bus)
    register_session_attach_event(bus)
    register_session_detach_event(bus)


def register_handlers(
    bus: ExtensionEventBus,
    webui: WebUIRuntime,
    *,
    file_service: SessionFileService | None = None,
) -> None:
    """Register BUILTIN handlers for existing event types."""

    webui.register_handlers(bus)
    register_session_query_handlers(bus)
    register_session_ops(bus, file_service=file_service)
    _register_team_ops(bus)


def _register_team_event_types(bus: ExtensionEventBus) -> None:
    """Register the team events (monitor notifications, policy builds).

    Soft-gated: an agent-core without the team extension surface (older
    checkouts) keeps the rest of extensions v2 alive instead of failing
    the whole assembly.
    """

    try:
        from openjiuwen.agent_teams.extensions.events import register_team_event_types
    except ImportError as exc:
        logger.warning("[ExtensionsV2] team events unavailable (agent-core too old): %s", exc)
        return
    register_team_event_types(bus)


def _register_team_ops(bus: ExtensionEventBus) -> None:
    """Contribute the read-only ``ops.team`` namespace during ``ops.build``.

    The resolver reads the global runner's team pool lazily: ``ops.build``
    runs at process start, long before any team runtime exists. Same
    soft-gate as the team events.
    """

    try:
        from openjiuwen.agent_teams.extensions.resolver import GlobalRunnerPoolResolver
        from openjiuwen.extensions.ops_build_handlers import team as team_ops
    except ImportError as exc:
        logger.warning("[ExtensionsV2] team ops unavailable (agent-core too old): %s", exc)
        return
    team_ops.register(bus, GlobalRunnerPoolResolver())
