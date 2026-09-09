"""DingTalk cron delivery session binding helpers (Issue #2449).

Cron jobs created from DingTalk should push back to the originating
conversation, not the process-global ``last_*`` identity overwritten by
later inbound messages.
"""

from __future__ import annotations

from typing import Any

# Gateway 内部会话 ID 前缀（如 dingtalk_19fb740bf54_5d52cf），不能当作钉钉 staffId。
_INTERNAL_SESSION_PREFIXES = (
    "dingtalk_",
    "feishu_",
    "xiaoyi_",
    "wecom_",
    "wechat_",
    "whatsapp_",
    "telegram_",
    "discord_",
    "slack_",
    "sess_",
    "cron_",
    "web_",
    "tui_",
    "heartbeat_",
)


def is_usable_dingtalk_staff_id(value: str | None) -> bool:
    """Whether ``value`` can be passed to DingTalk private-message ``userIds``."""
    v = str(value or "").strip()
    if not v:
        return False
    # Delivery binding uses ``dingtalk::…``; never treat that whole string as staffId.
    if v.startswith("dingtalk::"):
        return False
    if v.startswith(_INTERNAL_SESSION_PREFIXES):
        return False
    return True


def encode_dingtalk_cron_session_id(
    *,
    sender_id: str,
    conversation_id: str = "",
    conversation_type: str = "1",
) -> str:
    """Encode originating DingTalk routing into ``CronJob.session_id``.

    Format: ``dingtalk::{conversation_id}::{sender_id}::{conversation_type}``
    """
    sender = (sender_id or "").strip()
    if not is_usable_dingtalk_staff_id(sender):
        return ""
    conv_id = (conversation_id or "").strip()
    conv_type = (conversation_type or "").strip() or "1"
    return f"dingtalk::{conv_id}::{sender}::{conv_type}"


def parse_dingtalk_cron_session_id(session_id: str) -> dict[str, str] | None:
    """Parse ``encode_dingtalk_cron_session_id`` output into send metadata.

    Returns ``None`` when ``session_id`` is not a DingTalk delivery binding
    (including Gateway internal ids like ``dingtalk_…`` and plain staff ids).
    """
    sid = (session_id or "").strip()
    if not sid.startswith("dingtalk::"):
        return None
    parts = sid.split("::")
    if len(parts) < 3:
        return None
    conv_id = str(parts[1] or "").strip()
    sender = str(parts[2] or "").strip()
    conv_type = str(parts[3] or "").strip() if len(parts) >= 4 else "1"
    if not sender and not conv_id:
        return None
    # Reject bindings that accidentally encoded an internal session id as staff.
    if sender and not is_usable_dingtalk_staff_id(sender):
        return None
    return {
        "dingtalk_sender_id": sender,
        "dingtalk_chat_id": conv_id,
        "conversation_id": conv_id,
        "conversation_type": conv_type or "1",
    }


def dingtalk_chat_type_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Map DingTalk ``conversation_type`` / ``chat_type`` to cron ``chat_type``."""
    if not isinstance(metadata, dict):
        return None
    chat_type = str(metadata.get("chat_type") or "").strip() or None
    if chat_type:
        return chat_type
    conv_type = str(metadata.get("conversation_type") or "").strip()
    if conv_type == "2":
        return "group"
    if conv_type == "1":
        return "p2p"
    return None


def build_dingtalk_cron_session_id_from_context(
    *,
    session_id: str | None,
    metadata: dict[str, Any] | None,
) -> str | None:
    """Build a bound DingTalk session id from cron tool context fields.

    Only real DingTalk staff ids are accepted as ``sender_id``. Gateway internal
    session ids (``dingtalk_…``) must never be encoded into the delivery binding.
    """
    md = metadata if isinstance(metadata, dict) else {}
    sender = str(md.get("dingtalk_sender_id") or "").strip()
    if not is_usable_dingtalk_staff_id(sender):
        alt = str(session_id).strip() if isinstance(session_id, str) else ""
        sender = alt if is_usable_dingtalk_staff_id(alt) else ""
    if not sender:
        return None
    conv_id = str(md.get("conversation_id") or md.get("dingtalk_chat_id") or "").strip()
    conv_type = str(md.get("conversation_type") or "").strip() or "1"
    encoded = encode_dingtalk_cron_session_id(
        sender_id=sender,
        conversation_id=conv_id,
        conversation_type=conv_type,
    )
    return encoded or None


def resolve_dingtalk_push_metadata(routing_sid: str) -> dict[str, str] | None:
    """Resolve cron push metadata from ``job.session_id``.

    - ``dingtalk::…`` delivery binding → originating conversation metadata
    - plain usable staff id → private chat to that user
    - Gateway internal session id (``dingtalk_…``) → ``None`` (caller may use last_*)
    """
    sid = (routing_sid or "").strip()
    if not sid:
        return None
    bound = parse_dingtalk_cron_session_id(sid)
    if bound is not None:
        return bound
    if is_usable_dingtalk_staff_id(sid):
        return {
            "dingtalk_sender_id": sid,
            "conversation_type": "1",
        }
    return None
