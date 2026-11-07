# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.data``: the calling package's private data directory.

The directory shares the package store's lifecycle: it survives reload,
upgrade and restart, and is removed when the package is uninstalled.
Files a package places under ``public/`` can be addressed with
``ops.ui.asset_url(path, source="data")``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.events import Event, EventHandlerOps, EventHandlerOpsBuilder, EventNext

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime


def build_data_ops_build_handler(runtime: GatewayRuntime):
    """Return the ``ops.build`` BUILTIN that registers ``ops.data.root``."""

    async def data_root(ops: EventHandlerOps) -> Path:
        """Create and return the calling package's data directory.

        Like store calls, this is refused once the activation that issued
        ``ops`` has been released, and uninstall waits for it to finish.
        """
        package_id = ops.package_metadata.package_id if ops.package_metadata else ""
        if not package_id or not runtime.is_active_package(package_id):
            raise ValueError("ops.data requires an active Gateway package")
        with runtime.package_data_call(package_id, ops):
            directory = runtime.package_data_dir(package_id)
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            # Re-check after creation so a concurrent swap to a symlink is refused.
            return runtime.package_data_dir(package_id)

    async def data_ops_build(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        del ops
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("data")
        builder.add_method("data", "root", data_root)
        return builder

    return data_ops_build
