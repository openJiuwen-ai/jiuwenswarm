# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage Runtime Workers. This process does not execute Agent turns."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import encode_response
from jiuwenswarm.server.ipc.client import connect_with_retry, try_connect
from jiuwenswarm.server.ipc.protocol import (
    KIND_GATEWAY_ATTACH,
    KIND_GATEWAY_DETACH,
    KIND_HELLO,
    KIND_HEARTBEAT,
    KIND_READINESS,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    KIND_WIRE,
    PROTOCOL_VERSION,
    dump_agent_request,
)
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor.affinity import SessionAffinity
from jiuwenswarm.server.supervisor.worker_process import (
    WorkerProcess,
    default_socket_path,
)
from jiuwenswarm.server.supervisor.worker_registry import WorkerRegistry, WorkerSlot

logger = logging.getLogger(__name__)


def _is_server_push(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY

    return bool(metadata.get(E2A_WIRE_SERVER_PUSH_KEY))


_HEARTBEAT_INTERVAL = 10.0
_HEARTBEAT_TIMEOUT = 30.0
_HELLO_TIMEOUT = 10.0


class RuntimeSupervisor:
    """Spawn, health-check, and route execution to the resident Worker.

    Implements the Front ``RuntimeBackend`` protocol. Control-plane RPCs never
    reach this object. Warming requests stay in ``ExecutionAdmission``; this
    class only starts forwarding once the Worker reports ``AGENT_READY``.
    """

    def __init__(
        self,
        readiness: Readiness,
        *,
        forwarder: EventForwarder | None = None,
        socket_path: str | None = None,
        host: str = "127.0.0.1",
        port: int = 18092,
        stub: bool = False,
        stub_ready_delay: float = 0.0,
        stub_exit_on_request: bool = False,
        max_restarts: int = 3,
        connect_timeout: float = 30.0,
    ) -> None:
        self._readiness = readiness
        self._forwarder = forwarder
        self._socket_path = socket_path or default_socket_path()
        self._host = host
        self._port = port
        self._stub = stub
        self._stub_ready_delay = stub_ready_delay
        self._stub_exit_on_request = stub_exit_on_request
        self._max_restarts = max_restarts
        self._connect_timeout = connect_timeout
        self._affinity = SessionAffinity()
        self._registry = WorkerRegistry()
        self._conn: IpcConnection | None = None
        self._process: WorkerProcess | None = None
        self._owns_process = False
        self._worker_pid: int | None = None
        self._worker_instance: str | None = None
        self._runtime_loaded = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._routes: dict[str, tuple[Any, asyncio.Lock]] = {}
        self._gateway_ws: Any = None
        self._gateway_lock: asyncio.Lock | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._side_tasks: list[asyncio.Task[None]] = []
        self._ipc_ready = asyncio.Event()
        self._stopping = False
        self._fatal = False
        self._fatal_reason = ""
        self._restart_count = 0
        self._generation = 0
        self._peer_generation: int | None = None
        self._last_rx = 0.0
        self._session_lock = asyncio.Lock()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def owns_process(self) -> bool:
        return self._owns_process

    @property
    def worker_pid(self) -> int | None:
        return self._worker_pid

    @property
    def worker_instance(self) -> str | None:
        """Stable id from the Worker hello, independent of the launcher pid."""
        return self._worker_instance

    @property
    def runtime_loaded(self) -> bool:
        """True when the connected Worker already has Agent Runtime loaded."""
        return self._runtime_loaded

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    def worker_for_session(self, session_id: str | None) -> str:
        return self._affinity.worker_for(session_id)

    async def run_until_ipc_ready(self) -> None:
        """Connect to a resident Worker, or spawn one, then return."""
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._supervise(), name="runtime-supervisor")
        await self._ipc_ready.wait()
        if self._fatal and self._conn is None:
            raise RuntimeError(self._fatal_reason or "runtime worker failed to start")

    def attach_gateway_connection(self, ws: Any, send_lock: asyncio.Lock) -> None:
        """Remember the Front-owned Gateway socket. The Worker never sees it."""
        self._gateway_ws = ws
        self._gateway_lock = send_lock
        self._schedule(self._notify_gateway(attached=True))

    async def on_gateway_disconnect(self, ws: Any, remote: Any) -> None:
        if self._gateway_ws is ws:
            self._gateway_ws = None
            self._gateway_lock = None
        await self._notify_gateway(attached=False, remote=remote)

    async def dispatch_parsed_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Forward one execution request and wait until the Worker finishes it."""
        worker_id = self._affinity.worker_for(request.session_id)
        slot = self._registry.get(worker_id)
        if slot is None or self._conn is None:
            await self._emit_error(ws, request, send_lock, "runtime worker is not connected")
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        request_id = str(request.request_id or "")
        if request_id:
            self._pending[request_id] = future
            self._routes[request_id] = (ws, send_lock)
        try:
            await self._conn.send(
                {
                    "kind": KIND_REQUEST,
                    "generation": self._peer_generation,
                    "request_id": request_id,
                    "session_id": request.session_id or "",
                    "worker_id": worker_id,
                    "payload": dump_agent_request(request),
                }
            )
            if not request_id:
                return
            result = await future
        except Exception as exc:  # noqa: BLE001
            await self._emit_error(ws, request, send_lock, f"runtime worker closed: {exc}")
            return
        finally:
            self._pending.pop(request_id, None)
            self._routes.pop(request_id, None)
        if request_id and not result.get("ok", False) and not result.get("delivered", False):
            error = str(result.get("error") or "runtime worker closed")
            await self._emit_error(ws, request, send_lock, error)

    async def stop(self, *, terminate_worker: bool = False) -> None:
        """Detach from the Worker so this Front can exit.

        A Worker this supervisor spawned stays running, so the next Front
        reconnects instead of loading Agent Runtime again.

        ``terminate_worker=True`` stops only a Worker this supervisor spawned.
        A reattached Front does not own that process and cannot terminate it.
        Reaping a resident Worker on upgrade, uninstall, or final exit is a
        later lifecycle task.
        """
        self._stopping = True
        conn = self._conn
        if conn is not None:
            try:
                await conn.send({"kind": KIND_GATEWAY_DETACH, "remote": "front-stop"})
            except (OSError, RuntimeError):
                logger.info("[Supervisor] gateway detach skipped; worker ipc is down")
        if terminate_worker and self._owns_process:
            await self._shutdown_owned_worker()
        elif terminate_worker:
            logger.warning(
                "[Supervisor] terminate_worker ignored: this Front reattached "
                "and cannot shut down the resident Worker"
            )
            self._owns_process = False
            self._process = None
        else:
            self._owns_process = False
            self._process = None
        self._conn = None
        if conn is not None:
            await conn.close()
        await self._cancel_side_tasks()
        task = self._loop_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._ipc_ready.set()

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                await self._bring_up()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Supervisor] worker bring-up failed: %s", exc)
                if not await self._schedule_restart(str(exc)):
                    break
                continue
            self._ipc_ready.set()
            try:
                await self._pump()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Supervisor] worker session ended: %s", exc)
            if self._stopping:
                await self._fail_pending("runtime worker stopped")
                break
            await self._fail_pending("runtime worker closed")
            self._conn = None
            self._registry.mark_ready("primary", ready=False)
            if self._fatal:
                self._readiness.mark_failed(self._fatal_reason or "runtime worker failed")
                break
            if not await self._schedule_restart("runtime worker closed"):
                break

    async def _bring_up(self) -> None:
        await self._cancel_side_tasks()
        async with self._session_lock:
            existing = await try_connect(self._socket_path, timeout=0.5)
            if existing is not None:
                self._conn = existing
                self._owns_process = False
                self._process = None
                logger.info("[Supervisor] reattached to resident runtime worker")
            else:
                process = WorkerProcess(
                    self._socket_path,
                    host=self._host,
                    port=self._port,
                    stub=self._stub,
                    stub_ready_delay=self._stub_ready_delay,
                    stub_exit_on_request=self._stub_exit_on_request,
                )
                await process.spawn()
                self._process = process
                self._owns_process = True
                self._conn = await connect_with_retry(
                    self._socket_path, timeout=self._connect_timeout
                )
        conn = self._conn
        if conn is None:
            raise ConnectionError("runtime worker ipc missing after bring-up")
        try:
            hello = await asyncio.wait_for(conn.recv(), timeout=_HELLO_TIMEOUT)
            if not isinstance(hello, dict) or hello.get("kind") != KIND_HELLO:
                kind = hello.get("kind") if isinstance(hello, dict) else type(hello).__name__
                raise RuntimeError(f"expected hello, got {kind!r}")
            if int(hello.get("protocol") or 0) != PROTOCOL_VERSION:
                raise RuntimeError("runtime ipc protocol mismatch")
        except Exception:
            self._conn = None
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                logger.debug("[Supervisor] ipc close after bad hello failed", exc_info=True)
            raise
        self._generation += 1
        peer = hello.get("generation")
        self._peer_generation = peer if isinstance(peer, int) else self._generation
        self._runtime_loaded = bool(hello.get("runtime_loaded"))
        self._last_rx = time.monotonic()
        pid = hello.get("pid")
        self._worker_pid = int(pid) if isinstance(pid, int) else None
        instance = hello.get("instance")
        self._worker_instance = instance if isinstance(instance, str) and instance else None
        self._registry.upsert(
            WorkerSlot(
                worker_id="primary",
                pid=self._worker_pid,
                socket_path=self._socket_path,
                ready=False,
            )
        )
        self._start_side_tasks()
        if self._gateway_ws is not None:
            await conn.send({"kind": KIND_GATEWAY_ATTACH})

    async def _pump(self) -> None:
        conn = self._conn
        generation = self._generation
        if conn is None:
            return
        while not self._stopping and self._generation == generation:
            message = await conn.recv()
            self._last_rx = time.monotonic()
            await self._handle(message)

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = str(message.get("kind") or "")
        if kind == KIND_WIRE:
            if not self._accepts_peer_generation(message):
                logger.warning(
                    "[Supervisor] dropped worker wire; stale generation request_id=%s generation=%s current=%s",
                    message.get("request_id"),
                    message.get("generation"),
                    self._peer_generation,
                )
                return
            await self._forward_wire(message)
            return
        if kind == KIND_REQUEST_DONE:
            if not self._accepts_peer_generation(message):
                logger.warning(
                    "[Supervisor] dropped request.done; stale generation request_id=%s generation=%s current=%s",
                    message.get("request_id"),
                    message.get("generation"),
                    self._peer_generation,
                )
                return
            self._finish_request(message)
            return
        if kind == KIND_READINESS:
            self._apply_readiness(message)
            return
        if kind == KIND_HEARTBEAT:
            return
        if kind == KIND_HELLO:
            return
        logger.info("[Supervisor] ignored ipc kind=%s", kind)

    async def _forward_wire(self, message: dict[str, Any]) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        request_id = str(message.get("request_id") or payload.get("request_id") or "")
        route = self._routes.get(request_id) if request_id else None
        if route is not None:
            ws, send_lock = route
        elif _is_server_push(payload):
            ws, send_lock = self._gateway_ws, self._gateway_lock
        else:
            logger.warning(
                "[Supervisor] dropped worker wire; no route request_id=%s generation=%s",
                request_id,
                message.get("generation"),
            )
            return
        if self._forwarder is None or ws is None or send_lock is None:
            logger.warning(
                "[Supervisor] dropped worker wire; no Gateway connection request_id=%s",
                request_id,
            )
            return
        try:
            await self._forwarder.send(payload, ws=ws, send_lock=send_lock)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Supervisor] dropped worker wire; Gateway forward failed request_id=%s",
                request_id,
                exc_info=True,
            )

    def _accepts_peer_generation(self, message: dict[str, Any]) -> bool:
        raw = message.get("generation")
        return isinstance(raw, int) and raw == self._peer_generation

    def _finish_request(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id") or "")
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(message)

    def _apply_readiness(self, message: dict[str, Any]) -> None:
        state = str(message.get("state") or "")
        reason = str(message.get("reason") or "")
        if state == ReadinessState.AGENT_READY.value:
            self._fatal = False
            self._restart_count = 0
            self._registry.mark_ready("primary", ready=True)
            self._readiness.mark_agent_ready()
            logger.info("[Supervisor] runtime worker agent_ready pid=%s", self._worker_pid)
            return
        if state == ReadinessState.DEGRADED.value:
            if self._readiness.state is not ReadinessState.AGENT_READY:
                self._readiness.mark_agent_ready()
            self._readiness.mark_degraded()
            self._registry.mark_ready("primary", ready=True)
            return
        if state == ReadinessState.RUNTIME_WARMING.value:
            self._registry.mark_ready("primary", ready=False)
            if self._readiness.state in {ReadinessState.AGENT_READY, ReadinessState.DEGRADED}:
                self._readiness.mark_runtime_warming()
            self._readiness.note_warmup_retry(reason or "runtime warming")
            return
        if state == ReadinessState.FAILED.value:
            self._fatal = True
            self._fatal_reason = reason or "runtime worker failed"
            self._registry.mark_ready("primary", ready=False)
            self._readiness.mark_failed(self._fatal_reason)

    async def _schedule_restart(self, reason: str) -> bool:
        if self._stopping or self._fatal:
            self._ipc_ready.set()
            return False
        if self._restart_count >= self._max_restarts:
            self._fatal = True
            self._fatal_reason = reason
            self._readiness.mark_failed(reason)
            self._ipc_ready.set()
            return False
        self._restart_count += 1
        logger.info(
            "[Supervisor] restarting runtime worker (%s/%s): %s",
            self._restart_count,
            self._max_restarts,
            reason,
        )
        self._readiness.mark_runtime_warming()
        self._readiness.note_warmup_retry(reason)
        await self._stop_spawned_process()
        await asyncio.sleep(min(2.0, 0.2 * self._restart_count))
        return True

    async def _fail_pending(self, error: str) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _request_id, future in pending:
            if not future.done():
                future.set_result({"ok": False, "delivered": False, "error": error})
        self._routes.clear()

    async def _notify_gateway(self, *, attached: bool, remote: Any = None) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            if attached:
                await conn.send({"kind": KIND_GATEWAY_ATTACH})
            else:
                await conn.send({"kind": KIND_GATEWAY_DETACH, "remote": str(remote)})
        except (OSError, RuntimeError):
            logger.info("[Supervisor] gateway notify skipped; worker ipc is down")

    async def _emit_error(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        error: str,
    ) -> None:
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": error, "code": "WORKER_CLOSED"},
            metadata=request.metadata,
        )
        wire = encode_response(response, response_id=request.request_id)
        if self._forwarder is None:
            return
        await self._forwarder.send(wire, ws=ws, send_lock=send_lock)

    @staticmethod
    def _schedule(coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    def _start_side_tasks(self) -> None:
        generation = self._generation
        self._side_tasks = [
            asyncio.create_task(self._heartbeat(generation), name="runtime-heartbeat"),
            asyncio.create_task(self._watchdog(generation), name="runtime-watchdog"),
            asyncio.create_task(self._watch_process(generation), name="runtime-process-watch"),
        ]

    async def _cancel_side_tasks(self) -> None:
        tasks = self._side_tasks
        self._side_tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, generation: int) -> None:
        while not self._stopping and generation == self._generation:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            conn = self._conn
            if conn is None or generation != self._generation:
                return
            try:
                await conn.send({"kind": KIND_HEARTBEAT})
            except (OSError, RuntimeError):
                return

    async def _watchdog(self, generation: int) -> None:
        while not self._stopping and generation == self._generation:
            await asyncio.sleep(5.0)
            if generation != self._generation:
                return
            if time.monotonic() - self._last_rx <= _HEARTBEAT_TIMEOUT:
                continue
            logger.warning("[Supervisor] runtime worker heartbeat timed out")
            conn = self._conn
            if conn is not None:
                await conn.close()
            return

    async def _watch_process(self, generation: int) -> None:
        process = self._process if self._owns_process else None
        if process is None:
            return
        while not self._stopping and generation == self._generation:
            code = await process.wait(timeout=1.0)
            if code is None:
                continue
            if self._stopping or generation != self._generation:
                return
            logger.warning("[Supervisor] runtime worker exited: code=%s", code)
            conn = self._conn
            if conn is not None:
                await conn.close()
            return

    async def _shutdown_owned_worker(self) -> None:
        if not self._owns_process:
            return
        conn = self._conn
        process = self._process
        if conn is not None:
            try:
                await conn.send({"kind": KIND_SHUTDOWN})
            except (OSError, RuntimeError):
                pass
        if process is None:
            self._owns_process = False
            return
        if await process.wait(timeout=5.0) is None:
            await process.stop()
        self._process = None
        self._owns_process = False

    async def _stop_spawned_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        await process.stop()
        self._owns_process = False
