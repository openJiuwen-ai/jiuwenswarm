"""Settle a terminal host request's original, verified confirmation identity."""

from __future__ import annotations

import asyncio

from openjiuwen.harness import persistent_agent as core


async def discard_permission_confirmation(*, sessions, windows, sharing, approvals, record, approval,
                                          runtime_fingerprint: str | None = None) -> None:
    """Never use a newer runtime to bypass core's validation of the paused Spec."""
    reader = getattr(sessions, "runtime_fingerprint", None)
    execution_id = getattr(approval, "execution_id", None)
    saved = (
        await asyncio.to_thread(reader, record.namespace_id, record.request_id, execution_id)
        if reader is not None and execution_id is not None else None
    )
    fingerprint = saved or runtime_fingerprint or record.config_fingerprint
    generation = (
        await asyncio.to_thread(sharing.repository.saved_request_window, record.namespace_id, record.request_id)
        if sharing is not None else ""
    )
    await core.discard_persistent_confirmation(
        windows.bind(record.namespace_id, record.agent_id, record.session_id, generation=generation),
        agent_id=record.agent_id, session_id=record.session_id, request_id=record.request_id,
        spec_fingerprint=fingerprint,
    )
    await asyncio.to_thread(approvals.consume, approval.approval_id)
