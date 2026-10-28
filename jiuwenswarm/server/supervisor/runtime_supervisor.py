# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage Runtime Workers. This process does not execute Agent turns."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import encode_response
from jiuwenswarm.server.ipc.client import connect_with_retry, endpoint_is_listening, try_connect
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
from jiuwenswarm.server.supervisor.diagnostics import dump_runtime_state
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
_WATCHDOG_INTERVAL = 5.0
_FORWARD_QUEUE_SIZE = 256
_FORWARD_TIMEOUT = 10.0
_FORWARD_DRAIN_TIMEOUT = 1.0
# Transport stays alive (pongs flow) but the Worker's business loop has not
# run: warn once it outlasts a heartbeat window, replace the Worker only when
# the stall is sustained.
_WORKER_STALL_WARN = 30.0
_WORKER_STALL_RESTART = 180.0

_GatewayRoute = tuple[Any, asyncio.Lock]
_ForwardQueue = asyncio.Queue[tuple[dict[str, Any], _GatewayRoute | None]]

# Worker restart + fresh Runtime import budget for the reload-driven recovery.
# Gateway unary requests default to a 600s timeout, so blocking one reload
# request for this long stays well inside the caller's patience.
_RECOVERY_TIMEOUT = 90.0


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
        self._validated_conn: IpcConnection | None = None
        self._process: WorkerProcess | None = None
        self._owns_process = False
        self._worker_pid: int | None = None
        self._worker_create_time: float | None = None
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
        self._terminating = False
        self._stop_deadline: float | None = None
        self._fatal = False
        self._fatal_reason = ""
        self._restart_count = 0
        self._generation = 0
        self._peer_generation: int | None = None
        self._last_rx = 0.0
        self._worker_last_beat = 0.0
        self._stall_warned_at = 0.0
        self._forward_queue: _ForwardQueue | None = None
        self._forward_started = 0.0
        self._forward_request_id = ""
        self._event_loop_lag = 0.0
        self._observed_loop_lag = 0.0
        self._last_dump_at = 0.0
        self._last_dump_path: Path | None = None
        self._session_lock = asyncio.Lock()
        self._recover_lock = asyncio.Lock()

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

    async def recover_from_failed(self, timeout: float = _RECOVERY_TIMEOUT) -> bool:
        """Restart a FAILED Worker and wait for ``AGENT_READY``.

        Called by ``ExecutionAdmission`` when a config reload reaches a FAILED
        runtime: reloading config is the operator's "dependencies are fixed,
        try again" signal, so refusing it would leave the AgentServer dead
        until a full process restart. The failed resident Worker is shut down
        even when this supervisor reattached to it — a Worker whose Runtime
        failed to load never retries on its own, so keeping it alive only
        blocks the IPC socket a fresh Worker must claim.
        """
        if self._stopping:
            return False
        state = self._readiness.state
        if state is ReadinessState.DRAINING:
            return False
        if state is not ReadinessState.FAILED:
            return bool(self._readiness.snapshot()["agent_ready"])
        async with self._recover_lock:
            state = self._readiness.state
            if state is ReadinessState.DRAINING:
                return False
            if state is not ReadinessState.FAILED:
                return bool(self._readiness.snapshot()["agent_ready"])
            logger.warning(
                "[Supervisor] reload requested while runtime FAILED; restarting worker: %s",
                self._fatal_reason or "unknown failure",
            )
            # Clear the sticky failure so the supervise loop may bring the
            # Worker up again, then tear the broken Worker process down.
            self._fatal = False
            self._fatal_reason = ""
            self._restart_count = 0
            self._readiness.mark_recovering("reload requested after runtime failure")
            # Hold the session lock so a background restart inside a live
            # supervise loop cannot reattach to the dying Worker socket.
            async with self._session_lock:
                await self._terminate_failed_worker()
                task = self._loop_task
                if task is not None and not task.done():
                    # The loop is parked in _pump/recv; closing the connection
                    # wakes it into its own restart path.
                    conn = self._conn
                    if conn is not None:
                        self._conn = None
                        try:
                            await conn.close()
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[Supervisor] recovery ipc close failed", exc_info=True
                            )
                else:
                    self._loop_task = asyncio.create_task(
                        self._supervise(), name="runtime-supervisor"
                    )
            try:
                return await asyncio.wait_for(
                    self._readiness.wait_agent_ready(), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Supervisor] runtime recovery did not reach AGENT_READY in %.0fs",
                    timeout,
                )
                return False

    async def _terminate_failed_worker(self) -> None:
        """Shut the FAILED Worker down, including a reattached resident one."""
        conn = self._conn
        self._conn = None
        process = self._process if self._owns_process else None
        if conn is not None:
            try:
                await conn.send(
                    {"kind": KIND_SHUTDOWN, "reason": "runtime-failed-recovery"}
                )
            except (OSError, RuntimeError):
                logger.info("[Supervisor] worker shutdown send skipped; ipc is down")
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                logger.debug("[Supervisor] worker shutdown close failed", exc_info=True)
        if process is not None:
            if await process.wait(timeout=5.0) is None:
                await process.stop()
        else:
            # A reattached Worker has no process handle; give the SHUTDOWN a
            # moment to release the IPC socket before the supervise loop
            # reconnects, so it spawns a fresh Worker instead of reattaching
            # to the dying one.
            await asyncio.sleep(0.5)
        self._process = None
        self._owns_process = False

    async def dispatch_parsed_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Forward one execution request and wait until the Worker finishes it."""
        worker_id = self._affinity.worker_for(request.session_id)
        slot = self._registry.get(worker_id)
        if slot is None or self._conn is None:
            await self._emit_error(
                ws, request, send_lock, "runtime worker is not connected"
            )
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
            await self._emit_error(
                ws, request, send_lock, f"runtime worker closed: {exc}"
            )
            return
        finally:
            self._pending.pop(request_id, None)
            self._routes.pop(request_id, None)
        if (
            request_id
            and not result.get("ok", False)
            and not result.get("delivered", False)
        ):
            error = str(result.get("error") or "runtime worker closed")
            await self._emit_error(
                ws,
                request,
                send_lock,
                error,
                code=str(result.get("code") or "WORKER_CLOSED"),
            )

    async def stop(self, *, terminate_worker: bool = False) -> None:
        """Detach from the Worker so this Front can exit.

        A Worker this supervisor spawned stays running, so the next Front
        reconnects instead of loading Agent Runtime again.

        ``terminate_worker=True`` explicitly exercises lifecycle authority for
        final exit or upgrade, including after a validated reattachment.
        """
        conn = self._conn
        deadline = time.monotonic() + _FINAL_SHUTDOWN_TIMEOUT if terminate_worker else None
        self._stop_deadline = deadline
        owned_process = self._process
        if self._validated_conn is None:
            self._stopping = True
            task = self._loop_task
            if task is not None and not task.done():
                task.cancel()
        shutdown_error: Exception | None = None
        if terminate_worker:
            self._terminating = True
            try:
                await self._shutdown_owned_worker(deadline=deadline)
            except Exception as exc:  # noqa: BLE001
                shutdown_error = exc
                logger.exception("[Supervisor] final Worker shutdown failed")
        self._stopping = True
        if conn is not None and not terminate_worker:
            try:
                await conn.send({"kind": KIND_GATEWAY_DETACH, "remote": "front-stop"})
            except (OSError, RuntimeError):
                logger.info("[Supervisor] gateway detach skipped; worker ipc is down")
        self._owns_process = False
        self._process = None
        self._conn = None
        self._validated_conn = None
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
        remaining_process = self._process or owned_process
        has_owned_pid = remaining_process is not None and getattr(remaining_process, "pid", None) is not None
        if (terminate_worker and has_owned_pid
                and getattr(remaining_process, "returncode", 0) is None):
            self._process = remaining_process
            self._owns_process = True
            shutdown_error = shutdown_error or RuntimeError("Owned Worker exit incomplete after final stop")
        if shutdown_error is not None:
            raise shutdown_error

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
                break
            await self._fail_pending("runtime worker closed")
            self._conn = None
            self._validated_conn = None
            self._registry.mark_ready("primary", ready=False)
            if self._fatal:
                self._readiness.mark_failed(self._fatal_reason or "runtime worker failed")
                break
            if not await self._schedule_restart("runtime worker closed"):
                break

    async def _bring_up(self) -> None:
        await self._cancel_side_tasks()
        from jiuwenswarm.server.supervisor.identity import code_identity, data_scope, deployment_scope
        from jiuwenswarm.server.supervisor.resident_lifecycle import retire_worker, startup_lock

        async with self._session_lock:
            self._validated_conn = None
            self._worker_pid = None
            self._worker_create_time = None
            deadline = time.monotonic() + self._connect_timeout
            async with startup_lock(self._socket_path, deadline):
                if self._stopping or self._terminating:
                    raise RuntimeError("Worker startup cancelled by supervisor stop")
                await self._migrate_legacy_workers()
                for attempt in range(2):
                    if self._stopping or self._terminating:
                        raise RuntimeError("Worker startup cancelled by supervisor stop")
                    launch_identity = None
                    existing = None
                    process = None
                    conn = None
                    try:
                        existing = await try_connect(self._socket_path, timeout=0.05)
                        conn = existing
                        if self._stopping or self._terminating:
                            raise RuntimeError("Worker startup cancelled by supervisor stop")
                        if existing is not None:
                            self._owns_process = False
                            self._process = None
                            logger.info("[Supervisor] reattached to resident runtime worker")
                        elif endpoint_is_listening(self._socket_path):
                            self._owns_process = False
                            self._process = None
                            conn = await connect_with_retry(
                                self._socket_path, timeout=max(0.001, deadline - time.monotonic())
                            )
                            existing = conn
                        else:
                            process = WorkerProcess(
                                self._socket_path, host=self._host, port=self._port,
                                stub=self._stub, stub_ready_delay=self._stub_ready_delay,
                                stub_exit_on_request=self._stub_exit_on_request,
                            )
                            # Retain the owner before any await can create a child.
                            self._process = process
                            self._owns_process = True
                            await process.spawn()
                            if self._stopping or self._terminating:
                                raise RuntimeError("Worker startup cancelled by supervisor stop")
                            launch_identity = getattr(process, "launch_identity", None)
                            conn = await connect_with_retry(self._socket_path, timeout=self._connect_timeout)
                        self._conn = conn
                        if self._stopping or self._terminating:
                            raise RuntimeError("Worker startup cancelled by supervisor stop")
                        if conn is None:
                            raise ConnectionError("runtime worker ipc missing after bring-up")
                        hello = await asyncio.wait_for(conn.recv(), timeout=_HELLO_TIMEOUT)
                        if self._stopping or self._terminating:
                            raise RuntimeError("Worker startup cancelled by supervisor stop")
                        if not isinstance(hello, dict) or hello.get("kind") != KIND_HELLO:
                            kind = hello.get("kind") if isinstance(hello, dict) else type(hello).__name__
                            raise RuntimeError(f"expected hello, got {kind!r}")
                        protocol = hello.get("protocol")
                        if protocol not in (1, 2):
                            self._fatal = True
                            self._fatal_reason = "runtime ipc protocol mismatch"
                            raise RuntimeError("runtime ipc protocol mismatch")
                        if (hello.get("data_scope") not in (None, data_scope())
                                or hello.get("deployment_scope") not in (None, deployment_scope())):
                            self._fatal = True
                            self._fatal_reason = "resident Worker belongs to a different deployment"
                            raise RuntimeError(self._fatal_reason)
                        if bool(hello.get("stub")) != self._stub:
                            self._fatal = True
                            self._fatal_reason = "resident Worker execution profile mismatch"
                            raise RuntimeError(self._fatal_reason)
                        # Spawn already pins a freshly validated identity. Only
                        # reattachment needs another observation of disk inputs.
                        expected = launch_identity or await asyncio.to_thread(code_identity)
                        if protocol != PROTOCOL_VERSION or hello.get("code_identity") != expected:
                            if existing is None or attempt:
                                raise RuntimeError("new Worker build identity did not match its parent")
                            logger.warning("[Supervisor] replacing resident Worker from a different code build")
                            try:
                                await retire_worker(conn, hello, self._socket_path,
                                    deadline=time.monotonic() + _FINAL_SHUTDOWN_TIMEOUT, allow_legacy=True)
                            except Exception:
                                self._fatal = True
                                self._fatal_reason = "resident Worker replacement failed"
                                raise
                            await conn.close()
                            self._conn = None
                            self._validated_conn = None
                            deadline = time.monotonic() + self._connect_timeout
                            continue
                        from jiuwenswarm.server.supervisor.resident_lifecycle import worker_process

                        try:
                            validated_process, validated_created = worker_process(
                                hello, self._socket_path, allow_legacy=True)
                        except RuntimeError as exc:
                            self._fatal = True
                            self._fatal_reason = str(exc)
                            raise
                        break
                    except (Exception, asyncio.CancelledError):
                        self._conn = None
                        self._validated_conn = None
                        try:
                            if conn is not None:
                                await conn.close()
                        finally:
                            if process is not None:
                                budget = (5.0 if self._stop_deadline is None else
                                          max(0.0, self._stop_deadline - time.monotonic()))
                                await process.stop(timeout=budget)
                                if (self._process is process
                                        and (process.pid is None or process.returncode is not None)):
                                    self._process = None
                                    self._owns_process = False
                        raise
        self._generation += 1
        peer = hello.get("generation")
        self._peer_generation = peer if isinstance(peer, int) else self._generation
        self._runtime_loaded = bool(hello.get("runtime_loaded"))
        self._last_rx = time.monotonic()
        # The hello itself was produced by the Worker's event loop.
        self._worker_last_beat = self._last_rx
        self._stall_warned_at = 0.0
        self._worker_pid = validated_process.pid
        self._worker_create_time = validated_created
        instance = hello.get("instance")
        self._worker_instance = instance if isinstance(instance, str) and instance else None
        self._validated_conn = conn
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
        queue: _ForwardQueue = asyncio.Queue(maxsize=_FORWARD_QUEUE_SIZE)
        self._forward_queue = queue
        writer = asyncio.create_task(
            self._forward_messages(queue, generation), name="runtime-gateway-forward"
        )
        try:
            while not self._stopping and self._generation == generation:
                message = await conn.recv()
                self._last_rx = time.monotonic()
                await self._handle(message)
        except (OSError, RuntimeError):
            # Deliver already-received terminal frames on EOF when possible,
            # but never hold Worker recovery behind an unavailable Gateway.
            if not self._stopping and self._generation == generation:
                try:
                    await asyncio.wait_for(queue.join(), _FORWARD_DRAIN_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[Supervisor] Gateway forward drain timed out; queued=%s generation=%s",
                        queue.qsize(),
                        generation,
                    )
            raise
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if self._forward_queue is queue:
                self._forward_queue = None

    async def _forward_messages(self, queue: _ForwardQueue, generation: int) -> None:
        while not self._stopping and self._generation == generation:
            message, route = await queue.get()
            try:
                if self._stopping or self._generation != generation:
                    return
                if message.get("kind") == KIND_REQUEST_DONE:
                    self._finish_request(message)
                elif route is not None and self._route_is_current(message, route):
                    await self._forward_wire(message, route=route)
            finally:
                queue.task_done()

    def _queue_forward(
        self, message: dict[str, Any], route: _GatewayRoute | None
    ) -> None:
        queue = self._forward_queue
        if queue is None:
            raise RuntimeError("Gateway forwarding queue is not running")
        try:
            queue.put_nowait((message, route))
        except asyncio.QueueFull:
            request_id = str(message.get("request_id") or "")
            error = "Gateway forwarding queue is full"
            logger.warning(
                "[Supervisor] %s; request_id=%s queued=%s generation=%s",
                error,
                request_id,
                queue.qsize(),
                self._generation,
            )
            self._fail_forwarding(request_id, error)

    def _log_diagnostics(self, reason: str) -> None:
        now = time.monotonic()
        conn_stats = getattr(self._conn, "diagnostics", None)
        forward_stats = getattr(self._forwarder, "diagnostics", None)
        stderr_path = getattr(self._process, "stderr_path", None)
        snapshot = {
            "reason": reason,
            "pid": self._worker_pid,
            "generation": self._generation,
            "last_rx_age": now - self._last_rx,
            "ipc": conn_stats() if callable(conn_stats) else {},
            "gateway": forward_stats() if callable(forward_stats) else {},
            "forward_queued": self._forward_queue.qsize()
            if self._forward_queue is not None
            else 0,
            "forward_request_id": self._forward_request_id,
            "forward_seconds": now - self._forward_started
            if self._forward_started
            else 0.0,
            "event_loop_lag_seconds": max(
                self._event_loop_lag, self._observed_loop_lag
            ),
            "worker_loop_beat_age": now - self._worker_last_beat
            if self._worker_last_beat
            else None,
            "worker_stderr": str(stderr_path) if stderr_path is not None else None,
        }
        logger.warning("[Supervisor] runtime diagnostics: %s", json.dumps(snapshot))
        if now - self._last_dump_at < 10.0:
            return
        self._last_dump_at = now
        try:
            if stderr_path is not None:
                directory = stderr_path.parent
            else:
                from jiuwenswarm.common.utils import get_logs_dir

                directory = get_logs_dir() / "runtime_diagnostics"
            self._last_dump_path = dump_runtime_state(directory, snapshot)
            self._observed_loop_lag = 0.0
            logger.warning(
                "[Supervisor] runtime diagnostic dump=%s", self._last_dump_path
            )
        except Exception:  # noqa: BLE001
            logger.warning("[Supervisor] runtime diagnostic dump failed", exc_info=True)

    def _route_is_current(self, message: dict[str, Any], route: _GatewayRoute) -> bool:
        payload = message.get("payload") or {}
        request_id = str(message.get("request_id") or payload.get("request_id") or "")
        current = self._routes.get(request_id)
        if current is not None:
            return current[0] is route[0] and current[1] is route[1]
        return _is_server_push(payload) and self._gateway_ws is route[0]

    def _fail_forwarding(self, request_id: str, error: str) -> None:
        # Completion from the Worker cannot turn a failed Gateway delivery
        # into success. Retire its route so queued frames cannot follow an error.
        self._routes.pop(request_id, None)
        self._finish_request(
            {
                "request_id": request_id,
                "ok": False,
                "delivered": False,
                "error": error,
                "code": "GATEWAY_FORWARD_FAILED",
            }
        )

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = str(message.get("kind") or "")
        self._note_worker_beat(message)
        if kind == KIND_WIRE:
            if not self._accepts_peer_generation(message):
                logger.warning(
                    "[Supervisor] dropped worker wire; stale generation request_id=%s generation=%s current=%s",
                    message.get("request_id"),
                    message.get("generation"),
                    self._peer_generation,
                )
                return
            route = self._wire_route(message)
            if route is not None:
                self._queue_forward(message, route)
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
            self._queue_forward(
                message, self._routes.get(str(message.get("request_id") or ""))
            )
            return
        if kind == KIND_READINESS:
            self._apply_readiness(message)
            return
        if kind == KIND_HEARTBEAT:
            return
        if kind == KIND_HELLO:
            return
        logger.info("[Supervisor] ignored ipc kind=%s", kind)

    def _wire_route(self, message: dict[str, Any]) -> _GatewayRoute | None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return None
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
            return None
        if self._forwarder is None or ws is None or send_lock is None:
            logger.warning(
                "[Supervisor] dropped worker wire; no Gateway connection request_id=%s",
                request_id,
            )
            return None
        return ws, send_lock

    async def _forward_wire(
        self, message: dict[str, Any], *, route: _GatewayRoute
    ) -> None:
        payload = message["payload"]
        request_id = str(message.get("request_id") or payload.get("request_id") or "")
        ws, send_lock = route
        self._forward_started = time.monotonic()
        self._forward_request_id = request_id
        try:
            async with asyncio.timeout(_FORWARD_TIMEOUT):
                await self._forwarder.send(payload, ws=ws, send_lock=send_lock)
        except asyncio.TimeoutError:
            error = "Gateway forwarding timed out"
            logger.warning(
                "[Supervisor] %s; request_id=%s timeout=%.1fs generation=%s",
                error,
                request_id,
                _FORWARD_TIMEOUT,
                self._generation,
            )
            self._log_diagnostics(error)
            self._fail_forwarding(request_id, error)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Supervisor] dropped worker wire; Gateway forward failed request_id=%s",
                request_id,
                exc_info=True,
            )
            self._fail_forwarding(request_id, "Gateway forwarding failed")
        finally:
            self._forward_started = 0.0
            self._forward_request_id = ""

    def _accepts_peer_generation(self, message: dict[str, Any]) -> bool:
        raw = message.get("generation")
        return isinstance(raw, int) and raw == self._peer_generation

    def _note_worker_beat(self, message: dict[str, Any]) -> None:
        """Track when the Worker's event loop last provably ran.

        Every frame is loop-produced except the off-loop heartbeat pong, which
        reports its own stall age instead. A missing field (legacy Worker)
        counts as a fresh beat, keeping old Workers outside stall detection.
        """
        age = message.get("loop_beat_age")
        if isinstance(age, (int, float)) and age >= 0:
            self._worker_last_beat = time.monotonic() - float(age)
        else:
            self._worker_last_beat = time.monotonic()

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
        *,
        code: str = "WORKER_CLOSED",
    ) -> None:
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": error, "code": code},
            metadata=request.metadata,
        )
        wire = encode_response(response, response_id=request.request_id)
        if self._forwarder is None:
            return
        try:
            async with asyncio.timeout(_FORWARD_TIMEOUT):
                await self._forwarder.send(wire, ws=ws, send_lock=send_lock)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Supervisor] Gateway error delivery failed; request_id=%s error=%s",
                request.request_id,
                error,
                exc_info=True,
            )

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
            expected_wakeup = time.monotonic() + _WATCHDOG_INTERVAL
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            self._event_loop_lag = max(0.0, time.monotonic() - expected_wakeup)
            if generation != self._generation:
                return
            now = time.monotonic()
            if now - self._last_rx > _HEARTBEAT_TIMEOUT:
                if self._event_loop_lag >= _WATCHDOG_INTERVAL:
                    # This Front's own loop stalled, so pending pongs may be
                    # sitting in resolved futures. Do not execute the Worker
                    # for our own stall; the next cycle re-checks with fresh
                    # evidence once the pump has drained. Keep the peak lag
                    # so the deferred verdict still carries the evidence.
                    self._observed_loop_lag = max(
                        self._observed_loop_lag, self._event_loop_lag
                    )
                    logger.warning(
                        "[Supervisor] supervisor event loop stalled %.1fs; "
                        "deferring worker health verdict",
                        self._event_loop_lag,
                    )
                    continue
                logger.warning(
                    "[Supervisor] runtime worker heartbeat timed out; last_rx_age=%.1fs "
                    "forward_queued=%s generation=%s pid=%s",
                    now - self._last_rx,
                    self._forward_queue.qsize() if self._forward_queue is not None else 0,
                    generation,
                    self._worker_pid,
                )
                self._log_diagnostics("runtime worker heartbeat timed out")
                conn = self._conn
                if conn is not None:
                    await conn.close()
                return
            if self._worker_last_beat and await self._classify_worker_stall(now):
                return
            continue

    async def _classify_worker_stall(self, now: float) -> bool:
        """Separate a stalled business loop from a healthy transport.

        Returns True when the session was terminated and the Worker is being
        replaced, so the caller stops checking this generation.
        """
        beat_age = now - self._worker_last_beat
        if beat_age <= _WORKER_STALL_WARN:
            self._stall_warned_at = 0.0
            return False
        if beat_age > _WORKER_STALL_RESTART:
            logger.warning(
                "[Supervisor] runtime worker business loop stalled %.1fs "
                "(transport alive); restarting generation=%s pid=%s",
                beat_age,
                self._generation,
                self._worker_pid,
            )
            self._log_diagnostics("worker business loop stalled")
            conn = self._conn
            if conn is not None:
                await conn.close()
            return True
        if now - self._stall_warned_at >= _WATCHDOG_INTERVAL * 2:
            self._stall_warned_at = now
            logger.warning(
                "[Supervisor] runtime worker loop unresponsive %.1fs "
                "(transport alive, waiting up to %.0fs); pid=%s",
                beat_age,
                _WORKER_STALL_RESTART,
                self._worker_pid,
            )
            self._log_diagnostics("worker loop unresponsive")
        return False

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
            logger.warning(
                "[Supervisor] runtime worker exited: code=%s stderr=%s",
                code,
                process.stderr_path,
            )
            conn = self._conn
            if conn is not None:
                await conn.close()
            return

    async def _shutdown_owned_worker(self, *, deadline: float | None = None) -> None:
        deadline = deadline if deadline is not None else time.monotonic() + _FINAL_SHUTDOWN_TIMEOUT
        conn = self._conn
        process = self._process
        if conn is not None and conn is not self._validated_conn:
            if process is None or getattr(process, "pid", None) is None:
                raise RuntimeError("Worker deployment validation incomplete; termination refused")
            # A Popen handle establishes ownership of our own child even before
            # HELLO, but does not authorize IPC commands on an unverified peer.
            conn = None
        # IPC and graceful exit must leave time for forced exit observation.
        grace = deadline - min(1.0, max(0.0, deadline - time.monotonic()) / 2)
        if conn is not None:
            try:
                await asyncio.wait_for(
                    conn.send({"kind": KIND_SHUTDOWN}), timeout=max(0.0, grace - time.monotonic())
                )
            except (OSError, RuntimeError, asyncio.TimeoutError):
                logger.warning("[Supervisor] Worker shutdown send failed or shutdown deadline exhausted")
        if process is not None:
            if conn is None:
                # No validated peer received a shutdown request. Stop only the
                # Popen-owned child now instead of waiting for an impossible ACK.
                await process.stop(timeout=max(0.0, deadline - time.monotonic()))
            elif await process.wait(timeout=max(0.0, grace - time.monotonic())) is None:
                await process.stop(timeout=max(0.0, deadline - time.monotonic()))
        if self._worker_pid is not None and self._worker_create_time is not None:
            await asyncio.to_thread(self._reap_validated_worker, deadline=deadline)
        self._process = None
        self._validated_conn = None

    async def _stop_spawned_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        await process.stop()
        self._owns_process = False


    async def _finish_stop(self, conn: IpcConnection | None) -> None:
        if conn is not None:
            await conn.close()
        await self._cancel_side_tasks()
        task = self._loop_task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=5.0)
            if task not in done:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)



    async def _migrate_legacy_workers(self) -> None:
        """Retire an owned historical endpoint instead of merely abandoning it."""
        from jiuwenswarm.server.supervisor.worker_process import legacy_socket_paths
        from jiuwenswarm.server.supervisor.resident_lifecycle import retire_worker, startup_lock

        if self._socket_path != default_socket_path():
            return
        for path in legacy_socket_paths():
            if path == self._socket_path:
                continue
            deadline = time.monotonic() + _FINAL_SHUTDOWN_TIMEOUT
            async with startup_lock(path, deadline):
                connection = await try_connect(path, timeout=.05)
                if connection is None:
                    if not endpoint_is_listening(path):
                        continue
                    connection = await connect_with_retry(path, timeout=max(.001, deadline - time.monotonic()))
                try:
                    hello = await asyncio.wait_for(connection.recv(), timeout=_HELLO_TIMEOUT)
                    if not isinstance(hello, dict) or bool(hello.get("stub")) != self._stub:
                        raise RuntimeError("legacy Worker execution profile mismatch")
                    await retire_worker(connection, hello, path, deadline=deadline, allow_legacy=True)
                except Exception:
                    self._fatal = True
                    self._fatal_reason = "legacy Worker migration failed"
                    self._readiness.mark_failed(self._fatal_reason)
                    raise
                finally:
                    await connection.close()



    def _reap_validated_worker(self, *, deadline: float) -> None:
        """Final-exit authority is explicit; a reused PID must never be killed."""
        import psutil
        try:
            process = psutil.Process(self._worker_pid)
            if process.create_time() != self._worker_create_time:
                raise RuntimeError("Worker PID identity changed during final exit")
            grace = deadline - min(1.0, max(0.0, deadline - time.monotonic()) / 2)
            try:
                process.wait(timeout=max(0.0, grace - time.monotonic()))
            except psutil.TimeoutExpired as exc:
                if process.create_time() != self._worker_create_time:
                    raise RuntimeError("Worker PID identity changed during final exit") from exc
                logger.error("[Supervisor] Worker final exit timed out; forcing validated process termination")
                process.kill()
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except psutil.TimeoutExpired as exit_error:
                    raise RuntimeError(
                        "Validated Worker exit could not be observed before the shutdown deadline"
                    ) from exit_error
        except psutil.NoSuchProcess:
            pass
        self._owns_process = False

