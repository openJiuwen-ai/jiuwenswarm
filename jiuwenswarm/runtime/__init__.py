"""Minimal public surface for optional Runtime host capabilities.

This package is not the Session Runtime in ``jiuwenswarm.server.runtime``.
Commit 3 only exposes the push install/restore/send API used by AgentServer
and MultiSessionToolkit. Wake/Xiaoyi providers and AgentRuntime are not
exported here.
"""

from jiuwenswarm.runtime.host_services import (
    RuntimeHostPushTransport,
    RuntimePushHandler,
    install_runtime_push_handler,
    restore_runtime_push_handler,
    send_runtime_push,
)

__all__ = [
    "RuntimeHostPushTransport",
    "RuntimePushHandler",
    "install_runtime_push_handler",
    "restore_runtime_push_handler",
    "send_runtime_push",
]
