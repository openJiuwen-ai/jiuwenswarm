# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Finite optional import tasks owned by the Worker lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time

logger = logging.getLogger(__name__)


class ModuleImportTask:
    """One daemon with a finite allowlist and cooperative per-import shutdown."""

    def __init__(self, modules: tuple[str, ...], *, name: str) -> None:
        self._modules = modules
        self._name = name
        self._stop = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.completed: list[str] = []
        self.failed: list[str] = []

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        began = time.monotonic()
        try:
            for module in self._modules:
                if self._stop.is_set():
                    break
                try:
                    importlib.import_module(module)
                except BaseException as error:  # optional import must not terminate its owner
                    self.failed.append(module)
                    logger.info("[Worker] import_preparation module=%s result=%s", module, type(error).__name__)
                else:
                    self.completed.append(module)
        finally:
            self._done.set()
            logger.info("[Worker] preparation stage=%s duration_ms=%.3f completed=%d failed=%d",
                        self._name, (time.monotonic() - began) * 1000, len(self.completed), len(self.failed))

    async def wait(self) -> None:
        while not self._done.is_set():
            await asyncio.sleep(.005)
        if self.failed or len(self.completed) != len(self._modules):
            raise ImportError("Optional module preparation did not finish")

    async def close(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        deadline = time.monotonic() + timeout
        while self._thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        if self._thread.is_alive():
            # Python cannot preempt a synchronous import. No next module starts;
            # the enclosing Worker shutdown still owns its process deadline.
            raise TimeoutError("Optional import still running at Worker shutdown")
