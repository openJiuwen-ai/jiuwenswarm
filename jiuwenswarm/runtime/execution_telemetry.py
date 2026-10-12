# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Transport-neutral observation of the prepared, real execution entry."""
from contextvars import ContextVar
import json
import logging
import os
import time
from typing import Any, Callable

execution_entry_sink: ContextVar[Callable[[Any, str], None] | None] = ContextVar(
    "runtime_execution_entry_sink", default=None,
)
sdk_entry_sink: ContextVar[Callable[[], None] | None] = ContextVar(
    "runtime_sdk_entry_sink", default=None,
)


def observe_execution_entry(request: Any, mode: str) -> None:
    sink = execution_entry_sink.get()
    if sink is not None:
        sink(request, mode)


def observe_sdk_entry(request: Any, sender: Any) -> None:
    """Observe completed preparation and optionally time the public SDK call.

    The local acceptance driver sets the destination. Ordinary production
    execution does no file IO. The progress callback reports only completed
    preparation; it neither generates business responses nor queue ACKs.
    No request content or credentials are collected.
    """
    sink = sdk_entry_sink.get()
    if sink is not None:
        sink()
    destination = os.environ.get("JIUWENSWARM_SDK_ENTRY_LOG")
    if not destination:
        return
    entered_at_ns = time.perf_counter_ns()
    try:
        owner = getattr(sender, "__self__", None)
        row = {
            "request_id": request.request_id,
            "entered_at_ns": entered_at_ns,
            "pid": os.getpid(),
            "sender_module": getattr(sender, "__module__", ""),
            "sender_owner": type(owner).__qualname__ if owner is not None else "",
        }
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError, AttributeError):
        logging.getLogger(__name__).debug("SDK entry timing unavailable", exc_info=True)
