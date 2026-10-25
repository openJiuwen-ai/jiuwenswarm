"""Worker-owned, serial and observable refresh of published model snapshots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)
_FAILED = "部分同事配置未更新；请检查未发布修改或模型选择，原有版本仍保留。"


class ModelRefreshQueue:
    """Serialize worker-local publication and retain only the latest pending payload."""

    def __init__(self, refresh: Callable[[dict[str, Any] | None], Awaitable[bool]]) -> None:
        self._refresh = refresh
        self._task: asyncio.Task[None] | None = None
        self._pending: tuple[int, dict[str, Any] | None] | None = None
        self._latest_payload: dict[str, Any] | None = None
        self._generation = 0
        self._state: dict[str, Any] = {"status": "applied", "generation": 0}
        self._closed = False

    def status(self) -> dict[str, Any]:
        return dict(self._state)

    def schedule(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("model refresh queue is closed")
        if payload is not None:
            self._latest_payload = deepcopy(payload)
        self._generation += 1
        # A bare reload must not erase a saved configuration that has not yet
        # reached the service (including after a failed refresh).
        self._pending = self._generation, deepcopy(self._latest_payload)
        self._state = {"status": "pending", "generation": self._generation}
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="jiuwenmate-model-refresh")
        return self.status()

    async def _run(self) -> None:
        while self._pending is not None:
            generation, payload = self._pending
            self._pending = None
            try:
                applied = await self._refresh(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Configuration and exception messages can contain credentials.
                logger.warning("[JiuwenMate] background model refresh failed: error_type=%s", type(exc).__name__)
                applied = False
            if generation == self._generation:
                self._state = {"status": "applied" if applied else "failed", "generation": generation}
                if not applied:
                    self._state["detail"] = _FAILED

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def aclose(self) -> None:
        # Publication runs in a thread: cancelling its await would not stop the
        # database write. Drain it before this reusable adapter can start again.
        self._closed = True
        try:
            await self.wait()
        finally:
            self._closed = False
