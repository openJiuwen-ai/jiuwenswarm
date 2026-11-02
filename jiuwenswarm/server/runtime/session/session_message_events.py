# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted Worker boundary between authorized send and durable acceptance."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.core.events import Event, EventHandlerOps, ExtensionEventBus, SessionContext

from jiuwenswarm.extensions_v2.event_types.session_receive import (
    SESSION_RECEIVE_EVENT,
    SessionReceiveConsumed,
    SessionReceiveInput,
    SessionReceiveOrigin,
    SessionReceiveResult,
)
from jiuwenswarm.extensions_v2.event_types.session_send import SESSION_SEND_EVENT, SessionSendDenied, SessionSendInput
from jiuwenswarm.server.runtime.session.session_message_service import SessionMessagingError

CommitDelivery = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass
class _DeliveryInvocation:
    """Keep host authority and receipt separate from public handler payloads."""

    context: SessionContext
    original: SessionSendInput | SessionReceiveInput
    commit: CommitDelivery
    receipt: dict[str, Any] | None = None
    result: SessionReceiveResult | None = None
    active: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def require_active(self) -> None:
        """Reject work inherited by a task after its dispatch has finished."""
        if not self.active:
            raise SessionMessagingError("DELIVERY_CONTEXT_EXPIRED", "delivery invocation has ended")


_SEND: ContextVar[_DeliveryInvocation | None] = ContextVar("session_send_invocation", default=None)
_RECEIVE: ContextVar[_DeliveryInvocation | None] = ContextVar("session_receive_invocation", default=None)


def send_invocation() -> _DeliveryInvocation | None:
    """Return the current outgoing platform call for validators and CORE."""
    return _SEND.get()


def receive_invocation() -> _DeliveryInvocation | None:
    """Return the current incoming platform call for validators and CORE."""
    return _RECEIVE.get()


def require_delivery_invocation(event_type: str, ops: EventHandlerOps | None) -> _DeliveryInvocation:
    """Require a real host call and Ops bound to its exact session identity."""
    state = send_invocation() if event_type == SESSION_SEND_EVENT else receive_invocation()
    if state is None or ops is None or ops.session_context is None:
        raise SessionMessagingError("DELIVERY_CONTEXT_REQUIRED", "platform delivery context is required")
    state.require_active()
    if ops.session_context.session_id != state.context.session_id:
        raise SessionMessagingError("INVALID_SESSION", "delivery session does not match")
    return state


def _session_context(session_id: str, metadata: Mapping[str, Any]) -> SessionContext:
    """Use only host metadata already authorized by the message service."""
    channel = metadata.get("channel_metadata")
    channel = channel if isinstance(channel, Mapping) else {}
    candidates = (channel.get("cwd"), metadata.get("cwd"), metadata.get("project_dir"), channel.get("project_dir"))
    cwd = next((value for value in candidates if isinstance(value, str) and value and Path(value).is_absolute()), None)
    root = metadata.get("project_dir")
    facts = {"project_root": root} if isinstance(root, str) and root and Path(root).is_absolute() else {}
    return SessionContext(session_id=session_id, cwd=cwd, metadata=facts)


async def _dispatch_receive(
    bus: ExtensionEventBus, context: SessionContext, text: str, commit: CommitDelivery,
) -> dict[str, Any]:
    if receive_invocation() is not None:
        raise SessionMessagingError("SESSION_RECEIVE_REENTRANT", "nested receive is not allowed")
    original = SessionReceiveInput(origin=SessionReceiveOrigin(kind="peer-send-message"), text=text)
    state = _DeliveryInvocation(context, copy.deepcopy(original), commit)
    token = _RECEIVE.set(state)
    try:
        result = await bus.trigger(Event(SESSION_RECEIVE_EVENT, copy.deepcopy(original)), context)
        if state.receipt is not None:
            return copy.deepcopy(state.receipt)
        if isinstance(result, SessionReceiveConsumed):
            reason = result.consumed.strip()
            return {
                "accepted": False, "target_session_id": context.session_id,
                "code": "SESSION_RECEIVE_CONSUMED", "error": reason, "consumed": reason,
            }
        raise SessionMessagingError("DELIVERY_RESULT_INVALID", "receive did not produce a real queue receipt")
    finally:
        state.active = False
        _RECEIVE.reset(token)


async def route_session_message(
    *, source_session_id: str, target_session_id: str, text: str, input_mode: str,
    source_metadata: Mapping[str, Any], target_metadata: Mapping[str, Any], commit: CommitDelivery,
) -> dict[str, Any]:
    """Run send then receive around one original durable mailbox operation.

    The caller must authorize both sessions first. A disabled assembly keeps
    the original platform path; this function never invents a successful row.
    """
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus

    if send_invocation() is not None or receive_invocation() is not None:
        raise SessionMessagingError("SESSION_SEND_REENTRANT", "nested send from a delivery hook is not allowed")
    bus = get_extension_bus()
    if bus is None:
        return await commit(text)
    source_context = _session_context(source_session_id, source_metadata)
    target_context = _session_context(target_session_id, target_metadata)

    async def receive(body: str) -> dict[str, Any]:
        return await _dispatch_receive(bus, target_context, body, commit)

    original = SessionSendInput(target_session_id, text, input_mode or "follow_up")
    state = _DeliveryInvocation(source_context, copy.deepcopy(original), receive)
    token = _SEND.set(state)
    try:
        result = await bus.trigger(Event(SESSION_SEND_EVENT, copy.deepcopy(original)), source_context)
        if state.receipt is not None:
            return copy.deepcopy(state.receipt)
        if isinstance(result, SessionSendDenied):
            reason = result.deny.strip()
            return {
                "accepted": False, "target_session_id": target_session_id,
                "code": "SESSION_SEND_DENIED", "error": reason, "deny": reason,
            }
        raise SessionMessagingError("DELIVERY_RESULT_INVALID", "send did not produce a real queue receipt")
    finally:
        state.active = False
        _SEND.reset(token)
