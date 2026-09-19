# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Call-time artifact identity: turn placement vs Xiaoyi A2A routing.

History / desktop file runId use InvocationContext.request_id.
``xiaoyi_task_id`` stays in metadata for phone A2A and is never used as the
turn key. Toolkits must not treat cached ``self.request_id`` as the source
of truth when an invocation is bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _strip(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True, slots=True)
class ArtifactDelivery:
    turn_request_id: str
    session_id: str
    channel_id: str
    metadata: dict[str, Any]


def overlay_bound_runtime(
    *,
    fallback_channel_id: str | None,
    fallback_metadata: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Prefer the per-request ContextVar channel/metadata when bound."""
    channel_id = _strip(fallback_channel_id)
    metadata = dict(fallback_metadata) if isinstance(fallback_metadata, dict) else None
    try:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            _CRON_TOOL_BOUND,
            _CRON_TOOL_CHANNEL_ID,
            _CRON_TOOL_METADATA,
        )
    except Exception:
        return channel_id, metadata
    if not _CRON_TOOL_BOUND.get():
        return channel_id, metadata
    live_channel = _strip(_CRON_TOOL_CHANNEL_ID.get())
    if live_channel:
        channel_id = live_channel
    live_metadata = _CRON_TOOL_METADATA.get()
    if isinstance(live_metadata, dict):
        metadata = dict(live_metadata)
    return channel_id, metadata


def resolve_artifact_delivery(
    *,
    fallback_request_id: str | None,
    fallback_session_id: str | None,
    fallback_channel_id: str | None,
    metadata: dict[str, Any] | None,
) -> ArtifactDelivery:
    """Build history / send_push identity from the current invocation.

    Does not compare against ``xiaoyi_task_id``. Channel is not taken from
    invocation; overlay may still read live ``desktop``. After metadata merge,
    a sticky Xiaoyi identity forces ``send_push`` onto ``xiaoyi`` so ChannelManager
    does not drop PC-continuation artifacts.
    """
    invocation = None
    try:
        from jiuwenswarm.common.invocation_context import get_current_invocation_context

        invocation = get_current_invocation_context()
    except Exception:
        invocation = None

    turn_request_id = ""
    session_id = _strip(fallback_session_id)
    if invocation is not None:
        turn_request_id = _strip(getattr(invocation, "request_id", None))
        invocation_session = _strip(getattr(invocation, "session_id", None))
        if invocation_session:
            session_id = invocation_session
    else:
        logger.error(
            "[artifact-identity] invocation missing; falling back to toolkit identity "
            "session_id=%s",
            session_id or fallback_request_id,
        )

    if not turn_request_id:
        turn_request_id = _strip(fallback_request_id)

    channel_id = _strip(fallback_channel_id)
    merged: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    if turn_request_id:
        merged["request_id"] = turn_request_id

    sticky_task = _strip(merged.get("xiaoyi_task_id"))
    sticky_session = _strip(merged.get("xiaoyi_session_id"))
    if (sticky_task or sticky_session) and channel_id != "xiaoyi":
        logger.info(
            "[artifact-identity] force channel xiaoyi from=%s turn_request_id=%s "
            "xiaoyi_task_id=%s xiaoyi_session_id=%s",
            channel_id,
            turn_request_id,
            sticky_task,
            sticky_session,
        )
        channel_id = "xiaoyi"
    if sticky_task and sticky_task != turn_request_id:
        logger.info(
            "[artifact-identity] turn_request_id=%s xiaoyi_task_id=%s "
            "session_id=%s channel_id=%s",
            turn_request_id,
            sticky_task,
            session_id,
            channel_id,
        )
    return ArtifactDelivery(
        turn_request_id=turn_request_id,
        session_id=session_id,
        channel_id=channel_id,
        metadata=merged,
    )


def resolve_toolkit_delivery(toolkit: Any) -> ArtifactDelivery:
    """Send-path helper: live bound route + invocation turn id."""
    channel_id, metadata = overlay_bound_runtime(
        fallback_channel_id=getattr(toolkit, "channel_id", None),
        fallback_metadata=getattr(toolkit, "_request_metadata", None),
    )
    return resolve_artifact_delivery(
        fallback_request_id=getattr(toolkit, "request_id", None),
        fallback_session_id=getattr(toolkit, "session_id", None),
        fallback_channel_id=channel_id,
        metadata=metadata,
    )
