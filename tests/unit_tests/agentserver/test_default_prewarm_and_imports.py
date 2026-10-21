# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded defaults, foreground checkpoints and Source-only import ownership."""

import asyncio
import threading

import pytest

from jiuwenswarm.server.worker.preparation import PreparationRegistry
from jiuwenswarm.server.worker.profile_prewarm import ProfilePrewarmer, prewarm_checkpoint


async def wait_state(controller, state):
    async with asyncio.timeout(3):
        while controller.snapshot()["state"] != state:
            await asyncio.sleep(.005)


@pytest.mark.asyncio
async def test_missing_switch_defaults_to_one_profile_and_no_repeated_preparation():
    config = {"runtime_prewarm": {"idle_delay_seconds": 0}}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    calls = []
    model = object()

    async def prepare(mode, channel, _config):
        calls.append((mode, channel))
        return model

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare, admission=owner,
                                  stop_event=asyncio.Event())
    try:
        controller.start()
        await wait_state(controller, "ready")
        assert calls == [("agent", "web")]
        assert controller.note_model_use(model)
        config["runtime_prewarm"]["enabled"] = False
        assert not controller.note_model_use(model)
    finally:
        await controller.close()
        await owner.close()


@pytest.mark.asyncio
async def test_explicitly_disabled_controller_can_start_after_config_reload():
    config = {"runtime_prewarm": {"enabled": False, "idle_delay_seconds": 0}}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    calls = []

    async def prepare(*_):
        calls.append(True)

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare, admission=owner,
                                  stop_event=asyncio.Event())
    try:
        controller.start()
        assert controller._task is None
        config["runtime_prewarm"]["enabled"] = True
        controller.start()
        await wait_state(controller, "ready")
        assert len(calls) == 1
    finally:
        await controller.close()
        await owner.close()


@pytest.mark.asyncio
async def test_foreground_accepted_during_step_prevents_later_background_steps():
    config = {"runtime_prewarm": {"idle_delay_seconds": 0, "max_attempts": 1}}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    steps = []
    tokens = []

    async def prepare(*_):
        steps.append("accepted import completed")
        tokens.append(owner.pause_background())
        await prewarm_checkpoint()
        steps.append("model construction must not start")

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare, admission=owner,
                                  stop_event=asyncio.Event())
    try:
        controller.start()
        await wait_state(controller, "yielded_to_foreground")
        assert steps == ["accepted import completed"]
        assert controller.snapshot()["attempts"] == 1
    finally:
        for token in tokens:
            owner.resume_background(token)
        await controller.close()
        await owner.close()


@pytest.mark.asyncio
async def test_configuration_change_at_checkpoint_cannot_publish_an_old_profile():
    config = {"runtime_prewarm": {"idle_delay_seconds": 0, "max_attempts": 1}, "model": "old"}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    stopped = asyncio.Event()

    async def prepare(*_):
        config["model"] = "new"
        try:
            await prewarm_checkpoint()
        finally:
            stopped.set()
        pytest.fail("old profile must not be published")

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare, admission=owner,
                                  stop_event=asyncio.Event())
    try:
        controller.start()
        await asyncio.wait_for(stopped.wait(), 2)
        await asyncio.sleep(.01)
        assert controller.snapshot()["state"] != "ready"
        assert not controller.note_model_use(object())
    finally:
        await controller.close()
        await owner.close()


@pytest.mark.asyncio
async def test_optional_failures_are_capped_at_two_even_with_larger_config():
    config = {"runtime_prewarm": {"idle_delay_seconds": 0, "retry_delay_seconds": 0, "max_attempts": 99}}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    calls = []

    async def prepare(*_):
        calls.append(True)
        raise OSError("optional provider failure")

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare, admission=owner,
                                  stop_event=asyncio.Event())
    try:
        controller.start()
        async with asyncio.timeout(2):
            while controller.snapshot()["attempts"] < 2:
                await asyncio.sleep(.005)
        await asyncio.sleep(.03)
        assert len(calls) == 2 and controller.snapshot()["state"] == "failed"
    finally:
        await controller.close()
        await owner.close()


@pytest.mark.asyncio
async def test_import_shutdown_cannot_interrupt_current_module_but_stops_the_next(monkeypatch):
    from jiuwenswarm.server.worker import import_preparation

    accepted, finish = threading.Event(), threading.Event()
    calls = []

    def import_module(name):
        calls.append(name)
        accepted.set()
        assert finish.wait(2)

    monkeypatch.setattr(import_preparation.importlib, "import_module", import_module)
    task = import_preparation.ModuleImportTask(("first", "must-not-start"), name="test-owned-import")
    task.start()
    try:
        assert await asyncio.to_thread(accepted.wait, 1)
        with pytest.raises(TimeoutError):
            await task.close(timeout=.02)
        assert calls == ["first"]
    finally:
        finish.set()
        await task.close(timeout=1)
    assert calls == ["first"]
