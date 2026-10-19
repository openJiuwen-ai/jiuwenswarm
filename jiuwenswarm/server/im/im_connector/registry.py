"""In-memory connector registry keyed by ``meta.id``."""

from __future__ import annotations

from typing import Iterator, Optional

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin


class ConnectorRegistry:
    """Holds connectors keyed by channel id. Built once at startup."""

    def __init__(self, plugins: Optional[list[ChannelPlugin]] = None) -> None:
        self._plugins: dict[str, ChannelPlugin] = {}
        for plugin in plugins or ():
            self.register(plugin)

    def register(self, plugin: ChannelPlugin) -> None:
        key = plugin.meta.id
        if key in self._plugins:
            raise ValueError(f"channel already registered: {key}")
        self._plugins[key] = plugin

    def get(self, channel_id: str) -> Optional[ChannelPlugin]:
        return self._plugins.get(channel_id)

    def require(self, channel_id: str) -> ChannelPlugin:
        plugin = self._plugins.get(channel_id)
        if plugin is None:
            raise KeyError(f"channel not registered: {channel_id}")
        return plugin

    def items(self) -> Iterator[tuple[str, ChannelPlugin]]:
        return iter(self._plugins.items())

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, channel_id: object) -> bool:
        return isinstance(channel_id, str) and channel_id in self._plugins


__all__ = ["ConnectorRegistry"]
