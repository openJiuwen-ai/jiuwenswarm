# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Host channel types used by channel extensions.

These are the host's exact runtime classes. Extensions may subclass
``BaseChannel`` and use the other types in its send and metadata contract.
"""

from jiuwenswarm.gateway.channel_manager.base import (
    BaseChannel,
    ChannelMetadata,
    RobotMessageRouter,
)
from jiuwenswarm.gateway.routing.keys import (
    SlackDeliveryTarget,
    make_builtin_delivery_target,
)
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

__all__ = [
    "BaseChannel",
    "ChannelMetadata",
    "RobotMessageRouter",
    "RoutingTarget",
    "SlackDeliveryTarget",
    "make_builtin_delivery_target",
]
