"""Parse lark-cli JSON into the shared message dict shape."""

from __future__ import annotations

import json
from typing import Any, Optional


from datetime import datetime, timedelta, timezone

_CN_TZ = timezone(timedelta(hours=8))


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_json(stdout: str) -> Any:
    if not stdout or not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except (TypeError, ValueError):
        return None


def _to_ms(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text[:1].isdigit() and "T" not in text and " " not in text and "-" not in text[4:5]:
            try:
                return _number_to_ms(float(text))
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(text[:19], fmt).replace(tzinfo=_CN_TZ)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0
    try:
        return _number_to_ms(float(value))
    except (TypeError, ValueError):
        return 0


def _number_to_ms(number: float) -> int:
    if number <= 0:
        return 0
    if number < 1e12:
        return int(number * 1000)
    return int(number)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            nested = _load_json(stripped)
            if isinstance(nested, dict):
                text = nested.get("text")
                if isinstance(text, str) and text.strip():
                    return text
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _pagination(payload: Any) -> tuple[Optional[str], bool]:
    if not isinstance(payload, dict):
        return None, False
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    token = (
        payload.get("page_token")
        or payload.get("nextPageToken")
        or nested.get("page_token")
        or nested.get("nextPageToken")
    )
    token_text = str(token).strip() if token not in (None, "") else ""
    has_more_raw = payload.get("has_more")
    if has_more_raw is None:
        has_more_raw = payload.get("hasMore")
    if has_more_raw is None:
        has_more_raw = nested.get("has_more") if nested else None
    if has_more_raw is None:
        has_more = bool(token_text)
    else:
        has_more = bool(has_more_raw)
    if not has_more:
        token_text = ""
    return token_text or None, has_more


def parse_history_page(stdout: str) -> tuple[list[dict[str, Any]], Optional[str], bool]:
    payload = _load_json(stdout)
    token, has_more = _pagination(payload)
    return parse_history_messages(stdout), token, has_more


def _data_record(payload: Any) -> dict[str, Any]:
    record = _as_record(payload)
    nested = _as_record(record.get("data"))
    return nested if nested else record


def parse_history_messages(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    if not isinstance(payload, dict):
        return []
    data = _data_record(payload)
    messages = data.get("messages")
    if not isinstance(messages, list):
        messages = data.get("items") if isinstance(data.get("items"), list) else []
    if not isinstance(messages, list):
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    results: list[dict[str, Any]] = []
    for item in messages:
        raw = _as_record(item)
        sender_obj = raw.get("sender")
        if isinstance(sender_obj, dict):
            sender = str(sender_obj.get("id") or sender_obj.get("sender_id") or "").strip()
            sender_name = str(sender_obj.get("name") or sender).strip()
        else:
            sender = str(sender_obj or "").strip()
            sender_name = sender
        content = _text_from_content(raw.get("content"))
        sent_at = _to_ms(raw.get("create_time") or raw.get("createTime"))
        msg_id = str(raw.get("message_id") or raw.get("msg_id") or "").strip()
        if not content or sent_at <= 0:
            continue
        if not sender:
            sender = "system"
            sender_name = sender_name or "system"
        results.append({
            "sender": sender,
            "senderName": sender_name,
            "content": content,
            "serverSendTime": sent_at,
            "msgId": msg_id,
            "contentType": str(raw.get("msg_type") or "") or None,
        })
    return results


def parse_person_search_results(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    if not isinstance(payload, dict):
        return []
    data = _data_record(payload)
    users = data.get("users")
    if not isinstance(users, list):
        users = data.get("items") if isinstance(data.get("items"), list) else []
    if not isinstance(users, list):
        users = payload.get("users") if isinstance(payload.get("users"), list) else []
    results: list[dict[str, Any]] = []
    for item in users:
        person = _as_record(item)
        account = str(
            person.get("open_id")
            or person.get("openId")
            or person.get("user_id")
            or person.get("userId")
            or ""
        ).strip()
        if not account:
            continue
        name = str(
            person.get("name")
            or person.get("localized_name")
            or person.get("display_name")
            or person.get("userName")
            or account
        ).strip()
        results.append({"user_account": account, "name": name, "open_id": account})
    return results


def parse_self_display_name(stdout: str, *, open_id: str) -> Optional[str]:
    """从 ``+search-user --user-ids me`` 取客户端昵称，优先 ``localized_name``。"""
    wanted = (open_id or "").strip()
    payload = _load_json(stdout)
    if not isinstance(payload, dict):
        return None
    data = _data_record(payload)
    users = data.get("users")
    if not isinstance(users, list):
        users = []
    for item in users:
        person = _as_record(item)
        account = str(
            person.get("open_id")
            or person.get("openId")
            or person.get("user_id")
            or ""
        ).strip()
        if wanted and account and account != wanted:
            continue
        localized = str(person.get("localized_name") or person.get("localizedName") or "").strip()
        if localized:
            return localized
        name = str(person.get("name") or person.get("display_name") or "").strip()
        if name:
            return name
    return None


def parse_recent_conversations(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    if not isinstance(payload, dict):
        return []
    data = _data_record(payload)
    chats = data.get("chats")
    if not isinstance(chats, list):
        chats = data.get("items") if isinstance(data.get("items"), list) else []
    if not isinstance(chats, list):
        chats = payload.get("chats") if isinstance(payload.get("chats"), list) else []
    results: list[dict[str, Any]] = []
    for item in chats:
        row = _as_record(item)
        mode = str(row.get("chat_mode") or row.get("chat_type") or "").strip().lower()
        name = str(row.get("name") or "").strip()
        if mode == "p2p":
            external_id = str(row.get("p2p_target_id") or row.get("chat_id") or "").strip()
            if external_id:
                results.append({"kind": "user", "external_id": external_id, "title": name})
        else:
            external_id = str(row.get("chat_id") or "").strip()
            if external_id:
                results.append({"kind": "group", "external_id": external_id, "title": name})
    return results


def parse_identity(stdout: str) -> Optional[dict[str, Any]]:
    payload = _load_json(stdout)
    if not isinstance(payload, dict):
        return None
    identities = _as_record(payload.get("identities"))
    user = _as_record(identities.get("user") or payload.get("user") or payload)
    account = str(
        user.get("open_id")
        or user.get("openId")
        or user.get("user_id")
        or user.get("union_id")
        or ""
    ).strip()
    name = str(
        user.get("name")
        or user.get("userName")
        or user.get("display_name")
        or account
    ).strip()
    if not account:
        return None
    return {"user_account": account, "name": name, "open_id": account}
