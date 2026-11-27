# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Private invocation facts for catalog descriptions and native save continuations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass

from openjiuwen.core.events import Event, EventHandlerOps, SessionContext

from jiuwenswarm.extensions_v2.event_types.config import ConfigExtensionError


@dataclass(slots=True)
class ConfigFrame:
    original: dict
    context: SessionContext | None
    task: asyncio.Task | None
    continuation: Callable[[bool | str], Awaitable[dict]] | None = None
    active: bool = True
    core_called: bool = False
    result: object = None


DESCRIBE_FRAME: ContextVar[ConfigFrame | None] = ContextVar("config_describe_frame", default=None)
SET_FRAME: ContextVar[ConfigFrame | None] = ContextVar("config_set_frame", default=None)


def current_frame(variable: ContextVar) -> ConfigFrame | None:
    frame = variable.get()
    return frame if frame is not None and frame.active and frame.task is asyncio.current_task() else None


def valid_describe_input(value: object) -> bool:
    from jiuwenswarm.extensions_v2.event_types.config import valid_describe_result

    frame = current_frame(DESCRIBE_FRAME)
    return bool(
        frame is not None and isinstance(value, Mapping)
        and set(value) == set(frame.original)
        and all(value[key] == frame.original[key] for key in ("key", "provider", "scope"))
        and valid_describe_result({key: value[key] for key in ("label", "description", "isHidden")})
    )


def valid_set_input(value: object) -> bool:
    frame = current_frame(SET_FRAME)
    return bool(
        frame is not None and isinstance(value, Mapping) and set(value) == set(frame.original)
        and type(value["value"]) is type(frame.original["value"])
        and type(value["previous"]) is type(frame.original["previous"])
        and all(value[key] == frame.original[key] for key in ("key", "previous", "provider", "origin", "scope"))
    )


def describe_core(event: Event, ops: EventHandlerOps | None) -> dict:
    frame = current_frame(DESCRIBE_FRAME)
    if frame is None or not valid_describe_input(event.event_input):
        raise ConfigExtensionError("UNBOUND_INVOCATION")
    if ops is None or ops.session_context != frame.context:
        raise ConfigExtensionError("UNBOUND_INVOCATION")
    return {key: event.event_input[key] for key in ("label", "description", "isHidden")}


async def set_core(event: Event, ops: EventHandlerOps | None) -> dict:
    from jiuwenswarm.extensions_v2.config_extension.catalog import BY_KEY

    frame = current_frame(SET_FRAME)
    if frame is None or frame.continuation is None or not valid_set_input(event.event_input):
        raise ConfigExtensionError("UNBOUND_INVOCATION")
    if ops is None or ops.session_context != frame.context:
        raise ConfigExtensionError("UNBOUND_INVOCATION")
    if not BY_KEY[frame.original["key"]].validate(event.event_input["value"]):
        raise ConfigExtensionError("INVALID_VALUE")
    if not frame.core_called:
        frame.core_called = True
        frame.result = await frame.continuation(event.event_input["value"])
    return dict(frame.result)
