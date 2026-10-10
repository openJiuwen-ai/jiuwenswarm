# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Immutable options owned by one invocation, consumed once by its session."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RunContext:
    max_turns: int | None = None
    host_tools: tuple[Any, ...] = field(default_factory=tuple, repr=False)
    budget: int | None = None
    tool_permissions: frozenset[str] | None = None
    credential_context: Any = field(default=None, repr=False, compare=False)
