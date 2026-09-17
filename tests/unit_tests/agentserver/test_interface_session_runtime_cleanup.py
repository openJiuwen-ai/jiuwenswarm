# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for facade-owned session runtime teardown."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


class _CleanupAdapter:
    def __init__(self) -> None:
        self.cleaned_sessions: list[str] = []
        self.active_sessions: set[str] = set()

    async def cleanup_session_adapter(self, session_id: str) -> bool:
        self.cleaned_sessions.append(session_id)
        self.active_sessions.discard(session_id)
        return True

    def has_session_runtime(self) -> bool:
        return bool(self.active_sessions)


async def _submit_quick_task(swarm: JiuWenSwarm, session_id: str) -> None:
    completed = asyncio.Event()

    async def quick_task() -> None:
        completed.set()

    await swarm._session_manager.submit_task(session_id, quick_task)
    await asyncio.wait_for(completed.wait(), timeout=1)
    for _ in range(100):
        if swarm._session_manager.get_current_task(session_id) is None:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"session did not become idle: {session_id}")


@pytest.mark.asyncio
async def test_cleanup_session_runtime_closes_processor_and_adapter() -> None:
    swarm = JiuWenSwarm()
    adapter = _CleanupAdapter()
    swarm._adapter = adapter
    adapter.active_sessions.add("tui_exit")
    await _submit_quick_task(swarm, "tui_exit")

    try:
        assert swarm.has_session_runtime() is True
        assert await swarm.cleanup_session_runtime("tui_exit") is True
        assert adapter.cleaned_sessions == ["tui_exit"]
        assert swarm._session_manager.has_active_processor("tui_exit") is False
        assert swarm.has_session_runtime() is False
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_cleanup_session_runtime_closes_processor_without_adapter() -> None:
    swarm = JiuWenSwarm()
    await _submit_quick_task(swarm, "tui_no_adapter")

    try:
        assert await swarm.cleanup_session_runtime("tui_no_adapter") is True
        assert swarm._session_manager.has_active_processor("tui_no_adapter") is False
    finally:
        await swarm._session_manager.close_all_sessions()


@pytest.mark.asyncio
async def test_cleanup_closes_all_session_processors() -> None:
    swarm = JiuWenSwarm()
    await _submit_quick_task(swarm, "tui_one")
    await _submit_quick_task(swarm, "tui_two")

    try:
        await swarm.cleanup()

        assert swarm._session_manager.has_active_processor("tui_one") is False
        assert swarm._session_manager.has_active_processor("tui_two") is False
    finally:
        await swarm._session_manager.close_all_sessions()
