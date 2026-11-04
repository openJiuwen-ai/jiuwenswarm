# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task-scoped authorization dispatch and validated value unwrapping."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from openjiuwen.core.events import Event
from openjiuwen.core.common.exception.errors import ValidationError

from jiuwenswarm.extensions_v2.event_types.session_authorize import (
    SESSION_AUTHORIZE_EVENT, SessionAuthorizationDenied, SessionAuthorizationError,
    valid_authorize_input, valid_authorize_output,
)


@dataclass(slots=True)
class _AuthorizationFrame:
    service: object
    caller: object
    context: object
    task: object
    active: bool = True
    core_called: bool = False
    issued: object = None


_FRAME = ContextVar("session_authorization_call", default=None)


def current_frame() -> _AuthorizationFrame | None:
    frame = _FRAME.get()
    if frame is None or not frame.active or frame.task is not asyncio.current_task():
        return None
    return frame


def authorize_core(event, ops) -> dict:
    frame = current_frame()
    if frame is None or ops is None or ops.session_context != frame.context or not valid_authorize_input(event.event_input):
        raise SessionAuthorizationError("UNBOUND_INVOCATION")
    if not frame.core_called:
        frame.issued = frame.service.issue(frame.caller, frame.context.session_id)
        frame.core_called = True
    return {"value": None if frame.issued is None else dict(frame.issued)}


async def dispatch_authorization(service, ops, caller):
    if current_frame() is not None:
        raise SessionAuthorizationError("AUTHORIZATION_REENTRANT")
    frame = _AuthorizationFrame(service, caller, ops.session_context, asyncio.current_task())
    token = _FRAME.set(frame)
    retained = False
    try:
        try:
            output = await service.bus.trigger(
                Event(SESSION_AUTHORIZE_EVENT, MappingProxyType({})), frame.context,
            )
        except ValidationError:
            raise SessionAuthorizationError("INVALID_RESULT") from None
        if not valid_authorize_output(output):
            raise SessionAuthorizationError("INVALID_RESULT")
        if not service.caller_is_current(ops, caller):
            raise SessionAuthorizationError("UNBOUND_INVOCATION")
        if "deny" in output:
            raise SessionAuthorizationDenied(output["deny"].strip())
        result = output["value"]
        if result is None:
            return None
        if not isinstance(result, Mapping) or not service.valid_result(frame, result):
            raise SessionAuthorizationError("INVALID_RESULT")
        retained = True
        return {"handle": result["handle"], "kind": result["kind"]}
    finally:
        if not retained and frame.issued is not None:
            service.revoke_handle(frame.issued["handle"])
        frame.active = False
        _FRAME.reset(token)
