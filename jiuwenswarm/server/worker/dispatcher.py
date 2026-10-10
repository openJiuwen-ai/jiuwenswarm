# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution dispatch inside the Worker. The Worker owns active requests."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.ipc.protocol import (
    KIND_CANCEL,
    KIND_GATEWAY_ATTACH,
    KIND_GATEWAY_DETACH,
    KIND_HEARTBEAT,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    cancel_request,
    load_agent_request,
)
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.gateway_bridge import (
    WorkerGatewayBridge,
    begin_delivery_tracking,
    begin_request_generation,
    end_delivery_tracking,
    end_request_generation,
)

logger = logging.getLogger(__name__)

_MAX_QUEUED_REQUESTS = 32
_DRAIN_TIMEOUT = 5.0


class WorkerDispatcher:
    """Turn IPC messages into Runtime calls and stream wire frames back."""

    def __init__(self, bridge: WorkerGatewayBridge) -> None:
        self._bridge = bridge
        self._lock = asyncio.Lock()
        self._runtime: Any = None
        self._attached = False
        self._queued: list[tuple[int, AgentRequest]] = []
        self._tasks: dict[asyncio.Task[None], int] = {}
        self._state_lock = asyncio.Lock()
        self.shutdown_requested = False
        self._draining = False

    async def bind_runtime(self, runtime: Any) -> None:
        """Publish the Runtime before it reports ``AGENT_READY``."""
        async with self._state_lock:
            self._runtime = runtime
            attached = self._attached
            queued = self._queued
            self._queued = []
            live = self._bridge.holder.generation
        if attached:
            runtime.attach_gateway_connection(self._bridge, self._lock)
        for generation, request in queued:
            if self._draining or generation != live:
                logger.info(
                    "[Worker] dropped queued request_id=%s generation=%s",
                    request.request_id,
                    generation,
                )
                continue
            self._spawn(self._run_request(request, generation), generation)

    async def handle(self, message: dict[str, Any], conn: IpcConnection) -> bool:
        """Handle one frame. Return False when the session should close."""
        kind = str(message.get("kind") or "")
        generation = self._bridge.holder.generation
        if not _frame_matches_session(message, generation):
            logger.warning(
                "[Worker] dropped stale ipc kind=%s generation=%s current=%s",
                kind,
                message.get("generation"),
                generation,
            )
            return True
        if kind == KIND_HEARTBEAT:
            await conn.send({"kind": KIND_HEARTBEAT, "pid": os.getpid()})
            return True
        if kind == KIND_SHUTDOWN:
            self.shutdown_requested = True
            return False
        if kind == KIND_GATEWAY_ATTACH:
            async with self._state_lock:
                self._attached = True
                runtime = self._runtime
            if runtime is not None:
                runtime.attach_gateway_connection(self._bridge, self._lock)
            return True
        if kind == KIND_GATEWAY_DETACH:
            await self.detach_gateway(message.get("remote"))
            return True
        if kind == KIND_CANCEL:
            request = cancel_request(message)
            if await self._drop_queued_cancel(request):
                await self._finish(
                    generation,
                    str(request.request_id or ""),
                    ok=True,
                    error="",
                    delivered=False,
                )
                return True
            self._spawn(self._run_request(request, generation), generation)
            return True
        if kind == KIND_REQUEST:
            payload = message.get("payload")
            if not isinstance(payload, dict):
                await self._done(
                    conn,
                    str(message.get("request_id") or ""),
                    generation=generation,
                    ok=False,
                    error="missing payload",
                )
                return True
            self._spawn(self._run_request(load_agent_request(payload), generation), generation)
            return True
        logger.info("[Worker] ignored ipc kind=%s", kind)
        return True

    async def detach_gateway(self, remote: object = None) -> None:
        """Run Runtime disconnect cleanup once per attached Gateway session."""
        async with self._state_lock:
            if not self._attached:
                return
            self._attached = False
            runtime = self._runtime
        if runtime is None:
            return
        disconnect = getattr(runtime, "on_gateway_disconnect", None)
        if not callable(disconnect):
            return
        try:
            await disconnect(self._bridge, remote)
        except Exception:  # noqa: BLE001
            logger.exception("[Worker] gateway disconnect cleanup failed")

    async def retire_generation(self, generation: int) -> None:
        """Cancel work owned by a supervisor connection that has gone away."""
        async with self._state_lock:
            self._queued = [item for item in self._queued if item[0] != generation]
            tasks = [task for task, owner in self._tasks.items() if owner == generation]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(set(tasks), timeout=_DRAIN_TIMEOUT)

    async def drain(self, *, timeout: float = _DRAIN_TIMEOUT) -> bool:
        """Stop new work and cancel active dispatch.

        Return False when a task is still running after ``timeout``. The caller
        must not shut the Runtime down while that task can still use it.
        """
        async with self._state_lock:
            self._draining = True
            self.shutdown_requested = True
            self._queued.clear()
            tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return True
        done, pending = await asyncio.wait(set(tasks), timeout=timeout)
        for task in pending:
            logger.warning("[Worker] dispatch still running after shutdown timeout")
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning("[Worker] dispatch ended during shutdown: %s", exc)
        return not pending

    def _spawn(self, coro: Any, generation: int) -> None:
        task = asyncio.create_task(coro)
        self._tasks[task] = generation
        task.add_done_callback(self._forget_task)

    def _forget_task(self, task: asyncio.Task) -> None:
        self._tasks.pop(task, None)

    async def _drop_queued_cancel(self, request: AgentRequest) -> bool:
        """Drop a cancel that arrived before Runtime exists. Return True when handled."""
        target = str((request.params or {}).get("request_id") or "")
        session_id = request.session_id or ""
        async with self._state_lock:
            if self._runtime is not None or self._draining:
                return self._draining
            kept: list[tuple[int, AgentRequest]] = []
            dropped: list[tuple[int, AgentRequest]] = []
            for generation, queued in self._queued:
                same_request = bool(target) and str(queued.request_id or "") == target
                same_session = bool(session_id) and (queued.session_id or "") == session_id
                if same_request or same_session:
                    dropped.append((generation, queued))
                    continue
                kept.append((generation, queued))
            self._queued = kept
        for generation, queued in dropped:
            logger.info(
                "[Worker] cancelled queued request_id=%s generation=%s",
                queued.request_id,
                generation,
            )
            await self._finish(
                generation,
                str(queued.request_id or ""),
                ok=False,
                error="cancelled before runtime ready",
                delivered=False,
            )
        return True

    async def _run_request(self, request: AgentRequest, generation: int) -> None:
        async with self._state_lock:
            if self._draining or generation != self._bridge.holder.generation:
                logger.info(
                    "[Worker] dropped request_id=%s generation=%s draining=%s",
                    request.request_id,
                    generation,
                    self._draining,
                )
                return
            runtime = self._runtime
            if runtime is None:
                if len(self._queued) >= _MAX_QUEUED_REQUESTS:
                    overflow = True
                else:
                    self._queued.append((generation, request))
                    return
            else:
                overflow = False
        if overflow:
            await self._finish(
                generation,
                str(request.request_id or ""),
                ok=False,
                error="worker queue is full",
                delivered=False,
            )
            return
        gen_token = begin_request_generation(generation)
        token, box = begin_delivery_tracking()
        error = ""
        ok = True
        try:
            await runtime.dispatch_parsed_request(self._bridge, request, self._lock)
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = str(exc)
            logger.exception(
                "[Worker] dispatch failed: request_id=%s", request.request_id
            )
        finally:
            end_delivery_tracking(token)
            end_request_generation(gen_token)
        await self._finish(
            generation,
            str(request.request_id or ""),
            ok=ok,
            error=error,
            delivered=bool(box),
        )

    async def _finish(
        self,
        generation: int,
        request_id: str,
        *,
        ok: bool,
        error: str,
        delivered: bool,
    ) -> None:
        if generation != self._bridge.holder.generation:
            logger.info(
                "[Worker] dropped request.done request_id=%s generation=%s current=%s",
                request_id,
                generation,
                self._bridge.holder.generation,
            )
            return
        conn = self._bridge.holder.conn
        if conn is None:
            return
        try:
            await self._done(
                conn,
                request_id,
                generation=generation,
                ok=ok,
                error=error,
                delivered=delivered,
            )
        except (OSError, RuntimeError):
            logger.info("[Worker] request.done dropped; supervisor disconnected")

    async def _done(
        self,
        conn: IpcConnection,
        request_id: str,
        *,
        generation: int,
        ok: bool,
        error: str = "",
        delivered: bool = False,
    ) -> None:
        await conn.send(
            {
                "kind": KIND_REQUEST_DONE,
                "request_id": request_id,
                "generation": generation,
                "ok": ok,
                "error": error,
                "delivered": delivered,
            }
        )


def _frame_matches_session(message: dict[str, Any], generation: int) -> bool:
    raw = message.get("generation")
    if raw is None:
        return True
    return isinstance(raw, int) and raw == generation
