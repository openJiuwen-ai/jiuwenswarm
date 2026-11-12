# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser invalidation shared by Web and local CLI package management."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)


async def notify_installed(runtime: GatewayRuntime, package_ids: Sequence[str]) -> bool:
    """Ask mounted slots for new views after released views have been cleared.

    A delivery failure is distinct from the already committed package result.
    """
    if not package_ids:
        return True
    await runtime.browser.settle()
    try:
        # The browser must know the new activation before it renders again; otherwise the
        # first frame of a re-enabled package would be rejected as undeclared (S_06).
        await runtime.browser.publish_declarations()
        await runtime.channel.broadcast_event("extensions.v2.installed", {"package_ids": list(package_ids)})
    except Exception:  # noqa: BLE001 - installation has already committed
        logger.warning("[GatewayExtensions] installed packages could not notify browsers", exc_info=True)
        return False
    return True


async def notify_disabled(runtime: GatewayRuntime, package_ids: Sequence[str]) -> bool:
    """Withdraw declarations after released views have been cleared by the browser.

    Publishing before the management response lands keeps the browser's request
    bindings and the runtime activation in step, so the next request is not judged
    against stale capabilities.
    """
    if not package_ids:
        return True
    await runtime.browser.settle()
    try:
        await runtime.browser.publish_declarations()
    except Exception:  # noqa: BLE001 - the package result has already committed
        logger.warning("[GatewayExtensions] released packages could not notify browsers", exc_info=True)
        return False
    return True
