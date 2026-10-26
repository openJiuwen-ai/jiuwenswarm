# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settle a cancelled permission/confirm HITL so the next turn is a new query.

Team cancel hides the approval card on the client but historically left
``INTERRUPTION_KEY`` in the session. This module writes a legal ReAct
``ToolMessage`` and clears the interrupt, without treating the cancel as an
approval and without re-emitting the same ``tool_call_id``.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
)


logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_LANGUAGE = "cn"
_SKIP_CONFIRM_TOOL_NAMES = frozenset({"ask_user", "skill_acceleration_exec"})


def cancelled_tool_result_content(tool_name: str, *, language: str = "cn") -> str:
    """Return the NativeHarness/ReAct placeholder for a cancelled tool call."""
    name = str(tool_name or "tool").strip() or "tool"
    if str(language or "").strip().lower().startswith("en"):
        return (
            f"[Tool interrupted] Tool {name} was interrupted by the user "
            "and has no result."
        )
    return f"[工具执行被中断] 工具 {name} 执行过程中被用户打断，没有执行结果。"


def is_pure_confirm_payload_interrupt(state: Any) -> bool:
    """Return whether ``state`` is confirm/permission HITL and nothing else.

    Mixed interrupts (ask-user answers, SkillTurbo, unknown schemas) are left
    untouched so cancel cannot drop a questionnaire the next turn still needs.

    Implemented here instead of importing agent-core ``resume_guard``, which
    is not on the published openjiuwen pin used by CI.
    """
    interrupted = getattr(state, "interrupted_tools", None)
    if not isinstance(interrupted, dict) or not interrupted:
        return False

    found_confirm = False
    for entry in interrupted.values():
        tool_name = _tool_call_name(getattr(entry, "tool_call", None))
        if tool_name in _SKIP_CONFIRM_TOOL_NAMES:
            return False
        requests = getattr(entry, "interrupt_requests", None)
        if not isinstance(requests, dict) or not requests:
            return False
        for request in requests.values():
            if not _is_confirm_payload_schema(getattr(request, "payload_schema", None)):
                return False
            found_confirm = True
    return found_confirm


def collect_cancelled_confirm_tool_calls(state: Any) -> list[tuple[str, str]]:
    """Return ``(tool_call_id, tool_name)`` pairs that still need a result."""
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(tool_call: Any, fallback_id: str = "") -> None:
        tool_call_id = _tool_call_id(tool_call) or str(fallback_id or "").strip()
        if not tool_call_id or tool_call_id in seen:
            return
        seen.add(tool_call_id)
        pending.append((tool_call_id, _tool_call_name(tool_call) or "tool"))

    interrupted = getattr(state, "interrupted_tools", None)
    if isinstance(interrupted, dict):
        for outer_id, entry in interrupted.items():
            _add(getattr(entry, "tool_call", None), str(outer_id or ""))

    ai_message = getattr(state, "ai_message", None)
    for tool_call in list(getattr(ai_message, "tool_calls", None) or []):
        _add(tool_call)
    return pending


async def settle_cancelled_confirm_interrupt(
    session: Any,
    *,
    context: Any | None = None,
    context_engine: Any | None = None,
    hitl_handler: Any | None = None,
    language: str = _DEFAULT_CONTEXT_LANGUAGE,
) -> bool:
    """Write cancelled ``ToolMessage``s and clear a pure confirm interrupt.

    Returns True when this session had a confirm interrupt that was settled.
    """
    if session is None:
        return False
    try:
        state = session.get_state(INTERRUPTION_KEY)
    except Exception:
        logger.debug(
            "settle cancelled confirm interrupt: failed to read INTERRUPTION_KEY",
            exc_info=True,
        )
        return False
    if not is_pure_confirm_payload_interrupt(state):
        return False

    pending = collect_cancelled_confirm_tool_calls(state)
    written = await _write_cancelled_tool_results(
        session,
        pending,
        context=context,
        context_engine=context_engine,
        language=language,
    )
    session.update_state({INTERRUPTION_KEY: None})
    if hitl_handler is not None:
        try:
            hitl_handler.clear(session)
        except Exception:
            logger.debug(
                "settle cancelled confirm interrupt: hitl_handler.clear failed",
                exc_info=True,
            )
    await _persist_settled_session(session, context_engine)
    logger.info(
        "settled cancelled confirm interrupt: session_id=%s tools=%s written=%s",
        _session_id(session),
        [item[0] for item in pending],
        written,
    )
    return True


async def settle_live_cancelled_confirm_interrupt(
    team_agent: Any,
    *,
    language: str = _DEFAULT_CONTEXT_LANGUAGE,
) -> bool:
    """Settle from a still-alive TeamAgent / NativeHarness session."""
    if team_agent is None:
        return False
    session, context, context_engine, hitl_handler, resolved_language = (
        resolve_live_confirm_interrupt_resources(team_agent)
    )
    if session is None:
        logger.info(
            "skip live cancelled confirm settle: reason=no_interrupt_session"
        )
        return False
    return await settle_cancelled_confirm_interrupt(
        session,
        context=context,
        context_engine=context_engine,
        hitl_handler=hitl_handler,
        language=resolved_language or language,
    )


async def settle_persisted_cancelled_confirm_interrupt(
    session_id: str,
    *,
    card: Any | None = None,
    language: str = _DEFAULT_CONTEXT_LANGUAGE,
) -> bool:
    """Reload the checkpointed session and settle a leftover confirm interrupt."""
    sid = str(session_id or "").strip()
    if not sid:
        return False

    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from openjiuwen.core.single_agent import create_agent_session
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from jiuwenswarm.server.runtime.agent_adapter.plan_pause_helpers import (
        post_agent_execute_for_session,
    )

    try:
        exists = await CheckpointerFactory.get_checkpointer().session_exists(sid)
    except Exception:
        exists = True
    if exists is False:
        logger.info(
            "skip persisted cancelled confirm settle: session_id=%s reason=session_missing",
            sid,
        )
        return False

    session = create_agent_session(
        session_id=sid,
        card=card or AgentCard(id="jiuwenswarm", name="jiuwenswarm"),
    )
    await session.pre_run(inputs=None)
    try:
        settled = await settle_cancelled_confirm_interrupt(
            session,
            language=language,
        )
        if not settled:
            logger.info(
                "skip persisted cancelled confirm settle: session_id=%s reason=not_confirm",
                sid,
            )
            return False
        await post_agent_execute_for_session(session)
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()
        return True
    finally:
        post_run = getattr(session, "post_run", None)
        if callable(post_run):
            await post_run()


def resolve_live_confirm_interrupt_resources(
    team_agent: Any,
) -> tuple[Any | None, Any | None, Any | None, Any | None, str]:
    """Best-effort live session / context / HITL handles from a TeamAgent."""
    harness = getattr(team_agent, "harness", None)
    native = getattr(harness, "_native", None)
    agent_like = native if native is not None else harness
    session = _live_interrupt_session(harness, agent_like)
    react_agent = getattr(agent_like, "react_agent", None) or getattr(
        agent_like, "_react_agent", None
    )
    context_engine = getattr(agent_like, "context_engine", None)
    if context_engine is None and react_agent is not None:
        context_engine = getattr(react_agent, "context_engine", None)
    hitl_handler = getattr(react_agent, "_hitl_handler", None)
    context = None
    if context_engine is not None and session is not None:
        try:
            context = context_engine.get_context(session_id=session.get_session_id())
        except Exception:
            logger.debug(
                "resolve live confirm interrupt: get_context failed",
                exc_info=True,
            )
    language = _prompt_language_from_agent(agent_like)
    return session, context, context_engine, hitl_handler, language


def prompt_language_from_team_agent(team_agent: Any) -> str:
    """Return the live agent's prompt language, defaulting to Chinese."""
    harness = getattr(team_agent, "harness", None) if team_agent is not None else None
    native = getattr(harness, "_native", None)
    return _prompt_language_from_agent(native if native is not None else harness)


def _live_interrupt_session(harness: Any, agent_like: Any) -> Any | None:
    candidates: list[Any] = []
    getter = getattr(harness, "_interrupt_session", None)
    if callable(getter):
        try:
            session = getter()
        except Exception:
            logger.debug(
                "resolve live confirm interrupt: _interrupt_session failed",
                exc_info=True,
            )
        else:
            if session is not None:
                candidates.append(session)
    if agent_like is not None:
        for attr in ("_session", "loop_session"):
            session = getattr(agent_like, attr, None)
            if session is not None and session not in candidates:
                candidates.append(session)
    for session in candidates:
        try:
            has_interrupt = session.get_state(INTERRUPTION_KEY) is not None
        except Exception:
            logger.debug(
                "resolve live confirm interrupt: get_state failed",
                exc_info=True,
            )
            has_interrupt = False
        if has_interrupt:
            return session
    return candidates[0] if candidates else None


def _prompt_language_from_agent(agent: Any) -> str:
    builder = getattr(agent, "system_prompt_builder", None)
    language = getattr(builder, "language", None)
    if isinstance(language, str) and language.strip():
        return language.strip()
    return _DEFAULT_CONTEXT_LANGUAGE


def _is_confirm_payload_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    return "approved" in properties and "answers" not in properties


def _tool_call_id(tool_call: Any) -> str:
    if tool_call is None:
        return ""
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        nested_id = function.get("id") if isinstance(function, dict) else ""
        return str(tool_call.get("id") or tool_call.get("tool_call_id") or nested_id or "")
    return str(
        getattr(tool_call, "id", "")
        or getattr(tool_call, "tool_call_id", "")
        or ""
    )


def _tool_call_name(tool_call: Any) -> str:
    if tool_call is None:
        return ""
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or tool_call.get("name") or "")
        return str(tool_call.get("name") or "")
    return str(getattr(tool_call, "name", "") or "")


def _is_tool_message(message: Any) -> bool:
    if isinstance(message, ToolMessage):
        return True
    if isinstance(message, dict):
        return str(message.get("role") or "") == "tool"
    return str(getattr(message, "role", "") or "") == "tool"


def _message_tool_call_id(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id") or "")
    return str(getattr(message, "tool_call_id", "") or "")


def _assistant_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, AssistantMessage):
        return list(getattr(message, "tool_calls", None) or [])
    if isinstance(message, dict) and str(message.get("role") or "") == "assistant":
        return list(message.get("tool_calls") or [])
    tool_calls = getattr(message, "tool_calls", None)
    if str(getattr(message, "role", "") or "") == "assistant":
        return list(tool_calls or [])
    return []


def _make_tool_message(tool_call_id: str, tool_name: str, language: str) -> ToolMessage:
    return ToolMessage(
        content=cancelled_tool_result_content(tool_name, language=language),
        tool_call_id=tool_call_id,
    )


def _existing_tool_message_ids(messages: list[Any]) -> set[str]:
    found: set[str] = set()
    for message in messages:
        if not _is_tool_message(message):
            continue
        tool_call_id = _message_tool_call_id(message)
        if tool_call_id:
            found.add(tool_call_id)
    return found


def insert_cancelled_tool_messages(
    messages: list[Any],
    pending: list[tuple[str, str]],
    *,
    language: str = _DEFAULT_CONTEXT_LANGUAGE,
) -> tuple[list[Any], int]:
    """Insert missing cancelled results after the matching assistant tool_calls.

    Unmatched pending ids are left untouched. Never append orphan ``ToolMessage``
    rows onto a transcript that does not contain the corresponding assistant
    call — a session checkpoint may hold several context blobs.
    """
    if not pending:
        return list(messages), 0
    pending_by_id = {tool_call_id: name for tool_call_id, name in pending if tool_call_id}
    missing = {
        tool_call_id: name
        for tool_call_id, name in pending_by_id.items()
        if tool_call_id not in _existing_tool_message_ids(messages)
    }
    if not missing:
        return list(messages), 0

    rebuilt: list[Any] = []
    inserted = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        rebuilt.append(message)
        expected_ids = [
            tool_call_id
            for tool_call_id in (_tool_call_id(tool_call) for tool_call in _assistant_tool_calls(message))
            if tool_call_id
        ]
        if not expected_ids:
            index += 1
            continue
        index += 1
        while index < len(messages) and _is_tool_message(messages[index]):
            rebuilt.append(messages[index])
            index += 1
        have_ids = _existing_tool_message_ids(rebuilt)
        for tool_call_id in expected_ids:
            if tool_call_id not in missing or tool_call_id in have_ids:
                continue
            rebuilt.append(
                _make_tool_message(tool_call_id, missing[tool_call_id], language)
            )
            inserted += 1
            have_ids.add(tool_call_id)
            missing.pop(tool_call_id, None)

    return rebuilt, inserted


async def _write_cancelled_tool_results(
    session: Any,
    pending: list[tuple[str, str]],
    *,
    context: Any | None,
    context_engine: Any | None,
    language: str,
) -> int:
    written = 0
    if context is not None:
        written = await _write_cancelled_tool_results_to_context(
            context,
            pending,
            language=language,
        )
    if written <= 0 or context_engine is None:
        written = max(
            written,
            _write_cancelled_tool_results_to_session_state(
                session,
                pending,
                language=language,
            ),
        )
    return written


async def _write_cancelled_tool_results_to_context(
    context: Any,
    pending: list[tuple[str, str]],
    *,
    language: str,
) -> int:
    getter = getattr(context, "get_messages", None)
    if not callable(getter):
        return 0
    messages = list(getter() or [])
    rebuilt, inserted = insert_cancelled_tool_messages(
        messages,
        pending,
        language=language,
    )
    if inserted <= 0:
        return 0
    setter = getattr(context, "set_messages", None)
    if callable(setter):
        setter(rebuilt, with_history=True)
        return inserted
    adder = getattr(context, "add_messages", None)
    if not callable(adder):
        return 0
    existing_ids = _existing_tool_message_ids(messages)
    to_add = [
        _make_tool_message(tool_call_id, tool_name, language)
        for tool_call_id, tool_name in pending
        if tool_call_id and tool_call_id not in existing_ids
    ]
    if not to_add:
        return 0
    result = adder(to_add)
    if hasattr(result, "__await__"):
        await result
    return len(to_add)


def _write_cancelled_tool_results_to_session_state(
    session: Any,
    pending: list[tuple[str, str]],
    *,
    language: str,
) -> int:
    try:
        context_state = session.get_state("context")
    except Exception:
        return 0
    if not isinstance(context_state, dict) or not context_state:
        return 0

    inserted_total = 0
    changed = False
    for blob in context_state.values():
        if not isinstance(blob, dict):
            continue
        messages = blob.get("messages")
        if not isinstance(messages, list):
            continue
        rebuilt, inserted = insert_cancelled_tool_messages(
            messages,
            pending,
            language=language,
        )
        if inserted <= 0:
            continue
        blob["messages"] = rebuilt
        inserted_total += inserted
        changed = True
    if not changed:
        return 0
    session.update_state({"context": None})
    session.update_state({"context": context_state})
    return inserted_total


async def _persist_settled_session(session: Any, context_engine: Any | None) -> None:
    if context_engine is not None:
        save = getattr(context_engine, "save_contexts", None)
        if callable(save):
            await save(session)
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()


def _session_id(session: Any) -> str:
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    return str(getattr(session, "session_id", "") or "")
