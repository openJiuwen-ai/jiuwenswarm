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

from jiuwenswarm.server.ipc.protocol import KIND_HELLO, PROTOCOL_VERSION
from jiuwenswarm.server.ipc.server import IpcServer
from jiuwenswarm.server.ipc.stream import IpcConnection
from jiuwenswarm.server.worker.dispatcher import WorkerDispatcher
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder, WorkerGatewayBridge
from jiuwenswarm.server.worker.signals import RuntimeSignalSink

logger = logging.getLogger(__name__)

_RUNTIME_LOAD_SHUTDOWN_TIMEOUT = 10.0
_DISPATCH_SHUTDOWN_TIMEOUT = 5.0
_RUNTIME_SHUTDOWN_TIMEOUT = 5.0


class WorkerForceExit(RuntimeError):
    """Dispatch ignored cancellation, so Runtime shutdown must not run."""


async def serve_worker(socket_path: str, *, host: str, port: int) -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    signals = RuntimeSignalSink(holder)
    dispatcher = WorkerDispatcher(bridge)
    server = IpcServer(socket_path)
    await server.start()
    logger.info("[Worker] ipc listening: %s", socket_path)
    loaded: dict[str, Any] = {"runtime": None}
    runtime_task: asyncio.Task | None = None
    instance = uuid.uuid4().hex
    try:
        while True:
            conn = await server.accept()
            generation = holder.open_session(conn)
            await _send_hello(
                conn,
                runtime_loaded=loaded["runtime"] is not None,
                generation=generation,
                instance=instance,
            )
            if runtime_task is None:
                runtime_task = asyncio.create_task(
                    _load_runtime(host, port, signals, dispatcher, loaded),
                    name="runtime-load",
                )
            elif holder.last_readiness is not None:
                await conn.send(holder.last_readiness)
            try:
                keep_running = await _read_session(conn, dispatcher)
            except (OSError, asyncio.IncompleteReadError):
                logger.info("[Worker] supervisor disconnected; runtime stays resident")
                await _retire_session(holder, dispatcher)
                continue
            await _retire_session(holder, dispatcher)
            if not keep_running or dispatcher.shutdown_requested:
                break
    except asyncio.CancelledError:
        logger.info("[Worker] stop requested")
    finally:
        await _close_worker(dispatcher, runtime_task, loaded, server)


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
    if runtime is not None or runtime_task is None:
        return runtime
    done, _pending = await asyncio.wait({runtime_task}, timeout=timeout)
    runtime = loaded.get("runtime")
    if runtime is not None:
        return runtime
    if runtime_task not in done:
        logger.warning(
            "[Worker] runtime load still running after %.0fs during shutdown; continuing cleanup",
            timeout,
        )
        runtime_task.cancel()
        runtime_task.add_done_callback(_log_late_runtime_load)
        return None
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
    task = asyncio.create_task(_invoke_shutdown(runtime))
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        logger.warning(
            "[Worker] runtime shutdown still running after %.0fs; continuing cleanup",
            timeout,
        )
        task.cancel()
        task.add_done_callback(_log_late_runtime_load)
        return
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("[Worker] runtime shutdown cancelled")
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime shutdown failed")


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
) -> None:
    await conn.send(
        {
            "kind": KIND_HELLO,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "instance": instance,
            "stub": False,
            "runtime_loaded": runtime_loaded,
            "generation": generation,
        }
    )


async def _read_session(conn: IpcConnection, dispatcher: WorkerDispatcher) -> bool:
    while True:
        message = await conn.recv()
        if not await dispatcher.handle(message, conn):
            return False
