# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the root adapter's on-demand DeepAgent construction."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_adapter(session_id: str | None) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter carrying only the lazy-build bookkeeping state."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    adapter._is_session_scoped_adapter = session_id is not None
    adapter._parent_session_id = session_id
    adapter._root_instance_requested = False
    adapter._root_instance_lock = None
    adapter._session_instance_config = {"agent_name": "main_agent"}
    adapter._session_instance_mode = "agent"
    adapter._session_instance_sub_mode = None
    return adapter


def _stub_create_instance(adapter: JiuWenSwarmDeepAdapter, calls: list[str]) -> None:
    """Replace ``create_instance`` with a slow builder that records its calls."""

    async def _create_instance(config=None, *, mode="agent", sub_mode=None):
        calls.append(mode)
        # Yield control so a concurrent waiter can interleave if the lock is
        # missing; without an await the race would be untestable.
        await asyncio.sleep(0)
        adapter._instance = object()

    adapter.create_instance = _create_instance


def test_root_adapter_skips_its_own_instance_build() -> None:
    """A root adapter defers building until someone actually asks for a handle."""
    adapter = _make_adapter(None)

    assert adapter._skip_own_instance_build() is True


def test_session_adapter_always_builds_its_instance() -> None:
    """Session adapters own the live DeepAgent, so they never defer."""
    adapter = _make_adapter("sess_a")

    assert adapter._skip_own_instance_build() is False


def test_root_adapter_stops_skipping_once_requested() -> None:
    """After a build is requested the root adapter builds eagerly on rebuilds."""
    adapter = _make_adapter(None)
    adapter._root_instance_requested = True

    assert adapter._skip_own_instance_build() is False


@pytest.mark.asyncio
async def test_ensure_instance_builds_the_root_agent_once() -> None:
    """The first call builds; later calls hand back the same instance."""
    adapter = _make_adapter(None)
    calls: list[str] = []
    _stub_create_instance(adapter, calls)

    first = await adapter.ensure_instance()
    second = await adapter.ensure_instance()

    assert first is second
    assert calls == ["agent"]


@pytest.mark.asyncio
async def test_concurrent_ensure_instance_builds_only_once() -> None:
    """Parallel callers must not each build a DeepAgent.

    Two builds would register two sets of tools under the same owner id, which
    is exactly the duplicate-registration state the owner scoping prevents.
    """
    adapter = _make_adapter(None)
    calls: list[str] = []
    _stub_create_instance(adapter, calls)

    results = await asyncio.gather(*(adapter.ensure_instance() for _ in range(5)))

    assert len(calls) == 1
    assert len({id(item) for item in results}) == 1


@pytest.mark.asyncio
async def test_ensure_instance_returns_existing_instance_without_building() -> None:
    """A session adapter already holds an instance, so nothing is rebuilt."""
    adapter = _make_adapter("sess_a")
    existing = object()
    adapter._instance = existing
    calls: list[str] = []
    _stub_create_instance(adapter, calls)

    assert await adapter.ensure_instance() is existing
    assert calls == []


@pytest.mark.asyncio
async def test_ensure_instance_preserves_the_configured_mode() -> None:
    """The deferred build must reuse the mode create_instance was called with."""
    adapter = _make_adapter(None)
    adapter._session_instance_mode = "code"
    adapter._session_instance_sub_mode = "plan"
    calls: list[str] = []
    _stub_create_instance(adapter, calls)

    await adapter.ensure_instance()

    assert calls == ["code"]
