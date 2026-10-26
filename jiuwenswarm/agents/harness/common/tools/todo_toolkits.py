# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Todo toolkit for agent task tracking.

Provides todo_create, todo_complete, todo_insert, todo_remove tools that persist
tasks to a markdown file under the predefined session directory. Tools can be
registered in the openJiuwen Runner via TodoToolkit.get_tools().
"""

from __future__ import annotations

import os
import threading
from enum import Enum
from pathlib import Path
from typing import ClassVar, Dict, List

from pydantic import BaseModel

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.common.utils import get_agent_sessions_dir


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


class TodoToolkit:
    """Toolkit for agent todo task tracking. Persists tasks to markdown under session dir."""

    TODO_FILENAME = "todo.md"

    # Markdown line format: - [ ] 1. task | status  or  - [x] 1. task | status | result
    # parts[0]=task, parts[1]=status, parts[2]=result (optional)
    PARTS_MIN_COUNT_WITH_RESULT = 3  # 至少 3 段才有 result
    PARTS_INDEX_RESULT = 2  # result 在 split("|") 后的索引

    # 按 session_id 分组的文件锁，防止并发任务对同一 todo.md 进行 read-modify-write 时丢失更新
    _session_locks: ClassVar[Dict[str, threading.Lock]] = {}
    _meta_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _get_session_lock(cls, session_id: str) -> threading.Lock:
        """获取指定 session 的文件操作锁（线程安全）."""
        with cls._meta_lock:
            if session_id not in cls._session_locks:
                cls._session_locks[session_id] = threading.Lock()
            return cls._session_locks[session_id]

    def __init__(self, session_id: str, todo_dir: Path | None = None):
        """Initialize TodoToolkit for a session.

        Args:
            session_id: Session/conversation identifier for scoping todo files.
            todo_dir: Optional custom directory. Defaults to agent/sessions/{session_id}/.
        """
        self.session_id = session_id
        self.todo_dir = todo_dir or (get_agent_sessions_dir() / session_id)
        self.todo_dir.mkdir(parents=True, exist_ok=True)
        self._todo_path = self.todo_dir / self.TODO_FILENAME

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
                else:
                    checked = "[x]" in line.lower() or "[√]" in line
                    status = TaskStatus.COMPLETED if checked else TaskStatus.WAITING
                result = ""
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= self.PARTS_MIN_COUNT_WITH_RESULT:
                    result = parts[self.PARTS_INDEX_RESULT]
                # 解析行："- [ ] 1. xxx" / "- [x] 1. xxx"
                rest = line.replace("- [x]", "").replace("- [-]", "").replace("- [ ]", "").strip()
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

    def todo_create(self, tasks: List[str]) -> str:
        """Create a list of todo tasks. Fails if a todo list already exists.

        Args:
            tasks: List of task descriptions to create.

        Returns:
            Status message (success or error) and current todo list.
        """
        with self._get_session_lock(self.session_id):
            if self._todo_path.exists():
                return self._append_todo_list(
                    f"Error: A todo list for session {self.session_id} already exists."
                    " Use todo_insert to add more tasks."
                )
            todo_tasks = [
                TodoTask(idx=i + 1, tasks=t, status=TaskStatus.WAITING, result="")
                for i, t in enumerate(tasks)
            ]
            self._save_tasks(todo_tasks)
            return self._append_todo_list(f"Created {len(todo_tasks)} todo tasks.")

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
                    return self._append_todo_list(f"Task {idx} marked as completed.")
            return self._append_todo_list(f"Error: Task {idx} not found.")

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
                return self._append_todo_list(f"Created {len(new_tasks)} todo tasks.")
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
            return self._append_todo_list(f"Inserted {len(tasks)} task(s) at index {idx}.")

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
                return self._append_todo_list(f"Error: Task {idx} not found.")
            todo_tasks = [t for t in todo_tasks if t.idx != idx]
            # Renumber
            for i, t in enumerate(todo_tasks, 1):
                t.idx = i
            self._save_tasks(todo_tasks)
            return self._append_todo_list(f"Removed task {idx}.")

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
            status_icon = "[x]" if t.status == TaskStatus.COMPLETED else "[ ]"
            suffix = f" | {t.result}" if t.result else ""
            lines.append(f"{status_icon} {t.idx}. {t.tasks}{suffix}")
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
        session_id = self.session_id

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

        def todo_complete_wrapper(idx: int, result: str = "") -> str:
            return self.todo_complete(idx, result)

        def todo_insert_wrapper(idx: int, tasks: List[str]) -> str:
            return self.todo_insert(idx, tasks)

        def todo_remove_wrapper(idx: int) -> str:
            return self.todo_remove(idx)

        def todo_list_wrapper() -> str:
            return self.todo_list()

        return [
            make_tool(
                name="todo_create",
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
            make_tool(
                name="todo_complete",
                description="Mark a task as completed and save a brief result.",
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
            ),
            make_tool(
                name="todo_insert",
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
                name="todo_remove",
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
                name="todo_list",
                description="List all current todo tasks with their status.",
                input_params={"type": "object", "properties": {}},
                func=todo_list_wrapper,
            ),
        ]
