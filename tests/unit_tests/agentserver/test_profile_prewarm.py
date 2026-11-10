# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
from copy import deepcopy

from jiuwenswarm.server.worker.preparation import PreparationRegistry
from jiuwenswarm.server.worker.profile_prewarm import ProfilePrewarmer
from jiuwenswarm.runtime.model_observation import record_model_use


async def _wait_state(controller, state):
    async with asyncio.timeout(4):
        while controller.snapshot()["state"] != state:
            await asyncio.sleep(0.005)


def test_explicit_off_creates_no_task_or_profile():
    controller = ProfilePrewarmer(get_config=lambda: {"runtime_prewarm": {"enabled": False}}, prepare=None,
                                  admission=None, stop_event=asyncio.Event())
    controller.start()
    assert controller._task is None
    assert controller.snapshot()["state"] == "disabled"


async def test_prewarm_joins_admission_retries_and_invalidates_changed_configuration():
    config = {"runtime_prewarm": {"enabled": True, "idle_delay_seconds": 0,
                                  "retry_delay_seconds": 0}, "model": "first"}
    owner = PreparationRegistry()
    foreground = owner.pause_background()
    calls = []

    async def prepare(mode, channel, snapshot):
        calls.append((mode, channel, deepcopy(snapshot)))
        if len(calls) == 1:
            raise RuntimeError("temporary factory failure")

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare,
                                  admission=owner, stop_event=asyncio.Event())
    try:
        controller.start()
        await asyncio.sleep(0)
        assert calls == []  # Required Runtime barrier has not completed.
        owner.mark_minimum_ready()
        await asyncio.sleep(0.02)
        assert calls == []  # Foreground preparation still owns admission.
        owner.resume_background(foreground)
        await _wait_state(controller, "ready")
        assert len(calls) == 2
        assert controller.note_use(mode="agent", channel_id="web", config=config)
        assert not controller.note_use(mode="code.normal", channel_id="web", config=config)
        config["model"] = "second"
        assert not controller.note_use(mode="agent", channel_id="web", config=config)
        async with asyncio.timeout(4):
            while len(calls) < 3:
                await asyncio.sleep(0.01)
        await _wait_state(controller, "ready")
        assert calls[-1][2]["model"] == "second"
        assert controller.snapshot()["attempts"] == 1
        config["runtime_prewarm"]["enabled"] = False
        assert not controller.note_use(mode="agent", channel_id="web", config=config)
    finally:
        await controller.close()
        await owner.close()


async def test_budget_overrun_stops_retries_and_never_reports_a_hit(monkeypatch):
    from jiuwenswarm.server.worker import profile_prewarm

    memory = {"bytes": 0}
    monkeypatch.setattr(profile_prewarm, "_rss", lambda: memory["bytes"])
    config = {"runtime_prewarm": {"enabled": True, "idle_delay_seconds": 0,
                                  "max_rss_delta_mb": 1}}
    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    calls = []

    async def prepare(*args):
        calls.append(args)
        memory["bytes"] = 2 * 1024 * 1024

    controller = ProfilePrewarmer(get_config=lambda: config, prepare=prepare,
                                  admission=owner, stop_event=asyncio.Event())
    try:
        controller.start()
        await _wait_state(controller, "budget_exceeded")
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        assert not controller.note_use(mode="agent", channel_id="web", config=config)
    finally:
        await controller.close()
        await owner.close()


async def test_shutdown_cancels_inflight_preparation_before_resource_cleanup():
    began, cancelled = asyncio.Event(), asyncio.Event()

    async def prepare(*_):
        began.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    controller = ProfilePrewarmer(
        get_config=lambda: {"runtime_prewarm": {"enabled": True, "idle_delay_seconds": 0}},
        prepare=prepare, admission=owner, stop_event=asyncio.Event(),
    )
    try:
        controller.start()
        await asyncio.wait_for(began.wait(), 2)
        await controller.close()
        assert cancelled.is_set()
        assert controller.snapshot()["state"] == "cancelled"
    finally:
        await controller.close()
        await owner.close()


async def test_readiness_is_separate_from_actual_model_reuse_and_observer_failure_is_isolated():
    model = object()
    config = {"runtime_prewarm": {"enabled": True, "mode": "code", "idle_delay_seconds": 0}}
    fail_config = False

    def get_config():
        if fail_config:
            raise OSError("configuration temporarily unavailable")
        return config

    async def prepare(*_):
        return model

    owner = PreparationRegistry()
    owner.mark_minimum_ready()
    controller = ProfilePrewarmer(get_config=get_config, prepare=prepare,
                                  admission=owner, stop_event=asyncio.Event())
    try:
        controller.start()
        await _wait_state(controller, "ready")
        assert controller.note_use(mode="agent.code.normal", channel_id="web", config=config)
        assert not controller.note_use(mode="team.code.normal", channel_id="web", config=config)
        assert controller.snapshot()["hits"] == 0
        record_model_use(object())
        assert controller.snapshot()["hits"] == 0
        record_model_use(model)
        assert controller.snapshot()["hits"] == 1
        fail_config = True
        record_model_use(model)  # Optional statistics cannot fail a model acquisition.
        assert controller.snapshot()["hits"] == 1
        await controller.close()
        record_model_use(model)
        assert controller.snapshot()["hits"] == 1
        assert not controller.note_use(mode="code.normal", channel_id="web", config=config)
    finally:
        await controller.close()
        await owner.close()
