# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep control continuations bound to an already verified session owner."""

from typing import Any

from jiuwenswarm.common.mode_matrix import resolve_adapter_mode
from jiuwenswarm.common.schema.agent import AgentRequest


def bind_request_to_session_owner(request: AgentRequest, owner: Any) -> None:
    """Use only after the caller has located and authorized the paused owner.

    Preserve business mode and plan state; only the implementation identity
    follows the live facade instead of the current default configuration.
    """
    if getattr(owner, "_adapter", None) is None:
        return
    bound_mode = getattr(owner, "_adapter_mode", None)
    if not isinstance(bound_mode, str) or not bound_mode.strip():
        return
    request.resolved_adapter_mode = resolve_adapter_mode(bound_mode)
    request.resolved_adapter_source = "session_owner"
