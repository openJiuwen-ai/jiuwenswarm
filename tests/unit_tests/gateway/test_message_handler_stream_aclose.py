"""Regression coverage: process_stream must close the agent stream generator.

``WebSocketAgentServerClient`` keys its in-flight queues by request_id and
de-registers in the ``finally`` of ``send_request_stream``. That ``finally``
only runs once the generator is finalized, so a consumer that stops iterating
-- a client closing its session page mid-stream -- leaves the entry behind and
every later ``chat.send`` reusing the request_id is refused as a duplicate
in-flight request.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _RecordingAgentClient:
    """Stands in for WebSocketAgentServerClient's queue registry."""

    def __init__(self) -> None:
        self.in_flight: set[str] = set()

    async def send_request_stream(self, env: Any):
        rid = env.request_id
        self.in_flight.add(rid)
        try:
            # Park at the yield: the consumer is cancelled while handling this
            # chunk, which is what leaves the generator suspended.
            yield SimpleNamespace(payload={"event_type": "chat.chunk"}, is_complete=False)
            yield SimpleNamespace(payload={}, is_complete=True)  # pragma: no cover
        finally:
            self.in_flight.discard(rid)


def _bare_handler(agent_client: object) -> MessageHandler:
    """Avoid singleton initialization; process_stream only needs these fields."""
    handler = object.__new__(MessageHandler)
    handler.agent_client = agent_client
    handler._stream_app_ids = {}
    handler._stream_modes = {}
    handler._stream_user_ids = {}
    handler._stream_sessions = {}
    handler._stream_metadata = {}
    handler._non_stream_chat_messages = {}

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _false(*_args: Any, **_kwargs: Any) -> bool:
        return False

    handler._active_chat_tasks = {}
    handler.publishing = asyncio.Event()

    async def _blocking_publish(*_args: Any, **_kwargs: Any) -> bool:
        handler.publishing.set()
        await asyncio.Event().wait()
        return True  # pragma: no cover

    handler.publish_stream_chunk = _blocking_publish
    handler._evolution_approval = SimpleNamespace(
        clear_session_in_progress=lambda *_a, **_k: None,
    )

    handler._sync_agentos_cron_jobs = _noop
    handler._apply_session_login_owner = _noop
    handler._publish_stream_error = _noop
    handler._pop_stream_tracking = lambda *_a, **_k: None
    handler._pop_stream_tracking_and_broadcast = _noop
    handler._send_processing_status = _noop
    handler._complete_evolution_approval_if_current = _noop
    handler._session_has_streams_blocking_processing_false = _false
    handler._is_terminal_stream_chunk = _false
    handler._is_interrupt_evolution_approval_answer_payload = _false
    handler.publish_robot_messages = _noop
    return handler


@pytest.mark.asyncio
async def test_process_stream_releases_in_flight_request_on_cancel() -> None:
    """A cancelled consumer must not leave the request_id registered."""
    client = _RecordingAgentClient()
    handler = _bare_handler(client)
    env = SimpleNamespace(
        request_id="chat-x",
        channel="web",
        params={},
        session_id="sess-1",
        is_stream=True,
    )

    task = asyncio.create_task(
        handler.process_stream(env, "sess-1", {})
    )
    # Wait until the first chunk is being handled: the generator is now parked
    # at its yield and the consumer is inside the loop body.
    await asyncio.wait_for(handler.publishing.wait(), timeout=5)
    assert client.in_flight == {"chat-x"}, "stream should be registered while running"

    task.cancel()
    # process_stream absorbs CancelledError (it only records cancelled=True),
    # so the task completes normally -- which is why the leak is silent.
    await task

    assert client.in_flight == set(), (
        "process_stream left request_id='chat-x' registered after cancellation; "
        "the next chat.send reusing it would be refused as a duplicate"
    )


@pytest.mark.asyncio
async def test_safe_aclose_is_a_noop_without_a_generator() -> None:
    from jiuwenswarm.gateway.message_handler.message_handler import _safe_aclose

    await _safe_aclose(None, request_id="x")
    await _safe_aclose(object(), request_id="y")
