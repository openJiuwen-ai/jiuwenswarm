from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

_TARGET = {
    "session_id": "webui-session",
    "slot_id": "conversation.panel.right",
    "package_id": "webui.demo",
    "instance_key": "main",
}


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        (
            "webui.render",
            {
                "event_type": "webui.render",
                "target": dict(_TARGET),
                "revision": 2,
                "mode": "replace",
                "root": {"component": "Card", "props": {"title": "demo"}},
            },
        ),
        (
            "webui.render.error",
            {
                "event_type": "webui.render.error",
                "target": dict(_TARGET),
                "revision": 2,
                "code": "SCHEMA_ERROR",
                "message": "root is too large",
            },
        ),
        (
            "webui.render.clear",
            {
                "event_type": "webui.render.clear",
                "target": dict(_TARGET),
                "revision": 3,
                "reason": "session_closed",
            },
        ),
    ],
)
def test_webui_render_frames_preserve_complete_payload(
    event_name: str,
    payload: dict[str, object],
) -> None:
    message = Message(
        id="webui-webui.render-test",
        type="event",
        channel_id="web",
        session_id="webui-session",
        params={},
        timestamp=0.0,
        ok=True,
        payload=payload,
    )

    assert WebChannel._build_event_payload(message, event_name) == {
        **payload,
        "session_id": "webui-session",
    }


@pytest.mark.parametrize("session_id", ["", "webui-session"])
async def test_package_change_notification_preserves_package_ids_on_web_downlink(session_id: str) -> None:
    channel = object.__new__(WebChannel)
    socket = object()
    channel._session_busy = {}
    channel._clients_by_key = {}
    channel._ws_by_id = {"ws-1": socket, "ws-other": object()}
    channel._a4p_rebound_ws = {}
    channel._broadcast_to = AsyncMock()
    payload = {
        "event_type": "extensions.v2.installed",
        "package_ids": ["sample.counter", "another.pkg"],
        "session_id": session_id,
    }
    message = Message(
        id="extension-installed-test",
        type="event",
        channel_id="web",
        session_id=session_id or None,
        params={},
        timestamp=0.0,
        ok=True,
        payload=payload,
        metadata={"ws_id": "ws-1"},
    )

    await channel.send(message)

    channel._broadcast_to.assert_awaited_once()
    frame, clients = channel._broadcast_to.await_args.args
    assert frame == {"type": "event", "event": "extensions.v2.installed", "payload": payload}
    assert len(clients) == 1 and next(iter(clients)) is socket
