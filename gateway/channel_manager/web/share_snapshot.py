# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build personal export snapshots through the existing AgentServer history RPC."""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Request

from gateway.channel_manager.web.web_http_dispatch import dispatch_http_request
from gateway.channel_manager.web.web_http_timeouts import (
    resolve_web_http_history_timeout,
    resolve_web_http_unary_timeout,
)


def _check_response(frame: dict[str, Any]) -> None:
    if frame.get("type") == "res" and not frame.get("ok", True):
        code = str(frame.get("code") or "")
        status = 404 if code == "NOT_FOUND" else 403 if code in {"FORBIDDEN", "UNAUTHORIZED"} else 502
        raise HTTPException(status, str(frame.get("error") or "share_export_history_failed"))


async def _dispatch(request: Request, channel: Any, method: str, params: dict[str, Any], stream: bool):
    return await dispatch_http_request(
        channel, method=method, params=params, headers=request.headers,
        client_host=request.client.host if request.client else None,
        is_stream=stream, use_sse=stream,
    )


async def get_share_session_metadata(request: Request, channel: Any, session_id: str) -> dict[str, Any]:
    outbound, rid, _ = await _dispatch(request, channel, "session.get_metadata", {"session_id": session_id}, False)
    try:
        frame = await outbound.wait_response(rid, timeout=resolve_web_http_unary_timeout())
        _check_response(frame)
        metadata = frame.get("payload") or {}
        return metadata if isinstance(metadata, dict) else {}
    finally:
        await channel.unregister_request_outbound(outbound)


async def build_share_snapshot(
    request: Request, channel: Any, session_id: str, *, metadata: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Freeze all cursor pages, retaining split final text and chronological order."""
    records: list[dict[str, Any]] = []
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        page, done = await _history_page(request, channel, session_id, cursor)
        records.extend(page)
        if not done.get("has_more"):
            break
        cursor = done.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise HTTPException(502, "share_export_history_cursor_invalid")
        seen_cursors.add(cursor)
    records.reverse()
    title = metadata.get("title") if isinstance(metadata, dict) else None
    if not isinstance(title, str) or not title.strip():
        title = session_id
        for item in records:
            if item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                title = content.strip().replace("\n", " ")[:80]
                break
    filename = f"jiuwenswarm-share-{time.strftime('%Y%m%d-%H%M%S')}.png"
    return {
        "session_id": session_id,
        "metadata": {"title": title, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "filename": filename},
        "records": records,
    }, filename


async def _history_page(request: Request, channel: Any, session_id: str, cursor: str | None):
    outbound, rid, _ = await _dispatch(request, channel, "history.get", {
        "session_id": session_id, "cursor": cursor, "limit": 50,
    }, True)
    records: list[dict[str, Any]] = []
    parts: dict[str, tuple[int, dict[int, dict[str, Any]]]] = {}
    try:
        async for frame in outbound.iter_sse_frames(rid, timeout=resolve_web_http_history_timeout()):
            _check_response(frame)
            if frame.get("type") != "event":
                continue
            payload = frame.get("payload") or {}
            if frame.get("event") == "chat.error" or payload.get("status") == "error":
                raise HTTPException(502, str(payload.get("error") or "share_export_history_failed"))
            if frame.get("event") != "history.message":
                continue
            if payload.get("status") == "done":
                if parts:
                    raise HTTPException(502, "share_export_history_incomplete")
                return records, payload
            record = payload.get("message")
            if not isinstance(record, dict):
                continue
            part = record.get("_part")
            if not isinstance(part, dict):
                records.append(record)
                continue
            key, index, total = part.get("record_id"), part.get("part_idx"), part.get("total_parts")
            if not isinstance(key, str) or not key:
                raise HTTPException(502, "share_export_history_part_invalid")
            if type(index) is not int or type(total) is not int:
                raise HTTPException(502, "share_export_history_part_invalid")
            if not 0 <= index < total:
                raise HTTPException(502, "share_export_history_part_invalid")
            expected_total, bucket = parts.setdefault(key, (total, {}))
            if total != expected_total:
                raise HTTPException(502, "share_export_history_part_invalid")
            bucket[index] = record
            if len(bucket) == expected_total:
                # Bounded indexes and one fixed total guarantee all parts are present.
                ordered = [item for _, item in sorted(bucket.items())]
                merged = dict(ordered[0])
                merged.pop("_part", None)
                merged["content"] = "".join(str(item.get("content") or "") for item in ordered)
                records.append(merged)
                del parts[key]
        raise HTTPException(502, "share_export_history_incomplete")
    finally:
        await channel.unregister_request_outbound(outbound)
