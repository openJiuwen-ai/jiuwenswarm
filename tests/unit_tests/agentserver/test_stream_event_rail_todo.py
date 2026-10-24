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
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.resolve_context_window_tokens",
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
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.resolve_context_window_tokens",
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

    assert session.outputs[0].payload["tokens_used"] == 0


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
async def test_after_model_call_persists_cumulative_usage_without_emitting_an_event(
    monkeypatch,
    tmp_path,
):
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.runtime.session.session_token_usage import get_session_token_usage

    class _UsageMetadata:
        @staticmethod
        def model_dump():
            return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(),
        context_usage_report=SimpleNamespace(parts={}),
        extra={"_context_usage_event_emitted": True},
        agent=SimpleNamespace(),
        inputs=SimpleNamespace(
            context_usage_request_id="call-1",
            context_usage_attribution={
                "product_session_id": "team/workflow/avatar",
                "depth": 2,
                "team_id": "team-1",
                "member_name": "leader",
                "agent_path": ["leader"],
            },
            response=SimpleNamespace(usage_metadata=_UsageMetadata()),
        ),
    )

    rail = _TestRail()
    rail.init(SimpleNamespace(_build_context=SimpleNamespace(role="leader", session_id="sess_usage")))
    await rail.after_model_call(ctx)

    assert session.outputs == []
    assert get_session_token_usage("sess_usage")["main"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_calls": 1,
        "output_calls": 1,
        "calls_total": 1,
    }
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import SessionTokenUsageRail

    ctx.inputs.context_usage_request_id = "call-child"
    ctx.inputs.context_usage_attribution["product_session_id"] = "sess_usage"
    await SessionTokenUsageRail().after_model_call(ctx)
    assert get_session_token_usage("sess_usage")["main"]["input_tokens"] == 100
    assert get_session_token_usage("sess_usage")["others"]["input_tokens"] == 100
    assert session.outputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_build_context", [True, False])
async def test_subagent_hook_keeps_root_owner_for_nested_async_children(monkeypatch, tmp_path, has_build_context):
    from openjiuwen.core.single_agent.rail.base import bind_usage_invocation_id, reset_usage_invocation_id
    from openjiuwen.harness.deep_agent import DeepAgent
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import (
        install_session_token_usage_subagent_hook,
    )
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.runtime.session.session_token_usage import get_session_token_usage

    class _Child:
        def __init__(self):
            self.rails = []

        def configured_rails(self):
            return self.rails

        def add_rail(self, rail):
            self.rails.append(rail)

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(DeepAgent, "create_subagent", lambda _self, *_args, **_kwargs: _Child())
    install_session_token_usage_subagent_hook()
    parent = SimpleNamespace(
        _usage_build_context=SimpleNamespace(session_id="root_usage") if has_build_context else None,
    )
    child = DeepAgent.create_subagent(parent, "general-purpose", "team/workflow/child")
    rail = child.rails[0]
    ctx = SimpleNamespace(
        extra={},
        session=None,
        inputs=SimpleNamespace(conversation_id="team/workflow/child", invocation_id="invoke-child"),
    )
    ctx.inputs = SimpleNamespace(
        context_usage_request_id="child-call",
        context_usage_attribution={
            "product_session_id": "team/workflow" if has_build_context else "root_usage",
            "execution_session_id": "team/workflow/child",
            "invocation_id": "invoke-child",
        },
        response=SimpleNamespace(usage_metadata={"input_tokens": 7, "output_tokens": 3}),
    )
    await rail.after_model_call(ctx)
    token = bind_usage_invocation_id("invoke-child")
    try:
        nested = DeepAgent.create_subagent(child, "general-purpose", "team/workflow/nested")
    finally:
        reset_usage_invocation_id(token)
    nested_ctx = SimpleNamespace(
        extra={},
        session=None,
        inputs=SimpleNamespace(conversation_id="team/workflow/nested", invocation_id="invoke-nested"),
    )
    nested_ctx.inputs = SimpleNamespace(
        context_usage_request_id="nested-call",
        context_usage_attribution={
            "product_session_id": "team/workflow/child",
            "execution_session_id": "team/workflow/nested",
            "invocation_id": "invoke-nested",
        },
        response=SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 2}),
    )
    await nested.rails[0].after_model_call(nested_ctx)
    assert get_session_token_usage("root_usage")["others"]["input_tokens"] == 12
    assert get_session_token_usage("root_usage")["others"]["output_tokens"] == 5
    await rail.after_invoke(SimpleNamespace(inputs=SimpleNamespace(conversation_id="team/workflow/child")))
    assert not rail.invocation_products


def test_subagent_hook_adds_one_token_usage_rail(monkeypatch):
    from openjiuwen.harness.deep_agent import DeepAgent
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import (
        SessionTokenUsageRail,
        install_session_token_usage_subagent_hook,
    )

    class _FakeSubagent:
        def __init__(self):
            self.rails = []

        def configured_rails(self):
            return self.rails

        def add_rail(self, rail):
            self.rails.append(rail)

    child = _FakeSubagent()
    monkeypatch.setattr(DeepAgent, "create_subagent", lambda _self, *_args, **_kwargs: child)
    install_session_token_usage_subagent_hook()

    created = DeepAgent.create_subagent(object(), "general-purpose", "sub-session")

    assert created is child
    assert sum(isinstance(rail, SessionTokenUsageRail) for rail in child.rails) == 1


@pytest.mark.asyncio
async def test_context_usage_keeps_runtime_context_limit_fallback(monkeypatch):
    captured_kwargs = {}

    def _resolve_context_window_tokens(**kwargs):
        captured_kwargs.update(kwargs)
        return 1000000

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail.resolve_context_window_tokens",
        _resolve_context_window_tokens,
    )
    session = _FakeSession()
    ctx = SimpleNamespace(
        session=session,
        context=SimpleNamespace(_context_window_tokens=1048576),
        agent=None,
        inputs=SimpleNamespace(response=None),
    )

    await _TestRail().emit_context_usage(ctx)

    assert captured_kwargs["context_engine_config"]["context_window_tokens"] == 1048576
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
