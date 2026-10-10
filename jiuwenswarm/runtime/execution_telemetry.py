# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Transport-neutral observation of the prepared, real execution entry."""
from contextvars import ContextVar
from typing import Any, Callable

execution_entry_sink: ContextVar[Callable[[Any, str], None] | None] = ContextVar(
    "runtime_execution_entry_sink", default=None,
)


def observe_execution_entry(request: Any, mode: str) -> None:
    sink = execution_entry_sink.get()
    if sink is not None:
        sink(request, mode)
