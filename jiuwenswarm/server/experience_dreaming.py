"""AgentServer ownership of shared experience Dreaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import (
    build_experience_dreaming_provider,
)

logger = logging.getLogger(__name__)
_EXPERIENCE_INTERVAL = 8 * 60 * 60
_STOP_TIMEOUT = 10.0


class AgentMemoryExperienceDreamingService:
    """Keep the scheduler runtime alive for the lifetime of AgentServer."""

    def __init__(self, has_running_work: Callable[[], bool]) -> None:
        self._has_running_work = has_running_work
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="experience_dreaming")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if task is None:
            return
        _, pending = await asyncio.wait({task}, timeout=_STOP_TIMEOUT)
        if pending:
            logger.warning("Experience Dreaming stop timed out; cancelling the background task")
            task.cancel()
        elif not task.cancelled():
            task.result()

    async def _run(self) -> None:
        server_loop = asyncio.get_running_loop()

        async def is_idle() -> bool:
            return not self._stopping.is_set() and not self._has_running_work()

        async def ready() -> bool:
            if asyncio.get_running_loop() is server_loop:
                return await is_idle()
            result = asyncio.run_coroutine_threadsafe(is_idle(), server_loop)
            return await asyncio.wait_for(asyncio.wrap_future(result), timeout=2)

        provider = None
        scope = None
        delay = 1
        try:
            while not self._stopping.is_set():
                try:
                    if provider is None:
                        built = build_experience_dreaming_provider()
                        if built is None:
                            return
                        provider, scope = built
                    if not provider.is_initialized:
                        await provider.initialize(
                            user_id=scope["user"],
                            scope_id=scope["org"],
                            agent_id=scope["agent"],
                            register_experience_jobs=False,
                        )
                    job_id = await provider.ensure_experience_dreaming(
                        interval=_EXPERIENCE_INTERVAL,
                        ready=ready,
                    )
                    logger.info("Experience Dreaming ready: job_id=%s", job_id)
                    await self._stopping.wait()
                    return
                except Exception as exc:
                    logger.warning(
                        "Experience Dreaming startup will retry: error_type=%s delay=%s",
                        type(exc).__name__, delay,
                    )
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    except TimeoutError:
                        delay = min(30, delay * 2)
        finally:
            if provider is not None:
                try:
                    await provider.shutdown()
                except Exception as exc:
                    logger.warning("Experience Dreaming shutdown failed: %s", type(exc).__name__)
