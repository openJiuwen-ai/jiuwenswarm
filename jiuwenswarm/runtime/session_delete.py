# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permanent Session deletion contracts owned by :class:`AgentRuntime`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from weakref import WeakValueDictionary

from jiuwenswarm.runtime.session_lifecycle import SessionLifecycleTarget


@dataclass(frozen=True, slots=True)
class SessionDeleteResult:
    """Transport-independent result of one permanent Session deletion."""

    ok: bool
    session_id: str
    channel_id: str | None = None
    is_team: bool = False
    team_name: str = ""
    deleted: bool = False
    recovery_required: bool = False
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def failure(
        cls,
        session_id: str,
        *,
        code: str,
        message: str,
        deleted: bool = False,
        recovery_required: bool = False,
        channel_id: str | None = None,
        is_team: bool = False,
        team_name: str = "",
    ) -> SessionDeleteResult:
        return cls(
            ok=False,
            session_id=session_id,
            channel_id=channel_id,
            is_team=is_team,
            team_name=team_name,
            deleted=deleted,
            recovery_required=recovery_required,
            error_code=code,
            error_message=message,
        )


@dataclass(frozen=True, slots=True)
class TeamDeleteResult:
    ok: bool
    team_name: str
    session_ids: tuple[str, ...] = ()
    failed_session_ids: tuple[str, ...] = ()
    deleted: bool = False
    recovery_required: bool = False
    error_code: str | None = None
    error_message: str | None = None


class TeamExecutionController(Protocol):
    async def quiesce_for_delete(
        self,
        target: SessionLifecycleTarget,
        *,
        reason: str,
    ) -> None:
        ...

    async def dispose_after_resource_release(
        self,
        target: SessionLifecycleTarget,
        *,
        reason: str,
    ) -> None:
        ...

    def delete_aborted(self, target: SessionLifecycleTarget) -> None:
        ...

    def delete_committed(self, target: SessionLifecycleTarget) -> None:
        ...


class TeamDeletionInProgress(RuntimeError):
    """A Team mutation raced with an in-process permanent deletion."""

    code = "TEAM_DELETING"


class TeamDeletionGate:
    """Process-local mutation fence shared by Team delete/create/bind paths."""

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._deleting: set[str] = set()

    def mutation_lock(self, team_name: str) -> asyncio.Lock:
        return self._locks.setdefault(team_name, asyncio.Lock())

    def begin_delete_locked(self, team_name: str) -> bool:
        created = team_name not in self._deleting
        self._deleting.add(team_name)
        return created

    def assert_not_deleting_locked(self, team_name: str) -> None:
        if team_name in self._deleting:
            raise TeamDeletionInProgress(
                f"TEAM_DELETING: team is being permanently deleted: {team_name}"
            )

    def finish_delete_locked(self, team_name: str) -> None:
        self._deleting.discard(team_name)


TEAM_DELETION_GATE = TeamDeletionGate()


_SESSION_DELETE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_TEAM_DELETE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def session_delete_lock(session_id: str) -> asyncio.Lock:
    return _SESSION_DELETE_LOCKS.setdefault(session_id, asyncio.Lock())


def team_delete_lock(team_name: str) -> asyncio.Lock:
    return _TEAM_DELETE_LOCKS.setdefault(team_name, asyncio.Lock())


__all__ = [
    "SessionDeleteResult",
    "TeamDeleteResult",
    "TeamDeletionInProgress",
    "TeamDeletionGate",
    "TeamExecutionController",
    "TEAM_DELETION_GATE",
    "session_delete_lock",
    "team_delete_lock",
]
