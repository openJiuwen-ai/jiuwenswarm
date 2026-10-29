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
    _start_profile_prewarm(runtime)
    measurements.stage("runtime_host_installed")
    return runtime


def _start_profile_prewarm(runtime: WorkerRuntime) -> None:
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.worker.profile_prewarm import ProfilePrewarmer

    preparation = getattr(runtime.server, "_preparation_registry", None)
    if preparation is None:
        return
    manager = runtime.server.get_agent_manager()

    async def prepare(mode: str, channel_id: str, config: dict) -> Any:
        import sys
        from jiuwenswarm.server.worker.profile_prewarm import prewarm_checkpoint

        await prewarm_checkpoint()
        if getattr(sys, "frozen", False):
            # Frozen uses idle preparation, never Source's parallel SDK strategy.
            # The accepted import can finish; the next checkpoint yields to input.
            from jiuwenswarm.server.worker.import_preparation import ModuleImportTask

            if runtime.idle_import is None or runtime.idle_import.failed:
                runtime.idle_import = ModuleImportTask(("trafilatura.settings",), name="idle-trafilatura")
                runtime.idle_import.start()
            await runtime.idle_import.wait()
            await prewarm_checkpoint()
        manager = runtime.server.get_agent_manager()
        agent = await manager.get_agent(channel_id=channel_id, mode=mode,
                                        sub_mode="normal" if mode == "code" else None)
        if agent is None:
            raise RuntimeError("profile does not support model preparation")
        await prewarm_checkpoint()
        return await agent.prepare_kernel(mode=mode, config_base=config)

    runtime.profile_prewarmer = ProfilePrewarmer(
        get_config=get_config, prepare=prepare, admission=preparation,
        stop_event=runtime.stop_event,
    )
    # Runtime owns the full foreground turn. Pause this optional profile with
    # a token distinct from startup priority released at the actual SDK entry.
    manager.set_preparation_admission(runtime.profile_prewarmer)
    runtime.server._profile_prewarmer = runtime.profile_prewarmer
    try:
        runtime.profile_prewarmer.start()
    except Exception as exc:  # noqa: BLE001 - optional preparation cannot fail required startup
        logger.warning("[Worker] profile prewarm not started: %s", type(exc).__name__)
