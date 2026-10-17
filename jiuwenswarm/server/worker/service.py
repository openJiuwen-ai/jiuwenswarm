# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resident Worker service loop.

The process binds IPC first, then loads Agent Runtime. Supervisor disconnects
do not unload OpenJiuwen; only an explicit shutdown does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
_DISPATCH_SHUTDOWN_TIMEOUT = 10.0
_RUNTIME_SHUTDOWN_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 30.0


class WorkerForceExit(RuntimeError):
    """Dispatch ignored cancellation, so Runtime shutdown must not run."""


async def serve_worker(socket_path: str, *, host: str, port: int,
                       code_identity_value: str | None = None) -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    beat = LoopBeat()
    dispatcher = WorkerDispatcher(bridge, beat=beat)
    signals = RuntimeSignalSink(holder, on_ready=dispatcher.mark_ready)
    server = IpcServer(socket_path)
    await server.start()
    signals.ipc_ready()
    logger.info("[Worker] ipc listening: %s", socket_path)
    loaded: dict[str, Any] = {"runtime": None}
    runtime_task: asyncio.Task | None = None
    beat_task = asyncio.create_task(_beat_loop(beat), name="worker-loop-beat")
    instance = bridge.events.instance
    shutdown_conn: IpcConnection | None = None
    # WorkerForceExit means the process must die hard (main logs + os._exit).
    # Capture it so finally does not run graceful `_close_worker` and replace it
    # with CancelledError from ipc server teardown.
    fatal: WorkerForceExit | None = None
    try:
        # This is the identity of this process's loaded code, not the source
        # currently on disk at the next Front reconnect. A new Front must
        # reject this Worker after a source/dependency upgrade.
        from jiuwenswarm.server.supervisor.identity import code_identity

        worker_code_identity = (
            code_identity_value if code_identity_value is not None else await asyncio.to_thread(code_identity)
        )
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
            except (OSError, RuntimeError):
                await _retire_session(holder, dispatcher)
                await conn.close()
                continue
            if runtime_task is None:
                runtime_task = asyncio.create_task(
                    _load_runtime(host, port, signals, dispatcher, loaded),
                    name="runtime-load",
                )
            if holder.last_readiness is not None:
                await conn.send(holder.last_readiness)
            try:
                keep_running = await _read_session(conn, dispatcher)
            except WorkerForceExit as exc:
                fatal = exc
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
            await conn.close()
    except asyncio.CancelledError:
        logger.info("[Worker] stop requested")
    finally:
        beat_task.cancel()
        await asyncio.gather(beat_task, return_exceptions=True)
        try:
            try:
                if fatal is None:
                    await _close_worker(dispatcher, runtime_task, loaded, server)
            except Exception as exc:
                if shutdown_conn is not None:
                    try:
                        await shutdown_conn.send({"kind": KIND_SHUTDOWN_DONE, "ok": False,
                                                  "error": str(exc)})
                    except (OSError, RuntimeError):
                        pass
                if isinstance(exc, WorkerForceExit):
                    fatal = exc
                else:
                    raise
            if shutdown_conn is not None and fatal is None:
                await shutdown_conn.send({"kind": KIND_SHUTDOWN_DONE, "ok": True})
        finally:
            if shutdown_conn is not None:
                await shutdown_conn.close()
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


def _log_late_runtime_load(task: asyncio.Task) -> None:
    """Consume a load that finished after shutdown already moved on."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("[Worker] runtime load finished after shutdown timeout: %s", exc)
        return
    logger.warning(
        "[Worker] runtime load finished after shutdown timeout; cleanup already continued"
    )


async def _runtime_for_shutdown(
    runtime_task: asyncio.Task | None,
    loaded: dict[str, Any],
    *,
    timeout: float = _RUNTIME_LOAD_SHUTDOWN_TIMEOUT,
) -> Any:
    """Return a loaded Runtime, or give up once ``timeout`` elapses.

    ``asyncio.wait_for`` still waits until a cancelled task finishes. A load
    blocked in ``asyncio.to_thread`` does not finish when cancelled, so this
    uses ``asyncio.wait`` and continues cleanup while that thread runs.
    """
    runtime = loaded.get("runtime")
    if runtime_task is None:
        return runtime
    done, _pending = await asyncio.wait({runtime_task}, timeout=timeout)
    runtime = loaded.get("runtime")
    if runtime_task not in done:
        logger.warning(
            "[Worker] runtime load still running after %.0fs during shutdown; continuing cleanup",
            timeout,
        )
        runtime_task.cancel()
        runtime_task.add_done_callback(_log_late_runtime_load)
        raise WorkerForceExit("runtime load did not drain before shutdown deadline")
    try:
        return runtime_task.result() or runtime
    except asyncio.CancelledError:
        logger.info("[Worker] runtime load cancelled during shutdown")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime load failed during shutdown")
        return runtime


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
    total_timeout: float = SHUTDOWN_TIMEOUT,
) -> None:
    """Cancel active dispatch before shutting the Runtime down.

    A dispatch that is still running after the drain timeout keeps its
    reference to Runtime. Skip the graceful shutdown and force the process
    to exit instead of releasing that Runtime underneath it.
    """
    deadline = time.monotonic() + total_timeout

    def remaining(stage_limit: float) -> float:
        return max(0.001, min(stage_limit, deadline - time.monotonic()))

    if not await dispatcher.drain(timeout=remaining(dispatch_timeout)):
        logger.error(
            "[Worker] dispatch still running after shutdown timeout; skipping runtime shutdown"
        )
        raise WorkerForceExit("dispatch still running")
    runtime = await _runtime_for_shutdown(runtime_task, loaded, timeout=remaining(load_timeout))
    if runtime is not None:
        await _shutdown_runtime(runtime, timeout=remaining(runtime_timeout))
    close = getattr(server, "close", None)
    if callable(close):
        await close()


async def _shutdown_runtime(runtime: Any, *, timeout: float) -> None:
    task = asyncio.create_task(_invoke_shutdown(runtime))
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        logger.warning(
            "[Worker] runtime shutdown still running after %.0fs; continuing cleanup",
            timeout,
        )
        task.cancel()
        task.add_done_callback(_log_late_runtime_load)
        raise WorkerForceExit("runtime shutdown exceeded shared deadline")
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("[Worker] runtime shutdown cancelled")
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime shutdown failed")
        raise WorkerForceExit("runtime shutdown failed; persistence or resource cleanup incomplete") from None


async def _invoke_shutdown(runtime: Any) -> None:
    shutdown = getattr(runtime, "shutdown", None)
    if callable(shutdown):
        await shutdown()
    else:
        stop = getattr(runtime, "stop", None)
        if callable(stop):
            await stop()


async def _load_runtime(
    host: str,
    port: int,
    signals: RuntimeSignalSink,
    dispatcher: WorkerDispatcher,
    loaded: dict[str, Any],
) -> Any:
    from jiuwenswarm.server.worker.lifecycle import WorkerRuntime, start_worker_runtime

    async def constructed(server: Any) -> WorkerRuntime:
        # Retain a cleanup owner even when preparation fails after construction.
        # Shutdown still waits for the loader to stop before releasing resources.
        loaded["runtime"] = WorkerRuntime(server=server, stop_event=asyncio.Event())
        await dispatcher.bind_runtime(server)
        return loaded["runtime"]

    try:
        runtime = await start_worker_runtime(
            host,
            port,
            signals,
            on_constructed=constructed,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Worker] runtime load failed: %s", exc)
        signals.failed(str(exc))
        await dispatcher.mark_failed(str(exc))
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
    from jiuwenswarm.server.supervisor.identity import code_identity, data_scope
    identity = code_identity_value if code_identity_value is not None else await asyncio.to_thread(code_identity)
    await conn.send(
        {
            "kind": KIND_HELLO,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "instance": instance,
            "stub": False,
            "runtime_loaded": runtime_loaded,
            "generation": generation,
            "data_scope": data_scope(),
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
