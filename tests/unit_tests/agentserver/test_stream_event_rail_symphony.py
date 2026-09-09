from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.symphony.discovery import SkillDCICommandResult

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyToolStreamHandler,
)
from jiuwenswarm.agents.harness.common.tool_progress_context import (
    current_tool_progress,
)


class _StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


def _minimal_planned_graph(status="ready"):
    nodes = (
        {}
        if status == "no_plan"
        else {
            "writer": {"label": "Writer", "metadata": {"type": "skill"}},
            "reviewer": {"label": "Reviewer", "metadata": {"type": "skill"}},
        }
    )
    return {
        "graph": {
            "id": "plan-1",
            "type": "planned_graph",
            "directed": True,
            "metadata": {"status": status},
            "nodes": nodes,
            "edges": (
                []
                if status == "no_plan"
                else [
                    {
                        "source": "writer",
                        "target": "reviewer",
                        "relation": "can_feed",
                    }
                ]
            ),
        }
    }


def test_symphony_tool_stream_handler_matches_only_compose_tool():
    handler = SymphonyToolStreamHandler()

    assert handler.matches(SimpleNamespace(name="symphony_compose_graph"))
    assert not handler.matches(SimpleNamespace(name="todo_list"))


def _ctx(
    session,
    tool_name: str,
    tool_call_id: str = "call-1",
    tool_result=None,
):
    tool_call = SimpleNamespace(id=tool_call_id, name=tool_name, arguments={})
    force_finish_requests = []
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result=tool_result if tool_result is not None else {"success": True},
        ),
        extra={},
        exception=None,
        request_force_finish=force_finish_requests.append,
        force_finish_requests=force_finish_requests,
    )


def _model_ctx(messages):
    return SimpleNamespace(
        context=_ModelContext(messages),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )


@pytest.mark.asyncio
async def test_stream_event_rail_strips_image_blocks_when_read_image_multimodal_disabled(
    monkeypatch,
):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.stream_event_rail."
        "should_enable_read_image_multimodal",
        lambda _agent: True,
    )
    rail = JiuSwarmStreamEventRail()
    message = UserMessage(
        content=[
            {"type": "text", "text": "Image loaded from read_file: C:/tmp/blog.png"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc"},
            },
        ],
    )
    ctx = SimpleNamespace(
        session=None,
        inputs=SimpleNamespace(tools=[]),
        context=_ModelContext([message]),
        extra={},
    )

    await rail.before_model_call(ctx)

    assert message.content == "Image loaded from read_file: C:/tmp/blog.png"


@pytest.mark.asyncio
async def test_stream_event_rail_keeps_image_blocks_when_read_image_multimodal_enabled():
    rail = JiuSwarmStreamEventRail()
    rail.init(
        SimpleNamespace(
            deep_config=SimpleNamespace(enable_read_image_multimodal=True),
        )
    )
    content = [
        {"type": "text", "text": "Image loaded from read_file: C:/tmp/blog.png"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        },
    ]
    message = UserMessage(content=list(content))
    ctx = SimpleNamespace(
        session=None,
        inputs=SimpleNamespace(tools=[]),
        context=_ModelContext([message]),
        extra={},
    )

    await rail.before_model_call(ctx)

    assert message.content == content


@pytest.mark.asyncio
async def test_stream_event_rail_does_not_enable_symphony_status_events_for_plan_tool():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "symphony_compose_graph", tool_call_id="parent-call")

    await rail.before_tool_call(ctx)

    status_events = [
        chunk
        for chunk in session.chunks
        if chunk.type == "chat.symphony_status"
    ]
    assert status_events == []

    await rail.after_tool_call(ctx)
    assert not any(chunk.type == "chat.symphony_status" for chunk in session.chunks)


@pytest.mark.asyncio
async def test_stream_event_rail_emits_beam_progress_as_tool_update():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "symphony_compose_graph", tool_call_id="beam-call")

    await rail.before_tool_call(ctx)
    callback = current_tool_progress()
    assert callback is not None
    await callback({
        "event": "started",
        "language": "cn",
        "round_index": 0,
        "graph": {
            "nodes": [{"id": "seed", "label": "Seed", "status": "seed"}],
            "edges": [],
        },
    })

    updates = [chunk for chunk in session.chunks if chunk.type == "tool_update"]
    assert updates[-1].payload["tool_update"]["tool_call_id"] == "beam-call"
    assert updates[-1].payload["tool_update"]["beam_search_event"]["event"] == "started"

    await rail.after_tool_call(ctx)
    assert current_tool_progress() is None


@pytest.mark.asyncio
async def test_stream_event_rail_does_not_force_finish_normal_compose_result():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "success": True,
        "planned_graph": _minimal_planned_graph(),
        "graph_status": {"success": True, "exists": True, "stale": False},
    }
    ctx = _ctx(session, "symphony_compose_graph", tool_result=result)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    tool_results = []
    for chunk in session.chunks:
        tool_result = chunk.payload.get("tool_result")
        if (
            chunk.type == "tool_result"
            and tool_result is not None
            and tool_result.get("tool_name") == "symphony_compose_graph"
        ):
            tool_results.append(tool_result)
    assert tool_results[0]["raw_output"] == result
    assert tool_results[0]["graph_status"] == result["graph_status"]
    assert "direct_display" not in tool_results[0]
    assert "followup_action" not in tool_results[0]
    direct_messages = [chunk for chunk in session.chunks if chunk.type == "chat.final"]
    assert direct_messages == []
    assert ctx.force_finish_requests == []


@pytest.mark.asyncio
async def test_stream_event_rail_does_not_add_skill_gap_followup_to_compose_result():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "success": True,
        "planned_graph": _minimal_planned_graph("no_plan"),
    }
    ctx = _ctx(session, "symphony_compose_graph", tool_result=result)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    tool_results = [
        chunk.payload.get("tool_result")
        for chunk in session.chunks
        if chunk.type == "tool_result"
    ]
    assert "continue_after_display" not in tool_results[0]
    assert "followup_action" not in tool_results[0]
    assert not any(chunk.type == "chat.final" for chunk in session.chunks)
    assert ctx.force_finish_requests == []


@pytest.mark.asyncio
async def test_stream_event_rail_emits_skill_index_result_as_raw_output():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    diagnostics = {
        "command": 'rg -il "office|documents" /',
        "cwd": "/",
        "output": "[skill] documents/META.md  desc: Document Writer",
        "observed_skill_ids": ["documents"],
        "candidate_count": 1,
        "error": False,
        "truncated": False,
        "runtime": "SkillDCI.SkillFSToolkit",
    }
    result = SkillDCICommandResult(
        diagnostics["output"],
        detailed_output=diagnostics,
    )
    ctx = _ctx(session, "skill_index", tool_result=result)

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    tool_results = [
        chunk.payload.get("tool_result")
        for chunk in session.chunks
        if chunk.type == "tool_result"
    ]
    assert tool_results[0]["result"] == str(result)
    assert tool_results[0]["raw_output"] == diagnostics
    assert tool_results[0]["raw_output"]["observed_skill_ids"] == ["documents"]


@pytest.mark.asyncio
async def test_stream_event_rail_does_not_enable_symphony_status_events_for_other_tools():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _ctx(session, "todo_list")

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert not any(chunk.type == "chat.symphony_status" for chunk in session.chunks)
    assert ctx.force_finish_requests == []


@pytest.mark.asyncio
async def test_stream_event_rail_removes_orphan_tool_messages_before_model_call():
    rail = JiuSwarmStreamEventRail()
    ctx = _model_ctx([
        UserMessage(content="first request"),
        ToolMessage(content="cancelled build result", tool_call_id="orphan-call"),
        UserMessage(content="retry request"),
    ])

    await rail.before_model_call(ctx)

    messages = ctx.context.get_messages()
    assert [type(message) for message in messages] == [UserMessage, UserMessage]
    assert all(not isinstance(message, ToolMessage) for message in messages)


@pytest.mark.asyncio
async def test_stream_event_rail_inserts_missing_tool_result_after_cancelled_call():
    rail = JiuSwarmStreamEventRail()
    ctx = _model_ctx([
        UserMessage(content="compose a skill plan"),
        AssistantMessage(
            content="",
            tool_calls=[{
                "type": "function",
                "id": "compose-call",
                "function": {
                    "name": "symphony_compose_graph",
                    "arguments": "{\"query\":\"compose\"}",
                },
            }],
        ),
        UserMessage(content="retry request"),
    ])

    await rail.before_model_call(ctx)

    messages = ctx.context.get_messages()
    assert isinstance(messages[1], AssistantMessage)
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "compose-call"
    assert "symphony_compose_graph" in messages[2].content
    assert isinstance(messages[3], UserMessage)
