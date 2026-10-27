# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Correctness tests for the OfficeClaw MCP schema in-process cache.

These do NOT spawn a real Node process; they monkeypatch the uncached
discovery so the cache logic (key, single-flight, deepcopy, entry cap)
is exercised deterministically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import mcp_config


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


@pytest.fixture()
def bundle_params(tmp_path: Path) -> dict:
    """Params backed by a real temp bundle so the fingerprint is non-empty.

    Mirrors the deployed layout: a thin entry shell next to the toolset
    module that actually defines the tool schemas.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("shell v1")
    (dist / "server-toolsets.js").write_text("toolset v1")
    return {
        "command": "node",
        "args": [str(dist / "index.js")],
        "cwd": str(tmp_path),
        "env": {"OFFICE_CLAW_MCP_EXCLUDED_TOOLS": "office_claw_list_tasks"},
    }


@pytest.mark.asyncio
async def test_second_call_hits_cache_and_returns_equal_schema(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
) -> None:
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    first = await mcp_config.list_office_claw_mcp_tools(bundle_params)
    second = await mcp_config.list_office_claw_mcp_tools(bundle_params)

    assert calls == 1, "second call must hit the cache, not re-discover"
    assert first == second == [_make_tool("office_claw_post_message")]


@pytest.mark.asyncio
async def test_returned_schema_is_deep_copied_so_callers_cannot_mutate_cache(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
) -> None:
    monkeypatch.setattr(
        mcp_config,
        "_list_office_claw_mcp_tools_uncached",
        AsyncMock(return_value=[_make_tool("office_claw_post_message")]),
    )

    first = await mcp_config.list_office_claw_mcp_tools(bundle_params)
    first[0]["name"] = "MUTATED"

    second = await mcp_config.list_office_claw_mcp_tools(bundle_params)
    assert second[0]["name"] == "office_claw_post_message", (
        "cache must not be mutated by callers"
    )


@pytest.mark.asyncio
async def test_invalidate_forces_rediscovery(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
) -> None:
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool(f"tool_{calls}")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    await mcp_config.list_office_claw_mcp_tools(bundle_params)
    await mcp_config.list_office_claw_mcp_tools(bundle_params)
    assert calls == 1

    mcp_config.invalidate_office_claw_mcp_schema_cache()
    await mcp_config.list_office_claw_mcp_tools(bundle_params)
    assert calls == 2, "invalidate must drop the cache and force a fresh discovery"


@pytest.mark.asyncio
async def test_disabled_flag_falls_back_to_uncached(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_MCP_SCHEMA_CACHE", "0")
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    await mcp_config.list_office_claw_mcp_tools(bundle_params)
    await mcp_config.list_office_claw_mcp_tools(bundle_params)
    assert calls == 2, "with cache disabled every call must discover"


@pytest.mark.asyncio
async def test_concurrent_discovery_coalesces_into_one_inflight_task(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
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

    t1 = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(bundle_params))
    await started.wait()
    t2 = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(bundle_params))

    await asyncio.sleep(0)  # let t2 observe in-flight task
    releases.set()

    r1, r2 = await asyncio.gather(t1, t2)
    assert calls == 1, "concurrent identical discovery must coalesce into one task"
    assert r1 == r2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_evict_shared_inflight_task(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
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

    first = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(bundle_params))
    await started.wait()  # producer discovery is in-flight

    # a second waiter joins the same in-flight task
    second = asyncio.create_task(mcp_config.list_office_claw_mcp_tools(bundle_params))
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
    assert fp1 is not None
    assert len(fp1) == 1, "relative bundle must be found via params cwd"
    assert fp1[0]["path"].replace("\\", "/").endswith("dist/collab.js")

    # rebuild the bundle with different content -> fingerprint must change
    bundle.write_text("v2-with-more-bytes-so-size-differs")
    fp2 = mcp_config._office_claw_mcp_build_fingerprint(params)
    assert fp2 != fp1, (
        "bundle rebuild must rotate the fingerprint (and thus the cache key)"
    )


def test_bundle_tree_change_rotates_key_without_entry_change(
    tmp_path,
) -> None:
    """A rebuild that only touches the entry's imported sibling must rotate the key.

    The entry file (index.js) is a thin shell; the toolset module next to it
    defines the schemas. The fingerprint covers the whole owning directory so
    a sibling-only rebuild cannot keep serving a stale cached schema — the
    exact blind spot the old revision-based invalidation used to cover by
    accident (by wiping on every catalog change).
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    entry = dist / "index.js"
    entry.write_text("shell v1")
    (dist / "server-toolsets.js").write_text("toolset v1")

    params = {
        "command": "node",
        "args": [str(entry)],
        "cwd": str(tmp_path),
        "env": {},
    }
    key1 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key1 is not None

    (dist / "server-toolsets.js").write_text("toolset v2-with-more-bytes")
    key2 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key2 is not None
    assert key2 != key1, "sibling-only rebuild must rotate the cache key"

    # node_modules and source maps stay out of the fingerprint
    (dist / "node_modules").mkdir()
    (dist / "node_modules" / "dep.js").write_text("dep")
    (dist / "extras.js.map").write_text("{}")
    key3 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key3 == key2, "node_modules / *.map must not affect the fingerprint"


@pytest.mark.asyncio
async def test_remote_config_without_local_files_is_never_cached(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No local bundle files (remote-style config) -> always uncached.

    A remote MCP schema can change server-side at any time and a key without
    local files would never rotate, so the wrapper must bypass the cache
    entirely instead of serving a possibly stale entry forever.
    """
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool("office_claw_post_message")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    params = {
        "command": "node",
        "args": ["/nonexistent/bundle.js"],
        "cwd": str(tmp_path),
        "env": {},
    }
    await mcp_config.list_office_claw_mcp_tools(params)
    await mcp_config.list_office_claw_mcp_tools(params)
    assert calls == 2, "remote-style configs must not be cached"
    assert len(mcp_config._office_claw_mcp_schema_cache) == 0


@pytest.mark.asyncio
async def test_capacity_bound_clears_orphaned_entries(
    monkeypatch: pytest.MonkeyPatch,
    bundle_params: dict,
) -> None:
    """Entries beyond the cap are dropped instead of accumulating forever.

    Orphaned keys (nobody computes them anymore after a bundle upgrade) are
    bounded by the entry cap; clearing on overflow costs one re-discovery.
    """
    monkeypatch.setattr(mcp_config, "_OFFICE_CLAW_MCP_SCHEMA_CACHE_MAX_ENTRIES", 2)
    calls = 0

    async def _discover(params):
        nonlocal calls
        calls += 1
        return [_make_tool(f"tool_{calls}")]

    monkeypatch.setattr(mcp_config, "_list_office_claw_mcp_tools_uncached", _discover)

    variants = []
    for index in range(3):
        params = dict(bundle_params)
        params["env"] = {"OFFICE_CLAW_MCP_EXCLUDED_TOOLS": f"office_claw_tool_{index}"}
        variants.append(params)
        await mcp_config.list_office_claw_mcp_tools(params)

    assert len(mcp_config._office_claw_mcp_schema_cache) == 1, (
        "third insert must clear the overflowed cache and keep only the new entry"
    )
    # the cleared entries are gone: a repeat of the first variant re-discovers
    await mcp_config.list_office_claw_mcp_tools(variants[0])
    assert calls == 4, "evicted entry must re-discover"


def test_declared_fingerprint_roots_extend_coverage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JIUWENSWARM_MCP_BUNDLE_FINGERPRINT_ROOTS folds extra directories into the key.

    For layouts that keep schema-defining code outside the entry's owning
    directory (see the review on !9045): files under a declared root rotate
    the cache key even though the entry tree itself is unchanged; without
    the declaration they stay invisible to the fingerprint.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    entry = dist / "index.js"
    entry.write_text("shell v1")

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "toolsets.js").write_text("toolset v1")

    params = {
        "command": "node",
        "args": [str(entry)],
        "cwd": str(tmp_path),
        "env": {},
    }

    # undeclared: changes under shared/ do not affect the key
    key1 = mcp_config._office_claw_mcp_schema_cache_key(params)
    (shared / "toolsets.js").write_text("toolset v2-with-more-bytes")
    key2 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key1 == key2, "undeclared external tree must not affect the key"

    # declared: the root joins the fingerprint and its changes rotate the key
    monkeypatch.setenv("JIUWENSWARM_MCP_BUNDLE_FINGERPRINT_ROOTS", "shared")
    key3 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key3 is not None and key3 != key2, (
        "declared root must join the fingerprint"
    )
    (shared / "toolsets.js").write_text("toolset v3-with-even-more-bytes")
    key4 = mcp_config._office_claw_mcp_schema_cache_key(params)
    assert key4 is not None and key4 != key3, (
        "file change under a declared root must rotate the cache key"
    )
