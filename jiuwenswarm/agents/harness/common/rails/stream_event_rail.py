# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuSwarmStreamEventRail — Stream event emission, pause checks, context fix.

Migrated from JiuSwarmReActAgent:
  - _emit_tool_call / _emit_tool_result / _emit_todo_updated / _emit_context_usage
  - _fix_incomplete_tool_context
  - Pause checkpoint logic
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Any, List, Optional

from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolMessage,
)
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools import TodoListTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    strip_image_content_from_model_context,
)
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyToolStreamHandler,
)
from jiuwenswarm.common.tool_display import (
    build_tool_display_name,
    extract_call_goal,
    inject_call_goal_schema,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.common.todo_snapshot import format_todos_for_frontend

_TODO_TOOL_NAMES = frozenset(["todo_create", "todo_get", "todo_list", "todo_modify"])


def _structured_tool_result_payload(result: Any) -> Any | None:
    detailed_output = getattr(result, "detailed_output", None)
    if detailed_output is not None:
        return detailed_output
    if isinstance(result, (dict, list)):
        return result
    return None


def _parse_tool_call_arguments(tool_call: Any) -> dict[str, Any]:
    raw_args = getattr(tool_call, "arguments", None) if tool_call else None
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_tool_interrupt(value: Any) -> Any | None:
    if value is None:
        return None
    if value.__class__.__name__ == "ToolInterruptException" and hasattr(value, "request"):
        return value

    for attr_name in ("cause", "__cause__"):
        cause = getattr(value, attr_name, None)
        if cause is not None and cause is not value:
            interrupt = _extract_tool_interrupt(cause)
            if interrupt is not None:
                return interrupt
    return None


def _normalize_ask_user_interrupt_value(value_obj: Any, tool_args: dict[str, Any]) -> Any:
    """Attach ask_user tool metadata so plain-query interrupts are not misclassified."""
    if isinstance(value_obj, dict):
        if str(value_obj.get("tool_name") or "").strip() == "ask_user":
            return value_obj
        if value_obj.get("tool_args"):
            return value_obj
        return {
            **value_obj,
            "tool_name": "ask_user",
            "tool_args": tool_args,
            "message": value_obj.get("message") or tool_args.get("query") or "",
        }

    tool_name = str(getattr(value_obj, "tool_name", "") or "").strip()
    existing_args = getattr(value_obj, "tool_args", None)
    if tool_name == "ask_user" and existing_args:
        return value_obj

    return {
        "tool_name": "ask_user",
        "tool_args": tool_args,
        "message": str(getattr(value_obj, "message", "") or tool_args.get("query") or ""),
        "questions": getattr(value_obj, "questions", None) or tool_args.get("questions") or [],
    }


def _ask_user_question_payload_from_interrupt(tool_call: Any, interrupt: Any) -> dict[str, Any] | None:
    request_id = str(
        getattr(getattr(interrupt, "request", None), "tool_call_id", None)
        or getattr(tool_call, "id", "")
        or ""
    ).strip()
    if not request_id:
        return None

    args = _parse_tool_call_arguments(tool_call)
    value_obj = getattr(interrupt, "request", None)
    if value_obj is None:
        if not args:
            return None
        value_obj = {"tool_name": "ask_user", "tool_args": args, "questions": args.get("questions", [])}
    elif args:
        value_obj = _normalize_ask_user_interrupt_value(value_obj, args)

    return convert_interactions_to_ask_user_question([{"id": request_id, "value": value_obj}])


def _boolish_false(value: Any) -> bool:
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}


def _boolish_true(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _nonzero_exit(value: Any) -> bool | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        try:
            return int(value.strip()) != 0
        except ValueError:
            return None
    return None


def _infer_tool_result_error(value: Any) -> bool | None:
    if isinstance(value, dict):
        if "success" in value:
            if _boolish_false(value.get("success")):
                return True
            if _boolish_true(value.get("success")):
                return False
        if _boolish_true(value.get("is_error")) or _boolish_true(value.get("isError")):
            return True
        status = value.get("status")
        if isinstance(status, str) and status.strip().lower() in {"error", "failed", "failure"}:
            return True
        for key in ("exit_code", "exitCode", "returncode", "return_code"):
            exit_failed = _nonzero_exit(value.get(key))
            if exit_failed is not None:
                return exit_failed
        for key in ("data", "raw_output", "rawOutput", "result"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                nested_error = _infer_tool_result_error(nested)
                if nested_error is not None:
                    return nested_error
        return None

    if isinstance(value, list):
        for item in value:
            item_error = _infer_tool_result_error(item)
            if item_error:
                return True
        return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            parsed_error = _infer_tool_result_error(parsed)
            if parsed_error is not None:
                return parsed_error
        if re.search(r"\bsuccess\s*[:=]\s*False\b", text, re.IGNORECASE):
            return True
        if text.startswith("[ERROR]"):
            return True
        exit_match = re.search(
            r"\b(?:exit(?:[_ ]?code)?|returncode|return[_ ]code)\s*[:= ]\s*(-?\d+)\b",
            text,
            re.IGNORECASE,
        )
        if exit_match:
            return int(exit_match.group(1)) != 0
    return None


class JiuSwarmStreamEventRail(DeepAgentRail):
    """Emit frontend stream events and enforce pause/abort checkpoints.

    Pause/abort state is owned by this Rail (not DeepAgent) so that
    interface.py can call rail.pause() / rail.resume() / rail.abort()
    without requiring changes to DeepAgent.
    """

    priority = 80

    # Key used in ctx.extra to carry session_id from before_invoke to checkpoints.
    # ctx.extra persists across all events within a single invoke, so sub-agent
    # checkpoints inherit the parent's session_id (correct: parent abort → sub stops).
    _SID_KEY = "__jiuwenswarm_session_id__"
    _SHELL_SID_TOKEN_KEY = "__jiuwenswarm_shell_session_token__"

    def __init__(self, *, member_name: str | None = None, role: str | None = None) -> None:
        super().__init__()
        self._deep_agent: Optional[Any] = None
        self._member_name = str(member_name or "").strip()
        self._role = str(role or "").strip().lower()
        # Per-session pause/abort state.  Keyed by session_id (conversation_id).
        # Shared adapter instances serve multiple concurrent sessions; scalar state
        # would cause cross-session contamination (session A cancel kills session B).
        self._abort_requested: dict[str, bool] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        # Per-session conversation context
        self._conversation_ids: dict[str, str] = {}
        self._main_sessions: dict[str, Session] = {}
        # Shared across sessions (same workspace → same tool instance)
        self._main_todo_tool: Optional[TodoListTool] = None
        # Track in-flight tool calls for cancellation status emission
        self._inflight_tool_calls: dict[str, dict[str, Any]] = {}
        # Store cancelled tool info for interrupt response (per-session to avoid
        # cross-session leakage in concurrent collect→get→clear sequences).
        self._cancelled_tool_results: dict[str, list[dict[str, Any]]] = {}
        self._symphony_stream_handler = SymphonyToolStreamHandler()

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    def _get_prompt_language(self) -> str:
        """Get the current prompt language from the agent's system_prompt_builder."""
        return getattr(
            getattr(self._deep_agent, "system_prompt_builder", None),
            "language", None,
        ) or "cn"

    def _read_image_multimodal_enabled(self) -> bool:
        deep_config = (
            getattr(self._deep_agent, "deep_config", None)
            or getattr(self._deep_agent, "_deep_config", None)
        )
        return bool(getattr(deep_config, "enable_read_image_multimodal", False))

    def _tool_interrupted_message(self, tool_name: str) -> str:
        """Build a language-aware tool interruption message."""
        if self._get_prompt_language() == "en":
            return f"[Tool interrupted] Tool {tool_name} was interrupted by the user and has no result."
        return f"[工具执行被中断] 工具 {tool_name} 执行过程中被用户打断，没有执行结果。"

    @staticmethod
    def _tool_call_id(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("id") or tool_call.get("tool_call_id") or "")
        return str(
            getattr(tool_call, "id", "")
            or getattr(tool_call, "tool_call_id", "")
            or ""
        )

    @staticmethod
    def _tool_call_name(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            function = tool_call.get("function")
            if isinstance(function, dict):
                return str(function.get("name") or tool_call.get("name") or "")
            return str(tool_call.get("name") or "")
        return str(getattr(tool_call, "name", "") or "")

    def _tool_interrupt_placeholders_by_id(
        self,
        messages: list[Any],
    ) -> dict[str, str]:
        """Map tool_call_id to the exact placeholder content emitted by this rail."""
        placeholders: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            for tool_call in getattr(message, "tool_calls", None) or []:
                tool_call_id = self._tool_call_id(tool_call)
                if not tool_call_id:
                    continue
                placeholders[tool_call_id] = self._tool_interrupted_message(
                    self._tool_call_name(tool_call),
                )
        return placeholders

    @staticmethod
    def _tool_call_names_by_id(messages: list[Any]) -> dict[str, str]:
        """Map tool_call_id back to the originating assistant tool name.

        This is NOT enough to classify fake/real tool messages by itself.
        We use it only to recover the expected tool name for a ToolMessage's
        tool_call_id, then match that message content against known interrupt
        placeholder templates for that tool.
        """
        names: dict[str, str] = {}
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            for tool_call in getattr(message, "tool_calls", None) or []:
                tool_call_id = JiuSwarmStreamEventRail._tool_call_id(tool_call)
                if not tool_call_id:
                    continue
                names[tool_call_id] = JiuSwarmStreamEventRail._tool_call_name(tool_call)
        return names

    @staticmethod
    def _tool_message_text(message: ToolMessage) -> str | None:
        content = getattr(message, "content", None)
        return content if isinstance(content, str) else None

    @staticmethod
    def _normalize_tool_interrupt_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _is_legacy_tool_interrupt_placeholder_text(
        self,
        content: str,
        tool_name: str,
    ) -> bool:
        legacy_templates = [
            f"[Tool execution interrupted] Tool {tool_name} was interrupted by user during execution, "
            f"no result available.",
            f"[Tool interrupted] Tool {tool_name} was interrupted by the user and has no result.",
            f"[工具执行被中断] 工具 {tool_name} 执行过程中被用户打断，没有执行结果。",
        ]
        normalized_content = self._normalize_tool_interrupt_text(content)
        return any(
            normalized_content == self._normalize_tool_interrupt_text(template)
            for template in legacy_templates
        )

    def _is_tool_interrupt_placeholder(
        self,
        message: Any,
        placeholders_by_id: dict[str, str],
        tool_names_by_id: dict[str, str],
    ) -> bool:
        """Classify whether a ToolMessage is an interrupt placeholder.

        Decision rule:
        1. tool_call_id identifies which assistant tool call this ToolMessage
           belongs to.
        2. tool_call_id -> tool_name lets us recover the expected tool name.
        3. We then compare content against known interrupt placeholder text
           variants for that tool. So fake/real is still determined by content,
           not by tool_call_id alone.
        """
        if not isinstance(message, ToolMessage):
            return False
        tool_call_id = getattr(message, "tool_call_id", "")
        if not tool_call_id:
            return False
        expected = placeholders_by_id.get(tool_call_id)
        content = self._tool_message_text(message)
        if not content:
            return False
        if expected and content == expected:
            return True
        tool_name = tool_names_by_id.get(tool_call_id, "")
        if not tool_name:
            return False
        return self._is_legacy_tool_interrupt_placeholder_text(content, tool_name)

    def _resolve_sid(self, ctx: AgentCallbackContext, session: Session | None = None) -> str:
        """Resolve the per-session key used by this rail.

        99aa04963 made pause/abort and conversation state per-session. Most
        callbacks inherit ctx.extra from before_invoke, but tool callbacks can
        arrive without that value depending on agent-core callback boundaries.
        Fall back to the captured main session identity so main-agent tool calls
        can still find their conversation_id while unrelated sessions remain
        isolated.
        """
        sid = ctx.extra.get(self._SID_KEY, "")
        if isinstance(sid, str) and sid:
            return sid
        if session is not None:
            for known_sid, known_session in self._main_sessions.items():
                if session is known_session:
                    ctx.extra[self._SID_KEY] = known_sid
                    return known_sid
        return "default"

    # -- pause / resume / abort API for interface.py --
    # All methods accept session_id to scope state per-session on shared adapters.

    def _get_pause_event(self, sid: str) -> asyncio.Event:
        """Lazily get/create pause event for a session. Created events start in set (unpaused)."""
        event = self._pause_events.get(sid)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._pause_events[sid] = event
        return event

    def pause(self, session_id: str = "") -> None:
        sid = session_id or "default"
        self._get_pause_event(sid).clear()

    def resume(self, session_id: str = "") -> None:
        sid = session_id or "default"
        self._abort_requested.pop(sid, None)
        self._get_pause_event(sid).set()

    def abort(self, session_id: str = "") -> None:
        sid = session_id or "default"
        self._abort_requested[sid] = True
        self._get_pause_event(sid).set()
        if sid:
            try:
                from openjiuwen.core.sys_operation.shell_process_registry import (
                    kill_shell_processes_for_session_tree,
                )

                killed = kill_shell_processes_for_session_tree(sid)
                if killed:
                    logger.info(
                        "[StreamEventRail] killed %d shell process(es) for session=%s",
                        killed,
                        sid,
                    )
            except Exception:
                logger.debug(
                    "[StreamEventRail] kill_commands_for_session failed",
                    exc_info=True,
                )

    def reset_abort(self, session_id: str = "") -> None:
        sid = session_id or "default"
        self._abort_requested.pop(sid, None)

    def reset_for_new_task(self, session_id: str = "") -> None:
        """Unblock the pause event for the next task without touching the abort flag.

        Called on cancel so that a new task can start without being stuck at
        the _pause_event.wait() checkpoint. The abort flag is intentionally
        NOT cleared here — it must remain True until the next task's
        process_message_*_impl calls reset_abort() at entry, ensuring the
        in-flight checkpoint (before_model_call / before_tool_call) can still
        observe the flag and raise CancelledError.
        """
        sid = session_id or "default"
        self._get_pause_event(sid).set()
        self._conversation_ids.pop(sid, None)
        self._main_sessions.pop(sid, None)

    def cleanup_session(self, session_id: str = "") -> None:
        """Remove ALL per-session state for *session_id*.

        Called by the adapter when the last task for a session completes
        (Counter drops to 0). Prevents unbounded growth of the per-session
        dicts on long-lived adapters serving many unique sessions.
        """
        sid = session_id or "default"
        self._abort_requested.pop(sid, None)
        self._pause_events.pop(sid, None)
        self._conversation_ids.pop(sid, None)
        self._main_sessions.pop(sid, None)
        self._cancelled_tool_results.pop(sid, None)

    def get_cancelled_tool_results(self, session_id: str = "") -> list[dict[str, Any]]:
        """Get cancelled tool results collected during interrupt.

        Args:
            session_id: Return results for this session only.

        Returns list of tool_result dicts for gateway to forward to frontend.
        """
        sid = session_id or "default"
        return list(self._cancelled_tool_results.get(sid, []))

    def clear_cancelled_tool_results(self, session_id: str = "") -> None:
        """Clear cancelled tool results after they've been retrieved."""
        sid = session_id or "default"
        self._cancelled_tool_results.pop(sid, None)

    def collect_cancelled_tool_updates(self, session_id: str = "") -> None:
        """Collect cancelled tool info for interrupt response.

        Args:
            session_id: Only collect tools for this session. If empty, collect all.
        """
        sid = session_id or "default"
        bucket = self._cancelled_tool_results.setdefault(sid, [])
        for tc_id, info in list(self._inflight_tool_calls.items()):
            # Only collect tools matching the target session
            if session_id and info.get("session_id") != session_id:
                continue
            tc = info.get("tool_call")
            if tc is None:
                continue
            bucket.append({
                "tool_name": getattr(tc, "name", ""),
                "tool_call_id": tc_id,
                "result": "[Interrupted] Tool execution cancelled by user.",
                "status": "error",
            })
            self._inflight_tool_calls.pop(tc_id, None)
        logger.info(
            "[StreamEventRail] collected %d cancelled tools for session=%s",
            len(bucket),
            session_id,
        )

    # ------------------------------------------------------------------
    # before_invoke (Outer event on DeepAgent): capture conversation_id
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, InvokeInputs):
            return
        # Subagents have no session on their before_invoke (ctx.session is None);
        # the main agent always has one.  Use this to distinguish without relying
        # on conv_id naming conventions.
        if ctx.session is None:
            return
        # Use the real conversation_id as the session key; fall back to "default"
        # only for the pause/abort state lookup key (sid).  Do NOT store the
        # "default" sentinel as a conversation_id value — after_tool_call uses
        # truthiness to decide whether to emit todo.updated, and a literal
        # "default" would trigger _emit_todo_updated with a bogus session key.
        raw_conv_id = ctx.inputs.conversation_id or ""
        sid = raw_conv_id or "default"
        if raw_conv_id:
            self._conversation_ids[sid] = raw_conv_id
        self._main_sessions[sid] = ctx.session
        # Carry session_id through ctx.extra so checkpoints (before_model_call,
        # before_tool_call) within this invoke can look up per-session state.
        # Sub-agents inherit this from the parent's invoke since they don't
        # fire their own before_invoke (ctx.session is None → early return above).
        ctx.extra[self._SID_KEY] = sid
        try:
            from openjiuwen.core.sys_operation.shell_process_registry import (
                set_shell_session_id,
            )

            ctx.extra[self._SHELL_SID_TOKEN_KEY] = set_shell_session_id(raw_conv_id or sid)
        except Exception:
            logger.debug("[StreamEventRail] set_shell_session_id failed", exc_info=True)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        token = ctx.extra.pop(self._SHELL_SID_TOKEN_KEY, None)
        if token is None:
            return
        try:
            from openjiuwen.core.sys_operation.shell_process_registry import (
                reset_shell_session_id,
            )

            reset_shell_session_id(token)
        except Exception:
            logger.debug("[StreamEventRail] reset_shell_session_id failed", exc_info=True)

    # ------------------------------------------------------------------
    # before_model_call: pause check + context fix + compression info
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        sid = self._resolve_sid(ctx, ctx.session)
        await self._get_pause_event(sid).wait()
        if self._abort_requested.get(sid, False):
            raise asyncio.CancelledError("Agent abort requested")

        self._inject_tool_call_goal_schema(ctx)

        if ctx.context is not None:
            if not self._read_image_multimodal_enabled():
                strip_image_content_from_model_context(ctx.context)
            await self._fix_incomplete_tool_context(ctx)

    @staticmethod
    def _inject_tool_call_goal_schema(ctx: AgentCallbackContext) -> None:
        """仅给送入 LLM 的 ToolInfo 注入 call_goal，且必须 deepcopy。

        不可就地改 parameters / card.input_params：ToolInfo 与执行侧 schema 常共享
        内层 properties，注入后 SchemaUtils 会补上 call_goal=None，LocalFunction
        再 **kwargs 传给 send_file 等实现会直接 TypeError（表现为工具挂掉）。
        """
        tools = getattr(ctx.inputs, "tools", None) or []
        if not tools:
            return
        next_tools: list[Any] = []
        changed = False
        for tool in tools:
            params = getattr(tool, "parameters", None)
            if not isinstance(params, dict):
                next_tools.append(tool)
                continue
            props = params.get("properties")
            if isinstance(props, dict) and "call_goal" in props:
                next_tools.append(tool)
                continue
            cloned = copy.deepcopy(params)
            inject_call_goal_schema(cloned)
            if cloned == params:
                next_tools.append(tool)
                continue
            model_copy = getattr(tool, "model_copy", None)
            if callable(model_copy):
                try:
                    next_tools.append(model_copy(update={"parameters": cloned}))
                    changed = True
                    continue
                except Exception as exc:
                    # model_copy 可能抛 ValidationError 等与具体 ToolInfo 实现相关的异常；
                    # 注入失败时跳过该工具，不阻断主链路。
                    logger.warning(
                        "[StreamEventRail] model_copy for call_goal failed; skip inject tool=%s err=%s",
                        getattr(tool, "name", type(tool).__name__),
                        exc,
                    )
            # 无 model_copy / copy 失败：绝不回写原始 ToolInfo，避免 call_goal 泄漏进执行侧。
            next_tools.append(tool)
        if changed:
            try:
                ctx.inputs.tools = next_tools
            except (AttributeError, TypeError) as exc:
                logger.warning(
                    "[StreamEventRail] replace tools with call_goal schema failed: %s",
                    exc,
                )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._emit_context_usage(
            ctx,
            member_name=self._member_name or None,
            role=self._role or None,
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        sid = self._resolve_sid(ctx, ctx.session)
        await self._get_pause_event(sid).wait()
        if self._abort_requested.get(sid, False):
            raise asyncio.CancelledError("Agent abort requested")

        session = ctx.session
        if session is not None and isinstance(ctx.inputs, ToolCallInputs):
            tc = ctx.inputs.tool_call
            # 主模型随 tool_call 产出的目标文案（call_goal）：取出后剥掉，避免 schema 拒收。
            # 绝不碰 display_name（team 成员名等业务字段）。
            model_display, cleaned_args = extract_call_goal(
                getattr(tc, "arguments", {}) if tc else {}
            )
            # 无论是否填了 call_goal，都写回清洗后的 arguments，避免执行侧拿到该字段。
            if tc is not None:
                try:
                    tc.arguments = cleaned_args
                except (AttributeError, TypeError) as exc:
                    logger.warning(
                        "[StreamEventRail] rewrite tool arguments without call_goal failed; tool_id=%s err=%s",
                        getattr(tc, "id", ""),
                        exc,
                    )
                ctx.inputs.tool_args = cleaned_args
            await self._emit_tool_call(session, tc, model_display_name=model_display)
            await self._emit_tool_update(session, tc, status="in_progress")
            self._symphony_stream_handler.bind_progress(ctx, session, tc)
            # Track in-flight tool call for cancellation
            tc_id = getattr(tc, "id", "")
            if tc_id:
                self._inflight_tool_calls[tc_id] = {
                    "tool_call": tc,
                    "session": session,
                    "session_id": sid,
                }

    # ------------------------------------------------------------------
    # after_tool_call: emit tool_result + todo.updated
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        session = ctx.session
        if session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return

        tc = ctx.inputs.tool_call
        tc_id = getattr(tc, "id", "")
        self._symphony_stream_handler.reset_progress(ctx)
        # Remove from in-flight tracking on completion
        if tc_id:
            self._inflight_tool_calls.pop(tc_id, None)

        await self._emit_tool_result(session, tc, ctx.inputs.tool_result)
        self._symphony_stream_handler.request_force_finish(
            ctx,
            tc,
            ctx.inputs.tool_result,
        )
        await self._emit_ask_user_question_if_interrupted(
            session,
            tc,
            ctx.inputs.tool_name,
            ctx.inputs.tool_result,
            ctx.exception,
        )

        tool_name = ctx.inputs.tool_name
        sid = self._resolve_sid(ctx, session)
        conv_id = self._conversation_ids.get(sid, "")
        if not conv_id:
            return
        if tool_name in _TODO_TOOL_NAMES:
            # Emit the main-agent todo snapshot after every todo tool call.  The
            # todo tool itself is loaded from the main workspace below, so this
            # stays authoritative even when a resumed/supplement turn uses a
            # different stream session object.
            await self._emit_todo_updated(session, conv_id)

    # ------------------------------------------------------------------
    # on_model_exception: attempt context repair
    # ------------------------------------------------------------------

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        if ctx.context is not None:
            logger.info("[StreamEventRail] Attempting context repair after model exception")
            await self._fix_incomplete_tool_context(ctx)

    # ------------------------------------------------------------------
    # Private helpers (migrated from JiuSwarmReActAgent)
    # ------------------------------------------------------------------

    @staticmethod
    async def _emit_tool_call(
        session: Session,
        tool_call: Any,
        *,
        model_display_name: str = "",
    ) -> None:
        try:
            name = getattr(tool_call, "name", "")
            arguments = getattr(tool_call, "arguments", {})
            tool_call_payload: dict[str, Any] = {
                "name": name,
                "arguments": arguments,
                "tool_call_id": getattr(tool_call, "id", ""),
            }
            # 优先用主模型随 tool_call 产出的目标文案；未填时再规则兜底。
            display_name = (model_display_name or "").strip() or build_tool_display_name(
                name, arguments
            )
            if display_name:
                tool_call_payload["display_name"] = display_name
            await session.write_stream(
                OutputSchema(
                    type="tool_call",
                    index=0,
                    payload={"tool_call": tool_call_payload},
                )
            )
        except Exception:
            logger.debug("tool_call emit failed", exc_info=True)

    async def _emit_tool_result(
        self,
        session: Session,
        tool_call: Any,
        result: Any,
    ) -> None:
        try:
            raw_output = _structured_tool_result_payload(result)
            tool_result_payload = {
                "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                "result": str(result)[:60000] if result is not None else "",
            }
            if raw_output is not None:
                tool_result_payload["raw_output"] = raw_output
                self._symphony_stream_handler.enrich_result_payload(
                    tool_call,
                    tool_result_payload,
                    raw_output,
                )
            error_state = _infer_tool_result_error(raw_output if raw_output is not None else result)
            if error_state is not None:
                tool_result_payload["success"] = not error_state
                if error_state:
                    tool_result_payload["status"] = "error"
                    tool_result_payload["is_error"] = True
            await session.write_stream(
                OutputSchema(
                    type="tool_result",
                    index=0,
                    payload={
                        "tool_result": tool_result_payload
                    },
                )
            )
        except Exception:
            logger.debug("tool_result emit failed", exc_info=True)

    @staticmethod
    async def _emit_ask_user_question_if_interrupted(
        session: Session,
        tool_call: Any,
        tool_name: str,
        result: Any,
        exception: Any = None,
    ) -> None:
        if str(tool_name or "").strip() != "ask_user":
            return
        interrupt = _extract_tool_interrupt(result) or _extract_tool_interrupt(exception)
        if interrupt is None:
            return
        payload = _ask_user_question_payload_from_interrupt(tool_call, interrupt)
        if not payload:
            logger.debug("[StreamEventRail] ask_user interrupt payload unavailable")
            return
        try:
            await session.write_stream(
                OutputSchema(
                    type="chat.ask_user_question",
                    index=0,
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("ask_user question emit failed", exc_info=True)

    @staticmethod
    async def _emit_tool_update(session: Session, tool_call: Any, *, status: str) -> None:
        try:
            await session.write_stream(
                OutputSchema(
                    type="tool_update",
                    index=0,
                    payload={
                        "tool_update": {
                            "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                            "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                            "arguments": getattr(tool_call, "arguments", {}) if tool_call else {},
                            "status": str(status or "").strip() or "in_progress",
                        }
                    },
                )
            )
        except Exception:
            logger.debug("tool_update emit failed", exc_info=True)

    async def _emit_todo_updated(self, session: Session, session_id: str) -> None:
        """Load the main agent's todo list and push a todo.updated event to the frontend."""
        todo_tool = self._get_todo_tool()
        if todo_tool is None:
            logger.debug("[StreamEventRail] TodoListTool not available")
            return

        try:
            todos_data = await todo_tool.load_todos(session_id)
        except Exception as exc:
            logger.debug(
                "[StreamEventRail] Failed to load todos: %s", exc
            )
            return

        todos = self._format_todos_for_frontend(todos_data)

        try:
            await session.write_stream(
                OutputSchema(
                    type="todo.updated",
                    index=0,
                    payload={"todos": todos},
                )
            )
        except Exception:
            logger.debug("todo.updated emit failed", exc_info=True)

    def _get_todo_tool(self) -> TodoListTool | None:
        """Build and cache a TodoListTool from the main agent's deep_config workspace.

        Avoids Runner.resource_mgr: subagents register their own tools there and
        overwrite the main agent's entry, causing load_todos to read from the wrong
        workspace path.  deep_config.workspace is fixed at main-agent init time.
        """
        if self._main_todo_tool is not None:
            return self._main_todo_tool

        da = self._deep_agent
        if da is None:
            return None

        try:
            deep_config = da.deep_config
            workspace_path = str(deep_config.workspace.get_node_path(WorkspaceNode.TODO))
            language = getattr(deep_config, "language", None) or getattr(
                getattr(da, "system_prompt_builder", None), "language", "cn"
            ) or "cn"
            self._main_todo_tool = TodoListTool(
                operation=deep_config.sys_operation,
                workspace=workspace_path,
                language=language,
                agent_id=da.card.id,
            )
            return self._main_todo_tool
        except Exception as exc:
            logger.debug(
                "[StreamEventRail] Failed to create TodoListTool: %s", exc
            )
            return None

    @staticmethod
    def _format_todos_for_frontend(
        todos_data: List[Any],
    ) -> List[dict[str, Any]]:
        """Format todo items for frontend compatibility.

        Delegates to the shared snapshot helper so history restore and live
        tool-call emits stay on one field mapping.
        """
        return format_todos_for_frontend(todos_data)

    @staticmethod
    async def _emit_context_usage(
        ctx: AgentCallbackContext,
        *,
        member_name: str | None = None,
        role: str | None = None,
    ) -> None:
        """Emit context usage stats (context_max, tokens_used, rate)."""
        session = ctx.session
        if session is None:
            return

        context = ctx.context
        if context is None:
            return

        model_name = None
        try:
            agent = ctx.agent
            if agent is not None:
                config = getattr(agent, '_config', None)
                if config is not None:
                    model_name = getattr(config, 'model_name', None)
        except Exception:
            logger.debug("Failed to get model_name from ctx.agent", exc_info=True)

        try:
            # raw_total_tokens: model max context window — use agent-core's resolver
            # with built-in dict + 200000 fallback (never returns 0)
            raw_total_tokens = ContextUtils.resolve_context_max(
                model_name=model_name,
                fallback_context_window_tokens=getattr(context, "_context_window_tokens", None),
                model_context_window_tokens=getattr(context, "_model_context_window_tokens", None),
            )

            # The context window contains model input, not the generated reply.
            # Some providers only expose total_tokens, so keep it as a fallback.
            response = ctx.inputs.response
            usage_metadata = {}
            if response and hasattr(response, 'usage_metadata') and response.usage_metadata:
                usage_metadata = response.usage_metadata.model_dump()
            current_context_tokens = 0
            if isinstance(usage_metadata, dict):
                for token_key in ("input_tokens", "prompt_tokens", "total_tokens"):
                    token_value = usage_metadata.get(token_key)
                    if token_value is not None:
                        current_context_tokens = token_value
                        break

            if raw_total_tokens != 0:
                rate = current_context_tokens / raw_total_tokens * 100
            else:
                rate = 0

            payload = {
                "rate": rate,
                "context_max": raw_total_tokens,
                "tokens_used": current_context_tokens,
            }
            if role:
                payload["role"] = role
            if member_name:
                payload["member_name"] = member_name

            await session.write_stream(
                OutputSchema(
                    type="context.usage",
                    index=0,
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("context_usage emit failed", exc_info=True)

    def _ensure_json_arguments(self, arguments: Any) -> str:
        """Ensure tool call arguments are valid JSON string.

        If arguments is a dict, convert to JSON string. If arguments is a string,
        attempt multi-stage repair (json_repair, rule-based quote fixing) before
        returning valid JSON. If all repair attempts fail, return empty JSON object.

        Args:
            arguments: The arguments value from tool_call.

        Returns:
            Valid JSON string (e.g., '{"key": "value"}').
        """
        if isinstance(arguments, dict):
            return json.dumps(arguments, ensure_ascii=False)
        if isinstance(arguments, str):
            _arguments = arguments.strip()
            if not _arguments:
                return "{}"

            # First attempt: direct parsing
            try:
                json.loads(_arguments)
                return arguments
            except json.JSONDecodeError:
                pass

            # Second attempt: json_repair library
            try:
                import json_repair
                repaired = json_repair.loads(_arguments)
                if isinstance(repaired, dict):
                    logger.info(
                        "[_ensure_json_arguments] stage=json_repair outcome=success."
                    )
                    return json.dumps(repaired, ensure_ascii=False)
                # json_repair returned non-dict (e.g., list, str, int)
                logger.warning(
                    "[_ensure_json_arguments] stage=json_repair outcome=failed."
                )
            except Exception as exc:
                logger.warning(
                    "[_ensure_json_arguments] stage=json_repair, error=%s",
                    str(exc),
                )

            # Third attempt: rule-based quote fixing
            fixed = self._fix_missing_quotes(_arguments)
            if fixed != _arguments:
                try:
                    result = json.loads(fixed)
                    logger.info(
                        "[_ensure_json_arguments] stage=rule_fix outcome=success"
                    )
                    return json.dumps(result, ensure_ascii=False)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[_ensure_json_arguments] stage=rule_fix outcome=failed, error=%s",
                        str(exc),
                    )
            else:
                # rule_fix made no structural change
                logger.warning(
                    "[_ensure_json_arguments] stage=rule_fix outcome=failed"
                )

            logger.warning(
                "[_ensure_json_arguments] outcome=failed_all_stages"
            )
            return "{}"
        return "{}"

    @staticmethod
    def _fix_missing_quotes(json_str: str) -> str:
        """Attempt to fix missing quotes in JSON string.

        Common repair scenarios:
        1. Missing end quote: {"query": hello} -> {"query": "hello"}
        2. Missing key quote: {query: "hello"} -> {"query": "hello"}
        3. Windows path without quotes: {"path": D:/work/file.txt} -> {"path": "D:/work/file.txt"}

        Args:
            json_str: Possibly malformed JSON string

        Returns:
            Repaired JSON string, or original if no repair possible
        """
        s = json_str.strip()

        # Pattern 1: Fix Windows paths (D:/path, C:/path)
        s = re.sub(
            r':\s+([A-Za-z]:/[^\{\[]*?)(?=\s*[,\}\]])',
            lambda m: f': "{m.group(1)}"',
            s
        )

        # Pattern 2: Fix missing end quote for string values (non-path)
        # Match ": value" where value is unquoted string
        s = re.sub(
            r':\s+(?!"|true|false|null|\d+|{|\[|:|"|[A-Za-z]:/)([^\s,\}\[\]""]+?)(?=\s*[,}\]])',
            lambda m: f': "{m.group(1)}"',
            s
        )

        # Pattern 3: Fix missing key quotes ({key: value} -> {"key": value})
        s = re.sub(
            r'{\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
            r'{"\1":',
            s
        )

        return s

    async def _fix_incomplete_tool_context(self, ctx: AgentCallbackContext) -> None:
        """Repair incomplete tool-call history with minimal, rule-based replay.

        Rule:
        - For each assistant tool_calls block, only the window before the next
          UserMessage counts as the "immediate response" area.
        - If a tool_call_id has no ToolMessage in that window, insert one
          immediately after the assistant.
        - If a placeholder ToolMessage exists and a later real ToolMessage with
          the same tool_call_id exists, replace the placeholder in-place with
          the real ToolMessage and drop the later duplicate.
        """
        try:
            context = ctx.context
            if context is None:
                return
            messages = context.get_messages()
            tools = getattr(ctx.inputs, "tools", None) or []
            for tool in tools:
                if not tool.parameters:
                    tool.parameters = {
                        "type": "object",
                        "properties": {}
                    }
                if tool.parameters.get("type") is None:
                    tool.parameters["type"] = "object"
            self._inject_tool_call_goal_schema(ctx)
            len_messages = len(messages)
            if len_messages == 0:
                return

            # Defensive normalization of malformed tool-call argument JSON on
            # replayed history. _ensure_json_arguments returns well-formed
            # arguments byte-for-byte unchanged, so we reassign ONLY when the
            # value actually changes -- i.e. only genuinely malformed JSON
            # (missing quotes / unbalanced braces) is rewritten, while valid
            # arguments stay identical to preserve faithful replay for
            # reasoning models. The authoritative repair still lives in
            # ability_manager at execution time; this is just a safety net.
            for m in messages:
                if isinstance(m, AssistantMessage) and getattr(m, "tool_calls", None):
                    for tc in m.tool_calls:
                        raw = getattr(tc, "arguments", None)
                        if not isinstance(raw, str) or not raw.strip():
                            continue
                        normalized = self._ensure_json_arguments(raw)
                        if normalized != raw:
                            tc.arguments = normalized

            placeholders_by_id = self._tool_interrupt_placeholders_by_id(messages)
            tool_names_by_id = self._tool_call_names_by_id(messages)

            real_tool_messages_by_id: dict[str, ToolMessage] = {}
            for message in messages:
                if not isinstance(message, ToolMessage):
                    continue
                tool_call_id = getattr(message, "tool_call_id", "")
                if (
                    tool_call_id
                    and tool_call_id not in real_tool_messages_by_id
                    and not self._is_tool_interrupt_placeholder(
                        message, placeholders_by_id, tool_names_by_id)
                ):
                    real_tool_messages_by_id[tool_call_id] = message

            rebuilt_messages: list[Any] = []
            changed = False
            inserted = 0
            removed_orphan = 0
            removed_duplicate = 0
            replaced_placeholder = 0
            consumed_real_ids: set[str] = set()
            idx = 0

            while idx < len(messages):
                message = messages[idx]

                if isinstance(message, ToolMessage):
                    removed_orphan += 1
                    changed = True
                    idx += 1
                    continue

                rebuilt_messages.append(message)

                if not isinstance(message, AssistantMessage) or not getattr(message, "tool_calls", None):
                    idx += 1
                    continue

                expected: list[tuple[str, Any]] = []
                expected_ids: set[str] = set()
                for tool_call in message.tool_calls:
                    tcid = self._tool_call_id(tool_call)
                    if tcid and tcid not in expected_ids:
                        expected.append((tcid, tool_call))
                        expected_ids.add(tcid)

                seen_ids: set[str] = set()
                idx += 1
                while idx < len(messages) and isinstance(messages[idx], ToolMessage):
                    tool_message = messages[idx]
                    tool_message_id = str(getattr(tool_message, "tool_call_id", "") or "")
                    if tool_message_id not in expected_ids:
                        changed = True
                        removed_orphan += 1
                        idx += 1
                        continue
                    if tool_message_id in seen_ids:
                        changed = True
                        removed_duplicate += 1
                        idx += 1
                        continue

                    replacement = None
                    if (
                        self._is_tool_interrupt_placeholder(
                            tool_message,
                            placeholders_by_id,
                            tool_names_by_id,
                        )
                        and tool_message_id in real_tool_messages_by_id
                        and real_tool_messages_by_id[tool_message_id] is not tool_message
                    ):
                        replacement = real_tool_messages_by_id[tool_message_id]
                    if replacement is not None:
                        rebuilt_messages.append(replacement)
                        consumed_real_ids.add(tool_message_id)
                        replaced_placeholder += 1
                        changed = True
                    else:
                        rebuilt_messages.append(tool_message)
                    seen_ids.add(tool_message_id)
                    idx += 1

                for tcid, tool_call in expected:
                    if tcid in seen_ids:
                        continue
                    replacement = real_tool_messages_by_id.get(tcid)
                    if replacement is not None:
                        rebuilt_messages.append(replacement)
                        consumed_real_ids.add(tcid)
                    else:
                        rebuilt_messages.append(ToolMessage(
                            content=self._tool_interrupted_message(self._tool_call_name(tool_call)),
                            tool_call_id=tcid,
                        ))
                    inserted += 1
                    changed = True

            if not changed:
                return

            context.pop_messages(size=len(messages))
            for message in rebuilt_messages:
                await context.add_messages(message)

            repair_count = (
                inserted
                + removed_orphan
                + removed_duplicate
                + replaced_placeholder
            )
            if repair_count:
                logger.info(
                    "Repaired tool message context: inserted=%d orphan_removed=%d "
                    "duplicate_removed=%d placeholder_replaced=%d",
                    inserted,
                    removed_orphan,
                    removed_duplicate,
                    replaced_placeholder,
                )
        except Exception as e:
            logger.warning("Failed to fix incomplete tool context: %s", e)
