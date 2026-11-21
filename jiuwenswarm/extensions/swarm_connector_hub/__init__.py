# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm Connector Hub extension package (OpenJiuwen Hub MCP catalogue)."""

from .constants import OPENJIUWEN_HUB_PROVIDER_TYPE, OPENJIUWEN_HUB_SOURCE_ID
from .extension import register_extensions

__all__ = [
    "OPENJIUWEN_HUB_PROVIDER_TYPE",
    "OPENJIUWEN_HUB_SOURCE_ID",
    "register_extensions",
]