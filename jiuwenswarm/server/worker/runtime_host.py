# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The resident Runtime's resource owner, including failed startup cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.server.worker import telemetry

logger = logging.getLogger(__name__)


@dataclass
class WorkerRuntime:
    """Own the constructed server and every Worker-created preparation task."""

    server: Any
    stop_event: asyncio.Event
    teammate_task: asyncio.Task | None = None
    zen_task: asyncio.Task | None = None
    proactive_task: asyncio.Task | None = None
    image_task: asyncio.Task | None = None
    background_tasks: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """Keep finite background notifications attached to this owner."""
        if self.stop_event.is_set():
            coroutine.close()
            raise RuntimeError("Worker Runtime is stopping")
        task = asyncio.create_task(coroutine, name=name)
        self.background_tasks.add(task)

        def completed(value: asyncio.Task) -> None:
            self.background_tasks.discard(value)
            if not value.cancelled():
                error = value.exception()
                if error is not None:
                    logger.warning("[Worker] background task %s failed: %s", value.get_name(), error)

        task.add_done_callback(completed)
        return task

    async def shutdown(self) -> None:
        """Drain resources within the enclosing Worker service's deadline."""
        try:
            await self._shutdown_owned_resources()
        finally:
            registry_owner = getattr(self.server, "worker_callback_registry_owner", None)
            if registry_owner is not None:
                await registry_owner.aclose()

    async def _shutdown_owned_resources(self) -> None:
        """Keep registry dispatch available until SDK teardown has settled."""
        self.stop_event.set()
        owned = (self.image_task, self.proactive_task, self.teammate_task, self.zen_task, *self.background_tasks)
        tasks = list(dict.fromkeys(task for task in owned if task is not None))
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[Worker] optional preparation stop failed: %s", result)
        await telemetry.unbind_runtime_observability()
        stop = getattr(self.server, "stop", None)
        if callable(stop):
            await stop()
        # A stopped server can still have queued persistence writes. Join both
        # stores before closing application owners or reporting shutdown done.
        from jiuwenswarm.server.runtime.session.session_history import flush_pending_writes as flush_history
        from jiuwenswarm.server.runtime.session.session_metadata import flush_pending_writes as flush_metadata

        flushed = await asyncio.gather(
            asyncio.to_thread(flush_history, 10.0), asyncio.to_thread(flush_metadata, 10.0),
        )
        if not all(flushed):
            raise RuntimeError("Worker shutdown persistence barrier did not complete")
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner import (
            get_kv_cache_application_owner,
        )

        await get_kv_cache_application_owner().close()
        telemetry.shutdown_runtime_observability()
        from jiuwenswarm.runtime.kernel import process_kernel

        await process_kernel().close()
