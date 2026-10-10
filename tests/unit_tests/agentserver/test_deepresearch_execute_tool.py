"""Regression tests for deterministic DeepResearch orchestration."""

from __future__ import annotations

import asyncio
import base64
import json
import pickle
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
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
from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt


class _Session:
    def __init__(self):
        self.state: dict[str, object] = {}

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, values):
        self.state.update(values)


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
    requested_report_type=None,
    conversation_id="",
):
    saved: list[dict] = []
    token = de.bind_deepresearch_execution_context(
        tool_call_id="call-1",
        state=state,
        user_input=user_input,
        model=model,
        save_state=lambda value: saved.append(dict(value)),
        requested_report_type=requested_report_type,
    )
    try:
        result = await de.deepresearch_execute._func(
            query=query,
            file_name=file_name,
            conversation_id=conversation_id,
        )
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
async def test_new_query_persists_request_report_type_before_sdk_start():
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
        _, saved = await _invoke(requested_report_type="brief")

    assert saved[0]["phase"] == "starting"
    assert saved[0]["requested_report_type"] == "brief"
    assert stream.await_args.kwargs["report_type"] == "brief"


@pytest.mark.asyncio
async def test_execute_claims_interrupted_conversation_with_cached_outline():
    """传入 conversation_id 且缓存有大纲 → 跳过 start 重跑，直接呈现大纲审阅卡片。"""
    outline = {
        "title": "智能家电竞争格局研究",
        "sections": [
            {"id": "1", "title": "市场规模", "is_core_section": True, "description": "整体规模"},
            {"id": "2", "title": "主要玩家"},
        ],
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S1")
    try:
        dt._cache_outline_json(dt._get_route(), "C-OLD", outline)
        with patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream:
            result, saved = await _invoke(conversation_id="C-OLD")
    finally:
        dt.reset_deepresearch_route(route_token)
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()

    assert result["kind"] == "interaction"
    stream.assert_not_awaited()
    final = saved[-1]
    assert final["phase"] == "wait_outline"
    assert final["conversation_id"] == "C-OLD"
    assert final["outline_presented"] is True
    question = result["interaction"]["questions"][0]
    assert [option["label"] for option in question["options"]] == [
        "确认大纲，继续研究",
        "需要修改",
    ]
    assert "### P1: 市场规模（重点）" in question["preview"]["text"]
    assert "### P2: 主要玩家" in question["preview"]["text"]


@pytest.mark.asyncio
async def test_execute_claim_without_cached_outline_restarts_with_fresh_id():
    """认领失败（缓存无大纲）→ 换全新 conversation_id 从头执行，绝不复用旧 id 发起新跑。"""
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S2")
    try:
        with patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ) as stream:
            result, saved = await _invoke(conversation_id="C-GONE")
    finally:
        dt.reset_deepresearch_route(route_token)

    assert result["kind"] == "error"
    assert saved[-1]["phase"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-GONE"


def _encode_checkpoint_blob(value):
    return "__BYTES__:" + base64.b64encode(pickle.dumps(value)).decode("ascii")


def _write_checkpoint_db(
    root,
    conversation_id,
    *,
    node,
    node_status="__interrupt__",
    questions="",
    outline=None,
):
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    connection = sqlite3.connect(str(data_dir / "checkpointer.db"))
    try:
        connection.execute(
            "CREATE TABLE kv_store (key VARCHAR(255), value VARCHAR(4096))"
        )
        graph_state = SimpleNamespace(
            pending_node={node: SimpleNamespace(status=node_status)}
        )
        search_context = {}
        if questions:
            search_context["questions"] = questions
        if outline is not None:
            search_context["current_outline"] = outline
        workflow_state = {"global_state": {"search_context": search_context}}
        connection.executemany(
            "INSERT INTO kv_store (key, value) VALUES (?, ?)",
            [
                (
                    f"{conversation_id}:workflow-graph:research_workflow:checkpoint_data_value",
                    _encode_checkpoint_blob(graph_state),
                ),
                (
                    f"{conversation_id}:workflow:research_workflow:workflow_state_blobs",
                    _encode_checkpoint_blob(workflow_state),
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_probe_resumable_checkpoint_reads_feedback_stage(tmp_path):
    """停研究问题阶段的会话 → 探测出节点与按段落切分的问题清单。"""
    _write_checkpoint_db(
        tmp_path,
        "C-FB",
        node="feedback_handler",
        questions="1. 重点研究哪些品类？\n第一问的补充限定\n\n2. 覆盖哪些市场？",
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        probe = dt._probe_resumable_checkpoint("C-FB")

    assert probe == {
        "node": "feedback_handler",
        "questions": ["1. 重点研究哪些品类？\n第一问的补充限定", "2. 覆盖哪些市场？"],
    }


def test_probe_resumable_checkpoint_reads_outline_stage(tmp_path):
    """停大纲确认阶段的会话 → 探测出节点与工具层大纲 JSON。"""
    outline = SimpleNamespace(
        title="智能家电竞争格局研究",
        thought="先规模后玩家",
        sections=[
            SimpleNamespace(title="市场规模", is_core_section=True),
            SimpleNamespace(title="主要玩家", is_core_section=False),
        ],
    )
    _write_checkpoint_db(
        tmp_path,
        "C-OL",
        node="outline_interaction",
        questions="1. 问题",
        outline=outline,
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        probe = dt._probe_resumable_checkpoint("C-OL")

    assert probe == {
        "node": "outline_interaction",
        "questions": ["1. 问题"],
        "outline_json": {
            "title": "智能家电竞争格局研究",
            "thought": "先规模后玩家",
            "sections": [
                {"title": "市场规模", "is_core_section": True},
                {"title": "主要玩家", "is_core_section": False},
            ],
        },
    }


def test_probe_resumable_checkpoint_returns_none_without_resumable_interrupt(tmp_path):
    """非中断状态 / 会话不存在 / db 不存在 → 均探测不到，认领退回新跑。"""
    _write_checkpoint_db(
        tmp_path,
        "C-ERR",
        node="feedback_handler",
        node_status="__error__",
        questions="1. 问题",
    )

    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert dt._probe_resumable_checkpoint("C-ERR") is None
        assert dt._probe_resumable_checkpoint("C-UNKNOWN") is None
    (tmp_path / "data" / "checkpointer.db").unlink()
    with patch.object(dt, "_resolve_skill_root", return_value=str(tmp_path)):
        assert dt._probe_resumable_checkpoint("C-ERR") is None
    with patch.object(dt, "_resolve_skill_root", return_value=""):
        assert dt._probe_resumable_checkpoint("C-ERR") is None


@pytest.mark.asyncio
async def test_execute_claims_feedback_stage_from_checkpoint_probe():
    """checkpointer.db 探测到停研究问题阶段 → 出反馈卡，state 进入 wait_feedback。"""
    probe = {
        "node": "feedback_handler",
        "questions": ["1. 重点研究哪些品类？", "2. 覆盖哪些市场？"],
    }
    model = _Model(_option_payload("重点研究哪些品类？", "覆盖哪些市场？"))
    with (
        patch.object(
            de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)
        ) as probe_mock,
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(model=model, conversation_id="C-FB")

    probe_mock.assert_called_once_with("C-FB")
    stream.assert_not_awaited()
    assert result["kind"] == "interaction"
    assert result["interaction"]["query"] == "请回答以下研究主题澄清问题"
    cards = result["interaction"]["questions"]
    assert [card["question"] for card in cards] == [
        "重点研究哪些品类？",
        "覆盖哪些市场？",
    ]
    assert all(len(card["options"]) == 2 for card in cards)
    final = saved[-1]
    assert final["phase"] == "wait_feedback"
    assert final["conversation_id"] == "C-FB"
    assert final["questions"] == ["重点研究哪些品类？", "覆盖哪些市场？"]


@pytest.mark.asyncio
async def test_execute_claims_outline_stage_from_checkpoint_probe():
    """checkpointer.db 探测到停大纲确认 → 无需内存缓存直接出审阅卡（跨重启认领）。"""
    probe = {
        "node": "outline_interaction",
        "outline_json": {
            "title": "智能家电竞争格局研究",
            "thought": "先规模后玩家",
            "sections": [
                {"title": "市场规模", "is_core_section": True},
                {"title": "主要玩家", "is_core_section": False},
            ],
        },
    }
    with (
        patch.object(de, "_probe_resumable_checkpoint", new=Mock(return_value=probe)),
        patch.object(de, "_call_deepresearch_stream_impl", new=AsyncMock()) as stream,
    ):
        result, saved = await _invoke(conversation_id="C-OL")

    stream.assert_not_awaited()
    assert result["kind"] == "interaction"
    final = saved[-1]
    assert final["phase"] == "wait_outline"
    assert final["conversation_id"] == "C-OL"
    assert final["outline_presented"] is True
    question = result["interaction"]["questions"][0]
    assert "**研究思路**：先规模后玩家" in question["preview"]["text"]
    assert "### P1: 市场规模（重点）" in question["preview"]["text"]
    assert "### P2: 主要玩家" in question["preview"]["text"]


@pytest.mark.asyncio
async def test_execute_restarts_fresh_when_checkpoint_lacks_questions():
    """db 指向研究问题阶段但取不到问题清单 → 认领失败，换全新 id 重跑。"""
    outcome = {
        "status": "error",
        "error_code": "runner_failed",
        "error": "boom",
    }
    route_token = dt.push_deepresearch_route("R1", "CH1", "S3")
    try:
        with (
            patch.object(
                de,
                "_probe_resumable_checkpoint",
                new=Mock(return_value={"node": "feedback_handler"}),
            ),
            patch.object(
                de,
                "_call_deepresearch_stream_impl",
                new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
            ) as stream,
        ):
            result, saved = await _invoke(conversation_id="C-FB2")
    finally:
        dt.reset_deepresearch_route(route_token)

    assert result["kind"] == "error"
    assert saved[-1]["phase"] == "error"
    stream.assert_awaited_once()
    assert stream.await_args.kwargs["action"] == "start"
    new_cid = stream.await_args.kwargs["conversation_id"]
    assert new_cid and new_cid != "C-FB2"


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
async def test_option_generation_timeout_keeps_free_text_questions():
    questions = ["重点研究哪些品类？", "覆盖哪些市场？"]
    outcome = {
        "status": "interrupted",
        "conversation_id": "conversation-1",
        "node_id": "feedback_handler",
        "marker": {"questions": "\n".join(questions)},
    }

    async def never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    model = SimpleNamespace(invoke=never_returns)
    with (
        patch.object(
            de,
            "_OPTIONS_GENERATION_TIMEOUT_SECONDS",
            0.01,
        ),
        patch.object(
            de,
            "_call_deepresearch_stream_impl",
            new=AsyncMock(return_value=json.dumps(outcome, ensure_ascii=False)),
        ),
    ):
        result, saved = await asyncio.wait_for(_invoke(model=model), timeout=0.2)

    assert result["kind"] == "interaction"
    assert [item["question"] for item in result["interaction"]["questions"]] == questions
    assert [item["options"] for item in result["interaction"]["questions"]] == [[], []]
    assert saved[-1]["phase"] == "wait_feedback"


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
        "requested_report_type": "brief",
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
        result, saved = await _invoke(
            state=state,
            user_input=answer,
            requested_report_type="professional",
        )

    assert result["kind"] == "completed"
    assert "✅ **深度研究已完成！**" in result["content"]
    assert "智能家电报告已成功生成并交付" in result["content"]
    assert saved[0]["phase"] == "resuming_feedback"
    assert saved[-1]["phase"] == "completed"
    assert stream.await_count == 1
    assert stream.await_args.kwargs["action"] == "resume"
    assert stream.await_args.kwargs["node"] == "feedback_handler"
    assert stream.await_args.kwargs["report_type"] == "brief"
    assert "空调与冰箱" in stream.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_legacy_feedback_state_resumes_with_native_auto_report_type():
    state = {
        "schema_version": 1,
        "phase": "wait_feedback",
        "query": "q",
        "file_name": "r",
        "conversation_id": "conversation-1",
        "questions": ["继续吗？"],
        "revision": 3,
    }
    answer = {
        "status": "answered",
        "answers": [{"question": "继续吗？", "selected_options": ["继续"]}],
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
        await _invoke(
            state=state,
            user_input=answer,
            requested_report_type="brief",
        )

    assert stream.await_args.kwargs["report_type"] is None


@pytest.mark.asyncio
async def test_completion_warns_when_html_style_falls_back():
    completed = {
        "status": "completed",
        "conversation_id": "conversation-1",
        "report_delivered": True,
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
        "requested_report_type": "professional",
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
            state=interaction["state"],
            user_input=outline_answer,
            requested_report_type="brief",
        )

    assert result["kind"] == "completed"
    assert "1. 市场格局" in result["content"]
    assert saved[0]["phase"] == "resuming_outline"
    assert stream.await_args.kwargs["node"] == "outline_interaction"
    assert stream.await_args.kwargs["report_type"] == "professional"
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
    assert ctx.inputs.tool_msg is None


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
    assert "✅ **深度研究已完成！**" in second.force_finish_requests[0]["output"]
    assert session.state[DEEPRESEARCH_EXECUTION_STATE_KEY] == {}
    assert session.state[DEEPRESEARCH_EXECUTION_ALIAS_KEY] == {}
