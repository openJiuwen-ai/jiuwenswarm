# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage Runtime Workers. This process does not execute Agent turns."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import encode_response
from jiuwenswarm.server.ipc.client import connect_with_retry, endpoint_is_listening, try_connect
from jiuwenswarm.server.ipc.protocol import (
    KIND_GATEWAY_ATTACH,
    KIND_DELIVERY_ACK,
    KIND_GATEWAY_DETACH,
    KIND_HELLO,
    KIND_HEARTBEAT,
    KIND_READINESS,
    KIND_REQUEST,
    KIND_REQUEST_DONE,
    KIND_SHUTDOWN,
    KIND_SHUTDOWN_DONE,
    KIND_SUBSCRIBE,
    KIND_SNAPSHOT,
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
_FINAL_SHUTDOWN_TIMEOUT = 30.0
_SNAPSHOT_WAIT_SECONDS = 0.3
_SNAPSHOT_WAIT_ATTEMPTS = 3


class RuntimeSupervisor:
    """Spawn, health-check, and route execution to the resident Worker.

    Implements the Front ``RuntimeBackend`` protocol. Control-plane RPCs never
    reach this object. Warming requests are owned and queued by the Worker so
    reattachment does not lose them. Execution waits for ``AGENT_READY``.
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
        self._worker_create_time: float | None = None
        self._worker_instance: str | None = None
        self._runtime_loaded = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._routes: dict[str, tuple[Any, asyncio.Lock]] = {}
        self._queries: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._shutdown_done: asyncio.Future[dict[str, Any]] | None = None
        self._gateway_ws: Any = None
        self._gateway_lock: asyncio.Lock | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._side_tasks: list[asyncio.Task[None]] = []
        self._ipc_ready = asyncio.Event()
        self._stopping = False
        self._terminating = False
        self._fatal = False
        self._fatal_reason = ""
        self._restart_count = 0
        self._generation = 0
        self._peer_generation: int | None = None
        self._last_rx = 0.0
        self._session_lock = asyncio.Lock()
        self._wire_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._wire_task: asyncio.Task[None] | None = None
        self._wire_bytes = 0

    accepts_warming = True

    async def get_runtime_snapshot(self, request: AgentRequest) -> dict[str, Any]:
        conn = self._conn
        if conn is None:
            return self._recovery_pending("worker_disconnected")
        query_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._queries[query_id] = future
        resume = (request.params or {}).get("runtime_resume")
        resume = resume if isinstance(resume, dict) else {}
        try:
            after = max(0, int(resume.get("after") or 0))
            await asyncio.wait_for(conn.send({"kind": KIND_SUBSCRIBE, "query_id": query_id,
                                              "generation": self._peer_generation,
                                              "payload": dump_agent_request(request),
                                              "subscribe": bool(resume), "after": after,
                                              "worker_instance": resume.get("worker_instance")}),
                                   timeout=_SNAPSHOT_WAIT_SECONDS)
            # Retry the wait on the same query. Sending another subscription
            # would replay the journal twice and could race the first recovery.
            for _ in range(_SNAPSHOT_WAIT_ATTEMPTS):
                try:
                    return await asyncio.wait_for(asyncio.shield(future), timeout=_SNAPSHOT_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    if self._conn is not conn:
                        return self._recovery_pending("worker_disconnected")
            logger.warning("[Supervisor] runtime snapshot pending query_id=%s session_id=%s",
                           query_id, request.session_id)
            return self._recovery_pending("snapshot_timeout")
        except asyncio.TimeoutError:
            return self._recovery_pending("snapshot_transport_timeout")
        except (OSError, RuntimeError, ValueError) as exc:
            logger.info("[Supervisor] runtime snapshot unavailable: %s", exc)
            return self._recovery_pending("snapshot_unavailable")
        finally:
            self._queries.pop(query_id, None)
            if not future.done():
                future.cancel()

    def _recovery_pending(self, reason: str) -> dict[str, Any]:
        return {"recovery_pending": True, "recovery_reason": reason,
                "retry_after_ms": int(_SNAPSHOT_WAIT_SECONDS * 1000),
                "worker_instance": self._worker_instance}

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
        # connect() publishes the transport before hello validates its identity
        # and registers the Worker. Requests must wait for that whole barrier.
        if (self._conn is None or self._registry.get(worker_id) is None) and not self._fatal:
            try:
                await asyncio.wait_for(self.run_until_ipc_ready(), timeout=self._connect_timeout)
            except asyncio.TimeoutError:
                await self._emit_error(ws, request, send_lock, "runtime worker IPC is still warming")
                return
        slot = self._registry.get(worker_id)
        if slot is None or self._conn is None:
            await self._emit_error(ws, request, send_lock, "runtime worker is not connected")
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        request_id = str(request.request_id or "")
        dispatch_id = uuid.uuid4().hex
        if request_id:
            self._pending[dispatch_id] = future
            self._routes[dispatch_id] = (ws, send_lock)
        try:
            await self._conn.send(
                {
                    "kind": KIND_REQUEST,
                    "generation": self._peer_generation,
                    "request_id": request_id,
                    "dispatch_id": dispatch_id,
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
            if self._pending.get(dispatch_id) is future:
                self._pending.pop(dispatch_id, None)
                self._routes.pop(dispatch_id, None)
        if request_id and not result.get("ok", False) and not result.get("delivered", False):
            error = str(result.get("error") or "runtime worker closed")
            await self._emit_error(ws, request, send_lock, error)

    async def stop(self, *, terminate_worker: bool = False) -> None:
        """Detach from the Worker so this Front can exit.

        A Worker this supervisor spawned stays running, so the next Front
        reconnects instead of loading Agent Runtime again.

        ``terminate_worker=True`` explicitly exercises lifecycle authority for
        final exit or upgrade, including after a validated reattachment.
        """
        conn = self._conn
        deadline = time.monotonic() + _FINAL_SHUTDOWN_TIMEOUT if terminate_worker else None
        if terminate_worker:
            self._terminating = True
            await self._shutdown_owned_worker(deadline=deadline)
        self._stopping = True
        if conn is not None and not terminate_worker:
            try:
                await conn.send({"kind": KIND_GATEWAY_DETACH, "remote": "front-stop"})
            except (OSError, RuntimeError):
                logger.info("[Supervisor] gateway detach skipped; worker ipc is down")
        self._owns_process = False
        self._process = None
        self._conn = None
        if deadline is None:
            await self._finish_stop(conn)
        else:
            cleanup = asyncio.create_task(self._finish_stop(conn))
            done, _ = await asyncio.wait({cleanup}, timeout=max(0.0, deadline - time.monotonic()))
            if cleanup in done:
                await cleanup
            else:
                cleanup.cancel()
                logger.error("[Supervisor] final shutdown deadline exhausted before IPC/task cleanup completed")
        self._ipc_ready.set()

    async def _finish_stop(self, conn: IpcConnection | None) -> None:
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
            if self._stopping or self._terminating:
                await self._fail_pending("runtime worker stopped")
                if self._shutdown_done is not None and not self._shutdown_done.done():
                    self._shutdown_done.set_result({"ok": False, "error": "worker closed before shutdown acknowledgement"})
                break
            await self._fail_pending("runtime worker closed")
            self._conn = None
            self._ipc_ready.clear()
            self._registry.mark_ready("primary", ready=False)
            if self._fatal:
                self._readiness.mark_failed(self._fatal_reason or "runtime worker failed")
                break
            if not await self._schedule_restart("runtime worker closed"):
                break

    async def _bring_up(self) -> None:
        await self._cancel_side_tasks()
        launch_identity = None
        async with self._session_lock:
            from filelock import FileLock

            lock_path = Path(self._socket_path + ".startup.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            from filelock import Timeout

            election = FileLock(str(lock_path), timeout=0, thread_local=False)
            deadline = time.monotonic() + self._connect_timeout
            while True:
                try:
                    election.acquire()
                    break
                except Timeout:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("worker election timed out") from None
                    await asyncio.sleep(0.025)
            try:
                existing = await try_connect(self._socket_path, timeout=0.05)
                if existing is not None:
                    self._conn = existing
                    logger.info("[Supervisor] reattached to resident runtime worker")
                else:
                    if endpoint_is_listening(self._socket_path):
                        # The lifetime lease can precede IPC listening, and the
                        # previous Front may still be detaching. Wait within
                        # this election's existing deadline without spawning.
                        self._conn = await connect_with_retry(
                            self._socket_path,
                            timeout=max(0.001, deadline - time.monotonic()),
                        )
                        logger.info("[Supervisor] reattached to resident runtime worker")
                    else:
                        process = WorkerProcess(
                            self._socket_path, host=self._host, port=self._port,
                            stub=self._stub, stub_ready_delay=self._stub_ready_delay,
                            stub_exit_on_request=self._stub_exit_on_request,
                        )
                        await process.spawn()
                        launch_identity = getattr(process, "launch_identity", None)
                        self._process = process
                        self._owns_process = True
                        self._conn = await connect_with_retry(self._socket_path, timeout=self._connect_timeout)
            finally:
                election.release()
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
            from jiuwenswarm.server.supervisor.identity import code_identity, data_scope

            identity = launch_identity or await asyncio.to_thread(code_identity)
            if hello.get("data_scope") != data_scope() or hello.get("code_identity") != identity:
                self._fatal = True
                self._fatal_reason = "resident Worker belongs to a different deployment or code version"
                raise RuntimeError(self._fatal_reason)
            if bool(hello.get("stub")) != self._stub:
                self._fatal = True
                self._fatal_reason = "resident Worker execution profile mismatch"
                raise RuntimeError(self._fatal_reason)
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
        if self._worker_pid is not None:
            import psutil
            try:
                self._worker_create_time = psutil.Process(self._worker_pid).create_time()
            except psutil.Error:
                self._worker_create_time = None
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
            if self._wire_task is None:
                await self._forward_wire(message)
            else:
                try:
                    size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                    if self._wire_bytes and self._wire_bytes + size > 16 * 1024 * 1024:
                        raise asyncio.QueueFull
                    message["_queue_bytes"] = size
                    self._wire_queue.put_nowait(message)
                    self._wire_bytes += size
                except asyncio.QueueFull:
                    await self._ack_delivery(message, False)
                    self._drop_gateway_delivery()
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
            if self._wire_task is None:
                self._finish_request(message)
            else:
                try:
                    self._wire_queue.put_nowait(message)
                except asyncio.QueueFull:
                    self._drop_gateway_delivery()
                    self._finish_request({**message, "ok": False, "delivered": False,
                                          "error": "Gateway delivery requires reconnection"})
            return
        if kind == KIND_SNAPSHOT:
            if not self._accepts_peer_generation(message):
                return
            future = self._queries.get(str(message.get("query_id") or ""))
            if future is not None and not future.done():
                future.set_result(message.get("payload") or {})
            return
        if kind == KIND_SHUTDOWN_DONE:
            if self._shutdown_done is not None and not self._shutdown_done.done():
                self._shutdown_done.set_result(message)
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
        route_key = str(message.get("dispatch_id") or request_id)
        route = self._routes.get(route_key)
        if route is not None:
            ws, send_lock = route
        elif _is_server_push(payload) or payload.get("is_stream"):
            ws, send_lock = self._gateway_ws, self._gateway_lock
            from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY

            payload = dict(payload)
            payload["metadata"] = {**(payload.get("metadata") or {}), E2A_WIRE_SERVER_PUSH_KEY: True}
        else:
            logger.warning(
                "[Supervisor] dropped worker wire; no route request_id=%s generation=%s",
                request_id,
                message.get("generation"),
            )
            await self._ack_delivery(message, False)
            return
        if self._forwarder is None or ws is None or send_lock is None:
            logger.warning(
                "[Supervisor] dropped worker wire; no Gateway connection request_id=%s",
                request_id,
            )
            await self._ack_delivery(message, False)
            return
        delivered = False
        try:
            delivered = await asyncio.wait_for(self._forwarder.send(payload, ws=ws, send_lock=send_lock), timeout=1.0)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Supervisor] dropped worker wire; Gateway forward failed request_id=%s",
                request_id,
                exc_info=True,
            )
            self._drop_gateway_delivery()
        await self._ack_delivery(message, delivered is True)

    def _drop_gateway_delivery(self) -> None:
        """A slow consumer must not block Worker health/cancel/readiness frames."""
        ws = self._gateway_ws
        self._gateway_ws = None
        self._gateway_lock = None
        if self._forwarder is not None:
            detach = getattr(self._forwarder, "detach", None)
            if callable(detach):
                detach(ws)
        close = getattr(ws, "close", None)
        if callable(close):
            self._schedule(close())

    async def _write_gateway_frames(self, generation: int) -> None:
        while not self._stopping and self._generation == generation:
            message = await self._wire_queue.get()
            try:
                if message.get("kind") == KIND_REQUEST_DONE:
                    self._finish_request(message)
                else:
                    await self._forward_wire(message)
            finally:
                self._wire_bytes -= int(message.get("_queue_bytes") or 0)
                self._wire_queue.task_done()

    async def _ack_delivery(self, message: dict[str, Any], delivered: bool) -> None:
        if not message.get("wire_id") or self._conn is None:
            return
        try:
            await self._conn.send({"kind": KIND_DELIVERY_ACK, "wire_id": message["wire_id"],
                                   "generation": message.get("generation"), "delivered": delivered})
        except (OSError, RuntimeError):
            logger.info("[Supervisor] delivery acknowledgement lost on IPC detach")

    def _accepts_peer_generation(self, message: dict[str, Any]) -> bool:
        raw = message.get("generation")
        return isinstance(raw, int) and raw == self._peer_generation

    def _finish_request(self, message: dict[str, Any]) -> None:
        dispatch_id = str(message.get("dispatch_id") or message.get("request_id") or "")
        future = self._pending.get(dispatch_id)
        # Legacy stubs may omit dispatch_id; only an unambiguous pending route
        # may consume their completion.
        if future is None and not message.get("dispatch_id") and len(self._pending) == 1:
            future = next(iter(self._pending.values()))
        if future is not None and not future.done():
            future.set_result(message)

    def _apply_readiness(self, message: dict[str, Any]) -> None:
        state = str(message.get("state") or "")
        reason = str(message.get("reason") or "")
        if state == ReadinessState.AGENT_READY.value:
            if message.get("agent_ready") is False:
                return
            self._fatal = False
            self._restart_count = 0
            self._registry.mark_ready("primary", ready=True)
            self._readiness.mark_agent_ready()
            logger.info("[Supervisor] runtime worker agent_ready pid=%s", self._worker_pid)
        elif state == ReadinessState.DEGRADED.value:
            if self._readiness.snapshot()["agent_ready"] or message.get("agent_ready") is True:
                if not self._readiness.snapshot()["agent_ready"]:
                    self._readiness.mark_agent_ready()
                self._readiness.mark_degraded()
                self._registry.mark_ready("primary", ready=True)
        elif state == ReadinessState.RUNTIME_WARMING.value:
            self._registry.mark_ready("primary", ready=False)
            if self._readiness.state in {ReadinessState.AGENT_READY, ReadinessState.DEGRADED}:
                self._readiness.mark_runtime_warming()
            self._readiness.note_warmup_retry(reason or "runtime warming")
        elif state == ReadinessState.FAILED.value:
            self._fatal = True
            self._fatal_reason = reason or "runtime worker failed"
            self._registry.mark_ready("primary", ready=False)
            self._readiness.mark_failed(self._fatal_reason)
        self._readiness.note_runtime_progress(
            runtime_phase=message.get("runtime_phase"),
            capability_readiness=message.get("capability_readiness"),
        )

    async def _schedule_restart(self, reason: str) -> bool:
        if self._stopping or self._terminating or self._fatal:
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
        if self._process is not None and self._process.returncode is not None:
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
        self._wire_task = asyncio.create_task(self._write_gateway_frames(generation), name="runtime-event-forwarder")
        self._side_tasks = [
            self._wire_task,
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
        self._wire_task = None
        while not self._wire_queue.empty():
            message = self._wire_queue.get_nowait()
            await self._ack_delivery(message, False)
            self._wire_bytes -= int(message.get("_queue_bytes") or 0)
            self._wire_queue.task_done()

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

    async def _shutdown_owned_worker(self, *, deadline: float) -> None:
        conn = self._conn
        process = self._process
        if conn is not None:
            self._shutdown_done = asyncio.get_running_loop().create_future()
            try:
                await asyncio.wait_for(conn.send({"kind": KIND_SHUTDOWN}), timeout=max(0.0, deadline - time.monotonic()))
                if self._loop_task is not None:
                    outcome = await asyncio.wait_for(asyncio.shield(self._shutdown_done), timeout=max(0.0, deadline - time.monotonic()))
                    if not outcome.get("ok"):
                        logger.error("[Supervisor] Worker shutdown incomplete: %s", outcome.get("error"))
            except (OSError, RuntimeError, asyncio.TimeoutError):
                logger.warning("[Supervisor] Worker shutdown acknowledgement missing or shutdown deadline exhausted")
        if process is not None and await process.wait(timeout=max(0.0, deadline - time.monotonic())) is None:
            await process.stop(timeout=max(0.0, deadline - time.monotonic()))
        if self._worker_pid is not None and self._worker_create_time is not None:
            await asyncio.to_thread(self._reap_validated_worker, deadline=deadline)
        self._process = None

    def _reap_validated_worker(self, *, deadline: float) -> None:
        """Final-exit authority is explicit; a reused PID must never be killed."""
        import psutil
        try:
            process = psutil.Process(self._worker_pid)
            if process.create_time() != self._worker_create_time:
                raise RuntimeError("Worker PID identity changed during final exit")
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except psutil.TimeoutExpired:
                if process.create_time() != self._worker_create_time:
                    raise RuntimeError("Worker PID identity changed during final exit")
                logger.error("[Supervisor] Worker final exit timed out; forcing validated process termination")
                process.kill()
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except psutil.TimeoutExpired:
                    logger.error("[Supervisor] validated Worker exit could not be observed before the shutdown deadline")
        except psutil.NoSuchProcess:
            pass
        self._owns_process = False

    async def _stop_spawned_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        await process.stop()
        self._owns_process = False
