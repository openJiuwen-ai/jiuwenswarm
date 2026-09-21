"""Parse dws JSON into the shared message dict shape."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
                number = float(text)
            except ValueError:
                number = None
            if number is not None:
                return _number_to_ms(number)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text[:19], fmt).replace(tzinfo=_CN_TZ)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        if text.endswith("Z") or "+" in text[10:]:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except ValueError:
                return 0
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


def _iter_rows(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    data = _as_record(payload.get("data") or payload.get("result"))
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    if isinstance(data, list):
        return data
    return []


def _content_text(raw: dict[str, Any]) -> str:
    text = raw.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = raw.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


def _pagination(payload: Any) -> tuple[Optional[str], bool]:
    if not isinstance(payload, dict):
        return None, False
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    token = (
        payload.get("nextPageToken")
        or payload.get("page_token")
        or payload.get("next_page_token")
        or nested.get("nextPageToken")
        or nested.get("page_token")
        or result.get("nextPageToken")
        or result.get("page_token")
    )
    token_text = str(token).strip() if token not in (None, "") else ""
    has_more_raw = payload.get("hasMore")
    if has_more_raw is None:
        has_more_raw = payload.get("has_more")
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


def parse_history_messages(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    rows = _iter_rows(payload, "messages", "items", "list")
    results: list[dict[str, Any]] = []
    for item in rows:
        raw = _as_record(item)
        sender_obj = raw.get("sender")
        sender_id = str(raw.get("senderId") or raw.get("sender_id") or "").strip()
        sender_name = ""
        if isinstance(sender_obj, dict):
            sender_id = str(
                sender_obj.get("openDingTalkId")
                or sender_obj.get("userId")
                or sender_obj.get("id")
                or sender_id
            ).strip()
            sender_name = str(sender_obj.get("name") or "").strip()
        elif isinstance(sender_obj, str):
            sender_name = sender_obj.strip()
        content = _content_text(raw)
        sent_at = _to_ms(raw.get("createTime") or raw.get("create_time") or raw.get("sendTime"))
        msg_id = str(raw.get("messageId") or raw.get("msgId") or "").strip()
        if not sender_id or not content.strip() or sent_at <= 0:
            continue
        results.append({
            "sender": sender_id,
            "senderName": sender_name or sender_id,
            "content": content,
            "serverSendTime": sent_at,
            "msgId": msg_id,
        })
    return results


def parse_person_search_results(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    rows = _iter_rows(payload, "users", "items", "list", "result")
    results: list[dict[str, Any]] = []
    for item in rows:
        person = _as_record(item)
        model = _as_record(person.get("orgEmployeeModel") or person)
        user_id = str(
            person.get("userId") or model.get("userId") or model.get("orgUserId") or ""
        ).strip()
        open_id = str(person.get("openDingTalkId") or "").strip()
        account = user_id or open_id
        if not account:
            continue
        name = str(
            person.get("name") or person.get("nick") or model.get("orgUserName") or account
        ).strip()
        results.append({
            "user_account": account,
            "name": name,
            "user_id": user_id,
            "open_dingtalk_id": open_id,
        })
    return results


def parse_recent_conversations(stdout: str) -> list[dict[str, Any]]:
    payload = _load_json(stdout)
    rows = _iter_rows(payload, "chats", "conversations", "items", "list")
    results: list[dict[str, Any]] = []
    for item in rows:
        row = _as_record(item)
        mode = str(row.get("conversationType") or row.get("chatMode") or "").strip().lower()
        title = str(row.get("title") or row.get("name") or row.get("groupName") or "").strip()
        if mode in {"p2p", "direct"}:
            external_id = str(
                row.get("openDingTalkId")
                or row.get("openDingtalkId")
                or row.get("userId")
                or row.get("openConversationId")
                or ""
            ).strip()
            kind = "user"
        else:
            external_id = str(row.get("openConversationId") or row.get("conversationId") or "").strip()
            kind = "group"
        if external_id:
            results.append({"kind": kind, "external_id": external_id, "title": title})
    return results


def parse_identity(stdout: str) -> Optional[dict[str, Any]]:
    payload = _load_json(stdout)
    rows = _iter_rows(payload, "result", "items")
    record = rows[0] if rows else payload
    person = _as_record(record)
    model = _as_record(person.get("orgEmployeeModel") or person)
    account = str(
        model.get("userId") or model.get("orgUserId") or person.get("userId") or ""
    ).strip()
    name = str(model.get("orgUserName") or person.get("name") or account).strip()
    if not account:
        return None
    return {"user_account": account, "name": name, "user_id": account}
