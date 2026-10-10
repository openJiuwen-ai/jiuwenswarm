# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Update local config from the AgentOS data-plane configuration.

Reads the ``gateway`` section of one etcd key and overlays managed fields onto
the Gateway's in-memory full-config snapshot.

The etcd transport lives in ``jiuwenswarm.common.etcd``; Gateway injects the
runtime refresh callback and owns this service's process lifecycle.

Trust boundary: this key is management-plane input and may change local
resource/reclamation settings. Its etcd endpoint must be restricted to trusted
networks and protected by deployment-side etcd ACLs; plain HTTP is only
appropriate on an isolated internal network.
"""

from __future__ import annotations

# External contract shared with the management plane (writer) and jiuwenbox
# (another reader) -- do not change without coordinating all three.
CONFIG_SYNC_KEY = "/agentos/config/data-plane"

__all__ = ["CONFIG_SYNC_KEY"]
