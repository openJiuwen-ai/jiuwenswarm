# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sticky session → Worker routing.

Desktop AgentServer runs one primary Worker. The spare slot is reserved for
a later warm standby and is not started in phase 2.
"""

from __future__ import annotations


class SessionAffinity:
    """Pin each session to one Worker id."""

    def __init__(self, primary_id: str = "primary") -> None:
        self._primary_id = primary_id
        self._sessions: dict[str, str] = {}

    @property
    def primary_id(self) -> str:
        return self._primary_id

    def worker_for(self, session_id: str | None) -> str:
        sid = str(session_id or "").strip()
        if not sid:
            return self._primary_id
        return self._sessions.setdefault(sid, self._primary_id)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(str(session_id or "").strip(), None)

    def clear(self) -> None:
        self._sessions.clear()
