"""Resume chat.final mirror publishes to live; hold settle is separate."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from jiuwenswarm.gateway.cron.live_bus import CronLiveBus
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


def test_mirror_cron_final_publishes_without_releasing_hold(monkeypatch):
    """Contentful final must not release hold / clear replay by itself.

    Multi-tool rounds may emit finals between asks; clearing here would drop
    the next permission card and free run_now too early.
    """
    bus = CronLiveBus(replay_size=8, replay_ttl_seconds=60.0)
    sid = "cron_mirror_clear_1"
    bus.publish(
        sid,
        {
            "event": "chat.ask_user_question",
            "payload": {"request_id": "ask-1", "session_id": sid},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.live_bus.cron_live_bus",
        bus,
    )

    msg = SimpleNamespace(
        payload={
            "event_type": "chat.final",
            "content": "验证完成",
            "session_id": sid,
        },
        event_type=SimpleNamespace(value="chat.final"),
    )
    handler = MagicMock(spec=MessageHandler)
    handler._cron_registry = None
    handler._cron_controller = None
    MessageHandler._mirror_cron_live_stream_chunk(handler, sid, msg)

    handler._release_cron_permission_hold.assert_not_called()
    queue = bus.subscribe(sid)
    # Prior ask remains in replay; settle path clears when hold is released.
    assert not queue.empty()
    frame = queue.get_nowait()
    assert frame.get("event") == "chat.ask_user_question"
    # Mirrored final is also in the buffer after the ask.
    frame2 = queue.get_nowait()
    assert frame2.get("event") == "chat.final"
    bus.unsubscribe(sid, queue)
