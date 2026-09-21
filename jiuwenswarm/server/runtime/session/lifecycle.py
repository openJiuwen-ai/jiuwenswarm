"""Persistent lifecycle fences shared by request admission and disk writers.

The resource file is the commit point: it embeds the current operation so a
crash between journal and pointer writes cannot expose an unfenced resource.
No process-local cache is used for admission decisions.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
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
def resource_lock(kind: str, resource_id: str) -> Iterator[None]:
    path = resource_path(kind, resource_id)
    held = getattr(_HELD_LOCKS, "paths", None)
    if held is None:
        held = _HELD_LOCKS.paths = set()
    if path in held:
        yield
        return
    with file_lock(path):
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)


def state(kind: str, resource_id: str) -> dict:
    return read_json(resource_path(kind, resource_id)) if resource_id else {}


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


def checkpoint_project(resource_id: str, params: dict) -> dict:
    """Validate the token and commit progress under one cross-process lock."""
    with resource_lock("project", resource_id):
        value = state("project", resource_id)
        operation = value.get("operation") or {}
        if (
            params.get("operation_id") != operation.get("operation_id")
            or params.get("generation") != operation.get("generation")
            or operation.get("status") == "completed"
        ):
            raise LifecycleError("OPERATION_IN_PROGRESS", "stale operation")
        if "completed_cron_job_ids" in params:
            completed = dict(operation.get("completed_items", {}))
            completed["cron"] = list(
                dict.fromkeys(
                    [*completed.get("cron", []), *params["completed_cron_job_ids"]]
                )
            )
            operation["completed_items"] = completed
        if "planned_cron_job_ids" in params:
            operation["planned_cron_job_ids"] = list(
                dict.fromkeys(
                    [
                        *operation.get("planned_cron_job_ids", []),
                        *params["planned_cron_job_ids"],
                    ]
                )
            )
        if params.get("failed"):
            operation.update(
                status="failed",
                phase=params.get("phase", "stop_cron"),
                errors=[params.get("error", "cron stage failed")],
            )
        save_locked("project", resource_id, value)
        return operation


def session_paths(
    session_id: str, *, sessions_root: Path | None = None
) -> tuple[Path, Path]:
    validate_id(session_id)
    root = get_agent_sessions_dir() if sessions_root is None else sessions_root
    active, archived = root / session_id, root.parent / "sessions_archived" / session_id
    for path in (active, archived):
        # Junctions and symlinks must never widen physical deletion scope.
        if path.is_symlink() or (
            path.exists() and path.resolve().parent != path.parent.resolve()
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
            code = (
                "OPERATION_IN_PROGRESS"
                if operation.get("status") != "completed"
                else ("NOT_FOUND" if value.get("deleted") else "SESSION_ARCHIVED")
            )
            raise LifecycleError(code, "session lifecycle blocks this operation")
        if session_paths(session_id)[1].exists():
            raise LifecycleError("SESSION_ARCHIVED", "session is archived")
        project_id = project_id or project_id_for(raw_metadata(session_id))
    if project_id:
        value = state("project", project_id)
        if value.get("blocked"):
            code = (
                "OPERATION_IN_PROGRESS"
                if value.get("operation", {}).get("status") != "completed"
                else "NOT_FOUND"
            )
            raise LifecycleError(code, "project lifecycle blocks this operation")


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


def assert_runtime_owner(session_id: str) -> None:
    """Never move files while another live process owns untracked writers."""
    import psutil

    owner = state("session", session_id).get("runtime_owner")
    if not owner or owner["pid"] == os.getpid():
        return
    try:
        process = psutil.Process(owner["pid"])
        if abs(process.create_time() - owner["started_at"]) < 0.01:
            raise LifecycleError(
                "OPERATION_IN_PROGRESS",
                "another live AgentServer owns the session runtime",
            )
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise LifecycleError(
            "OPERATION_IN_PROGRESS", "cannot verify the previous runtime owner"
        ) from exc


def claim_runtime(session_id: str) -> None:
    import psutil

    with resource_lock("session", session_id):
        guard(session_id)
        assert_runtime_owner(session_id)
        value = state("session", session_id)
        value["runtime_owner"] = dict(
            pid=os.getpid(), started_at=psutil.Process().create_time()
        )
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


def page(items: list[dict], params: dict, key: str, maximum: int) -> dict:
    limit, offset = params.get("limit", 20), params.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise LifecycleError("BAD_REQUEST", "invalid pagination")
    if type(offset) is not int or offset < 0:
        raise LifecycleError("BAD_REQUEST", "invalid pagination")
    work_mode, keyword = params.get("work_mode"), params.get("keyword", "")
    if (
        work_mode is not None
        and work_mode not in ("work", "code")
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


def event_snapshots() -> list[dict]:
    """Internal Gateway refresh feed. Resource revisions survive restarts."""
    directory = get_agent_root_dir() / "lifecycle" / "resources"
    result = []
    # Sessions share few projects: cache each project state once per poll and
    # hand every entry its own already-loaded value instead of letting
    # projection() re-read the same resource file.
    project_states: dict[str, dict] = {}
    for path in directory.glob("*.json") if directory.exists() else ():
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
    return result


def migrate_project_archives() -> None:
    """One-time upgrade; never enable cron or clear session/delete fences."""
    from jiuwenswarm.server.runtime.session import project_store

    marker = get_agent_root_dir() / "lifecycle" / "project_delete_v2.json"
    with file_lock(marker):
        if marker.exists():
            return
        resources = marker.parent / "resources"
        for path in resources.glob("project_*.json") if resources.exists() else ():
            try:
                value = read_json(path)
                operation = value.get("operation") or {}
                if operation.get("kind") not in {"archive", "unarchive"}:
                    continue
                pid = operation["resource_id"]
                with resource_lock("project", pid):
                    value = state("project", pid)
                    if (value.get("operation") or {}).get("kind") not in {
                        "archive",
                        "unarchive",
                    }:
                        continue
                    value.update(blocked=False, write_blocked=False)
                    value["operation"].update(
                        status="completed", stop_pending=False, result={}, migrated=True
                    )
                    save_locked("project", pid, value)
            except Exception:
                # 单个损坏的生命周期文件不应阻断其余项目的迁移。
                logger.warning(
                    "project archive migration skipped %s", path.name, exc_info=True
                )
        project_store.migrate_archived_projects()
        atomic_json(marker, {"version": 2, "completed_at": time.time()})
