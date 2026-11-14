# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Local request model and shared response/identity schema exports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from e2a.agent_models import (  # noqa: F401
    AgentRef,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)

from jiuwenswarm.common.schema.req_method import ReqMethod


@dataclass
class AgentRequest:
    """Agent request copied from e2a, using JiuwenSwarm request methods.

    Transport ingress resolves supported method strings to the local enum.
    Internal requests may omit the method.
    """

    request_id: str
    channel_id: str = ""
    session_id: str | None = None
    chat_id: str | None = None
    req_method: ReqMethod | str | None = None
    params: dict = field(default_factory=dict)
    is_stream: bool = False
    timestamp: float = 0.0
    metadata: dict[str, Any] | None = None
    enable_memory: bool | None = None
    permission_context: PermissionContext | None = None
    agent_ref: AgentRef | dict[str, Any] | None = None
    user_id: str = ""
    # Server-only context; never populated from untrusted Gateway payloads.
    trusted_session_message_route: dict[str, Any] | None = field(
        default=None, repr=False, compare=False,
    )
    trusted_runtime_binding: dict[str, Any] | None = field(
        default=None, repr=False, compare=False,
    )


__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
    "ReqMethod",
]
