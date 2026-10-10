# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Standalone AgentServer entrypoint.

Front listens first. A Runtime Supervisor then attaches a resident Worker
over local IPC. OpenJiuwen and Agent Runtime load in that Worker, not in
this process.

Gateway should be started separately and connect to this ws server.
Both processes share the same user workspace directory (~/.jiuwenswarm).

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time


# Include entry-module import/configuration work in later startup phase logs.
# PyInstaller boot time is intentionally outside this boundary.
_PROCESS_START_T0 = time.monotonic()
_STARTUP_IMPORT_PHASES: list[tuple[str, float]] = [("entry", _PROCESS_START_T0)]


def _mark_startup_import_phase(stage: str) -> None:
    _STARTUP_IMPORT_PHASES.append((stage, time.monotonic()))

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-agentserver")
_mark_startup_import_phase("dotenv_parsed")

# Standalone entrypoints retain workspace preparation; Desktop/app already do
# it before spawning us and pass the marker to avoid duplicate disk work.
# OpenJiuwen-touching prep (stale desc cleanup, config migrate) waits until
# the Runtime backend starts, so Front can listen without importing it.
from jiuwenswarm.common.utils import (
    get_env_file,
    logger,
)

_env_file = get_env_file()
load_dotenv_runtime(dotenv_path=_env_file, override=True)
_mark_startup_import_phase("runtime_environment_applied")
_mark_startup_import_phase("runtime_workspace_deferred")
_mark_startup_import_phase("entry_module_ready")



async def _start_supervisor(front: object, host: str, port: int) -> object:
    """Attach the supervisor and wait until Worker IPC is up.

    ``AGENT_READY`` arrives later on the IPC readiness stream. Control-plane
    requests are already served by Front.
    """
    from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor

    readiness = getattr(front, "readiness", None)
    supervisor = RuntimeSupervisor(
        readiness,
        forwarder=getattr(front, "forwarder", None),
        host=host,
        port=port,
    )
    attach = getattr(front, "attach_runtime_backend", None)
    if callable(attach):
        attach(supervisor)
    await supervisor.run_until_ipc_ready()
    logger.info(
        "[AgentServer] runtime worker ipc ready: ws://%s:%s pid=%s",
        host,
        port,
        getattr(supervisor, "worker_pid", None),
    )
    return supervisor


async def _run(host: str, port: int) -> None:
    from jiuwenswarm.server.front.server import AgentServerFront

    startup_t0 = time.monotonic()
    logger.info("[AgentServer] starting: ws://%s:%s", host, port)
    for import_stage, marked_at in _STARTUP_IMPORT_PHASES:
        logger.info(
            "[AgentServer] startup import stage=%s process_elapsed=%.2fs",
            import_stage,
            marked_at - _PROCESS_START_T0,
        )
    logger.info(
        "[AgentServer] startup stage=%s process_elapsed=%.2fs run_elapsed=%.2fs",
        "run_entered",
        time.monotonic() - _PROCESS_START_T0,
        time.monotonic() - startup_t0,
    )

    front = AgentServerFront(host=host, port=port)
    await front.start()
    logger.info(
        "[AgentServer] port listening: ws://%s:%s (elapsed %.2fs)",
        host,
        port,
        time.monotonic() - startup_t0,
    )
    logger.info(
        "[AgentServer] startup stage=%s process_elapsed=%.2fs run_elapsed=%.2fs control_ready=%s",
        "agent_ws_listening",
        time.monotonic() - _PROCESS_START_T0,
        time.monotonic() - startup_t0,
        front.readiness.snapshot().get("control_ready"),
    )

    stop_event = asyncio.Event()
    supervisor: object | None = None
    backend_task = asyncio.create_task(
        _start_supervisor(front, host, port),
        name="runtime-supervisor",
    )

    async def _watch_backend() -> None:
        nonlocal supervisor
        try:
            supervisor = await backend_task
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentServer] runtime supervisor failed: %s", exc)
            mark_failed = getattr(front.readiness, "mark_failed", None)
            if callable(mark_failed):
                mark_failed(str(exc))

    watcher = asyncio.create_task(_watch_backend(), name="runtime-supervisor-watch")

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        import signal

        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except (NotImplementedError, OSError):
        pass

    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("[AgentServer] stopping…")
        stop_event.set()
        begin_drain = getattr(front, "begin_drain", None)
        if callable(begin_drain):
            begin_drain()
        await asyncio.sleep(0)
        if supervisor is None and backend_task.done() and not backend_task.cancelled():
            try:
                supervisor = backend_task.result()
            except Exception:  # noqa: BLE001
                supervisor = None
        if supervisor is not None:
            # Detach only. The Worker keeps its loaded Runtime for the next Front.
            stop = getattr(supervisor, "stop", None)
            if callable(stop):
                await stop()
        elif not backend_task.done():
            backend_task.cancel()
        if not watcher.done():
            watcher.cancel()
        pending = [task for task in (backend_task, watcher) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await front.stop()
        logger.info("[AgentServer] stopped")


def _detect_sandbox_local_ip() -> str | None:
    """Best-effort 检测当前进程所在网络命名空间的非 loopback IPv4。

    用 UDP socket 连一个远端地址(不实际发包),取 ``getsockname()`` 的本端 IP。
    ISOLATED 沙箱(独立 netns)里拿到 veth 地址;HOST 模式拿到宿主出口 IP。
    失败或仅有 loopback 时返回 None,由调用方回退 127.0.0.1。
    """
    import socket

    for target in ("169.254.1.1", "1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect((target, 80))
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return None


def _resolve_bind_host() -> str:
    """决定 agentserver 的 bind host,兼顾单机版与沙箱一体机模式。"""
    env_host = os.getenv("AGENT_SERVER_HOST", "").strip()
    if env_host:
        return env_host

    if os.getenv("JIUWENBOX_LISTEN"):
        detected = _detect_sandbox_local_ip()
        if detected:
            logger.info(
                "[AgentServer] AGENT_SERVER_HOST unset in sandbox; "
                "detected sandbox local IP: %s",
                detected,
            )
            return detected
        logger.info(
            "[AgentServer] AGENT_SERVER_HOST unset in sandbox but no non-loopback "
            "IP detected; falling back to 127.0.0.1"
        )

    return "127.0.0.1"


def main() -> None:
    from jiuwenswarm.common.debug_dump import (
        install_async_dump_handler,
        install_crash_exit_handler,
    )
    from jiuwenswarm.dotenv_early import get_parsed_dotenv

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-agentserver",
        description="Start JiuwenSwarm AgentServer (standalone process for Gateway to connect).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="Bind port (default: AGENT_SERVER_PORT env or 18092).",
    )
    parser.add_argument(
        "--name",
        metavar="<name>",
        help="Start a named instance from instances.yaml.",
    )
    parser.add_argument(
        "--dotenv",
        metavar="<path>",
        help="Load environment from .env file (processed at startup, not used here).",
    )
    args = parser.parse_args()

    if args.name and get_parsed_dotenv() is None:
        raise SystemExit(1)

    host = _resolve_bind_host()
    port = args.port
    if port is None:
        for key in ("AGENT_SERVER_PORT", "AGENT_PORT"):
            raw = os.getenv(key)
            if raw:
                port = int(raw)
                break
        else:
            port = 18092

    install_crash_exit_handler("agentserver")
    install_async_dump_handler("agentserver")
    asyncio.run(_run(host=host, port=port))


if __name__ == "__main__":
    main()
