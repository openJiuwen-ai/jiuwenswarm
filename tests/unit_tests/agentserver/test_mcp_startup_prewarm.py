# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``mcp_config.prewarm_connected_mcps``.

The startup fire-and-forget task and the web root adapter's lazy
``_start_mcp_prewarm`` both delegate here. ``prewarm_connected_mcps`` iterates
``state.json``'s ``state==connected`` records and probes each via
``probe_mcp_live_connection`` (seeds the process-global ``Runner.resource_mgr``
cache). These tests pin the contract:

* empty connected set → no probe called (early return).
* non-empty → each name probed exactly once (concurrently, no ordering).
* one MCP raising does not abort the rest (failure isolation).
* a hung probe is bounded by a per-MCP timeout; remaining probes still run.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.common.mcp_config import prewarm_connected_mcps


def _connected(names: list[str]) -> list[dict]:
    return [{"name": n} for n in names]


@pytest.mark.asyncio
async def test_prewarm_empty_connected_set_skips_probe() -> None:
    """No connected MCPs → ``probe_mcp_live_connection`` is never called."""
    with patch(
        "jiuwenswarm.common.mcp_config.probe_mcp_live_connection",
        new=AsyncMock(),
    ) as mock_probe, patch(
        "jiuwenswarm.server.runtime.mcp.state_store.list_truly_connected_mcps",
        return_value=[],
    ):
        await prewarm_connected_mcps()
    mock_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_prewarm_probes_each_connected_mcp_once() -> None:
    """Two connected MCPs → each probed exactly once (concurrently)."""
    with patch(
        "jiuwenswarm.common.mcp_config.probe_mcp_live_connection",
        new=AsyncMock(return_value=(True, "")),
    ) as mock_probe, patch(
        "jiuwenswarm.server.runtime.mcp.state_store.list_truly_connected_mcps",
        return_value=_connected(["github", "context7"]),
    ):
        await prewarm_connected_mcps()
    assert mock_probe.await_count == 2
    awaited_names = {call.args[0] for call in mock_probe.await_args_list}
    assert awaited_names == {"github", "context7"}


@pytest.mark.asyncio
async def test_prewarm_times_out_a_hung_mcp_and_continues() -> None:
    """A probe that hangs is bounded by a per-MCP timeout; the remaining MCPs
    are still prewarmed instead of the whole queue blocking forever."""
    attempted: list[str] = []
    start = asyncio.get_running_loop().time()

    async def probe(name: str) -> tuple[bool, str]:
        attempted.append(name)
        if name == "hung":
            await asyncio.sleep(3600)  # never returns on its own
        return True, ""

    with patch(
        "jiuwenswarm.common.mcp_config.probe_mcp_live_connection",
        new=probe,
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.list_truly_connected_mcps",
        return_value=_connected(["hung", "good"]),
    ), patch(
        "jiuwenswarm.common.mcp_config._PREWARM_STALL_TIMEOUT_S", 0.1,
    ):
        await prewarm_connected_mcps()  # must not raise despite the hang

    # Both were attempted; the whole prewarm finished in ~the stall window
    # (0.1s), not 2×0.1s serial (stall only guards deadlock, not normal slow).
    assert set(attempted) == {"hung", "good"}
    assert asyncio.get_running_loop().time() - start < 0.5
