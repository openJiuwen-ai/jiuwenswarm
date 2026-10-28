# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Copy durable organization facts into the Owner's ordered result inbox."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox
from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped
from jiuwenswarm.server.runtime.session import session_metadata


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


async def capture_task_results(session_id: str, backend: Any, source_team: str = "") -> int:
    """Backfill saved results after a crash, including missed best-effort notifications."""
    owner = str((session_metadata.get_session_metadata(session_id) or {}).get("team_name") or "")
    store = owner_inbox()
    since = await asyncio.to_thread(store.enabled_at, session_id, owner)
    manager = getattr(backend, "org_task_manager", None)
    pool = getattr(manager, "task_pool", None)
    if since is None or pool is None:
        return 0
    limit = 100
    while True:
        tasks = await pool.list_tasks(limit=limit)
        if len(tasks) < limit:
            break
        limit *= 2
    copied = 0
    for task in sorted(tasks, key=lambda item: _timestamp(getattr(item, "updated_at", None))):
        assigned = str(getattr(getattr(task, "assignment", None), "team_id", "") or "")
        if not assigned or assigned == owner:
            continue
        if source_team and assigned != source_team:
            continue
        status = getattr(task, "status", "")
        status = str(getattr(status, "value", status)).lower()
        if status not in {"completed", "failed", "cancelled", "canceled"}:
            continue
        if _timestamp(getattr(task, "updated_at", None)) < since:
            continue
        data = task.model_dump(mode="json")
        snapshot = {}
        for key in (
            "task_id", "parent_task_id", "root_task_id", "title", "description", "status",
            "output_spec", "output_context", "output_abstract", "failure_code", "failure_reason",
        ):
            snapshot[key] = data.get(key)
        snapshot["organization_id"] = manager.organization_id
        snapshot["assigned_team_id"] = assigned
        await asyncio.to_thread(store.append, session_id, owner, {"kind": "task_result", "task": snapshot})
        copied += 1
    return copied


async def capture_child_completion(
    session_id: str, team_id: str, backend: Any, content: str, turn_id: str = "",
) -> None:
    owner = str((session_metadata.get_session_metadata(session_id) or {}).get("team_name") or "")
    store = owner_inbox()
    if not owner or owner == team_id:
        return
    if await asyncio.to_thread(store.enabled_at, session_id, owner) is None:
        if not owner_is_stopped(session_id, owner):
            return
        await asyncio.to_thread(store.enable, session_id, owner, 0.0)
    await capture_task_results(session_id, backend, team_id)
    manager = getattr(backend, "org_task_manager", None)
    if content.strip() and manager is not None:
        await asyncio.to_thread(store.append, session_id, owner, {
            "kind": "team_result", "from_team_id": team_id,
            "organization_id": manager.organization_id, "content": content, "turn_id": turn_id,
        })


async def capture_leader_messages(session_id: str, owner: str, manager: Any) -> None:
    store = owner_inbox()
    since = await asyncio.to_thread(store.enabled_at, session_id, owner)
    if since is None:
        return
    offset = 0
    pending = []
    while True:
        messages = await manager.message_service.list_leader_messages(
            team_id=owner, unread_only=True, limit=100, offset=offset,
        )
        pending.extend(messages)
        if len(messages) < 100:
            break
        offset += len(messages)
    for message in sorted(pending, key=lambda item: (_timestamp(item.get("created_at")), item["message_id"])):
        if _timestamp(message.get("created_at")) < since:
            continue
        snapshot = {}
        for key in (
            "message_id", "organization_id", "from_team_id", "to_team_id", "content", "metadata", "created_at",
        ):
            snapshot[key] = message.get(key)
        snapshot["created_at"] = _timestamp(message.get("created_at"))
        await asyncio.to_thread(
            store.append, session_id, owner, {"kind": "leader_message", "message": snapshot},
            "leader_message:" + message["message_id"],
        )


async def queue_owner_notification(
    session_id: str, owner_id: str, source: str, inputs: object,
) -> dict[str, Any] | None:
    if (session_metadata.get_session_metadata(session_id) or {}).get("team_name") != owner_id:
        return None
    query = inputs.get("query") if isinstance(inputs, dict) else None
    if not isinstance(query, str) or not query.strip():
        return None
    store = owner_inbox()
    if await asyncio.to_thread(store.enabled_at, session_id, owner_id) is None:
        if not owner_is_stopped(session_id, owner_id):
            return None
        # Migrate the earlier stop-only implementation without losing older saved results.
        await asyncio.to_thread(store.enable, session_id, owner_id, 0.0)
    match = re.search(r"(?:Leader message|completion notice|blocker notice) ([\w-]+)", query)
    native_id = match.group(1) if match else ""
    message_id = await asyncio.to_thread(
        store.append, session_id, owner_id,
        {"kind": "notification", "source": source, "query": query, "leader_message_id": native_id},
        "leader_message:" + native_id if native_id else "",
    )
    return await asyncio.to_thread(store.get_result, session_id, owner_id, message_id)
