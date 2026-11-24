from __future__ import annotations

import importlib.util

import pytest

from jiuwenswarm.extensions.rea_setup.extension import (
    ReaSetupApplicationPlugin,
    register_extensions,
)

_HAS_REA_SETUP = (
    importlib.util.find_spec("jiuwenswarm.common.rea_setup") is not None
)


class _Registry:
    def __init__(self) -> None:
        self.plugins = []

    def register_application_plugin(self, plugin) -> None:
        self.plugins.append(plugin)


class _Channel:
    def __init__(self) -> None:
        self.methods = {}

    def register_method(self, name, handler) -> None:
        self.methods[name] = handler


async def test_register_extensions_adds_rea_application_plugin() -> None:
    registry = _Registry()

    extensions = await register_extensions(registry)

    assert registry.plugins == extensions
    assert len(extensions) == 1
    assert isinstance(extensions[0], ReaSetupApplicationPlugin)


@pytest.mark.skipif(
    not _HAS_REA_SETUP,
    reason="rea_setup is delivered by the preceding backend PR",
)
async def test_bind_web_channel_registers_rea_methods() -> None:
    channel = _Channel()

    ReaSetupApplicationPlugin().bind_web_channel(channel, services=None)

    assert "rea.status.get" in channel.methods
    assert "rea.base.install" in channel.methods
    assert "rea.enhanced.install" in channel.methods
