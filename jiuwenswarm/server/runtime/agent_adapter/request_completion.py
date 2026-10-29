# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable request boundaries shared by foreground and continuation execution."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from jiuwenswarm.common.schema.agent import AgentRequest
from gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.server.runtime.session.session_history import (
    enqueue_history_request_completion, SESSION_REQUEST_COMPLETED_EVENT,
)
from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

logger = logging.getLogger(__name__)
_COMPLETION_ANNOUNCEMENTS: set[asyncio.Task[None]] = set()
_HISTORY_RECEIPT_WAIT_SECONDS = 30.0

def _announce_durable_completion(
    session_id: str,
    request_id: str,
    completion_receipt: Future[None],
) -> None:
    """Tell the gateway once the request's terminal row is on disk.

    A gateway that stopped waiting for the request (cancel, transport error)
    has no other way to learn when AgentServer finishes unwinding it.
    """

    loop = asyncio.get_running_loop()

    def announce() -> None:
        task = loop.create_task(WebSocketGatewayPushTransport().send_push({
            "channel_id": "",
            "session_id": session_id,
            "request_id": request_id,
            "payload": {"event_type": SESSION_REQUEST_COMPLETED_EVENT},
        }))
        _COMPLETION_ANNOUNCEMENTS.add(task)
        task.add_done_callback(_COMPLETION_ANNOUNCEMENTS.discard)
        from jiuwenswarm.server.runtime.recommendation_host import (
            active_recommendation_host,
        )

        host = active_recommendation_host()
        if host is not None:
            try:
                host.on_chat_completed(session_id, request_id)
            except Exception:  # noqa: BLE001 - replenishment must not block the receipt
                logger.warning(
                    "topic replenishment after completion failed: session_id=%s",
                    session_id,
                    exc_info=True,
                )

    # Observe the receipt with a done callback so no cancelled waiter can
    # cancel the receipt the history writer still has to resolve.
    def on_written(receipt: Future[None]) -> None:
        if not receipt.cancelled() and receipt.exception() is None:
            if loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(announce)
            except RuntimeError:
                if not loop.is_closed():
                    raise

    completion_receipt.add_done_callback(on_written)


async def drain_completion_announcements() -> None:
    """Join receipt callbacks after the Worker's history writer has drained."""
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    tasks = [task for task in _COMPLETION_ANNOUNCEMENTS if task.get_loop() is loop]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_history_receipt(completion_receipt: Future[None]) -> None:
    """Wait for the terminal row to reach disk, bounded, without cancelling the writer.

    Shielded: cancelling this wait must not cancel the writer's receipt. A wait that times
    out leaves the receipt to resolve on its own and lets the turn finish.
    """

    try:
        await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(completion_receipt)),
            timeout=_HISTORY_RECEIPT_WAIT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "history completion receipt not confirmed within %.0fs",
            _HISTORY_RECEIPT_WAIT_SECONDS,
        )


def _complete_history_request(
    session_id: str,
    request_id: str,
    *,
    terminal_status: str = "success",
) -> Future[None] | None:
    """Persist the request boundary, then announce it to the gateway once durable."""

    completion_receipt = enqueue_history_request_completion(
        session_id,
        request_id,
        terminal_status=terminal_status,
    )
    if completion_receipt is not None:
        _announce_durable_completion(session_id, request_id, completion_receipt)
    return completion_receipt


def _session_message_owns_completion(request: AgentRequest) -> bool:
    """Session-message turns get their terminal row from AgentWebSocketServer."""

    params = request.params if isinstance(request.params, dict) else {}
    return isinstance(params.get(SESSION_MESSAGE_INTERNAL_KEY), dict) or bool(
        getattr(request, "trusted_session_message_route", None)
    )


class RequestCompletion:
    """One execution owns its terminal receipt and suggestion notification."""

    def __init__(self, request, session_id, *, is_team_mode, ports):
        from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion

        self.request, self.session_id, self.ports = request, session_id, ports
        self.status = "success"
        self.waiting_for_user = False
        self.terminal_final_persisted = False
        self.receipt = None
        self.scheduled = ports.session_message_owns_completion(request)
        self.forks = prompt_suggestion.forks
        self.forks.request_started(
            session_id, person_turn=ports.person_drove_turn(request, is_team_mode=is_team_mode),
        )
        self.is_team_mode = is_team_mode

    def observe(self, chunk):
        from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import USER_INTERACTION_EVENTS

        payload = chunk if isinstance(chunk, dict) else getattr(chunk, "payload", None)
        if not isinstance(payload, dict):
            return
        event = payload.get("event_type")
        if event == "chat.error":
            self.status = "error"
        self.waiting_for_user = self.waiting_for_user or event in USER_INTERACTION_EVENTS
        if event == "chat.final" and (not self.is_team_mode or payload.get("role") != "teammate"):
            self.terminal_final_persisted = True

    def complete(self, status=None):
        if self.scheduled:
            return
        if status == "cancelled" and self.status == "error":
            status = "error"
        if status == "cancelled" and self.terminal_final_persisted:
            status = "success"
        self.status = status or self.status
        self.receipt = self.ports.complete_history_request(
            self.session_id, self.request.request_id, terminal_status=self.status,
        )
        self.scheduled = True
        self.forks.turn_ended(
            self.session_id, self.request.request_id, status=self.status,
            waiting_for_user=self.waiting_for_user, completion=self.receipt,
        )

    async def wait(self):
        if self.receipt is not None:
            await self.ports.await_history_receipt(self.receipt)
