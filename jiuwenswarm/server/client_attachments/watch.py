# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a paused conversation still asks its client, and the sweep that answers for a client that is gone.

A turn that runs a tool in the client pauses until the client answers. What
the pause still waits on (its client tool requests and its questions) is read
from the paused agent each time (``AgentRuntime.pending_interactions``), never
kept here, and a client that attaches gets all of it with the attach.

Every ``SWEEP_SECONDS`` the sweep drops the attachments whose lease lapsed and
looks at each conversation whose turn waits on answers
(``AgentRuntime.sessions_awaiting_answers``). One that waits on client tool
calls while no live attachment shows it for ``CLIENT_GONE_GRACE_SECONDS`` has
those calls answered ``client_gone``, so the turn does not wait forever; a
client that attaches within the grace gets the calls instead. Presence is
thereby derived on each sweep from the directory and the runtime, and nothing
has to report a closed socket.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.runtime.client_tools import ClientToolAnswerMismatch
from jiuwenswarm.runtime.session.model import NoControlTargetError
from jiuwenswarm.server.client_attachments import directory
from jiuwenswarm.server.client_attachments.directory import Attachment, get_attachment_directory, owner_scope

logger = logging.getLogger(__name__)

#: How long the client tool calls of a conversation no live attachment shows wait for a client.
CLIENT_GONE_GRACE_SECONDS = 30.0
SWEEP_SECONDS = 10.0

Push = Callable[[dict[str, Any]], Awaitable[Any]]
#: The events of what a conversation's pause still waits on, in the order they were asked.
PendingInteractions = Callable[[str], Awaitable[list[dict[str, Any]]]]
#: The conversations whose turns wait on answers now.
AwaitingAnswers = Callable[[], Iterable[str]]
#: ``(channel_id, owner_id)`` of a conversation, read from what it stored.
ConversationRoute = Callable[[str], tuple[str, str]]
#: Answers ``(session_id, channel_id, owner_id, client tool request events)`` as ``client_gone``.
ClientGone = Callable[[str, str, str, list[dict[str, Any]]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ConversationAccess:
    pending: PendingInteractions
    awaiting: AwaitingAnswers
    route: ConversationRoute
    client_gone: ClientGone
    push: Push


_access: _ConversationAccess | None = None
# Conversation -> when a sweep first saw its client tool calls with no attachment showing it.
_unattended: dict[str, float] = {}
_answering_client_gone: dict[str, frozenset[str]] = {}
_answers: set[asyncio.Task] = set()
_sweeper: asyncio.Task | None = None


def set_conversation_access(
    pending: PendingInteractions, awaiting: AwaitingAnswers, route: ConversationRoute,
    client_gone: ClientGone, push: Push,
) -> None:
    global _access
    _access = _ConversationAccess(pending, awaiting, route, client_gone, push)


def _conversations() -> _ConversationAccess:
    if _access is None:
        raise RuntimeError(
            "client attachments have no conversation access: AgentServer calls set_conversation_access at start"
        )
    return _access


async def pending_interactions(session_id: str) -> list[dict[str, Any]]:
    """What the pause of ``session_id`` still asks its client and its user, less what is answered as gone."""
    events = await _conversations().pending(session_id)
    answering = _answering_client_gone.get(session_id, frozenset())
    return [event for event in events if event["request_id"] not in answering]


async def _pending_client_tool_requests(session_id: str) -> list[dict[str, Any]]:
    return [event for event in await pending_interactions(session_id)
            if event["event_type"] == EventType.CHAT_CLIENT_TOOL_REQUEST.value]


def start_client_attachment_sweep() -> None:
    """Sweep every ``SWEEP_SECONDS`` on the running loop until ``close_client_attachments``."""
    global _sweeper
    if _sweeper is None or _sweeper.done() or _sweeper.get_loop() is not asyncio.get_running_loop():
        _sweeper = asyncio.create_task(_sweep_forever())


async def _sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_once(directory.clock())
        except Exception:  # noqa: BLE001 - the next sweep tries again
            logger.warning("[client_attachments] the sweep failed", exc_info=True)


async def sweep_once(now: float) -> None:
    """Drop the lapsed attachments, then answer the calls that waited long enough for a client as gone."""
    lapsed = get_attachment_directory().expire(now)
    for owner_id in dict.fromkeys(owner_scope(c.owner_id) for c in lapsed):
        try:
            await announce_change(_conversations().push, owner_id)
        except Exception:  # noqa: BLE001 - best effort, as for an attach
            logger.warning("[client_attachments] announcing lapsed attachments failed", exc_info=True)
    waiting = tuple(_conversations().awaiting())
    for session_id in _unattended.keys() - set(waiting):
        del _unattended[session_id]
    for session_id in waiting:
        try:
            await _sweep_conversation(session_id, now)
        except Exception:  # noqa: BLE001 - the other conversations are swept all the same
            logger.warning("[client_attachments] sweeping %s failed", session_id, exc_info=True)


async def _sweep_conversation(session_id: str, now: float) -> None:
    calls = await _pending_client_tool_requests(session_id)
    if not calls or get_attachment_directory().connection_for_session(session_id) is not None:
        _unattended.pop(session_id, None)
        return
    since = _unattended.setdefault(session_id, now)
    if now - since < CLIENT_GONE_GRACE_SECONDS:
        return
    del _unattended[session_id]
    answering = frozenset(event["request_id"] for event in calls)
    # Held back before the answer starts, so that an attach meanwhile is not handed these calls.
    _answering_client_gone[session_id] = answering
    task = asyncio.create_task(_answer_as_gone(session_id, calls, answering))
    _answers.add(task)
    task.add_done_callback(_answers.discard)


async def _answer_as_gone(session_id: str, calls: list[dict[str, Any]], answering: frozenset[str]) -> None:
    if not await _answered_as_gone(session_id, calls, answering):
        await _give_withheld_calls_to_a_client_attached_meanwhile(session_id)


async def _answered_as_gone(session_id: str, calls: list[dict[str, Any]], answering: frozenset[str]) -> bool:
    try:
        channel_id, owner_id = await asyncio.to_thread(_conversations().route, session_id)
        await _conversations().client_gone(session_id, channel_id, owner_id, calls)
    except (ClientToolAnswerMismatch, NoControlTargetError) as exc:
        logger.info("[client_attachments] the calls of unattended %s were answered meanwhile: %s", session_id, exc)
    except Exception:  # noqa: BLE001 - the calls still wait for a client
        logger.warning("[client_attachments] answering the calls of unattended %s failed", session_id, exc_info=True)
        return False
    finally:
        # An overlapping answer of the same conversation may have taken the entry.
        if _answering_client_gone.get(session_id) is answering:
            del _answering_client_gone[session_id]
    return True


async def _give_withheld_calls_to_a_client_attached_meanwhile(session_id: str) -> None:
    connection = get_attachment_directory().connection_for_session(session_id)
    if connection is None:
        return
    try:
        for event in await _pending_client_tool_requests(session_id):
            await push_to(_conversations().push, connection, event)
    except Exception:  # noqa: BLE001 - its next attach still gets them
        logger.warning("[client_attachments] giving the calls of %s back to its client failed", session_id,
                       exc_info=True)


def close_client_attachments() -> None:
    """The server stops: forget every attachment and stop the sweep."""
    global _sweeper
    get_attachment_directory().close()
    for task in (_sweeper, *_answers):
        if task is not None:
            task.cancel()
    _sweeper = None
    _answers.clear()
    _unattended.clear()
    _answering_client_gone.clear()


async def announce_change(push: Push, owner_id: str) -> None:
    """Tell every attached client of this owner to re-read ``client.list``.

    Best effort: the change already happened, and a client that misses the
    notice reads the list on its next look.
    """
    owner = owner_scope(owner_id)
    for connection in [c for c in get_attachment_directory().connections() if owner_scope(c.owner_id) == owner]:
        payload = {"event_type": EventType.CLIENT_ATTACHMENTS_CHANGED.value,
                   "request_id": f"client_attachments_changed_{uuid.uuid4().hex}"}
        await push_to(push, connection, payload)


async def push_to(push: Push, connection: Attachment, payload: dict[str, Any]) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import build_server_push_message

    message = build_server_push_message(
        session_id=connection.session_id, request_id=str(payload.get("request_id") or ""),
        payload=payload, fallback_channel_id=connection.channel_id,
    )
    message["channel_id"] = connection.channel_id
    message["metadata"] = {**message.get("metadata", {}), "ws_id": connection.ws_id}
    await push(message)
