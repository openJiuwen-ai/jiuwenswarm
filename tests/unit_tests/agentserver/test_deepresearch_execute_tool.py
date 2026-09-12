"""Regression tests for deterministic DeepResearch orchestration."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
    DEEPRESEARCH_EXECUTION_ALIAS_KEY,
    DEEPRESEARCH_EXECUTION_STATE_KEY,
    DeepResearchExecutionRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de


class _Session:
    def __init__(self, session_id: str = ""):
        self.state: dict[str, object] = {}
        self._session_id = session_id

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, values):
        self.state.update(values)

    def get_session_id(self):
        return self._session_id


class _Model:
    def __init__(self, content: str):
        self.invoke = AsyncMock(return_value=SimpleNamespace(content=content))


def test_execution_rail_converts_before_stream_event_rail():
    assert DeepResearchExecutionRail.priority > JiuSwarmStreamEventRail.priority


def _option_payload(*questions: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "question_index": index,
                    "options": [
                        {"label": f"{question}：方向A"},
                        {"label": f"{question}：方向B"},
                    ],
                }
                for index, question in enumerate(questions)
            ]
        },
        ensure_ascii=False,
    )


async def _invoke(
    *,
    state=None,
    user_input=None,
    model=None,
    query="研究智能家电竞争格局",
    file_name="智能家电报告",
):
    saved: list[dict] = []
    token = de.bind_deepresearch_execution_context(
        tool_call_id="call-1",
        state=state,
        user_input=user_input,
        model=model,
        save_state=lambda value: saved.append(dict(value)),
    )
    try:
        result = await de.deepresearch_execute._func(query=query, file_name=file_name)
    finally:
        de.reset_deepresearch_execution_context(token)
    return result, saved


@pytest.mark.asyncio
async def test_missing_query_fails_before_sdk_start():
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, saved = await _invoke(query="   ")

    assert result["kind"] == "error"
    assert result["error_code"] == "query_missing"
    assert saved[-1]["phase"] == "error"
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_error_preserves_bounded_subprocess_diagnostics():
    outcome = {
        "status": "error",
        "error_code": "terminal_marker_missing",
        "error": "no terminal marker",
        "returncode": 1,
        "stderr_tail": "ModuleNotFoundError: No module named 'aiosqlite'",
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, saved = await _invoke(query="请生成一份详细的智能家电竞争报告")

    assert result["kind"] == "error"
    assert result["error_code"] == "terminal_marker_missing"
    assert result["returncode"] == 1
    assert result["stderr_tail"] == "ModuleNotFoundError: No module named 'aiosqlite'"
    assert saved[-1]["phase"] == "error"


@pytest.mark.asyncio
async def test_new_query_starts_sdk_directly():
    sdk_usage = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "llm_call_count": 2,
        "agent_name_token_usage": [],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 42,
        "workflow_llm_token_usage": sdk_usage,
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(query="研究智能家电竞争格局")

    assert result["kind"] == "completed"
    assert result["workflow_llm_token_usage"] == sdk_usage
    assert saved[0]["phase"] == "starting"
    stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_option_generation_retries_then_keeps_free_text_questions():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }
    model = _Model("not-json")

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, _ = await _invoke(model=model)

    assert [item["question"] for item in result["interaction"]["questions"]] == questions
    assert [item["options"] for item in result["interaction"]["questions"]] == [[], []]
    assert model.invoke.await_count == 2


@pytest.mark.asyncio
async def test_option_generation_preserves_model_defaults_and_completion_budget():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？", "报告用于什么决策？"]
    model = _Model(_option_payload(*questions))
    model.model_config = SimpleNamespace(model_name="glm-5.2")
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=model,
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
    )

    options = await de._generate_options(context, "研究智能家电竞争格局", questions)

    assert all(len(item) == 2 for item in options)
    messages = model.invoke.await_args.args[0]
    kwargs = model.invoke.await_args.kwargs
    assert kwargs["max_tokens"] == 2048
    assert "extra_body" not in kwargs
    prompt = messages[0]["content"]
    assert "description 不超过 50 字" in prompt
    assert "不得输出分析或推理过程" in prompt


def test_option_description_is_truncated_without_rejecting_valid_options():
    payload = json.dumps(
        {
            "items": [
                {
                    "question_index": 0,
                    "options": [
                        {"label": "方向A", "description": "说明" * 30},
                        {"label": "方向B", "description": "简短说明"},
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    parsed = de._parse_option_payload(payload, 1)

    assert parsed is not None
    assert len(parsed[0][0]["description"]) == 50
    assert parsed[0][1]["description"] == "简短说明"


def test_options_llm_is_recorded_in_request_summary_with_distinct_source():
    collector = SimpleNamespace(
        get_accumulator=Mock(return_value=None),
        record_llm=Mock(),
    )
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2")),
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
    )
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(input_tokens=12, output_tokens=7)
    )

    with (
        patch(
            "jiuwenswarm.perf.context.get_request_context",
            return_value={"request_id": "request-1"},
        ),
        patch("jiuwenswarm.perf.context.get_react_iteration", return_value=2),
        patch("jiuwenswarm.perf.context.resolve_task_id", return_value=None),
        patch(
            "jiuwenswarm.perf.collector.get_perf_collector",
            return_value=collector,
        ),
    ):
        de._record_options_llm_perf(
            context,
            response=response,
            duration_ms=1234.5,
            status="ok",
        )

    request_id, event = collector.record_llm.call_args.args
    assert request_id == "request-1"
    assert event.model == "glm-5.2"
    assert event.duration_ms == 1234.5
    assert event.input_tokens == 12
    assert event.output_tokens == 7
    assert event.agent_id == "jiuwenswarm"
    assert event.stream_source_id == "deepresearch_options"


def test_options_llm_uses_bound_request_id_when_task_context_is_missing():
    collector = SimpleNamespace(
        get_accumulator=Mock(return_value=None),
        record_llm=Mock(),
    )
    context = de.DeepResearchExecutionContext(
        tool_call_id="call-1",
        state=None,
        user_input=None,
        model=SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2")),
        agent_id="jiuwenswarm",
        save_state=lambda _value: None,
        request_id="request-from-rail",
    )

    with (
        patch("jiuwenswarm.perf.context.get_request_context", return_value=None),
        patch("jiuwenswarm.perf.context.get_react_iteration", return_value=0),
        patch("jiuwenswarm.perf.context.resolve_task_id", return_value=None),
        patch(
            "jiuwenswarm.perf.collector.get_perf_collector",
            return_value=collector,
        ),
    ):
        de._record_options_llm_perf(
            context,
            response=SimpleNamespace(),
            duration_ms=500.0,
            status="ok",
        )

    request_id, event = collector.record_llm.call_args.args
    assert request_id == "request-from-rail"
    assert event.stream_source_id == "deepresearch_options"


@pytest.mark.asyncio
async def test_timing_logs_attribute_sdk_and_options_without_business_content():
    query = "请详细研究绝密智能家电主题"
    questions = ["绝密问题甲？", "绝密问题乙？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }
    model = _Model(_option_payload(*questions))

    with (
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ),
        patch.object(de.logger, "info") as log_info,
    ):
        await _invoke(query=query, model=model)

    messages = "\n".join(
        call.args[0] % call.args[1:]
        for call in log_info.call_args_list
    )
    assert "sdk window" in messages
    assert "action=start" in messages
    assert "status=interrupted" in messages
    assert "option generation completed" in messages
    assert "attempts=1" in messages
    assert "questions=2" in messages
    assert query not in messages
    assert all(question not in messages for question in questions)
    assert "方向A" not in messages


@pytest.mark.asyncio
async def test_missing_sdk_questions_preserve_original_free_text_fallback():
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"prompt": "请说明希望补充的研究方向"},
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
    ):
        result, _ = await _invoke(query="请详细研究智能家电")

    assert result["interaction"]["query"] == "请补充研究方向反馈"
    assert result["interaction"]["questions"] == [
        {
            "header": "研究方向反馈",
            "question": "请说明希望补充的研究方向",
            "multi_select": False,
            "options": [],
        }
    ]


def test_question_split_keeps_every_valid_sdk_question():
    questions = [f"问题{i}？" for i in range(1, 6)]

    assert de._split_questions(
        "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    ) == questions


def test_execution_tool_is_an_exclusive_batch_barrier():
    assert de.deepresearch_execute.card.parallel_safe is False


@pytest.mark.asyncio
async def test_feedback_answer_resumes_once_and_returns_direct_completion():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["重点研究哪些品类？"],
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 12345,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "completed"
    assert "12,345" in result["content"]
    assert "✅ **深度研究已完成！**" in result["content"]
    assert "智能家电报告已成功生成并交付" in result["content"]
    assert saved[0]["phase"] == "resuming_feedback"
    assert saved[-1]["phase"] == "completed"
    assert stream.await_count == 1
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "feedback_handler"
    assert "空调与冰箱" in stream.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_completion_warns_when_html_style_falls_back():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 42,
        "html_style_status": "fallback",
        "html_style_phase": "invoke_llm",
        "html_style_reason_code": "llm_call_failed",
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(query="研究智能家电竞争格局")

    assert result["kind"] == "completed"
    assert result["html_style_status"] == "fallback"
    assert result["html_style_phase"] == "invoke_llm"
    assert result["html_style_reason_code"] == "llm_call_failed"
    assert (
        "HTML 已交付内置基础视觉模板，但 AI 生成的增强样式未应用"
        in result["content"]
    )
    assert saved[-1]["html_style_status"] == "fallback"
    assert saved[-1]["html_style_phase"] == "invoke_llm"
    assert saved[-1]["html_style_reason_code"] == "llm_call_failed"


@pytest.mark.asyncio
async def test_terminal_result_preserves_all_sdk_timing_windows():
    previous_window = {
        "action": "start",
        "node": "",
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "timing": {
            "schema_version": 2,
            "runner_total_ms": 120,
            "runner_bootstrap_ms": 20,
            "sdk_execution_ms": 100,
            "sdk_node_spans": [],
        },
        "skill_execution_ms": 130,
    }
    state = {
        "schema_version": 1,
        "phase": "wait_outline",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "outline_presented": True,
        "timing_windows": [previous_window],
        "revision": 5,
    }
    answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 42,
        "timing": {
            "schema_version": 2,
            "runner_total_ms": 340,
            "runner_bootstrap_ms": 40,
            "sdk_execution_ms": 300,
            "sdk_first_node_ms": 6,
            "sdk_node_spans": [],
        },
        "skill_execution_ms": 350,
        "report_delivery_ms": 10,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        result, saved = await _invoke(state=state, user_input=answer)

    assert result["kind"] == "completed"
    assert result["timing"]["sdk_execution_ms"] == 300
    assert result["skill_execution_ms"] == 350
    assert result["report_delivery_ms"] == 10
    assert [window["action"] for window in result["timing_windows"]] == [
        "start",
        "resume",
    ]
    assert result["timing_windows"][1]["node"] == "outline_interaction"
    assert saved[-1]["timing_windows"] == result["timing_windows"]


@pytest.mark.asyncio
async def test_partial_feedback_marks_unanswered_sdk_question_without_finishing():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["是否结束研究？", "重点研究哪些品类？"],
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
    }

    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        await _invoke(state=state, user_input=answer)

    feedback = json.loads(stream.await_args.kwargs["feedback"])["feedback"]
    assert "问题1: 是否结束研究？\n回答: 未回答" in feedback
    assert "问题2: 重点研究哪些品类？\n回答: 空调与冰箱" in feedback
    assert feedback != "finish"


@pytest.mark.asyncio
async def test_outline_is_presented_once_then_confirmed_without_main_agent():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "研究智能家电竞争格局",
        "file_name": "智能家电报告",
        "conversation_id": "conversation-1",
        "questions": ["重点研究哪些品类？"],
        "revision": 3,
    }
    outline = "## 页面规划\n\n### P1: 市场格局"
    interrupted = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "outline_interaction",
        "marker": {"outline": outline},
    }
    feedback_answer = {
        "status": "answered",
        "answers": [{"selected_options": ["空调与冰箱"]}],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(interrupted, ensure_ascii=False)),
    ):
        interaction, _ = await _invoke(state=state, user_input=feedback_answer)

    assert interaction["kind"] == "interaction"
    assert interaction["state"]["phase"] == "wait_outline"
    assert interaction["state"]["outline_sections"] == ["市场格局"]
    assert interaction["interaction"]["questions"][0]["preview"]["text"] == outline

    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 100,
    }
    outline_answer = {
        "status": "answered",
        "answers": [{"selected_options": ["确认大纲，继续研究"]}],
    }
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ) as stream:
        result, saved = await _invoke(
            state=interaction["state"], user_input=outline_answer
        )

    assert result["kind"] == "completed"
    assert "1. 市场格局" in result["content"]
    assert saved[0]["phase"] == "resuming_outline"
    assert stream.await_args.kwargs["node"] == "outline_interaction"
    assert "accepted" in stream.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_inflight_replay_fails_closed_without_duplicate_sdk_call():
    state = {
        "schema_version": 1,
        "phase": "resuming_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "revision": 4,
    }
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, _ = await _invoke(state=state, user_input={"status": "answered"})

    assert result["kind"] == "error"
    assert result["error_code"] == "execution_uncertain"
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_feedback_stops_without_resuming_sdk():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
        result, saved = await _invoke(
            state=state,
            user_input={"status": "cancelled", "answers": []},
        )

    assert result["kind"] == "cancelled"
    assert saved[-1]["phase"] == "cancelled"
    stream.assert_not_awaited()


def _rail_ctx(*, result, session=None, resume_input=None, tool_call_id="call-1"):
    session = session or _Session()
    tool_call = SimpleNamespace(
        id=tool_call_id,
        name="deepresearch_execute",
        arguments={"query": "q", "file_name": "r"},
    )
    forced: list[dict] = []
    extra = {}
    if resume_input is not None:
        from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        interactive_input = InteractiveInput()
        interactive_input.update(tool_call_id, resume_input)
        extra[RESUME_USER_INPUT_KEY] = interactive_input
    return SimpleNamespace(
        session=session,
        agent=None,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="deepresearch_execute",
            tool_args=tool_call.arguments,
            tool_result=result,
        ),
        extra=extra,
        exception=None,
        request_force_finish=forced.append,
        force_finish_requests=forced,
    )


@pytest.mark.asyncio
async def test_execution_rail_turns_interaction_result_into_native_interrupt():
    state = {"schema_version": 1, "phase": "wait_feedback", "revision": 1}
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "interaction",
        "interaction": {
            "query": "请回答以下研究主题澄清问题",
            "return_json": True,
            "questions": [
                {
                    "header": "研究方向反馈",
                    "question": "重点研究哪些品类？",
                    "options": [{"label": "方向A"}, {"label": "方向B"}],
                }
            ],
        },
        "state": state,
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    assert ctx.inputs.tool_result.request.questions == result["interaction"]["questions"]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY]["call-1"] == state
    # ability_manager falls back to the raw envelope ToolMessage when a rail
    # clears tool_msg, so the rail must hand over a compact placeholder instead.
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "call-1"
    assert "研究方向澄清" in ctx.inputs.tool_msg.content
    assert "重点研究哪些品类" not in ctx.inputs.tool_msg.content


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self, size=None, with_history=True):
        return list(self.messages)


@pytest.mark.asyncio
async def test_execution_rail_syncs_root_tool_message_on_resumed_completion():
    """Regression for the 2026-09-08 12:40 rerun (session officeclaw_c5eb4ee7).

    The root call's ToolMessage held the first interaction envelope; the
    completed result arrived under alias ``call-1_interaction_4`` and was
    dropped as an orphan by sanitize_tool_pairing, so the model re-asked the
    clarification questions and ran DeepResearch a second time.
    """
    root_message = ToolMessage(content="{'kind': 'interaction', ...}", tool_call_id="call-1")
    other_message = ToolMessage(content="todo ok", tool_call_id="call-0")
    session = _Session()
    session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-1_interaction_4": "call-1"}})
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session, tool_call_id="call-1_interaction_4")
    ctx.context = _ModelContext([other_message, root_message])
    ctx.inputs.tool_msg = ToolMessage(content=str(result), tool_call_id="call-1_interaction_4")
    todos = [
        {"id": "deep_research", "status": "in_progress"},
        {"id": "generate_ppt", "status": "pending"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ),
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    assert "generate_ppt" in root_message.content
    assert "禁止再次调用 deepresearch_execute" in root_message.content
    assert "interaction" not in root_message.content
    assert other_message.content == "todo ok"
    assert ctx.inputs.tool_msg.content == root_message.content


@pytest.mark.asyncio
async def test_execution_rail_syncs_root_tool_message_on_resumed_interaction():
    root_message = ToolMessage(content="{'kind': 'interaction', 'phase': 'wait_feedback'}", tool_call_id="call-1")
    session = _Session()
    session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-1_interaction_2": "call-1"}})
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "interaction",
        "interaction": {
            "query": "请确认大纲",
            "questions": [{"header": "大纲确认", "question": "是否接受该大纲？", "options": []}],
        },
        "state": {"schema_version": 1, "phase": "wait_outline", "revision": 3},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session, tool_call_id="call-1_interaction_2")
    ctx.context = _ModelContext([root_message])

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    assert "大纲确认" in root_message.content
    assert "wait_feedback" not in root_message.content
    assert ctx.inputs.tool_msg.tool_call_id == "call-1_interaction_2"


@pytest.mark.asyncio
async def test_execution_rail_syncs_root_tool_message_before_force_finish():
    root_message = ToolMessage(content="{'kind': 'interaction', ...}", tool_call_id="call-1")
    session = _Session()
    session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-1_interaction_4": "call-1"}})
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session, tool_call_id="call-1_interaction_4")
    ctx.context = _ModelContext([root_message])

    with patch(
        "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
        "_load_todo_items",
        return_value=[],
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [
        {"output": "研究报告已生成并交付。", "result_type": "answer"}
    ]
    assert root_message.content == "研究报告已生成并交付。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "extra", "content"),
    [
        ("error", {"error_code": "empty_report"}, "DeepResearch 执行失败：workflow ended without report content"),
        ("cancelled", {}, "DeepResearch 任务已取消。"),
    ],
)
async def test_execution_rail_force_finishes_failed_research_even_with_followup_todos(kind, extra, content):
    """Regression for the 2026-09-08 14:49 rerun.

    The SDK sub_reporter crashed (float - str), deepresearch_execute returned
    kind=error and, because the PPT todo was still pending, the rail handed the
    failure text to the model, which "retried once" — a second 20 min research
    run. error / cancelled must end the turn like SKILL.md promises.
    """
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": kind,
        "content": content,
        "state": {"schema_version": 1, "phase": kind, "revision": 5},
        **extra,
    }
    root_message = ToolMessage(content="{'kind': 'interaction', ...}", tool_call_id="call-1")
    session = _Session()
    session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: {"call-1_interaction_4": "call-1"}})
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session, tool_call_id="call-1_interaction_4")
    ctx.context = _ModelContext([root_message])
    todos = [
        {"id": "deep_research", "status": "in_progress"},
        {"id": "generate_ppt", "status": "pending"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ) as save_todos,
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [{"output": content, "result_type": "answer"}]
    # No handoff: todos untouched, the failure text is what the user sees.
    save_todos.assert_not_called()
    assert todos[0]["status"] == "in_progress"
    assert todos[1]["status"] == "pending"
    assert root_message.content == content
    assert ctx.inputs.tool_result["content"] == content


def test_resolve_research_todo_prefers_name_over_active_binding():
    from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
        _resolve_research_todo_id,
    )

    todos = [
        {"id": "deep_research", "status": "completed"},
        {"id": "generate_ppt", "status": "in_progress"},
    ]
    # Model re-ran deepresearch_execute while the PPT todo was active: the
    # research todo is still deep_research, not the bound PPT todo.
    assert _resolve_research_todo_id(todos, "generate_ppt") == "deep_research"
    assert _resolve_research_todo_id(
        [{"id": "研究招行", "status": "in_progress"}, {"id": "ppt", "status": "pending"}], ""
    ) == "研究招行"
    # Ambiguous loose matches fall through to the binding.
    assert _resolve_research_todo_id(
        [{"id": "research_a", "status": "in_progress"}, {"id": "research_b", "status": "pending"}],
        "research_b",
    ) == "research_b"


@pytest.mark.asyncio
async def test_execution_rail_force_finishes_terminal_result_without_next_llm():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [
        {"output": "研究报告已生成并交付。", "result_type": "answer"}
    ]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}


@pytest.mark.asyncio
async def test_execution_rail_skips_force_finish_when_followup_todo_pending():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)
    todos = [
        {"id": "deepresearch", "status": "in_progress"},
        {"id": "generate_ppt", "status": "pending"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ) as save_todos,
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert todos[0]["status"] == "completed"
    assert todos[1]["status"] == "in_progress"
    save_todos.assert_called_once()
    assert ctx.inputs.tool_result["kind"] == "completed"
    handoff = ctx.inputs.tool_result["content"]
    assert "generate_ppt" in handoff
    assert "deepresearch_execute" in handoff
    assert "ask_user" in handoff


@pytest.mark.asyncio
async def test_execution_rail_compacts_long_report_in_followup_handoff():
    long_report = (
        "预搜索发现招行零售资产质量仍承压，需要你确认以下研究方向问题。" * 40
    )
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": long_report,
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)
    todos = [
        {"id": "deepresearch", "status": "in_progress"},
        {"id": "generate_ppt", "status": "pending"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ),
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    handoff = ctx.inputs.tool_result["content"]
    assert len(handoff) < len(long_report)
    assert "预搜索发现" not in handoff
    assert "generate_ppt" in handoff
    assert "ask_user" in handoff
    assert "禁止" in handoff
    assert ctx.inputs.tool_result["kind"] == "completed"


@pytest.mark.asyncio
async def test_execution_rail_force_finishes_when_only_research_todo_remains():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)

    with patch(
        "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
        "_load_todo_items",
        return_value=[{"id": "deepresearch", "status": "in_progress"}],
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [
        {"output": "研究报告已生成并交付。", "result_type": "answer"}
    ]


@pytest.mark.asyncio
async def test_execution_rail_reads_followup_todos_from_disk(tmp_path):
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session(session_id="session-disk")
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)
    todo_path = tmp_path / "todo.json"
    todo_path.write_text(
        json.dumps(
            [
                {"id": "deepresearch", "status": "in_progress"},
                {"id": "write_html", "status": "pending", "content": "写汇报"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with patch(
        "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
        "deepresearch_todo_path",
        return_value=todo_path,
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    saved = json.loads(todo_path.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "completed"
    assert saved[1]["status"] == "in_progress"
    assert "write_html" in ctx.inputs.tool_result["content"]


@pytest.mark.asyncio
async def test_execution_rail_uses_agent_workspace_todo_path(tmp_path):
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    session = _Session(session_id="session-agent")
    todo_path = tmp_path / "session-agent" / "todo.json"
    todo_path.parent.mkdir(parents=True)
    todo_path.write_text(
        json.dumps(
            [
                {"id": "deepresearch", "status": "in_progress"},
                {"id": "next_task", "status": "pending"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(
            workspace=SimpleNamespace(get_node_path=lambda _node: tmp_path)
        )
    )
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result, session=session)
    ctx.agent = agent

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    saved = json.loads(todo_path.read_text(encoding="utf-8"))
    assert saved[1]["status"] == "in_progress"
    assert "next_task" in ctx.inputs.tool_result["content"]


@pytest.mark.asyncio
async def test_execution_rail_uses_active_task_binding_for_custom_research_id():
    """Model-chosen ids (deep_research) must resolve via TaskExecutionRail binding.

    Regression for the 2026-09-08 rerun: todo id was ``deep_research``; name
    matching missed it, the handoff pointed at the research todo itself and
    the model re-asked the user instead of moving on to PPT.
    """
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)
    todos = [
        {"id": "deep_research", "status": "in_progress", "content": "深度研究"},
        {"id": "generate_ppt", "status": "pending", "content": "生成 PPT"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ) as save_todos,
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "get_current_task_id",
            return_value="todo:deep_research",
        ),
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    assert todos[0]["status"] == "completed"
    assert todos[1]["status"] == "in_progress"
    save_todos.assert_called_once()
    handoff = ctx.inputs.tool_result["content"]
    assert "generate_ppt" in handoff
    assert "todo_modify" in handoff
    assert "「deep_research: 深度研究」" not in handoff


@pytest.mark.asyncio
async def test_execution_rail_falls_back_to_single_in_progress_research_todo():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)
    todos = [
        {"id": "research_banks", "status": "in_progress"},
        {"id": "make_slides", "status": "pending"},
    ]

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=todos,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_save_todo_items",
            return_value=True,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "get_current_task_id",
            return_value=None,
        ),
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == []
    assert todos[0]["status"] == "completed"
    assert todos[1]["status"] == "in_progress"
    assert "make_slides" in ctx.inputs.tool_result["content"]


@pytest.mark.asyncio
async def test_execution_rail_force_finishes_when_custom_research_todo_is_last():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "_load_todo_items",
            return_value=[{"id": "deep_research", "status": "in_progress"}],
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "get_current_task_id",
            return_value="todo:deep_research",
        ),
    ):
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_requests == [
        {"output": "研究报告已生成并交付。", "result_type": "answer"}
    ]


@pytest.mark.asyncio
async def test_execution_rail_binds_request_id_for_nested_options_llm():
    result = {
        "schema_version": de.EXECUTION_SCHEMA,
        "kind": "completed",
        "content": "研究报告已生成并交付。",
        "state": {"schema_version": 1, "phase": "completed", "revision": 5},
    }
    rail = DeepResearchExecutionRail(model_provider=lambda: None)
    ctx = _rail_ctx(result=result)

    with (
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "extract_session_id_from_callback",
            return_value="session-1",
        ),
        patch(
            "jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail."
            "get_request_context",
            return_value={"request_id": "request-from-registry"},
        ),
    ):
        await rail.before_tool_call(ctx)

    assert de._execution_context.get().request_id == "request-from-registry"
    await rail.after_tool_call(ctx)
    assert de._execution_context.get() is None


@pytest.mark.asyncio
async def test_same_outer_tool_call_survives_multiple_native_interrupts():
    session = _Session()
    rail = DeepResearchExecutionRail(model_provider=lambda: None)

    sdk_questions = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "1. 重点研究哪些品类？"},
    }

    first = _rail_ctx(result=None, session=session)
    await rail.before_tool_call(first)
    start_sdk = AsyncMock(return_value=json.dumps(sdk_questions, ensure_ascii=False))
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=start_sdk,
    ):
        first.inputs.tool_result = await de.deepresearch_execute._func(
            query="研究智能家电竞争格局",
            file_name="智能家电报告",
        )
    await rail.after_tool_call(first)
    assert isinstance(first.inputs.tool_result, ToolInterruptException)
    first_interaction_id = first.inputs.tool_result.tool_call.id
    assert first_interaction_id != "call-1"
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY]["call-1"]["phase"] == (
        "wait_feedback"
    )
    assert first.inputs.tool_result.request.questions[0]["question"] == (
        "重点研究哪些品类？"
    )

    feedback_answer = {
        "status": "answered",
        "answers": [
            {
                "question": "重点研究哪些品类？",
                "selected_options": ["空调与冰箱"],
            }
        ],
    }
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
        "report_chars": 321,
    }
    second = _rail_ctx(
        result=None,
        session=session,
        resume_input=feedback_answer,
        tool_call_id=first_interaction_id,
    )
    await rail.before_tool_call(second)
    with patch.object(
        de,
        "_call_deepresearch_stream_impl",
        new=AsyncMock(return_value=json.dumps(completed, ensure_ascii=False)),
    ):
        second.inputs.tool_result = await de.deepresearch_execute._func(
            query="研究智能家电竞争格局",
            file_name="智能家电报告",
        )
    await rail.after_tool_call(second)

    assert second.force_finish_requests[0]["result_type"] == "answer"
    assert "321" in second.force_finish_requests[0]["output"]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}
