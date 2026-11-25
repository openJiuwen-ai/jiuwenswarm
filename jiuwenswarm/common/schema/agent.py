# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Shared agent models with JiuwenSwarm-owned request methods."""

from __future__ import annotations

from dataclasses import dataclass

from e2a.agent_models import (  # noqa: F401
    AgentRef,
    AgentRequest as ProtocolAgentRequest,
    AgentResponse,
    AgentResponseChunk,
    PermissionContext,
)

from jiuwenswarm.common.schema.message import ReqMethod


@dataclass
class AgentRequest(ProtocolAgentRequest):
    """Shared request fields with the local method type used by dispatch."""

    req_method: ReqMethod | str | None = None


__all__ = [
    "AgentRef",
    "AgentRequest",
    "AgentResponse",
    "AgentResponseChunk",
    "PermissionContext",
    "ReqMethod",
]
