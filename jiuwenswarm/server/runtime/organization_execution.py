# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Track host-relayed organization turns independently of foreground requests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator
from weakref import WeakValueDictionary


@dataclass(frozen=True)
class OrganizationTurn:
    session_id: str
    team_id: str
    request_id: str
    source: str


_turns: dict[asyncio.Task, OrganizationTurn] = {}
_stopping: set[tuple[str, str]] = set()
_stop_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def owner_is_stopped(session_id: str, team_id: str = "") -> bool:
    """Persist user intent across reconnects without suppressing child Teams."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    metadata = get_session_metadata(session_id) or {}
    if team_id and metadata.get("team_name") != team_id:
        return False
    return metadata.get("organization_owner_stopped") is True


def set_owner_stopped(session_id: str, stopped: bool) -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

    update_session_metadata(
        session_id=session_id, organization_owner_stopped=stopped,
        touch_last_message_at=False, sync_write=True,
    )


def is_stopping(session_id: str, team_id: str) -> bool:
    return (session_id, team_id) in _stopping


def register_turn(turn: OrganizationTurn) -> None:
    task = asyncio.current_task()
    if task is not None:
        _turns[task] = turn


def unregister_turn() -> None:
    """Remove only the current asyncio task, matching register_turn ownership."""
    task = asyncio.current_task()
    _turns.pop(task, None)


def active_turns(session_id: str) -> list[dict[str, str]]:
    return [
        {"team_id": turn.team_id, "request_id": turn.request_id, "source": turn.source}
        for task, turn in _turns.items()
        if turn.session_id == session_id and not task.done()
    ]


@asynccontextmanager
async def stopping_teams(session_id: str, team_ids: set[str]) -> AsyncIterator[list[dict[str, str]]]:
    """Prevent new host turns while cancelling actual runners and their queues."""
    lock = _stop_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        keys = {(session_id, team_id) for team_id in team_ids}
        _stopping.update(keys)
        try:
            cancelled = [item for item in active_turns(session_id) if item["team_id"] in team_ids]
            tasks = [
                task for task, turn in list(_turns.items())
                if (turn.session_id, turn.team_id) in keys and task is not asyncio.current_task() and not task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            yield cancelled
        finally:
            _stopping.difference_update(keys)
