# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native HITL bridge for the high-level DeepResearch execution tool."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRequest
from openjiuwen.harness.workspace.workspace import WorkspaceNode

from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    EXECUTION_SCHEMA,
    bind_deepresearch_execution_context,
    reset_deepresearch_execution_context,
)
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    get_current_task_id,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    deepresearch_todo_path,
)
from jiuwenswarm.common.schema.ask_user import ask_user_response_schema
from jiuwenswarm.common.utils import logger
from jiuwenswarm.perf.context import (
    extract_session_id_from_callback,
    get_request_context,
)

DEEPRESEARCH_EXECUTION_STATE_KEY = "deepresearch_execution_states"
DEEPRESEARCH_EXECUTION_ALIAS_KEY = "deepresearch_execution_aliases"
_TOKENS_KEY = "__deepresearch_execution_context_tokens__"
_TOOL_NAME = "deepresearch_execute"
_TODO_DONE = frozenset({"completed", "cancelled"})
_CURRENT_RESEARCH_TODO_IDS = frozenset({"deepresearch", "todo:deepresearch"})
_FOLLOWUP_HANDOFF_BODY = (
    "深度研究工具已成功结束。完整研究报告已通过文件交付，无需再向用户确认研究方向。"
)


def _todo_item_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("task_id") or "").strip()


def _normalize_todo_id(task_id: str) -> str:
    return str(task_id or "").strip().removeprefix("todo:").strip()


def _todo_status(item: Mapping[str, Any]) -> str:
    return str(item.get("status") or "pending").strip().lower()


def _active_todo_id() -> str:
    """Outer todo bound by TaskExecutionRail while this tool call runs."""
    try:
        return _normalize_todo_id(str(get_current_task_id() or ""))
    except (LookupError, RuntimeError, TypeError, ValueError):
        return ""


def _looks_like_research_todo(task_id: str) -> bool:
    normalized = _normalize_todo_id(task_id)
    if not normalized:
        return False
    if normalized in _CURRENT_RESEARCH_TODO_IDS or task_id in _CURRENT_RESEARCH_TODO_IDS:
        return True
    if normalized.startswith("deepresearch_stage_"):
        return True
    return normalized.endswith(":deepresearch")


def _looks_like_research_todo_loose(task_id: str) -> bool:
    """Model-chosen ids: deep_research / deep-research / research_banks / 深度研究."""
    normalized = _normalize_todo_id(task_id)
    compact = normalized.lower().replace("_", "").replace("-", "").replace(" ", "")
    return bool(compact) and ("research" in compact or "研究" in compact)


def _resolve_research_todo_id(
    items: list[dict[str, Any]],
    active_id: str = "",
) -> str:
    """Pick the single outer todo that represents this DeepResearch run.

    Preference order: strict research id → loose research id (only when exactly
    one todo matches) → the todo TaskExecutionRail bound to the tool call → the
    single in_progress todo.

    Name matching goes before the binding on purpose: the bound todo is
    whatever the model was working on when it called the tool. If the model
    (wrongly) re-runs deepresearch_execute while the PPT todo is active, the
    bound id is the PPT todo and using it would force_finish the whole turn
    with the research summary instead of letting the PPT step run.
    """
    ids = [_todo_item_id(item) for item in items]
    normalized = [_normalize_todo_id(task_id) for task_id in ids]
    for task_id in ids:
        if _looks_like_research_todo(task_id):
            return _normalize_todo_id(task_id)
    loose = [
        _normalize_todo_id(task_id)
        for task_id in ids
        if _looks_like_research_todo_loose(task_id)
    ]
    if len(loose) == 1:
        return loose[0]
    if active_id and active_id in normalized:
        return active_id
    in_progress = [
        _normalize_todo_id(_todo_item_id(item))
        for item in items
        if _todo_status(item) == "in_progress"
    ]
    if len(in_progress) == 1:
        return in_progress[0]
    return ""


def _is_current_research_todo(task_id: str, research_id: str = "") -> bool:
    normalized = _normalize_todo_id(task_id)
    if research_id:
        return normalized == research_id or normalized.startswith("deepresearch_stage_")
    return _looks_like_research_todo(task_id)


def _session_id_from_ctx(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    if session is None:
        return ""
    get_sid = getattr(session, "get_session_id", None)
    if not callable(get_sid):
        return ""
    try:
        return str(get_sid() or "").strip()
    except (TypeError, ValueError, AttributeError):
        return ""


def _agent_todo_json_path(agent: Any, session_id: str) -> Path | None:
    deep_config = getattr(agent, "deep_config", None)
    if deep_config is None:
        nested = getattr(agent, "deep_agent", None)
        deep_config = getattr(nested, "deep_config", None)
    workspace = getattr(deep_config, "workspace", None)
    get_node_path = getattr(workspace, "get_node_path", None)
    if not callable(get_node_path):
        return None
    try:
        return Path(get_node_path(WorkspaceNode.TODO)) / session_id / "todo.json"
    except (TypeError, ValueError, OSError, AttributeError):
        return None


def _todo_json_path(ctx: AgentCallbackContext) -> Path | None:
    """Resolve outer harness todo.json from agent workspace, then default path."""
    session_id = _session_id_from_ctx(ctx)
    if not session_id:
        return None
    agent = getattr(ctx, "agent", None)
    path = _agent_todo_json_path(agent, session_id)
    if path is not None:
        return path
    try:
        return deepresearch_todo_path(session_id=session_id)
    except ValueError:
        return None


def _load_todo_items(ctx: AgentCallbackContext) -> list[dict[str, Any]]:
    path = _todo_json_path(ctx)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug(
            "[DeepResearchExecutionRail] failed to read todo.json path=%s",
            path,
            exc_info=True,
        )
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save_todo_items(ctx: AgentCallbackContext, items: list[dict[str, Any]]) -> bool:
    path = _todo_json_path(ctx)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        logger.warning(
            "[DeepResearchExecutionRail] failed to write todo.json path=%s",
            path,
            exc_info=True,
        )
        return False


def _has_followup_todos(
    items: list[dict[str, Any]],
    research_id: str = "",
) -> bool:
    """True when the outer plan still has work after DeepResearch returns."""
    for item in items:
        if _todo_status(item) in _TODO_DONE:
            continue
        if _is_current_research_todo(_todo_item_id(item), research_id):
            continue
        return True
    return False


def _followup_todo_label(item: Mapping[str, Any] | None) -> str:
    if not item:
        return ""
    task_id = _todo_item_id(item)
    subject = str(item.get("content") or item.get("subject") or "").strip()
    if task_id and subject:
        return f"{task_id}: {subject}"
    return task_id or subject


def _compact_followup_tool_content(
    next_followup: Mapping[str, Any] | None,
    research_id: str = "",
) -> str:
    """Drop the report body and point the model at the next outer todo.

    The full report is already delivered via chat.file. Replaying a research
    summary in the tool message caused the model to re-clarify instead of
    continuing the remaining plan.
    """
    label = _followup_todo_label(next_followup)
    next_id = _todo_item_id(next_followup) if next_followup else ""
    if research_id and next_id:
        todo_step = (
            f"先调用 todo_modify 将「{research_id}」标记为 completed、"
            f"「{next_id}」标记为 in_progress，然后"
        )
    elif next_id:
        todo_step = f"先调用 todo_modify 将「{next_id}」标记为 in_progress，然后"
    else:
        todo_step = ""
    next_step = (
        f"{todo_step}立即执行外层待办「{label}」。"
        if label
        else f"{todo_step}立即执行尚未完成的外层待办。"
    )
    return (
        f"{_FOLLOWUP_HANDOFF_BODY}\n\n"
        "[系统衔接] 深度研究已全部完成（用户澄清与大纲确认均已结束），"
        "研究报告已通过文件交付。"
        f"{next_step}"
        "禁止再次调用 deepresearch_execute，禁止调用 ask_user 重复提问或再次澄清研究方向。"
    )


def _advance_outer_todo_items(
    items: list[dict[str, Any]],
    research_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    changed = False
    next_followup: dict[str, Any] | None = None
    for item in items:
        task_id = _todo_item_id(item)
        status = _todo_status(item)
        if _is_current_research_todo(task_id, research_id) and status not in _TODO_DONE:
            item["status"] = "completed"
            changed = True
            continue
        if (
            next_followup is None
            and status not in _TODO_DONE
            and not _is_current_research_todo(task_id, research_id)
        ):
            next_followup = item

    if next_followup is not None:
        if _todo_status(next_followup) != "in_progress":
            next_followup["status"] = "in_progress"
            changed = True
    return items, next_followup, changed


def _rewrite_followup_tool_result(
    ctx: AgentCallbackContext,
    payload: dict[str, Any],
    handoff_content: str,
) -> None:
    payload["content"] = handoff_content
    tool_result = ctx.inputs.tool_result
    detailed = getattr(tool_result, "detailed_output", None)
    if isinstance(detailed, dict):
        detailed["content"] = handoff_content
    elif isinstance(tool_result, dict):
        tool_result["content"] = handoff_content
    else:
        ctx.inputs.tool_result = dict(payload)

    tool_msg = getattr(ctx.inputs, "tool_msg", None)
    if tool_msg is None or not hasattr(tool_msg, "content"):
        return
    try:
        tool_msg.content = handoff_content
    except (AttributeError, TypeError, ValueError):
        logger.debug(
            "[DeepResearchExecutionRail] failed to rewrite tool_msg content",
            exc_info=True,
        )


def _apply_followup_handoff(
    ctx: AgentCallbackContext,
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    research_id: str = "",
) -> None:
    """Mark research done, advance the next outer todo, compact the tool result."""
    if not items:
        return

    items, next_followup, changed = _advance_outer_todo_items(items, research_id)
    if changed:
        _save_todo_items(ctx, items)

    content = str(payload.get("content") or "").rstrip()
    if not content:
        return
    handoff_content = _compact_followup_tool_content(next_followup, research_id)
    _rewrite_followup_tool_result(ctx, payload, handoff_content)
    logger.info(
        "[DeepResearchExecutionRail] followup handoff applied; "
        "research_todo=%s next_todo=%s content_chars=%s",
        research_id,
        _todo_item_id(next_followup) if next_followup else "",
        len(handoff_content),
    )


def _tool_name(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(
        ctx.inputs.tool_name
        or getattr(ctx.inputs.tool_call, "name", "")
        or ""
    ).strip()


def _tool_call_id(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(getattr(ctx.inputs.tool_call, "id", "") or "").strip()


def _load_states(session: Any) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): dict(state)
        for key, state in value.items()
        if isinstance(state, Mapping)
    }


def _save_states(session: Any, states: dict[str, dict[str, Any]]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_STATE_KEY: states})


def _load_aliases(session: Any) -> dict[str, str]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_ALIAS_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(workflow_id)
        for key, workflow_id in value.items()
        if str(key).strip() and str(workflow_id).strip()
    }


def _save_aliases(session: Any, aliases: dict[str, str]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: aliases})


def _workflow_id(session: Any, tool_call_id: str) -> str:
    return _load_aliases(session).get(tool_call_id, tool_call_id)


def _interaction_tool_call(tool_call: Any, interaction_id: str) -> Any:
    model_copy = getattr(tool_call, "model_copy", None)
    cloned = model_copy(deep=True) if callable(model_copy) else copy.deepcopy(tool_call)
    cloned.id = interaction_id
    return cloned


def _resume_input(ctx: AgentCallbackContext, tool_call_id: str) -> Any:
    value = getattr(ctx, "extra", {}).get(RESUME_USER_INPUT_KEY)
    if isinstance(value, InteractiveInput):
        return value.user_inputs.get(tool_call_id)
    if isinstance(value, Mapping) and tool_call_id in value:
        return value[tool_call_id]
    return value


def _result_payload(value: Any) -> dict[str, Any] | None:
    detailed_output = getattr(value, "detailed_output", None)
    if isinstance(detailed_output, Mapping):
        value = detailed_output
    return dict(value) if isinstance(value, Mapping) else None


def _interaction_placeholder(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    phase = str(state.get("phase") or "").strip() if isinstance(state, Mapping) else ""
    stage = "大纲确认" if "outline" in phase.lower() else "研究方向澄清"
    return (
        f"[系统衔接] deepresearch_execute 正在等待用户通过原生交互卡完成「{stage}」。"
        "该交互由系统直接呈现并恢复研究流程，Main Agent 无需转述问题、无需调用 ask_user，"
        "也不要再次调用 deepresearch_execute。"
    )


def _sync_root_tool_message(
    ctx: AgentCallbackContext,
    root_id: str,
    content: str,
) -> bool:
    """Rewrite the ToolMessage paired with the original deepresearch_execute call.

    Interaction resumes execute under alias ids (``<root>_interaction_N``).
    Their ToolMessages never pair with an assistant tool_call, so
    ``LLMStabilityRail.sanitize_tool_pairing`` drops them before every model
    call. The only DeepResearch tool result the model can ever see is the one
    attached to the root call, which the framework filled with whatever the
    first execution returned (``ability_manager`` falls back to the raw
    tool_msg when a rail clears ``tool_msg``). Without this sync the model
    keeps seeing the stale first interaction envelope after the research has
    completed, re-presents the clarification questions via ask_user and
    starts a second DeepResearch run.
    """
    context = getattr(ctx, "context", None)
    get_messages = getattr(context, "get_messages", None)
    if not root_id or not callable(get_messages):
        return False
    try:
        messages = get_messages()
    except Exception:  # noqa: BLE001 - never break the tool loop on context access
        logger.debug(
            "[DeepResearchExecutionRail] failed to read model context messages",
            exc_info=True,
        )
        return False
    updated = False
    for message in messages or []:
        if (
            isinstance(message, ToolMessage)
            and str(getattr(message, "tool_call_id", "") or "") == root_id
        ):
            message.content = content
            updated = True
    if updated:
        logger.info(
            "[DeepResearchExecutionRail] synced root tool message tool_call_id=%s "
            "content_chars=%s",
            root_id,
            len(content),
        )
    return updated


def _set_tool_message(ctx: AgentCallbackContext, tool_call_id: str, content: str) -> None:
    tool_msg = getattr(ctx.inputs, "tool_msg", None)
    if isinstance(tool_msg, ToolMessage):
        tool_msg.content = content
        return
    ctx.inputs.tool_msg = ToolMessage(content=content, tool_call_id=tool_call_id)


def _handle_interaction_payload(
    ctx: AgentCallbackContext,
    payload: dict[str, Any],
    workflow_id: str,
    tool_call_id: str = "",
) -> bool:
    if payload.get("kind") != "interaction":
        return False
    interaction = payload.get("interaction")
    if not isinstance(interaction, Mapping):
        return True
    questions = interaction.get("questions")
    if not isinstance(questions, list):
        return True
    state = payload.get("state")
    request = AskUserRequest(
        message=str(interaction.get("query") or ""),
        payload_schema=ask_user_response_schema(),
        questions=questions,
    )
    revision = int(state.get("revision") or 0) if isinstance(state, Mapping) else 0
    interaction_id = f"{workflow_id}_interaction_{revision}"
    aliases = _load_aliases(ctx.session)
    aliases[interaction_id] = workflow_id
    _save_aliases(ctx.session, aliases)
    ctx.inputs.tool_result = ToolInterruptException(
        request=request,
        tool_call=_interaction_tool_call(ctx.inputs.tool_call, interaction_id),
    )
    # Do NOT clear tool_msg: ability_manager falls back to the raw envelope
    # message when a rail sets tool_msg=None, which is how the raw interaction
    # payload used to reach the model context. Replace it with a compact
    # placeholder instead, and keep the root call's message current on resumes.
    placeholder = _interaction_placeholder(payload)
    _set_tool_message(ctx, tool_call_id or workflow_id, placeholder)
    if tool_call_id and tool_call_id != workflow_id:
        _sync_root_tool_message(ctx, workflow_id, placeholder)
    return True


def _handle_terminal_execute_result(
    ctx: AgentCallbackContext,
    payload: dict[str, Any],
    workflow_id: str = "",
    tool_call_id: str = "",
) -> None:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return
    items = _load_todo_items(ctx)
    if not items:
        logger.info(
            "[DeepResearchExecutionRail] no outer todo items found "
            "(path=%s); force_finish after deepresearch_execute",
            _todo_json_path(ctx),
        )
    research_id = _resolve_research_todo_id(items, _active_todo_id())
    resumed = bool(workflow_id) and bool(tool_call_id) and tool_call_id != workflow_id
    kind = str(payload.get("kind") or "").strip()
    # Only a *completed* research hands over to the remaining outer todos.
    # error / cancelled end the turn right here (SKILL.md: 工具完成或失败后会直接
    # 结束本轮): giving the failure text to the model makes it "retry once", i.e.
    # another 15-20 min DeepResearch run the user never asked for (2026-09-08
    # 14:49 rerun, SDK sub_reporter crash → second research).
    if kind == "completed" and _has_followup_todos(items, research_id):
        _apply_followup_handoff(ctx, payload, items, research_id)
        if resumed:
            _sync_root_tool_message(ctx, workflow_id, str(payload.get("content") or ""))
        logger.info(
            "[DeepResearchExecutionRail] skip force_finish; "
            "outer todos still pending after deepresearch_execute "
            "research_todo=%s",
            research_id,
        )
        return
    if resumed:
        _sync_root_tool_message(ctx, workflow_id, content.strip())
    logger.info(
        "[DeepResearchExecutionRail] force_finish after deepresearch_execute "
        "kind=%s research_todo=%s error_code=%s",
        kind,
        research_id,
        payload.get("error_code") or "",
    )
    ctx.request_force_finish(
        {"output": content.strip(), "result_type": "answer"}
    )


class DeepResearchExecutionRail(DeepAgentRail):
    """Persist the workflow state and turn tool envelopes into native HITL."""

    # Higher priorities run first. Convert the envelope before
    # JiuSwarmStreamEventRail(priority=80), so the stream rail sees the native
    # interrupt and does not leak the internal envelope as a tool result.
    priority = 81

    def __init__(self, *, model_provider):
        super().__init__()
        self._model_provider = model_provider
        self._agent_id = "jiuwenswarm"

    def init(self, agent: Any) -> None:
        card = getattr(agent, "card", None)
        self._agent_id = str(
            getattr(card, "id", "")
            or getattr(card, "name", "")
            or "jiuwenswarm"
        ).strip()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        tool_call_id = _tool_call_id(ctx)
        workflow_id = _workflow_id(ctx.session, tool_call_id)
        states = _load_states(ctx.session)

        def save_state(state: dict[str, Any]) -> None:
            current = _load_states(ctx.session)
            current[workflow_id] = dict(state)
            _save_states(ctx.session, current)

        model = self._model_provider() if callable(self._model_provider) else None
        request_context = get_request_context(
            session_id=extract_session_id_from_callback(ctx)
        )
        request_id = str((request_context or {}).get("request_id") or "").strip()
        token = bind_deepresearch_execution_context(
            tool_call_id=workflow_id,
            state=states.get(workflow_id),
            user_input=_resume_input(ctx, tool_call_id),
            model=model,
            save_state=save_state,
            agent_id=self._agent_id,
            request_id=request_id,
        )
        tokens = ctx.extra.setdefault(_TOKENS_KEY, {})
        tokens[tool_call_id] = token

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        try:
            payload = _result_payload(ctx.inputs.tool_result)
            if payload is None or payload.get("schema_version") != EXECUTION_SCHEMA:
                return
            tool_call_id = _tool_call_id(ctx)
            workflow_id = _workflow_id(ctx.session, tool_call_id)
            state = payload.get("state")
            states = _load_states(ctx.session)
            if isinstance(state, Mapping):
                states[workflow_id] = dict(state)
                _save_states(ctx.session, states)

            if _handle_interaction_payload(ctx, payload, workflow_id, tool_call_id):
                return

            states.pop(workflow_id, None)
            _save_states(ctx.session, states)
            aliases = {
                alias: target
                for alias, target in _load_aliases(ctx.session).items()
                if target != workflow_id
            }
            _save_aliases(ctx.session, aliases)
            _handle_terminal_execute_result(ctx, payload, workflow_id, tool_call_id)
        finally:
            tool_call_id = _tool_call_id(ctx)
            tokens = ctx.extra.get(_TOKENS_KEY, {})
            token = tokens.pop(tool_call_id, None) if isinstance(tokens, dict) else None
            if not tokens:
                ctx.extra.pop(_TOKENS_KEY, None)
            reset_deepresearch_execution_context(token)


__all__ = [
    "DEEPRESEARCH_EXECUTION_ALIAS_KEY",
    "DEEPRESEARCH_EXECUTION_STATE_KEY",
    "DeepResearchExecutionRail",
]
