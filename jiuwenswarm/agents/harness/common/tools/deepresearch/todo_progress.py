# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retain DeepResearch's completed stage snapshot as harness todos."""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.core.common.exception.errors import BaseError

from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    DEEPRESEARCH_STAGES,
)
from jiuwenswarm.common.todo_protocol import todo_v2_enabled
from jiuwenswarm.common.utils import get_tenant_agent_workspace_dir

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


def persist_deepresearch_task_update(payload: dict[str, Any], *, todo_path: Path) -> bool:
    """拒绝没有宿主归属的旧同步写入口；纯工作流事件仍可正常交付。"""
    tasks = _deepresearch_tasks(payload)
    if tasks is None or any(task["status"] != "completed" for task in tasks):
        return False
    raise RuntimeError("DeepResearch Todo writes require a captured owner and the async transaction API")


async def capture_deepresearch_todo_binding(
    tool: Any, session_id: str, workflow_id: str, *, previous: dict | None = None,
    fresh: dict | None = None, allow_initialize: bool = True,
) -> dict | None:
    """在工作开始前冻结真实存储对象；旧无归属状态不能事后认领父计划。"""
    if tool is None or not callable(getattr(tool, "todo_store", None)):
        return None
    store = tool.todo_store(session_id)
    if not await store.capable():
        return None
    binding = dict(previous) if isinstance(previous, dict) else None
    if binding and binding.get("store_key", store.key) != store.key:
        return None
    if todo_v2_enabled():
        if binding is not None:
            if isinstance(fresh, dict) and binding.get("store_id") == fresh.get("store_id") and fresh.get("plan_id") in {
                binding.get("plan_id"), binding.get("planned_plan_id"),
            }:
                binding.update(fresh)
        elif allow_initialize and isinstance(fresh, dict):
            binding = dict(fresh)
        if not binding or not binding.get("store_id"):
            return None
        binding.setdefault("planned_plan_id", str(uuid.uuid5(uuid.NAMESPACE_OID, f"{binding['store_id']}:{workflow_id}")))
    elif binding is None:
        if not allow_initialize:
            return None
        trusted = _open_trusted_parent(Path(store.path)) if store.key.startswith("local:") else None
        try:
            if trusted is not None:
                _read_existing_todo(Path(store.path), trusted)
                _verify_named_parent(Path(store.path), trusted)
            snapshot = await store.read(protect_path=trusted is not None)
            if trusted is not None:
                _verify_named_parent(Path(store.path), trusted)
        finally:
            if trusted is not None and trusted.fd is not None:
                os.close(trusted.fd)
        if snapshot.metadata is not None:
            return None
        binding = {"initial_exists": snapshot.exists, "initial_content_sha256": snapshot.content_sha256}
    elif binding.get("store_id"):
        return None
    binding.update(store_key=store.key, session_id=session_id, workflow_id=workflow_id)
    return binding


async def persist_deepresearch_task_update_transactional(
    payload: dict[str, Any], *, todo_path: Path, tool: Any = None,
    binding: dict[str, Any] | None = None, session: Any = None,
) -> Any:
    """Only a captured workflow owner may update its existing stage rows.

    An unrelated parent plan is never replaced. An empty/finished plan may be
    explicitly initialized by a new research workflow; the planned identity is
    recorded before commit so an interrupted response can resume safely.
    """
    tasks = _deepresearch_tasks(payload)
    if tasks is None or tool is None or not binding or not binding.get("workflow_id") or not binding.get("session_id"):
        return None
    session_id = binding["session_id"]
    store = tool.todo_store(session_id)
    if binding.get("store_key", store.key) != store.key:
        return None
    versioned = todo_v2_enabled()
    if versioned and not binding.get("store_id"):
        return None
    expected = dict(binding) if versioned else None
    owner = str(binding["workflow_id"])
    planned = binding.setdefault("planned_plan_id", str(uuid.uuid5(uuid.NAMESPACE_OID, f"{binding['store_id']}:{owner}"))) if versioned else None
    now = datetime.now(timezone.utc).isoformat()
    trusted = None
    path = Path(store.path)
    if store.key.startswith("local:"):
        trusted = _open_trusted_parent(path)
        try:
            _read_existing_todo(path, trusted)
        except BaseException:
            if trusted.fd is not None:
                os.close(trusted.fd)
            raise

    async def apply():
        if trusted is not None:
            _verify_named_parent(path, trusted)
        transaction = store.active()
        owned = [item for item in transaction.todos
                 if (item.get("meta_data") or {}).get("deepresearch_workflow_id") == owner]
        initialized = bool(owned) or binding.get("owns_stages") or (transaction.metadata is not None and transaction.metadata.get("plan_id") == planned)
        if not initialized:
            if not versioned and (transaction.original.exists != binding.get("initial_exists") or
                                  transaction.original.content_sha256 != binding.get("initial_content_sha256")):
                return {"owned": False}
            plan = [item for item in transaction.todos if not (item.get("meta_data") or {}).get("persistent")]
            if any(item.get("status", "pending") not in {"completed", "cancelled"} for item in plan):
                return {"owned": False}
            invoke_id = str(binding.get("invoke_id") or owner)
            if transaction.metadata and transaction.metadata.get("last_create_invoke_id") == invoke_id:
                return {"owned": False}
            reminders = [item for item in transaction.todos if (item.get("meta_data") or {}).get("persistent")
                         and item.get("status", "pending") not in {"completed", "cancelled"}]
            if {item.get("id") for item in reminders} & {task["id"] for task in tasks}:
                raise ValueError("DeepResearch stage IDs conflict with persistent reminders")
            new_items = [{**task, "generation_token": (transaction.metadata or {}).get("generation_token"),
                          "createdAt": now, "updatedAt": now,
                          "meta_data": {"deepresearch_workflow_id": owner}} for task in tasks]
            if versioned:
                transaction.todos = new_items + reminders
                transaction.metadata.update(plan_id=planned, last_create_invoke_id=invoke_id, legacy_plan=False)
            else:
                # 数组没有计划版本边界；保留所有既有条目，只在冻结的初始快照上追加自己的阶段。
                if {item.get("id") for item in transaction.todos} & {item["id"] for item in new_items}:
                    return {"owned": False}
                transaction.todos.extend(new_items)
        else:
            desired = {item["id"]: item["status"] for item in tasks}
            rank = {"pending": 0, "in_progress": 1, "completed": 2, "cancelled": 3}
            for item in owned:
                status = desired.get(item.get("id"))
                if status is not None and rank.get(status, 0) > rank.get(item.get("status"), 0):
                    item["status"] = status
                    item["updatedAt"] = now
        return {"owned": True}

    try:
        async def commit():
            return await store.transact(apply, binding=expected, invoke_id=str(binding.get("invoke_id") or owner),
                                        expected_parent_identity=trusted.identity if trusted else None)
        try:
            result = await commit()
        except BaseError as exc:
            if not versioned or not isinstance(exc.details, dict) or exc.details.get("todo_error") != "stale_plan":
                raise
            # Read-confirm a previously initialized plan whose response was lost.
            # The writer epoch must still match; this cannot revive a superseded execution.
            snapshot = await store.read(protect_path=trusted is not None)
            meta = snapshot.metadata or {}
            if meta.get("store_id") != expected.get("store_id") or meta.get("plan_id") != planned \
                    or meta.get("writer_epoch") != expected.get("writer_epoch"):
                raise
            expected["plan_id"] = planned
            result = await commit()
        if not result.get("owned"):
            return None
        receipt = result.todo_receipt
        binding["owns_stages"] = True
        if receipt.snapshot.metadata is not None:
            binding.update(receipt.snapshot.binding(), revision=receipt.snapshot.metadata["revision"])
            tool.publish_commit_context(session_id, receipt, session=session, invoke_id=str(binding.get("invoke_id") or owner))
        return receipt
    finally:
        if trusted is not None and trusted.fd is not None:
            os.close(trusted.fd)
