# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodo extension entry — discovered by ExtensionLoader at startup.

Gate: FLASH_TODO_ENABLED=1 env or config react.todo.unified=true.
When enabled, patches the adapter to build FlashTodoRail instead of
ConcurrentSafeTaskPlanningRail, and runtime-patches the todo tool-name
sets (task_execution_rail.TODO_TOOLS / stream_event_rail._TODO_TOOL_NAMES)
so the frontend refresh / PPT event chain recognize the unified "todo" name.

Zero modification to any stock jiuwenswarm source file — all wiring is
monkey-patched here at startup, before any adapter is created.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCH_APPLIED = False


def _is_enabled() -> bool:
    """FLASH_TODO_ENABLED env > react.todo.unified config."""
    raw = os.getenv("FLASH_TODO_ENABLED", "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    try:
        from jiuwenswarm.common.config import get_config

        todo_cfg = (get_config().get("react", {}) or {}).get("todo", {}) or {}
        return bool(todo_cfg.get("unified", False))
    except Exception:
        return False


def _patch_todo_tool_name_sets() -> None:
    """Runtime-patch the todo tool-name frozensets so "todo" is recognized.

    - task_execution_rail.TaskExecutionRail.TODO_TOOLS (class attribute)
      → controls PPT skill_turbo event chain (task.start/complete)
    - stream_event_rail._TODO_TOOL_NAMES (module-level variable)
      → controls frontend todo snapshot refresh
    """
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )
    from jiuwenswarm.agents.harness.common.rails import stream_event_rail as _ser

    if "todo" not in TaskExecutionRail.TODO_TOOLS:
        TaskExecutionRail.TODO_TOOLS = TaskExecutionRail.TODO_TOOLS | {"todo"}
        logger.info("[FlashTodo] patched TaskExecutionRail.TODO_TOOLS += 'todo'")

    if "todo" not in _ser._TODO_TOOL_NAMES:
        _ser._TODO_TOOL_NAMES = _ser._TODO_TOOL_NAMES | {"todo"}
        logger.info("[FlashTodo] patched stream_event_rail._TODO_TOOL_NAMES += 'todo'")


def _patch_adapter_rail_builder() -> None:
    """Patch interface_deep._build_task_planning_rail to build FlashTodoRail.

    The stock method builds ConcurrentSafeTaskPlanningRail. We wrap it: when
    enabled, return FlashTodoRail instead; when disabled, call the original.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    original = _iface.JiuWenSwarmDeepAdapter._build_task_planning_rail

    def _patched_build(self, config=None):
        try:
            # Primary gate: FLASH_TODO_ENABLED env (single-switch control —
            # the extension is only patched when this env is set, so it
            # unconditionally selects the unified path here).
            raw = os.getenv("FLASH_TODO_ENABLED", "").strip().lower()
            if raw in ("1", "true", "yes", "on"):
                from jiuwenswarm.extensions.flash_todo.FlashTodoRail import (
                    FlashTodoRail,
                )

                rail = FlashTodoRail()
                logger.info(
                    "[FlashTodo] FlashTodoRail create success (env gate)"
                )
                return rail

            # Secondary gate: react.todo.unified config
            react_cfg = config if config is not None else self._config_cache
            todo_cfg = (react_cfg or {}).get("todo", {}) or {}
            if isinstance(todo_cfg, dict) and todo_cfg.get("unified", False):
                from jiuwenswarm.extensions.flash_todo.FlashTodoRail import (
                    FlashTodoRail,
                )

                rail = FlashTodoRail()
                logger.info(
                    "[FlashTodo] FlashTodoRail create success (config gate)"
                )
                return rail
        except Exception as exc:
            logger.warning("[FlashTodo] unified path failed, falling back: %s", exc)
        return original(self, config)

    _iface.JiuWenSwarmDeepAdapter._build_task_planning_rail = _patched_build
    logger.info("[FlashTodo] patched _build_task_planning_rail (unified gate)")


def _patch_progressive_eager_tools() -> None:
    """Patch _normalize_progressive_eager_tools to swap legacy todo names.

    When enabled, replaces todo_create/todo_list/todo_modify entries with a
    single "todo" in the eager tools list (ProgressiveToolRail schema).
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    original = _iface._normalize_progressive_eager_tools

    def _patched_normalize(value, default=None):
        result = original(value, default)
        if "todo" in result:
            return result  # already unified (idempotent)
        legacy = {"todo_create", "todo_list", "todo_modify", "todo_get"}
        if legacy & set(result):
            result = [name for name in result if name not in legacy]
            result.append("todo")
        return result

    _iface._normalize_progressive_eager_tools = _patched_normalize
    logger.info("[FlashTodo] patched _normalize_progressive_eager_tools (swap todo)")


async def register_extensions(registry):
    """ExtensionLoader entry point."""
    global _PATCH_APPLIED

    if not _is_enabled():
        logger.info("[FlashTodo] disabled (FLASH_TODO_ENABLED not set / unified not true)")
        return []

    if _PATCH_APPLIED:
        return []

    try:
        _patch_todo_tool_name_sets()
        _patch_adapter_rail_builder()
        _patch_progressive_eager_tools()
        _PATCH_APPLIED = True
        logger.info("[FlashTodo] switch ARMED: unified todo tool (3-in-1)")
    except Exception as exc:
        logger.warning("[FlashTodo] setup failed: %s", exc)

    return []
