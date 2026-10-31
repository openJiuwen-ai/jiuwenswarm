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


async def start_worker_runtime(
    host: str, port: int, signals: RuntimeSignalSink, *, on_constructed: Any = None,
) -> WorkerRuntime:
    """Run measured preparation; signals alone determine chat readiness."""
    measurements = telemetry.StartupTelemetry()
    async with measurements.monitor():
        return await _start_worker_runtime(
            host, port, signals, on_constructed=on_constructed, measurements=measurements,
        )


async def _start_worker_runtime(
    host: str, port: int, signals: RuntimeSignalSink, *, on_constructed: Any = None,
    measurements: telemetry.StartupTelemetry | None = None,
) -> WorkerRuntime:
    measurements = measurements or telemetry.StartupTelemetry()
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
