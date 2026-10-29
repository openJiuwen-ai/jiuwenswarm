# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodoRail —— 统一 todo 工具的挂载 rail（原与 UnifiedTodoTool 同文件，拆分迁入）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.prompts.sections.todo import build_todo_section
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.tools.todo_store import (
    bind_todo_batch,
    prepare_todo_batch,
    reset_todo_binding,
    snapshot_from_result,
)

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    _todo_close_nudge_message,
    is_todo_close_nudge,
)
from jiuwenswarm.agents.harness.common.rails.todo_close_gate import TodoCloseGate
from jiuwenswarm.agents.harness.flash.tools.flash_todo import (
    _TODO_NAME_REPLACEMENTS,
    _UNIFIED_TODO_NAME,
    UnifiedTodoTool,
)
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.agent_adapter.refusal_text_scrub import (
    RefusalScrubGate,
    is_canned_refusal_text,
)

logger = logging.getLogger(__name__)

# 网关把 deepseek 的完成替换成这句时，正文往往只有 chat.final。
# 非 team 前端不渲染 chat.final，回答气泡是空的。命中后改写成可见正文。
# 文案之外还要求：无 reasoning、无缓存命中——真回答很少三项同时成立。
# 没有进行中的待办时，原文不会进 todo hold，要靠 RefusalScrubGate 先挂住再丢。
MODEL_REFUSAL_BODY = "模型拒绝回答这个问题，任务已停止。请更换模型，或调整问题后再试。"


def _usage_value(usage: Any, name: str, default: Any = None) -> Any:
    if usage is None:
        return default
    if isinstance(usage, dict):
        return usage.get(name, default)
    return getattr(usage, name, default)


def _has_no_reasoning(response: Any) -> bool:
    if str(getattr(response, "reasoning_content", None) or "").strip():
        return False
    tokens = _usage_value(getattr(response, "usage_metadata", None), "reasoning_tokens", 0)
    try:
        return int(tokens or 0) == 0
    except (TypeError, ValueError):
        return False


def _has_no_cache_hit(response: Any) -> bool:
    usage = getattr(response, "usage_metadata", None)
    cache_read = _usage_value(usage, "cache_read_tokens", None)
    if cache_read is not None:
        try:
            return int(cache_read) == 0
        except (TypeError, ValueError):
            return False
    try:
        return int(_usage_value(usage, "cache_tokens", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


def is_canned_model_refusal(response: Any) -> bool:
    """True for the short gateway stub: canned text, no reasoning, no cache hit."""
    if response is None:
        return False
    if not is_canned_refusal_text(getattr(response, "content", None)):
        return False
    if not _has_no_reasoning(response):
        return False
    return _has_no_cache_hit(response)


def bind_todo_lifecycle(tool):
    """Bind the host's session record to the SDK transaction boundary."""
    from jiuwenswarm.server.runtime.session.session_metadata import (
        check_todo_initialization,
    )

    async def guard(session_id, snapshot, *, committing=False):
        root = get_agent_sessions_dir()
        await asyncio.to_thread(check_todo_initialization, session_id, file_exists=snapshot.exists,
                                committing=committing, sessions_root=root)

    tool.snapshot_guard = guard


class FlashTodoRail(DeepAgentRail):
    """Standalone unified-todo rail (replaces ConcurrentSafeTaskPlanningRail).

    - init: build 4 Todo engines (shared TodoLockManager, never individually
      registered) and register one UnifiedTodoTool.
    - before_model_call: inject the stock todo section with tool names
      rewritten to the unified action form.
    - No outer-task-loop hooks (no after_task_iteration): TaskPlan sync /
      advance reminders are intentionally dropped.
    - Close nudge (``TodoCloseGate``): once this invoke has created or modified
      todos, a text-only ending while an item is still in_progress is swapped
      for one internal ``todo`` call asking the model to close it — otherwise
      the frontend task list stays "in progress" after delivery.

    Concurrency safety: registration goes through ``add_ability`` alone, which
    rebinds the resource-manager entry with ``refresh=True`` atomically (later
    owner wins) — no racy get-then-add against the process-global registry.
    """

    priority = 90
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._tools: list = []
        self.engines: dict = {}
        self._agent: Any = None
        self._close_gate = TodoCloseGate(_UNIFIED_TODO_NAME)
        self._refusal_scrub = RefusalScrubGate()
        self._open_todos: list[tuple[str, dict[str, Any]]] = []
        self._todo_revision = -1
        self._warned_unknown_iteration = False

    def init(self, agent) -> None:
        from openjiuwen.harness.tools import (
            TodoCreateTool,
            TodoGetTool,
            TodoListTool,
            TodoModifyTool,
        )
        from openjiuwen.harness.tools.todo import TodoLockManager
        from openjiuwen.harness.workspace.workspace import WorkspaceNode

        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            logger.warning("[FlashTodoRail] agent has no ability_manager; skip")
            return

        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

        if self.workspace is None:
            deep_cfg = getattr(agent, "deep_config", None) or getattr(
                agent, "_deep_config", None
            )
            workspace = getattr(deep_cfg, "workspace", None) if deep_cfg else None
            if workspace is not None:
                self.set_workspace(workspace)
        if self.workspace is None or self.sys_operation is None:
            logger.warning("[FlashTodoRail] init skip: no workspace/sys_operation")
            return

        workspace_dir = str(self.workspace.get_node_path(WorkspaceNode.TODO))
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        language = (
            self.system_prompt_builder.language
            if self.system_prompt_builder
            else "cn"
        )

        shared_lock = TodoLockManager()
        engines = {
            "create": TodoCreateTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
            "list": TodoListTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
            "get": TodoGetTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
            # CompatibleTodoModifyTool (installed via install_todo_modify_compat_patch
            # at import time in interface_deep) tolerates deleted/canceled status
            # variants from clients.
            "modify": TodoModifyTool(
                self.sys_operation, workspace_dir, language, agent_id, shared_lock
            ),
        }
        self.engines = engines
        for engine in engines.values():
            bind_todo_lifecycle(engine)
        unified_tool = UnifiedTodoTool(engines, language, agent_id)

        try:
            # 统一卡是 stateful（card.stateless 默认 False），add_ability 内部即
            # 以 refresh=True 在 resource_mgr 原子重绑（后注册者胜）——不在这里
            # 先 get_tool/add_tool：那对全局注册表是非原子的检查后写入，并发
            # 建同名 agent 时会竞出 "already exist"。
            ability_manager.add_ability(unified_tool.card, unified_tool)
            self._tools.append(unified_tool)
            logger.info(
                "[FlashTodoRail] unified todo tool registered "
                "(engines: create/list/get/modify, 4-in-1)"
            )
        except Exception as exc:
            logger.warning("[FlashTodoRail] register unified todo failed: %s", exc)

    def uninit(self, agent) -> None:
        try:
            if self.system_prompt_builder:
                self.system_prompt_builder.remove_section(SectionName.TODO)
            ability_manager = getattr(agent, "ability_manager", None)
            if ability_manager and self._tools:
                for tool in self._tools:
                    name = getattr(tool.card, "name", None)
                    if name:
                        try:
                            ability_manager.remove_ability(name)
                        except Exception as exc:
                            logger.debug(
                                "[FlashTodoRail] remove_ability %s failed: %s", name, exc
                            )
        except Exception as exc:
            logger.warning("[FlashTodoRail] uninit failed: %s", exc)

    async def before_invoke(self, ctx) -> None:
        reset_todo_binding()
        if "create" in self.engines and ctx.session is not None:
            self.engines["create"].reset_invoke_marker(ctx.session.get_session_id())
        self._close_gate.reset()
        self._refusal_scrub.reset()
        self._open_todos = []
        self._todo_revision = -1

    async def before_model_call(self, ctx) -> None:
        self._inject_todo_section()
        can_nudge = self._can_nudge(ctx)
        await self._close_gate.arm(ctx.session, can_nudge=can_nudge)
        # 待办 hold 会吞掉整段正文。没有它时，只挂住仍可能是拒答的短前缀。
        if not can_nudge:
            await self._refusal_scrub.arm(ctx.session)

    async def after_model_call(self, ctx) -> None:
        prepare_todo_batch(ctx)
        if self._replace_canned_refusal(ctx):
            # 先丢掉挂起的拒答原文，再写提示。顺序反了会被同一次 hold 一起丢掉。
            await self._close_gate.finish(ctx.session, dropped=True)
            await self._refusal_scrub.finish(ctx.session, dropped=True)
            # 进行中的待办会把后续正文收进任务卡。先收口，停止提示才落在回答气泡。
            await self._close_open_task_segment(ctx.session)
            await self._emit_refusal_body(ctx.session)
            return
        dropped = False
        try:
            dropped = self._close_gate.attach(
                getattr(ctx.inputs, "response", None), can_nudge=self._can_nudge(ctx)
            )
            if dropped:
                logger.info(
                    "[FlashTodoRail] todo close nudge: open=%s",
                    [todo_id for todo_id, _ in self._open_todos],
                )
        finally:
            await self._close_gate.finish(ctx.session, dropped=dropped)
            await self._refusal_scrub.finish(ctx.session, dropped=False)

    async def before_tool_call(self, ctx) -> None:
        bind_todo_batch(ctx, self.engines.get("modify"))
        if not isinstance(ctx.inputs, ToolCallInputs) or not is_todo_close_nudge(ctx.inputs.tool_call):
            return
        msg = _todo_close_nudge_message(self._open_todos)
        for old, new in _TODO_NAME_REPLACEMENTS:
            msg = msg.replace(old, new)
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = msg
        ctx.inputs.tool_msg = ToolMessage(
            content=msg, tool_call_id=str(getattr(ctx.inputs.tool_call, "id", "") or "")
        )

    async def after_tool_call(self, ctx) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs) or ctx.inputs.tool_name != _UNIFIED_TODO_NAME:
            return
        if is_todo_close_nudge(ctx.inputs.tool_call):
            return
        if _todo_action(ctx.inputs.tool_args) not in ("create", "modify"):
            return
        snapshot = snapshot_from_result(ctx.inputs.tool_result)
        if snapshot is not None:
            if snapshot.revision < self._todo_revision:
                return
            self._todo_revision = snapshot.revision
            self._open_todos = [(row["id"], {"content": row.get("content", "")})
                                for row in snapshot.todos if row.get("status") == "in_progress"]
        else:
            self._open_todos = await self._load_open_todos(ctx.session)

    def _replace_canned_refusal(self, ctx) -> bool:
        response = getattr(ctx.inputs, "response", None)
        if response is None:
            return False
        if getattr(response, "tool_calls", None):
            return False
        if not is_canned_model_refusal(response):
            return False
        response.content = MODEL_REFUSAL_BODY
        response.tool_calls = []
        ctx.request_force_finish({
            "output": MODEL_REFUSAL_BODY,
            "result_type": "answer",
        })
        logger.info("[FlashTodoRail] canned model refusal; writing the notice into the answer body")
        return True

    async def _close_open_task_segment(self, session: Any) -> None:
        if session is None or not self._open_todos:
            return
        open_todos = list(self._open_todos)
        if not await self._persist_cancel_open_todos(session, open_todos):
            return
        self._open_todos = []
        rows = await self._refusal_todo_rows(session, open_todos)
        await self._write_control(session, "todo.updated", {"todos": rows})
        for todo_id, meta in open_todos:
            await self._write_control(session, "task.complete", {
                "task_id": f"todo:{todo_id}",
                "task_content": str(meta.get("content") or ""),
                "status": "skipped",
                "duration_ms": 0,
                "source": "todo",
            })

    async def _persist_cancel_open_todos(self, session: Any, open_todos: list) -> bool:
        modify = self.engines.get("modify")
        ids = [todo_id for todo_id, _ in open_todos]
        if modify is None or not ids:
            logger.warning("[FlashTodoRail] refusal cancel skipped: no modify engine")
            return False
        try:
            result = await modify.invoke({"action": "cancel", "ids": ids}, session=session)
        except Exception as exc:
            logger.warning("[FlashTodoRail] refusal cancel failed: %s", exc)
            return False
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        if not message.startswith("Successfully cancelled"):
            logger.warning("[FlashTodoRail] refusal cancel rejected: %s", message)
            return False
        return True

    async def _refusal_todo_rows(self, session: Any, open_todos: list) -> list[dict[str, str]]:
        open_ids = {todo_id for todo_id, _ in open_todos}
        items = await self._load_todo_items(session)
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            status = getattr(item.status, "value", item.status)
            status_text = str(status or "")
            if item.id in open_ids or status_text == "in_progress":
                status_text = "cancelled"
            rows.append({
                "id": str(item.id),
                "content": str(getattr(item, "content", "") or ""),
                "status": status_text,
                "activeForm": str(getattr(item, "activeForm", "") or ""),
            })
            seen.add(str(item.id))
        for todo_id, meta in open_todos:
            if todo_id in seen:
                continue
            rows.append({
                "id": todo_id,
                "content": str(meta.get("content") or ""),
                "status": "cancelled",
                "activeForm": "",
            })
        return rows

    async def _load_todo_items(self, session: Any) -> list[Any]:
        list_engine = self.engines.get("list")
        if list_engine is None:
            return []
        try:
            return await list_engine.load_todos(session.get_session_id())
        except Exception as exc:
            logger.debug("[FlashTodoRail] load todos for refusal close failed: %s", exc)
            return []

    @staticmethod
    async def _write_control(session: Any, stream_type: str, payload: dict[str, Any]) -> None:
        try:
            await session.write_stream(OutputSchema(type=stream_type, index=0, payload=payload))
        except Exception as exc:
            logger.warning("[FlashTodoRail] %s failed: %s", stream_type, exc)

    @staticmethod
    async def _emit_refusal_body(session: Any) -> None:
        if session is None:
            return
        try:
            await session.write_stream(OutputSchema(
                type="llm_output",
                index=0,
                payload={"content": MODEL_REFUSAL_BODY, "result_type": "answer"},
            ))
        except Exception as exc:
            logger.warning("[FlashTodoRail] refusal body stream failed: %s", exc)

    def _can_nudge(self, ctx) -> bool:
        return ctx.session is not None and bool(self._open_todos) and self._has_rounds_to_close_and_reply(ctx)

    def _has_rounds_to_close_and_reply(self, ctx) -> bool:
        iteration = int(getattr(ctx.inputs, "react_iteration", 0) or 0)
        react = getattr(self._agent, "_react_agent", None)
        max_iterations = getattr(getattr(react, "_config", None), "max_iterations", None)
        if iteration <= 0 or not isinstance(max_iterations, int) or max_iterations <= 0:
            if not self._warned_unknown_iteration:
                self._warned_unknown_iteration = True
                logger.warning(
                    "[FlashTodoRail] react iteration unknown (iteration=%s max=%r); todo close nudge disabled",
                    iteration,
                    max_iterations,
                )
            return False
        return iteration <= max_iterations - 2

    async def _load_open_todos(self, session) -> list[tuple[str, dict[str, Any]]]:
        list_engine = self.engines.get("list")
        if session is None or list_engine is None:
            return []
        try:
            items = await list_engine.load_todos(session.get_session_id())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[FlashTodoRail] load todos for close nudge failed: %s", exc)
            return []
        return [
            (item.id, {"content": item.content})
            for item in items
            if item.status == TodoStatus.IN_PROGRESS
        ]

    def _inject_todo_section(self) -> None:
        """Inject the todo anchor section with unified tool names."""
        if self.system_prompt_builder is None:
            return
        section = build_todo_section(
            language=self.system_prompt_builder.language,
        )
        if section is not None:
            adapted = section
            content = getattr(section, "content", None)
            if isinstance(content, dict):
                new_content = {}
                for lang_key, text in content.items():
                    if isinstance(text, str):
                        for old, new in _TODO_NAME_REPLACEMENTS:
                            text = text.replace(old, new)
                    new_content[lang_key] = text
                try:
                    adapted = type(section)(
                        name=section.name,
                        content=new_content,
                        priority=section.priority,
                    )
                except Exception:
                    adapted = section
            self.system_prompt_builder.add_section(adapted)
        else:
            self.system_prompt_builder.remove_section(SectionName.TODO)


def _todo_action(tool_args: Any) -> str:
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (TypeError, ValueError):
            return ""
    if not isinstance(tool_args, dict):
        return ""
    return str(tool_args.get("action") or "").strip().lower()


__all__ = ["MODEL_REFUSAL_BODY", "FlashTodoRail", "is_canned_model_refusal"]
