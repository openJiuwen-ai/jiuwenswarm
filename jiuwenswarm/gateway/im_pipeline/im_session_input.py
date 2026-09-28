# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared IM handoff onto the public Session input entry.

Platform connectors keep message normalization, sender identity, and target
session association. Every IM platform sends ``chat.send``. This module is
the only place that decides execution control from Session state: text that
arrives while that Session is already processing becomes ``steer`` and must
not cancel the active turn. Idle Sessions keep an ordinary new turn.
Interaction answers stay on the existing resume path.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.runtime.session_input import SessionInputMode, resolve_session_input_mode

_SHARED_IM_CHANNELS = frozenset({
    "feishu",
    "wecom",
    "dingtalk",
    "slack",
    "discord",
    "telegram",
    "wechat",
    "whatsapp",
    "xiaoyi",
})
_INTENT_KEYS = ("input_mode", "runtime_mode", "expected_execution_id")
_ATTACHMENT_KEYS = (
    "images",
    "image_files",
    "files",
    "attachments",
    "audio_files",
    "video_files",
)


def is_shared_im_channel(channel_id: object) -> bool:
    """Return whether this channel reuses the shared IM session-input path."""
    channel = str(channel_id or "").strip().lower()
    if channel in _SHARED_IM_CHANNELS:
        return True
    if channel.startswith("feishu:") or channel.startswith("feishu_enterprise:"):
        return True
    return False


def prepare_im_session_input(msg: Message) -> bool:
    """Normalize an already-identified IM supplement for public delivery.

    Returns True when the message is explicit session input. A True result
    means callers must not treat the text as a new turn, a slash command, or
    the answer to a pending question.
    """
    if not is_shared_im_channel(getattr(msg, "channel_id", "")):
        return False
    if not _is_chat_send(msg):
        return False
    if _has_interaction_answers(msg):
        return False

    params = dict(msg.params) if isinstance(msg.params, dict) else {}
    _promote_missing_intent(params, _metadata(msg))
    try:
        mode = resolve_session_input_mode(params)
    except ValueError:
        msg.params = params
        return False
    if mode is None:
        return False
    _write_normalized_mode(params, mode)
    msg.params = params
    return True


def steer_busy_im_chat(msg: Message) -> bool:
    """Mark an ordinary IM ``chat.send`` as steer.

    The caller has already resolved the target Session and observed that it
    is processing. Idle messages must not call this. Resume answers,
    attachments, and team turns stay on their existing paths.
    """
    if not is_shared_im_channel(getattr(msg, "channel_id", "")):
        return False
    if not _is_chat_send(msg):
        return False
    if _has_interaction_answers(msg):
        return False
    if _is_resume_message(msg):
        return False
    if _has_attachments(msg):
        return False
    params = dict(msg.params) if isinstance(msg.params, dict) else {}
    if is_team_mode(params.get("mode")):
        return False
    try:
        existing = resolve_session_input_mode(params)
    except ValueError:
        return False
    if existing is not None:
        return False
    text = params.get("query") if isinstance(params.get("query"), str) else params.get("content")
    if not isinstance(text, str) or not text.strip():
        return False
    if not isinstance(params.get("query"), str) or not params["query"].strip():
        params["query"] = text
    params["input_mode"] = SessionInputMode.STEER.value
    params.pop("runtime_mode", None)
    msg.params = params
    return True


def _is_chat_send(msg: Message) -> bool:
    method = getattr(msg, "req_method", None)
    value = getattr(method, "value", method)
    return value == "chat.send"


def _has_interaction_answers(msg: Message) -> bool:
    params = msg.params if isinstance(msg.params, dict) else {}
    return bool(params.get("answers"))


def _is_resume_message(msg: Message) -> bool:
    metadata = _metadata(msg)
    if metadata.get("is_resume_message"):
        return True
    return bool(str(metadata.get("dm_pending_interaction_id") or "").strip())


def _has_attachments(msg: Message) -> bool:
    params = msg.params if isinstance(msg.params, dict) else {}
    for key in _ATTACHMENT_KEYS:
        if params.get(key):
            return True
    return False


def _metadata(msg: Message) -> dict[str, Any]:
    metadata = getattr(msg, "metadata", None)
    if isinstance(metadata, dict):
        return metadata
    return {}


def _promote_missing_intent(params: dict[str, Any], metadata: dict[str, Any]) -> None:
    for key in _INTENT_KEYS:
        if key in params:
            continue
        if key not in metadata:
            continue
        params[key] = metadata[key]


def _write_normalized_mode(params: dict[str, Any], mode: SessionInputMode) -> None:
    params["input_mode"] = mode.value
    params.pop("runtime_mode", None)
