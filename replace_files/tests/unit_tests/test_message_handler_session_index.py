from __future__ import annotations

from unittest.mock import patch

from jiuwenclaw.gateway.message_handler import MessageHandler
from jiuwenclaw.schema.agent import AgentResponse
from jiuwenclaw.schema.message import EventType, Message, ReqMethod


def _make_message(
    *,
    channel_id: str = "web",
    session_id: str = "sess_001",
    req_method: ReqMethod | None = None,
    event_type: EventType | None = None,
    payload: dict | None = None,
) -> Message:
    return Message(
        id="req_1",
        type="event" if event_type else "req",
        channel_id=channel_id,
        session_id=session_id,
        params={},
        timestamp=123.0,
        ok=True,
        req_method=req_method,
        event_type=event_type,
        payload=payload or {},
    )


class TestMessageHandlerSessionIndexWebOnly:
    @staticmethod
    def test_user_msg_only_web_updates_index() -> None:
        msg = _make_message(channel_id="feishu", req_method=ReqMethod.CHAT_SEND)
        msg.params = {"query": "hello"}
        with (
            patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=True),
            patch("jiuwenclaw.gateway.session_index.upsert") as upsert,
        ):
            MessageHandler._maybe_update_session_index_on_user_msg(msg)
            upsert.assert_not_called()

    @staticmethod
    def test_user_msg_web_updates_index() -> None:
        msg = _make_message(channel_id="web", req_method=ReqMethod.CHAT_SEND)
        msg.params = {"query": "hello"}
        with (
            patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=True),
            patch("jiuwenclaw.gateway.session_index.upsert") as upsert,
        ):
            MessageHandler._maybe_update_session_index_on_user_msg(msg)
            upsert.assert_called_once()

    @staticmethod
    def test_robot_msg_only_web_updates_index() -> None:
        msg = _make_message(
            channel_id="wecom",
            event_type=EventType.CHAT_FINAL,
            payload={"content": "done"},
        )
        with (
            patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=True),
            patch("jiuwenclaw.gateway.session_index.upsert") as upsert,
        ):
            MessageHandler._maybe_update_session_index_on_robot_msg(msg)
            upsert.assert_not_called()

    @staticmethod
    def test_session_delete_only_web_syncs_index() -> None:
        msg = _make_message(channel_id="xiaoyi", req_method=ReqMethod.SESSION_DELETE)
        msg.params = {"session_id": "sess_001"}
        resp = AgentResponse(
            request_id="req_1",
            channel_id="xiaoyi",
            ok=True,
            payload={"session_id": "sess_001"},
        )
        with (
            patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=True),
            patch("jiuwenclaw.gateway.session_index.remove") as remove,
            patch("jiuwenclaw.gateway.session_index.upsert") as upsert,
        ):
            MessageHandler._maybe_sync_session_index_on_response(msg, resp)
            remove.assert_not_called()
            upsert.assert_not_called()

    @staticmethod
    def test_session_create_web_syncs_index() -> None:
        msg = _make_message(channel_id="web", req_method=ReqMethod.SESSION_CREATE)
        resp = AgentResponse(
            request_id="req_1",
            channel_id="web",
            ok=True,
            payload={"session_id": "sess_001"},
        )
        with (
            patch("jiuwenclaw.gateway.session_index.is_remote_storage", return_value=True),
            patch("jiuwenclaw.gateway.session_index.upsert") as upsert,
        ):
            MessageHandler._maybe_sync_session_index_on_response(msg, resp)
            upsert.assert_called_once()
