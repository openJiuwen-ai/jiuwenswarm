# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    utils,
    xiaoyi_gui_tool,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi import xiaoyi_connect
from jiuwenswarm.runtime import host_services


@pytest.fixture(autouse=True)
def _isolate_runtime_xiaoyi_channel_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the process-global provider isolated from every other test."""
    monkeypatch.setattr(host_services, "_runtime_xiaoyi_channel_provider", None)
    monkeypatch.setattr(host_services, "_runtime_xiaoyi_channel_providers", [])
    monkeypatch.setattr(xiaoyi_connect, "_xiaoyi_channel_instances", {})
    monkeypatch.setattr(xiaoyi_connect, "_xiaoyi_channel_owners", {})
    monkeypatch.setattr(
        xiaoyi_connect,
        "_runtime_xiaoyi_provider_installed",
        False,
    )
    monkeypatch.setattr(
        xiaoyi_connect,
        "_runtime_xiaoyi_provider_previous",
        None,
    )


def _channel(channel_id: str, *, enabled: bool = True):
    config = xiaoyi_connect.XiaoyiChannelConfig(
        enabled=enabled,
        channel_id=channel_id,
        mode="xiaoyi_claw",
    )
    return xiaoyi_connect.XiaoyiChannel(
        config,
        xiaoyi_connect.RobotMessageRouter(),
    )


def test_import_does_not_permanently_install_runtime_provider() -> None:
    importlib.reload(xiaoyi_connect)

    assert host_services.get_runtime_xiaoyi_channel() is None


@pytest.mark.asyncio
async def test_provider_installs_on_first_start_and_restores_after_last_stop() -> None:
    fallback = object()

    def fallback_provider(_channel_id: str):
        return fallback

    host_services.install_runtime_xiaoyi_channel_provider(fallback_provider)
    first = _channel("xiaoyi-first")
    second = _channel("xiaoyi-second")

    await first.start()
    await second.start()

    assert host_services.get_runtime_xiaoyi_channel("xiaoyi-first") is first
    assert host_services.get_runtime_xiaoyi_channel("xiaoyi-second") is second

    await first.stop()

    assert host_services.get_runtime_xiaoyi_channel("xiaoyi-first") is None
    assert host_services.get_runtime_xiaoyi_channel("xiaoyi-second") is second

    await second.stop()
    await asyncio.sleep(0)

    assert host_services.get_runtime_xiaoyi_channel("xiaoyi-second") is fallback


@pytest.mark.asyncio
async def test_same_id_channels_support_out_of_order_stop() -> None:
    first = _channel("xiaoyi")
    second = _channel("xiaoyi")
    never_started = _channel("xiaoyi", enabled=False)

    await never_started.start()
    assert host_services.get_runtime_xiaoyi_channel() is None

    await first.start()
    await second.start()

    assert xiaoyi_connect.get_xiaoyi_channel() is second

    await first.stop()
    await never_started.stop()

    assert xiaoyi_connect.get_xiaoyi_channel() is second
    assert host_services.get_runtime_xiaoyi_channel() is second

    await second.stop()
    await asyncio.sleep(0)

    assert xiaoyi_connect.get_xiaoyi_channel() is None
    assert host_services.get_runtime_xiaoyi_channel() is None


def test_runtime_xiaoyi_channel_provider_install_and_lookup() -> None:
    first_channel = object()
    second_channel = object()
    calls: list[tuple[str, str]] = []

    def first_provider(channel_id: str) -> Any:
        calls.append(("first", channel_id))
        return first_channel

    def second_provider(channel_id: str) -> Any:
        calls.append(("second", channel_id))
        return second_channel

    assert host_services.get_runtime_xiaoyi_channel() is None
    assert host_services.install_runtime_xiaoyi_channel_provider(first_provider) is None
    assert host_services.get_runtime_xiaoyi_channel() is first_channel

    previous = host_services.install_runtime_xiaoyi_channel_provider(second_provider)

    assert previous is first_provider
    assert (
        host_services.get_runtime_xiaoyi_channel("xiaoyi-secondary") is second_channel
    )
    assert calls == [
        ("first", "xiaoyi"),
        ("second", "xiaoyi-secondary"),
    ]


@pytest.mark.asyncio
async def test_device_command_rejects_missing_runtime_xiaoyi_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[str] = []

    def no_active_channel(channel_id: str) -> None:
        lookups.append(channel_id)
        return None

    def unexpected_config_lookup() -> dict[str, Any]:
        pytest.fail("session config must not be read without an active Xiaoyi channel")

    host_services.install_runtime_xiaoyi_channel_provider(no_active_channel)
    monkeypatch.setattr(utils, "get_config", unexpected_config_lookup)

    with pytest.raises(RuntimeError, match="No active XY session found"):
        await utils.execute_device_command("TestIntent", {"payload": {}})

    assert lookups == ["xiaoyi"]


@pytest.mark.asyncio
async def test_gui_tool_rejects_missing_runtime_xiaoyi_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[str] = []

    def no_active_channel(channel_id: str) -> None:
        lookups.append(channel_id)
        return None

    def unexpected_config_lookup() -> dict[str, Any]:
        pytest.fail("session config must not be read without an active Xiaoyi channel")

    host_services.install_runtime_xiaoyi_channel_provider(no_active_channel)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        unexpected_config_lookup,
    )

    with pytest.raises(RuntimeError, match="xiaoyi_gui_agent"):
        await xiaoyi_gui_tool.xiaoyi_gui_agent._func("open settings")

    assert lookups == ["xiaoyi"]
