# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import dataclasses

import pytest

from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.gateway.channel_manager.sdk import ChannelCapabilities


def test_defaults_declare_a_plain_text_channel():
    caps = ChannelCapabilities()
    assert caps.buttons is False
    assert caps.streaming is False
    assert caps.file_upload is False
    assert caps.rich_text is False
    assert caps.threads is False
    assert caps.max_message_length is None


def test_capabilities_are_immutable():
    caps = ChannelCapabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.buttons = True


def test_adapter_can_declare_its_own_capabilities():
    class DummyChannel(BaseChannel):
        name = "dummy"
        capabilities = ChannelCapabilities(
            buttons=True,
            threads=True,
            max_message_length=2000,
        )

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    assert DummyChannel.capabilities.buttons is True
    assert DummyChannel.capabilities.threads is True
    assert DummyChannel.capabilities.max_message_length == 2000
    assert DummyChannel.capabilities.streaming is False


def test_base_channel_defaults_to_no_capabilities():
    assert BaseChannel.capabilities == ChannelCapabilities()