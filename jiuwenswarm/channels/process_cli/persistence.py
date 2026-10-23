# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Drain Process CLI Session persistence before a local child process exits."""

from __future__ import annotations

import asyncio


async def flush_session_writes(*, timeout_seconds: float = 5.0) -> None:
    """Wait for both independent writer barriers within one shutdown window."""
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    queues = (
        ("history", session_history.flush_pending_writes),
        ("metadata", session_metadata.flush_pending_writes),
    )
    flush_timeout = timeout_seconds - min(0.5, timeout_seconds / 10)
    results = await asyncio.gather(
        *(asyncio.to_thread(flush, flush_timeout) for _, flush in queues),
        return_exceptions=True,
    )
    failed = [name for (name, _), result in zip(queues, results) if result is not True]
    if failed:
        raise RuntimeError(f"{', '.join(failed)} writes did not finish before shutdown")
