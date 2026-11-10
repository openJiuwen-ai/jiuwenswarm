# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session owner lookup shared by adapter assembly and Cron routing."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def resolve_cron_session_identity(
    execution_request: Any | None,
    session_id: str,
) -> tuple[str, str]:
    """Resolve the authenticated request owner, falling back to session metadata.

    The Gateway remains the authority that validates Cron ownership; this
    function only carries the existing owner into the session tool context.
    """
    sid = str(session_id or "").strip()
    user_id = ""
    if execution_request is not None:
        user_id = str(getattr(execution_request, "user_id", "") or "").strip()
    if not user_id and sid:
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            metadata = get_session_metadata(sid) or {}
            user_id = str(metadata.get("user_id") or "").strip()
            if not user_id:
                channel_metadata = metadata.get("channel_metadata")
                if isinstance(channel_metadata, dict):
                    user_id = str(channel_metadata.get("user_id") or "").strip()
        except Exception:  # noqa: BLE001 - lookup must not block adapter creation
            logger.debug(
                "Cron owner lookup failed: session_id=%s", sid, exc_info=True,
            )
    return sid, user_id
