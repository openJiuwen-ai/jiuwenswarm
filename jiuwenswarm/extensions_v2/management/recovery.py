# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read current package facts after a management operation lost its Report."""

from __future__ import annotations

import logging

from openjiuwen.core.extension.management import QueryResult

from jiuwenswarm.extensions_v2.management.registry import ExtensionManager

logger = logging.getLogger(__name__)


async def query_after_error(
    manager: ExtensionManager, package_id: str | None = None,
) -> tuple[QueryResult | None, str | None]:
    """Query once without retrying the write or replacing its original error."""
    try:
        return await manager.query(package_id), None
    except Exception:  # noqa: BLE001 - a query failure does not prove the package is inactive
        logger.exception("[ExtensionsV2] state query failed after management error")
        return None, "Current package state could not be confirmed"
