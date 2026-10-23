# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared cleanup of this process's cached and pending JiuwenBox sandboxes.

Disable, restart and shutdown use the provider's lifecycle-aware deletion path.
It includes historical URLs, preserves failed deletes for retry, and never scans
or deletes sandboxes belonging to other AgentServer processes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_PER_DELETE_TIMEOUT_SECONDS: float = 5.0


async def cleanup_jiuwenbox_sandboxes(*, reason: str = "teardown", strict: bool = False) -> int:
    """Delete all process-known cached/pending entries, retaining failures.

    Run on the calling event loop so concurrent provider DELETE tasks can be
    joined safely. Strict callers must finish this phase before installing ACLs
    for any replacement instance.
    """
    try:
        from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import delete_jiuwenbox_sandbox

        deleted = await delete_jiuwenbox_sandbox(
            delete_all=True, reason=reason, timeout_seconds=_PER_DELETE_TIMEOUT_SECONDS,
            raise_on_error=strict,
        )
        return len(deleted)
    except Exception:
        if strict:
            raise
        logger.warning("[sandbox_lifecycle] sandbox cleanup failed", exc_info=True)
        return 0


async def recreate_all_sandboxes() -> int:
    """Best-effort cleanup; subsequent operations lazily create replacements."""
    return await cleanup_jiuwenbox_sandboxes(reason="policy_changed")


__all__ = ["cleanup_jiuwenbox_sandboxes", "recreate_all_sandboxes"]
