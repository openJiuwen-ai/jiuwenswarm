# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode todo tools with CC-aligned descriptions and schemas."""

from __future__ import annotations

import uuid
from typing import Optional

from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.tools.todo import (
    TodoCreateTool,
    TodoGetTool,
    TodoListTool,
    TodoLockManager,
    TodoModifyTool,
    TodoTool,
)

from jiuwenswarm.agents.harness.code.prompt.code_todo_tool_prompts import (
    CODE_TODO_TOOL_PROMPTS,
)


def _build_code_todo_card(
    name: str,
    tool_id_prefix: str,
    *,
    agent_id: Optional[str] = None,
) -> ToolCard:
    description, input_params = CODE_TODO_TOOL_PROMPTS[name]
    tool_id = f"{tool_id_prefix}_{agent_id}" if agent_id else f"{tool_id_prefix}_{uuid.uuid4().hex}"
    return ToolCard(
        id=tool_id,
        name=name,
        description=description,
        input_params=input_params or {"type": "object", "properties": {}, "required": []},
    )


class CodeTodoCreateTool(TodoCreateTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        TodoTool.__init__(
            self,
            _build_code_todo_card("todo_create", "TodoCreateTool", agent_id=agent_id),
            operation,
            workspace,
            lock_manager,
        )


class CodeTodoListTool(TodoListTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        TodoTool.__init__(
            self,
            _build_code_todo_card("todo_list", "TodoListTool", agent_id=agent_id),
            operation,
            workspace,
            lock_manager,
        )


class CodeTodoGetTool(TodoGetTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        TodoTool.__init__(
            self,
            _build_code_todo_card("todo_get", "TodoGetTool", agent_id=agent_id),
            operation,
            workspace,
            lock_manager,
        )


class CodeTodoModifyTool(TodoModifyTool):
    def __init__(
        self,
        operation: SysOperation,
        workspace: Optional[str] = None,
        language: str = "en",
        agent_id: Optional[str] = None,
        lock_manager: Optional[TodoLockManager] = None,
    ) -> None:
        _ = language
        TodoTool.__init__(
            self,
            _build_code_todo_card("todo_modify", "TodoModifyTool", agent_id=agent_id),
            operation,
            workspace,
            lock_manager,
        )
