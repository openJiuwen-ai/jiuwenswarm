from __future__ import annotations

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
