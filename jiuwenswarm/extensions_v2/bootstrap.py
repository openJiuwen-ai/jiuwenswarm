# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register JiuwenSwarm event types, CORE handlers and BUILTIN handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types.prompt_context import register_prompt_context_events
from jiuwenswarm.extensions_v2.event_types.session_attach import register_session_attach_event
from jiuwenswarm.extensions_v2.event_types.session_detach import register_session_detach_event
from jiuwenswarm.extensions_v2.event_types.session_end import register_session_end_event
from jiuwenswarm.extensions_v2.event_types.session_history import register_session_history_events
from jiuwenswarm.extensions_v2.event_types.session_start import register_session_start_event
from jiuwenswarm.extensions_v2.event_types.session_queries import register_session_query_events
from jiuwenswarm.extensions_v2.event_types.session_receive import register_session_receive_event
from jiuwenswarm.extensions_v2.event_types.session_compact import register_session_compact_event
from jiuwenswarm.extensions_v2.event_types.session_authorize import register_session_authorize_event
from jiuwenswarm.extensions_v2.event_types.server_run_command import register_server_run_command_event
from jiuwenswarm.extensions_v2.event_types.session_send import register_session_send_event
from jiuwenswarm.extensions_v2.event_handlers.session_queries import register_session_query_handlers
from jiuwenswarm.extensions_v2.event_handlers.session_authorize import register_authorization_lifecycle_handlers
from jiuwenswarm.extensions_v2.handler_ops.session_ops import SessionFileService, register_session_ops

if TYPE_CHECKING:
    from openjiuwen.core.events import ExtensionEventBus

    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime

logger = logging.getLogger(__name__)


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    """Register event types and their CORE handlers."""

    webui.register_event_types(bus)
    register_context_usage_events(bus)
    register_prompt_context_events(bus)
    register_session_history_events(bus)
    register_session_start_event(bus)
    register_session_end_event(bus)
    register_session_attach_event(bus)
    register_session_detach_event(bus)
    register_session_query_events(bus)
    register_session_send_event(bus)
    register_session_receive_event(bus)
    register_session_compact_event(bus)
    register_session_authorize_event(bus)
    register_server_run_command_event(bus)
    _register_team_event_types(bus)


def register_handlers(
    bus: ExtensionEventBus,
    webui: WebUIRuntime,
    *,
    file_service: SessionFileService | None = None,
    compact_service=None,
    authorization_service=None,
) -> None:
    """Register BUILTIN handlers for existing event types."""

    webui.register_handlers(bus)
    register_session_query_handlers(bus)
    register_session_ops(bus, file_service=file_service, compact_service=compact_service,
                         authorization_service=authorization_service)
    register_authorization_lifecycle_handlers(bus, authorization_service)
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

    The resolver answers from the team database via the host's shared
    singleton factory: ``ops.build`` runs at process start, long before
    any team exists, and teams stay queryable after they stop. Same
    soft-gate as the team events.
    """

    try:
        from openjiuwen.agent_teams.extensions.resolver import DatabaseResolver
        from openjiuwen.extensions.ops_build_handlers import team as team_ops
    except ImportError as exc:
        logger.warning("[ExtensionsV2] team ops unavailable (agent-core too old): %s", exc)
        return
    team_ops.register(bus, DatabaseResolver(_team_db_factory))


async def _team_db_factory():
    """Return the initialized shared team database.

    Follows the team monitor handler's pattern: the configured sqlite
    path resolved through host config, handed to the process-wide shared
    singleton. Only the static schema and the first bound session's
    dynamic tables are initialized here — which session's dynamic tables
    a query reads is the resolver's per-query concern, and the read path
    never runs DDL. Hosts without team storage configured answer ``None``
    (every team then resolves as not found, never a crash).
    """

    from openjiuwen.agent_teams.spawn.shared_resources import get_shared_db
    from openjiuwen.agent_teams.tools.database.config import DatabaseConfig

    from jiuwenswarm.agents.harness.team.config_loader import resolve_team_sqlite_db_path
    from jiuwenswarm.common.config import get_config

    db_path = resolve_team_sqlite_db_path(get_config())
    if db_path is None:
        return None
    # The DAOs mount after initialize (idempotent).
    db = get_shared_db(DatabaseConfig(db_type="sqlite", connection_string=str(db_path)))
    await db.initialize()
    return db
