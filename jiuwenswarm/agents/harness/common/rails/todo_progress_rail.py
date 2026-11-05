# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Todo 进度 rail：度量、todo 工具串行执行、写入回执增强与执行中提醒。

- 度量：每次主 agent invoke 统计 todo 写入次数、写入前后列表是否变化、结束时
  各状态条目数，在 after_invoke 输出一条 ``[TodoProgress] run_end`` 结构化日志，
  用于计算"正常结束仍残留未完成项"的比例；每次写入另记一条 ``[TodoProgress] write``。
- 串行：每次 invoke 开始时把主 agent 的 todo 工具设为不与其他工具并行
  （todo_tool_serial），写入前后比较也不会被穿插。不依赖快照读取，rail 初始化
  失败时照样执行。
- 回执：todo 写入成功后，在给模型的工具消息末尾追加当前列表（todo_modify）和
  一条下一步提示（todo_progress_receipt）；界面工具卡片取自 tool_result，不受影响。
- 提醒：列表连续 _REMIND_INTERVAL 次工具迭代没有实际变化、且距上次检查也满这么多
  次迭代、且还有未完成项时，在模型调用前追加一条 ``<system-reminder>``。满间隔后
  无论是否注入都重新计间隔。用 add_messages 追加到上下文末尾，不改动已有消息。
  问卷/审批暂停后的续跑沿用暂停时的计数。新一轮不额外注入上一轮的列表：之前轮次的
  todo 调用与回执仍在上下文里，列表与当前请求无关时也不会被带进来。

日志只记计数和结构信息，不记 todo 的 content/description/activeForm、
用户正文或提示词。rail 自身出错时跳过当前步骤并记 ``[TodoProgress] rail_error``，
不影响用户任务。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.task import TodoItem
from openjiuwen.harness.tools import TodoListTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import receipt_suffix
from jiuwenswarm.agents.harness.common.rails.todo_progress_reminder import build_reminder, unfinished
from jiuwenswarm.agents.harness.common.rails.todo_progress_snapshot import (
    TodoSnapshotError,
    error_fields,
    read_todo_snapshot,
    snapshot_key,
    status_counts,
)
from jiuwenswarm.agents.harness.common.rails.todo_reminder_intervals import Change, ReminderIntervals
from jiuwenswarm.agents.harness.common.rails.todo_tool_serial import TODO_WRITE_TOOLS, serialize_todo_tools
from jiuwenswarm.common.utils import logger

_LOG_PREFIX = "[TodoProgress]"
_SID_KEY = "__todo_progress_session_id__"
_SESSION_PREFIX_LEN = 20
_REMIND_INTERVAL = 10  # 提醒间隔：列表无实际变化、距上次检查，都按完整工具迭代计
# 续跑标记：问卷/审批续跑或 harness 续跑与暂停前属于同一轮，接着暂停时的提醒计数
_RESUME_MARKERS = ("_resume_continuation", "_resumed_from_hitl")
_CARRY_MAX = 64  # 暂停中待续跑的会话数上限，超出时丢弃最早的


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
    last_remind_check_iter: int = 0   # 最近一次周期检查或注入所在迭代
    changed_this_iter: bool = False
    first_call_pending: bool = True   # 本次 invoke 尚未做首次读取
    carried: Optional[tuple[int, int]] = None  # 上次暂停时距列表变化、距上次检查的迭代数
    pre_write: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # tool_call_id -> 写入前快照
    writes_ok: int = 0                # 工具执行结果：成功
    writes_failed: int = 0            # 工具执行结果：失败
    changes: int = 0                  # 前后比较：有变化
    noops: int = 0                    # 前后比较：无变化
    change_unknown: int = 0           # 前后比较：任一侧快照不可用
    receipts: int = 0                 # 增强过的回执条数
    receipt_chars: int = 0            # 回执追加文本字符数累计
    receipt_chars_max: int = 0        # 单条回执追加文本最大字符数
    reminders: int = 0                # 注入的周期提醒次数
    intervals: ReminderIntervals = field(default_factory=ReminderIntervals)
    had_todos: bool = False
    rail_errors: int = 0


class TodoProgressRail(DeepAgentRail):
    """主 agent 的 todo 进度度量。只装在主 agent 上，不进子 agent spec。"""

    priority = 70

    def __init__(self, *, mode: str = "agent") -> None:
        super().__init__()
        self._mode = mode
        self._agent: Any = None
        self._language = "cn"
        self._tool: Optional[TodoListTool] = None
        self._states: dict[str, _RunState] = {}
        self._carry: dict[str, tuple[int, int]] = {}  # 会话 id -> 暂停时的计数，见 _RunState.carried
        self._missing_tools_logged = False

    def init(self, agent: Any) -> None:
        # 与 stream_event_rail._get_todo_tool 同源：主 agent 的 todo 目录取自
        # deep_config.workspace，不取全局资源表（子 agent 会覆盖）。
        self._agent = agent
        try:
            deep_config = agent.deep_config
            language = getattr(deep_config, "language", None) or "cn"
            self._language = language
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
        if not isinstance(ctx.inputs, InvokeInputs):
            return
        state = self._begin_run(ctx)
        try:
            found = serialize_todo_tools(getattr(self._agent, "ability_manager", None))
        except Exception as exc:
            self._rail_error(state, "before_invoke", exc)
            return
        if found == 0:
            self._report_missing_tools(state)

    def _begin_run(self, ctx: AgentCallbackContext) -> Optional[_RunState]:
        if self._tool is None or ctx.session is None:
            return None
        try:
            sid = _session_id(ctx.session)
            if not sid:
                return None
            state = _RunState(session_id=sid, carried=self._carry.pop(sid, None))
            self._states[sid] = state
            ctx.extra[_SID_KEY] = sid
            return state
        except Exception as exc:
            self._rail_error(None, "before_invoke", exc)
            return None

    def _report_missing_tools(self, state: Optional[_RunState]) -> None:
        """每次执行都计入 rail_errors；日志只报一次，避免每轮刷屏。"""
        if self._missing_tools_logged:
            if state is not None:
                state.rail_errors += 1
            return
        self._missing_tools_logged = True
        self._rail_error(state, "before_invoke", TodoSnapshotError("missing_todo_tools"))

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        if state is None:
            return
        try:
            if state.first_call_pending:
                state.first_call_pending = False
                await self._on_first_call(ctx, state)
            await self._maybe_remind(ctx, state)
        except Exception as exc:
            self._rail_error(state, "before_model_call", exc)

    async def _on_first_call(self, ctx: AgentCallbackContext, state: _RunState) -> None:
        # 续跑标记只在内层上下文里，所以到首次模型调用才判断是否沿用暂停时的计数；
        # 不沿用的话，审批多的长任务每段都从 0 计，攒不满间隔。
        if state.carried is not None and any(ctx.extra.get(marker) for marker in _RESUME_MARKERS):
            since_change, since_check = state.carried
            state.last_change_iter = -since_change
            state.last_remind_check_iter = -since_check
        # 首次调用读一次：上一轮留下待办、本轮一直没写的执行也要把 had_todos 记为真，
        # 统计时与本轮写过 todo 的执行分开看。
        await self._read(state)

    async def _maybe_remind(self, ctx: AgentCallbackContext, state: _RunState) -> None:
        # 先比计数再读文件：大多数模型调用不满足间隔，不必读
        since_change = state.iteration - state.last_change_iter
        if since_change < _REMIND_INTERVAL or state.iteration - state.last_remind_check_iter < _REMIND_INTERVAL:
            return
        # 读之前就重新计间隔：没有未完成项或读取失败时，下次也要再隔满一个间隔才读
        state.last_remind_check_iter = state.iteration
        todos = await self._read(state)
        if unfinished(todos):
            await self._inject(ctx, state, build_reminder(todos, since_change, self._language), todos)

    async def _inject(self, ctx: AgentCallbackContext, state: _RunState, text: str, todos: list[TodoItem]) -> None:
        # 追加到上下文末尾并写回，进入本次请求；不改动已有消息，不破坏前缀缓存
        await ctx.context.add_messages(UserMessage(content=text))
        state.reminders += 1
        state.intervals.start(state.iteration)
        _log("inject", {
            "run_id": state.run_id,
            "reminder_seq": state.intervals.seq,
            "iteration": state.iteration,
            "iters_since_change": state.iteration - state.last_change_iter,
            "unfinished": len(unfinished(todos)),
            "chars": len(text),
        })

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
        if state is None or not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in TODO_WRITE_TOOLS:
            return
        try:
            call_id = getattr(inputs.tool_call, "id", None) or ""
            state.pre_write[call_id] = snapshot_key(await self._read(state))
        except Exception as exc:
            self._rail_error(state, "before_tool_call", exc)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        state = self._state(ctx)
        inputs = ctx.inputs
        if state is None or not isinstance(inputs, ToolCallInputs) or inputs.tool_name not in TODO_WRITE_TOOLS:
            return
        call_id = getattr(inputs.tool_call, "id", None) or ""
        executed = False
        succeeded = False
        change: Change = "unknown"
        receipt_chars = 0
        try:
            if not inputs.execution_started:
                return  # 被前置钩子拦下（如权限拒绝），工具没有执行，不算写入
            executed = True
            result = inputs.tool_result
            succeeded = isinstance(result, dict) and isinstance(result.get("message"), str)
            if succeeded:
                state.writes_ok += 1
            else:
                state.writes_failed += 1
            try:
                todos = await self._read(state)
            except Exception as exc:
                self._rail_error(state, "after_tool_call", exc)
                state.change_unknown += 1
                return  # 写入后快照不可用：保留工具原回执
            after = snapshot_key(todos)
            before = state.pre_write.get(call_id)
            if before is None:
                state.change_unknown += 1  # 写入前快照不可用，不能拿空列表代替
            elif before != after:
                state.changes += 1
                state.changed_this_iter = True
                change = "changed"
            else:
                state.noops += 1
                change = "noop"
            if succeeded:
                receipt_chars = self._enhance_receipt(inputs, state, todos, noop=change == "noop")
        except Exception as exc:
            self._rail_error(state, "after_tool_call", exc)
        finally:
            state.pre_write.pop(call_id, None)
            if executed:
                try:
                    self._log_write(state, inputs.tool_name, succeeded, change, receipt_chars)
                except Exception as exc:
                    self._rail_error(state, "after_tool_call", exc)

    def _enhance_receipt(self, inputs: ToolCallInputs, state: _RunState, todos: list[TodoItem], *, noop: bool) -> int:
        """追加回执并返回追加的字符数；没有增强时返回 0。"""
        tool_msg = inputs.tool_msg
        content = getattr(tool_msg, "content", None)
        if tool_msg is None or not isinstance(content, str) or not hasattr(tool_msg, "model_copy"):
            return 0
        suffix = receipt_suffix(inputs.tool_name, todos, noop=noop, language=self._language)
        # 只改给模型的工具消息（保留 tool_call_id）；界面卡片取自 tool_result，不受影响
        inputs.tool_msg = tool_msg.model_copy(update={"content": content + suffix})
        state.receipts += 1
        state.receipt_chars += len(suffix)
        state.receipt_chars_max = max(state.receipt_chars_max, len(suffix))
        return len(suffix)

    @staticmethod
    def _log_write(state: _RunState, tool_name: str, succeeded: bool, change: Change, receipt_chars: int) -> None:
        intervals = state.intervals
        after_reminder = intervals.record_write(change)
        _log("write", {
            "run_id": state.run_id,
            "tool": tool_name,
            "reminder_seq": intervals.seq,
            "iteration": state.iteration,
            "result": "ok" if succeeded else "failed",
            "change": change,
            "after_reminder": after_reminder,
            "iters_since_inject": state.iteration - intervals.inject_iter if intervals.seq else None,
            "receipt_chars": receipt_chars,
        })

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
            state.intervals.close()
            result = getattr(ctx.inputs, "result", None)
            result_type = result.get("result_type") if isinstance(result, dict) else None
            result_type = str(result_type) if result_type else "unknown"
            self._log_run_end(state, final, result_type)
            if result_type == "interrupt":
                self._keep_counts_for_resume(state)
        finally:
            self._states.pop(state.session_id, None)

    def _keep_counts_for_resume(self, state: _RunState) -> None:
        """问卷/审批暂停时记下计数，续跑时在 _on_first_call 里接着算。"""
        if state.first_call_pending and state.carried is not None:
            # 续跑只执行了已批准的工具、没调模型就又暂停（还有工具等审批）：
            # 本次没有迭代，带进来的计数原样留给下次续跑
            since_change, since_check = state.carried
        else:
            since_change = state.iteration - state.last_change_iter
            since_check = state.iteration - state.last_remind_check_iter
        # 暂停前改过列表、迭代还没收尾，也按刚变化处理
        if state.changed_this_iter:
            since_change = 0
        self._carry.pop(state.session_id, None)
        self._carry[state.session_id] = (since_change, since_check)
        while len(self._carry) > _CARRY_MAX:
            self._carry.pop(next(iter(self._carry)))

    def _log_run_end(self, state: _RunState, final: Optional[dict[str, int]], result_type: str) -> None:
        _log("run_end", {
            "run_id": state.run_id,
            "mode": self._mode,
            "model": state.model or "unknown",
            "session": _session_prefix(state.session_id),
            "result_type": result_type,
            "had_todos": state.had_todos,
            "final": final if final is not None else "unknown",
            "writes_ok": state.writes_ok,
            "writes_failed": state.writes_failed,
            "changes": state.changes,
            "noops": state.noops,
            "change_unknown": state.change_unknown,
            "receipts": state.receipts,
            "receipt_chars": state.receipt_chars,
            "receipt_chars_max": state.receipt_chars_max,
            "reminders": state.reminders,
            "intervals": dict(sorted(state.intervals.counts.items())),
            "iteration": state.iteration,
            "iters_since_change": state.iteration - state.last_change_iter,
            "rail_errors": state.rail_errors,
        })


__all__ = ["TodoProgressRail"]
