# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral contracts for Session history rewind operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SessionRewindAction(str, Enum):
    """Durable Session mutation selected by a Runtime caller."""

    CONVERSATION = "conversation"
    CONVERSATION_AND_FILES = "conversation_and_files"
    FILES_ONLY = "files_only"
    COMPACT_FROM = "compact_from"
    COMPACT_UP_TO = "compact_up_to"


class SessionRewindContextPolicy(str, Enum):
    """How Runtime obtains the Agent context that must follow history."""

    LIVE_ONLY = "live_only"
    ENSURE_PERSISTED = "ensure_persisted"


class SessionRewindError(ValueError):
    """Stable Runtime error carrying a transport-independent error code."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionRewindListInput:
    channel_id: str
    session_id: str
    project_dir: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRewindFile:
    path: str
    lines_added: int = 0
    lines_removed: int = 0
    is_new_file: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "linesAdded": self.lines_added,
            "linesRemoved": self.lines_removed,
            "isNewFile": self.is_new_file,
        }


@dataclass(frozen=True, slots=True)
class SessionRewindTurn:
    turn_index: int
    content_preview: str
    timestamp: Any = 0
    message_id: str = ""
    request_id: str = ""
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    files: tuple[SessionRewindFile, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "content_preview": self.content_preview,
            "timestamp": self.timestamp,
            "id": self.message_id,
            "request_id": self.request_id,
            "stats": {
                "filesChanged": self.files_changed,
                "linesAdded": self.lines_added,
                "linesRemoved": self.lines_removed,
            },
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class SessionRewindListResult:
    turns: tuple[SessionRewindTurn, ...]
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "turns": [item.to_dict() for item in self.turns],
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class SessionRewindInput:
    operation_id: str
    channel_id: str
    session_id: str
    turn_index: int
    action: SessionRewindAction = SessionRewindAction.CONVERSATION
    context_policy: SessionRewindContextPolicy = SessionRewindContextPolicy.LIVE_ONLY
    require_context: bool = False
    compact_summary: str = ""
    summarized_count: int = 0


@dataclass(frozen=True, slots=True)
class SessionRewindFileError:
    file: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"file": self.file, "error": self.error}


@dataclass(frozen=True, slots=True)
class SessionRewindResult:
    action: SessionRewindAction
    session_id: str
    turn_index: int
    content: str | None = None
    content_preview: str | None = None
    remaining_records: int | None = None
    removed_records: int | None = None
    context_rebuilt: bool | None = None
    restored_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    restore_errors: tuple[SessionRewindFileError, ...] = ()
    summarized_messages: int | None = None
    direction: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the domain result using the established product field names."""
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "turn_index": self.turn_index,
        }
        if self.action is not SessionRewindAction.FILES_ONLY:
            if self.content is not None:
                payload["content"] = self.content
            if self.content_preview is not None:
                payload["content_preview"] = self.content_preview
            if self.remaining_records is not None:
                payload["remaining_records"] = self.remaining_records
            if self.removed_records is not None:
                payload["removed_records"] = self.removed_records
            payload["rewind_context"] = bool(self.context_rebuilt)
        if self.action in {
            SessionRewindAction.CONVERSATION_AND_FILES,
            SessionRewindAction.FILES_ONLY,
        }:
            payload.update(
                {
                    "restored_files": list(self.restored_files),
                    "deleted_files": list(self.deleted_files),
                }
            )
            error_key = (
                "errors"
                if self.action is SessionRewindAction.FILES_ONLY
                else "restore_errors"
            )
            payload[error_key] = [item.to_dict() for item in self.restore_errors]
        if self.summarized_messages is not None:
            payload["summarized_messages"] = self.summarized_messages
        if self.direction is not None:
            payload["direction"] = self.direction
        return payload


__all__ = [
    "SessionRewindAction",
    "SessionRewindContextPolicy",
    "SessionRewindError",
    "SessionRewindFile",
    "SessionRewindFileError",
    "SessionRewindInput",
    "SessionRewindListInput",
    "SessionRewindListResult",
    "SessionRewindResult",
    "SessionRewindTurn",
]
