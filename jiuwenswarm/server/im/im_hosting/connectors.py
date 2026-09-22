"""Build ChannelPlugin instances from auto-resolved CLI paths."""

from __future__ import annotations

from typing import Optional

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_connector.registry import ConnectorRegistry
from jiuwenswarm.server.im.im_hosting.cli_resolve import resolve_cli_path
from jiuwenswarm.server.im.im_hosting.policy import CHANNEL_IDS

_CHANNEL_LABELS = {
    "feishu": "飞书",
    "dingtalk": "钉钉",
    "welink": "WeLink",
}


def channel_label(channel_id: str) -> str:
    return _CHANNEL_LABELS.get(channel_id, channel_id)


def build_connector(channel_id: str) -> Optional[ChannelPlugin]:
    path = resolve_cli_path(channel_id)
    if not path:
        return None
    config = {"cli_path": path}
    if channel_id == "feishu":
        from jiuwenswarm.server.im.im_connector.connectors.feishu import FeishuCli, FeishuConnector

        return FeishuConnector(FeishuCli(config))
    if channel_id == "dingtalk":
        from jiuwenswarm.server.im.im_connector.connectors.dingtalk import (
            DingTalkCli,
            DingTalkConnector,
        )

        return DingTalkConnector(DingTalkCli(config))
    if channel_id == "welink":
        from jiuwenswarm.server.im.im_connector.connectors.welink import WeLinkConnector, WelinkCli

        return WeLinkConnector(WelinkCli(config))
    return None


def build_registry() -> ConnectorRegistry:
    plugins: list[ChannelPlugin] = []
    for cid in CHANNEL_IDS:
        plugin = build_connector(cid)
        if plugin is not None:
            plugins.append(plugin)
    return ConnectorRegistry(plugins)
