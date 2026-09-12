# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodo extension: unified todo tool (3-in-1) as a loadable extension.

When FLASH_TODO_ENABLED=1 (or config react.todo.unified=true), patches the
adapter to use FlashTodoRail instead of ConcurrentSafeTaskPlanningRail, and
runtime-patches the todo tool-name sets in task_execution_rail and
stream_event_rail so the frontend refresh / PPT event chain keep working
with the unified ``todo`` tool name.

Zero modification to any stock jiuwenswarm source file.
"""

from jiuwenswarm.extensions.flash_todo.extension import register_extensions

__all__ = ["register_extensions"]
