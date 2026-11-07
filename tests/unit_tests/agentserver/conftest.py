# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixtures for tests simulating a process-lifetime Server Worker."""

import pytest


@pytest.fixture
async def worker_callback_framework(monkeypatch):
    """Give each simulated Worker its own SDK registry and cleanup boundary."""
    from jiuwenswarm.runtime import kernel as kernel_module
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback import AsyncCallbackFramework

    kernel = kernel_module.ResourceKernel()
    monkeypatch.setattr(kernel_module, "_PROCESS_KERNEL", kernel)
    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    yield framework
    owner = getattr(framework.register, "_worker_callback_registry_owner", None)
    if owner is not None:
        await owner.aclose()
    await kernel.close()


@pytest.fixture(autouse=True)
def _delivery_ledger_tmp_dir(tmp_path, monkeypatch):
    """P6 交付台账：agentserver 目录测试一律落 tmp，防真目录被测试条目污染。"""
    from jiuwenswarm.common import utils

    directory = tmp_path / "delivery-ledger-home"
    monkeypatch.setattr(utils, "get_delivery_ledger_dir", lambda: directory, raising=False)
    yield directory
