# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Correctness tests for the OfficeClaw MCP schema in-process cache.

These do NOT spawn a real Node process; they monkeypatch the uncached
discovery so the cache logic (key, single-flight, deepcopy, generation
invalidation) is exercised deterministically.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import mcp_config

_PARAMS = {
    "command": "node",
    "args": ["/repo/packages/mcp-server/dist/collab.js"],
    "cwd": "/repo",
    "env": {"OFFICE_CLAW_MCP_EXCLUDED_TOOLS": "office_claw_list_tasks"},
}


def _make_tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"d-{name}",
        "input_params": {"type": "object"},
    }


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "1")
    mcp_config._clear_office_claw_mcp_schema_cache_for_tests()
    yield
    mcp_config._clear_office_claw_mcp_schema_cache_for_tests()


@pytest.mark.asyncio
async def test_second_call_hits_cache_and_returns_equal_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    first = await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    second = await mcp_config.list_office_claw_mcp_tools(_PARAMS)

    assert calls == 1, "second call must hit the cache, not re-discover"
    assert first == second == [_make_tool("office_claw_post_message")]


@pytest.mark.asyncio
async def test_returned_schema_is_deep_copied_so_callers_cannot_mutate_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(return_value=[_make_tool("office_claw_post_message")]),
    )

    first = await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    first[0]["name"] = "MUTATED"

    second = await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    assert second[0]["name"] == "office_claw_post_message", (
        "cache must not be mutated by callers"
    )


@pytest.mark.asyncio
async def test_invalidate_forces_rediscovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool(f"tool_{calls}")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    assert calls == 1

    mcp_config.invalidate_office_claw_mcp_schema_cache()
    await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    assert calls == 2, "invalidate must drop the cache and force a fresh discovery"


@pytest.mark.asyncio
async def test_disabled_flag_falls_back_to_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "0")
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    await mcp_config.list_office_claw_mcp_tools(_PARAMS)
    assert calls == 2, "with cache disabled every call must discover"


@pytest.mark.asyncio
async def test_concurrent_discovery_coalesces_into_one_inflight_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    releases = asyncio.Event()
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        started.set()
        await releases.wait()
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    t1 = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(_PARAMS))
    await started.wait()
    t2 = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(_PARAMS))

    await asyncio.sleep(0)  # let t2 observe in-flight task
    releases.set()

    r1, r2 = await asyncio.gather(t1, t2)
    assert calls == 1, "concurrent identical discovery must coalesce into one task"
    assert r1 == r2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_evict_shared_inflight_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled first waiter must not pop the in-flight task or skip the cache write.

    Regression for the single-flight bug where the owner's ``finally`` removed
    the still-running shared task on cancellation, so the next request spawned a
    second discovery and the orphaned result was never cached. The cache write
    and in-flight cleanup are now bound to the producer task's own completion.
    """
    started = asyncio.Event()
    releases = asyncio.Event()
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        started.set()
        await releases.wait()
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    first = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(_PARAMS))
    await started.wait()  # producer discovery is in-flight

    # a second waiter joins the same in-flight task
    second = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(_PARAMS))
    await asyncio.sleep(0)
    assert len(mcp_config._office_claw_mcp_schema_inflight) == 1

    first.cancel()  # cancel the first waiter
    with pytest.raises(asyncio.CancelledError):
        await first

    # the shared producer task must still be alive in the in-flight table
    assert len(mcp_config._office_claw_mcp_schema_inflight) == 1, (
        "cancelled waiter must not evict the shared in-flight task"
    )

    releases.set()
    result = await second
    assert result == [_make_tool("office_claw_post_message")]
    assert calls == 1, (
        "no second discovery should be spawned after a waiter is cancelled"
    )
    # the producer must have filled the cache despite the first waiter being cancelled
    assert len(mcp_config._office_claw_mcp_schema_cache) == 1, (
        "producer must write the cache even when its first waiter was cancelled"
    )


def test_build_fingerprint_resolves_relative_args_against_params_cwd(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative script paths are fingerprinted under params['cwd'], not the process cwd.

    Regression for the bug where ``resolve()`` used the sidecar process cwd,
    so a relative ``args=['dist/collab.js']`` was fingerprinted against the wrong
    base and a bundle rebuild did not rotate the cache key -> stale schema.
    """
    bundle = tmp_path / "dist" / "collab.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("v1")

    # point params cwd at tmp_path, keep process cwd elsewhere so a wrong base
    # would fail to stat the bundle
    monkeypatch.chdir(tmp_path.parent)
    params = {
        "command": "node",
        "args": ["dist/collab.js"],
        "cwd": str(tmp_path),
        "env": {},
    }

    fp1 = mcp_config._office_claw_mcp_build_fingerprint(params)
    assert len(fp1) == 1, "relative bundle must be found via params cwd"
    assert fp1[0]["path"].replace("\\", "/").endswith("dist/collab.js")

    # rebuild the bundle with different content -> fingerprint must change
    bundle.write_text("v2-with-more-bytes-so-size-differs")
    fp2 = mcp_config._office_claw_mcp_build_fingerprint(params)
    assert fp2 != fp1, (
        "bundle rebuild must rotate the fingerprint (and thus the cache key)"
    )
