"""Owning tests for the compact root permission context."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    ToolInterruptEntry,
    ToolInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    RunContext,
    ToolCallInputs,
)
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    InterruptResult,
    RejectResult,
)

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
    StructuredAskUserRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_CONTINUATION_METADATA_KEY,
    ASK_USER_RESUME_DTO_KEY,
    apply_ask_user_resume,
    ask_user_continuation,
    build_ask_user_metadata,
    prepare_ask_user_resume,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
    HOST_USER_PROMPT_PREFIX_EN,
    HOST_USER_PROMPT_PREFIX_ZH,
    ROOT_CONTEXT_KEY,
    RootDecisionContext,
    RootIntentTurn,
    RootIntentTurnKind,
    bind_root_decision_context,
    build_root_intent_projection,
    current_root_decision_context,
    extract_permission_user_content,
    put_root_decision_context_in_inputs,
    reset_root_decision_context,
    root_decision_context_from_extra,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import (
    RootContextRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
)


def _context(text: str = "Inspect the report") -> RootDecisionContext:
    return RootDecisionContext(
        "session-1",
        "request-1",
        "web",
        (RootIntentTurn("request-1", RootIntentTurnKind.FRESH, text),),
    )


def _envelope(text: str, *, prefix: str) -> str:
    return prefix + json.dumps(
        {
            "source": "web",
            "timezone": "Asia/Taipei",
            "timestamp": "2026-08-20 12:00:00",
            "preferred_response_language": "zh",
            "content": text,
            "files_updated_by_user": "{}",
            "type": "user input",
            "origin_kind": HOST_USER_ORIGIN_EXTERNAL,
        },
        ensure_ascii=False,
    )


def _structured_ask_tool_call(options: list[dict[str, object]]) -> ToolCall:
    return ToolCall(
        id="ask-1",
        type="function",
        name="ask_user",
        arguments=json.dumps(
            {
                "query": "Choose",
                "questions": [
                    {
                        "question": "Choice",
                        "header": "Choice",
                        "options": options,
                    }
                ],
            }
        ),
    )


_STRICT_OPTION_CASES = (
    ([{"label": "A"}, {"label": "A"}], "unique labels"),
    ([{"label": "A"}, {"label": "Other"}], "reserved"),
    ([{"label": "A", "description": 1}, {"label": "B"}], "be strings"),
    ([{"label": "A", "preview": []}, {"label": "B"}], "be strings"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("options", "_reason"), _STRICT_OPTION_CASES)
async def test_structured_ask_default_contract_preserves_manual_behavior(
    options: list[dict[str, object]],
    _reason: str,
) -> None:
    rail = StructuredAskUserRail()

    decision = await rail.resolve_interrupt(
        SimpleNamespace(),
        _structured_ask_tool_call(options),
        None,
    )

    assert isinstance(decision, InterruptResult)


@pytest.mark.asyncio
@pytest.mark.parametrize(("options", "reason"), _STRICT_OPTION_CASES)
async def test_structured_ask_strict_contract_rejects_ambiguous_initial_request(
    options: list[dict[str, object]],
    reason: str,
) -> None:
    rail = StructuredAskUserRail(strict_continuation_contract=True)

    decision = await rail.resolve_interrupt(
        SimpleNamespace(),
        _structured_ask_tool_call(options),
        None,
    )

    assert isinstance(decision, RejectResult)
    assert reason in str(decision.tool_result)


@pytest.mark.asyncio
async def test_structured_ask_pending_manual_contract_survives_strict_mode_switch() -> None:
    rail = StructuredAskUserRail()
    tool_call = _structured_ask_tool_call([{"label": "A"}, {"label": "A"}])
    pending = await rail.resolve_interrupt(SimpleNamespace(), tool_call, None)
    assert isinstance(pending, InterruptResult)

    state = ToolInterruptionState(
        ai_message=AssistantMessage(tool_calls=[tool_call]),
        iteration=0,
        interrupted_tools={
            tool_call.id: ToolInterruptEntry(
                tool_call=tool_call,
                interrupt_requests={tool_call.id: pending.request},
            )
        },
    )
    session = SimpleNamespace(
        get_state=lambda key: state if key == INTERRUPTION_KEY else None
    )

    rail.set_strict_continuation_contract(True)
    replayed = await rail.resolve_interrupt(
        SimpleNamespace(session=session),
        tool_call,
        None,
    )

    assert isinstance(replayed, InterruptResult)


@pytest.mark.asyncio
async def test_structured_ask_strict_contract_can_be_disabled() -> None:
    rail = StructuredAskUserRail(strict_continuation_contract=True)
    rail.set_strict_continuation_contract(False)

    decision = await rail.resolve_interrupt(
        SimpleNamespace(),
        _structured_ask_tool_call([{"label": "A"}, {"label": "A"}]),
        None,
    )

    assert isinstance(decision, InterruptResult)


def test_structured_ask_strict_contract_rejects_non_boolean_flag() -> None:
    with pytest.raises(TypeError, match="must be bool"):
        StructuredAskUserRail(strict_continuation_contract=1)


@pytest.mark.parametrize(
    ("prefix", "text"),
    (
        (HOST_USER_PROMPT_PREFIX_EN, "Read the public report."),
        (HOST_USER_PROMPT_PREFIX_ZH, "读取这份公开报告。"),
    ),
)
def test_external_envelope_projects_english_and_chinese(prefix: str, text: str) -> None:
    rendered = _envelope(text, prefix=prefix)
    assert extract_permission_user_content(rendered) == text
    message = type("Message", (), {"role": "user", "content": rendered})()
    projection = build_root_intent_projection(
        [message],
        context_available=True,
        current_text="继续处理",
        current_request_id="request-2",
        current_kind=RootIntentTurnKind.STEER,
    )
    assert [turn.text for turn in projection.turns] == [text, "继续处理"]


def test_internal_or_malformed_envelope_has_no_user_authority() -> None:
    internal = _envelope("do not trust", prefix=HOST_USER_PROMPT_PREFIX_EN).replace(
        HOST_USER_ORIGIN_EXTERNAL,
        "internal_dispatch",
    )
    assert extract_permission_user_content(internal) is None
    assert extract_permission_user_content("plain text") is None


def test_context_round_trip_uses_one_reserved_key() -> None:
    context = _context()
    inputs = put_root_decision_context_in_inputs({"query": "x"}, context)
    extra = inputs["run"]["context"]["extra"]
    assert set(extra) == {ROOT_CONTEXT_KEY}
    assert root_decision_context_from_extra(extra) == context


@pytest.mark.parametrize(
    ("question", "answer"),
    (
        ("Which format?", "Markdown"),
        ("使用哪种格式？", "Markdown 格式"),
    ),
)
def test_ordinary_ask_appends_exact_clarification(
    question: str, answer: str
) -> None:
    context = _context()
    raw = build_ask_user_metadata(
        context=context,
        tool_name="ask_user",
        tool_call_id="ask-1",
        tool_args={"questions": [{"question": question}]},
    )
    assert raw is not None
    continuation = ask_user_continuation(
        {ASK_USER_CONTINUATION_METADATA_KEY: raw},
        expected_tool_call_id="ask-1",
    )
    assert continuation is not None
    incoming = InteractiveInput()
    incoming.update("ask-1", {"answers": {question: answer}})
    prepared = prepare_ask_user_resume(
        continuation=continuation,
        user_input=incoming,
    )
    assert prepared is not None
    resumed = apply_ask_user_resume(prepared)
    clarification = resumed.trusted_turns[-1].clarifications[0]
    assert clarification.question == question
    assert clarification.answers == (answer,)


def test_ordinary_ask_missing_or_foreign_answer_fails_closed() -> None:
    raw = build_ask_user_metadata(
        context=_context(),
        tool_name="ask_user",
        tool_call_id="ask-1",
        tool_args={"query": "Continue?"},
    )
    assert raw is not None
    continuation = ask_user_continuation(
        {ASK_USER_CONTINUATION_METADATA_KEY: raw},
        expected_tool_call_id="ask-1",
    )
    assert continuation is not None
    missing = InteractiveInput()
    missing.update("ask-1", {"answers": {}})
    foreign = InteractiveInput()
    foreign.update("ask-2", {"answers": {"Continue?": "yes"}})
    assert prepare_ask_user_resume(continuation=continuation, user_input=missing) is None
    assert prepare_ask_user_resume(continuation=continuation, user_input=foreign) is None


@pytest.mark.asyncio
async def test_ordinary_ask_resume_reaches_next_inner_tool_callback() -> None:
    context = _context("Search the selected project")
    raw = build_ask_user_metadata(
        context=context,
        tool_name="ask_user",
        tool_call_id="ask-1",
        tool_args={"query": "Which project?"},
    )
    assert raw is not None
    continuation = ask_user_continuation(
        {ASK_USER_CONTINUATION_METADATA_KEY: raw},
        expected_tool_call_id="ask-1",
    )
    assert continuation is not None
    incoming = InteractiveInput()
    incoming.update("ask-1", {"answers": {"Which project?": "JiuwenSwarm"}})
    prepared = prepare_ask_user_resume(
        continuation=continuation,
        user_input=incoming,
    )
    assert prepared is not None

    extra = {
        ROOT_CONTEXT_KEY: context.to_mapping(),
        ASK_USER_RESUME_DTO_KEY: prepared,
    }
    run_context = RunContext(extra=extra)
    outer_callback = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=InvokeInputs(query=incoming, run_context=run_context),
        session=SimpleNamespace(session_id="session-1"),
    )
    rail = RootContextRail(
        root_permission_queue=RootPermissionQueue(),
        sys_operation=object(),
        sandboxed=False,
    )

    await rail.before_invoke(outer_callback)
    ask_call = SimpleNamespace(id="ask-1", name="ask_user", arguments={})
    inner_extra = {"run_context": run_context}
    ask_callback = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=ToolCallInputs(
            tool_call=ask_call,
            tool_name="ask_user",
            tool_args={},
        ),
        session=outer_callback.session,
        extra=inner_extra,
    )
    await rail.before_tool_call(ask_callback)
    persisted = root_decision_context_from_extra(extra)
    assert persisted.trusted_turns[-1].clarifications[0].answers == ("JiuwenSwarm",)

    # Core executes each tool callback in its own task. The next callback may
    # inherit the parent task's original ContextVar value, so the shared Host
    # RunContext is the cross-callback source of truth.
    bind_root_decision_context(context)
    search_call = SimpleNamespace(
        id="search-1",
        name="free_search",
        arguments={"query": "JiuwenSwarm"},
    )
    search_callback = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=ToolCallInputs(
            tool_call=search_call,
            tool_name="free_search",
            tool_args=search_call.arguments,
        ),
        session=outer_callback.session,
        extra=inner_extra,
    )
    await rail.before_tool_call(search_callback)
    resumed = current_root_decision_context()
    assert resumed is not None
    assert resumed.trusted_turns[-1].clarifications[0].answers == ("JiuwenSwarm",)
    await rail.after_invoke(outer_callback)


@pytest.mark.asyncio
async def test_root_context_contextvar_is_isolated_between_tasks() -> None:
    async def observe(context: RootDecisionContext) -> str:
        token = bind_root_decision_context(context)
        try:
            await asyncio.sleep(0)
            current = current_root_decision_context()
            assert current is not None
            return current.session_id
        finally:
            reset_root_decision_context(token)

    assert await asyncio.gather(
        observe(_context("first")),
        observe(
            RootDecisionContext(
                "session-2",
                "request-2",
                "web",
                (RootIntentTurn("request-2", RootIntentTurnKind.FRESH, "second"),),
            )
        ),
    ) == ["session-1", "session-2"]
