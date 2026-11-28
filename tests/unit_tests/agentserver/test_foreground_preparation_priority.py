# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.worker.preparation import PreparationRegistry
from jiuwenswarm.server.worker.profile_prewarm import ProfilePrewarmer


def _manager(owner):
    manager = object.__new__(AgentManager)
    manager.warm_pool = SimpleNamespace(begin_foreground=AsyncMock(), end_foreground=AsyncMock())
    manager._foreground_preparation_tokens = []
    manager.set_preparation_admission(owner)
    return manager


async def test_sdk_entry_does_not_release_full_foreground_priority():
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    prepared = asyncio.Event()

    async def prepare(*_):
        prepared.set()

    controller = ProfilePrewarmer(
        get_config=lambda: {"runtime_prewarm": {"enabled": True, "idle_delay_seconds": 0}},
        prepare=prepare, admission=owner, stop_event=asyncio.Event(),
    )
    manager = _manager(controller)
    startup_token = owner.pause_background()
    try:
        await manager.begin_foreground_chat()
        controller.start()
        # The SDK sender releases startup admission, but a slow real turn is
        # still active and must prevent all subsequent background steps.
        owner.resume_background(startup_token)
        await asyncio.sleep(.03)
        assert not owner.background_paused  # SDK entry released startup priority.
        assert controller.foreground_paused
        assert not prepared.is_set()
        await manager.end_foreground_chat()
        await asyncio.wait_for(prepared.wait(), 2)
        assert not controller.foreground_paused
    finally:
        await controller.close()
        await owner.close()


async def test_concurrent_chats_keep_priority_until_last_completion():
    owner = PreparationRegistry()
    manager = _manager(owner)
    try:
        await asyncio.gather(manager.begin_foreground_chat(), manager.begin_foreground_chat())
        await manager.end_foreground_chat()
        assert owner.background_paused
        await manager.end_foreground_chat()
        assert not owner.background_paused
    finally:
        await owner.close()


@pytest.mark.parametrize("error", [RuntimeError("warm pool failure"), asyncio.CancelledError()])
async def test_failed_or_cancelled_foreground_begin_releases_its_token(error):
    owner = PreparationRegistry()
    manager = _manager(owner)
    manager.warm_pool.begin_foreground.side_effect = error
    try:
        with pytest.raises(type(error)):
            await manager.begin_foreground_chat()
        assert not owner.background_paused
        assert manager._foreground_preparation_tokens == []
    finally:
        await owner.close()


async def test_foreground_end_failure_still_releases_priority():
    owner = PreparationRegistry()
    manager = _manager(owner)
    manager.warm_pool.end_foreground.side_effect = RuntimeError("cleanup failure")
    try:
        await manager.begin_foreground_chat()
        with pytest.raises(RuntimeError, match="cleanup failure"):
            await manager.end_foreground_chat()
        assert not owner.background_paused
    finally:
        await owner.close()


async def test_disabled_profile_does_not_pause_other_first_use_capabilities():
    owner = PreparationRegistry()
    owner.register("first-use", lambda: "available", warm=False)
    owner.mark_minimum_ready()
    controller = ProfilePrewarmer(
        get_config=lambda: {"runtime_prewarm": {"enabled": False}},
        prepare=AsyncMock(), admission=owner, stop_event=asyncio.Event(),
    )
    manager = _manager(controller)
    try:
        await manager.begin_foreground_chat()
        controller.start()
        assert controller.foreground_paused
        assert controller._task is None
        assert not owner.background_paused
        assert await asyncio.wait_for(owner.ensure("first-use"), 1) == "available"
        await manager.end_foreground_chat()
    finally:
        await controller.close()
        await owner.close()
