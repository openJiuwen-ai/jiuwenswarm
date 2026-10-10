from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        (
            "personal_context.context.start",
            {"event_type": "personal_context.context.start", "context_ready": True},
        ),
        (
            "personal_context.context.nodes",
            {
                "event_type": "personal_context.context.nodes",
                "nodes": [{"id": "page:description.md"}],
            },
        ),
        (
            "personal_context.context.edges",
            {
                "event_type": "personal_context.context.edges",
                "edges": [
                    {
                        "source": "page:description.md",
                        "target": "page:sources/description.md",
                    }
                ],
            },
        ),
        (
            "personal_context.context.end",
            {
                "event_type": "personal_context.context.end",
                "node_count": 3,
                "edge_count": 2,
            },
        ),
    ],
)
def test_personal_context_structure_events_preserve_complete_payload(
    event_name: str,
    payload: dict[str, object],
) -> None:
    message = Message(
        id="personal-context-structure",
        type="event",
        channel_id="web",
        session_id="personal-context-session",
        params={},
        timestamp=0.0,
        ok=True,
        payload=payload,
    )

    assert WebChannel._build_event_payload(message, event_name) == {
        **payload,
        "session_id": "personal-context-session",
    }
