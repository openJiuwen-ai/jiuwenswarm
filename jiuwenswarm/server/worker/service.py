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
import uuid
from typing import Any

from jiuwenswarm.server.ipc.protocol import KIND_HELLO, PROTOCOL_VERSION
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
    """Dispatch ignored cancellation, so Runtime shutdown must not run."""


def _trace_enabled(trace_state: dict[str, Any] | None) -> bool:
    settings = (trace_state or {}).get("settings")
    return bool(settings is not None and getattr(settings, "enabled", False))


def _trace_recorder(trace_state: dict[str, Any] | None) -> Any:
    """Return the recorder without importing lifecycle while disabled."""
    if not _trace_enabled(trace_state):
        return None
    try:
        from jiuwenswarm.runtime_trace.lifecycle import current_recorder

        return current_recorder()
    except Exception:  # noqa: BLE001 - tracing must not affect worker flow
        return None


def _trace_lifecycle(trace_state: dict[str, Any] | None) -> Any:
    if not _trace_enabled(trace_state):
        return None
    try:
        from jiuwenswarm.runtime_trace import lifecycle

        return lifecycle
    except Exception:  # noqa: BLE001
        logger.warning("[Worker] runtime trace lifecycle unavailable", exc_info=True)
        return None


async def serve_worker(
    socket_path: str,
    *,
    host: str,
    port: int,
    trace_state: dict[str, Any] | None = None,
) -> None:
    holder = ConnHolder()
    bridge = WorkerGatewayBridge(holder)
    signals = RuntimeSignalSink(holder)
    beat = LoopBeat()
    dispatcher = WorkerDispatcher(bridge, beat=beat)
    server = IpcServer(socket_path)
    await server.start()
    logger.info("[Worker] ipc listening: %s", socket_path)
    if trace_state is not None:
        clock = trace_state.get("clock")
        if clock is not None:
            clock.mark("ipc_listening")
    loaded: dict[str, Any] = {"runtime": None}
    runtime_task: asyncio.Task | None = None
    beat_task = asyncio.create_task(_beat_loop(beat), name="worker-loop-beat")
    instance = uuid.uuid4().hex
    if trace_state is not None:
        trace_state["instance"] = instance
    recorder = _trace_recorder(trace_state)
    if recorder is not None:
        recorder.set_worker_instance(instance)
    # WorkerForceExit means the process must die hard (main logs + SystemExit).
    # Capture it so finally does not run graceful `_close_worker` and replace it
    # with CancelledError from ipc server teardown.
    fatal: WorkerForceExit | None = None
    cancelled = False
    try:
        while True:
            conn = await server.accept()
            _install_heartbeat_fast_path(conn, beat)
            generation = holder.open_session(conn)
            if trace_state is not None:
                trace_state["generation"] = generation
            recorder = _trace_recorder(trace_state)
            if recorder is not None:
                recorder.set_worker_generation(generation)
            await _send_hello(
                conn,
                runtime_loaded=loaded["runtime"] is not None,
                generation=generation,
                instance=instance,
            )
            if runtime_task is None:
                runtime_task = asyncio.create_task(
                    _load_runtime(
                        host, port, signals, dispatcher, loaded, trace_state
                    ),
                    name="runtime-load",
                )
            elif holder.last_readiness is not None:
                await conn.send(holder.last_readiness)
            try:
                keep_running = await _read_session(conn, dispatcher)
            except WorkerForceExit as exc:
                fatal = exc
                break
            except (OSError, asyncio.IncompleteReadError):
                logger.info("[Worker] supervisor disconnected; runtime stays resident")
                await _retire_session(holder, dispatcher)
                continue
            await _retire_session(holder, dispatcher)
            if not keep_running or dispatcher.shutdown_requested:
                break
    except asyncio.CancelledError:
        cancelled = True
        logger.info("[Worker] stop requested")
    finally:
        # The business shutdown budget starts here; tracing shares it and is
        # capped at the short drain limit.
        shutdown_started = time.monotonic()
        business_deadline = shutdown_started + 20.0
        beat_task.cancel()
        await asyncio.gather(beat_task, return_exceptions=True)
        lifecycle = _trace_lifecycle(trace_state)
        if lifecycle is not None:
            # A startup root still open means startup never completed; close it
            # before opening the shutdown root (the two never overlap).
            lifecycle.close_open_startup_root()
            lifecycle.start_shutdown_root()
        try:
            if fatal is None:
                try:
                    await _close_worker(
                        dispatcher,
                        runtime_task,
                        loaded,
                        server,
                        trace_state=trace_state,
                    )
                except WorkerForceExit as exc:
                    fatal = exc
        finally:
            if lifecycle is not None:
                if fatal is not None:
                    outcome, reason = "cancelled", "dispatch_stuck"
                elif cancelled:
                    outcome, reason = "cancelled", "cancel_requested"
                else:
                    outcome, reason = "success", None
                # Blocking teardown: an await here can be cancelled before the
                # writer drains, losing the shutdown root on Ctrl+C. The call
                # itself is bounded by the shared shutdown deadline.
                lifecycle.shutdown_runtime_trace(
                    deadline_mono=(
                        time.monotonic() + 0.5
                        if fatal is not None
                        else business_deadline
                    ),
                    outcome=outcome,
                    reason_code=reason,
                )
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
    trace_state: dict[str, Any] | None = None,
    dispatch_timeout: float = _DISPATCH_SHUTDOWN_TIMEOUT,
    load_timeout: float = _RUNTIME_LOAD_SHUTDOWN_TIMEOUT,
    runtime_timeout: float = _RUNTIME_SHUTDOWN_TIMEOUT,
) -> None:
    """Cancel active dispatch before shutting the Runtime down.

    A dispatch that is still running after the drain timeout keeps its
    reference to Runtime. Skip the graceful shutdown and force the process
    to exit instead of releasing that Runtime underneath it.
    """
    recorder = _trace_recorder(trace_state)
    dispatch_handle = (
        recorder.start(
            "shutdown.step.dispatch_drain",
            parent=recorder.shutdown_handle,
            level="normal",
        )
        if recorder is not None
        else None
    )
    drained = await dispatcher.drain(timeout=dispatch_timeout)
    if recorder is not None:
        recorder.end(
            dispatch_handle,
            "success" if drained else "error",
            attributes=None
            if drained
            else {"runtime.reason_code": "dispatch_stuck"},
        )
    if not drained:
        logger.error(
            "[Worker] dispatch still running after shutdown timeout; skipping runtime shutdown"
        )
        raise WorkerForceExit("dispatch still running")
    runtime_handle = (
        recorder.start(
            "shutdown.step.runtime",
            parent=recorder.shutdown_handle,
            level="normal",
        )
        if recorder is not None
        else None
    )
    runtime = await _runtime_for_shutdown(runtime_task, loaded, timeout=load_timeout)
    runtime_ok = True
    if runtime is not None:
        runtime_ok = await _shutdown_runtime(runtime, timeout=runtime_timeout)
    if recorder is not None:
        recorder.end(
            runtime_handle,
            "success" if runtime_ok else "error",
            attributes=None
            if runtime_ok
            else {"runtime.reason_code": "shutdown_timeout"},
        )
    ipc_handle = (
        recorder.start(
            "shutdown.step.ipc",
            parent=recorder.shutdown_handle,
            level="normal",
        )
        if recorder is not None
        else None
    )
    close = getattr(server, "close", None)
    if callable(close):
        await close()
    if recorder is not None:
        recorder.end(ipc_handle, "success")


async def _shutdown_runtime(runtime: Any, *, timeout: float) -> bool:
    task = asyncio.create_task(_invoke_shutdown(runtime))
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        logger.warning(
            "[Worker] runtime shutdown still running after %.0fs; continuing cleanup",
            timeout,
        )
        task.cancel()
        task.add_done_callback(_log_late_runtime_load)
        return False
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("[Worker] runtime shutdown cancelled")
        return False
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] runtime shutdown failed")
        return False
    return True


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
    trace_state: dict[str, Any] | None = None,
) -> Any:
    from jiuwenswarm.server.worker.lifecycle import start_worker_runtime

    try:
        runtime = await start_worker_runtime(
            host,
            port,
            signals,
            on_constructed=dispatcher.bind_runtime,
            trace_state=trace_state,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Worker] runtime load failed: %s", exc)
        signals.failed(str(exc))
        if _trace_enabled(trace_state):
            try:
                from jiuwenswarm.runtime_trace.lifecycle import (
                    attach_runtime_trace_on_failure,
                )

                await asyncio.to_thread(
                    attach_runtime_trace_on_failure,
                    settings=(trace_state or {}).get("settings"),
                    clock=(trace_state or {}).get("clock"),
                    worker_instance=(trace_state or {}).get("instance"),
                    generation=(trace_state or {}).get("generation"),
                    reason_code="runtime_load_failed",
                )
            except Exception:  # noqa: BLE001 - trace must not mask load failure
                logger.warning(
                    "[Worker] runtime trace failure attach skipped", exc_info=True
                )
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
    arm_stall_dump()
    try:
        while True:
            message = await conn.recv()
            arm_stall_dump()
            if not await dispatcher.handle(message, conn):
                return False
    finally:
        cancel_stall_dump()
