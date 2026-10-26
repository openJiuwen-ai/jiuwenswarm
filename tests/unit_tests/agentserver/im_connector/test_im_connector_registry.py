from __future__ import annotations

import pytest

from jiuwenswarm.server.im.im_connector.connectors.welink import MockWelinkCli, WeLinkConnector
from jiuwenswarm.server.im.im_connector.registry import ConnectorRegistry


def test_connector_registry_registers_and_requires_by_channel_id():
    plugin = WeLinkConnector(MockWelinkCli(), self_account="alice")
    registry = ConnectorRegistry()
    registry.register(plugin)
    assert "welink" in registry
    assert registry.require("welink") is plugin
    with pytest.raises(KeyError):
        registry.require("feishu")


def test_connector_registry_rejects_duplicate_channel():
    mock = MockWelinkCli()
    registry = ConnectorRegistry([WeLinkConnector(mock, self_account="a")])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(WeLinkConnector(MockWelinkCli(), self_account="b"))
