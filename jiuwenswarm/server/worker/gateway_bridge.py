# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Virtual Gateway socket. Worker writes land on IPC, never on the Front port."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextvars import ContextVar
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from weakref import WeakValueDictionary

from jiuwenswarm.server.ipc.protocol import KIND_WIRE
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.event_store import WorkerEventStore

logger = logging.getLogger(__name__)

_DELIVERED: ContextVar[list[bool] | None] = ContextVar("runtime_ipc_delivered", default=None)
_REQUEST_GENERATION: ContextVar[int | None] = ContextVar("runtime_ipc_generation", default=None)
_DISPATCH_ID: ContextVar[str] = ContextVar("runtime_dispatch_id", default="")
# The Front allows ten seconds for Gateway forwarding. Leave time for its
# failure acknowledgement before treating the IPC delivery itself as lost.
_DELIVERY_TIMEOUT = 15.0


class ConnHolder:
    """Current supervisor connection. Rebind does not reload the Runtime."""

    def __init__(self) -> None:
        self.conn: IpcConnection | None = None
        self.generation = 0
        self.last_readiness: dict[str, Any] | None = None

    def open_session(self, conn: IpcConnection) -> int:
        """Bind a new supervisor connection and return its generation."""
        self.generation += 1
        self.conn = conn
        return self.generation

    def close_session(self) -> int:
        """Detach the current connection without reusing its generation."""
        generation = self.generation
        self.conn = None
        return generation

    def remember(self, message: dict[str, Any]) -> None:
        self.last_readiness = message


class WorkerGatewayBridge:
    """Object passed to Agent Runtime wherever it used to hold a WebSocket."""

    def __init__(self, holder: ConnHolder) -> None:
        self._holder = holder
        self.remote_address = ("runtime-worker", 0)
        self.events = WorkerEventStore()
        self._publication_locks: WeakValueDictionary[tuple[str, ...], asyncio.Lock] = WeakValueDictionary()
        self._acks: dict[str, tuple[asyncio.Future[bool] | None, list[bool] | None, int]] = {}

    @property
    def holder(self) -> ConnHolder:
        return self._holder

    def publication_lock(self, scope: tuple[str, ...]) -> asyncio.Lock:
        """Order recovery and live frames only within their resolved caller scope."""
        lock = self._publication_locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._publication_locks[scope] = lock
        return lock

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        payload = json.loads(message)
        request_id = ""
        if isinstance(payload, dict):
            request_id = str(payload.get("request_id") or "")
        dispatch_id = _DISPATCH_ID.get()
        # Streams belong to the execution, not to the connection on which it
        # began. Unary replies retain delivery semantics for session transactions.
        metadata = payload.get("metadata") or {}
        from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY

        stream = bool(payload.get("is_stream") or payload.get("is_complete") is not None)
        critical = bool(dispatch_id) and not stream and not metadata.get(E2A_WIRE_SERVER_PUSH_KEY)
        wire_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future() if critical else None
        try:
            async with self.publication_lock(self.events.publication_scope(payload, dispatch_id)):
                generation = self._holder.generation
                if critical and _REQUEST_GENERATION.get() not in {None, generation}:
                    raise ConnectionError("response delivery connection was replaced")
                if stream or metadata.get(E2A_WIRE_SERVER_PUSH_KEY):
                    frame = self.events.append(payload, dispatch_id)
                else:
                    frame = {"request_id": request_id, "dispatch_id": dispatch_id, "payload": payload}
                conn = self._holder.conn
                if conn is None:
                    if critical:
                        raise ConnectionError("response has no Gateway delivery path")
                    return
                self._acks[wire_id] = (future, _DELIVERED.get(), generation)
                while len(self._acks) > 4096:
                    discard = next((key for key, value in self._acks.items() if value[0] is None), None)
                    if discard is None:
                        break
                    del self._acks[discard]
                await conn.send({**frame, "kind": KIND_WIRE, "wire_id": wire_id,
                                 "generation": generation, "requires_ack": critical})
            if future is not None and not await asyncio.wait_for(future, timeout=_DELIVERY_TIMEOUT):
                raise ConnectionError("Front could not deliver response to Gateway")
        except (OSError, RuntimeError, asyncio.TimeoutError):
            if critical:
                raise
            logger.info("[Worker] stream retained for Gateway reattachment")
        finally:
            if critical:
                self._acks.pop(wire_id, None)

    def acknowledge(self, message: dict[str, Any]) -> None:
        wire_id = str(message.get("wire_id") or "")
        pending = self._acks.get(wire_id)
        if pending is None or message.get("generation") != pending[2]:
            return
        self._acks.pop(wire_id, None)
        future, flag, _ = pending
        delivered = message.get("delivered") is True
        if delivered and flag is not None:
            flag.append(True)
        if future is not None and not future.done():
            future.set_result(delivered)

    def detach(self, generation: int) -> None:
        for wire_id, (future, _flag, owner) in list(self._acks.items()):
            if owner != generation:
                continue
            self._acks.pop(wire_id, None)
            if future is not None and not future.done():
                future.set_result(False)

    async def replay(self, frames: list[dict[str, Any]], generation: int,
                     include: Callable[[dict[str, Any]], bool] | None = None) -> None:
        for index, frame in enumerate(frames):
            if self._holder.generation != generation or self._holder.conn is None:
                return
            if include is not None and not include(frame):
                continue
            frame["payload"]["metadata"]["runtime_event"]["replayed"] = True
            await self._holder.conn.send({**frame, "kind": KIND_WIRE,
                                         "generation": generation, "replayed": True})
            if index % 32 == 0:
                await asyncio.sleep(0)

    async def close(self) -> None:
        return None

    @contextmanager
    def execution_scope(self, request: Any):
        """Give an internal authorized execution its own journal and entry hook."""
        from jiuwenswarm.runtime.execution_telemetry import execution_entry_sink

        parent = self.events.records.get(_DISPATCH_ID.get())
        metadata = dict(request.metadata or {})
        if parent and parent.scope[2]:
            metadata.setdefault("app_id", parent.scope[2])
        accepted = replace(request, metadata=metadata)
        record = self.events.internal_record(accepted)
        record.active = True
        record.is_processing = True
        token = _DISPATCH_ID.set(record.dispatch_id)
        entry = execution_entry_sink.set(self.observe_execution_entry)
        try:
            yield
        finally:
            execution_entry_sink.reset(entry)
            _DISPATCH_ID.reset(token)
            self.events.finish(record.dispatch_id)

    def observe_execution_entry(self, request: Any, mode: str) -> None:
        record = self.events.records.get(_DISPATCH_ID.get())
        if record is None or record.entered_at_ns is not None:
            return
        record.entered_at_ns = time.perf_counter_ns()
        logger.info("[Worker] prepared execution entered dispatch_id=%s request_id=%s mode=%s",
                    record.dispatch_id, request.request_id, mode)


def begin_delivery_tracking() -> Any:
    box: list[bool] = []
    return _DELIVERED.set(box), box


def end_delivery_tracking(token: Any) -> None:
    _DELIVERED.reset(token)


def begin_request_generation(generation: int) -> Any:
    return _REQUEST_GENERATION.set(generation)


def end_request_generation(token: Any) -> None:
    _REQUEST_GENERATION.reset(token)


def begin_request_dispatch(dispatch_id: str) -> Any:
    return _DISPATCH_ID.set(dispatch_id)


def end_request_dispatch(token: Any) -> None:
    _DISPATCH_ID.reset(token)
