# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retain DeepResearch's completed stage snapshot as harness todos."""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.common.utils import get_tenant_agent_workspace_dir, logger

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
        try:
            fd = os.open(current, flags, mode=0o700)
        except OSError:
            # os.open cannot open directories on Windows Python; degrade the
            # fd-based identity check to the lstat identity (best-effort).
            chain.append((current, (before.st_dev, before.st_ino)))
            continue
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


async def capture_deepresearch_todo_target(todo_path: Path):
    """Capture the owning plan/backend before the research stream starts."""
    from openjiuwen.harness.tools.todo_store import bound_todo_tool, capture_plan
    tool, session_id = bound_todo_tool()
    if tool is None or session_id is None:
        return None
    if Path(tool.get_file_path(session_id)).resolve() != todo_path.resolve():
        return None
    owner = asyncio.current_task() if getattr(tool, "snapshot_guard", None) is not None else None
    return tool, session_id, await capture_plan(tool, session_id), owner


async def persist_deepresearch_task_update(
    payload: dict[str, Any], *, todo_path: Path, target=None,
) -> bool:
    """Update existing research stages in the captured plan; preserve other rows."""
    from openjiuwen.harness.tools.todo_store import current_transaction, transact
    tasks = _deepresearch_tasks(payload)
    if tasks is None or any(task["status"] != "completed" for task in tasks):
        return False
    if target is None:
        raise ValueError("DeepResearch Todo callback has no captured owner")
    tool, session_id, plan_id, owner = target

    def check_active():
        if owner is not None and (owner.done() or owner.cancelling()):
            raise ValueError("DeepResearch Todo callback belongs to an inactive request")

    check_active()
    if plan_id is None:
        return False
    trusted = _open_trusted_parent(todo_path)
    try:
        _read_existing_todo(todo_path, trusted)
        _verify_named_parent(todo_path, trusted)

        async def update():
            _verify_named_parent(todo_path, trusted)
            tx = current_transaction(tool, session_id)
            if getattr(tool.fs, "mode", None) == "local":
                tx.write_options["expected_parent_identity"] = trusted.identity
            updates = {item["id"]: item for item in tasks}
            # Only the workflow's canonical, already-present stages are eligible.
            if not all(any(row["id"] == ident and row.get("content") == change["content"]
                           for row in tx.snapshot.todos) for ident, change in updates.items()):
                logger.warning("[deepresearch_stream] skip Todo persistence: canonical stages missing or changed")
                return False
            now = datetime.now(timezone.utc).isoformat()
            tx.pending = [{**row, "status": "completed", "updatedAt": now}
                          if row["id"] in updates else row for row in tx.snapshot.todos]
            return True
        return await transact(tool, session_id, update, expected_plan=plan_id, check_active=check_active)
    finally:
        if trusted.fd is not None:
            os.close(trusted.fd)
