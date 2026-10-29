# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resident Worker service loop.

The process binds IPC first, then loads Agent Runtime. Supervisor disconnects
do not unload OpenJiuwen; only an explicit shutdown does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN_DONE, PROTOCOL_VERSION
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.diagnostics import (
    LoopBeat,
    arm_stall_dump,
    cancel_stall_dump,
    heartbeat_reply,
)
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge
from jiuwenswarm.server.worker.signals import RuntimeSignalSink

logger = logging.getLogger(__name__)

_RUNTIME_LOAD_SHUTDOWN_TIMEOUT = 10.0
_DISPATCH_SHUTDOWN_TIMEOUT = 5.0
_RUNTIME_SHUTDOWN_TIMEOUT = 5.0


class WorkerForceExit(RuntimeError):
    """Runtime cleanup is incomplete, so the process must exit unsuccessfully."""


async def serve_worker(socket_path: str, *, host: str, port: int,
                       code_identity_value: str | None = None) -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    signals = RuntimeSignalSink(holder)
    beat = LoopBeat()
    dispatcher = WorkerDispatcher(bridge, beat=beat)
    server = IpcServer(socket_path)
    await server.start()
    logger.info("[Worker] ipc listening: %s", socket_path)
    loaded: dict[str, Any] = {"runtime": None}
    runtime_task: asyncio.Task | None = None
    beat_task = asyncio.create_task(_beat_loop(beat), name="worker-loop-beat")
    instance = uuid.uuid4().hex
    conn: IpcConnection | None = None
    shutdown_conn: IpcConnection | None = None
    # WorkerForceExit means the process must die hard (main logs + os._exit).
    # Capture it so finally does not run graceful `_close_worker` and replace it
    # with CancelledError from ipc server teardown.
    fatal: WorkerForceExit | None = None
    try:
        from jiuwenswarm.server.supervisor.identity import code_identity

        worker_code_identity = (code_identity_value if code_identity_value is not None
                                else await asyncio.to_thread(code_identity))
        while True:
            conn = await server.accept()
            _install_heartbeat_fast_path(conn, beat)
            generation = holder.open_session(conn)
            try:
                await _send_hello(
                    conn,
                    runtime_loaded=loaded["runtime"] is not None,
                    generation=generation,
                    instance=instance,
                    code_identity_value=worker_code_identity,
                )
                if runtime_task is None:
                    runtime_task = asyncio.create_task(
                        _load_runtime(host, port, signals, dispatcher, loaded),
                        name="runtime-load",
                    )
                elif holder.last_readiness is not None:
                    await conn.send(holder.last_readiness)
            except (OSError, RuntimeError):
                await _retire_session(holder, dispatcher)
                await conn.close()
                continue
            try:
                keep_running = await _read_session(conn, dispatcher)
            except WorkerForceExit as exc:
                fatal = exc
                if dispatcher.shutdown_requested:
                    shutdown_conn = conn
                break
            except (OSError, asyncio.IncompleteReadError):
                logger.info("[Worker] supervisor disconnected; runtime stays resident")
                await _retire_session(holder, dispatcher)
                await conn.close()
                continue
            await _retire_session(holder, dispatcher)
            if not keep_running or dispatcher.shutdown_requested:
                shutdown_conn = conn
                break
    except asyncio.CancelledError:
        logger.info("[Worker] stop requested")
    finally:
        beat_task.cancel()
        await asyncio.gather(beat_task, return_exceptions=True)
        try:
            if fatal is None:
                try:
                    await _close_worker(dispatcher, runtime_task, loaded, server)
                except Exception as exc:
                    fatal = (exc if isinstance(exc, WorkerForceExit) else
                             WorkerForceExit(f"Worker shutdown failed: {type(exc).__name__}: {exc}"))
            if shutdown_conn is not None:
                outcome = {"kind": KIND_SHUTDOWN_DONE, "ok": fatal is None}
                if fatal is not None:
                    outcome["error"] = str(fatal)
                try:
                    await asyncio.wait_for(shutdown_conn.send(outcome), timeout=1.0)
                except (OSError, RuntimeError, TimeoutError):
                    fatal = fatal or WorkerForceExit("shutdown result could not be delivered")
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except (OSError, RuntimeError, asyncio.CancelledError):
                    if fatal is None:
                        raise
                    logger.warning("[Worker] IPC close failed during fatal exit", exc_info=True)
    if fatal is not None:
        raise fatal


async def _beat_loop(beat: LoopBeat) -> None:
    while True:
        beat.beat()
        await asyncio.sleep(1.0)


def _install_heartbeat_fast_path(conn: IpcConnection, beat: LoopBeat) -> None:
    """Answer pings on the reader thread where the transport supports it.

    The pong reports the business loop's stall age, so heartbeats keep
    flowing during a blocked loop without masking the stall itself.
    """
    set_responder = getattr(conn, "set_heartbeat_responder", None)
    if not callable(set_responder) or not hasattr(conn, "send_direct"):
        return

    def respond(message: dict[str, Any]) -> None:
        arm_stall_dump()
        conn.send_direct(heartbeat_reply(beat.age))

    set_responder(respond)


def _log_late_runtime_task(task: asyncio.Task) -> None:
    """Consume a Runtime task that finished after its shutdown deadline."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("[Worker] %s finished after shutdown timeout: %s", task.get_name(), exc)
        return
    logger.warning(
        "[Worker] %s finished after shutdown timeout; process shutdown already started", task.get_name()
    )


async def _runtime_for_shutdown(
    runtime_task: asyncio.Task | None,
    loaded: dict[str, Any],
    *,
    timeout: float = _RUNTIME_LOAD_SHUTDOWN_TIMEOUT,
) -> Any:
    """Return a loaded Runtime, or force exit when loading cannot stop in time.

    ``asyncio.wait_for`` still waits until a cancelled task finishes. A load
    blocked in ``asyncio.to_thread`` does not finish when cancelled, so this
    uses ``asyncio.wait`` before escalating to process exit.
    """
    runtime = loaded.get("runtime")
    if runtime is not None or runtime_task is None:
        return runtime
    done, _pending = await asyncio.wait({runtime_task}, timeout=timeout)
    runtime = loaded.get("runtime")
    if runtime is not None:
        return runtime
    if runtime_task not in done:
        logger.warning(
            "[Worker] runtime load still running after %.0fs during shutdown; forcing process exit",
            timeout,
        )
        runtime_task.cancel()
        runtime_task.add_done_callback(_log_late_runtime_task)
        raise WorkerForceExit("runtime load exceeded shutdown deadline")
    try:
        return runtime_task.result()
    except asyncio.CancelledError:
        logger.info("[Worker] runtime load cancelled during shutdown")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime load failed during shutdown")
        return None


async def _retire_session(holder: ConnHolder, dispatcher: WorkerDispatcher) -> None:
    """Drop one supervisor session and clean up its Gateway attachment."""
    await dispatcher.detach_gateway(remote="supervisor disconnected")
    generation = holder.close_session()
    if generation:
        await dispatcher.retire_generation(generation)


async def _close_worker(
    dispatcher: WorkerDispatcher,
    runtime_task: asyncio.Task | None,
    loaded: dict[str, Any],
    server: Any,
    *,
    dispatch_timeout: float = _DISPATCH_SHUTDOWN_TIMEOUT,
    load_timeout: float = _RUNTIME_LOAD_SHUTDOWN_TIMEOUT,
    runtime_timeout: float = _RUNTIME_SHUTDOWN_TIMEOUT,
) -> None:
    """Cancel active dispatch before shutting the Runtime down.

    A dispatch that is still running after the drain timeout keeps its
    reference to Runtime. Skip the graceful shutdown and force the process
    to exit instead of releasing that Runtime underneath it.
    """
    if not await dispatcher.drain(timeout=dispatch_timeout):
        logger.error(
            "[Worker] dispatch still running after shutdown timeout; skipping runtime shutdown"
        )
        raise WorkerForceExit("dispatch still running")
    runtime = await _runtime_for_shutdown(runtime_task, loaded, timeout=load_timeout)
    if runtime is not None:
        await _shutdown_runtime(runtime, timeout=runtime_timeout)
    close = getattr(server, "close", None)
    if callable(close):
        await close()


async def _shutdown_runtime(runtime: Any, *, timeout: float) -> None:
    task = asyncio.create_task(_invoke_shutdown(runtime), name="runtime-shutdown")
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        logger.warning(
            "[Worker] runtime shutdown still running after %.0fs; forcing process exit",
            timeout,
        )
        task.cancel()
        task.add_done_callback(_log_late_runtime_task)
        raise WorkerForceExit("runtime shutdown exceeded deadline")
    try:
        task.result()
    except asyncio.CancelledError:
        raise WorkerForceExit("runtime shutdown cancelled; persistence or resource cleanup incomplete") from None
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime shutdown failed")
        raise WorkerForceExit("runtime shutdown failed; persistence or resource cleanup incomplete") from None


async def _invoke_shutdown(runtime: Any) -> None:
    shutdown = getattr(runtime, "shutdown", None)
    if callable(shutdown):
        await shutdown()


async def _load_runtime(
    host: str,
    port: int,
    signals: RuntimeSignalSink,
    dispatcher: WorkerDispatcher,
    loaded: dict[str, Any],
) -> Any:
    from jiuwenswarm.server.worker.lifecycle import start_worker_runtime

    try:
        runtime = await start_worker_runtime(
            host,
            port,
            signals,
            on_constructed=dispatcher.bind_runtime,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Worker] runtime load failed: %s", exc)
        signals.failed(str(exc))
        raise
    loaded["runtime"] = runtime
    return runtime


async def _send_hello(
    conn: IpcConnection,
    *,
    runtime_loaded: bool,
    generation: int,
    instance: str,
    code_identity_value: str | None = None,
) -> None:
    from jiuwenswarm.server.supervisor.identity import code_identity, data_scope, deployment_scope
    import psutil
    identity = code_identity_value if code_identity_value is not None else await asyncio.to_thread(code_identity)
    await conn.send(
        {
            "kind": KIND_HELLO,
            "protocol": PROTOCOL_VERSION,
            "shutdown_result": True,
            "pid": os.getpid(),
            "instance": instance,
            "stub": False,
            "runtime_loaded": runtime_loaded,
            "generation": generation,
            "data_scope": data_scope(),
            "deployment_scope": deployment_scope(),
            "process_created": psutil.Process().create_time(),
            "code_identity": identity,
        }
    )


async def _read_session(conn: IpcConnection, dispatcher: WorkerDispatcher) -> bool:
    arm_stall_dump()
    try:
        while True:
            message = await conn.recv()
            arm_stall_dump()
            if not await dispatcher.handle(message, conn):
                return False
    finally:
        cancel_stall_dump()
