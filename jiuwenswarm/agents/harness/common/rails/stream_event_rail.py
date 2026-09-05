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
import os
import re
from typing import Any, List, Optional

from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolMessage,
)
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.skills.skill_use_rail import get_current_skill_name
from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.tools import TodoListTool
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    strip_image_content_from_model_context,
)
from jiuwenswarm.agents.harness.common.tools.todo_resume import (
    get_stale_todo_ids,
    get_pre_invoke_todo_ids,
)
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyToolStreamHandler,
)
from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    extract_path_from_arguments,
    handle_read_file_before_tool_call,
    is_read_file_tool,
    normalize_read_file_tool_outcome,
)
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY,
    extract_effective_project_dir,
)
from jiuwenswarm.common.tool_display import (
    build_tool_display_name,
    extract_call_goal,
    inject_call_goal_schema,
)
from jiuwenswarm.common.utils import fix_json_arguments, logger

_TODO_TOOL_NAMES = frozenset(["todo_create", "todo_get", "todo_list", "todo_modify"])
_EARLY_CHECKPOINT_EXTRA_KEY = "_jiuwenswarm_early_checkpoint_done"
_EARLY_CHECKPOINT_ENV = "JIUWENCLAW_EARLY_CHECKPOINT"


def _early_checkpoint_disabled_by_env() -> bool:
    raw = (os.getenv(_EARLY_CHECKPOINT_ENV) or "").strip().lower()
    return raw in {"0", "false", "no", "off"}


# When TOOL_RESULT_DISPLAY_MAX_CHARS is unset, keep the historical emit cap for
# non-enterprise runs. Enterprise deploy normally sets the env (e.g. 500).
_DEFAULT_TOOL_RESULT_DISPLAY_MAX_CHARS = 60000
_TOOL_RESULT_DISPLAY_MAX_CHARS_LIMIT = 100_000


def _resolve_source_skill(session: Any = None) -> str:
    """Return active skill name for tool-call attribution, or empty string.

    Prefer session-backed binding (set by skill_tool); ContextVar alone does
    not propagate across tool execution contexts — same issue as skill_turbo
    request_metadata rebinding below.
    """
    try:
        name = get_current_skill_name(session)
    except Exception:
        logger.debug("resolve source_skill failed", exc_info=True)
        return ""
    return str(name or "").strip()


def _resolve_tool_result_display_max_chars() -> int:
    """Resolve streamed tool_result.result max chars.

    - env configured and valid -> use TOOL_RESULT_DISPLAY_MAX_CHARS
      (0 = no truncation; max 100000)
    - unset / invalid -> 60000 (legacy default in jiuwenswarm)
    """
    raw = os.getenv("TOOL_RESULT_DISPLAY_MAX_CHARS")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_TOOL_RESULT_DISPLAY_MAX_CHARS
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_TOOL_RESULT_DISPLAY_MAX_CHARS
    if parsed < 0 or parsed > _TOOL_RESULT_DISPLAY_MAX_CHARS_LIMIT:
        return _DEFAULT_TOOL_RESULT_DISPLAY_MAX_CHARS
    return parsed


def _format_tool_result_for_stream(result: Any) -> str:
    if result is None:
        return ""
    text = str(result)
    limit = _resolve_tool_result_display_max_chars()
    if limit == 0 or len(text) <= limit:
        return text
    return text[:limit]


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


def extract_tool_interrupt(value: Any) -> Any | None:
    """Find a tool interrupt in wrapped exception chains without looping.

    Class-name matching intentionally supports exceptions crossing duplicated
    SDK import boundaries, where ``isinstance`` can be false for equivalent
    ToolInterruptException classes.
    """
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if (
            current.__class__.__name__ == "ToolInterruptException"
            and hasattr(current, "request")
        ):
            return current
        for attr_name in ("cause", "__cause__"):
            cause = getattr(current, attr_name, None)
            if cause is not None and cause is not current:
                pending.append(cause)
    return None


# Backward-compatible private alias for existing tests/imports.
_extract_tool_interrupt = extract_tool_interrupt


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


_SKILL_TURBO_ADAPTER_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_adapter_token"
_SKILL_TURBO_METADATA_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_metadata_token"
_SKILL_TURBO_WORKSPACE_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_workspace_token"
_SKILL_TURBO_INTERACTIVE_ASK_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_interactive_ask_token"
_SKILL_TURBO_RESUME_ANSWERS_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_resume_answers_token"
_SKILL_TURBO_OUTER_TODO_TOKEN_EXTRA_KEY = "_jiuwenswarm_skill_turbo_outer_todo_token"
_SUBAGENT_PARENT_SESSION_TOKEN_EXTRA_KEY = "_jiuwenswarm_subagent_parent_session_token"


def _reset_skill_turbo_adapter_token(ctx: AgentCallbackContext) -> None:
    """Restore SkillTurbo adapter ContextVar binding for this tool call."""
    token = ctx.extra.pop(_SKILL_TURBO_ADAPTER_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            reset_current_skill_turbo_adapter,
        )
        reset_current_skill_turbo_adapter(token)


def _reset_skill_turbo_metadata_token(ctx: AgentCallbackContext) -> None:
    """Restore request-metadata ContextVar binding for this tool call."""
    token = ctx.extra.pop(_SKILL_TURBO_METADATA_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            reset_current_request_metadata,
        )
        reset_current_request_metadata(token)


def _reset_skill_turbo_workspace_token(ctx: AgentCallbackContext) -> None:
    """Restore effective_request_workspace_dir ContextVar binding for this tool call."""
    token = ctx.extra.pop(_SKILL_TURBO_WORKSPACE_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            reset_effective_request_workspace_dir,
        )
        reset_effective_request_workspace_dir(token)


def _reset_skill_turbo_interactive_ask_token(ctx: AgentCallbackContext) -> None:
    """Restore interactive_ask ContextVar binding for this tool call."""
    token = ctx.extra.pop(_SKILL_TURBO_INTERACTIVE_ASK_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            reset_interactive_ask,
        )
        reset_interactive_ask(token)


def _reset_skill_turbo_resume_answers_token(ctx: AgentCallbackContext) -> None:
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        return
    token = extra.pop(_SKILL_TURBO_RESUME_ANSWERS_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            reset_skill_turbo_resume_answers,
        )
        reset_skill_turbo_resume_answers(token)


def _reset_skill_turbo_outer_todo_token(ctx: AgentCallbackContext) -> None:
    """Restore the display-ownership binding for this tool call."""
    token = ctx.extra.pop(_SKILL_TURBO_OUTER_TODO_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
            reset_skill_turbo_outer_todo_active,
        )
        reset_skill_turbo_outer_todo_active(token)


def _bind_skill_turbo_outer_todo_token(ctx: AgentCallbackContext) -> None:
    """Rebind outer-todo display ownership into the tool context."""
    active = ctx.extra.get(SKILL_TURBO_OUTER_TODO_ACTIVE_EXTRA_KEY)
    if not isinstance(active, bool):
        return
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_skill_turbo_outer_todo_active,
    )
    ctx.extra[_SKILL_TURBO_OUTER_TODO_TOKEN_EXTRA_KEY] = (
        set_skill_turbo_outer_todo_active(active)
    )


def _reset_subagent_parent_session_token(ctx: AgentCallbackContext) -> None:
    """Restore subagent parent session ContextVar binding for this tool call."""
    token = ctx.extra.pop(_SUBAGENT_PARENT_SESSION_TOKEN_EXTRA_KEY, None)
    if token is not None:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            reset_subagent_parent_session,
        )
        reset_subagent_parent_session(token)


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
        # Tenant-scoped checkpointer for early checkpoint (prefer over Factory default).
        self._checkpointer: Optional[Any] = None
        self._skill_turbo_adapter: Any | None = None
        # 当前请求的 metadata（由 adapter 在 _apply_runtime_config_stages 注入）。
        # metadata 的 ContextVar 在请求任务中设置，但工具在 harness 执行任务里运行，
        # ContextVar 不跨任务传播，故经由本属性在 before_tool_call（工具执行上下文）转绑。
        self._skill_turbo_request_metadata: Optional[dict[str, Any]] = None
        # Per-request openjiuwen CwdState paths (cwd / project_root / workspace).
        # Seeded by the adapter in the request task; rebound here because the
        # interaction supervisor / round task does not inherit that ContextVar.
        self._runtime_cwd: Optional[str] = None
        self._runtime_project_root: Optional[str] = None
        self._runtime_workspace: Optional[str] = None

    def set_checkpointer(self, checkpointer: Optional[Any]) -> None:
        """Bind tenant-scoped checkpointer for early checkpoint saves."""
        self._checkpointer = checkpointer

    def set_skill_turbo_adapter(self, adapter: Any) -> None:
        """注入 SkillTurbo adapter，用于 HITL 中断/恢复桥接。"""
        self._skill_turbo_adapter = adapter

    def set_skill_turbo_request_metadata(self, metadata: Optional[dict]) -> None:
        """注入当前请求 metadata，供 skill_turbo 工具在工具执行上下文中读取。"""
        self._skill_turbo_request_metadata = (
            dict(metadata) if isinstance(metadata, dict) else None
        )

    def set_runtime_cwd_paths(
        self,
        *,
        cwd: str | None = None,
        project_root: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """Store per-request CWD layers for rebind in the interaction task.

        ``write_file`` / ``bash`` / ``glob`` / etc. resolve relative paths via
        ``get_cwd()`` / ``get_workspace()``. Those ContextVars are seeded in the
        request task but tools run under the DeepAgent supervisor round task,
        so this rail must re-apply the paths at invoke / tool boundaries.
        """
        def _norm(value: str | None) -> str | None:
            if not isinstance(value, str):
                return None
            stripped = value.strip()
            return stripped or None

        self._runtime_cwd = _norm(cwd)
        self._runtime_project_root = _norm(project_root) or self._runtime_cwd
        self._runtime_workspace = _norm(workspace) or self._runtime_cwd
        if self._runtime_cwd:
            logger.info(
                "[StreamEventRail] runtime cwd paths stored cwd=%s project_root=%s "
                "workspace=%s",
                self._runtime_cwd,
                self._runtime_project_root,
                self._runtime_workspace,
            )

    def _resolve_runtime_cwd_paths(self) -> tuple[str, str, str] | None:
        """Resolve cwd / project_root / workspace for the current request."""
        cwd = self._runtime_cwd
        project_root = self._runtime_project_root
        workspace = self._runtime_workspace
        if not cwd and isinstance(self._skill_turbo_request_metadata, dict):
            epd = self._skill_turbo_request_metadata.get("effective_project_dir")
            if isinstance(epd, str) and epd.strip():
                cwd = project_root = workspace = epd.strip()
        if not cwd:
            return None
        return (
            cwd,
            project_root or cwd,
            workspace or cwd,
        )

    def _rebind_runtime_cwd(self, *, replace: bool) -> None:
        """Apply stored runtime paths onto openjiuwen CwdState in this task.

        Args:
            replace: When True, ``init_cwd`` installs a fresh CwdState (use in
                ``before_invoke`` before tool ``asyncio.gather`` copies the
                ContextVar reference). When False, mutate the shared CwdState
                via ``set_cwd`` / ``set_project_root`` / ``set_workspace`` so
                gather siblings already holding the reference see the update.
        """
        paths = self._resolve_runtime_cwd_paths()
        if paths is None:
            return
        cwd, project_root, workspace = paths
        try:
            if replace:
                from openjiuwen.core.sys_operation.cwd import init_cwd

                init_cwd(cwd, project_root=project_root, workspace=workspace)
            else:
                from openjiuwen.core.sys_operation.cwd import (
                    set_cwd,
                    set_project_root,
                    set_workspace,
                )

                set_cwd(cwd)
                set_project_root(project_root)
                set_workspace(workspace)
            logger.debug(
                "[StreamEventRail] rebound runtime cwd replace=%s cwd=%s",
                replace,
                cwd,
            )
        except Exception:
            logger.warning(
                "[StreamEventRail] rebind runtime cwd failed replace=%s cwd=%s",
                replace,
                cwd,
                exc_info=True,
            )

    # Agent-internal subtrees that must stay under the agent workspace even when
    # a per-request project_dir is bound (todos, context offload, memory, …).
    _AGENT_INTERNAL_PATH_PREFIXES = frozenset(
        {
            "todo",
            "context",
            "skills",
            "memory",
            "sub_agents",
            ".agent_history",
            ".checkpoint",
            ".workspace",
        }
    )

    def _rebase_path_from_agent_workspace(self, file_path: str) -> str | None:
        """Rebase absolute agent-workspace user paths onto the request project_dir.

        OfficeClaw models often emit absolute paths under ``agent_default`` when
        the prompt lacked project_dir context. Relative resolution alone cannot
        fix those; rewrite only non-internal artifact paths.
        """
        if not isinstance(file_path, str) or not file_path.strip():
            return None
        expanded = os.path.expanduser(file_path.strip())
        if not (os.path.isabs(expanded) or expanded.startswith("\\\\") or expanded.startswith("//")):
            return None
        paths = self._resolve_runtime_cwd_paths()
        if paths is None:
            return None
        runtime_cwd, _, _ = paths
        try:
            from jiuwenswarm.common.utils import get_agent_workspace_dir
        except Exception:
            return None
        try:
            agent_ws = os.path.abspath(str(get_agent_workspace_dir()))
            abs_path = os.path.abspath(expanded)
            runtime_cwd_abs = os.path.abspath(runtime_cwd)
        except (OSError, TypeError, ValueError):
            return None
        if os.path.normcase(agent_ws) == os.path.normcase(runtime_cwd_abs):
            return None
        try:
            rel = os.path.relpath(abs_path, agent_ws)
        except ValueError:
            return None
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        first = rel.replace("\\", "/").split("/", 1)[0].lower()
        if first in self._AGENT_INTERNAL_PATH_PREFIXES or first.startswith("."):
            return None
        rebased = os.path.abspath(os.path.join(runtime_cwd_abs, rel))
        if os.path.normcase(rebased) == os.path.normcase(abs_path):
            return None
        return rebased

    def _rebase_user_artifact_path_args(self, tool_call: Any, tool_name: str) -> None:
        """Rewrite write/edit file_path when model hardcodes agent workspace."""
        args = getattr(tool_call, "arguments", None)
        if not isinstance(args, dict):
            return
        key = "file_path" if "file_path" in args else ("path" if "path" in args else None)
        if key is None:
            return
        original = args.get(key)
        rebased = self._rebase_path_from_agent_workspace(original) if isinstance(original, str) else None
        if not rebased:
            return
        try:
            new_args = dict(args)
            new_args[key] = rebased
            tool_call.arguments = new_args
            logger.info(
                "[StreamEventRail] rebased %s path from agent workspace: %s -> %s",
                tool_name,
                original,
                rebased,
            )
        except (AttributeError, TypeError) as exc:
            logger.warning(
                "[StreamEventRail] failed to rebase %s path: %s",
                tool_name,
                exc,
            )

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

        # Install request CwdState before tool gather copies the ContextVar ref.
        # Without this, relative write_file/bash/glob resolve against the default
        # agent workspace instead of relay/project_dir.
        self._rebind_runtime_cwd(replace=True)

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

        # SkillTurbo: ensure adapter token is reset after invoke
        if self._skill_turbo_adapter is not None:
            try:
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    clear_current_skill_turbo_adapter,
                )
                # 兜底清理：token 已丢失或需无条件清空时用 None 覆盖
                clear_current_skill_turbo_adapter()
            except Exception:
                logger.debug(
                    "[StreamEventRail] clear skill_turbo adapter failed after invoke",
                    exc_info=True,
                )

        # SkillTurbo: 确保请求级 ContextVar token 在 invoke 后还原
        # （与 after_tool_call / on_model_exception 对称，幂等：已 reset 则 pop 得 None 跳过）
        _reset_skill_turbo_metadata_token(ctx)
        _reset_skill_turbo_workspace_token(ctx)
        _reset_skill_turbo_interactive_ask_token(ctx)
        _reset_skill_turbo_resume_answers_token(ctx)
        _reset_skill_turbo_outer_todo_token(ctx)
        _reset_subagent_parent_session_token(ctx)

    # ------------------------------------------------------------------
    # before_model_call: pause check + context fix + compression info
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        sid = self._resolve_sid(ctx, ctx.session)
        await self._get_pause_event(sid).wait()
        if self._abort_requested.get(sid, False):
            raise asyncio.CancelledError("Agent abort requested")

        # Some task-loop paths reach model call without before_invoke; keep cwd
        # aligned for any rail/tool that reads get_cwd during the model turn.
        self._rebind_runtime_cwd(replace=False)

        self._inject_tool_call_goal_schema(ctx)
        self._ensure_tool_call_goal_prompt()

        if ctx.context is not None:
            if not self._read_image_multimodal_enabled():
                strip_image_content_from_model_context(ctx.context)
            await self._fix_incomplete_tool_context(ctx)

        await self._maybe_early_checkpoint(ctx)

    async def _maybe_early_checkpoint(self, ctx: AgentCallbackContext) -> None:
        """Persist context + agent state once per invoke before the first LLM call.

        Mitigates losing the user message if the process dies before ``post_run``:
        ``save_contexts`` then ``post_agent_execute`` (not ``post_run``, which closes
        the stream). Skipped on later ReAct iterations via ``ctx.extra`` flag.
        """
        if _early_checkpoint_disabled_by_env():
            return
        if ctx.extra.get(_EARLY_CHECKPOINT_EXTRA_KEY):
            return
        sid = self._resolve_sid(ctx, ctx.session)
        cid = (self._conversation_ids.get(sid, "") or "").strip()
        if cid.startswith("heartbeat"):
            return
        session = getattr(ctx, "session", None)
        agent = getattr(ctx, "agent", None)
        if session is None or agent is None:
            return
        context_engine = getattr(agent, "context_engine", None)
        if context_engine is None:
            return

        actual_session = getattr(session, "_parent", session) if session else None
        if actual_session is None:
            return

        try:
            await context_engine.save_contexts(actual_session)
            inner = getattr(actual_session, "_inner", actual_session)
            cp = (
                self._checkpointer
                if self._checkpointer is not None
                else CheckpointerFactory.get_checkpointer()
            )
            await cp.post_agent_execute(inner)
            ctx.extra[_EARLY_CHECKPOINT_EXTRA_KEY] = True
            session_id = ""
            gs = getattr(actual_session, "get_session_id", None)
            if callable(gs):
                session_id = str(gs())
            else:
                fn = getattr(actual_session, "session_id", None)
                if callable(fn):
                    session_id = str(fn())
            logger.debug(
                "[StreamEventRail] early checkpoint saved session_id=%s",
                session_id or "",
            )
        except Exception as exc:
            logger.warning(
                "[StreamEventRail] early checkpoint failed: %s",
                exc,
                exc_info=True,
            )

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

    def _ensure_tool_call_goal_prompt(self) -> None:
        builder = getattr(self._deep_agent, "system_prompt_builder", None)
        if builder is None:
            return
        try:
            from openjiuwen.harness.prompts import PromptSection
            cn_text = (
                "# 工具 call_goal\n\n"
                "每次调用工具时，请填写参数 `call_goal`：用一句简短中文说明"
                "这次调用要达成的目标（如「调研 openJiuwen 官网信息」「创建三子棋对战团队」），"
                "不要只写工具名或裸 URL。"
                "该字段仅用于界面展示，不影响工具实际执行。\n"
                "若工具参数里已有 `description`：`call_goal` 必须与 `description` 使用同一句，"
                "禁止再写一句近义复述（避免同一信息输出两遍）。\n"
                "团队工具也必须填 `call_goal`，且不能用其它字段代替：\n"
                "- `spawn_member` / `spawn_teammate`：`call_goal` 写「为何创建该成员」；"
                "`display_name` 仍是成员展示名，两者都要填。\n"
                "- `send_message`：`call_goal` 写「这次消息的目的」；"
                "`summary` 可继续填，但不能省略 `call_goal`。\n"
                "- `build_team`：`call_goal` 写建队目标；`display_name` 仍是团队名。"
            )
            en_text = (
                "# Tool call_goal\n\n"
                "When calling any tool, set `call_goal`: one short phrase for the goal of this call "
                "(e.g. \"Research openJiuwen official site\", \"Create tic-tac-toe team\"). "
                "Do not just repeat the tool name or raw URL. UI only; does not affect execution.\n"
                "If the tool already has a `description` parameter: set `call_goal` to the exact same "
                "string — do not invent a second near-duplicate phrase.\n"
                "Team tools must also set `call_goal`; do not substitute other fields:\n"
                "- `spawn_member` / `spawn_teammate`: `call_goal` = why spawn this member; "
                "`display_name` remains the member label — fill both.\n"
                "- `send_message`: `call_goal` = purpose of this message; "
                "`summary` may still be set, but `call_goal` is required too.\n"
                "- `build_team`: `call_goal` = team goal; `display_name` remains the team name."
            )
            builder.add_section(
                PromptSection(
                    name="tool_call_goal",
                    content={"cn": cn_text, "en": en_text},
                    priority=40,
                )
            )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("[StreamEventRail] inject call_goal prompt failed: %s", exc)

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

        # Mutate shared CwdState (gather children hold the same reference).
        # Covers write_file/edit_file/bash/glob/grep/command_tools/etc.
        self._rebind_runtime_cwd(replace=False)

        session = ctx.session
        if session is not None and isinstance(ctx.inputs, ToolCallInputs):
            tc = ctx.inputs.tool_call
            tool_name = str(
                getattr(ctx.inputs, "tool_name", "") or getattr(tc, "name", "") or ""
            )
            if tool_name in ("write_file", "edit_file"):
                self._rebase_user_artifact_path_args(tc, tool_name)
            if is_read_file_tool(tool_name):
                path = extract_path_from_arguments(getattr(tc, "arguments", {}))
                if path:
                    handle_read_file_before_tool_call(ctx, path)
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
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                extra = {}
            from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
            skip_resume_tool_call = (
                tool_name == "skill_acceleration_exec"
                and extra.get(RESUME_USER_INPUT_KEY) is not None
            )
            if not skip_resume_tool_call:
                await self._emit_tool_call(session, tc, model_display_name=model_display)
                if not ctx.extra.get("_skip_tool"):
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

        # SkillTurbo adapter ContextVar 绑定
        if self._skill_turbo_adapter is not None:
            try:
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    set_current_skill_turbo_adapter,
                )
                token = set_current_skill_turbo_adapter(self._skill_turbo_adapter)
                if not hasattr(ctx, 'extra'):
                    ctx.extra = {}
                ctx.extra[_SKILL_TURBO_ADAPTER_TOKEN_EXTRA_KEY] = token
            except Exception:
                logger.debug(
                    "[StreamEventRail] bind skill_turbo adapter token failed",
                    exc_info=True,
                )

        _bind_skill_turbo_outer_todo_token(ctx)

        # SkillTurbo request metadata ContextVar 转绑：
        # 请求任务里 set_current_request_metadata 的绑定无法传播到本工具执行上下文，
        # 这里用 rail 上保存的副本重新绑定，供 skill_turbo 工具读取 session_id 等。
        if self._skill_turbo_request_metadata is not None:
            if not hasattr(ctx, 'extra'):
                ctx.extra = {}
            from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                set_current_request_metadata,
            )
            meta_token = set_current_request_metadata(self._skill_turbo_request_metadata)
            ctx.extra[_SKILL_TURBO_METADATA_TOKEN_EXTRA_KEY] = meta_token

        # SkillTurbo effective_project_dir / interactive_ask ContextVar 转绑：
        # 与 metadata 同机制，从 rail 保存的副本中提取并在工具执行上下文重新绑定，
        # 供 skill_turbo 工具读取 effective_project_dir、rail 判定非引导模式跳过。
        if isinstance(self._skill_turbo_request_metadata, dict):
            if not hasattr(ctx, 'extra'):
                ctx.extra = {}
            _md = self._skill_turbo_request_metadata
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                set_effective_request_workspace_dir,
                set_interactive_ask,
            )
            _epd = extract_effective_project_dir(_md)
            if _epd is not None:
                ws_token = set_effective_request_workspace_dir(_epd)
                ctx.extra[_SKILL_TURBO_WORKSPACE_TOKEN_EXTRA_KEY] = ws_token
            _ia = _md.get("interactive_ask")
            if _ia is not None:
                ia_token = set_interactive_ask(bool(_ia))
                ctx.extra[_SKILL_TURBO_INTERACTIVE_ASK_TOKEN_EXTRA_KEY] = ia_token

        # Parent session for subagent / SkillTurbo event forwarding: tools such as
        # skill_acceleration_exec read get_subagent_parent_session() and write_stream
        # internal chunks back to the DeepAgent main session for frontend + history.
        parent_bind_session = session
        if parent_bind_session is not None:
            if not hasattr(ctx, "extra"):
                ctx.extra = {}
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                set_subagent_parent_session,
            )

            actual_session = getattr(parent_bind_session, "_parent", parent_bind_session)
            parent_token = set_subagent_parent_session(actual_session)
            ctx.extra[_SUBAGENT_PARENT_SESSION_TOKEN_EXTRA_KEY] = parent_token

        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
            resume_answers = extra.get(RESUME_USER_INPUT_KEY)
            if resume_answers is not None:
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    set_skill_turbo_resume_answers,
                )
                extra[_SKILL_TURBO_RESUME_ANSWERS_TOKEN_EXTRA_KEY] = (
                    set_skill_turbo_resume_answers(resume_answers)
                )

    # ------------------------------------------------------------------
    # after_tool_call: emit tool_result + todo.updated
    # ------------------------------------------------------------------

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        _reset_subagent_parent_session_token(ctx)
        _reset_skill_turbo_resume_answers_token(ctx)
        _reset_skill_turbo_outer_todo_token(ctx)

        session = ctx.session
        if session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return

        tc = ctx.inputs.tool_call
        tc_id = getattr(tc, "id", "")
        self._symphony_stream_handler.reset_progress(ctx)
        # Remove from in-flight tracking on completion
        if tc_id:
            self._inflight_tool_calls.pop(tc_id, None)

        # SkillTurbo HITL: skill_turbo_tools 在 ContextVar 存了 ToolInterruptException，
        # 此处改写 ctx.inputs.tool_result 为 TIE，使 harness 原生 HITL 机制检测并暂停。
        if self._skill_turbo_adapter is not None:
            _reset_skill_turbo_adapter_token(ctx)
            _reset_skill_turbo_metadata_token(ctx)
            _reset_skill_turbo_workspace_token(ctx)
            _reset_skill_turbo_interactive_ask_token(ctx)
            # Already reset at after_tool_call entry for the session-is-None
            # early return; this pop is defensive if that path was skipped.
            _reset_skill_turbo_resume_answers_token(ctx)
            try:
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    get_skill_turbo_hitl_tic,
                    set_skill_turbo_hitl_tic,
                )
                _skill_turbo_tic = get_skill_turbo_hitl_tic()
                if _skill_turbo_tic is not None:
                    set_skill_turbo_hitl_tic(None)
                    if isinstance(ctx.inputs, ToolCallInputs):
                        from openjiuwen.core.single_agent.interrupt.exception import (
                            ToolInterruptException,
                        )
                        new_tic = ToolInterruptException(
                            request=_skill_turbo_tic.request,
                            tool_call=ctx.inputs.tool_call,
                        )
                        ctx.inputs.tool_result = new_tic
                        ctx.inputs.tool_msg = ToolMessage(
                            content=self._tool_interrupted_message(
                                ctx.inputs.tool_name or "skill_acceleration_exec"
                            ),
                            tool_call_id=ctx.inputs.tool_call.id,
                        )
                    logger.info(
                        "[StreamEventRail] SkillTurbo HITL: rewrote tool_result to TIE. "
                        "original_tcid=%s harness_tcid=%s",
                        _skill_turbo_tic.tool_call.id if _skill_turbo_tic.tool_call else "?",
                        ctx.inputs.tool_call.id if isinstance(ctx.inputs, ToolCallInputs) else "?",
                    )
                    # 卡片由 harness __interaction__ 转换统一发出（外层
                    # tool_call_id，harness 恢复按同一 id 对齐）；此处不再主动
                    # emit，避免同一次中断发出两张 ask_user 卡片。
                    return  # 跳过 _emit_tool_result：中断态无结果可发
            except Exception:
                logger.debug(
                    "[StreamEventRail] skill_turbo HITL rewrite failed",
                    exc_info=True,
                )

        if (
            str(getattr(tc, "name", "") or "").strip() == "deepresearch_execute"
            and _extract_tool_interrupt(ctx.inputs.tool_result) is not None
        ):
            return

        normalize_read_file_tool_outcome(ctx)
        # A call suspended for user input (approval card, ask_user) has no
        # result yet: ToolCallResilienceRail only left a failure placeholder
        # on ctx.inputs.tool_result.  Emitting it would show the tool as
        # failed before the user has answered; the resumed call emits the
        # real result.
        if _extract_tool_interrupt(ctx.exception) is None:
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
        if tool_name in _TODO_TOOL_NAMES:
            # Prefer conversation_id (main session key for todo.json). Fall back
            # to resolved sid so perf/todo.updated still fire if mapping is late.
            todo_sid = conv_id or sid
            if todo_sid:
                # Emit the main-agent todo snapshot after every todo tool call.
                # The todo tool itself is loaded from the main workspace below, so
                # this stays authoritative even when a resumed/supplement turn uses
                # a different stream session object.
                await self._emit_todo_updated(session, todo_sid)

    # ------------------------------------------------------------------
    # on_model_exception: attempt context repair
    # ------------------------------------------------------------------

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        # Clear context on exception（四个 token 全清，避免异常时 ContextVar 泄漏）
        _reset_skill_turbo_adapter_token(ctx)
        _reset_skill_turbo_metadata_token(ctx)
        _reset_skill_turbo_workspace_token(ctx)
        _reset_skill_turbo_interactive_ask_token(ctx)
        _reset_skill_turbo_resume_answers_token(ctx)
        _reset_skill_turbo_outer_todo_token(ctx)
        _reset_subagent_parent_session_token(ctx)
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
            source_skill = _resolve_source_skill(session)
            if source_skill:
                tool_call_payload["source_skill"] = source_skill
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
                "result": _format_tool_result_for_stream(result),
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
            source_skill = _resolve_source_skill(session)
            if source_skill:
                tool_result_payload["source_skill"] = source_skill
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
            update_payload: dict[str, Any] = {
                "tool_name": getattr(tool_call, "name", "") if tool_call else "",
                "tool_call_id": getattr(tool_call, "id", "") if tool_call else "",
                "arguments": getattr(tool_call, "arguments", {}) if tool_call else {},
                "status": str(status or "").strip() or "in_progress",
            }
            source_skill = _resolve_source_skill(session)
            if source_skill:
                update_payload["source_skill"] = source_skill
            await session.write_stream(
                OutputSchema(
                    type="tool_update",
                    index=0,
                    payload={
                        "tool_update": update_payload,
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

        # skip 窗口内（prepare hook 清理了跨请求残留 todo）过滤掉同一批旧 id：
        # todo.updated 是全量快照旁路，不过滤会把旧任务的 completed 条目重新
        # 弹回前端（task.update 通道的 _stale_todo_ids 过滤管不到这条旁路）。
        try:
            stale_ids = get_stale_todo_ids(session)
        except Exception:
            stale_ids = set()
        if stale_ids:
            # 仅过滤 stale 集中仍处于终态（cancelled/completed）的旧残留项。
            # 本轮 LLM 通过 todo_create/todo_modify 重建的同 ID 项状态为
            # pending/in_progress，不会被过滤。
            # 额外排除本轮新建的同 ID 项：若 id 不在磁盘快照（pre_invoke_todo_ids）
            # 中，说明是本轮 LLM 新建的，即使 id 与 stale 集合重合也不应过滤。
            pre_invoke_ids = get_pre_invoke_todo_ids(session)
            _DONE_STATUSES = frozenset({"cancelled", "completed"})  # pylint: disable=huawei-invalid-name
            before = len(todos_data)
            todos_data = [
                t for t in todos_data
                if not (  # pylint: disable=complicate-comprehension
                    str(getattr(t, "id", "")) in stale_ids  # pylint: disable=complicate-comprehension
                    and str(getattr(t, "status", "")).lower() in _DONE_STATUSES
                    and (not pre_invoke_ids or str(getattr(t, "id", "")) in pre_invoke_ids)
                )
            ]
            logger.info(
                "[StreamEventRail] todo.updated filtered stale todos: "
                "session_id=%s stale_ids=%d before=%d after=%d",
                session_id,
                len(stale_ids),
                before,
                len(todos_data),
            )

        # Parent StreamEventRail only: team-member rails use their own
        # workspace and must not feed request_summaries.tasks.
        if not self._member_name:
            from jiuwenswarm.perf.guard import run_perf_safe
            from jiuwenswarm.perf.todo_tracker import (
                sync_main_agent_todos,
                todos_from_items,
            )

            run_perf_safe(
                "StreamEventRail",
                "perf sync main-agent todos",
                lambda: sync_main_agent_todos(
                    todos_from_items(todos_data),
                    session_id=session_id,
                ),
            )

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

        Maps internal TodoStatus values to frontend-compatible status strings.
        Cancelled items are omitted because the frontend todo panel tracks
        actionable or completed tasks only.

        Args:
            todos_data: List of TodoItem objects from TodoListTool.

        Returns:
            List of formatted todo dictionaries.
        """
        status_mapping = {
            TodoStatus.PENDING: "pending",
            TodoStatus.IN_PROGRESS: "in_progress",
            TodoStatus.COMPLETED: "completed",
        }

        return [
            {
                "id": item.id,
                "content": item.content,
                "activeForm": item.activeForm,
                "status": status_mapping.get(item.status, item.status.value),
            }
            for item in todos_data
            if item.status != TodoStatus.CANCELLED
        ]

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
            repaired = fix_json_arguments(arguments)
            if isinstance(repaired, dict):
                return json.dumps(repaired, ensure_ascii=False)
            logger.warning("Illegal Tool call arguments after repair: %s", arguments)
            return "{}"
        return "{}"

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
