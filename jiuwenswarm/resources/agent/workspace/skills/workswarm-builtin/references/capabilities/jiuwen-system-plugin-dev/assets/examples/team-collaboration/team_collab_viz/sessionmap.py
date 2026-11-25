# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map sessions to teams using observed events and verified default names.

Observed pairs are persisted through ``ops.state``. When a session has no
recorded pair, probe the Swarm default name through ``ops.team.info``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.events import EventHandlerOps

LOGGER = logging.getLogger(__name__)

PACKAGE_ID = "webui.team-collab-viz"
STATE_KEY_PREFIX = "session-team:"
SOURCE_EVENT = "event"
SOURCE_DEFAULT_NAME = "default_name"
SOURCE_NONE = "none"


def _attr(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing host object."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class SessionMap:
    """In-memory ``sid ↔ team`` pairs with defensive persistence and fallback."""

    def __init__(self) -> None:
        """Start with no learned pairs."""
        self._lock = threading.Lock()
        self._by_session: dict[str, str] = {}
        self._by_team: dict[str, str] = {}

    def learn(self, session_id: str | None, team_name: str | None) -> bool:
        """Record an observed pair; returns ``True`` when it is new information."""
        if not isinstance(session_id, str) or not session_id:
            return False
        if not isinstance(team_name, str) or not team_name:
            return False
        with self._lock:
            changed = self._by_session.get(session_id) != team_name
            self._by_session[session_id] = team_name
            self._by_team[team_name] = session_id
            return changed

    def team_of(self, session_id: str | None) -> str | None:
        """Team learned for ``session_id`` (``None`` when unknown)."""
        if not isinstance(session_id, str) or not session_id:
            return None
        with self._lock:
            return self._by_session.get(session_id)

    def session_of(self, team_name: str | None) -> str | None:
        """Session the team was observed in; used as the task-table lookup key."""
        if not isinstance(team_name, str) or not team_name:
            return None
        with self._lock:
            return self._by_team.get(team_name)

    def known(self) -> dict[str, str]:
        """Snapshot of the learned pairs (plain copy, for diagnostics)."""
        with self._lock:
            return dict(self._by_session)

    def clear(self) -> None:
        """Forget every pair (tests and reload hygiene)."""
        with self._lock:
            self._by_session.clear()
            self._by_team.clear()

    # ------------------------------------------------------------------ state

    @staticmethod
    def _state_key(session_id: str) -> str:
        return STATE_KEY_PREFIX + session_id

    async def restore(self, ops: Any, session_id: str | None) -> str | None:
        """Read a previously persisted pair; never raises."""
        if not isinstance(session_id, str) or not session_id:
            return None
        try:
            from openjiuwen.core.extension.data import StateRef
        except Exception:  # pragma: no cover - host without state data module
            return None
        try:
            ref = StateRef(PACKAGE_ID, self._state_key(session_id), "application")
            snapshot = await ops.state.get(ref)
            value = getattr(snapshot, "value", None)
            team_name = _attr(value, "team_name") if value is not None else None
            if isinstance(team_name, str) and team_name:
                self.learn(session_id, team_name)
                return team_name
        except Exception as error:  # noqa: BLE001 - persistence is best effort
            LOGGER.debug("team-collab-viz: state.get failed for %r: %r", session_id, error)
        return None

    async def persist(self, ops: Any, session_id: str | None, team_name: str | None) -> None:
        """Persist a learned pair; never raises."""
        if not isinstance(session_id, str) or not session_id:
            return
        if not isinstance(team_name, str) or not team_name:
            return
        try:
            from openjiuwen.core.extension.data import StateRef
        except Exception:  # pragma: no cover - host without state data module
            return
        try:
            ref = StateRef(PACKAGE_ID, self._state_key(session_id), "application")
            await ops.state.set(ref, {"team_name": team_name, "learned_at": int(time.time() * 1000)})
        except Exception as error:  # noqa: BLE001 - persistence is best effort
            LOGGER.debug("team-collab-viz: state.set failed for %r: %r", session_id, error)

    # --------------------------------------------------------------- fallback

    @staticmethod
    async def _find_default_team(ops: EventHandlerOps, session_id: str) -> str | None:
        """Confirm that the current session's default team exists."""
        suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id.strip()).strip("._-")
        if not suffix:
            return None
        candidate = f"jiuwen_team_{suffix}"
        try:
            info = await ops.team.info(team_name=candidate)
        except BaseError as error:
            if (
                error.status == StatusCode.AGENT_TEAM_STATE_INVALID
                and error.params.get("reason") == f"team '{candidate}' not found"
            ):
                return None
            raise
        if _attr(info, "team_name") == candidate:
            return candidate
        return None

    async def resolve(self, ops: EventHandlerOps, session_id: str | None) -> tuple[str | None, str]:
        """Resolve a team from events, persistent state, or the default name."""
        team_name = self.team_of(session_id)
        if team_name:
            return team_name, SOURCE_EVENT
        if not isinstance(session_id, str) or not session_id:
            return None, SOURCE_NONE
        restored = await self.restore(ops, session_id)
        if restored:
            return restored, SOURCE_EVENT

        default_team = await self._find_default_team(ops, session_id)
        observed_team = self.team_of(session_id)
        if observed_team:
            return observed_team, SOURCE_EVENT
        if default_team:
            return default_team, SOURCE_DEFAULT_NAME
        return None, SOURCE_NONE
