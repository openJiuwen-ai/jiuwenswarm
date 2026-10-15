# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
    _build_skill_protocol_section_text,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TODO_CLOSE_NUDGE_ID_PREFIX,
    TODO_CLOSE_NUDGE_TOOL_NAME,
    TaskExecutionRail,
)


class _FakeSession:
    def get_session_id(self) -> str:
        return "sess-1"

    async def write_stream(self, schema: object) -> None:
        return None


def _rail_with_open_task() -> TaskExecutionRail:
    rail = TaskExecutionRail()
    rail._todo_map = {
        "env": {
            "id": "env",
            "content": "环境预检",
            "status": "in_progress",
        },
        "source": {
            "id": "source",
            "content": "清点源文件",
            "status": "pending",
        },
    }
    rail._todo_started.add("env")
    rail._active_tasks["todo:env"] = SimpleNamespace()
    return rail


def _text_ctx(content: str, tool_calls: list | None = None) -> SimpleNamespace:
    response = AssistantMessage(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(
        session=_FakeSession(),
        inputs=SimpleNamespace(response=response),
        extra={},
    )


@pytest.mark.asyncio
async def test_text_only_ending_attaches_one_todo_close_nudge() -> None:
    rail = _rail_with_open_task()
    ctx = _text_ctx("流程在此暂停，请上传源文件。")

    await rail.after_model_call(ctx)
    calls = ctx.inputs.response.tool_calls
    assert len(calls) == 1
    assert calls[0].name == TODO_CLOSE_NUDGE_TOOL_NAME
    assert str(calls[0].id).startswith(TODO_CLOSE_NUDGE_ID_PREFIX)
    assert ctx.inputs.response.content == ""
    assert rail._todo_close_nudge_used is True

    await rail.after_model_call(ctx)
    assert len(ctx.inputs.response.tool_calls) == 1


@pytest.mark.asyncio
async def test_truncated_text_does_not_attach_nudge() -> None:
    rail = _rail_with_open_task()
    ctx = _text_ctx("正文被截断")
    ctx.inputs.response.finish_reason = "length"

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert ctx.inputs.response.content == "正文被截断"
    assert rail._todo_close_nudge_used is False


@pytest.mark.asyncio
async def test_last_react_iteration_does_not_attach_nudge() -> None:
    rail = _rail_with_open_task()
    rail._deep_agent = SimpleNamespace(
        _react_agent=SimpleNamespace(
            _config=SimpleNamespace(max_iterations=15),
        ),
    )
    ctx = _text_ctx("最后一轮正文")
    ctx.inputs.react_iteration = 15

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert rail._todo_close_nudge_used is False


@pytest.mark.asyncio
async def test_text_only_ending_keeps_real_tool_calls() -> None:
    rail = _rail_with_open_task()
    existing = ToolCall(
        id="call-1", type="function", name="todo_modify", arguments="{}",
    )
    ctx = _text_ctx("同时改状态", tool_calls=[existing])

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == [existing]
    assert rail._todo_close_nudge_used is False


@pytest.mark.asyncio
async def test_text_only_ending_skipped_without_open_task() -> None:
    rail = TaskExecutionRail()
    ctx = _text_ctx("没有进行中的步骤。")

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert rail._todo_close_nudge_used is False


@pytest.mark.asyncio
async def test_nudge_tool_is_skipped_and_does_not_advance_todos() -> None:
    rail = _rail_with_open_task()
    tool_call = ToolCall(
        id="todo-close-nudge-1",
        type="function",
        name=TODO_CLOSE_NUDGE_TOOL_NAME,
        arguments="{}",
    )
    ctx = SimpleNamespace(
        session=_FakeSession(),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=TODO_CLOSE_NUDGE_TOOL_NAME,
            tool_args={},
        ),
        extra={},
    )
    advanced: list[str] = []

    async def _record_advance(_ctx: object, task_id: str | None = None) -> None:
        advanced.append(task_id or "")

    rail._auto_advance_pending_to_in_progress = _record_advance  # type: ignore[method-assign]

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    assert "todo_modify" in ctx.inputs.tool_result
    assert "没有送达用户" in ctx.inputs.tool_result
    assert "完整重写" in ctx.inputs.tool_result
    assert "env" in ctx.inputs.tool_result
    assert ctx.inputs.tool_msg.tool_call_id == "todo-close-nudge-1"
    assert rail._todo_map["env"]["status"] == "in_progress"
    assert rail._todo_map["source"]["status"] == "pending"
    assert advanced == []


def test_skill_turbo_success_hint_closes_todo_before_summary() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import (
        _build_skill_turbo_guide_text,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT,
    )

    cn = _build_skill_turbo_guide_text("cn", ["pptx-craft"])
    en = _build_skill_turbo_guide_text("en", ["pptx-craft"])
    assert "先单独调 `todo_modify`" in cn
    assert "直接向用户总结结果即可" not in cn
    assert "first call `todo_modify` alone" in en
    assert "just summarize the result to the user" not in en
    assert "todo_modify" in _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT["cn"]
    assert "禁止 tool_call" not in _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT["cn"]
    assert "No further tool calls" not in _PPT_DELIVERY_SUMMARY_POST_TOOL_HINT["en"]


def test_skill_protocol_allows_standalone_todo_modify_before_final() -> None:
    cn = _build_skill_protocol_section_text("cn")
    en = _build_skill_protocol_section_text("en")
    assert "纯文本最终回复的前一轮" in cn
    assert "第 9 条的用户打断" in cn
    assert "including the user-interruption cases in rule 9" in en
    assert "所有工作完成后" not in cn
    assert "the turn immediately before the plain-text final reply" in en
    assert "when all work is done" not in en


@pytest.mark.asyncio
async def test_stream_rail_does_not_emit_todo_close_nudge(monkeypatch) -> None:
    rail = JiuSwarmStreamEventRail()
    emitted: list[str] = []

    async def _record(name: str, *_args: object, **_kwargs: object) -> None:
        emitted.append(name)

    monkeypatch.setattr(rail, "_emit_tool_call", lambda *a, **k: _record("call", *a, **k))
    monkeypatch.setattr(
        rail, "_emit_tool_update", lambda *a, **k: _record("update", *a, **k),
    )
    monkeypatch.setattr(
        rail, "_emit_tool_result", lambda *a, **k: _record("result", *a, **k),
    )
    tool_call = ToolCall(
        id="todo-close-nudge-1",
        type="function",
        name=TODO_CLOSE_NUDGE_TOOL_NAME,
        arguments="{}",
    )
    ctx = SimpleNamespace(
        session=_FakeSession(),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=TODO_CLOSE_NUDGE_TOOL_NAME,
            tool_args={},
            tool_result="nudge",
        ),
        extra={},
        exception=None,
    )

    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert emitted == []


@pytest.mark.asyncio
async def test_text_only_resume_nudges_disk_in_progress_without_memory() -> None:
    rail = TaskExecutionRail()
    rail._todo_map = {
        "env": {
            "id": "env",
            "content": "环境预检",
            "status": "in_progress",
        },
    }
    ctx = _text_ctx("问答之后直接说明卡点。")

    await rail.after_model_call(ctx)

    calls = ctx.inputs.response.tool_calls
    assert len(calls) == 1
    assert str(calls[0].id).startswith(TODO_CLOSE_NUDGE_ID_PREFIX)


@pytest.mark.asyncio
async def test_before_invoke_resets_nudge_budget() -> None:
    rail = _rail_with_open_task()
    rail._todo_close_nudge_used = True
    ctx = SimpleNamespace(session=None, inputs=object(), extra={})

    await rail.before_invoke(ctx)

    assert rail._todo_close_nudge_used is False
