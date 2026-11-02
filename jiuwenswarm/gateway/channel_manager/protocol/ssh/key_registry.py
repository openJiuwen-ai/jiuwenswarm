# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility re-exports for AgentOS SSH key registry types."""

from __future__ import annotations

from jiuwenswarm.extensions.agentos.auth.ssh_key_issuer import SshKeyIssuer
from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import (
    KeyRegistry,
    KeyRegistryEntry,
)

__all__ = ["KeyRegistry", "KeyRegistryEntry", "SshKeyIssuer"]
