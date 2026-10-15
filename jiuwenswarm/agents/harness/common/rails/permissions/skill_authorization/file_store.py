# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session-owned Skill state. Files are authoritative; grants are never cached."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

import portalocker
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.harness.security.skill_authorization import (
    GrantDecision,
    GrantStatus,
    SkillGrant,
    SkillGrantStore,
)

from jiuwenswarm.common.utils import (
    get_agent_sessions_dir,
    get_user_workspace_dir,
    is_valid_session_id,
)

logger = logging.getLogger(__name__)
_SCHEMA = 1
_PRIVATE_DIR = ".skill_authorization"
_LOCK_TIMEOUT = 0.3
_MAX_BYTES = 256 * 1024
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_IO_LOCKS: WeakValueDictionary = WeakValueDictionary()
# Request identity only, never authorization data. Supervisor tasks do not
# automatically inherit the HTTP request's tenant path bindings.
_session_file_context: ContextVar[tuple[str, Path] | None] = ContextVar(
    "skill_session_files", default=None
)


def bind_session_file_context(session_id: str, sessions_root: Path) -> None:
    _session_file_context.set((session_id, sessions_root))


def clear_session_file_context() -> None:
    _session_file_context.set(None)


def _session_files_root() -> Path:
    context = _session_file_context.get()
    return context[1] if context else get_agent_sessions_dir()


class _TransactionStore(SkillGrantStore):
    """Use SDK transitions on a disposable snapshot, without a process cache."""

    def __init__(self, sid: str, doc: dict, generation: int) -> None:
        if (
            not isinstance(doc.get("grants", {}), dict)
            or not isinstance(doc.get("executions", {}), dict)
            or not isinstance(doc.get("invalidated", []), list)
        ):
            raise TypeError("Invalid Skill state maps")
        if doc.get("active_skill") is not None and not isinstance(
            doc["active_skill"], dict
        ):
            raise TypeError("Invalid active Skill state")
        self._lock = threading.RLock()
        self._grants = {}
        self._skill_executions = {
            (sid, scope): name for scope, name in doc.get("executions", {}).items()
        }
        self._invalidated_scopes = {
            (sid, scope) for scope in doc.get("invalidated", [])
        }
        self._retired_sessions = set()
        self.generation = generation
        for scope, records in doc.get("grants", {}).items():
            if not isinstance(scope, str) or not isinstance(records, list):
                raise TypeError("Invalid Skill grant scope")
            decoded = {}
            for fields in records:
                if not isinstance(fields, dict):
                    raise TypeError("Invalid Skill grant record")
                try:
                    grant = SkillGrant(
                        **{
                            **fields,
                            "decision": GrantDecision(fields["decision"]),
                            "status": GrantStatus(fields["status"]),
                        }
                    )
                except (TypeError, KeyError) as exc:
                    raise ValueError("Invalid Skill grant record") from exc
                decoded[grant.skill_name] = grant
            self._grants[(sid, scope)] = decoded

    @property
    def authorization_generation(self) -> int:
        return self.generation

    def has_runtime_state(self) -> bool:
        """Startup must install storage before any native grants are created."""
        with self._lock:
            return bool(self._grants or self._skill_executions)

    def snapshot(self, sid: str) -> dict:
        """Expose data, not SDK protected members, to the persistence adapter."""
        return {
            "grants": {
                scope: [asdict(grant) for grant in grants.values()]
                for (stored_sid, scope), grants in self._grants.items()
                if stored_sid == sid
            },
            "executions": {
                scope: name
                for (stored_sid, scope), name in self._skill_executions.items()
                if stored_sid == sid
            },
            "invalidated": sorted(
                scope
                for stored_sid, scope in self._invalidated_scopes
                if stored_sid == sid
            ),
        }


def _open_at(directory: int | Path, name: str, flags: int, mode: int = 0o600) -> int:
    if isinstance(directory, int):
        return os.open(name, flags, mode, dir_fd=directory)
    target = directory / name
    if target.is_symlink():
        raise ValueError("Skill state may not be a symlink")
    return os.open(target, flags, mode)


@contextmanager
def _locked_directory(directory: Path, *, create: bool = False) -> Iterator[int | Path]:
    # Keep only locks for in-flight operations, never grants or session state.
    # Independent sessions must not queue behind each other's filesystem I/O.
    key = os.path.abspath(directory)
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_IO_LOCKS.get(key)
        if local_lock is None:
            local_lock = threading.RLock()
            _LOCAL_IO_LOCKS[key] = local_lock
    if not local_lock.acquire(timeout=_LOCK_TIMEOUT):
        raise TimeoutError("Skill state local I/O lock timed out")
    try:
        with _locked_directory_fd(directory, create=create) as fd:
            yield fd
    finally:
        local_lock.release()


@contextmanager
def _open_directory(directory: Path) -> Iterator[int | Path]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if (
        directory.is_symlink()
        or getattr(directory.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Skill state directory may not be a link")
    directory_fd = (
        directory if os.name == "nt" else os.open(directory, flags, mode=0o600)
    )
    try:
        yield directory_fd
    finally:
        if isinstance(directory_fd, int):
            os.close(directory_fd)


@contextmanager
def _locked_directory_fd(
    directory: Path, *, create: bool = False
) -> Iterator[int | Path]:
    """Bounded cross-process lock, anchored to the directory opened before deletion."""
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    # Windows cannot open a directory fd. Its open lock file prevents the
    # directory from being removed while the transaction holds the handle.
    with _open_directory(directory) as directory_fd:
        locked = False
        lock_fd = _open_at(
            directory_fd,
            "state.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(lock_fd, "a+b") as lock_file:
            deadline = time.monotonic() + _LOCK_TIMEOUT
            try:
                while not locked:
                    try:
                        portalocker.lock(
                            lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB
                        )
                        locked = True
                    except portalocker.exceptions.LockException:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Skill state file lock timed out"
                            ) from None
                        time.sleep(0.01)
                yield directory_fd
            finally:
                if locked:
                    portalocker.unlock(lock_file)


def _read_json(directory_fd: int | Path, name: str) -> tuple[dict, str]:
    try:
        fd = _open_at(directory_fd, name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}, ""
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        raw = source.read(_MAX_BYTES + 1)
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Skill state exceeds size limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("Skill state must be an object")
    return data, raw


def _atomic_json(directory_fd: int | Path, name: str, data: dict) -> None:
    """Replace within the original directory fd; never recreate a deleted session."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Skill state exceeds size limit")
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = _open_at(directory_fd, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        if isinstance(directory_fd, int):
            # POSIX rename atomically replaces the destination and advertises
            # dir_fd support, so independent directories need no global lock.
            os.rename(
                temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        else:
            os.replace(directory_fd / temporary, directory_fd / name)
    finally:
        try:
            if isinstance(directory_fd, int):
                os.unlink(temporary, dir_fd=directory_fd)
            else:
                os.unlink(directory_fd / temporary)
        except FileNotFoundError:
            pass


def _file_operation(method: Callable) -> Callable:
    @wraps(method)
    def call(self, session_id, *args, **kwargs):
        return self.operate(
            session_id,
            lambda store, doc: method(store, session_id, *args, **kwargs),
            write=method.__name__ not in {"get_grant", "list_grants"},
        )

    return call


class FileSkillGrantStore(SkillGrantStore):
    """Authoritative session files; no grant or active-skill dictionaries on this object."""

    def __init__(
        self,
        *,
        sessions_root: Callable[[], Path] | None = None,
        data_root: Callable[[], Path] | None = None,
    ) -> None:
        self.sessions_root = sessions_root or _session_files_root
        self.data_root = data_root or get_user_workspace_dir

    def session_directory(self, session_id: str) -> Path:
        """Resolve a real session without allowing traversal or symlink substitution."""
        from jiuwenswarm.server.runtime.session.session_metadata import (
            resolve_session_subdir,
        )

        if session_id == "default" or not is_valid_session_id(session_id):
            raise ValueError("A real session_id is required for Skill state")
        path = resolve_session_subdir(session_id, sessions_root=self.sessions_root())
        if path is None:
            raise ValueError("Invalid Skill state session directory")
        return path

    def epoch_directory(self) -> Path:
        """Instance-wide invalidation metadata; separate from user task files."""
        return self.data_root() / _PRIVATE_DIR

    def read_epoch(self) -> int:
        directory = self.epoch_directory()
        if not directory.exists():
            return 0
        # Writers serialize increments and atomically replace generation.json.
        # Fresh opens see an old or new complete value; readers need no global
        # file lock. operate still checks the epoch again before returning.
        with _open_directory(directory) as fd:
            data, _raw = _read_json(fd, "generation.json")
            return int(data.get("generation", 0))

    @property
    def authorization_generation(self) -> int:
        from jiuwenswarm.common.path_provider import current_path_context

        try:
            context = _session_file_context.get()
            sid = context[0] if context else current_path_context().session_id
            if not sid or sid == "default":
                raise ValueError("Skill approval requires a real session identity")
            return self.get_authorization_generation(sid)
        except (
            OSError, ValueError, TypeError, RuntimeError,
            portalocker.exceptions.LockException,
        ) as exc:
            # A normal exception is swallowed by the SDK callback dispatcher;
            # a constant fallback can compare equal across two failed reads.
            raise AbortError(
                "Skill authorization version unavailable; retry the request"
            ) from exc

    def get_authorization_generation(self, session_id: str) -> int:
        """Bind approvals to this session incarnation and the shared feature epoch."""
        # The first read must persist the nonce before an approval can wait.
        return self.operate(
            session_id, lambda store, doc: store.authorization_generation
        )

    @property
    def retired_session_memory_bytes(self) -> int:
        return 0

    def operate(self, session_id: str, action: Callable, *, write: bool = True) -> Any:
        """Read under lock, execute SDK transitions, persist only changed state."""
        sid = str(session_id or "").strip()
        session = self.session_directory(sid)
        directory = session / _PRIVATE_DIR
        epoch = self.read_epoch()
        if not session.is_dir():
            if write:
                raise RuntimeError(
                    f"Skill authorization session has been deleted or is not initialized: {session}"
                )
            return action(_TransactionStore(sid, {}, epoch << 64), {})
        if not directory.exists() and not write:
            return action(_TransactionStore(sid, {}, epoch << 64), {})
        with _locked_directory(directory, create=True) as fd:
            doc, raw = _read_json(fd, "state.json")
            if doc and (doc.get("schema") != _SCHEMA or doc.get("session_id") != sid):
                raise ValueError("Invalid Skill authorization state schema or session")
            if doc.get("deleted"):
                raise RuntimeError("Skill authorization session has been deleted")
            if not doc or doc.get("epoch") != epoch:
                doc = {
                    "schema": _SCHEMA,
                    "session_id": sid,
                    "epoch": epoch,
                    "nonce": secrets.randbits(63),
                }
            generation = (epoch << 64) | int(doc["nonce"])
            store = _TransactionStore(sid, doc, generation)
            result = action(store, doc)
            state = store.snapshot(sid)
            doc.update(state)
            if "main" in state["invalidated"]:
                doc.pop("active_skill", None)
            if self.read_epoch() != epoch:
                raise RuntimeError("Skill authorization changed during the operation")
            if (
                write
                and json.dumps(doc, ensure_ascii=False, separators=(",", ":")) != raw
            ):
                _atomic_json(fd, "state.json", doc)
            return result

    create_pending_grant = _file_operation(SkillGrantStore.create_pending_grant)
    activate_pending = _file_operation(SkillGrantStore.activate_pending)
    get_grant = _file_operation(SkillGrantStore.get_grant)
    list_grants = _file_operation(SkillGrantStore.list_grants)
    drop_pending = _file_operation(SkillGrantStore.drop_pending)
    enter_skill_execution = _file_operation(SkillGrantStore.enter_skill_execution)
    exit_skill_execution = _file_operation(SkillGrantStore.exit_skill_execution)

    def get_active(self, session_id: str, agent_scope_id: str):
        try:
            return self.operate(
                session_id,
                lambda store, doc: store.get_active(session_id, agent_scope_id),
                write=False,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            portalocker.exceptions.LockException,
        ):
            logger.warning(
                "[skill_authorization] session file unavailable; no overlay",
                exc_info=True,
            )
            return None

    def get_active_skill_execution(self, session_id: str, agent_scope_id: str):
        try:
            return self.operate(
                session_id,
                lambda store, doc: store.get_active_skill_execution(
                    session_id, agent_scope_id
                ),
                write=False,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            portalocker.exceptions.LockException,
        ):
            logger.warning(
                "[skill_authorization] session file unavailable; no execution window",
                exc_info=True,
            )
            return None

    def invalidate_scope(
        self, session_id: str, agent_scope_id: str, *, reason: str
    ) -> None:
        self.operate(
            session_id,
            lambda store, doc: store.invalidate_scope(
                session_id, agent_scope_id, reason=reason
            ),
        )

    def revoke_grants(
        self,
        session_id: str,
        agent_scope_id: str,
        *,
        skill_name=None,
        reason="skill_complete",
    ) -> None:
        self.operate(
            session_id,
            lambda store, doc: store.revoke_grants(
                session_id, agent_scope_id, skill_name=skill_name, reason=reason
            ),
        )

    def clear_scope(self, session_id: str, agent_scope_id: str) -> None:
        def clear(store, doc):
            store.clear_scope(session_id, agent_scope_id)
            if agent_scope_id == "main":
                doc.pop("active_skill", None)

        self.operate(session_id, clear)

    def clear_session(self, session_id: str) -> None:
        if not self.session_directory(session_id).is_dir():
            return

        def retire(store, doc):
            store.clear_all()
            doc.pop("active_skill", None)
            doc["deleted"] = True

        self.operate(session_id, retire)

    def sync_feature_state(self, session_id: str, *, enabled: bool) -> None:
        """A cold Pod observing disabled policy must retire persisted grants too."""

        def sync(store, doc):
            if doc.get("feature_enabled") is enabled:
                return
            if not enabled:
                store.clear_all()
                doc.pop("active_skill", None)
                doc["nonce"] = secrets.randbits(63)
            doc["feature_enabled"] = enabled

        self.operate(session_id, sync)

    def clear_all(self) -> None:
        self.data_root().mkdir(parents=True, exist_ok=True)
        with _locked_directory(self.epoch_directory(), create=True) as fd:
            data, _raw = _read_json(fd, "generation.json")
            _atomic_json(
                fd,
                "generation.json",
                {"generation": int(data.get("generation", 0)) + 1},
            )

    def get_active_skill_state(self, session_id: str) -> dict | None:
        try:
            return self.operate(
                session_id, lambda store, doc: doc.get("active_skill"), write=False
            )
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            portalocker.exceptions.LockException,
        ):
            logger.warning(
                "[skill_authorization] session file unavailable; no active skill",
                exc_info=True,
            )
            return None

    def update_active_skill_state(
        self, session_id: str, update: Callable[[dict], dict | None]
    ) -> dict | None:
        def action(store, doc):
            updated = update(dict(doc.get("active_skill") or {}))
            if updated is None:
                doc.pop("active_skill", None)
            else:
                doc["active_skill"] = updated
            return updated

        return self.operate(session_id, action)


def configure_shared_skill_authorization(_config: dict) -> FileSkillGrantStore:
    """Install session-file state before accepting requests; never fall back to memory."""
    from openjiuwen.harness.security.skill_authorization import (
        get_skill_grant_store,
        set_skill_grant_store,
    )

    current = get_skill_grant_store()
    if isinstance(current, FileSkillGrantStore):
        return current
    if not hasattr(SkillGrantStore, "authorization_generation"):
        raise RuntimeError(
            "Session Skill state requires SDK shared authorization_generation support"
        )
    if _TransactionStore.has_runtime_state(current):
        raise RuntimeError("Install session Skill state before serving requests")
    store = FileSkillGrantStore()
    set_skill_grant_store(store)
    logger.info(
        "[skill_authorization] session file state enabled; no process-local authorization cache"
    )
    return store
