# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral contracts for querying persisted Runtime Sessions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSummary:
    """Stable Session facts exposed without leaking storage metadata."""

    session_id: str
    channel_id: str
    title: str
    mode: str
    work_mode: str
    project_id: str = ""
    project_dir: str = ""
    model: str = ""
    created_at: float = 0.0
    last_message_at: float = 0.0
    message_count: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionListResult:
    """One immutable, already-filtered page of persisted Sessions."""

    sessions: tuple[SessionSummary, ...]
    total: int
    limit: int
    offset: int


__all__ = ["SessionListResult", "SessionSummary"]
