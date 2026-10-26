# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Channel 配置领域对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHANNEL_CONFIG_STORE_NAME = "channel_config"


@dataclass
class ChannelConfig:
    """一条频道配置。

    ``channel_id`` 是业务主键（``web`` / ``feishu`` 等），对应 personal YAML
    的 map key，以及 enterprise 表的 ``channel_id``。

    ``body`` 是 ChannelManager 当前使用的那段 dict（``send_file_allowed``、
    ``apps``、``enabled`` 等）。enterprise 行里它落在 ``config`` JSON 列。
    """

    channel_id: str
    body: dict[str, Any] = field(default_factory=dict)
    channel_name: str = ""
    channel_type: str = ""
    bot_id: str = ""
    status: str = "active"

    def to_map_entry(self) -> dict[str, Any]:
        """还原 ChannelManager / ``get_config()['channels'][id]`` 的那份 dict。"""
        return dict(self.body)


def channels_map(items: list[ChannelConfig]) -> dict[str, dict[str, Any]]:
    """拼成 ChannelManager 现在吃的 ``{channel_id: body}``。"""
    return {item.channel_id: item.to_map_entry() for item in items}


__all__ = [
    "CHANNEL_CONFIG_STORE_NAME",
    "ChannelConfig",
    "channels_map",
]
