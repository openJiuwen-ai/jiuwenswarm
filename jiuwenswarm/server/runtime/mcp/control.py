"""Coordination helpers for MCP lifecycle mutations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


@asynccontextmanager
async def mcp_change_guard(manager: Any, name: str) -> AsyncIterator[None]:
    """Use the manager-owned per-connector lock when available."""

    guard = getattr(manager, "mcp_change_guard", None)
    if not callable(guard):
        yield
        return
    async with guard(name):
        yield
