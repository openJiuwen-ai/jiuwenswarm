# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Immutable options owned by one invocation, consumed once by its session."""
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any


@dataclass(frozen=True, slots=True)
class RunContext:
    request_id: str = ""
    session_id: str = ""
    max_turns: int | None = None
    host_tools: tuple[Any, ...] = field(default_factory=tuple, repr=False)
    max_budget_usd: float | None = None
    tool_permissions: tuple[tuple[str, str], ...] | None = None
    credential_context: Any = field(default=None, repr=False, compare=False)
    supports_permission_approval: bool = True


CURRENT_RUN_CONTEXT: ContextVar[RunContext | None] = ContextVar("jiuwenswarm_run_context", default=None)
