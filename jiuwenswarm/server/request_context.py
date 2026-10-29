from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any

from jiuwenswarm.common.device_rpc.models import DeviceCommandContext
from jiuwenswarm.common.schema.agent import AgentRequest


_current_device_context: ContextVar[DeviceCommandContext | None] = ContextVar(
    "current_device_context",
    default=None,
)
_current_agent_request: ContextVar[AgentRequest | None] = ContextVar(
    "current_agent_request",
    default=None,
)
_current_stream_stop: ContextVar[asyncio.Event | None] = ContextVar("current_stream_stop", default=None)


def set_current_stream_stop(event: asyncio.Event) -> Token:
    return _current_stream_stop.set(event)


def get_current_stream_stop() -> asyncio.Event | None:
    return _current_stream_stop.get()


def reset_current_stream_stop(token: Token) -> None:
    _current_stream_stop.reset(token)


def raise_if_request_cancelled() -> None:
    event = get_current_stream_stop()
    task = asyncio.current_task()
    if (event is not None and event.is_set()) or (task is not None and task.cancelling()):
        raise asyncio.CancelledError


async def await_request_cleanup(awaitable):
    """Finish owned cleanup even when the caller is cancelled a second time.

    The owner keeps awaiting this task, so admission is not released while an
    output lease or partially initialized instance is still being torn down.
    RPC callers can time out independently without cancelling this cleanup.
    """
    task = asyncio.ensure_future(awaitable)
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


def set_device_context(context: DeviceCommandContext) -> Token:
    return _current_device_context.set(context)


def get_device_context() -> DeviceCommandContext | None:
    return _current_device_context.get()


def reset_device_context(token: Token) -> None:
    _current_device_context.reset(token)


def set_current_agent_request(request: AgentRequest) -> Token:
    return _current_agent_request.set(request)


def get_current_agent_request() -> AgentRequest | None:
    return _current_agent_request.get()


def reset_current_agent_request(token: Token) -> None:
    _current_agent_request.reset(token)


def build_device_context_from_request(request: AgentRequest) -> DeviceCommandContext:
    metadata = dict(request.metadata or {})
    params = request.params if isinstance(request.params, dict) else {}
    return DeviceCommandContext(
        source_request_id=str(request.request_id or ""),
        channel_id=str(request.channel_id or ""),
        jiuwen_session_id=request.session_id,
        xiaoyi_root_session_id=_first_text(
            metadata.get("xiaoyi_root_session_id"),
            metadata.get("xiaoyi_session_id"),
            request.chat_id,
        ),
        xiaoyi_params_session_id=_first_text(
            metadata.get("xiaoyi_params_session_id"),
            params.get("xiaoyi_session_id"),
            params.get("session_id"),
        ),
        xiaoyi_task_id=_first_text(
            metadata.get("xiaoyi_task_id"),
            params.get("task_id"),
            request.request_id,
        ),
        xiaoyi_rpc_id=_first_text(metadata.get("xiaoyi_rpc_id")),
        metadata=metadata,
    )


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
