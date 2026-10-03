# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process ownership of one Process CLI machine Session."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
import portalocker

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.runtime.agent_definition import RuntimeAgentDefinition

_BINDING_DIR = ".process_cli_bindings"


class SessionGuardError(RuntimeError):
    """A safe machine error for conflicting Session ownership."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SessionLease:
    """Hold an OS lock until Runtime and its Session have closed."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        lock_dir = get_agent_sessions_dir() / ".process_cli_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock = portalocker.Lock(
            str(lock_dir / f"{digest}.lock"),
            mode="a",
            timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        self._acquired = False

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except portalocker.exceptions.LockException as error:
            raise SessionGuardError(
                "Session is already executing in another Process CLI call.",
                code="SESSION_BUSY",
                retryable=True,
            ) from error
        self._acquired = True

    def release(self) -> None:
        if self._acquired:
            self._lock.release()
            self._acquired = False


def bind_agent(
    session_id: str,
    definition: dict[str, object] | None,
    *,
    resumed: bool,
) -> None:
    """Pin a Session to its declared root Agent under its Session lease."""

    directory = get_agent_sessions_dir() / _BINDING_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = (
        directory / f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.json"
    )
    fingerprint = (
        RuntimeAgentDefinition.from_mapping(definition).fingerprint
        if definition is not None
        else None
    )
    expected = {"schema": 1, "fingerprint": fingerprint}
    if target.exists():
        try:
            stored = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SessionGuardError(
                "Session Agent binding is unreadable.", code="AGENT_BINDING_INVALID"
            ) from error
        if stored != expected:
            raise SessionGuardError(
                "Session is bound to another Agent definition.",
                code="AGENT_DEFINITION_SESSION_CONFLICT",
            )
        return

    if resumed and definition is not None:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False
        )
        if int(metadata.get("message_count") or 0) > 0:
            raise SessionGuardError(
                "An existing unbound Session cannot adopt a custom Agent.",
                code="AGENT_DEFINITION_SESSION_CONFLICT",
            )

    temporary = directory / f"{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(expected, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["SessionGuardError", "SessionLease", "bind_agent"]
