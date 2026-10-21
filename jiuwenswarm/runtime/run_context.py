# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Request-local permission approval capability for Process CLI."""
from dataclasses import dataclass
from contextvars import ContextVar


@dataclass(frozen=True, slots=True)
class RunContext:
    supports_permission_approval: bool = True


CURRENT_RUN_CONTEXT: ContextVar[RunContext | None] = ContextVar("jiuwenswarm_run_context", default=None)
