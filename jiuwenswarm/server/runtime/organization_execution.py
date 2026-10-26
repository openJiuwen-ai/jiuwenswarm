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
