# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral, read-only Runtime Session query contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.mode_matrix import deprecate_mode, is_single_agent_mode

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200


class SessionCatalogError(RuntimeError):
    """A stable Session query validation or storage failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionGetInput:
    """Select one Session owned by the requesting Runtime Channel."""

    channel_id: str
    session_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionListInput:
    """Select one page of Sessions owned by a Runtime Channel."""

    channel_id: str
    limit: int = _DEFAULT_LIMIT
    offset: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionSummary:
    """Stable Session facts without delivery or Channel-private metadata."""

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "title": self.title,
            "mode": self.mode,
            "work_mode": self.work_mode,
            "project_id": self.project_id,
            "project_dir": self.project_dir,
            "model": self.model,
            "created_at": self.created_at,
            "last_message_at": self.last_message_at,
            "message_count": self.message_count,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionListResult:
    """One immutable page after ownership and single-Agent filtering."""

    sessions: tuple[SessionSummary, ...]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [item.to_dict() for item in self.sessions],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


def _normalize_channel_id(value: object) -> str:
    channel_id = value.strip().lower() if isinstance(value, str) else ""
    if not channel_id:
        raise SessionCatalogError("channel_id is required", code="BAD_REQUEST")
    return channel_id


def _normalize_session_id(value: object) -> str:
    session_id = value.strip() if isinstance(value, str) else ""
    if not session_id:
        raise SessionCatalogError("session_id is required", code="BAD_REQUEST")

    from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id

    if not is_valid_session_id(session_id):
        raise SessionCatalogError("invalid session_id", code="BAD_REQUEST")
    return session_id


def _is_safe_session_id(value: str) -> bool:
    from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id

    return is_valid_session_id(value)


def _validate_page(limit: object, offset: object) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_LIMIT
    ):
        raise SessionCatalogError("limit must be between 1 and 200", code="BAD_REQUEST")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise SessionCatalogError(
            "offset must be a non-negative integer",
            code="BAD_REQUEST",
        )
    return limit, offset


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _single_agent_mode(value: object) -> str:
    normalized = deprecate_mode(value)
    mode = normalized.strip() if isinstance(normalized, str) else ""
    return mode if mode and is_single_agent_mode(mode) else ""


def _project_session(
    metadata: Mapping[str, Any],
    *,
    channel_id: str,
) -> SessionSummary | None:
    item_channel = _safe_text(metadata.get("channel_id")).lower()
    if item_channel != channel_id:
        return None
    mode = _single_agent_mode(metadata.get("mode"))
    if not mode:
        return None
    session_id = _safe_text(metadata.get("session_id"))
    if not session_id or not _is_safe_session_id(session_id):
        return None
    return SessionSummary(
        session_id=session_id,
        channel_id=item_channel,
        title=_safe_text(metadata.get("title")),
        mode=mode,
        work_mode=_safe_text(metadata.get("work_mode")).lower(),
        project_id=_safe_text(metadata.get("project_id")),
        project_dir=_safe_text(metadata.get("project_dir")),
        model=_safe_text(metadata.get("model")),
        created_at=_safe_float(metadata.get("created_at")),
        last_message_at=_safe_float(metadata.get("last_message_at")),
        message_count=_safe_int(metadata.get("message_count")),
    )


def _read_session_metadata(session_id: str) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
    )

    return get_session_metadata(
        session_id,
        cache_bust=True,
        enable_writeback=False,
    )


def _collect_session_metadata() -> Iterable[Mapping[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    return collect_all_sessions_metadata()


def get_session(request: SessionGetInput) -> SessionSummary | None:
    """Read one owned single-Agent Session without metadata writeback."""
    if not isinstance(request, SessionGetInput):
        raise SessionCatalogError("session get input is required", code="BAD_REQUEST")
    channel_id = _normalize_channel_id(request.channel_id)
    session_id = _normalize_session_id(request.session_id)
    try:
        metadata = _read_session_metadata(session_id)
    except SessionCatalogError:
        raise
    except Exception:
        raise SessionCatalogError(
            "failed to read session", code="READ_FAILED"
        ) from None
    if not isinstance(metadata, Mapping) or not metadata:
        return None
    return _project_session(metadata, channel_id=channel_id)


def list_sessions(request: SessionListInput) -> SessionListResult:
    """Filter by Channel and single-Agent mode, then sort and paginate."""
    if not isinstance(request, SessionListInput):
        raise SessionCatalogError("session list input is required", code="BAD_REQUEST")
    channel_id = _normalize_channel_id(request.channel_id)
    limit, offset = _validate_page(request.limit, request.offset)
    try:
        raw_sessions = _collect_session_metadata()
        session_items: list[SessionSummary] = []
        for metadata in raw_sessions:
            if not isinstance(metadata, Mapping):
                continue
            projected = _project_session(metadata, channel_id=channel_id)
            if projected is not None:
                session_items.append(projected)
        filtered = tuple(session_items)
    except SessionCatalogError:
        raise
    except Exception:
        raise SessionCatalogError(
            "failed to list sessions", code="READ_FAILED"
        ) from None

    ordered = tuple(
        sorted(
            filtered,
            key=lambda item: (-item.last_message_at, item.session_id),
        )
    )
    page_end = offset + limit
    return SessionListResult(
        sessions=ordered[offset:page_end],
        total=len(ordered),
        limit=limit,
        offset=offset,
    )


__all__ = [
    "SessionCatalogError",
    "SessionGetInput",
    "SessionListInput",
    "SessionListResult",
    "SessionSummary",
    "get_session",
    "list_sessions",
]
