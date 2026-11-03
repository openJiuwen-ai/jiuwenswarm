# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Channel 配置领域：Repository + Codec。"""

from gateway.config.channel.codec import (
    ChannelConfigCodec,
    DbRowChannelCodec,
    YamlMapChannelCodec,
)
from gateway.config.channel.models import (
    CHANNEL_CONFIG_STORE_NAME,
    ChannelConfig,
    channels_map,
)
from gateway.config.channel.repository import ChannelConfigRepository

__all__ = [
    "CHANNEL_CONFIG_STORE_NAME",
    "ChannelConfig",
    "ChannelConfigCodec",
    "ChannelConfigRepository",
    "DbRowChannelCodec",
    "YamlMapChannelCodec",
    "channels_map",
]
