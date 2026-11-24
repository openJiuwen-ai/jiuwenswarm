# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Connection facts for one WebUI call, independent of chat session facts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True, weakref_slot=True)
class WebUIConnectionContext:
    """Shared lifetime marker for calls belonging to one browser connection."""

    connection_id: str
    user_id: str = ""
    channel_id: str = "web"
    closed: bool = False


_CONNECTION: ContextVar[WebUIConnectionContext | None] = ContextVar("webui_connection", default=None)


def current_connection() -> WebUIConnectionContext | None:
    """Return the connection bound to this asynchronous call."""
    return _CONNECTION.get()


@contextmanager
def bind_connection(connection: WebUIConnectionContext | None) -> Iterator[None]:
    """Bind connection facts until this call returns, fails, or is cancelled."""
    token = _CONNECTION.set(connection)
    try:
        yield
    finally:
        _CONNECTION.reset(token)
