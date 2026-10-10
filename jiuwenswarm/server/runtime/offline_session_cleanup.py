# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime-owned cleanup for persisted Sessions while AgentServer is offline."""

from __future__ import annotations

import logging

from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session_delete import SessionDeleteResult
from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry

logger = logging.getLogger(__name__)


async def delete_offline_session(
    *,
    channel_id: str,
    session_id: str,
) -> SessionDeleteResult:
    """Delete a local orphan through Runtime without attempting remote KVC work."""
    runtime = AgentRuntime(participant_registry=RuntimeParticipantRegistry())
    try:
        result = await runtime.delete_session(
            channel_id=channel_id,
            session_id=session_id,
        )
    except BaseException:
        try:
            await runtime.close()
        except BaseException:
            logger.warning(
                "Offline Session Runtime cleanup failed after delete error: session_id=%s",
                session_id,
                exc_info=True,
            )
        raise

    try:
        await runtime.close()
    except Exception:
        logger.warning(
            "Offline Session Runtime cleanup failed after delete completed: session_id=%s",
            session_id,
            exc_info=True,
        )
    return result


__all__ = ["delete_offline_session"]
