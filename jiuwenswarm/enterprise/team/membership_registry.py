# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side index of Web human memberships.

Populated when ``team.member.join`` / ``team.member.leave`` / ``team.members.list``
responses pass through the Web channel, and consulted by ``MessageHandler``
to tag Web ``chat.send`` requests with the caller's seat (``metadata.member_name``).

Unlike IM ``/join`` seats (``SessionSharingRegistry``), a Web membership
does **not** rewrite ``session_id`` and does not block GodView delivery: the
Web client stays on the team session it explicitly opened and still receives
the leader stream. The AgentServer re-validates the seat against its own
``TeamMembership`` records, so this index is only a routing hint.

Keyed by ``(channel_id, user_id, session_id)``; thread-safe; purely in-memory
(rebuilt from ``team.members.list`` on reconnect).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class WebTeamMembership:
    channel_id: str
    user_id: str
    session_id: str
    team_id: str
    member_name: str
    joined_at: float


class TeamMembershipRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str, str], WebTeamMembership] = {}

    @staticmethod
    def _key(channel_id: str, user_id: str, session_id: str) -> tuple[str, str, str]:
        return (
            str(channel_id or "").strip().lower(),
            str(user_id or "").strip(),
            str(session_id or "").strip(),
        )

    def register(
        self,
        *,
        channel_id: str,
        user_id: str,
        session_id: str,
        team_id: str,
        member_name: str,
    ) -> WebTeamMembership | None:
        key = self._key(channel_id, user_id, session_id)
        if not all(key) or not member_name:
            return None
        record = WebTeamMembership(
            channel_id=key[0],
            user_id=key[1],
            session_id=key[2],
            team_id=str(team_id or ""),
            member_name=str(member_name).strip(),
            joined_at=time.time(),
        )
        with self._lock:
            self._by_key[key] = record
        return record

    def unregister(self, *, channel_id: str, user_id: str, session_id: str) -> bool:
        key = self._key(channel_id, user_id, session_id)
        with self._lock:
            return self._by_key.pop(key, None) is not None

    def lookup(self, channel_id: str, user_id: str, session_id: str) -> WebTeamMembership | None:
        key = self._key(channel_id, user_id, session_id)
        if not all(key):
            return None
        with self._lock:
            return self._by_key.get(key)

    def member_name_for(self, channel_id: str, user_id: str, session_id: str) -> str:
        record = self.lookup(channel_id, user_id, session_id)
        return record.member_name if record else ""

    def sessions_for_user(self, channel_id: str, user_id: str) -> list[WebTeamMembership]:
        cid = str(channel_id or "").strip().lower()
        uid = str(user_id or "").strip()
        with self._lock:
            return [r for r in self._by_key.values() if r.channel_id == cid and r.user_id == uid]

    def clear(self) -> None:
        with self._lock:
            self._by_key.clear()


_DEFAULT: TeamMembershipRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def get_team_membership_registry() -> TeamMembershipRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = TeamMembershipRegistry()
    return _DEFAULT


__all__ = ["TeamMembershipRegistry", "WebTeamMembership", "get_team_membership_registry"]
