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


# 进程级共享注册表：托管链（im_hosting）与学习链（personal_context）复用
# 同一批 ChannelPlugin 实例，CLI 子进程运行时、节流与 429 退避只建一份；
# 两条链各自维护调度、范围与游标，不共享 Poller。
_shared_registry: ConnectorRegistry | None = None


def get_shared_registry() -> ConnectorRegistry:
    """构建一次并缓存；无任何 CLI 时返回空注册表（合法状态）。"""
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = build_registry()
    return _shared_registry


def ensure_connector(channel_id: str) -> Optional[ChannelPlugin]:
    """返回共享实例；CLI 后装时懒构建并注册进共享注册表。"""
    registry = get_shared_registry()
    plugin = registry.get(channel_id)
    if plugin is None:
        plugin = build_connector(channel_id)
        if plugin is not None:
            try:
                registry.register(plugin)
            except ValueError:
                plugin = registry.get(channel_id)
    return plugin


def reset_shared_registry() -> None:
    """测试隔离用：丢弃共享注册表单例。"""
    global _shared_registry
    _shared_registry = None


class LazySharedRegistryView(ConnectorRegistry):
    """学习链（PersonalContext AS-10）对共享注册表的按需视图。

    宿主构造时零副作用：真正的 Connector 解析推迟到首次学习抓取，经
    ensure_connector 走与托管代回链同一份共享注册表（CLI 子进程、节流与
    429 退避只建一份）。视图自身不持有插件，仅覆盖被学习链使用的
    get/require 语义。
    """

    def get(self, channel_id: str) -> Optional[ChannelPlugin]:
        return ensure_connector(channel_id)

    def require(self, channel_id: str) -> ChannelPlugin:
        plugin = ensure_connector(channel_id)
        if plugin is None:
            raise KeyError(f"channel not registered: {channel_id}")
        return plugin
