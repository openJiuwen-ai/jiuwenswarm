# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coordinate Worker startup phases without owning their implementations.

This entry stays lightweight; SDK and Runtime imports occur only after IPC is
bound. Constructed Runtime resources have one owner, including failed startup.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.server.worker import capabilities, runtime_backend, runtime_bootstrap, telemetry
from jiuwenswarm.server.worker.runtime_host import WorkerRuntime
from jiuwenswarm.server.worker.signals import RuntimeSignalSink

logger = logging.getLogger(__name__)
_STARTUP_FAILURE_CLEANUP_TIMEOUT = 5.0


async def _cold_load_logging_levels() -> None:
    """企业版：worker 进程冷加载 Manager 下发的日志级别。

    与 front（``app_agentserver``）对齐——worker 是独立解释器进程，模块级
    ``setup_logger()`` 只给了 config.yaml 默认级别，而运行时日志主力（agent
    适配器 / harness）都由本进程输出；不冷加载则 Manager 下发的 DB 行对
    worker 永不生效。必须在 ``preload_runtime_backend``（含 SDK imports 与
    OpenJiuwen 日志装配）之前应用。失败只告警，保持 yaml 默认，不阻断启动。
    """
    try:
        from jiuwenswarm.enterprise import is_enterprise

        if not is_enterprise():
            return
        from jiuwenswarm.enterprise.capabilities.logging import (
            reload_logging_levels_from_gateway_db,
        )

        await reload_logging_levels_from_gateway_db()
    except Exception:  # noqa: BLE001
        logger.warning("[Worker] logging_config cold load skipped", exc_info=True)


async def start_worker_runtime(
    host: str, port: int, signals: RuntimeSignalSink, *, on_constructed: Any = None,
) -> WorkerRuntime:
    """Run measured preparation; signals alone determine chat readiness."""
    measurements = telemetry.StartupTelemetry()
    local_owner: WorkerRuntime | None = None

    async def retain(server: object) -> WorkerRuntime:
        nonlocal local_owner
        local_owner = WorkerRuntime(server=server, stop_event=asyncio.Event())
        return local_owner

    try:
        async with measurements.monitor():
            return await _start_worker_runtime(
                host, port, signals, on_constructed=on_constructed or retain, measurements=measurements,
            )
    except BaseException:
        # The resident service cleans up any server handed to its callback.
        # Standalone callers have no service owner after a failed startup.
        if local_owner is not None:
            try:
                await _cleanup_failed_startup(local_owner)
            except BaseException as exc:
                logger.warning("[Worker] failed startup cleanup failed: %s", exc)
        raise


def _log_late_startup_cleanup(task: asyncio.Task) -> None:
    """Consume a cleanup that outlived the failed startup's grace period."""
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.warning("[Worker] late startup cleanup failed: %s", error)


async def _cleanup_failed_startup(runtime: WorkerRuntime) -> None:
    """Preserve the startup error while giving partial resources bounded cleanup."""
    owner = getattr(runtime.server, "worker_callback_registry_owner", None)
    cleanup = asyncio.create_task(runtime.shutdown())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_FAILURE_CLEANUP_TIMEOUT
    try:
        while not cleanup.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Worker failed startup cleanup exceeded its grace period")
            try:
                await asyncio.wait({cleanup}, timeout=remaining)
            except asyncio.CancelledError:
                # The outer handler re-raises the original startup exception,
                # including its first cancellation, after this bounded join.
                continue
        cleanup.result()
    finally:
        if not cleanup.done():
            cleanup.cancel()
            cleanup.add_done_callback(_log_late_startup_cleanup)
        if owner is not None:
            owner.close()


async def _start_worker_runtime(
    host: str, port: int, signals: RuntimeSignalSink, *, on_constructed: Any = None,
    measurements: telemetry.StartupTelemetry | None = None,
) -> WorkerRuntime:
    measurements = measurements or telemetry.StartupTelemetry()
    await _cold_load_logging_levels()
    await asyncio.to_thread(runtime_backend.preload_runtime_backend)
    await asyncio.sleep(0)
    measurements.stage("runtime_imports_ready")
    runtime: WorkerRuntime | None = None

    async def retain_constructed(server: object) -> None:
        nonlocal runtime
        owner = await on_constructed(server) if on_constructed is not None else None
        # The service publishes the same owner before preparation exceptions.
        # Every later capability task attaches to it rather than a replacement.
        runtime = owner if isinstance(owner, WorkerRuntime) else WorkerRuntime(
            server=server, stop_event=asyncio.Event(),
        )

    server = await runtime_bootstrap.construct_runtime_server(
        host, port, on_constructed=retain_constructed, telemetry=measurements,
    )
    if runtime is None:
        raise RuntimeError("Runtime construction did not publish its cleanup owner")
    runtime_bootstrap.bind_runtime_signals(server, signals)
    await server.start(bind_transport=False)
    measurements.stage("runtime_started")
    telemetry.install_runtime_observability(server)
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import (
        install_session_token_usage_subagent_hook,
    )

    install_session_token_usage_subagent_hook()
    measurements.stage("observability_installed")
    await capabilities.start_optional_preparation(runtime, signals, measurements)
    measurements.stage("runtime_host_installed")
    from jiuwenswarm.server.agent_http_server import start_http_if_enabled

    try:
        app_services = getattr(server, "app_services", None)
        if app_services is not None:
            await start_http_if_enabled(app_services)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AgentHTTPServer] start failed: %s", exc)
    return runtime
