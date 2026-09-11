"""Map cloud/report conv_id back onto an existing AgentServer session.

Desktop ReportConversations uses ``toLocalConvId(session_id)`` (see claw_desktop
``conversation-report.ts``). Phone A2A continues with that ``conv_*`` as
``conversationId`` / inbound ``session_id``. Without reverse lookup, MessageHandler
always ``session.create``s a fresh ``xiaoyi_*`` session and loses PC history.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def to_local_conv_id(session_id: str) -> str:
    """Mirror claw_desktop ``toLocalConvId``: stable ``conv_`` + first 24 alnum chars."""
    raw = str(session_id or "").strip()
    if not raw:
        return ""
    if raw.startswith("conv_"):
        return raw
    compact = _ALNUM.sub("", raw)[:24]
    return f"conv_{compact}" if compact else ""


def _read_session_metadata_raw(session_dir: Path) -> dict[str, Any]:
    meta_path = session_dir / "metadata.json"
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("skip session meta %s: %s", session_dir.name, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _channel_meta_matches(meta: dict[str, Any], external_id: str) -> bool:
    channel_meta = meta.get("channel_metadata")
    if not isinstance(channel_meta, dict):
        return False
    for key in (
        "xiaoyi_conversation_id",
        "xiaoyi_session_id",
        "conversation_id",
        "external_session_id",
    ):
        if str(channel_meta.get(key) or "").strip() == external_id:
            return True
    return False


def _session_priority(session_id: str) -> int:
    """Prefer desktop-originated sessions when multiple map to the same conv_id."""
    sid = str(session_id or "")
    if sid.startswith("desktop_"):
        return 0
    if sid.startswith("xiaoyi_"):
        return 2
    return 1


def find_local_session_for_external_conv_id(
    external_id: str,
    *,
    sessions_dir: Path | None = None,
) -> str | None:
    """Return an existing local session_id for cloud ``conv_*``, or None.

    Match rules (any one):
    1. ``to_local_conv_id(session_id) == external_id``
    2. ``channel_metadata`` stores the same conversation / external id

    When several match, prefer ``desktop_*``, then newest ``last_message_at``.
    """
    key = str(external_id or "").strip()
    if not key.startswith("conv_"):
        return None

    if sessions_dir is None:
        from jiuwenswarm.common.utils import get_agent_sessions_dir

        sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.is_dir():
        return None

    candidates: list[tuple[int, float, str]] = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        sid = session_dir.name.strip()
        if not sid or sid == "default":
            continue

        matched = to_local_conv_id(sid) == key
        meta: dict[str, Any] = {}
        if not matched:
            meta = _read_session_metadata_raw(session_dir)
            if not meta:
                continue
            matched = _channel_meta_matches(meta, key)
        if not matched:
            continue

        if not meta:
            meta = _read_session_metadata_raw(session_dir)
        try:
            last_at = float(meta.get("last_message_at") or meta.get("created_at") or 0.0)
        except (TypeError, ValueError):
            last_at = 0.0
        candidates.append((_session_priority(sid), -last_at, sid))

    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def read_locked_session_mode(session_id: str) -> str:
    """读取会话磁盘锁定的 mode（metadata.json 的 ``mode`` 字段），缺失返回 ``""``。

    复用本机会话时 params 不注入 mode（保护锁定 mode 不被渠道默认覆盖），
    需要会话真实模式的判定（如 GodView 注册的 team 判定）以此为准。
    每次调用读盘（装团/退团会改写该字段，缓存会陈旧）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    from jiuwenswarm.common.utils import get_agent_sessions_dir

    meta = _read_session_metadata_raw(get_agent_sessions_dir() / sid)
    return str(meta.get("mode") or "").strip()
