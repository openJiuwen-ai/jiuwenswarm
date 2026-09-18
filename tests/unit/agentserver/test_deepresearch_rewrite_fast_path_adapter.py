from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolMessage,
    UsageMetadata,
    UserMessage,
)

from jiuwenclaw.agentserver.deep_agent import interface_deep as interface_module
from jiuwenclaw.agentserver.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
)
from jiuwenclaw.agentserver.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlFollowupResult,
    RewriteHtmlTarget,
)
from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter
from jiuwenclaw.schema.agent import AgentRequest


_PREPARED = {
    "status": "prepared",
    "context_token": "context-token",
    "action": "polish",
    "units": [
        {
            "unit_id": "unit_1",
            "slots": [{"slot_id": "slot_1", "text": "原句。"}],
        }
    ],
    "readonly_context": {"previous_unit": None, "next_unit": None},
    "instruction": "",
    "allowed_source_ids": [],
    "citation_evidence": [],
}
_STRUCTURED_RESULT = {
    "units": [
        {
            "unit_id": "unit_1",
            "slots": [{"slot_id": "slot_1", "text": "改写后的句子。"}],
        }
    ],
    "facts_added": False,
}
_COMPLETED = {"status": "completed", "report_delivered": True}


def _json_result(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _query(action: str = "polish") -> str:
    payload = {
        "report_path": "/workspace/report.md",
        "action": action,
        "selection": {
            "protocol_version": 2,
            "start_byte": 0,
            "end_byte": 9,
            "selected_text": "原句。",
            "source_sha256": "0" * 64,
        },
        "instruction": "",
    }
    return (
        "<deepresearch_rewrite_request>"
        f"{_json_result(payload)}"
        "</deepresearch_rewrite_request>"
    )


def _result(
    *,
    status: str = "completed",
    error_code: str | None = None,
    message: str = (
        "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
        "如需继续改写，可直接选择下一处内容。"
    ),
    usage_metadata: object | None = None,
    model_calls: int = 1,
    model_output_adjustments: tuple[str, ...] = (),
    model_output_error_reason: str | None = None,
    commit_result: dict | None = None,
) -> RewriteFastPathResult:
    if usage_metadata is None and model_calls:
        usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        }
    if commit_result is None and status == "completed":
        commit_result = {
            "status": "completed",
            "report_delivered": True,
            "report_path": "/workspace/report.rewrite.md",
            "revision_id": "rev_child",
        }
    return RewriteFastPathResult(
        recognized=True,
        status=status,
        action="polish",
        error_code=error_code,
        message=message,
        usage_metadata=usage_metadata,
        prepare_ms=1.0,
        model_ms=20.0,
        commit_ms=2.0,
        total_ms=23.0,
        model_calls=model_calls,
        model_output_adjustments=model_output_adjustments,
        model_output_error_reason=model_output_error_reason,
        commit_result=commit_result,
    )


@pytest.mark.asyncio
async def test_adapter_fast_path_ignores_plain_query_without_dependencies():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._model = SimpleNamespace(invoke=AsyncMock())
    adapter._build_deepresearch_rewrite_model = Mock()

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_prepare_rewrite._func",
        new=AsyncMock(),
    ) as prepare, patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_commit_rewrite._func",
        new=AsyncMock(),
    ) as commit:
        result = await adapter._try_deepresearch_rewrite_fast_path("普通消息")

    assert result is None
    prepare.assert_not_awaited()
    adapter._build_deepresearch_rewrite_model.assert_not_called()
    adapter._model.invoke.assert_not_awaited()
    commit.assert_not_awaited()


def test_rewrite_model_uses_each_request_overlay_over_reloaded_config():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    reloaded_config = {
        "models": {
            "default": {
                "model_client_config": {
                    "api_base": "https://example.com/compatible-mode/v1",
                    "api_key": "bad-key",
                    "model_name": "your-model-name",
                    "client_provider": "OpenAI",
                    "timeout": 1800,
                    "verify_ssl": False,
                },
                "model_config_obj": {"temperature": 0.6},
            }
        }
    }

    first_token = interface_module.bind_task_env_overlay({
        "MODEL_NAME": "glm-5.2",
        "MODEL_PROVIDER": "OpenAI",
        "API_BASE": "https://maas.internal/v2",
        "API_KEY": "first-tenant-key",
    })
    try:
        with patch.object(interface_module, "get_config", return_value=reloaded_config):
            first_model = adapter._build_deepresearch_rewrite_model()
    finally:
        interface_module.reset_task_env_overlay(first_token)

    second_token = interface_module.bind_task_env_overlay({
        "MODEL_NAME": "glm-6",
        "MODEL_PROVIDER": "OpenAI",
        "API_BASE": "https://maas-next.internal/v2",
        "API_KEY": "second-tenant-key",
    })
    try:
        with patch.object(interface_module, "get_config", return_value=reloaded_config):
            second_model = adapter._build_deepresearch_rewrite_model()
    finally:
        interface_module.reset_task_env_overlay(second_token)

    assert first_model.model_config.model_name == "glm-5.2"
    assert first_model.model_client_config.api_base == "https://maas.internal/v2"
    assert first_model.model_client_config.api_key == "first-tenant-key"
    assert second_model.model_config.model_name == "glm-6"
    assert second_model.model_client_config.api_base == "https://maas-next.internal/v2"
    assert second_model.model_client_config.api_key == "second-tenant-key"
    assert first_model.model_config.model_name == "glm-5.2"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["polish", "expand", "shorten"])
async def test_adapter_fast_path_uses_private_request_model(action):
    model_response = SimpleNamespace(
        content=_json_result(_STRUCTURED_RESULT),
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    shared_model = SimpleNamespace(
        invoke=AsyncMock(side_effect=AssertionError("shared model must not be used"))
    )
    request_model = SimpleNamespace(invoke=AsyncMock(return_value=model_response))
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._model = shared_model
    adapter._model_cache = {"your-model-name": shared_model}
    adapter._build_deepresearch_rewrite_model = Mock(return_value=request_model)

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_prepare_rewrite._func",
        new=AsyncMock(return_value=_json_result({**_PREPARED, "action": action})),
    ) as prepare, patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_commit_rewrite._func",
        new=AsyncMock(return_value=_json_result(_COMPLETED)),
    ) as commit:
        result = await adapter._try_deepresearch_rewrite_fast_path(_query(action))

    assert result is not None
    assert result.status == "completed"
    assert result.model_calls == 1
    assert result.usage_metadata == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    prepare.assert_awaited_once()
    adapter._build_deepresearch_rewrite_model.assert_called_once_with()
    request_model.invoke.assert_awaited_once()
    shared_model.invoke.assert_not_awaited()
    commit.assert_awaited_once()
    assert adapter._model is shared_model
    assert adapter._model_cache == {"your-model-name": shared_model}


@pytest.mark.asyncio
async def test_adapter_fast_path_reuses_private_request_model_for_retry():
    model_responses = [
        SimpleNamespace(content="not json", usage_metadata=None),
        SimpleNamespace(
            content=_json_result(_STRUCTURED_RESULT),
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        ),
    ]
    shared_model = SimpleNamespace(
        invoke=AsyncMock(side_effect=AssertionError("shared model must not be used"))
    )
    request_model = SimpleNamespace(invoke=AsyncMock(side_effect=model_responses))
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._model = shared_model
    adapter._build_deepresearch_rewrite_model = Mock(return_value=request_model)

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_prepare_rewrite._func",
        new=AsyncMock(return_value=_json_result(_PREPARED)),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_commit_rewrite._func",
        new=AsyncMock(return_value=_json_result(_COMPLETED)),
    ):
        result = await adapter._try_deepresearch_rewrite_fast_path(_query())

    assert result is not None and result.status == "completed"
    assert result.model_calls == 2
    adapter._build_deepresearch_rewrite_model.assert_called_once_with()
    assert request_model.invoke.await_count == 2
    shared_model.invoke.assert_not_awaited()


def test_adapter_fast_path_success_chunk_uses_fixed_invitation():
    chunks = JiuWenClawDeepAdapter._fast_path_chunks(
        _result(),
        request_id="request-1",
        channel_id="web",
    )

    assert len(chunks) == 1
    assert chunks[0].payload == {
        "event_type": "chat.final",
        "content": (
            "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
            "如需继续改写，可直接选择下一处内容。"
        ),
        "is_complete": False,
    }
    assert chunks[0].is_complete is False


def test_adapter_fast_path_terminal_error_chunk_marks_protocol_error():
    chunks = JiuWenClawDeepAdapter._fast_path_chunks(
        _result(
            status="error",
            error_code="MODEL_OUTPUT_INVALID",
            message="invalid structured rewrite result",
        ),
        request_id="request-1",
        channel_id="web",
    )

    assert len(chunks) == 1
    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": "改写失败（MODEL_OUTPUT_INVALID）：invalid structured rewrite result",
    }
    assert "原句" not in chunks[0].payload["error"]
    assert chunks[0].is_complete is False


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        ("MODEL_CALL_TIMEOUT", "rewrite model call timed out"),
        ("REWRITE_TIMEOUT", "rewrite task timed out"),
    ],
)
def test_adapter_fast_path_timeout_chunk_marks_protocol_error(
    error_code,
    message,
):
    chunks = JiuWenClawDeepAdapter._fast_path_chunks(
        _result(
            status="error",
            error_code=error_code,
            message=message,
        ),
        request_id="request-1",
        channel_id="web",
    )

    assert len(chunks) == 1
    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": f"改写失败（{error_code}）：{message}",
    }
    assert chunks[0].is_complete is False


def test_adapter_fast_path_delivery_failure_does_not_claim_standard_success():
    chunks = JiuWenClawDeepAdapter._fast_path_chunks(
        _result(
            status="completed",
            error_code="REPORT_DELIVERY_FAILED",
            message="改写版本已成功保留，但报告文件交付失败。",
        ),
        request_id="request-1",
        channel_id="web",
    )

    assert chunks[0].payload == {
        "event_type": "chat.final",
        "content": "改写版本已成功保留，但报告文件交付失败。",
        "is_complete": False,
    }
    assert "生成 HTML" not in chunks[0].payload["content"]


def _stream_adapter(fast_path_result: RewriteFastPathResult | None):
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace()
    adapter._model = SimpleNamespace(
        model_config=SimpleNamespace(model_name="test-model"),
    )
    adapter._stream_event_rail = None
    adapter._telemetry_rail = None
    adapter._request_summary_rail = None
    adapter._skill_evolution_rail = None
    adapter._try_skill_turbo_resume = AsyncMock(return_value=None)
    adapter._has_valid_model_config = Mock(return_value=True)
    adapter._on_chat_request_start = AsyncMock(
        return_value=(None, None, None, None, None)
    )
    adapter._on_chat_request_end = AsyncMock()
    adapter._plain_chat_should_clear_stale_interrupt = Mock(return_value=False)
    adapter._handle_slash_command = AsyncMock(return_value=None)
    adapter._bind_runtime_cron_context = Mock(return_value=())
    adapter._reset_runtime_cron_context = Mock()
    adapter._tenant_disk_ids = Mock(return_value=("default", "default"))
    adapter._update_runtime_config = AsyncMock()
    adapter._try_deepresearch_rewrite_html_followup = AsyncMock(
        return_value=None
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(
        return_value=fast_path_result
    )
    adapter._persist_deepresearch_rewrite_fast_path_turn = AsyncMock(
        return_value=True
    )
    adapter._untrack_session_toolkit = Mock()
    adapter._cleanup_circuit_breaker_session = Mock()
    adapter._schedule_background_evolution_followup = Mock()
    return adapter


class _FakeContext:
    def __init__(self):
        self.messages = []

    async def add_messages(self, messages):
        if isinstance(messages, list):
            self.messages.extend(messages)
        else:
            self.messages.append(messages)


@pytest.mark.asyncio
async def test_persist_fast_path_turn_records_trusted_commit_tool_result():
    context = _FakeContext()
    react_agent = SimpleNamespace(
        _init_context=AsyncMock(return_value=context),
    )
    context_engine = SimpleNamespace(save_contexts=AsyncMock())
    session = SimpleNamespace(
        pre_run=AsyncMock(),
        post_run=AsyncMock(),
        get_session_id=Mock(return_value="session-1"),
        update_state=Mock(),
    )
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace(
        card=SimpleNamespace(id="main-agent"),
        react_agent=react_agent,
        context_engine=context_engine,
        loop_session=None,
    )
    adapter._checkpointer = SimpleNamespace()
    result = _result()

    with patch.object(
        interface_module,
        "_resolve_session_for_checkpoint",
        new=AsyncMock(return_value=(session, True)),
    ), patch.object(
        interface_module,
        "post_agent_execute_for_session",
        new=AsyncMock(),
    ) as flush:
        persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
            session_id="session-1",
            query=_query(),
            result=result,
        )

    assert persisted is True
    assert len(context.messages) == 4
    assert isinstance(context.messages[0], UserMessage)
    assert context.messages[0].content == _query()

    tool_call_message = context.messages[1]
    assert isinstance(tool_call_message, AssistantMessage)
    assert len(tool_call_message.tool_calls) == 1
    tool_call = tool_call_message.tool_calls[0]
    assert tool_call.name == "deepresearch_commit_rewrite"
    assert tool_call.arguments == "{}"

    tool_result_message = context.messages[2]
    assert isinstance(tool_result_message, ToolMessage)
    assert tool_result_message.tool_call_id == tool_call.id
    trusted_result = json.loads(tool_result_message.content)
    assert trusted_result["status"] == "completed"
    assert trusted_result["report_path"] == "/workspace/report.rewrite.md"
    assert trusted_result["revision_id"] == "rev_child"

    assert isinstance(context.messages[3], AssistantMessage)
    assert context.messages[3].content == result.message
    session.update_state.assert_called_once_with({
        PENDING_HTML_EXPORT_STATE_KEY: {
            "schema_version": 1,
            "report_path": "/workspace/report.rewrite.md",
            "revision_id": "rev_child",
        }
    })
    react_agent._init_context.assert_awaited_once_with(session)
    context_engine.save_contexts.assert_awaited_once_with(session)
    flush.assert_awaited_once_with(session, adapter._checkpointer)
    session.pre_run.assert_awaited_once_with(inputs=None)
    session.post_run.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_persist_fast_path_turn_rejects_completed_result_without_html_target():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace(card=SimpleNamespace(id="main-agent"))
    adapter._checkpointer = SimpleNamespace()
    result = _result(commit_result={
        "status": "completed",
        "report_path": "/workspace/report.rewrite.md",
    })

    with patch.object(
        interface_module,
        "_resolve_session_for_checkpoint",
        new=AsyncMock(),
    ) as resolve:
        persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
            session_id="session-1",
            query=_query(),
            result=result,
        )

    assert persisted is False
    resolve.assert_not_awaited()


@asynccontextmanager
async def _request_scope(**_kwargs):
    yield


async def _collect_stream(adapter, query: str):
    request = AgentRequest(
        request_id="request-1",
        channel_id="web",
        session_id="session-1",
        params={"query": query, "mode": "agent.plan"},
        metadata={},
        is_stream=True,
    )
    chunks = []
    async for chunk in adapter.process_message_stream_impl(
        request,
        {"query": query, "conversation_id": "session-1"},
    ):
        chunks.append(chunk)
    return chunks


async def _run_nonstream(adapter, query: str):
    request = AgentRequest(
        request_id="request-1",
        channel_id="web",
        session_id="session-1",
        params={"query": query, "mode": "agent.plan"},
        metadata={},
        is_stream=False,
    )
    return await adapter.process_message_impl(
        request,
        {"query": query, "conversation_id": "session-1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fast_path_result",
    [
        _result(),
        _result(
            status="error",
            error_code="REVISION_CONFLICT",
            message="the report revision changed",
            usage_metadata=None,
            model_calls=0,
        ),
    ],
)
async def test_process_stream_skips_runner_for_recognized_fast_path(
    monkeypatch,
    fast_path_result,
):
    adapter = _stream_adapter(fast_path_result)
    runner_calls = []

    async def empty_runner(*_args, **_kwargs):
        runner_calls.append("runner")
        if False:
            yield None

    monkeypatch.setattr(
        interface_module.Runner,
        "run_agent_streaming",
        empty_runner,
    )
    monkeypatch.setattr(
        interface_module,
        "ask_user_question_request_scope",
        _request_scope,
    )
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "mark_request_first_byte", Mock())

    chunks = await _collect_stream(adapter, _query())

    assert runner_calls == []
    event_types = [chunk.payload["event_type"] for chunk in chunks if chunk.payload]
    assert event_types[0] == (
        "chat.final"
        if fast_path_result.status == "completed"
        else "chat.error"
    )
    assert ("chat.usage_summary" in event_types) is bool(
        fast_path_result.usage_metadata
    )
    if fast_path_result.status == "completed":
        adapter._persist_deepresearch_rewrite_fast_path_turn.assert_awaited_once_with(
            session_id="session-1",
            query=_query(),
            result=fast_path_result,
        )
    else:
        adapter._persist_deepresearch_rewrite_fast_path_turn.assert_not_awaited()
    assert sum(chunk.is_complete for chunk in chunks) == 1


@pytest.mark.asyncio
async def test_html_followup_loads_target_through_adapter_checkpointer():
    target_state = {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    session = SimpleNamespace(
        _inner=object(),
        get_state=Mock(return_value=target_state),
        pre_run=AsyncMock(),
    )
    checkpointer = SimpleNamespace(pre_agent_execute=AsyncMock())
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._instance = SimpleNamespace(card=SimpleNamespace(id="main-agent"))
    adapter._checkpointer = checkpointer

    with patch.object(
        interface_module,
        "create_agent_session",
        return_value=session,
    ) as create_session:
        target = await adapter._load_deepresearch_rewrite_html_target("session-1")

    assert target == RewriteHtmlTarget(
        report_path="/workspace/report.rewrite.md",
        revision_id="rev_child",
    )
    create_session.assert_called_once_with(
        session_id="session-1",
        card=adapter._instance.card,
    )
    checkpointer.pre_agent_execute.assert_awaited_once_with(session._inner, None)
    session.get_state.assert_called_once_with(PENDING_HTML_EXPORT_STATE_KEY)
    session.pre_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_followup_invokes_existing_tool_once_with_restored_target():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._load_deepresearch_rewrite_html_target = AsyncMock(
        return_value=RewriteHtmlTarget(
            report_path="/workspace/report.rewrite.md",
            revision_id="rev_child",
        )
    )

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_generate_rewrite_html._func",
        new=AsyncMock(return_value=_json_result({
            "status": "completed",
            "html_delivered": True,
        })),
    ) as html_tool:
        result = await adapter._try_deepresearch_rewrite_html_followup(
            "请生成 HTML。",
            "session-1",
        )

    assert result == RewriteHtmlFollowupResult(
        status="completed",
        error_code=None,
        message="已生成美化后的 HTML。",
    )
    adapter._load_deepresearch_rewrite_html_target.assert_awaited_once_with(
        "session-1"
    )
    html_tool.assert_awaited_once_with(
        report_path="/workspace/report.rewrite.md",
        revision_id="rev_child",
    )


@pytest.mark.asyncio
async def test_html_followup_tool_failure_returns_fixed_safe_error():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._load_deepresearch_rewrite_html_target = AsyncMock(
        return_value=RewriteHtmlTarget(
            report_path="/workspace/report.rewrite.md",
            revision_id="rev_child",
        )
    )

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_generate_rewrite_html._func",
        new=AsyncMock(return_value=_json_result({
            "status": "error",
            "error_code": "HTML_DELIVERY_FAILED",
            "error": "/secret/path",
        })),
    ) as html_tool:
        result = await adapter._try_deepresearch_rewrite_html_followup(
            "生成 HTML",
            "session-1",
        )

    assert result == RewriteHtmlFollowupResult(
        status="error",
        error_code="HTML_DELIVERY_FAILED",
        message="HTML 生成失败，但 Markdown 改写版本仍然成功保留。",
    )
    assert "/secret/path" not in result.message
    html_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_html_followup_missing_target_returns_terminal_safe_error():
    adapter = object.__new__(JiuWenClawDeepAdapter)
    adapter._load_deepresearch_rewrite_html_target = AsyncMock(return_value=None)

    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch.rewrite_tools."
        "deepresearch_generate_rewrite_html._func",
        new=AsyncMock(),
    ) as html_tool:
        result = await adapter._try_deepresearch_rewrite_html_followup(
            "生成 HTML",
            "session-1",
        )

    assert result == RewriteHtmlFollowupResult(
        status="error",
        error_code="TARGET_UNAVAILABLE",
        message=(
            "未找到可生成 HTML 的已完成改写版本。"
            "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
        ),
    )
    html_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html_result",
    [
        RewriteHtmlFollowupResult(
            status="completed",
            error_code=None,
            message="已生成美化后的 HTML。",
        ),
        RewriteHtmlFollowupResult(
            status="error",
            error_code="TARGET_UNAVAILABLE",
            message=(
                "未找到可生成 HTML 的已完成改写版本。"
                "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
            ),
        ),
    ],
)
async def test_process_stream_skips_runner_for_html_followup(
    monkeypatch,
    html_result,
):
    adapter = _stream_adapter(None)
    adapter._try_deepresearch_rewrite_html_followup = AsyncMock(
        return_value=html_result
    )
    runner_calls = []

    async def empty_runner(*_args, **_kwargs):
        runner_calls.append("runner")
        if False:
            yield None

    monkeypatch.setattr(
        interface_module.Runner,
        "run_agent_streaming",
        empty_runner,
    )
    monkeypatch.setattr(
        interface_module,
        "ask_user_question_request_scope",
        _request_scope,
    )
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "mark_request_first_byte", Mock())

    chunks = await _collect_stream(adapter, "生成 HTML")

    assert runner_calls == []
    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()
    assert chunks[0].payload == {
        "event_type": "chat.final",
        "content": html_result.message,
        "is_complete": False,
    }
    assert sum(chunk.is_complete for chunk in chunks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html_result", "expected_ok"),
    [
        (
            RewriteHtmlFollowupResult(
                status="completed",
                error_code=None,
                message="已生成美化后的 HTML。",
            ),
            True,
        ),
        (
            RewriteHtmlFollowupResult(
                status="error",
                error_code="TARGET_UNAVAILABLE",
                message=(
                    "未找到可生成 HTML 的已完成改写版本。"
                    "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
                ),
            ),
            False,
        ),
    ],
)
async def test_process_nonstream_skips_runner_for_html_followup(
    monkeypatch,
    html_result,
    expected_ok,
):
    adapter = _stream_adapter(None)
    adapter._try_deepresearch_rewrite_html_followup = AsyncMock(
        return_value=html_result
    )
    run_agent = AsyncMock()
    monkeypatch.setattr(interface_module.Runner, "run_agent", run_agent)
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())

    response = await _run_nonstream(adapter, "生成 HTML")

    run_agent.assert_not_awaited()
    assert response.ok is expected_ok
    assert response.payload == {"content": html_result.message}


@pytest.mark.asyncio
async def test_process_stream_accounts_for_structured_fast_path_usage(monkeypatch):
    usage = UsageMetadata(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cache_tokens=10,
    )
    adapter = _stream_adapter(_result(usage_metadata=usage))

    async def empty_runner(*_args, **_kwargs):
        if False:
            yield None

    monkeypatch.setattr(
        interface_module.Runner,
        "run_agent_streaming",
        empty_runner,
    )
    monkeypatch.setattr(
        interface_module,
        "ask_user_question_request_scope",
        _request_scope,
    )
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "mark_request_first_byte", Mock())

    chunks = await _collect_stream(adapter, _query())

    usage_summaries = [
        chunk.payload
        for chunk in chunks
        if chunk.payload
        and chunk.payload.get("event_type") == "chat.usage_summary"
    ]
    assert usage_summaries
    assert usage_summaries[0]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_tokens": 10,
    }


@pytest.mark.asyncio
async def test_process_stream_logs_fast_path_output_diagnostics(monkeypatch):
    adapter = _stream_adapter(
        _result(
            model_calls=2,
            model_output_adjustments=("json_fence", "slot_metadata"),
        )
    )

    async def empty_runner(*_args, **_kwargs):
        if False:
            yield None

    monkeypatch.setattr(
        interface_module.Runner,
        "run_agent_streaming",
        empty_runner,
    )
    monkeypatch.setattr(
        interface_module,
        "ask_user_question_request_scope",
        _request_scope,
    )
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "mark_request_first_byte", Mock())
    log_info = Mock()
    monkeypatch.setattr(interface_module.logger, "info", log_info)

    await _collect_stream(adapter, _query())

    fast_path_logs = [
        call
        for call in log_info.call_args_list
        if call.args
        and call.args[0].startswith("[DeepResearchRewriteFastPath]")
    ]
    assert len(fast_path_logs) == 1
    assert fast_path_logs[0].args[-2:] == (
        "json_fence,slot_metadata",
        None,
    )


@pytest.mark.asyncio
async def test_process_stream_keeps_runner_for_plain_message(monkeypatch):
    adapter = _stream_adapter(None)
    runner_calls = []

    async def empty_runner(*_args, **_kwargs):
        runner_calls.append("runner")
        if False:
            yield None

    monkeypatch.setattr(
        interface_module.Runner,
        "run_agent_streaming",
        empty_runner,
    )
    monkeypatch.setattr(
        interface_module,
        "ask_user_question_request_scope",
        _request_scope,
    )
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "mark_request_first_byte", Mock())

    chunks = await _collect_stream(adapter, "普通消息")

    assert runner_calls == ["runner"]
    assert sum(chunk.is_complete for chunk in chunks) == 1
