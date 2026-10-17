# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker startup measurements and process-local observability bindings."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class StartupTelemetry:
    """Measure phases without owning Runtime readiness or execution tasks."""

    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stage(self, name: str) -> None:
        logger.info("[Worker] startup stage=%s run_elapsed=%.2fs", name, self.elapsed)

    @asynccontextmanager
    async def monitor(self) -> AsyncIterator[None]:
        from jiuwenswarm.server.worker.preparation import log_event_loop_lag

        task = asyncio.create_task(
            log_event_loop_lag("runtime_preparation"), name="runtime-preparation-loop-lag",
        )
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def install_runtime_observability(server: object) -> None:
    from openjiuwen.harness.observability import install_subagent_observability_hook
    from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

    install_subagent_observability_hook()
    send_push = getattr(server, "send_push", None)
    if send_push is not None:
        trajectory_gateway_hint_bridge.bind(asyncio.get_running_loop(), send_push)


async def unbind_runtime_observability() -> None:
    try:
        from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

        await trajectory_gateway_hint_bridge.unbind()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Worker] trajectory hint bridge unbind failed: %s", exc)


def shutdown_runtime_observability() -> None:
    try:
        from jiuwenswarm.agents.harness.team.team_manager import shutdown_team_observability

        shutdown_team_observability()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Worker] team observability shutdown failed: %s", exc)
    try:
        from jiuwenswarm.agents.harness.agent_observability import shutdown_agent_observability

        shutdown_agent_observability()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Worker] agent observability shutdown failed: %s", exc)
