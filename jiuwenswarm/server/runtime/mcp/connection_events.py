# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web notification for an MCP connection that did not come back.

The MCP call timeout patch persists the lost connection in ``state.json``
(``registry.mark_mcp_connection_lost``); the frontend badge, however, only
re-reads that state on ``mcp.list``. This module pushes the "something
changed, reload your MCP list" signal over the existing E2A server_push
channel so an open MCP page stops showing a phantom "connected".

Routing constraints (agent-gateway is a pinned external dependency, so they
cannot be relaxed from this repo):

* A frame **must** carry a ``session_id``. ``WebChannel._send_message`` drops
  anything with no ``routing_target``, no ``metadata.ws_id`` and no
  ``session_id`` ("to avoid cross-session broadcast"), so a session-less push
  never reaches a browser.
* ``channel_id`` is the web channel, matching the browser's ws registration.
* The event name reaches the browser through the ``payload.event_type``
  fallback in ``WebChannel._resolve_event_name`` — an unknown name is fine,
  the frame keeps it.
* The same gateway reduces the payload of non-whitelisted events to
  ``{session_id, content}``, so this event carries **no per-MCP fields**: the
  frontend treats it as "reload the list", which is also what keeps it
  correct when several MCPs changed.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: Event name the web frontend subscribes to (``useWebSocket``).
MCP_CONNECTION_CHANGED_EVENT = "mcp.connection_changed"

__all__ = [
    "MCP_CONNECTION_CHANGED_EVENT",
    "publish_connection_changed",
]


def _server_instance() -> Any | None:
    """The in-process agent server, or None when this process has none.

    Resolved lazily (``agent_ws_server`` imports a large slice of the server)
    and without calling ``get_instance()``: that would construct an idle
    server in a worker process just to have the push fail.
    """
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    return getattr(AgentWebSocketServer, "_instance", None)


async def publish_connection_changed(session_id: str) -> bool:
    """Ask the web client of ``session_id`` to reload its MCP list.

    Best-effort by design: the persistent state is already correct whatever
    this returns, and a timed-out tool call must not be slowed down by a
    notification. Returns True when a frame was handed to the transport.
    """
    sid = str(session_id or "").strip()
    if not sid:
        # No session means no browser connection the gateway would deliver to.
        logger.debug("[mcp.events] skip connection-changed push: no session_id")
        return False
    try:
        if _server_instance() is None:
            logger.debug("[mcp.events] skip connection-changed push: no agent server in this process")
            return False
        from jiuwenswarm.server.gateway_push.transport import (
            WebSocketGatewayPushTransport,
        )

        sent = await WebSocketGatewayPushTransport().send_push({
            "request_id": uuid.uuid4().hex,
            "channel_id": "web",
            "session_id": sid,
            "payload": {"event_type": MCP_CONNECTION_CHANGED_EVENT},
            "is_complete": False,
        })
        if not sent:
            logger.debug(
                "[mcp.events] connection-changed push not delivered (session=%s)", sid,
            )
        return bool(sent)
    except Exception as exc:  # noqa: BLE001 - a late push must not break the caller
        logger.warning("[mcp.events] connection-changed push failed: %s", exc)
        return False
