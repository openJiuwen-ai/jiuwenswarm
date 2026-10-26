# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parse welink-cli JSON for recent groups and history messages.

The CLI document does not publish the history schema. Parsers accept the
field names used by the command flags and a few common aliases.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, kw_only=True)
class WelinkMedia:
    """One image or file carried inside a WeLink message. The share URL stays off logs."""

    kind: str
    name: str
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    url: str = ""
    access_code: str = ""


@dataclass(frozen=True, kw_only=True)
class WelinkHistoryMessage:
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    group_id: str
    media: tuple[WelinkMedia, ...] = ()


_MESSAGE_ID_KEYS = ("messageId", "message_id", "msgId", "msg_id", "id")
_SENDER_KEYS = (
    "senderAccount",
    "sender_account",
    "userAccount",
    "user_account",
    "fromAccount",
    "from_account",
    "account",
    "sender",
    "from",
)
_NAME_KEYS = ("senderName", "sender_name", "userName", "user_name", "name", "fromName")
_TEXT_KEYS = ("text", "content", "body", "msg", "message")
_GROUP_KEYS = ("groupId", "group_id", "groupID", "chatId", "chat_id")
_GROUP_KINDS = {"group", "groupchat", "group_chat", "0", "1", "chat_type_group_msg", "group_msg", "normal_group"}
_DIRECT_KINDS = {"p2p", "single", "user", "private", "direct", "c2c", "chat_type_p2p_msg", "p2p_msg"}
_MEDIA_LABELS = {
    "PICTURE_MSG": "图片",
    "IMAGESPAN_MSG": "图片",
    "IMAGE_MSG": "图片",
    "IMG_MSG": "图片",
    "FILE_MSG": "文件",
    "VIDEO_MSG": "视频",
    "AUDIO_MSG": "音频",
}
_KIND_LABELS = {
    "img": "图片",
    "image": "图片",
    "picture": "图片",
    "file": "文件",
    "video": "视频",
    "audio": "音频",
}
_UM_BLOCK = re.compile(r"/:um_begin\{(.*?)\}/:um_end", re.DOTALL)
_ITEM_LIST_KEYS = (
    "conversation_info",
    "conversationInfo",
    "chatInfo",
    "chat_info",
    "messages",
    "messageList",
    "list",
    "conversations",
    "conversationList",
    "items",
    "respData",
    "resp_data",
    "data",
    "result",
)


def parse_cli_json(stdout: str) -> Any:
    """Return the first JSON value in a CLI stdout blob."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return value


@dataclass(frozen=True)
class WelinkGroupMember:
    account: str
    member_type: str


def parse_group_members(payload: Any) -> list[WelinkGroupMember]:
    """Members from ``im query-group-member``."""
    rows = payload.get("respData") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        for key in ("members", "memberList", "member_list", "data"):
            if isinstance(rows.get(key), list):
                rows = rows[key]
                break
    if not isinstance(rows, list):
        return []
    members: list[WelinkGroupMember] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        account = str(item.get("memberAccount") or item.get("account") or "").strip()
        member_type = str(item.get("memberType") or item.get("type") or "").strip()
        if account:
            members.append(WelinkGroupMember(account=account, member_type=member_type))
    return members


def reader_identity(account: str) -> str:
    """Employee or public-account token embedded in that account's assistant id."""
    value = account.strip()
    if len(value) > 1 and value[0].isalpha() and value[1:].isdigit():
        return value[1:]
    if "_" in value:
        prefix, remainder = value.split("_", 1)
        if len(prefix) == 1 and remainder:
            return remainder
    return value


def assistant_owner(account: str) -> str:
    parts = account.strip().split("_")
    if len(parts) >= 3 and parts[0] == "dig" and parts[1]:
        return parts[1]
    return ""


def reader_assistant_accounts(members: list[WelinkGroupMember], reader: str) -> set[str]:
    identity = reader_identity(reader)
    if not identity:
        return set()
    return {
        member.account
        for member in members
        if member.member_type == "TYPE_AGENT_ASSISTANT" and assistant_owner(member.account) == identity
    }


def shares_reader_assistant(members: list[WelinkGroupMember], reader: str) -> bool:
    """True only when the reading account and its own assistant are both members."""
    reader_account = reader.strip()
    if not reader_account or not any(member.account == reader_account for member in members):
        return False
    return bool(reader_assistant_accounts(members, reader_account))


def parse_recent_group_ids(payload: Any) -> list[str]:
    """Group ids from ``im query-recent-conversation``."""
    groups: list[str] = []
    for item in _as_items(payload):
        if not isinstance(item, dict):
            continue
        kind = str(
            item.get("recent_conversation_type")
            or item.get("recentConversationType")
            or item.get("type")
            or item.get("chatType")
            or item.get("conversationType")
            or item.get("sessionType")
            or item.get("group_type")
            or item.get("groupType")
            or ""
        ).strip().lower()
        group_id = _first_text(item, _GROUP_KEYS)
        if not group_id:
            if kind in _GROUP_KINDS:
                group_id = _first_text(item, ("id", "conversationId", "conversation_id"))
        if not group_id or group_id == "0" or kind in _DIRECT_KINDS:
            continue
        if kind and kind not in _GROUP_KINDS and _first_text(item, ("userAccount", "user_account", "receiver")):
            continue
        groups.append(group_id)
    return list(dict.fromkeys(groups))


def parse_history_messages(payload: Any, *, group_id: str) -> list[WelinkHistoryMessage]:
    """Messages from ``im query-history-message``. Order follows the payload."""
    messages: list[WelinkHistoryMessage] = []
    for item in _as_items(payload):
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("contentType") or item.get("content_type") or "").strip().upper()
        raw = _message_text(item)
        if raw is None:
            continue
        text, media = message_text_and_media(raw, content_type)
        kept = (not content_type) or ("TEXT" in content_type) or content_type in _MEDIA_LABELS or bool(media)
        if not kept or not text:
            continue
        message_id = _first_text(item, _MESSAGE_ID_KEYS)
        sender_id = _sender_id(item)
        if not message_id or not sender_id:
            continue
        name = _first_text(item, _NAME_KEYS) or _nested_text(item.get("sender"), _NAME_KEYS) or sender_id
        messages.append(
            WelinkHistoryMessage(
                message_id=message_id,
                sender_id=sender_id,
                sender_name=name,
                text=text,
                group_id=group_id,
                media=media,
            )
        )
    return messages


def select_new_messages(
    messages: list[WelinkHistoryMessage],
    cursor: str | None,
    *,
    account: str,
    ignore_senders: set[str] | None = None,
) -> tuple[list[WelinkHistoryMessage], str | None]:
    """Keep the reading account's own texts. Drop the assistant echo and anything at or before the cursor.

    The employee account only reads history for the colleague to analyze, so a
    message that person typed in the group is inbound, not an echo. Numeric
    message ids are treated as monotonic. The first time a group is seen, its
    current messages are inbound too: the first text after a group is created
    has no older cursor to stand on. The caller keeps only the newest few.
    """
    reader = account.strip()
    ignored = {
        item.strip()
        for item in (ignore_senders or set())
        if item and item.strip() and item.strip() != reader
    }
    if not cursor:
        deliver = [item for item in messages if item.sender_id.strip() not in ignored]
        if deliver and all(item.message_id.isdigit() for item in deliver):
            deliver.sort(key=lambda item: int(item.message_id))
        return deliver, _newest_id(messages)
    if cursor.isdigit() and messages and all(item.message_id.isdigit() for item in messages):
        ahead = [item for item in messages if int(item.message_id) > int(cursor)]
        deliver = [item for item in ahead if item.sender_id.strip() not in ignored]
        deliver.sort(key=lambda item: int(item.message_id))
        advanced = max(ahead, key=lambda item: int(item.message_id)).message_id if ahead else cursor
        return deliver, advanced
    deliver = [item for item in messages if item.message_id != cursor and item.sender_id.strip() not in ignored]
    advanced = messages[-1].message_id if messages and messages[-1].message_id != cursor else cursor
    if deliver:
        advanced = deliver[-1].message_id
    return deliver, advanced


def parse_auth_status(stdout: str, stderr: str = "") -> tuple[bool, str]:
    """Best-effort login flag and account from ``auth status`` text."""
    blob = f"{stdout or ''}\n{stderr or ''}"
    payload = parse_cli_json(stdout)
    account = _account_from_payload(payload) or _account_from_text(blob)
    lowered = blob.lower()
    if re.search(r"未登录|未授权|not authenticated|not logged|logged out|unauthorized|login required", lowered):
        return False, account
    uid_match = re.search(r"(?im)^\s*UID:\s*(\S+)\s*$", blob)
    if uid_match and not account:
        account = uid_match.group(1)
    token_match = re.search(r"(?i)User Token:\s*(\S+)", blob)
    has_user_token = bool(token_match and token_match.group(1).lower() not in {"none", "null", "-"})
    logged_in = has_user_token or bool(
        re.search(r"(?<!not )(?:已登录|登录成功|授权成功|logged in|authenticated|authorized)", lowered)
    )
    if isinstance(payload, dict):
        for key in ("loggedIn", "logged_in", "authorized", "isLogin"):
            if payload.get(key) is True:
                logged_in = True
            elif payload.get(key) is False:
                return False, account
        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"ok", "online", "success", "logged_in", "authorized"}:
            logged_in = True
    return logged_in, account


def extract_login_prompt(text: str) -> tuple[str, str]:
    """Return a login URL and QR-looking text from an ``auth login`` transcript."""
    redacted = redact_secrets(text or "")
    urls = re.findall(r"https?://[^\s\"']+", redacted)
    preferred = [url for url in urls if any(part in url.lower() for part in ("welink", "oauth", "login", "auth"))]
    qr_url = (preferred or urls or [""])[0]
    marks = sum(redacted.count(char) for char in "█▀▄▌▐▓")
    qr_text = redacted if marks >= 12 or "▄▄" in redacted or "▀▀" in redacted else ""
    return qr_url, qr_text


def message_text_and_media(content: str, content_type: str = "") -> tuple[str, tuple[WelinkMedia, ...]]:
    """Turn a WeLink body into readable text plus image or file records.

    Picture and file bodies use ``/:um_begin{url|kind|size|name|flag|width;height;hash|extras}/:um_end``.
    The share URL is kept on the media record for a later download and is removed from the text.
    """
    label = _MEDIA_LABELS.get(content_type.strip().upper(), "")
    media = tuple(item for item in (_media_from_um(block, label) for block in _UM_BLOCK.findall(content or "")) if item)
    if media:
        caption = _UM_BLOCK.sub("", content or "").strip()
        text = "\n".join(part for part in (*(describe_media(item) for item in media), caption) if part)
        return text, media
    if not label:
        return content, ()
    name = (content or "").strip()
    url = name if name.startswith(("http://", "https://")) else ""
    if url or len(name) > 120:
        name = ""
    described = f"[{label}] {name}".strip()
    record = WelinkMedia(kind=label, name=name, url=url)
    return described, (record,) if (name or url) else ()


def describe_media(item: WelinkMedia) -> str:
    """One line a colleague can read. No share URL."""
    label = _KIND_LABELS.get(item.kind.strip().lower(), "") or _MEDIA_LABELS.get(item.kind.strip().upper(), "") or item.kind or "文件"
    parts = [f"[{label}]"]
    if item.name:
        parts.append(item.name)
    if item.width and item.height:
        parts.append(f"{item.width}×{item.height}")
    if item.size_bytes:
        parts.append(_format_size(item.size_bytes))
    return " ".join(parts)


def is_image_media(item: WelinkMedia) -> bool:
    label = _KIND_LABELS.get(item.kind.strip().lower(), "") or _MEDIA_LABELS.get(item.kind.strip().upper(), "")
    if label == "图片":
        return True
    suffix = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
    return suffix in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def redact_secrets(text: str) -> str:
    return re.sub(
        r"(?i)(access_token|refresh_token|token|secret|authorization)(['\"\s:=]+)[^\s,'\"}]+",
        r"\1\2[redacted]",
        text or "",
    )


def _account_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("account", "userAccount", "user_account", "userId", "user_id", "uid"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("user", "data", "result"):
        nested = _account_from_payload(payload.get(nested_key))
        if nested:
            return nested
    return ""


def _account_from_text(blob: str) -> str:
    match = re.search(
        r"(?i)(?:account|userAccount|user_account|w3账号|账号)\s*[:：=]\s*([A-Za-z0-9._-]{2,64})",
        blob,
    )
    return match.group(1) if match else ""


def _as_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _ITEM_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _as_items(value)
            if nested:
                return nested
    return [payload]


def _sender_id(item: dict[str, Any]) -> str:
    direct = _first_text(item, _SENDER_KEYS)
    if direct and not direct.startswith("{") and len(direct) <= 128:
        return direct
    for key in ("sender", "from", "fromUser", "user"):
        nested = item.get(key)
        found = _first_text(nested, _SENDER_KEYS + ("id", "account"))
        if found:
            return found
    return ""


def _message_text(item: dict[str, Any]) -> str | None:
    for key in _TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = _message_text(value)
            if nested is not None:
                return nested
    return None


def _nested_text(value: Any, keys: tuple[str, ...]) -> str:
    return _first_text(value, keys)


def _first_text(value: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, int) and not isinstance(raw, bool):
            return str(raw)
    return ""


def _media_from_um(inner: str, label: str) -> WelinkMedia | None:
    parts = inner.split("|")
    if len(parts) < 2:
        return None
    url = parts[0].strip()
    kind = parts[1].strip() or label
    size = _int_or_none(parts[2]) if len(parts) > 2 else None
    name = parts[3].strip() if len(parts) > 3 else ""
    width = height = None
    access_code = ""
    if len(parts) > 5 and ";" in parts[5]:
        dims = parts[5].split(";")
        width = _int_or_none(dims[0])
        height = _int_or_none(dims[1]) if len(dims) > 1 else None
        if len(dims) > 2:
            access_code = dims[2].strip()
    if url.startswith(("http://", "https://")):
        share = url
    else:
        share = ""
    if not name and share:
        name = share.rsplit("/", 1)[-1][:80]
    if not kind and not name and not share:
        return None
    return WelinkMedia(
        kind=kind or label or "文件",
        name=name,
        size_bytes=size,
        width=width,
        height=height,
        url=share,
        access_code=access_code,
    )


def _int_or_none(value: str) -> int | None:
    text = value.strip()
    if not text.isdigit():
        return None
    return int(text)


def _format_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f}MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f}KB"
    return f"{size}B"


def _newest_id(messages: list[WelinkHistoryMessage]) -> str | None:
    if not messages:
        return None
    if all(item.message_id.isdigit() for item in messages):
        return max(messages, key=lambda item: int(item.message_id)).message_id
    return messages[-1].message_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
