"""Persistent lifecycle fences shared by request admission and disk writers.

The resource file is the commit point: it embeds the current operation so a
crash between journal and pointer writes cannot expose an unfenced resource.
No process-local cache is used for admission decisions.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agent_gateway.common.work_mode import SUPPORTED_WORK_MODES
from jiuwenswarm.server.runtime.session.project_store import file_lock

_HELD_LOCKS = threading.local()
logger = logging.getLogger(__name__)


def get_agent_sessions_dir() -> Path:
    from jiuwenswarm.common import utils

    return utils.get_agent_sessions_dir()


def get_agent_root_dir() -> Path:
    return get_agent_sessions_dir().parent


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def validate_id(value: Any) -> str:
    if not isinstance(value, str):
        raise LifecycleError("BAD_REQUEST", "invalid resource ID")
    if not value.strip() or value != value.strip():
        raise LifecycleError("BAD_REQUEST", "invalid resource ID")
    if ".." in value or value.endswith((".", " ")):
        raise LifecycleError("BAD_REQUEST", "invalid resource ID")
    if any(c in value for c in '/\\:\x00<>"|?*') or any(ord(c) < 32 for c in value):
        raise LifecycleError("BAD_REQUEST", "invalid resource ID")
    if value.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise LifecycleError("BAD_REQUEST", "reserved resource ID")
    return value


def resource_path(kind: str, resource_id: str) -> Path:
    if kind not in {"session", "project"}:
        raise ValueError(kind)
    return (
        get_agent_root_dir()
        / "lifecycle"
        / "resources"
        / f"{kind}_{validate_id(resource_id)}.json"
    )


# Same short-backoff budget the metadata writer's _atomic_replace grants itself:
# enough to ride out any tmp+rename replacement window, bounded so a genuinely
# unreadable file still fails fast enough for request admission.
_SHARING_RETRY_ATTEMPTS = 100


def _read_text_ride_out_sharing(path: Path) -> str:
    """Read file content, retrying transient Windows sharing violations.

    Atomic writers publish via tmp+rename; an unlocked reader that lands in
    the replacement window receives Errno 13 even though the file is healthy
    (issue #4923). Ride the window out instead of failing the whole request.
    """
    attempt = 0
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            attempt += 1
            if attempt >= _SHARING_RETRY_ATTEMPTS:
                raise
            time.sleep(min(0.001 * attempt, 0.01))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(_read_text_ride_out_sharing(path))
    if not isinstance(value, dict):
        raise LifecycleError("CONFLICT", f"invalid lifecycle data: {path.name}")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def resource_lock(kind: str, resource_id: str, *, blocking: bool = True) -> Iterator[None]:
    path = resource_path(kind, resource_id)
    held = getattr(_HELD_LOCKS, "paths", None)
    if held is None:
        held = _HELD_LOCKS.paths = set()
    if path in held:
        yield
        return
    with file_lock(path, blocking=blocking):
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)


def state(kind: str, resource_id: str) -> dict:
    return read_json(resource_path(kind, resource_id)) if resource_id else {}


def session_delete_pending(session_id: str) -> bool:
    """Whether an unfinished delete owns the Session execution barrier."""
    value = state("session", session_id)
    operation = value.get("operation") or {}
    return bool(
        value.get("blocked")
        and operation.get("kind") == "delete"
        and operation.get("status") != "completed"
    )


def save_locked(kind: str, resource_id: str, value: dict) -> None:
    value["revision"] = int(value.get("revision", 0)) + 1
    operation = value.get("operation")
    if operation:
        operation["updated_at"] = time.time()
        operation["revision"] = value["revision"]
        atomic_json(
            get_agent_root_dir()
            / "lifecycle"
            / "operations"
            / f"{operation['operation_id']}.json",
            operation,
        )
    atomic_json(resource_path(kind, resource_id), value)


def begin(
    kind: str, resource_id: str, action: str, *, block_execution: bool = True
) -> dict:
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value.get("operation")
        if operation and operation["status"] != "completed":
            if operation["kind"] != action:
                raise LifecycleError(
                    "OPERATION_IN_PROGRESS", "another lifecycle operation is pending"
                )
            return operation
        if value.get("deleted"):
            if action == "delete":
                return operation
            raise LifecycleError("NOT_FOUND", "resource was permanently deleted")
        generation = int(value.get("generation", 0)) + int(block_execution)
        now = time.time()
        operation = dict(
            operation_id=uuid.uuid4().hex,
            resource_type=kind,
            resource_id=resource_id,
            kind=action,
            status="running",
            phase="block" if block_execution else "check_sessions",
            generation=generation,
            created_at=now,
            updated_at=now,
            archived_at=now,
            completed_items={},
            errors=[],
            retryable=True,
            stop_pending=False,
            owner_id="",
            lease_expires_at=0,
        )
        value.update(
            operation=operation,
            generation=generation,
            blocked=block_execution or value.get("blocked", False),
            write_blocked=(action == "unarchive")
            if block_execution
            else value.get("write_blocked", False),
            drain_generation=generation - 1,
        )
        save_locked(kind, resource_id, value)
        return operation


def claim_operation(kind: str, resource_id: str, owner_id: str) -> dict:
    """Called only by the process holding the separate execution-owner lock."""
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value.get("operation")
        if not operation or operation["status"] == "completed":
            return operation or {}
        if operation.get("owner_id") and operation["owner_id"] != owner_id:
            value["generation"] = int(value.get("generation", 0)) + 1
            operation["generation"] = value["generation"]
        operation.update(owner_id=owner_id, lease_expires_at=time.time() + 30)
        save_locked(kind, resource_id, value)
        return operation


def renew_operation(
    kind: str, resource_id: str, owner_id: str, *, release=False
) -> None:
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value.get("operation")
        if (
            operation
            and operation.get("owner_id") == owner_id
            and operation["status"] != "completed"
        ):
            operation["lease_expires_at"] = 0 if release else time.time() + 30
            # Lease heartbeats do not represent a business state transition.
            atomic_json(
                get_agent_root_dir()
                / "lifecycle"
                / "operations"
                / f"{operation['operation_id']}.json",
                operation,
            )
            atomic_json(resource_path(kind, resource_id), value)


def update(kind: str, resource_id: str, **changes: Any) -> dict:
    """Update and return the lifecycle state's operation record."""
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value["operation"]
        operation.update(changes)
        save_locked(kind, resource_id, value)
        return operation


def complete(
    kind: str,
    resource_id: str,
    *,
    archived: bool = False,
    deleted: bool = False,
    result: dict | None = None,
) -> None:
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        value.update(
            blocked=archived or deleted,
            write_blocked=archived or deleted,
            deleted=deleted,
        )
        value["operation"].update(
            status="completed", stop_pending=False, result=result or {}
        )
        if deleted:
            value["operation"].pop("delete_metadata", None)
        save_locked(kind, resource_id, value)


def logically_deleted(value: dict) -> bool:
    """Whether a Session was accepted for deletion and is only awaiting cleanup."""
    return bool(value.get("deleted"))


def logically_archived(value: dict) -> bool:
    """Whether a Session leaves the active area even before its directory moves.

    Archive is accepted synchronously and finalized in the background, so the
    listing must hide it while the directory is still under ``sessions/``.
    Failed and abandoned archives keep the same logical result: the user asked
    for the Session to be archived, and cleanup only decides where the bytes
    live.
    """
    operation = value.get("operation") or {}
    return bool(
        operation
        and operation.get("kind") == "archive"
        and operation.get("status") != "completed"
    )


def mark_finalizing(
    kind: str,
    resource_id: str,
    *,
    deleted: bool = False,
    **changes: Any,
) -> dict:
    """Record the logical result and hand physical cleanup to a background task.

    ``write_blocked`` is deliberately untouched: writes already accepted by the
    metadata/history queues still have to drain.  The background finalizer
    fences writes after it flushes them.
    """
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value.get("operation")
        if not operation or operation.get("status") == "completed":
            return operation or {}
        if deleted:
            value["deleted"] = True
        value["blocked"] = True
        operation.update(
            status="running",
            phase="finalizing",
            stop_pending=True,
            retryable=True,
            cleanup_attempts=0,
            cleanup_next_at=0,
            cleanup_exhausted=False,
            **changes,
        )
        save_locked(kind, resource_id, value)
        return operation


def abandon_failed_operation(kind: str, resource_id: str, expected_kind: str) -> bool:
    """Close a failed/abandoned operation so the reverse action can take over.

    A failed archive leaves the Session logically archived with its directory
    still active.  Unarchiving must be able to recover from that state instead
    of being blocked forever by the leftover operation.
    """
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        operation = value.get("operation")
        if not operation or operation.get("status") == "completed":
            return False
        if (
            operation.get("kind") != expected_kind
            or operation.get("status") not in {"failed", "abandoned"}
        ):
            return False
        archived = (
            session_paths(resource_id)[1].exists() if kind == "session" else False
        )
        value.update(blocked=archived, write_blocked=archived, deleted=False)
        operation.update(status="completed", stop_pending=False, result={})
        save_locked(kind, resource_id, value)
        return True


def set_visibility(
    kind: str,
    resource_id: str,
    *,
    blocked: bool,
    write_blocked: bool | None = None,
    deleted: bool = False,
) -> None:
    """Rewrite the logical visibility flags without touching the operation.

    Restore paths use this after a physical move succeeded or failed: an
    unarchived Session must be visible whenever its directory is back in the
    active area, even if a later cleanup step failed.
    """
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        value["blocked"] = blocked
        value["write_blocked"] = blocked if write_blocked is None else write_blocked
        if not blocked:
            value["deleted"] = deleted
        save_locked(kind, resource_id, value)


def session_paths(
    session_id: str, *, sessions_root: Path | None = None
) -> tuple[Path, Path]:
    validate_id(session_id)
    root = get_agent_sessions_dir() if sessions_root is None else sessions_root
    active, archived = root / session_id, root.parent / "sessions_archived" / session_id
    for path in (active, archived):
        from jiuwenswarm.runtime.kernel import canonical_path

        # Junctions and symlinks must never widen physical deletion scope.
        if path.is_symlink() or (
            path.exists() and Path(canonical_path(path)).parent != Path(canonical_path(path.parent))
        ):
            raise LifecycleError("BAD_REQUEST", "session path escapes managed storage")
    if active.exists() and archived.exists():
        raise LifecycleError(
            "SESSION_ID_CONFLICT", "session exists in both storage areas"
        )
    return active, archived


def resolve_session(
    session_id: str, *, must_exist: bool = True, sessions_root: Path | None = None
) -> Path:
    active, archived = session_paths(session_id, sessions_root=sessions_root)
    path = archived if archived.exists() else active
    if must_exist and not path.is_dir():
        raise LifecycleError("NOT_FOUND", "session not found")
    return path


def raw_metadata(session_id: str) -> dict:
    # Match the metadata writer's existing lifecycle lock across processes.
    # An unlocked Windows reader can race an atomic replacement and receive a
    # sharing violation during admission even though the Session is healthy.
    with resource_lock("session", session_id):
        path = resolve_session(session_id, must_exist=False)
        return read_json(path / "metadata.json")


def build_project_lookup() -> tuple[
    dict[str, list[tuple[str, str]]], dict[str, str]
]:
    """Build the legacy project-directory lookup once for a batch operation.

    Session inventories can contain many old metadata files without a
    ``project_id``.  Rebuilding this mapping per session turns that compatible
    fallback into an N+1 read of ``projects.json``.  Callers that enumerate
    sessions should build it lazily and pass it to :func:`project_id_for`.
    """
    from jiuwenswarm.server.runtime.session.project_store import (
        list_projects,
        _normalize_path_for_match,
    )

    projects = list_projects(include_hidden=True, cache_bust=True)
    by_directory: dict[str, list[tuple[str, str]]] = {}
    for project in projects:
        if project.project_dir:
            by_directory.setdefault(
                _normalize_path_for_match(project.project_dir), []
            ).append((project.project_id, project.work_mode))
    return (
        by_directory,
        {project.project_id: project.work_mode for project in projects},
    )


def project_id_for(
    meta: dict,
    *,
    project_lookup: tuple[dict[str, list[tuple[str, str]]], dict[str, str]]
    | None = None,
) -> str:
    if not meta.get("project_id") and meta.get("project_dir"):
        # Legacy list queries infer this association without writing it back.
        # Archive checks and cascade inventories must use the same association.
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _apply_metadata_defaults_with_inference,
        )

        by_directory, id_to_work_mode = project_lookup or build_project_lookup()
        meta = _apply_metadata_defaults_with_inference(
            str(meta.get("session_id") or ""),
            dict(meta),
            enable_writeback=False,
            dir_to_projects=by_directory,
            id_to_work_mode=id_to_work_mode,
        )
    return str(
        meta.get("project_id")
        or ("default_code" if meta.get("work_mode") == "code" else "default")
    )


def project_admission_snapshot(project_id: str) -> dict:
    """定时任务准入要的项目快照：还在不在、有没有被隐藏、现在能不能跑。

    读的是当前租户工作区里的项目名单和生命周期文件。调用方要先绑好租户。
    """
    project_id = validate_id(project_id)
    value = state("project", project_id)
    from jiuwenswarm.server.runtime.session.project_store import get_project_by_id

    project = get_project_by_id(project_id, cache_bust=True)
    payload = projection("project", project_id, value=value)
    payload["exists"] = project is not None
    payload["hidden"] = bool(project is not None and project.hidden)
    payload["operation"] = value.get("operation")
    return payload


def projection(
    kind: str,
    resource_id: str,
    *,
    project_id: str = "",
    value: dict | None = None,
    project_value: dict | None = None,
    archived: bool | None = None,
) -> dict:
    """Lifecycle projection of one resource.

    Callers that just read the state file (e.g. event_snapshots) can pass it
    via ``value`` — and the parent project's state via ``project_value`` — so
    the same files are not read again per session entry.
    """
    if value is None:
        value = state(kind, resource_id)
    operation = value.get("operation")
    if operation and operation["status"] == "completed":
        operation = None
    blocked = bool(value.get("blocked"))
    if kind == "session":
        blocked = blocked or (
            session_paths(resource_id)[1].exists() if archived is None else archived
        )
        if project_id:
            parent = projection("project", project_id, value=project_value)
            blocked = blocked or parent["execution_blocked"]
            operation = operation or parent["lifecycle_operation"]
    keys = (
        "operation_id",
        "resource_type",
        "resource_id",
        "kind",
        "status",
        "phase",
        "retryable",
        "updated_at",
        "stop_pending",
    )
    return dict(
        lifecycle_operation={k: operation.get(k) for k in keys} if operation else None,
        execution_blocked=blocked,
        stop_pending=bool(operation and operation.get("stop_pending")),
    )


def guard(session_id: str = "", project_id: str = "") -> None:
    if session_id:
        value = state("session", session_id)
        if value.get("blocked"):
            operation = value.get("operation", {})
            if operation.get("status") != "completed":
                raise LifecycleError(
                    "OPERATION_IN_PROGRESS",
                    "session lifecycle operation in progress",
                )
            if value.get("deleted"):
                raise LifecycleError(
                    "NOT_FOUND",
                    "session was permanently deleted",
                )
            raise LifecycleError(
                "SESSION_ARCHIVED",
                "session is archived",
            )
        if session_paths(session_id)[1].exists():
            raise LifecycleError("SESSION_ARCHIVED", "session is archived")
        project_id = project_id or project_id_for(raw_metadata(session_id))
    if project_id:
        value = state("project", project_id)
        if value.get("blocked"):
            if value.get("operation", {}).get("status") != "completed":
                raise LifecycleError(
                    "OPERATION_IN_PROGRESS",
                    "project lifecycle operation in progress",
                )
            raise LifecycleError("NOT_FOUND", "project was permanently deleted")


def fence_writes(kind: str, resource_id: str) -> None:
    with resource_lock(kind, resource_id):
        value = state(kind, resource_id)
        value["write_blocked"] = True
        save_locked(kind, resource_id, value)
        if kind == "project":
            active = get_agent_sessions_dir()
            for root in (active, active.parent / "sessions_archived"):
                for path in root.iterdir() if root.exists() else ():
                    if (
                        not path.is_dir()
                        or project_id_for(raw_metadata(path.name)) != resource_id
                    ):
                        continue
                    with resource_lock("session", path.name):
                        child = state("session", path.name)
                        child["generation"] = int(child.get("generation", 0)) + 1
                        if (
                            child.get("operation")
                            and child["operation"]["status"] != "completed"
                        ):
                            child["operation"]["generation"] = child["generation"]
                        save_locked("session", path.name, child)


def write_guard(session_id: str, generation: int | None = None) -> None:
    """Permit accepted drain writes until shutdown and its FIFO barriers finish."""
    value = state("session", session_id)
    if (
        value.get("write_blocked", value.get("blocked", False))
        or session_paths(session_id)[1].exists()
    ):
        raise LifecycleError("OPERATION_IN_PROGRESS", "session writes are isolated")
    current = value.get("generation", 0)
    allowed = {current}
    if value.get("blocked") and not value.get("write_blocked"):
        allowed.add(value.get("drain_generation", current))
    if generation is not None and generation not in allowed:
        raise LifecycleError("OPERATION_IN_PROGRESS", "stale session writer generation")
    pid = project_id_for(raw_metadata(session_id))
    parent = state("project", pid)
    if parent.get("write_blocked", parent.get("blocked", False)):
        raise LifecycleError("OPERATION_IN_PROGRESS", "project writes are isolated")


def _proc_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Keep runtime-owner records compatible with the shared process reader."""
    from jiuwenswarm.common.process_identity import proc_start_ticks

    return proc_start_ticks(pid, proc_root=proc_root)


def _runtime_owner_record(pid: int) -> dict[str, Any]:
    """Build a runtime-owner record without requiring ``/proc/stat``."""
    import psutil

    owner: dict[str, Any] = {"pid": pid}
    try:
        owner["started_at"] = psutil.Process(pid).create_time()
    except (psutil.Error, OSError) as exc:
        start_ticks = _proc_start_ticks(pid)
        if start_ticks is not None:
            owner["start_ticks"] = start_ticks
            logger.warning(
                "runtime owner pid=%s uses /proc/%s/stat fallback; "
                "psutil create_time unavailable: %s",
                pid,
                pid,
                exc,
            )
        else:
            # The first request may proceed, but later checks fail closed.
            owner["identity"] = "pid"
            logger.warning(
                "runtime owner pid=%s is pid-only; process identity cannot "
                "be verified in this sandbox: %s",
                pid,
                exc,
            )
    else:
        # Keep the kernel counter so a record remains verifiable on a
        # restrictive HarmonyOS build after being created on a permissive OS.
        start_ticks = _proc_start_ticks(pid)
        if start_ticks is not None:
            owner["start_ticks"] = start_ticks
    return owner


def current_runtime_owner() -> dict:
    """Return a PID and a PID-reuse-safe process identity."""
    return _runtime_owner_record(os.getpid())


def runtime_owner_alive(owner: dict) -> bool:
    """Check process identity conservatively when its state cannot be inspected."""
    import psutil

    try:
        process = psutil.Process(owner["pid"])
        owner_start_ticks = owner.get("start_ticks")
        owner_started_at = owner.get("started_at")
        if owner_start_ticks is not None:
            current_start_ticks = _proc_start_ticks(owner["pid"])
            if current_start_ticks is None:
                raise LifecycleError(
                    "OPERATION_IN_PROGRESS",
                    "cannot verify the previous runtime owner",
                )
            if int(current_start_ticks) != int(owner_start_ticks):
                return False
        if owner_started_at is not None:
            try:
                if abs(process.create_time() - owner_started_at) >= 0.01:
                    return False
            except psutil.NoSuchProcess:
                raise
            except (psutil.Error, OSError):
                if owner_start_ticks is None:
                    raise
        if owner_start_ticks is not None or owner_started_at is not None:
            status = process.status()
            if status == psutil.STATUS_ZOMBIE:
                # A zombie group leader may still have threads owning writers.
                return process.num_threads() > 1
            return status != psutil.STATUS_DEAD
        raise LifecycleError(
            "OPERATION_IN_PROGRESS", "cannot verify the previous runtime owner"
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError) as exc:
        raise LifecycleError("OPERATION_IN_PROGRESS", "cannot verify the previous runtime owner") from exc


def assert_runtime_owner(session_id: str) -> None:
    """Never move files while another live process owns untracked writers."""
    owner = state("session", session_id).get("runtime_owner")
    if not owner or owner["pid"] == os.getpid():
        return
    if runtime_owner_alive(owner):
        raise LifecycleError("OPERATION_IN_PROGRESS", "another live AgentServer owns the session runtime")


def claim_runtime(session_id: str) -> None:
    with resource_lock("session", session_id):
        guard(session_id)
        assert_runtime_owner(session_id)
        value = state("session", session_id)
        value["runtime_owner"] = current_runtime_owner()
        save_locked("session", session_id, value)


def release_runtime(session_id: str) -> None:
    with resource_lock("session", session_id):
        value = state("session", session_id)
        if value.get("runtime_owner", {}).get("pid") == os.getpid():
            value.pop("runtime_owner", None)
            save_locked("session", session_id, value)


def visible(meta: dict) -> bool:
    sid = str(meta.get("session_id") or "")
    archived = bool(sid and session_paths(sid)[1].exists())
    return not archived


def parse_ids(params: dict, *, delete: bool = False) -> list[str]:
    ids = params.get("session_ids", [])
    if "session_ids" in params and (
        not isinstance(ids, list) or not 1 <= len(ids) <= 100
    ):
        raise LifecycleError("BAD_REQUEST", "session_ids must contain 1 to 100 IDs")
    if delete and "session_id" in params:
        ids = [validate_id(params["session_id"]), *ids]
    if not ids:
        raise LifecycleError("BAD_REQUEST", "session_ids is required")
    result = list(dict.fromkeys(validate_id(value) for value in ids))
    if len(result) > 100:
        raise LifecycleError("BAD_REQUEST", "at most 100 distinct IDs are allowed")
    return result


def _as_page_int(value: object, *, default: int) -> int | None:
    """HTTP GET 查询串常把数字变成字符串；WS/JSON 则已是 int。"""
    if value is None:
        return default
    if type(value) is int and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def page(items: list[dict], params: dict, key: str, maximum: int) -> dict:
    limit = _as_page_int(params.get("limit", 20), default=20)
    offset = _as_page_int(params.get("offset", 0), default=0)
    if limit is None or not 1 <= limit <= maximum:
        raise LifecycleError("BAD_REQUEST", "invalid pagination")
    if offset is None or offset < 0:
        raise LifecycleError("BAD_REQUEST", "invalid pagination")
    work_mode, keyword = params.get("work_mode"), params.get("keyword", "")
    if (
        work_mode is not None
        and work_mode not in SUPPORTED_WORK_MODES
        or not isinstance(keyword, str)
    ):
        raise LifecycleError("BAD_REQUEST", "invalid filter")
    if "project_id" in params:
        validate_id(params["project_id"])
    keyword = keyword.strip().casefold()
    filtered = []
    for item in items:
        if work_mode is not None and item.get("work_mode") != work_mode:
            continue
        if (
            keyword
            not in str(
                item.get("title" if key == "sessions" else "name", "")
            ).casefold()
        ):
            continue
        if "project_id" in params and item.get("project_id") != params["project_id"]:
            continue
        filtered.append(item)
    id_key = "session_id" if key == "sessions" else "project_id"
    items = filtered
    items.sort(key=lambda item: (-float(item.get("archived_at") or 0), item[id_key]))
    total = len(items)
    selected = items[offset:offset + limit]
    return {
        key: selected,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(selected) < total,
    }


# gateway watcher 轮询周期是 2s：绝大多数轮询没有任何生命周期变化，却每次都
# 全量 glob+parse+projection。缓存上次快照与每个文件的 mtime 快照，目录与文件
# mtime 都未变时直接返回上次结果（一次轮询从 O(文件数) 降为 O(文件数) 的 stat，
# 不再读文件、不再 JSON 解析、不再 projection）。写方仍走 atomic_json 的
# tmp+rename，rename 必更新 mtime，因此任何状态变化都会使缓存失效；文件删除
# 由目录内容快照长度/文件集合变化捕获。缓存键含目录路径，多实例/多数据目录
# 互不污染；进程重启自然失效，不影响 correctness，只影响性能。
_SNAPSHOT_CACHE: dict[str, tuple[dict[str, float], list[dict]]] = {}
_SNAPSHOT_CACHE_LOCK = threading.Lock()


def clear_snapshot_cache() -> None:
    """公开测试钩子：重置 event_snapshots 的 mtime 快照缓存（避免 G.CLS.11）。"""
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE.clear()


def event_snapshots() -> list[dict]:
    """Internal Gateway refresh feed. Resource revisions survive restarts."""
    from jiuwenswarm.common.paths import get_lifecycle_dir

    directory = get_lifecycle_dir() / "resources"
    if not directory.exists():
        return []
    try:
        mtimes = {
            path.name: path.stat().st_mtime for path in directory.glob("*.json")
        }
    except OSError:
        mtimes = None  # 扫描窗口内文件被删/共享冲突：退回全量重扫
    if mtimes is not None:
        with _SNAPSHOT_CACHE_LOCK:
            cached = _SNAPSHOT_CACHE.get(str(directory))
            if cached is not None and cached[0] == mtimes:
                # 返回拷贝：缓存里的 list 是内部对象，调用方就地修改会
                # 毒化后续命中（当前消费方只序列化，防御性隔离）。
                return list(cached[1])
    result = []
    # Sessions share few projects: cache each project state once per poll and
    # hand every entry its own already-loaded value instead of letting
    # projection() re-read the same resource file.
    project_states: dict[str, dict] = {}
    paths_iter = (
        (directory / name for name in sorted(mtimes))
        if mtimes is not None
        else directory.glob("*.json")
    )
    for path in paths_iter:
        value = read_json(path)
        operation = value.get("operation")
        if not operation:
            continue
        kind, resource_id = operation["resource_type"], operation["resource_id"]
        if kind == "project" and operation["kind"] != "delete":
            continue
        project_id = operation.get(
            "project_id", resource_id if kind == "project" else "default"
        )
        project_value = None
        if kind == "session":
            if project_id not in project_states:
                project_states[project_id] = state("project", project_id)
            project_value = project_states[project_id]
        payload = dict(
            resource_id=resource_id,
            operation_id=operation["operation_id"],
            revision=value["revision"],
            project_id=project_id,
            **projection(
                kind,
                resource_id,
                project_id=project_id if kind == "session" else "",
                value=value,
                project_value=project_value,
            ),
        )
        if kind == "session":
            payload["session_id"] = resource_id
        result.append(
            dict(
                event=f"{kind}.lifecycle.updated",
                payload=payload,
                completed=operation["status"] == "completed",
                kind=operation["kind"],
                result=operation.get("result", {}),
            )
        )
    if mtimes is not None:
        with _SNAPSHOT_CACHE_LOCK:
            _SNAPSHOT_CACHE[str(directory)] = (mtimes, result)
    return result


# 死资源回收：complete 只在 JSON 上标记 deleted=True，文件本体与其 .lock 永不
# 清理，目录随历史会话无上界增长（同时拖累 event_snapshots 的全量扫描与
# file_lock 的孤儿锁）。回收挂在 events_feed 上节流触发——watcher 每 2s 轮询
# 一次，GC 每 _GC_INTERVAL_SECONDS 才真正执行一次，不额外起线程。
#
# 两段式回收（墓碑有退出机制，不永久保留）：
#   T+24h 压缩：watcher 已拉走最终 revision（事件不丢，原 JW-56 论证），
#     JSON 重写为最小墓碑（只保留 guard/begin 准入语义需要的字段），
#     同时回收伴生 .lock——锁文件占泄漏的大头。墓碑仍是陈旧前端引用的
#     兜底：gateway 的 session.deleted 推送只覆盖"绑定了该会话的窗口"，
#     未绑定窗口/离线标签页靠墓碑在 guard() 得到干净的 NOT_FOUND。
#   T+7d 退出：陈旧引用的实际存活期（标签页/休眠恢复）以天计，7 天后
#     墓碑本身也回收，"已删除"与"从未存在"不再区分。前端的实时感知由
#     session.deleted 推送负责（gateway 侧已存在），墓碑只兜长尾。
# 归档资源（deleted=False）不回收，archive 语义是"挪到 archived 区"而非
# 死数据；孤儿锁单独回收：资源 JSON 已不存在（或已可回收）的 .lock 文件
# 没有持有者（flock 只在 file_lock with 块内持有），直接删除不影响在途操作。
_GC_INTERVAL_SECONDS = 6 * 3600
GC_TOMBSTONE_COMPRESS_AFTER = 24 * 3600
GC_TOMBSTONE_EXPIRE_AFTER = 7 * 24 * 3600
# 节流键为数据目录路径（str），不是进程级单一时间戳：GC 清理的是目录级
# 目标，多数据目录（多租户/多实例）各自独立节流，互不压占对方的窗口。
_last_gc_attempt: dict[str, float] = {}
_GC_LOCK = threading.Lock()


def _resource_name(path: Path) -> tuple[str, str]:
    """从 resources 目录文件名解析 (kind, resource_id)。无效名返回 ("", "")。"""
    stem = path.stem
    for kind in ("session_", "project_"):
        if stem.startswith(kind):
            return kind.removesuffix("_"), stem[len(kind):]
    return "", ""


def _sweep_dead_resources(now: float) -> None:
    """压缩/回收已完结的死资源。幂等；O(目录)。

    改动（检视意见 2①/3①）：压缩与删除均包在 ``resource_lock`` 内——
    无锁的 read-modify-write 会吞掉并发 ``save_locked`` 的写入并回退
    revision；`.lock` 的删除改为"持锁探测"：先 ``file_lock`` 抢到互斥
    （证明此刻无人持有），再在持锁窗口内 unlink——POSIX flock 锁的是
    inode，无门槛 unlink 正被持有的锁文件会让并发 open 建立新 inode，
    两个进程各自持锁、互斥静默失效。
    """
    from jiuwenswarm.common.paths import get_lifecycle_dir

    directory = get_lifecycle_dir() / "resources"
    if not directory.exists():
        return
    try:
        names = [entry.name for entry in os.scandir(directory)]
    except OSError:
        return
    json_names = {
        name for name in names if name.endswith(".json") and not name.startswith(".")
    }
    # 判定只算一次，lock 循环与 json 循环共享（避免同一 (json, lock) 对
    # 重复 stat+read）。ready=可推进（压缩或删除），expired=退出期已到。
    # 删除循环只回收 expired 的路径：compress 候选必须先压后留，
    # 若在压缩前删除，_compress_tombstone 会 read 到 {} 把字段全部归零。
    ready: dict[str, bool] = {}
    expired: dict[str, bool] = {}
    compress: list[Path] = []
    for name in sorted(json_names):
        path = directory / name
        is_ready, is_expired = _resource_ready_for_removal(
            path,
            now - GC_TOMBSTONE_COMPRESS_AFTER,
            now - GC_TOMBSTONE_EXPIRE_AFTER,
        )
        ready[name] = is_ready
        expired[name] = is_expired
        if is_ready and not is_expired:
            compress.append(path)
    for name in names:
        if not name.endswith(".lock"):
            continue
        base = name[: -len(".lock")]
        # 伴生锁（JSON 仍在）：只在对应资源已到退出期时，连同 JSON 一起
        # 在 resource_lock 块内删除（见下）；此处只处理孤儿锁。
        if base in json_names:
            continue
        _remove_lock_under_probe(directory / name)
    expired_locks: list[Path] = []
    for name, is_expired_flag in expired.items():
        if not is_expired_flag:
            continue
        path = directory / name
        kind, resource_id = _resource_name(path)
        if not kind:
            _unlink_quietly(path)
            expired_locks.append(path.with_name(path.name + ".lock"))
            continue
        # 与 save_locked 同一把跨进程互斥：防止 GC 读旧值回写吞掉并发写。
        removed = False
        try:
            with resource_lock(kind, resource_id):
                # 拿到锁后再确认一次：可能刚被并发写复活（如重试恢复）。
                is_ready, is_expired_now = _resource_ready_for_removal(
                    path,
                    now - GC_TOMBSTONE_COMPRESS_AFTER,
                    now - GC_TOMBSTONE_EXPIRE_AFTER,
                )
                if is_ready and is_expired_now:
                    _unlink_quietly(path)
                    removed = True
        except OSError:
            logger.debug("lifecycle GC lock contention on %s", path.name)
        # 伴生锁在 resource_lock 块外删：file_lock 持有期间句柄开着，
        # Windows 上 unlink 必然 PermissionError（WinError 32）。出块后
        # JSON 已删，锁即孤儿，走孤儿锁删除路径。
        if removed:
            expired_locks.append(path.with_name(path.name + ".lock"))
    for lock_path in expired_locks:
        _remove_lock_under_probe(lock_path)
    # 压缩在删除之后：同一轮内先到期的直接删，未到期的压成墓碑。
    # 压缩是 resource_lock 内的 atomic_json 重写（mtime 更新），下轮按
    # 墓碑形态走退出分支。
    compress_locks: list[Path] = []
    for path in compress:
        kind, resource_id = _resource_name(path)
        if not kind:
            continue
        compressed = False
        try:
            with resource_lock(kind, resource_id):
                # 锁内复核：状态可能已被并发操作改变。
                is_ready, is_expired_flag = _resource_ready_for_removal(
                    path,
                    now - GC_TOMBSTONE_COMPRESS_AFTER,
                    now - GC_TOMBSTONE_EXPIRE_AFTER,
                )
                if is_ready and not is_expired_flag:
                    _compress_tombstone(path)
                    compressed = True
        except Exception:  # noqa: BLE001 - 单文件压缩失败不影响其余回收
            logger.debug("lifecycle GC tombstone compress failed: %s", path.name)
        # 压缩后锁回收，同样在 resource_lock 块外（句柄已关）。墓碑不再
        # 被加锁——guard/begin 对 completed+deleted 资源不会再碰。
        if compressed:
            compress_locks.append(path.with_name(path.name + ".lock"))
    for lock_path in compress_locks:
        _remove_lock_under_probe(lock_path)


def _remove_lock_under_probe(lock_path: Path) -> None:
    """孤儿锁删除：POSIX 上先 flock 抢锁证明无持有者再在持锁窗口内 unlink。

    只对"数据 JSON 已不存在"的孤儿锁使用。POSIX flock 锁的是 inode：
    持锁窗口内 unlink 后，迟到的 open 会创建新 inode 走全新生命周期，
    不会与旧持有者产生假互斥；无门槛 unlink 正被持有的锁文件会让并发
    open 建新 inode，两个进程各自持锁、互斥静默失效（检视意见 3①）。
    Windows 的 msvcrt 锁要求文件保持打开，而打开期间 unlink 必然
    PermissionError（实测 WinError 32），"持锁窗口内删"不可行——退化为
    直接 unlink：失败（恰被并发 file_lock 打开）即视为有持有者，留待
    下轮；Windows 上删除时机的守卫由"文件被打开则删不掉"这个平台
    行为本身提供。
    """
    try:
        if sys.platform == "win32":
            lock_path.unlink(missing_ok=True)
            return
        import fcntl

        with open(lock_path, "a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # 有并发持有者：留待下轮
            try:
                lock_path.unlink(missing_ok=True)
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except FileNotFoundError:
        return  # 并发已删，幂等
    except OSError:
        logger.debug("lifecycle GC skip locked file %s", lock_path.name)


def _resource_ready_for_removal(
    path: Path, compress_cutoff: float, expire_cutoff: float
) -> tuple[bool, bool]:
    """死资源可否推进（ready）以及是否已到退出期（expired）。

    ready=False：存活/归档/保留期内/瞬时不可读——本轮一律不动。
    ready=True 且 expired=False：可压缩为墓碑（guard 准入语义保留）。
    ready=True 且 expired=True：墓碑退出期已到，整文件回收。
    """
    try:
        mtime = path.stat().st_mtime
        value = read_json(path)
    except FileNotFoundError:
        return True, True  # 并发删除，幂等
    except OSError:
        # 瞬时不可读（Windows 共享冲突/杀软握持）：视为存活，本轮跳过，
        # 留待下轮。误删存活资源会丢执行栅栏，宁可漏删不可误删。
        return False, False
    except (LifecycleError, ValueError):
        # 内容损坏的死文件（ValueError 兜住 JSONDecodeError 与
        # UnicodeDecodeError——二进制垃圾在 json.loads 之前就抛）。
        # 超过退出期的按死文件回收；未超期的跳过，避免误删曾被瞬时
        # 写坏但仍有在途操作的资源。
        return (mtime <= expire_cutoff, True)
    operation = value.get("operation") or {}
    if not (operation.get("status") == "completed" and value.get("deleted")):
        # 存活或归档中：不动。死亡判据只有 deleted=True——
        # complete(archived=True) 会写 blocked=True/deleted=False，归档是
        # "目录挪到 sessions_archived/"的活台账（unarchive 靠它恢复），不是
        # 死数据；把 blocked 当死亡标志会把归档压成 deleted 墓碑，
        # 24h~7d 内 begin(unarchive) 抛 NOT_FOUND、7d 后归档记录被删除。
        return False, False
    if mtime > compress_cutoff:
        return False, False  # 刚完结未过压缩期：watcher 可能尚未取走最终 revision
    if _is_minimal_tombstone(value):
        # 已是压缩墓碑：过了退出期才真正删除。
        return (mtime <= expire_cutoff, mtime <= expire_cutoff)
    # 非墓碑的死资源：到压缩期即可推进（压缩或删除由调用方按 expired 分流）。
    return True, mtime <= expire_cutoff


def _is_minimal_tombstone(value: dict) -> bool:
    # 用压缩器写入的显式标记区分：原始 completed-delete JSON 同样满足
    # deleted/kind/status 条件，不能当已压缩墓碑（否则压缩段会被跳过）。
    return bool(value.get("tombstone"))


def _compress_tombstone(path: Path) -> None:
    """把死删除资源重写为最小墓碑（保住 guard/begin 准入语义）。

    只保留 guard()/begin()/logically_deleted() 判定所需字段；其余（result、
    completed_items、runtime_owner、stop_generation 等）在删除收尾后无消费方。
    原子写（tmp+rename）保证与读方并发安全。
    """
    value = read_json(path)
    operation = value.get("operation") or {}
    minimal = dict(
        revision=int(value.get("revision", 0)),
        generation=int(value.get("generation", 0)),
        deleted=True,
        blocked=True,
        write_blocked=True,
        tombstone=True,
        operation=dict(
            operation_id=str(operation.get("operation_id") or ""),
            resource_type=str(operation.get("resource_type") or ""),
            resource_id=str(operation.get("resource_id") or path.stem),
            kind="delete",
            status="completed",
            # event_snapshots() 按 operation.project_id 决定事件的会话/项目
            # 归属（缺失回退 "default"），watcher 依赖它路由到正确的 project
            # 订阅者；channel_id 同为消费方使用的归属字段，一并保留。
            project_id=str(operation.get("project_id") or "default"),
            channel_id=str(operation.get("channel_id") or ""),
            revision=int(operation.get("revision", value.get("revision", 0))),
            updated_at=operation.get("updated_at"),
        ),
    )
    atomic_json(path, minimal)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Windows 上锁文件恰好被并发 file_lock 打开时删除失败：留待下一轮 GC。
        logger.debug("lifecycle GC skip locked file %s", path.name)


def maybe_sweep(now: float | None = None) -> None:
    """公开 GC 入口：节流触发死资源/孤儿锁回收（供 events_feed 与测试调用）。"""
    if now is None:
        now = time.time()
    from jiuwenswarm.common.paths import get_lifecycle_dir

    key = str(get_lifecycle_dir() / "resources")
    with _GC_LOCK:
        if now - _last_gc_attempt.get(key, 0.0) < _GC_INTERVAL_SECONDS:
            return
        _last_gc_attempt[key] = now
    try:
        _sweep_dead_resources(now)
    except Exception:  # noqa: BLE001 - GC 失败绝不影响事件流
        logger.exception("lifecycle resource GC failed")


def events_feed() -> dict:
    """Gateway 后台轮询：全量 lifecycle 事件（不需要 project_id）。"""
    maybe_sweep()
    return {"events": event_snapshots()}


def inventory_feed() -> dict:
    """Gateway/运维：项目名单 + 未完成的 lifecycle 残片。"""
    from jiuwenswarm.server.runtime.session.project_store import list_projects

    payload = {
        "projects": [
            dict(
                project_id=p.project_id,
                operation=state("project", p.project_id).get("operation"),
            )
            for p in list_projects(include_hidden=True, cache_bust=True)
        ]
    }
    known = {item["project_id"] for item in payload["projects"]}
    from jiuwenswarm.common.paths import get_lifecycle_dir

    directory = get_lifecycle_dir() / "resources"
    for path in directory.glob("project_*.json") if directory.exists() else ():
        operation = read_json(path).get("operation") or {}
        project_id = operation.get("resource_id")
        if (
            project_id
            and project_id not in known
            and operation.get("status") != "completed"
        ):
            payload["projects"].append(
                dict(project_id=project_id, operation=operation)
            )
    return payload
