# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pull the AgentOS data-plane config from etcd and apply the jiuwenbox section.

External contract shared with the management plane (writer) and the gateway
(another reader) -- do not change ``CONFIG_SYNC_KEY`` without coordinating
all three.
"""

from __future__ import annotations

# Same literal as jiuwenswarm.gateway.configs.etcd_sync.CONFIG_SYNC_KEY.
CONFIG_SYNC_KEY = "/agentos/config/data-plane"
COMPONENT_NAME = "jiuwenbox"
ETCD_ENDPOINTS_ENV = "JIUWENBOX_ETCD_ENDPOINTS"
ETCD_CONFIG_KEY_ENV = "JIUWENBOX_ETCD_CONFIG_KEY"

__all__ = [
    "COMPONENT_NAME",
    "CONFIG_SYNC_KEY",
    "ETCD_CONFIG_KEY_ENV",
    "ETCD_ENDPOINTS_ENV",
]
