# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose a three-level client API over the public two-level Core Ops API."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.events import EventHandlerOps


class LocalStorageOperations:
    """Queue operations for the calling package and browser only."""

    def __init__(self, ops: EventHandlerOps) -> None:
        self._ops = ops

    async def set(self, key: str, value: Any) -> dict:
        """Queue one browser localStorage write without waiting for a receipt."""
        return await self._ops.client.local_storage_set(key, value)

    async def delete(self, key: str) -> dict:
        """Queue deletion of the execution-time current value."""
        return await self._ops.client.local_storage_delete(key)


class PageOperations:
    """Queue explicit page lifecycle commands for the calling browser."""

    def __init__(self, ops: EventHandlerOps) -> None:
        self._ops = ops

    async def reload(self) -> dict:
        """Queue a page reload without waiting for a browser receipt."""
        return await self._ops.client.page_reload()


class BrowserClientOperations:
    """Browser capability namespaces exposed to a package."""

    def __init__(self, ops: EventHandlerOps) -> None:
        self.local_storage = LocalStorageOperations(ops)
        self.page = PageOperations(ops)


class GatewayHandlerOperations:
    """Delegate public Core operations and add the browser client facade."""

    def __init__(self, ops: EventHandlerOps) -> None:
        self._ops = ops
        self.client = BrowserClientOperations(ops)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ops, name)


def with_client_ops(ops: EventHandlerOps) -> GatewayHandlerOperations:
    """Return a composition wrapper without modifying the Core Ops object."""
    return GatewayHandlerOperations(ops)
