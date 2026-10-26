# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SSH server channel: accept SSH clients and deliver sessions to MessageHandler."""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ["SshChannel", "SshChannelConfig", "SshAuthConfig", "KeyRegistry"]

if TYPE_CHECKING:
    from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import KeyRegistry
    from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import (
        SshAuthConfig,
        SshChannel,
        SshChannelConfig,
    )


def __getattr__(name: str):
    if name in {"SshChannel", "SshChannelConfig", "SshAuthConfig"}:
        from jiuwenswarm.gateway.channel_manager.protocol.ssh import ssh_connect

        return getattr(ssh_connect, name)
    if name == "KeyRegistry":
        from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import KeyRegistry

        return KeyRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
