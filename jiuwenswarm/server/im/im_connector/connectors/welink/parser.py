"""Parsers for welink-cli stdout."""

from __future__ import annotations

import json
from typing import Any, Optional

from jiuwenswarm.server.im.im_connector.messages import to_im_message


def _normalize_message_id(value: Any) -> str:
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_fetched_message(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    sender = raw.get("sender")
    content = raw.get("content")
    if not isinstance(sender, str) or not sender.strip():
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    timestamp = raw.get("serverSendTime")
    try:
        ts_int = int(timestamp) if timestamp is not None else 0
    except (TypeError, ValueError):
        return None
    if ts_int <= 0:
        return None
    result: dict[str, Any] = {
        "sender": sender.strip(),
        "content": content,
        "serverSendTime": ts_int,
    }
    content_type = raw.get("contentType")
    if isinstance(content_type, str) and content_type.strip():
        result["contentType"] = content_type.strip()
    receiver = raw.get("receiver")
    if isinstance(receiver, str) and receiver.strip():
        result["receiver"] = receiver.strip()
    msg_id = _normalize_message_id(raw.get("msgId"))
    if msg_id:
        result["msgId"] = msg_id
    return result


def parse_history_messages(stdout: str) -> list[dict[str, Any]]:
    if not stdout or not stdout.strip():
        return []
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    resp_data = parsed.get("respData")
    chat_info = _as_record(resp_data).get("chatInfo") if isinstance(resp_data, dict) else None
    if not isinstance(chat_info, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in chat_info:
        normalized = normalize_fetched_message(item)
        if normalized is not None:
            messages.append(normalized)
    return messages


def parse_person_search_results(stdout: str) -> list[dict[str, Any]]:
    if not stdout or not stdout.strip():
        return []
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    search = _as_record(parsed.get("search_cli_person"))
    data = search.get("data") if isinstance(search, dict) else None
    if not isinstance(data, list):
        return []
    results: list[dict[str, Any]] = []
    for value in data:
        person = _as_record(value)
        if not person:
            continue
        user_account = person.get("w3account")
        if not isinstance(user_account, str) or not user_account.strip():
            continue
        welink_id = person.get("welinkId")
        if not isinstance(welink_id, str) or not welink_id.strip():
            continue
        user_account = user_account.strip()
        welink_id = welink_id.strip()
        name = person.get("name")
        if not isinstance(name, str) or not name.strip():
            name = person.get("userName")
        name = name.strip() if isinstance(name, str) else user_account
        results.append({
            "name": name or user_account,
            "user_account": user_account,
            "welink_id": welink_id,
        })
    return results


def parse_recent_conversations(stdout: str) -> list[dict[str, Any]]:
    if not stdout or not stdout.strip():
        return []
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    conversations = parsed.get("conversation_info")
    if not isinstance(conversations, list):
        return []
    results: list[dict[str, Any]] = []
    for value in conversations:
        row = _as_record(value)
        if not row:
            continue
        group_id = row.get("group_id")
        group_name = row.get("group_name")
        target_account = row.get("target_account")
        native_name = row.get("native_name") or ""
        group_id = group_id.strip() if isinstance(group_id, str) else ""
        group_name = group_name.strip() if isinstance(group_name, str) else ""
        target_account = target_account.strip() if isinstance(target_account, str) else ""
        native_name = native_name.strip() if isinstance(native_name, str) else ""
        if group_name and group_id:
            results.append({"kind": "group", "external_id": group_id, "title": group_name})
        elif group_id == "0" and target_account:
            results.append({"kind": "user", "external_id": target_account, "title": native_name})
    return results


def parse_auth_status_uid(stdout: str) -> str:
    if not stdout:
        return ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("uid"):
            _, _, rest = stripped.partition(":")
            if not rest:
                _, _, rest = stripped.partition(" ")
            token = rest.strip()
            if token:
                return token
    return ""


__all__ = [
    "parse_history_messages",
    "parse_person_search_results",
    "parse_auth_status_uid",
    "parse_recent_conversations",
    "normalize_fetched_message",
    "to_im_message",
]
