# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the fact-premise guard (year/edition questions answered blind).

The regression being pinned: "2028 年世界杯冠军是谁" was answered with the 2026
edition's details, with zero retrieval calls. The guard has to (a) recognise a
year-scoped question by its *shape* rather than by an event-noun list — so an
unlisted event name ("华为云 2025 Inspire 大会", "AWS re:Invent 2026") still gets
guarded, (b) inject a graded verification clause into the user turn, and (c)
never touch ordinary editing tasks or break a turn.
"""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ModelCallInputs, UserMessageInputs

from jiuwenswarm.agents.harness.common.rails.fact_premise_rail import (
    _FORCED_KEY,
    _HIT_KEY,
    _LEVEL_KEY,
    _RETRIEVED_KEY,
    FactPremiseRail,
    _answer_text,
    answer_asserts_year_fact,
    build_fact_check_clause,
    fact_premise_level,
    is_fact_premise_question,
)

TODAY = date(2026, 11, 18)

ENVELOPE = (
    '你收到一条消息：\n{{"source": "officeclaw", "timestamp": "2026-11-18 09:00:00", '
    '"preferred_response_language": "zh", "content": "{content}", '
    '"files_updated_by_user": "{{}}", "type": "user input"}}'
)


# ---------------------------------------------------------------------------
# trigger
# ---------------------------------------------------------------------------


def test_nonexistent_year_event_triggers() -> None:
    assert is_fact_premise_question("2028年世界杯冠军是谁？", today=TODAY)


def test_future_edition_triggers() -> None:
    assert is_fact_premise_question("2030年世界杯在哪些国家举办？", today=TODAY)


def test_current_year_event_triggers() -> None:
    assert is_fact_premise_question("2026年11月还有哪些赛事在进行", today=TODAY)


def test_relative_year_triggers() -> None:
    assert is_fact_premise_question("今年的总决赛冠军是谁", today=TODAY)


def test_english_question_triggers() -> None:
    assert is_fact_premise_question("Who won the 2028 World Cup?", today=TODAY)


def test_settled_past_edition_does_not_trigger() -> None:
    """A past edition is ordinary knowledge; don't pay a search for it."""
    assert not is_fact_premise_question("2022年世界杯冠军是谁？", today=TODAY)


def test_ordinary_editing_request_does_not_trigger() -> None:
    assert not is_fact_premise_question("把这份 PPT 的第 2 页改成深色", today=TODAY)


def test_year_mentioned_in_editing_request_does_not_trigger() -> None:
    assert not is_fact_premise_question(
        "把 2028 年世界杯冠军那页的数据改成 2026 年的", today=TODAY
    )


def test_year_only_edit_does_not_trigger() -> None:
    """A year plus an editing verb is a document task, not a fact question."""
    assert not is_fact_premise_question("把预算表里的 2028 改成 2029", today=TODAY)


def test_empty_or_non_string_does_not_trigger() -> None:
    assert not is_fact_premise_question("", today=TODAY)
    assert not is_fact_premise_question(None, today=TODAY)  # type: ignore[arg-type]


def test_unlisted_event_name_still_triggers() -> None:
    """The hole this fix closes: 「峰会」matched, 「Inspire 大会」did not."""
    assert is_fact_premise_question("华为云2025 Inspire大会在哪举办？", today=TODAY)
    assert is_fact_premise_question("2025年华为云Inspire大会有什么发布？", today=TODAY)
    assert is_fact_premise_question("2026年世界人工智能大会（WAIC）在哪开？", today=TODAY)
    assert is_fact_premise_question("2026年CES 展会有哪些新品？", today=TODAY)


def test_shape_only_hit_is_level_one() -> None:
    """An unknown topic must still be guarded, just with the shorter clause."""
    assert fact_premise_level("2025 re:Invent 有哪些新发布？", today=TODAY) == 1
    assert fact_premise_level("2026年Q1目标是什么？", today=TODAY) == 1


def test_event_topic_is_level_two() -> None:
    assert fact_premise_level("2026年华为云峰会有什么发布？", today=TODAY) == 2
    assert fact_premise_level("华为云2025 Inspire大会在哪举办？", today=TODAY) == 2


def test_english_question_without_question_mark_triggers() -> None:
    assert is_fact_premise_question("What did AWS announce at re:Invent 2026", today=TODAY)


def test_deictic_edition_is_a_temporal_anchor() -> None:
    """「本届 / 上届」anchor a question without a year number."""
    assert is_fact_premise_question("上届冠军是谁", today=TODAY)
    assert not is_fact_premise_question("把上届冠军那页的数据改成 2026 年的", today=TODAY)


def test_envelope_metadata_does_not_widen_the_year_window() -> None:
    """The relay stamps today's year into every turn; judging the raw envelope
    would make ``recent_year`` always true and void ``_PAST_YEAR_SLACK``."""
    assert is_fact_premise_question(
        ENVELOPE.format(content="华为云2025 Inspire大会在哪举办？"), today=TODAY
    )
    assert fact_premise_level(
        ENVELOPE.format(content="华为云2025 Inspire大会在哪举办？"), today=TODAY
    ) == 2
    assert not is_fact_premise_question(
        ENVELOPE.format(content="2024年华为云峰会有什么发布？"), today=TODAY
    )
    # A settled past edition without the envelope must behave the same way.
    assert not is_fact_premise_question("2024年华为云峰会有什么发布？", today=TODAY)


def test_malformed_envelope_falls_back_to_raw_text() -> None:
    assert not is_fact_premise_question("你收到一条消息：{not json}", today=TODAY)
    assert fact_premise_level(
        '你收到一条消息：{"timestamp": "2026-11-18", "content": ""}', today=TODAY
    ) == 0


def test_short_clause_is_level_one_and_keeps_the_prefix() -> None:
    short_cn = build_fact_check_clause("cn", level=1)
    full_cn = build_fact_check_clause("cn")
    assert short_cn.startswith("[事实核对]")
    assert len(short_cn) * 2 < len(full_cn)
    # The cycle rule and the tool-call wording belong to the full clause only.
    assert "每 4 年一届" not in short_cn
    assert "核实优先于工具调用次数控制" not in short_cn
    assert build_fact_check_clause("en", level=1).startswith("[Fact-check]")
    assert len(build_fact_check_clause("en", level=1)) < len(build_fact_check_clause("en"))


def test_clause_is_bilingual_and_names_the_cycle_rule() -> None:
    cn = build_fact_check_clause("cn")
    en = build_fact_check_clause("en")
    assert cn.startswith("[事实核对]")
    assert "每 4 年一届" in cn
    assert "不要编造" in cn
    # Neutral priority wording: the Flash-only "one tool per question" rule does
    # not exist in deep/team prompts, so the clause must not cite it.
    assert "默认一个问题一个工具" not in cn
    assert "核实优先于工具调用次数控制" in cn
    assert en.startswith("[Fact-check]")
    assert "every four years" in en
    assert "one-tool" not in en


# ---------------------------------------------------------------------------
# lever 1: user-turn injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_user_message_prepends_clause_and_keeps_original() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["2028年世界杯冠军是谁？"]
    ctx = SimpleNamespace(
        inputs=UserMessageInputs(parts=parts, source="query"),
        extra={},
    )

    await rail.on_user_message(ctx)

    assert parts[0].startswith("[事实核对]")
    assert parts[1] == "2028年世界杯冠军是谁？"
    assert parts[0] == build_fact_check_clause("cn")
    assert ctx.extra[_HIT_KEY] is True
    assert ctx.extra[_LEVEL_KEY] == 2


@pytest.mark.asyncio
async def test_on_user_message_injects_short_clause_for_shape_only_hit() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["2026年Q1目标是什么？"]
    ctx = SimpleNamespace(
        inputs=UserMessageInputs(parts=parts, source="query"),
        extra={},
    )

    await rail.on_user_message(ctx)

    assert parts[0] == build_fact_check_clause("cn", level=1)
    assert parts[1] == "2026年Q1目标是什么？"
    assert ctx.extra[_LEVEL_KEY] == 1


@pytest.mark.asyncio
async def test_on_user_message_guards_an_unlisted_event_name() -> None:
    """End-to-end shape trigger on the case that used to pass through silently."""
    rail = FactPremiseRail(language="cn")
    parts = ["华为云2025 Inspire大会在哪举办？"]
    ctx = SimpleNamespace(
        inputs=UserMessageInputs(parts=parts, source="query"),
        extra={},
    )

    await rail.on_user_message(ctx)

    assert parts[0] == build_fact_check_clause("cn")
    assert parts[1] == "华为云2025 Inspire大会在哪举办？"


@pytest.mark.asyncio
async def test_on_user_message_is_a_noop_when_not_matched() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["把这份 PPT 的第 2 页改成深色"]
    original = list(parts)
    ctx = SimpleNamespace(
        inputs=UserMessageInputs(parts=parts, source="query"),
        extra={},
    )

    await rail.on_user_message(ctx)

    assert parts == original
    assert ctx.extra == {}


@pytest.mark.asyncio
async def test_on_user_message_ignores_steering_batches() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["2028年世界杯冠军是谁？"]
    ctx = SimpleNamespace(
        inputs=UserMessageInputs(parts=parts, source="steering"),
        extra={},
    )

    await rail.on_user_message(ctx)

    assert parts == ["2028年世界杯冠军是谁？"]
    assert ctx.extra == {}


@pytest.mark.asyncio
async def test_on_user_message_is_fail_open_on_bad_inputs() -> None:
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(inputs=SimpleNamespace(parts="not-a-list", source="query"), extra=None)

    await rail.on_user_message(ctx)  # must not raise


@pytest.mark.asyncio
async def test_on_user_message_does_not_inject_twice() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["2028年世界杯冠军是谁？"]
    ctx = SimpleNamespace(inputs=UserMessageInputs(parts=parts, source="query"), extra={})

    await rail.on_user_message(ctx)
    await rail.on_user_message(ctx)

    assert sum(1 for part in parts if part.startswith("[事实核对]")) == 1


@pytest.mark.asyncio
async def test_short_clause_is_not_injected_twice() -> None:
    rail = FactPremiseRail(language="cn")
    parts = ["2026年Q1目标是什么？"]
    ctx = SimpleNamespace(inputs=UserMessageInputs(parts=parts, source="query"), extra={})

    await rail.on_user_message(ctx)
    await rail.on_user_message(ctx)

    assert sum(1 for part in parts if part.startswith("[事实核对]")) == 1


@pytest.mark.asyncio
async def test_user_quoting_the_marker_still_gets_guarded() -> None:
    """A substring scan would mistake a quoted marker for an injected clause."""
    rail = FactPremiseRail(language="cn")
    parts = ["[事实核对] 这个机制是什么？2028年世界杯冠军是谁？"]
    ctx = SimpleNamespace(inputs=UserMessageInputs(parts=parts, source="query"), extra={})

    await rail.on_user_message(ctx)

    assert len(parts) == 2
    assert parts[0] == build_fact_check_clause("cn")
    assert parts[1].startswith("[事实核对] 这个机制是什么？")


# ---------------------------------------------------------------------------
# lever 2: optional forced retrieval
# ---------------------------------------------------------------------------


def _model_ctx(*, tools, iteration: int, extra: dict) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=ModelCallInputs(tools=tools, react_iteration=iteration),
        extra=extra,
    )


@pytest.mark.asyncio
async def test_before_model_call_forces_advertised_retrieval_tool() -> None:
    rail = FactPremiseRail(
        language="cn",
        force_tool_names=("web_flash",),
        force_first_retrieval=True,
    )
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="read_file"), SimpleNamespace(name="web_flash")],
        iteration=1,
        extra={_HIT_KEY: True},
    )

    await rail.before_model_call(ctx)

    choice = ctx.extra["llm_call_kwargs"]["tool_choice"]
    assert choice == {"type": "function", "function": {"name": "web_flash"}}
    assert ctx.extra[_FORCED_KEY] is True


@pytest.mark.asyncio
async def test_before_model_call_merges_existing_llm_kwargs() -> None:
    """Thinking kwargs written by a higher-priority rail must survive."""
    rail = FactPremiseRail(force_tool_names=("web_flash",), force_first_retrieval=True)
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="web_flash")],
        iteration=1,
        extra={
            _HIT_KEY: True,
            "llm_call_kwargs": {"extra_body": {"thinking": {"type": "enabled"}}},
        },
    )

    await rail.before_model_call(ctx)

    assert ctx.extra["llm_call_kwargs"]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "tool_choice" in ctx.extra["llm_call_kwargs"]


@pytest.mark.asyncio
async def test_before_model_call_does_not_force_on_level_one_hit() -> None:
    """Only the full-clause hit may take the intrusive lever."""
    rail = FactPremiseRail(force_tool_names=("web_flash",), force_first_retrieval=True)
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="web_flash")],
        iteration=1,
        extra={_HIT_KEY: True, _LEVEL_KEY: 1},
    )

    await rail.before_model_call(ctx)

    assert "llm_call_kwargs" not in ctx.extra


@pytest.mark.asyncio
async def test_before_model_call_does_not_force_when_tool_absent() -> None:
    rail = FactPremiseRail(force_tool_names=("web_flash",), force_first_retrieval=True)
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="read_file")],
        iteration=1,
        extra={_HIT_KEY: True},
    )

    await rail.before_model_call(ctx)

    assert "llm_call_kwargs" not in ctx.extra


@pytest.mark.asyncio
async def test_before_model_call_does_not_force_on_later_iterations() -> None:
    rail = FactPremiseRail(force_tool_names=("web_flash",), force_first_retrieval=True)
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="web_flash")],
        iteration=2,
        extra={_HIT_KEY: True},
    )

    await rail.before_model_call(ctx)

    assert "llm_call_kwargs" not in ctx.extra


@pytest.mark.asyncio
async def test_before_model_call_does_nothing_when_not_matched() -> None:
    rail = FactPremiseRail(force_tool_names=("web_flash",), force_first_retrieval=True)
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="web_flash")],
        iteration=1,
        extra={},
    )

    await rail.before_model_call(ctx)

    assert "llm_call_kwargs" not in ctx.extra


@pytest.mark.asyncio
async def test_before_model_call_is_off_by_default() -> None:
    rail = FactPremiseRail(force_tool_names=("web_flash",))
    ctx = _model_ctx(
        tools=[SimpleNamespace(name="web_flash")],
        iteration=1,
        extra={_HIT_KEY: True},
    )

    await rail.before_model_call(ctx)

    assert "llm_call_kwargs" not in ctx.extra


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


def _answer(content: str, *, tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


@pytest.mark.asyncio
async def test_after_model_call_warns_when_final_answer_used_no_retrieval(caplog) -> None:
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(response=_answer("2028 年世界杯冠军是……")),
        extra={_HIT_KEY: True, "_fact_premise_query": "2028年世界杯冠军是谁？"},
    )

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert any("without retrieval" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_after_model_call_ignores_tool_call_turns(caplog) -> None:
    """A tool-call turn is not the answer; the loop will continue."""
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(
            response=_answer("", tool_calls=[SimpleNamespace(name="web_flash")])
        ),
        extra={_HIT_KEY: True},
    )

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert not any("without retrieval" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_after_model_call_ignores_empty_responses(caplog) -> None:
    """Empty no-tool turns are retried in core, so they are not answers."""
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(response=_answer("   ")),
        extra={_HIT_KEY: True},
    )

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert not any("without retrieval" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# output-side check (turns the input gate never classified)
# ---------------------------------------------------------------------------


def test_answer_asserts_year_fact_positive() -> None:
    assert answer_asserts_year_fact(
        "2026 年世界杯将于 6 月 11 日开幕，共 48 支球队参加。", today=TODAY
    )
    assert answer_asserts_year_fact(
        "第 23 届冬奥会于 2026 年 2 月 6 日开幕。", today=TODAY
    )
    assert answer_asserts_year_fact(
        "2025 年华为云 Inspire 大会在上海举办，发布了三款新品。", today=TODAY
    )
    # List-shaped content is flattened before matching.
    flattened = _answer_text(_answer(["2026 年世界杯共 48 支球队。"]))
    assert flattened == "2026 年世界杯共 48 支球队。"
    assert answer_asserts_year_fact(flattened, today=TODAY)


def test_answer_asserts_year_fact_negative() -> None:
    # A refusal is the behaviour we want, not a blind claim.
    assert not answer_asserts_year_fact("抱歉，我无法确认 2028 年是否有世界杯。", today=TODAY)
    assert not answer_asserts_year_fact("There is no such event; cannot confirm.", today=TODAY)
    # Settled past edition, no assertion, or no year at all.
    assert not answer_asserts_year_fact("2022 年世界杯冠军是阿根廷。", today=TODAY)
    assert not answer_asserts_year_fact("已把 2026 年那页改成深色。", today=TODAY)
    assert not answer_asserts_year_fact("好的，我这就去看。", today=TODAY)
    assert not answer_asserts_year_fact(None, today=TODAY)  # type: ignore[arg-type]
    # Everyday office verbs next to a year are not factual claims; bare "发布"
    # is excluded on purpose so "2026 年发布计划" does not drown the signal.
    assert not answer_asserts_year_fact("已按 2026 年发布计划更新文档。", today=TODAY)
    assert not answer_asserts_year_fact("2026 年预算需要 5 人参与评审。", today=TODAY)


@pytest.mark.asyncio
async def test_unclassified_answer_that_states_a_year_fact_warns(caplog) -> None:
    """No temporal anchor in the input, so the gate never ran — the answer is the only signal."""
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(
            response=_answer("2025 年华为云 Inspire 大会于 9 月 19 日在上海举办。")
        ),
        extra={},
    )

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert any("unclassified answer" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_unclassified_answer_with_retrieval_does_not_warn(caplog) -> None:
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(
            response=_answer("2025 年华为云 Inspire 大会于 9 月 19 日在上海举办。")
        ),
        extra={},
    )

    await rail.after_tool_call(
        SimpleNamespace(inputs=SimpleNamespace(tool_name="web_flash"), extra=ctx.extra)
    )
    assert ctx.extra[_RETRIEVED_KEY] is True

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert not any("unclassified answer" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_unclassified_claim_logging_can_be_disabled(caplog) -> None:
    rail = FactPremiseRail(language="cn", log_unclassified_claims=False)
    ctx = SimpleNamespace(
        inputs=ModelCallInputs(
            response=_answer("2025 年华为云 Inspire 大会于 9 月 19 日在上海举办。")
        ),
        extra={},
    )

    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)

    assert not any("unclassified answer" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_after_tool_call_records_retrieval_without_a_hit() -> None:
    """The flag must not depend on ``_hit``: unclassified turns need it too."""
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="web_search"), extra={})

    await rail.after_tool_call(ctx)

    assert ctx.extra[_RETRIEVED_KEY] is True


@pytest.mark.asyncio
async def test_after_tool_call_marks_retrieval_and_silences_warning(caplog) -> None:
    rail = FactPremiseRail(language="cn")
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="web_flash"),
        extra={_HIT_KEY: True, "_fact_premise_query": "2028年世界杯冠军是谁？"},
    )

    await rail.after_tool_call(ctx)
    assert ctx.extra[_RETRIEVED_KEY] is True

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await rail.after_model_call(ctx)
    assert not any("without retrieval" in record.message for record in caplog.records)


def test_observability_hooks_are_bridged_to_the_inner_react_agent() -> None:
    """Pin the routing contract: outer-only events cannot see the hit flag."""
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
    from openjiuwen.harness.deep_agent import _BRIDGE_EVENTS

    callbacks = FactPremiseRail().get_callbacks()

    assert AgentCallbackEvent.AFTER_MODEL_CALL in callbacks
    assert AgentCallbackEvent.AFTER_MODEL_CALL in _BRIDGE_EVENTS
    assert AgentCallbackEvent.AFTER_INVOKE not in callbacks
