# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise package store CRUD using the host's existing usage event."""

import asyncio
import logging

from openjiuwen.core.events import Event, EventHandlerOps, EventNext
from openjiuwen.core.extension import ExtensionRegistrar

logger = logging.getLogger("jiuwenswarm.server.extensions.store_probe")


class StoreProbe:
    """Keep a persistent counter and log each store method's actual result."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.register_event_handler("context.usage.updated", self.on_usage, {})

    async def on_usage(self, event: Event, ops: EventHandlerOps, next_handler: EventNext) -> object:
        async with self._lock:
            await self.probe(ops)
        return await next_handler(event)

    async def probe(self, ops: EventHandlerOps) -> None:
        package_id = await ops.package.name()
        previous = await ops.store.get("counter", 0)
        count = previous + 1
        saved = await ops.store.set("counter", count)
        logger.info("[store-probe] set package=%s previous=%s current=%s saved=%s", package_id, previous, count, saved)
        if not saved:
            return
        restored = await ops.store.get("counter")
        scratch_saved = await ops.store.set("scratch", {"temporary": True})
        keys = await ops.store.keys()
        deleted = await ops.store.delete("scratch")
        remaining = await ops.store.keys()
        logger.info(
            "[store-probe] get=%s keys=%s scratch_saved=%s delete=%s remaining=%s session=%s",
            restored, keys, scratch_saved, deleted, remaining,
            ops.session_context.session_id if ops.session_context else None,
        )


def create_extension() -> StoreProbe:
    return StoreProbe()
