"""Restart the DingTalk channels for one configuration at a time."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.configuration import dingtalk_channel_settings


class DingTalkChannelSet:
    """Own the registered DingTalk channels: stop the old ones, then start one per configured app.

    A save can restart DingTalk while an earlier restart is still stopping its channels. Restarts
    therefore run one at a time, each reads the latest settings only after the old channels have
    stopped, and a stopped channel is never unregistered by channel id, which would also remove
    channels registered meanwhile.
    """

    def __init__(
        self,
        channel_manager: Any,
        *,
        create_channel: Callable[[dict[str, Any]], Any],
        stop_channel: Callable[[Any], Awaitable[None]],
        api_base: str,
        oapi_base: str,
    ) -> None:
        self._channel_manager = channel_manager
        self._create_channel = create_channel
        self._stop_channel = stop_channel
        self._api_base = api_base
        self._oapi_base = oapi_base
        self._lock = asyncio.Lock()

    async def restart(self) -> list[str]:
        """Replace the running channels with the current settings; return the started client IDs."""
        async with self._lock:
            for channel in self._channel_manager.pop_channels_by_id("dingtalk"):
                await self._stop_channel(channel)
            started = []
            for settings in dingtalk_channel_settings(
                self._channel_manager.get_conf("dingtalk"), api_base=self._api_base, oapi_base=self._oapi_base,
            ):
                channel = self._create_channel(settings)
                self._channel_manager.register_channel(channel)
                channel.start_task = asyncio.create_task(channel.start(), name=f"dingtalk-{settings['client_id']}")
                started.append(settings["client_id"])
            return started
