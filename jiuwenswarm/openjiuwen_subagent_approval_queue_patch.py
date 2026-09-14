# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Queue same-scope hosted subagent approvals instead of rejecting them.

openjiuwen's ``SubagentApprovalRegistry.request`` raises
``SubagentApprovalCapacityError`` when a scope already has a pending card.
pptx-craft slide-designer subagents often fire several ``read_file`` calls in
one batch; extras were denied immediately, then retried until the 120s TTL,
so HTML generation never finished.

Serialize ``request()`` per ``(session_id, agent_scope_id)`` so later calls
wait for the in-flight card instead of failing closed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger("jiuwenswarm.openjiuwen_subagent_approval_queue_patch")

_PATCHED = False
_SCOPE_LOCKS_GUARD = threading.Lock()
_SCOPE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _scope_lock(session_id: str, agent_scope_id: str) -> asyncio.Lock:
    key = (session_id, agent_scope_id)
    with _SCOPE_LOCKS_GUARD:
        lock = _SCOPE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SCOPE_LOCKS[key] = lock
        return lock


def apply_subagent_approval_queue_patch() -> None:
    """Monkey-patch registry.request so same-scope approvals queue."""
    global _PATCHED
    if _PATCHED:
        return
    try:
        from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
            SubagentApprovalRegistry,
        )
    except ImportError:
        return
    if getattr(SubagentApprovalRegistry.request, "_jiuwen_scope_queued", False):
        _PATCHED = True
        return

    original = SubagentApprovalRegistry.request

    async def queued_request(self, *args: Any, **kwargs: Any) -> Any:
        session_id = str(kwargs.get("session_id") or "").strip()
        agent_scope_id = str(kwargs.get("agent_scope_id") or "").strip()
        if not session_id or not agent_scope_id:
            return await original(self, *args, **kwargs)
        lock = _scope_lock(session_id, agent_scope_id)
        if lock.locked():
            logger.info(
                "[SubagentApprovalQueue] wait for in-flight hosted approval "
                "session=%s scope=%s tool_call=%s",
                session_id,
                agent_scope_id,
                kwargs.get("tool_call_id"),
            )
        async with lock:
            return await original(self, *args, **kwargs)

    queued_request._jiuwen_scope_queued = True  # type: ignore[attr-defined]
    SubagentApprovalRegistry.request = queued_request  # type: ignore[method-assign]
    _PATCHED = True
    logger.info("[SubagentApprovalQueue] same-scope hosted approvals now queue")
