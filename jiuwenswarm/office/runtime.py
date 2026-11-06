# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The JiuwenSwarm facilities the Office backend reads: a conversation's directory, its files, its uploads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_gateway.common.protocol_ids import validate_session_id
from jiuwenswarm.common.utils import get_agent_sessions_dir


def session_dir(session_id: str) -> Path:
    return Path(get_agent_sessions_dir()) / validate_session_id(session_id)


def session_id_of(session: Any) -> str:
    """The conversation a rail callback or a tool call runs in, or ``""``."""
    getter = getattr(session, "get_session_id", None)
    return str(getter() or "").strip() if callable(getter) else ""


def asset_roots(session_id: str) -> tuple[Path, ...]:
    """Where a file the agent publishes into the presentation may come from.

    The conversation's project, the agent workspace, and the conversation's
    uploads (images the user attached in the task pane).
    """
    from jiuwenswarm.common.utils import get_agent_workspace_dir
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    metadata = get_session_metadata(session_id, enable_writeback=False) or {}
    project_dir = str(metadata.get("project_dir") or "").strip()
    roots = (project_dir, get_agent_workspace_dir(), session_dir(session_id) / "uploads")
    return tuple(Path(root) for root in roots if root)


def image_attachment(session_id: str, mime_type: str, base64_data: str) -> dict[str, Any]:
    """Store an image in the conversation's uploads, as a chat attachment is stored."""
    from jiuwenswarm.server.runtime.attachments.media_attachments import normalize_chat_media_attachments

    params = {"media_items": [{"type": "image", "mime_type": mime_type, "filename": "slide-render.png",
                               "base64_data": base64_data}]}
    normalize_chat_media_attachments(params, session_id)
    items = params.get("media_items") or []
    if len(items) != 1:
        raise ValueError("the image could not be stored with the conversation")
    return items[0]
