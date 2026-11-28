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
import logging
import os
import time


# Include entry-module import/configuration work in later startup phase logs.
# PyInstaller boot time is intentionally outside this boundary.
_PROCESS_START_T0 = time.monotonic()
_STARTUP_IMPORT_PHASES: list[tuple[str, float]] = [("entry", _PROCESS_START_T0)]

# A websocket client may never finish its close handshake. Front's HTTP/WS
# stop must not consume the launcher's graceful-shutdown grace, which also
# has to cover the supervisor stop and the runtime trace drain.
_FRONT_STOP_TIMEOUT_SECONDS = 3.0


def _mark_startup_import_phase(stage: str) -> None:
    _STARTUP_IMPORT_PHASES.append((stage, time.monotonic()))

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
from jiuwenswarm.common.runtime_settings import prepare_runtime_environment
prepare_runtime_environment()
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



async def _start_supervisor(
    front: object, host: str, port: int, recorder: object = None
) -> object:
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
        recorder=recorder,
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


def _replay_front_startup_phases(recorder: object, config_end_ns: int) -> None:
    """Replay import marks and the config phase under the startup root."""
    if recorder is None:
        return
    try:
        marks = list(_STARTUP_IMPORT_PHASES)
        for previous, current in zip(marks, marks[1:]):
            stage = current[0]
            handle = recorder.start(
                f"startup.phase.{stage}",
                parent=recorder.startup_handle,
                level="normal",
                mono_start=int(previous[1] * 1_000_000_000),
                attributes={"runtime.stage": stage},
            )
            recorder.end(
                handle, "success", mono_end=int(current[1] * 1_000_000_000)
            )
        handle = recorder.start(
            "startup.phase.config",
            parent=recorder.startup_handle,
            level="normal",
            mono_start=int(_PROCESS_START_T0 * 1_000_000_000),
            attributes={"runtime.stage": "config"},
        )
        recorder.end(handle, "success", mono_end=config_end_ns)
    except Exception:  # noqa: BLE001 - tracing must not affect startup
        logger.warning("[AgentServer] runtime trace replay failed", exc_info=True)


def _end_startup_phase(
    recorder: object,
    name: str,
    start_ns: int,
    outcome: str = "success",
    reason_code: str | None = None,
    *,
    end_ns: int | None = None,
    parent: object = None,
) -> None:
    """Close one explicit startup/shutdown phase span."""
    if recorder is None:
        return
    try:
        attributes: dict[str, object] = {"runtime.stage": name.rsplit(".", 1)[-1]}
        if reason_code:
            attributes["runtime.reason_code"] = reason_code
        parent_handle = parent if parent is not None else recorder.startup_handle
        handle = recorder.start(
            name,
            parent=parent_handle,
            level="normal",
            mono_start=start_ns,
            attributes=attributes,
        )
        recorder.end(
            handle,
            outcome,
            mono_end=end_ns if isinstance(end_ns, int) else time.monotonic_ns(),
            attributes=attributes,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[AgentServer] runtime trace phase failed", exc_info=True)


def _close_startup_root(
    recorder: object, outcome: str | None, reason_code: str | None
) -> None:
    if recorder is None:
        return
    try:
        from jiuwenswarm.runtime_trace.lifecycle import close_open_startup_root

        if outcome is None:
            close_open_startup_root()
        else:
            close_open_startup_root(outcome=outcome, reason_code=reason_code)
    except Exception:  # noqa: BLE001
        logger.warning("[AgentServer] runtime trace startup close failed", exc_info=True)


def _configure_openjiuwen_logging() -> None:
    """Bind OpenJiuwen logging to the writable workspace before Front serves.

    Front handles control-plane requests (``history.get`` among them) whose
    todo snapshot lazily imports OpenJiuwen. Without an explicit configuration
    OpenJiuwen falls back to its relative default ``./logs/``, resolved against
    the process CWD. AgentOS sandboxes run with CWD=/ on a read-only root, so
    logger creation raises and the history stream aborts before its done frame.

    Both branches are guarded: a missing or malformed ``logging.yaml`` (or an
    unwritable path inside it) must not crash Front startup, so the workspace
    fallback config is tried next and only if that also fails does the function
    give up with a warning, leaving Front's default logging intact.
    """
    from jiuwenswarm.common.utils import get_root_dir

    logging_yaml = get_root_dir() / "config" / "logging.yaml"
    if logging_yaml.exists():
        try:
            from openjiuwen.core.common.logging.log_config import configure_log

            configure_log(str(logging_yaml))
            return
        except Exception:  # noqa: BLE001
            logger.warning(
                "[AgentServer] openjiuwen logging.yaml config failed; "
                "trying workspace fallback",
                exc_info=True,
            )

    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config

        home = (
            os.environ.get("JIUWENSWARM_HOME")
            or os.environ.get("HOME")
            or os.path.expanduser("~")
        )
        configure_log_config({
            "backend": "default",
            "level": "INFO",
            "log_path": f"{home}/.jiuwenswarm/logs/",
            "log_file": "run/jiuwen.log",
            "output": ["file"],
            "structured_output_format": "json",
            "interface_log_file": "interface/jiuwen_interface.log",
            "prompt_builder_interface_log_file": "interface/jiuwen_prompt_builder_interface.log",
            "performance_log_file": "performance/jiuwen_performance.log",
        })
    except Exception:  # noqa: BLE001
        logger.warning(
            "[AgentServer] openjiuwen log config failed; Front keeps default logging",
            exc_info=True,
        )
        return

    try:
        from openjiuwen.core.common.logging import LogManager

        for _lg in LogManager.get_all_loggers().values():
            _lg.set_level(logging.CRITICAL)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[AgentServer] openjiuwen log level tuning failed; "
            "Front keeps default levels",
            exc_info=True,
        )


async def _run(host: str, port: int) -> None:
    # Install stop handlers before any startup work so Ctrl+C during the
    # (multi-second) trace attach or Front import still becomes a normal
    # shutdown with a shutdown root instead of KeyboardInterrupt.
    stop_event = asyncio.Event()
    stop_signal = False
    supervisor: object | None = None

    def _on_signal() -> None:
        nonlocal stop_signal
        stop_signal = True
        stop_event.set()
        # Mark the supervisor as stopping immediately. The Worker also gets
        # Ctrl+C and can close its IPC before this coroutine reaches the
        # shutdown block; without this, the EOF looks like a crash and the
        # restart path terminates a Worker that is shutting down gracefully.
        request_stop = getattr(supervisor, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[AgentServer] supervisor stop request failed", exc_info=True
                )

    loop = asyncio.get_running_loop()
    try:
        import signal

        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except (NotImplementedError, OSError):
        # Windows event loops do not support add_signal_handler. Install a
        # plain SIGINT/SIGBREAK handler that schedules the same callback,
        # otherwise Ctrl+C becomes KeyboardInterrupt and skips the normal
        # shutdown block (and its shutdown trace).
        try:
            import signal

            def _fallback_handler(signum, frame) -> None:
                try:
                    loop.call_soon_threadsafe(_on_signal)
                except RuntimeError:
                    pass

            for signal_name in ("SIGINT", "SIGBREAK"):
                signum = getattr(signal, signal_name, None)
                if signum is None:
                    continue
                try:
                    signal.signal(signum, _fallback_handler)
                except (ValueError, OSError):
                    continue
        except Exception:  # noqa: BLE001
            logger.debug(
                "[AgentServer] fallback signal handler install failed",
                exc_info=True,
            )

    # Runtime trace is assembled before the Front import so a WS bind failure
    # still leaves a startup root on disk. Disabled settings load nothing heavy.
    trace_recorder = None
    config_end_ns = time.monotonic_ns()
    trace_attach_end_ns = config_end_ns
    try:
        from jiuwenswarm.runtime_trace.config import load_runtime_trace_settings

        settings = load_runtime_trace_settings()
        config_end_ns = time.monotonic_ns()
        if settings.enabled:
            from jiuwenswarm.runtime_trace.clock import ClockBase
            from jiuwenswarm.runtime_trace.lifecycle import attach_runtime_trace

            trace_recorder = attach_runtime_trace(
                role="front",
                settings=settings,
                clock=ClockBase(),
                startup_start_mono_ns=int(_PROCESS_START_T0 * 1_000_000_000),
            )
        trace_attach_end_ns = time.monotonic_ns()
    except Exception:  # noqa: BLE001 - tracing must never block startup
        logger.warning("[AgentServer] runtime trace init failed", exc_info=True)
        trace_attach_end_ns = time.monotonic_ns()
        trace_recorder = None

    if trace_recorder is not None:
        # Assembling the recorder imports OpenJiuwen and OpenTelemetry. Front
        # does not load them otherwise, so this is a real, visible cold-start
        # cost and must not hide between config and front_init.
        _end_startup_phase(
            trace_recorder,
            "startup.phase.trace_attach",
            config_end_ns,
            end_ns=trace_attach_end_ns,
        )

    # Import phases above stop at entry_module_ready. The Front server import,
    # logging setup, and the shell patch below import OpenJiuwen, which is a
    # multi-second step on a cold start; cover it so the startup root has no
    # unexplained gap before ws_listen.
    front_init_start_ns = time.monotonic_ns()
    from jiuwenswarm.server.front.server import AgentServerFront

    _configure_openjiuwen_logging()
    from jiuwenswarm.server.runtime.mcp.cli_env_patch import install_shell_patch

    install_shell_patch()
    _end_startup_phase(
        trace_recorder, "startup.phase.front_init", front_init_start_ns
    )

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
    _replay_front_startup_phases(trace_recorder, config_end_ns)

    backend_task: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    front: object | None = None
    startup_outcome: str | None = None
    startup_reason: str | None = None
    shutdown_outcome = "success"
    shutdown_entered = False

    try:
        front = AgentServerFront(host=host, port=port)
        ws_start_ns = time.monotonic_ns()
        try:
            await front.start()
        except Exception:
            startup_outcome, startup_reason = "error", "ws_bind_failed"
            _end_startup_phase(
                trace_recorder,
                "startup.phase.ws_listen",
                ws_start_ns,
                "error",
                "ws_bind_failed",
            )
            raise
        else:
            _end_startup_phase(
                trace_recorder, "startup.phase.ws_listen", ws_start_ns
            )
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

        supervisor_start_ns = time.monotonic_ns()
        backend_task = asyncio.create_task(
            _start_supervisor(front, host, port, recorder=trace_recorder),
            name="runtime-supervisor",
        )

        async def _watch_backend() -> None:
            nonlocal supervisor, startup_outcome, startup_reason
            try:
                supervisor = await backend_task
            except Exception as exc:  # noqa: BLE001
                logger.exception("[AgentServer] runtime supervisor failed: %s", exc)
                startup_outcome, startup_reason = "error", "ipc_timeout"
                mark_failed = getattr(front.readiness, "mark_failed", None)
                if callable(mark_failed):
                    mark_failed(str(exc))
            finally:
                phase_outcome = "success" if startup_outcome is None else "error"
                _end_startup_phase(
                    trace_recorder,
                    "startup.phase.supervisor_start",
                    supervisor_start_ns,
                    phase_outcome,
                    startup_reason,
                )
                if startup_outcome is None:
                    _close_startup_root(trace_recorder, "success", None)
                else:
                    _close_startup_root(
                        trace_recorder, startup_outcome, startup_reason
                    )

        watcher = asyncio.create_task(
            _watch_backend(), name="runtime-supervisor-watch"
        )

        try:
            await stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            if startup_outcome is None:
                startup_outcome = "cancelled"
                startup_reason = "cancel_requested"
        finally:
            logger.info("[AgentServer] stopping…")
            stop_event.set()
            _close_startup_root(trace_recorder, startup_outcome, startup_reason)
            shutdown_entered = True
            try:
                from jiuwenswarm.runtime_trace.lifecycle import (
                    start_shutdown_root,
                )

                start_shutdown_root()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] runtime trace shutdown root failed",
                    exc_info=True,
                )
            shutdown_handle = (
                trace_recorder.shutdown_handle
                if trace_recorder is not None
                else None
            )

            def _end_shutdown_step(
                name: str, start_ns: int, outcome: str = "success"
            ) -> None:
                if trace_recorder is None or shutdown_handle is None:
                    return
                _end_startup_phase(
                    trace_recorder,
                    name,
                    start_ns,
                    outcome,
                    parent=shutdown_handle,
                    end_ns=time.monotonic_ns(),
                )

            try:
                drain_start_ns = time.monotonic_ns()
                try:
                    begin_drain = getattr(front, "begin_drain", None)
                    if callable(begin_drain):
                        begin_drain()
                    await asyncio.sleep(0)
                except BaseException:
                    _end_shutdown_step(
                        "shutdown.step.drain", drain_start_ns, "error"
                    )
                    raise
                _end_shutdown_step("shutdown.step.drain", drain_start_ns)

                if (
                    (stop_signal or startup_outcome == "cancelled")
                    and trace_recorder is not None
                    and trace_recorder.shutdown_handle is not None
                    and not trace_recorder.shutdown_handle.ended
                ):
                    # Operator stop: end the shutdown root together with the
                    # drain step. The launcher may kill Front while the slow
                    # supervisor/front stop awaits run, and an open root is
                    # never exported. Later steps then become their own roots
                    # in the same run trace; the stop itself stays visible.
                    trace_recorder.end(
                        trace_recorder.shutdown_handle,
                        "cancelled",
                        attributes={"runtime.reason_code": "cancel_requested"},
                    )

                supervisor_start_ns = time.monotonic_ns()
                try:
                    if supervisor is None and backend_task is not None:
                        if backend_task.done() and not backend_task.cancelled():
                            try:
                                supervisor = backend_task.result()
                            except Exception:  # noqa: BLE001
                                supervisor = None
                    if supervisor is not None:
                        # Detach only. The Worker keeps its loaded Runtime for the next Front.
                        stop = getattr(supervisor, "stop", None)
                        if callable(stop):
                            await stop()
                    elif backend_task is not None and not backend_task.done():
                        backend_task.cancel()
                    if watcher is not None and not watcher.done():
                        watcher.cancel()
                    pending = [
                        task
                        for task in (backend_task, watcher)
                        if task is not None and not task.done()
                    ]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                except BaseException:
                    _end_shutdown_step(
                        "shutdown.step.supervisor_stop",
                        supervisor_start_ns,
                        "error",
                    )
                    raise
                _end_shutdown_step(
                    "shutdown.step.supervisor_stop", supervisor_start_ns
                )

                front_stop_start_ns = time.monotonic_ns()
                try:
                    await asyncio.wait_for(
                        front.stop(), timeout=_FRONT_STOP_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[AgentServer] front stop exceeded %.1fs; continuing "
                        "shutdown",
                        _FRONT_STOP_TIMEOUT_SECONDS,
                    )
                    _end_shutdown_step(
                        "shutdown.step.front_stop",
                        front_stop_start_ns,
                        "error",
                    )
                except BaseException:
                    _end_shutdown_step(
                        "shutdown.step.front_stop",
                        front_stop_start_ns,
                        "error",
                    )
                    raise
                else:
                    _end_shutdown_step(
                        "shutdown.step.front_stop", front_stop_start_ns
                    )
            except BaseException:
                shutdown_outcome = "error"
                raise
            logger.info("[AgentServer] stopped")
    finally:
        if trace_recorder is not None:
            if not shutdown_entered:
                _close_startup_root(trace_recorder, startup_outcome, startup_reason)
            if shutdown_outcome == "error":
                final_outcome, final_reason = "error", None
            elif startup_outcome == "cancelled" or stop_signal:
                final_outcome, final_reason = "cancelled", "cancel_requested"
            elif startup_outcome == "error" and not shutdown_entered:
                final_outcome, final_reason = "error", startup_reason
            else:
                final_outcome, final_reason = "success", None
            try:
                from jiuwenswarm.runtime_trace.config import _DRAIN_CAP_SECONDS
                from jiuwenswarm.runtime_trace.lifecycle import (
                    shutdown_runtime_trace,
                )

                # Blocking here is intentional: at final process teardown a
                # cancellable await can be interrupted by a second Ctrl+C or
                # loop cancellation, leaving the shutdown root open and the
                # trace undrained. The call itself is bounded by the deadline.
                shutdown_runtime_trace(
                    deadline_mono=time.monotonic() + _DRAIN_CAP_SECONDS,
                    outcome=final_outcome,
                    reason_code=final_reason,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] runtime trace drain failed", exc_info=True
                )


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
    """决定 agentserver 的 bind host,兼顾单机版与沙箱一体机模式。

    优先级:
    1. ``AGENT_SERVER_HOST`` 环境变量(显式指定,含 127.0.0.1)——保持单机版与显式配置兼容;
    2. 沙箱环境(``JIUWENBOX_LISTEN`` 存在,表明处于 jiuwenbox 沙箱管控下)且 env 为空:
       先读 ``SANDBOX_IP`` 快照(jiuwenbox runtime 在 create/start 时注入,与对外暴露的
       ``ip_address`` 一致,免去运行时探测开销),缺失时再 ``_detect_sandbox_local_ip()``
       探测沙箱本地非 loopback IP,ISOLATED 模式拿到 veth 地址,外部可达;
    3. 其余(单机版 env 为空)回退 127.0.0.1,保持 ``os.getenv("AGENT_SERVER_HOST", "127.0.0.1")``
       的单机默认语义不变。
    """
    env_host = os.getenv("AGENT_SERVER_HOST", "").strip()
    if env_host:
        return env_host

    if os.getenv("JIUWENBOX_LISTEN"):
        sandbox_ip = os.getenv("SANDBOX_IP", "").strip()
        if sandbox_ip:
            logger.info(
                "[AgentServer] AGENT_SERVER_HOST unset in sandbox; "
                "using SANDBOX_IP snapshot: %s",
                sandbox_ip,
            )
            return sandbox_ip
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
    try:
        asyncio.run(_run(host=host, port=port))
    except KeyboardInterrupt:
        # Operator stop (Ctrl+C) is a normal shutdown, not a crash. Letting it
        # reach the crash excepthook would exit with CRASH_EXIT_CODE and make
        # the supervisor treat the stop as a crash.
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
