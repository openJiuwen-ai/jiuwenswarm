# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _FakeTodoTool:
    async def load_todos(self, session_id: str):
        assert session_id == "sess-1"
        return []


class _FakeSession:
    def __init__(self):
        self.outputs = []

    async def write_stream(self, output):
        self.outputs.append(output)


class _TestRail(JiuSwarmStreamEventRail):
    def install_todo_tool(self, tool):
        self._main_todo_tool = tool

    async def emit_todo_updated(self, session, session_id: str):
        await self._emit_todo_updated(session, session_id)

    async def emit_ask_user_question_if_interrupted(
        self,
        session,
        tool_call,
        tool_name,
        result,
        exception=None,
    ):
        await self._emit_ask_user_question_if_interrupted(
            session,
            tool_call,
            tool_name,
            result,
            exception,
        )

    async def emit_context_usage(self, ctx):
        await self._emit_context_usage(ctx)


@pytest.mark.asyncio
async def test_empty_todo_list_is_emitted_to_clear_frontend():
    rail = _TestRail()
    rail.install_todo_tool(_FakeTodoTool())
    session = _FakeSession()

    await rail.emit_todo_updated(session, "sess-1")

    assert len(session.outputs) == 1
    output = session.outputs[0]
    assert output.type == "todo.updated"
    assert output.payload == {"todos": []}


@pytest.mark.asyncio
async def test_context_usage_reports_input_tokens_instead_of_reply_total(monkeypatch):
    class _UsageMetadata:
        @staticmethod
        def model_dump():
            return {
                "input_tokens": 1200,
                "output_tokens": 800,
                "total_tokens": 2000,
            }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.ContextUtils.resolve_context_max",
        lambda **_kwargs: 10000,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        agent=None,
        inputs=SimpleNamespace(
            response=SimpleNamespace(usage_metadata=_UsageMetadata()),
        ),
    )

    await _TestRail().emit_context_usage(ctx)

    assert len(session.outputs) == 1
    output = session.outputs[0]
    assert output.type == "context.usage"
    assert output.payload == {
        "rate": 12.0,
        "context_max": 10000,
        "tokens_used": 1200,
        "input_tokens": 1200,
        "output_tokens": 800,
        "total_tokens": 2000,
        "usage_source": "usage_metadata",
    }


@pytest.mark.asyncio
async def test_context_usage_keeps_zero_input_tokens_instead_of_falling_back(monkeypatch):
    class _UsageMetadata:
        @staticmethod
        def model_dump():
            return {
                "input_tokens": 0,
                "total_tokens": 800,
            }

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.ContextUtils.resolve_context_max",
        lambda **_kwargs: 10000,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        agent=None,
        inputs=SimpleNamespace(
            response=SimpleNamespace(usage_metadata=_UsageMetadata()),
        ),
    )

    await _TestRail().emit_context_usage(ctx)

    assert session.outputs[0].payload == {
        "rate": 0.0,
        "context_max": 10000,
        "tokens_used": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 800,
        "usage_source": "usage_metadata",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_source"),
    [
        (SimpleNamespace(usage={"prompt_tokens": 11, "completion_tokens": 4}), "usage"),
        (
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17)
            ),
            "usage",
        ),
        (
            SimpleNamespace(
                raw=SimpleNamespace(
                    usage=type(
                        "DictUsage",
                        (),
                        {"dict": lambda self: {"prompt_tokens": 13, "completion_tokens": 6}},
                    )()
                )
            ),
            "raw.usage",
        ),
    ],
)
async def test_context_usage_normalizes_provider_shapes(
    monkeypatch, response, expected_source
):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.ContextUtils.resolve_context_max",
        lambda **_kwargs: 100,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        agent=None,
        inputs=SimpleNamespace(response=response),
    )

    await _TestRail().emit_context_usage(ctx)

    payload = session.outputs[0].payload
    assert payload["usage_source"] == expected_source
    assert payload["input_tokens"] in {11, 12, 13}
    assert payload["output_tokens"] in {4, 5, 6}
    assert payload["total_tokens"] == payload["input_tokens"] + payload["output_tokens"]
    assert payload["tokens_used"] == payload["input_tokens"]


@pytest.mark.asyncio
async def test_context_usage_falls_back_to_total_only_when_input_is_absent(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.ContextUtils.resolve_context_max",
        lambda **_kwargs: 10000,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        agent=None,
        inputs=SimpleNamespace(response=SimpleNamespace(usage={"total_tokens": 9})),
    )

    await _TestRail().emit_context_usage(ctx)

    assert session.outputs[0].payload["tokens_used"] == 9
    assert session.outputs[0].payload["input_tokens"] == 0


@pytest.mark.asyncio
async def test_context_usage_rail_does_not_duplicate_core_snapshot():
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        context_usage_report=SimpleNamespace(parts={"tools": {"tokens": 1}}),
        agent=None,
        inputs=SimpleNamespace(response=None),
    )

    await _TestRail().after_model_call(ctx)

    assert session.outputs == []


@pytest.mark.asyncio
async def test_context_usage_rail_does_not_duplicate_core_snapshot_without_report():
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        context_usage_report=None,
        extra={"_context_usage_event_emitted": True},
        agent=None,
        inputs=SimpleNamespace(response=None, context_usage_report=None),
    )

    await _TestRail().after_model_call(ctx)

    assert session.outputs == []


@pytest.mark.asyncio
async def test_context_usage_keeps_runtime_context_limit_fallback(monkeypatch):
    captured_kwargs = {}

    def _resolve_context_max(**kwargs):
        captured_kwargs.update(kwargs)
        return 1000000

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.ContextUtils.resolve_context_max",
        _resolve_context_max,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(_context_window_tokens=1048576),
        agent=None,
        inputs=SimpleNamespace(response=None),
    )

    await _TestRail().emit_context_usage(ctx)

    assert captured_kwargs["fallback_context_window_tokens"] == 1048576
    assert session.outputs[0].payload["context_max"] == 1000000


@pytest.mark.asyncio
async def test_ask_user_interrupt_emits_question_event_from_tool_args():
    class ToolInterruptException(Exception):
        def __init__(self):
            super().__init__()
            self.request = SimpleNamespace(
                tool_call_id="tool-ask-1",
                tool_args={
                    "questions": [
                        {
                            "question": "请选择方案",
                            "header": "方案",
                            "options": [
                                {"label": "A", "description": "方案 A"},
                            ],
                        }
                    ]
                },
            )

    session = _FakeSession()
    tool_call = SimpleNamespace(id="tool-ask-1", arguments="{}")
    rail = _TestRail()

    await rail.emit_ask_user_question_if_interrupted(
        session,
        tool_call,
        "ask_user",
        ToolInterruptException(),
    )

    assert len(session.outputs) == 1
    output = session.outputs[0]
    assert output.type == "chat.ask_user_question"
    assert output.payload["request_id"] == "tool-ask-1"
    assert output.payload["source"] == "ask_user_interrupt"
    assert output.payload["questions"][0]["question"] == "请选择方案"


@pytest.mark.asyncio
async def test_ask_user_interrupt_emits_question_event_from_exception_cause():
    class ToolInterruptException(Exception):
        def __init__(self):
            super().__init__()
            self.request = SimpleNamespace(
                tool_call_id="tool-ask-2",
                questions=[
                    {
                        "question": "是否继续",
                        "header": "确认",
                        "options": [
                            {"label": "继续", "description": "继续执行"},
                        ],
                    }
                ],
            )

    session = _FakeSession()
    tool_call = SimpleNamespace(id="tool-ask-2", arguments="{}")
    exception = SimpleNamespace(cause=ToolInterruptException())
    rail = _TestRail()

    await rail.emit_ask_user_question_if_interrupted(
        session,
        tool_call,
        "ask_user",
        None,
        exception,
    )

    assert len(session.outputs) == 1
    output = session.outputs[0]
    assert output.type == "chat.ask_user_question"
    assert output.payload["request_id"] == "tool-ask-2"
    assert output.payload["questions"][0]["question"] == "是否继续"
