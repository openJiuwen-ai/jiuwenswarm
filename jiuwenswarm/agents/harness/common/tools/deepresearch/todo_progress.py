# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retain DeepResearch's completed stage snapshot as harness todos."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.common.utils import get_tenant_agent_workspace_dir

_TODO_WRITE_LOCK = threading.Lock()
_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
_INVALID_PATH = "deepresearch_todo_invalid_path"
_MAX_COMPONENT_BYTES = 255
_MAX_TODO_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class _TrustedParent:
    fd: int | None
    identity: tuple[int, int]
    ancestors: tuple[tuple[Path, tuple[int, int]], ...] = ()


def _validate_component(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(_INVALID_PATH)
    invalid_component = (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
        or len(value.encode("utf-8")) > _MAX_COMPONENT_BYTES
    )
    if invalid_component:
        raise ValueError(_INVALID_PATH)
    return value


def deepresearch_todo_path(
    *,
    session_id: str,
    workspace_key: str | None = None,
) -> Path:
    """Return the standard harness todo.json path for one tenant session."""
    safe_session = _validate_component(session_id)
    if workspace_key is None:
        wk = "default"
    else:
        wk = _validate_component(str(workspace_key).strip())
    workspace = get_tenant_agent_workspace_dir(wk)
    return workspace / "todo" / safe_session / "todo.json"


def _validate_direct_path(path: Path) -> None:
    if not path.is_absolute():
        raise OSError("unsafe todo parent")
    for part in path.parent.parts[1:]:
        if (
            part in {".", ".."}
            or "\0" in part
            or len(part.encode("utf-8")) > _MAX_COMPONENT_BYTES
        ):
            raise OSError("unsafe todo parent")


def _open_posix_parent(path: Path, *, create: bool) -> int:
    _validate_direct_path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(Path(path.anchor or "/"), flags, mode=0o700)
    try:
        for part in path.parent.parts[1:]:
            try:
                next_fd = os.open(part, flags, mode=0o700, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, flags, mode=0o700, dir_fd=fd)
            try:
                os.close(fd)
            except BaseException:
                with suppress(OSError):
                    os.close(next_fd)
                raise
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_windows_parent(
    path: Path, *, create: bool
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    """Best-effort Windows ancestor chain with lstat/open/fstat identity."""
    _validate_direct_path(path)
    current = Path(path.anchor)
    chain: list[tuple[Path, tuple[int, int]]] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for part in (None, *path.parent.parts[1:]):
        if part is not None:
            current = current / part
        try:
            before = current.lstat()
        except FileNotFoundError:
            if not create or part is None:
                raise
            current.mkdir(mode=0o700)
            before = current.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise OSError("unsafe todo parent")
        fd = os.open(current, flags, mode=0o700)
        try:
            opened = os.fstat(fd)
        finally:
            os.close(fd)
        identity = (before.st_dev, before.st_ino)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or identity != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("unsafe todo parent")
        chain.append((current, identity))
    return tuple(chain)


def _open_trusted_parent(path: Path) -> _TrustedParent:
    """Hold the parent used by all reads, writes, replace and cleanup."""
    if os.name == "nt":
        ancestors = _open_windows_parent(path, create=True)
        return _TrustedParent(None, ancestors[-1][1], ancestors)
    fd = _open_posix_parent(path, create=True)
    opened = os.fstat(fd)
    return _TrustedParent(fd, (opened.st_dev, opened.st_ino))


def _verify_named_parent(path: Path, trusted: _TrustedParent) -> None:
    """Fail if the held directory is no longer the named tenant parent."""
    if trusted.fd is None:
        current_chain = _open_windows_parent(path, create=False)
        if current_chain != trusted.ancestors:
            raise OSError("unsafe todo parent")
        return
    named_fd = _open_posix_parent(path, create=False)
    try:
        current = os.fstat(named_fd)
        if (current.st_dev, current.st_ino) != trusted.identity:
            raise OSError("unsafe todo parent")
    finally:
        os.close(named_fd)


def _leaf_stat(todo_path: Path, trusted: _TrustedParent) -> os.stat_result:
    if trusted.fd is None:
        return todo_path.lstat()
    return os.stat(
        todo_path.name,
        dir_fd=trusted.fd,
        follow_symlinks=False,
    )


def _open_leaf(todo_path: Path, trusted: _TrustedParent, flags: int) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if trusted.fd is None:
        return os.open(todo_path, flags, mode=0o600)
    return os.open(todo_path.name, flags, mode=0o600, dir_fd=trusted.fd)


def _read_existing_todo(
    todo_path: Path, trusted: _TrustedParent
) -> bytes | None:
    try:
        before = _leaf_stat(todo_path, trusted)
    except FileNotFoundError:
        return None
    invalid_named_leaf = (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > _MAX_TODO_BYTES
    )
    if invalid_named_leaf:
        raise OSError("unsafe todo leaf")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = _open_leaf(todo_path, trusted, flags)
    try:
        opened = os.fstat(fd)
        invalid_open_leaf = (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > _MAX_TODO_BYTES
            or (before.st_dev, before.st_ino)
            != (opened.st_dev, opened.st_ino)
        )
        if invalid_open_leaf:
            raise OSError("unsafe todo leaf")
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65_536, _MAX_TODO_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_TODO_BYTES:
                raise OSError("unsafe todo leaf")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
            raise OSError("unsafe todo leaf")
        return bytes(data)
    finally:
        os.close(fd)


def _existing_created_at(
    todo_path: Path, trusted: _TrustedParent
) -> dict[str, str]:
    try:
        raw = _read_existing_todo(todo_path, trusted)
        if raw is None:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    created_at_by_id = {}
    for item in data:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("createdAt"), str)
        ):
            created_at_by_id[str(item.get("id"))] = item["createdAt"]
    return created_at_by_id


def _allocate_temp(
    todo_path: Path, trusted: _TrustedParent
) -> tuple[str, int, tuple[int, int]]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(32):
        name = f".deepresearch-todo-{secrets.token_hex(16)}.json"
        try:
            if trusted.fd is None:
                fd = os.open(todo_path.parent / name, flags, 0o600)
            else:
                fd = os.open(name, flags, 0o600, dir_fd=trusted.fd)
        except FileExistsError:
            continue
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(fd)
            raise OSError("unsafe todo temp")
        return name, fd, (opened.st_dev, opened.st_ino)
    raise OSError("todo temp allocation failed")


def _cleanup_owned_temp(
    todo_path: Path,
    trusted: _TrustedParent,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        if trusted.fd is None:
            current = (todo_path.parent / name).lstat()
        else:
            current = os.stat(
                name, dir_fd=trusted.fd, follow_symlinks=False
            )
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and (current.st_dev, current.st_ino) == identity
    ):
        if trusted.fd is None:
            os.unlink(todo_path.parent / name)
        else:
            os.unlink(name, dir_fd=trusted.fd)


def _deepresearch_tasks(payload: dict[str, Any]) -> list[dict[str, str]] | None:
    if payload.get("event_type") != "task.update":
        return None
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != len(DEEPRESEARCH_STAGES):
        return None

    normalized = []
    for index, title in enumerate(DEEPRESEARCH_STAGES, start=1):
        item = tasks[index - 1]
        expected_id = f"deepresearch_stage_{index}"
        if not isinstance(item, dict):
            return None
        if item.get("task_id") != expected_id or item.get("task_content") != title:
            return None
        status = item.get("status")
        if status not in _TODO_STATUSES:
            return None
        normalized.append({
            "id": expected_id,
            "content": title,
            "activeForm": title,
            "status": status,
        })
    return normalized


def persist_deepresearch_task_update(
    payload: dict[str, Any],
    *,
    todo_path: Path,
) -> bool:
    """Retain a completed canonical task.update in the standard todo file."""
    tasks = _deepresearch_tasks(payload)
    if tasks is None or any(task["status"] != "completed" for task in tasks):
        return False

    todo_path = Path(todo_path)
    with _TODO_WRITE_LOCK:
        trusted = _open_trusted_parent(todo_path)
        temp_name = ""
        temp_identity: tuple[int, int] | None = None
        try:
            created_at = _existing_created_at(todo_path, trusted)
            now = datetime.now(timezone.utc).isoformat()
            items = [
                {
                    **task,
                    "createdAt": created_at.get(task["id"], now),
                    "updatedAt": now,
                }
                for task in tasks
            ]
            temp_name, temp_fd, temp_identity = _allocate_temp(
                todo_path, trusted
            )
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                json.dump(items, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                opened = os.fstat(temp_file.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != temp_identity
                ):
                    raise OSError("unsafe todo temp")
            _verify_named_parent(todo_path, trusted)
            if trusted.fd is not None:
                os.replace(
                    temp_name,
                    todo_path.name,
                    src_dir_fd=trusted.fd,
                    dst_dir_fd=trusted.fd,
                )
            else:
                os.replace(todo_path.parent / temp_name, todo_path)
                _verify_named_parent(todo_path, trusted)
        finally:
            if temp_name and temp_identity is not None:
                _cleanup_owned_temp(
                    todo_path, trusted, temp_name, temp_identity
                )
            if trusted.fd is not None:
                os.close(trusted.fd)
    return True
