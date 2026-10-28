# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixtures for tests simulating a process-lifetime Server Worker."""

import pytest


@pytest.fixture
async def worker_callback_framework(monkeypatch):
    """Give each simulated Worker its own SDK registry and cleanup boundary."""
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback import AsyncCallbackFramework

    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    yield framework
    owner = getattr(framework.register, "_worker_callback_registry_owner", None)
    if owner is not None:
        await owner.aclose()
