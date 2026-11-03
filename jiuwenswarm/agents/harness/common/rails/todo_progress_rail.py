# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Todo 进度度量 rail（只观测，不改变业务行为）。

每次主 agent invoke 统计 todo 写入次数、写入前后列表是否变化、结束时各状态
条目数，在 after_invoke 输出一条 ``[TodoProgress] run_end`` 结构化日志，用于
计算"正常结束仍残留未完成项"的比例。

日志只记计数和结构信息，不记 todo 的 content/description/activeForm、
用户正文或提示词。rail 自身出错时跳过当前步骤并记 ``[TodoProgress] rail_error``，
不影响用户任务。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.task import TodoItem
from openjiuwen.harness.tools import TodoListTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.common.rails.todo_progress_snapshot import (
    TodoSnapshotError,
    error_fields,
    read_todo_snapshot,
    snapshot_key,
    status_counts,
)
from jiuwenswarm.common.utils import logger

_LOG_PREFIX = "[TodoProgress]"
_SID_KEY = "__todo_progress_session_id__"
_SESSION_PREFIX_LEN = 20
_WRITE_TOOLS = frozenset({"todo_create", "todo_modify"})


def _session_prefix(session_id: str) -> str:
    return session_id[:_SESSION_PREFIX_LEN]


def _session_id(session: Any) -> Optional[str]:
    get_id = getattr(session, "get_session_id", None)
    sid = get_id() if callable(get_id) else None
    return sid if isinstance(sid, str) and sid else None


def _usage_field(usage: Any, name: str) -> Any:
    return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)


def _log(event: str, fields: dict[str, Any], *, error: bool = False) -> None:
    line = f"{_LOG_PREFIX} {event} {json.dumps(fields, ensure_ascii=False, sort_keys=True)}"
    if error:
        logger.error(line)
    else:
        logger.info(line)


@dataclass
class _RunState:
    session_id: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # 同一行可能被写进多个日志文件，统计时按它去重
    model: str = ""
    iteration: int = 0                # 本次 invoke 已完成的工具迭代数
    last_change_iter: int = 0         # 最近一次列表实际变化所在迭代
    changed_this_iter: bool = False
    first_call_pending: bool = True   # 本次 invoke 尚未做首次读取
    pre_write: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # tool_call_id -> 写入前快照
    writes_ok: int = 0                # 工具执行结果：成功
    writes_failed: int = 0            # 工具执行结果：失败
    changes: int = 0                  # 前后比较：有变化
    noops: int = 0                    # 前后比较：无变化
    change_unknown: int = 0           # 前后比较：任一侧快照不可用
    had_todos: bool = False
    rail_errors: int = 0


class TodoProgressRail(DeepAgentRail):
    """主 agent 的 todo 进度度量。只装在主 agent 上，不进子 agent spec。"""

    priority = 70

    def __init__(self, *, mode: str = "agent") -> None:
        super().__init__()
        self._mode = mode
        self._tool: Optional[TodoListTool] = None
        self._states: dict[str, _RunState] = {}

    def init(self, agent: Any) -> None:
        # 与 stream_event_rail._get_todo_tool 同源：主 agent 的 todo 目录取自
        # deep_config.workspace，不取全局资源表（子 agent 会覆盖）。
        try:
            deep_config = agent.deep_config
            language = getattr(deep_config, "language", None) or "cn"
            self._tool = TodoListTool(
                operation=deep_config.sys_operation,
                workspace=str(deep_config.workspace.get_node_path(WorkspaceNode.TODO)),
                language=language,
                agent_id=agent.card.id,
            )
        except Exception as exc:
            self._tool = None
            _log("rail_error", {"hook": "init", **error_fields(exc)}, error=True)

    # -- 状态与错误 ------------------------------------------------------

    def _state(self, ctx: AgentCallbackContext) -> Optional[_RunState]:
        # DeepAgent 的外层 invoke 与内层 ReAct 循环各建一个 AgentCallbackContext，
        # 模型/工具/迭代钩子拿到的是内层上下文，extra 是新的；此时按会话 id 找状态。
        # todo 工具也按同一个 session.get_session_id() 定位文件，两者一致。
        sid = ctx.extra.get(_SID_KEY)
        if not isinstance(sid, str) or not sid:
            sid = _session_id(ctx.session)
        return self._states.get(sid) if sid else None

    @staticmethod
    def _rail_error(state: Optional[_RunState], hook: str, exc: Exception) -> None:
        fields: dict[str, Any] = {"hook": hook, **error_fields(exc)}
        if state is not None:
            state.rail_errors += 1
            fields["session"] = _session_prefix(state.session_id)
        _log("rail_error", fields, error=True)

    async def _read(self, state: _RunState) -> list[TodoItem]:
        todos = await read_todo_snapshot(self._tool, state.session_id)
        state.had_todos |= bool(todos)
        return todos

    # -- 钩子 --------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if self._tool is None or not isinstance(ctx.inputs, InvokeInputs) or ctx.session is None:
            return
        try:
            sid = _session_id(ctx.session)
            if not sid:
                return
            self._states[sid] = _RunState(session_id=sid)
            ctx.extra[_SID_KEY] = sid
        except Exception as exc:
            self._rail_error(None, "before_invoke", exc)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        if state is None or not state.first_call_pending:
            return
        state.first_call_pending = False
        # 首次调用无论是否续跑都读一次：上一轮留下待办、本轮一直没写的执行
        # 也要把 had_todos 记为真，否则会从残留率统计里漏掉。
        try:
            await self._read(state)
        except Exception as exc:
            self._rail_error(state, "before_model_call", exc)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        if state is None:
            return
        try:
            response = getattr(ctx.inputs, "response", None)
            usage = getattr(response, "usage_metadata", None)
            model = _usage_field(usage, "model_name")
            if model:
                state.model = str(model)
        except Exception as exc:
            self._rail_error(state, "after_model_call", exc)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        inputs = ctx.inputs
        if state is None or not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in _WRITE_TOOLS:
            return
        try:
            call_id = getattr(inputs.tool_call, "id", None) or ""
            state.pre_write[call_id] = snapshot_key(await self._read(state))
        except Exception as exc:
            self._rail_error(state, "before_tool_call", exc)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        inputs = ctx.inputs
        if state is None or not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in _WRITE_TOOLS:
            return
        call_id = getattr(inputs.tool_call, "id", None) or ""
        try:
            if not inputs.execution_started:
                return  # 被前置钩子拦下（如权限拒绝），工具没有执行，不算写入
            result = inputs.tool_result
            if isinstance(result, dict) and isinstance(result.get("message"), str):
                state.writes_ok += 1
            else:
                state.writes_failed += 1
            try:
                after = snapshot_key(await self._read(state))
            except Exception as exc:
                self._rail_error(state, "after_tool_call", exc)
                state.change_unknown += 1
                return
            before = state.pre_write.get(call_id)
            if before is None:
                state.change_unknown += 1  # 写入前快照不可用，不能拿空列表代替
            elif before != after:
                state.changes += 1
                state.changed_this_iter = True
            else:
                state.noops += 1
        except Exception as exc:
            self._rail_error(state, "after_tool_call", exc)
        finally:
            state.pre_write.pop(call_id, None)

    async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        if state is None:
            return
        state.iteration += 1
        if state.changed_this_iter:
            state.last_change_iter = state.iteration
            state.changed_this_iter = False

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if self._tool is None or not isinstance(ctx.inputs, InvokeInputs) or ctx.session is None:
            return
        sid = ctx.extra.get(_SID_KEY)
        state = self._states.get(sid) if isinstance(sid, str) else None
        if state is None:
            # before_invoke 没建状态（如当时会话 id 取不到），本次不报 run_end
            self._rail_error(None, "after_invoke", TodoSnapshotError("missing_state"))
            return
        try:
            try:
                final: Optional[dict[str, int]] = status_counts(await self._read(state))
            except Exception as exc:
                self._rail_error(state, "after_invoke", exc)
                final = None
            self._log_run_end(ctx, state, final)
        finally:
            self._states.pop(state.session_id, None)

    def _log_run_end(
        self,
        ctx: AgentCallbackContext,
        state: _RunState,
        final: Optional[dict[str, int]],
    ) -> None:
        result = getattr(ctx.inputs, "result", None)
        result_type = result.get("result_type") if isinstance(result, dict) else None
        _log("run_end", {
            "run_id": state.run_id,
            "mode": self._mode,
            "model": state.model or "unknown",
            "session": _session_prefix(state.session_id),
            "result_type": str(result_type) if result_type else "unknown",
            "had_todos": state.had_todos,
            "final": final if final is not None else "unknown",
            "writes_ok": state.writes_ok,
            "writes_failed": state.writes_failed,
            "changes": state.changes,
            "noops": state.noops,
            "change_unknown": state.change_unknown,
            "iteration": state.iteration,
            "iters_since_change": state.iteration - state.last_change_iter,
            "rail_errors": state.rail_errors,
        })


__all__ = ["TodoProgressRail"]
