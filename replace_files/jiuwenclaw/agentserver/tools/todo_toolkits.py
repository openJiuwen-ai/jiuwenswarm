# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Todo toolkit for agent task tracking.

Provides todo_create, todo_complete, todo_insert, todo_remove tools that persist
tasks to a markdown file under the predefined session directory. Tools can be
registered in the openJiuwen Runner via TodoToolkit.get_tools().

TodoToolkit 可无参构造，此时 session_id 在每次工具调用时通过
jiuwenclaw.agentserver.plan_todo_context.get_plan_todo_session_id() 动态解析，
使一个全局注册的实例能在多 session 并发下正确路由到各自的
agent/sessions/{session_id}/ 文件。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Dict, List, Optional

from pydantic import BaseModel

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.utils import get_agent_sessions_dir


class TaskStatus(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoTask(BaseModel):
    idx: int
    tasks: str
    status: TaskStatus
    result: str = ""


# Operation kinds emitted by the toolkit. Rails match on these exact strings
# (no string-prefix sniffing on user-facing messages).
class TodoOpKind(str, Enum):
    CREATE = "create"
    START = "start"
    COMPLETE = "complete"
    INSERT = "insert"
    REMOVE = "remove"


@dataclass(frozen=True)
class TodoOpResult:
    """
    Structured result of a write operation. ``cancelled`` tasks are
    excluded from both ``remaining_count`` and ``total_count`` (treated as
    voided, not as plan members).
    """
    kind: TodoOpKind
    success: bool
    message: str             # human-readable string returned to the LLM
    remaining_count: int     # waiting + running, after the op
    total_count: int         # waiting + running + completed, after the op
    all_completed: bool      # True iff total_count > 0 and remaining_count == 0


# Per-session publish/consume of the most recent TodoOpResult. The toolkit
# publishes inside the user-facing method (within the per-session lock) and
# the rail consumes once in after_tool_call. Same-session writers are
# serialized by the lock; cross-session writes are isolated by key.
_last_op_result: Dict[str, TodoOpResult] = {}
_last_op_result_lock = threading.Lock()


def _publish_op_result(session_id: str, result: TodoOpResult) -> None:
    if not session_id:
        return
    with _last_op_result_lock:
        _last_op_result[session_id] = result


def consume_last_op_result(session_id: str) -> Optional[TodoOpResult]:
    """Pop the most recent TodoOpResult for ``session_id`` (one-shot)."""
    if not session_id:
        return None
    with _last_op_result_lock:
        return _last_op_result.pop(session_id, None)


def reset_op_results() -> None:
    """Clear the entire ``TodoOpResult`` publish-bus across all sessions.

    Intended for test isolation between cases that share the process-global
    publish bus. Production code should not need this — use
    :func:`consume_last_op_result` for normal one-shot consumption.
    """
    with _last_op_result_lock:
        _last_op_result.clear()


def _resolve_runtime_session_id() -> str:
    """从 plan_todo_context 解析当前请求 session_id，失败兜底 "default"。"""
    try:
        from jiuwenclaw.agentserver.plan_todo_context import get_plan_todo_session_id
        return get_plan_todo_session_id() or "default"
    except Exception:
        return "default"


class TodoToolkit:
    """Toolkit for agent todo task tracking. Persists tasks to markdown under session dir."""

    TODO_FILENAME: ClassVar[str] = "todo.md"
    TOOL_PREFIX: ClassVar[str] = "todo"

    # Subclasses may flip these to tailor which surface is exposed without
    # rewriting get_tools.
    EXPOSE_START: ClassVar[bool] = True
    EXPOSE_COMPLETE_BATCH: ClassVar[bool] = False

    # Markdown line format: - [ ] 1. task | status  or  - [x] 1. task | status | result
    # parts[0]=task, parts[1]=status, parts[2]=result (optional)
    PARTS_MIN_COUNT_WITH_RESULT = 3  # 至少 3 段才有 result
    PARTS_INDEX_RESULT = 2  # result 在 split("|") 后的索引

    # 按 {class}:{session_id} 分组的文件锁，防止并发任务对同一 todo.md 进行
    # read-modify-write 时丢失更新；不同 toolkit 子类不共享锁。
    _session_locks: ClassVar[Dict[str, threading.Lock]] = {}
    _meta_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _get_session_lock(cls, session_id: str) -> threading.Lock:
        """获取指定 session 的文件操作锁（线程安全）."""
        key = f"{cls.__name__}:{session_id}"
        with cls._meta_lock:
            if key not in cls._session_locks:
                cls._session_locks[key] = threading.Lock()
            return cls._session_locks[key]

    def __init__(self, session_id: Optional[str] = None, todo_dir: Path | None = None):
        """Initialize TodoToolkit for a session.

        Args:
            session_id: Session/conversation identifier for scoping todo files. 为 None
                时每次工具调用从 plan_todo_context 动态解析，同一实例可被多 session 并发共用。
            todo_dir: Optional custom directory. Defaults to agent/sessions/{session_id}/.
        """
        self._explicit_session_id: Optional[str] = session_id
        self._fixed_todo_dir: Optional[Path] = Path(todo_dir) if todo_dir is not None else None

    @property
    def session_id(self) -> str:
        return self._explicit_session_id or _resolve_runtime_session_id()

    def set_session_id(self, session_id: str) -> None:
        """Set the session ID for this toolkit instance.

        Args:
            session_id: The session/conversation identifier to use.
        """
        self._explicit_session_id = session_id

    @property
    def todo_dir(self) -> Path:
        if self._fixed_todo_dir is not None:
            todo_dir = self._fixed_todo_dir
        else:
            todo_dir = get_agent_sessions_dir() / self.session_id
        todo_dir.mkdir(parents=True, exist_ok=True)
        return todo_dir

    @property
    def _todo_path(self) -> Path:
        return self.todo_dir / self.__class__.TODO_FILENAME

    def resolve_todo_path(self) -> tuple[str, Path]:
        """Return (session_id, todo_file_path) for external callers (e.g. rails)."""
        return self.session_id, self._todo_path

    def load_tasks(self) -> List[TodoTask]:
        """Public wrapper of :meth:`_load_tasks` for external callers (e.g. rails)."""
        return self._load_tasks()

    def clear_tasks(self) -> bool:
        """Delete the todo file for the current session if it exists.

        Returns True if a file was actually removed, False if there was nothing
        to remove. Safe under the per-session lock.
        """
        with self._get_session_lock(self.session_id):
            path = self._todo_path
            if not path.exists():
                return False
            path.unlink()
            return True

    def _load_tasks(self) -> List[TodoTask]:
        """Load tasks from markdown file."""
        if not self._todo_path.exists():
            return []
        tasks: List[TodoTask] = []
        with open(self._todo_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: - [ ] 1. task | status  or  - [x] 1. task | status | result
                if "[-]" in line:
                    status = TaskStatus.CANCELLED
                    checked = False
                elif "[>]" in line:
                    status = TaskStatus.RUNNING
                    checked = False
                else:
                    checked = "[x]" in line.lower() or "[√]" in line
                    status = TaskStatus.COMPLETED if checked else TaskStatus.WAITING
                result = ""
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= self.PARTS_MIN_COUNT_WITH_RESULT:
                    result = parts[self.PARTS_INDEX_RESULT]
                # 解析状态字段（第二段）
                if len(parts) >= 2:
                    status_str = parts[1].strip().lower()
                    if status_str == "running":
                        status = TaskStatus.RUNNING
                    elif status_str == "waiting":
                        status = TaskStatus.WAITING
                    elif status_str == "completed":
                        status = TaskStatus.COMPLETED
                    elif status_str == "cancelled":
                        status = TaskStatus.CANCELLED
                # 解析行："- [ ] 1. xxx" / "- [x] 1. xxx" / "- [>] 1. xxx"
                rest = line.replace("- [x]", "").replace("- [-]", "").replace("- [>]", "").replace("- [ ]", "").strip()
                if "." in rest:
                    idx_str, _, task_text = rest.partition(".")
                    task_text = task_text.split("|")[0].strip()  # drop | status | result
                    try:
                        idx = int(idx_str.strip())
                        tasks.append(
                            TodoTask(idx=idx, tasks=task_text, status=status, result=result)
                        )
                    except ValueError:
                        pass
        return sorted(tasks, key=lambda t: t.idx)

    def _save_tasks(self, tasks: List[TodoTask]) -> None:
        """Save tasks to markdown file."""
        lines = ["# Todo List", ""]
        for t in sorted(tasks, key=lambda x: x.idx):
            if t.status == TaskStatus.COMPLETED:
                checkbox = "[x]"
            elif t.status == TaskStatus.CANCELLED:
                checkbox = "[-]"
            elif t.status == TaskStatus.RUNNING:
                checkbox = "[>]"
            else:
                checkbox = "[ ]"
            if t.result:
                lines.append(f"- {checkbox} {t.idx}. {t.tasks} | {t.status.value} | {t.result}")
            else:
                lines.append(f"- {checkbox} {t.idx}. {t.tasks} | {t.status.value}")
        self.todo_dir.mkdir(parents=True, exist_ok=True)
        with open(self._todo_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _append_todo_list(self, message: str) -> str:
        """Append current todo list to a status message."""
        current = self.todo_list()
        return f"{message}\n\nCurrent todo list:\n{current}"

    @staticmethod
    def _compute_counts(tasks: List[TodoTask]) -> tuple[int, int]:
        """Return (remaining_count, total_count). cancelled tasks are excluded
        from both — they're voided and not considered plan members."""
        waiting_running = sum(
            1 for t in tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)
        )
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        return waiting_running, waiting_running + completed

    def _publish(
        self, kind: TodoOpKind, success: bool, message: str,
        tasks_after: Optional[List[TodoTask]],
    ) -> None:
        """Compute counts from ``tasks_after`` and publish a TodoOpResult."""
        if tasks_after is None:
            remaining, total = 0, 0
        else:
            remaining, total = self._compute_counts(tasks_after)
        _publish_op_result(self.session_id, TodoOpResult(
            kind=kind,
            success=success,
            message=message,
            remaining_count=remaining,
            total_count=total,
            all_completed=(success and total > 0 and remaining == 0),
        ))

    def todo_create(self, tasks: List[str]) -> str:
        """Create a list of todo tasks. Fails if a todo list already exists.

        Args:
            tasks: List of task descriptions to create.

        Returns:
            Status message (success or error) and current todo list.
        """
        with self._get_session_lock(self.session_id):
            if self._todo_path.exists():
                msg = (
                    f"Error: A todo list for session {self.session_id} already exists. "
                    f"Use {self.__class__.TOOL_PREFIX}_insert to add more tasks."
                )
                self._publish(TodoOpKind.CREATE, False, msg, self._load_tasks())
                return self._append_todo_list(msg)
            todo_tasks = [
                TodoTask(idx=i + 1, tasks=t, status=TaskStatus.WAITING, result="")
                for i, t in enumerate(tasks)
            ]
            self._save_tasks(todo_tasks)
            msg = f"Created {len(todo_tasks)} todo tasks."
            self._publish(TodoOpKind.CREATE, True, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_start(self, idx: int) -> str:
        """Mark a task as running (in progress).

        Args:
            idx: 1-based index of the task.

        Returns:
            Status message and current todo list.
        """
        with self._get_session_lock(self.session_id):
            todo_tasks = self._load_tasks()
            for t in todo_tasks:
                if t.idx == idx:
                    if t.status == TaskStatus.COMPLETED:
                        msg = f"Error: Task {idx} is already completed."
                        self._publish(TodoOpKind.START, False, msg, todo_tasks)
                        return self._append_todo_list(msg)
                    if t.status == TaskStatus.CANCELLED:
                        msg = f"Error: Task {idx} is cancelled."
                        self._publish(TodoOpKind.START, False, msg, todo_tasks)
                        return self._append_todo_list(msg)
                    t.status = TaskStatus.RUNNING
                    self._save_tasks(todo_tasks)
                    msg = f"Task {idx} marked as running."
                    self._publish(TodoOpKind.START, True, msg, todo_tasks)
                    return self._append_todo_list(msg)
            msg = f"Error: Task {idx} not found."
            self._publish(TodoOpKind.START, False, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_complete(self, idx: int, result: str = "") -> str:
        """Mark a task as completed and save a brief result.

        Args:
            idx: 1-based index of the task.
            result: Brief result or outcome of the task.

        Returns:
            Status message and current todo list.
        """
        with self._get_session_lock(self.session_id):
            todo_tasks = self._load_tasks()
            for t in todo_tasks:
                if t.idx == idx:
                    t.status = TaskStatus.COMPLETED
                    t.result = result or "done"
                    self._save_tasks(todo_tasks)
                    msg = f"Task {idx} marked as completed."
                    self._publish(TodoOpKind.COMPLETE, True, msg, todo_tasks)
                    return self._append_todo_list(msg)
            msg = f"Error: Task {idx} not found."
            self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_complete_batch(
        self, indices: List[int], results: Optional[List[str]] = None,
    ) -> str:
        """Mark several tasks as completed in a single call.

        Indices must be strictly ascending and start at the first task that is
        not yet completed/cancelled, so the agent cannot retroactively close a
        gap or close steps out of order. ``results`` is optional; when provided
        it must align 1:1 with ``indices``. The whole batch is atomic — on any
        validation error nothing is written and a single failure event is
        published.
        """
        with self._get_session_lock(self.session_id):
            todo_tasks = self._load_tasks()

            if not indices:
                msg = "Error: indices must be a non-empty list."
                self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                return self._append_todo_list(msg)

            if results is None:
                results = ["done"] * len(indices)
            elif len(results) != len(indices):
                msg = (
                    f"Error: results length ({len(results)}) does not match "
                    f"indices length ({len(indices)})."
                )
                self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                return self._append_todo_list(msg)

            for i in range(1, len(indices)):
                if indices[i] != indices[i - 1] + 1:
                    msg = (
                        f"Error: indices must be strictly ascending and "
                        f"contiguous. Got {indices}."
                    )
                    self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                    return self._append_todo_list(msg)

            first_open_idx: Optional[int] = None
            for t in todo_tasks:
                if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                    first_open_idx = t.idx
                    break
            if first_open_idx is None:
                msg = "Error: no open tasks left to complete."
                self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                return self._append_todo_list(msg)
            if indices[0] != first_open_idx:
                msg = (
                    f"Error: batch must start at idx {first_open_idx} (the "
                    f"first open task); got start idx {indices[0]}."
                )
                self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                return self._append_todo_list(msg)

            tasks_by_idx = {t.idx: t for t in todo_tasks}
            for idx in indices:
                t = tasks_by_idx.get(idx)
                if t is None:
                    msg = f"Error: Task {idx} not found."
                    self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                    return self._append_todo_list(msg)
                if t.status == TaskStatus.COMPLETED:
                    msg = f"Error: Task {idx} is already completed."
                    self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                    return self._append_todo_list(msg)
                if t.status == TaskStatus.CANCELLED:
                    msg = f"Error: Task {idx} is cancelled."
                    self._publish(TodoOpKind.COMPLETE, False, msg, todo_tasks)
                    return self._append_todo_list(msg)

            for idx, result in zip(indices, results):
                t = tasks_by_idx[idx]
                t.status = TaskStatus.COMPLETED
                t.result = (result or "").strip() or "done"

            self._save_tasks(todo_tasks)
            msg = f"Tasks {list(indices)} marked as completed."
            self._publish(TodoOpKind.COMPLETE, True, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_insert(self, idx: int, tasks: List[str]) -> str:
        """Insert new tasks at the given index. Existing tasks are shifted.

        Args:
            idx: 1-based index where to insert (tasks will start at this index).
            tasks: New task descriptions to insert.

        Returns:
            Status message and current todo list.
        """
        with self._get_session_lock(self.session_id):
            todo_tasks = self._load_tasks()
            if not self._todo_path.exists():
                # 锁内直接创建，避免释放锁后被其他线程抢先
                new_tasks = [
                    TodoTask(idx=i + 1, tasks=t, status=TaskStatus.WAITING, result="")
                    for i, t in enumerate(tasks)
                ]
                self._save_tasks(new_tasks)
                msg = f"Created {len(new_tasks)} todo tasks."
                # Insert-into-empty is semantically a create — publish CREATE so
                # the rail's WAITING_PLAN→IN_PROGRESS transition fires.
                self._publish(TodoOpKind.CREATE, True, msg, new_tasks)
                return self._append_todo_list(msg)
            new_tasks = [
                TodoTask(idx=i + idx, tasks=t, status=TaskStatus.WAITING, result="")
                for i, t in enumerate(tasks)
            ]
            # Shift existing tasks at or after idx
            for t in todo_tasks:
                if t.idx >= idx:
                    t.idx += len(tasks)
            todo_tasks.extend(new_tasks)
            todo_tasks.sort(key=lambda x: x.idx)
            self._save_tasks(todo_tasks)
            msg = f"Inserted {len(tasks)} task(s) at index {idx}."
            self._publish(TodoOpKind.INSERT, True, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_remove(self, idx: int) -> str:
        """Remove a task and renumber remaining tasks.

        Args:
            idx: 1-based index of the task to remove.

        Returns:
            Status message and current todo list.
        """
        with self._get_session_lock(self.session_id):
            todo_tasks = self._load_tasks()
            found = [t for t in todo_tasks if t.idx == idx]
            if not found:
                msg = f"Error: Task {idx} not found."
                self._publish(TodoOpKind.REMOVE, False, msg, todo_tasks)
                return self._append_todo_list(msg)
            todo_tasks = [t for t in todo_tasks if t.idx != idx]
            # Renumber
            for i, t in enumerate(todo_tasks, 1):
                t.idx = i
            self._save_tasks(todo_tasks)
            msg = f"Removed task {idx}."
            self._publish(TodoOpKind.REMOVE, True, msg, todo_tasks)
            return self._append_todo_list(msg)

    def todo_list(self) -> str:
        """List all current todo tasks.

        Returns:
            Formatted string of tasks.
        """
        todo_tasks = self._load_tasks()
        if not todo_tasks:
            return "No todo tasks."
        lines = []
        for t in todo_tasks:
            if t.status == TaskStatus.COMPLETED:
                status_icon = "[x]"
            elif t.status == TaskStatus.RUNNING:
                status_icon = "[>]"
            elif t.status == TaskStatus.CANCELLED:
                status_icon = "[-]"
            else:
                status_icon = "[ ]"
            suffix = f" | {t.result}" if t.result else ""
            lines.append(f"- {status_icon} {t.idx}. {t.tasks}{suffix}")
        return "\n".join(lines)

    def get_tools(self) -> List[Tool]:
        """Return all todo tools for registration in the openJiuwen Runner.

        Usage:
            toolkit = TodoToolkit(session_id="abc123")
            tools = toolkit.get_tools()
            Runner.resource_mgr.add_tool(tools)
            for t in tools:
                agent.ability_manager.add(t.card)

        Returns:
            List of Tool instances (LocalFunction) ready for Runner/agent registration.
        """
        prefix = self.__class__.TOOL_PREFIX

        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        def todo_create_wrapper(tasks: List[str]) -> str:
            return self.todo_create(tasks)

        def todo_start_wrapper(idx: int) -> str:
            return self.todo_start(idx)

        def todo_complete_wrapper(idx: int, result: str = "") -> str:
            return self.todo_complete(idx, result)

        def todo_complete_batch_wrapper(
            indices: List[int], results: Optional[List[str]] = None,
        ) -> str:
            return self.todo_complete_batch(indices, results)

        def todo_insert_wrapper(idx: int, tasks: List[str]) -> str:
            return self.todo_insert(idx, tasks)

        def todo_remove_wrapper(idx: int) -> str:
            return self.todo_remove(idx)

        def todo_list_wrapper() -> str:
            return self.todo_list()

        tools: List[Tool] = [
            make_tool(
                name=f"{prefix}_create",
                description=(
                    "Create a list of todo tasks. Cannot be called when a todo list already exists. "
                    "Use this to plan and track work. Pass a list of task descriptions."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of task descriptions to create",
                        }
                    },
                    "required": ["tasks"],
                },
                func=todo_create_wrapper,
            ),
        ]

        if self.__class__.EXPOSE_START:
            tools.append(make_tool(
                name=f"{prefix}_start",
                description="Mark a task as running (in progress). Call this before starting to work on a task.",
                input_params={
                    "type": "object",
                    "properties": {
                        "idx": {
                            "type": "integer",
                            "description": "1-based index of the task to start",
                        },
                    },
                    "required": ["idx"],
                },
                func=todo_start_wrapper,
            ))

        complete_desc = "Mark a task as completed and save a brief result."
        if self.__class__.EXPOSE_COMPLETE_BATCH:
            complete_desc = (
                "Mark a single task as completed and save a brief result. "
                f"When several already-finished steps can be closed together, "
                f"prefer ``{prefix}_complete_batch`` to avoid extra tool round-trips."
            )
        tools.append(make_tool(
            name=f"{prefix}_complete",
            description=complete_desc,
            input_params={
                "type": "object",
                "properties": {
                    "idx": {
                        "type": "integer",
                        "description": "1-based index of the task to complete",
                    },
                    "result": {
                        "type": "string",
                        "description": "Brief result or outcome",
                        "default": "",
                    },
                },
                "required": ["idx"],
            },
            func=todo_complete_wrapper,
        ))

        if self.__class__.EXPOSE_COMPLETE_BATCH:
            tools.append(make_tool(
                name=f"{prefix}_complete_batch",
                description=(
                    "Mark several already-finished tasks as completed in one "
                    "call. Indices must be strictly ascending, contiguous, and "
                    "start at the first open task — gaps, reordering, and "
                    "closing already-completed tasks are rejected. Use this "
                    "only for steps that are truly done; never pre-close steps."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "1-based indices of tasks to complete; "
                                "strictly ascending and contiguous, must "
                                "start at the first open task."
                            ),
                        },
                        "results": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Per-task brief outcomes, aligned 1:1 with "
                                "indices. Optional; defaults to 'done' for "
                                "each entry."
                            ),
                        },
                    },
                    "required": ["indices"],
                },
                func=todo_complete_batch_wrapper,
            ))

        tools.extend([
            make_tool(
                name=f"{prefix}_insert",
                description="Insert new tasks at the given index. Existing tasks are shifted.",
                input_params={
                    "type": "object",
                    "properties": {
                        "idx": {
                            "type": "integer",
                            "description": "1-based index where to insert",
                        },
                        "tasks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "New task descriptions to insert",
                        },
                    },
                    "required": ["idx", "tasks"],
                },
                func=todo_insert_wrapper,
            ),
            make_tool(
                name=f"{prefix}_remove",
                description="Remove a task by index. Remaining tasks are renumbered.",
                input_params={
                    "type": "object",
                    "properties": {
                        "idx": {
                            "type": "integer",
                            "description": "1-based index of the task to remove",
                        },
                    },
                    "required": ["idx"],
                },
                func=todo_remove_wrapper,
            ),
            make_tool(
                name=f"{prefix}_list",
                description="List all current todo tasks with their status.",
                input_params={"type": "object", "properties": {}},
                func=todo_list_wrapper,
            ),
        ])
        return tools
