# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, UsageMetadata
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.schema.task import TodoStatus

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    TODO_CLOSE_NUDGE_ID_PREFIX,
)
from jiuwenswarm.agents.harness.common.rails.todo_close_gate import TodoCloseGate
from jiuwenswarm.agents.harness.flash.rails import flash_todo_rail
from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import (
    MODEL_REFUSAL_BODY,
    FlashTodoRail,
    is_canned_model_refusal,
)


class _FakeSession:
    def __init__(self, *, fail_writes: int = 0) -> None:
        self.events: list[str] = []
        self.fail_writes = fail_writes

    def get_session_id(self) -> str:
        return "sess-flash"

    async def write_stream(self, schema: object) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("stream closed")
        self.events.append(str(getattr(schema, "type", "")))


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FakeModify:
    def __init__(self, engine: "_FakeListEngine", *, fail: bool = False, message: str | None = None) -> None:
        self.engine = engine
        self.fail = fail
        self.message = message
        self.calls: list[dict] = []

    async def invoke(self, inputs: dict, session=None) -> dict:
        assert session is not None
        self.calls.append(inputs)
        if self.fail:
            raise RuntimeError("cancel failed")
        if self.message is not None:
            return {"message": self.message}
        for todo_id in inputs["ids"]:
            self.engine.statuses[todo_id] = TodoStatus.CANCELLED
        return {"message": f"Successfully cancelled {len(inputs['ids'])} task(s)"}


class _FakeListEngine:
    def __init__(self, statuses: dict[str, TodoStatus], *, error: Exception | None = None) -> None:
        self.statuses = statuses
        self.error = error

    async def load_todos(self, session_id: str) -> list:
        assert session_id == "sess-flash"
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(id=k, content=f"任务 {k}", status=v) for k, v in self.statuses.items()]


def _rail(statuses: dict[str, TodoStatus], **kwargs) -> tuple[FlashTodoRail, _FakeListEngine]:
    rail = FlashTodoRail()
    engine = _FakeListEngine(statuses, **kwargs)
    rail.engines = {"list": engine}
    rail._agent = SimpleNamespace(_react_agent=SimpleNamespace(_config=SimpleNamespace(max_iterations=5)))
    return rail, engine


def _todo_ctx(session: _FakeSession, action: str, call_id: str = "call-todo") -> SimpleNamespace:
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_name="todo", tool_args={"action": action}, tool_call=SimpleNamespace(id=call_id)
        ),
        extra={},
    )


def _refusal_usage(**overrides) -> UsageMetadata:
    usage = {
        "output_tokens": 47,
        "reasoning_tokens": 0,
        "cache_read_tokens": None,
        "cache_tokens": 0,
    }
    usage.update(overrides)
    return UsageMetadata(**usage)


def _model_ctx(
    session: _FakeSession,
    content: str,
    tool_calls: list | None = None,
    *,
    reasoning_content: str = "",
    usage_metadata: UsageMetadata | None = None,
    **inputs,
) -> SimpleNamespace:
    response = AssistantMessage(
        content=content,
        tool_calls=tool_calls or [],
        reasoning_content=reasoning_content,
        usage_metadata=usage_metadata,
    )
    inputs.setdefault("react_iteration", 1)
    ctx = SimpleNamespace(session=session, inputs=SimpleNamespace(response=response, **inputs), extra={}, finished=None)

    def request_force_finish(result: dict) -> None:
        ctx.finished = result

    ctx.request_force_finish = request_force_finish
    return ctx


async def _final_turn(
    rail: FlashTodoRail,
    session: _FakeSession,
    content: str,
    tool_calls: list | None = None,
    **inputs,
) -> SimpleNamespace:
    ctx = _model_ctx(session, content, tool_calls=tool_calls, **inputs)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    return ctx


@pytest.mark.asyncio
async def test_text_ending_with_open_todo_is_nudged_once() -> None:
    rail, _ = _rail({"export": TodoStatus.COMPLETED, "deliver": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    ctx = await _final_turn(rail, session, "以下是完成情况概要：PPT 已生成。")

    calls = ctx.inputs.response.tool_calls
    assert len(calls) == 1
    assert calls[0].name == "todo"
    assert str(calls[0].id).startswith(TODO_CLOSE_NUDGE_ID_PREFIX)
    assert ctx.inputs.response.content == ""
    assert session.events == ["todo_close_hold", "todo_close_drop"]

    session.events.clear()
    again = await _final_turn(rail, session, "模型仍不改状态，这次放行。")
    assert again.inputs.response.tool_calls == []
    assert again.inputs.response.content == "模型仍不改状态，这次放行。"
    assert session.events == ["refusal_scrub_hold", "refusal_scrub_release"]


_CANNED_REFUSAL = "作为一个人工智能语言模型，我还没学习如何回答这个问题，您可以向我问一些其它的问题，我会尽力帮您解决的。"


@pytest.mark.asyncio
async def test_refusal_after_nudge_is_still_held() -> None:
    rail, engine = _rail({"deliver": TodoStatus.IN_PROGRESS})
    rail.engines["modify"] = _FakeModify(engine)
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))
    await _final_turn(rail, session, "以下是完成情况概要：PPT 已生成。")
    assert rail._close_gate.used is True

    session.events.clear()
    ctx = await _final_turn(rail, session, _CANNED_REFUSAL, usage_metadata=_refusal_usage())

    assert ctx.inputs.response.content == MODEL_REFUSAL_BODY
    assert ctx.finished == {"output": MODEL_REFUSAL_BODY, "result_type": "answer"}
    assert session.events[0] == "refusal_scrub_hold"
    assert "refusal_scrub_drop" in session.events
    assert "llm_output" in session.events
    assert engine.statuses["deliver"] == TodoStatus.CANCELLED


def _canned_response(**kwargs) -> AssistantMessage:
    defaults = {
        "content": _CANNED_REFUSAL,
        "reasoning_content": "",
        "usage_metadata": _refusal_usage(),
        "tool_calls": [],
    }
    defaults.update(kwargs)
    return AssistantMessage(**defaults)


@pytest.mark.asyncio
async def test_canned_refusal_replaces_the_answer_body_and_stops() -> None:
    rail, engine = _rail({"collect": TodoStatus.IN_PROGRESS})
    modify = _FakeModify(engine)
    rail.engines["modify"] = modify
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))

    ctx = await _final_turn(rail, session, _CANNED_REFUSAL, usage_metadata=_refusal_usage())

    assert ctx.inputs.response.tool_calls == []
    assert ctx.inputs.response.content == MODEL_REFUSAL_BODY
    assert ctx.finished == {"output": MODEL_REFUSAL_BODY, "result_type": "answer"}
    assert modify.calls == [{"action": "cancel", "ids": ["collect"]}]
    assert engine.statuses["collect"] == TodoStatus.CANCELLED
    assert rail._open_todos == []
    assert session.events == [
        "todo_close_hold",
        "todo_close_drop",
        "todo.updated",
        "task.complete",
        "llm_output",
    ]


@pytest.mark.asyncio
async def test_refusal_does_not_announce_cancel_when_persist_fails() -> None:
    rail, engine = _rail({"collect": TodoStatus.IN_PROGRESS})
    rail.engines["modify"] = _FakeModify(engine, fail=True)
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))

    ctx = await _final_turn(rail, session, _CANNED_REFUSAL, usage_metadata=_refusal_usage())

    assert ctx.finished["result_type"] == "answer"
    assert engine.statuses["collect"] == TodoStatus.IN_PROGRESS
    assert "todo.updated" not in session.events
    assert "task.complete" not in session.events


@pytest.mark.asyncio
async def test_canned_refusal_without_open_todo_still_writes_the_body() -> None:
    rail, _ = _rail({})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())

    ctx = await _final_turn(
        rail, session, f"  {_CANNED_REFUSAL}\n", usage_metadata=_refusal_usage()
    )

    assert ctx.inputs.response.content == MODEL_REFUSAL_BODY
    assert ctx.finished["result_type"] == "answer"
    assert session.events == ["refusal_scrub_hold", "refusal_scrub_drop", "llm_output"]


def test_ordinary_or_quoted_text_is_not_a_canned_refusal() -> None:
    assert is_canned_model_refusal(_canned_response()) is True
    assert is_canned_model_refusal(_canned_response(content="口播稿已写完。")) is False
    quoted = "报道原句是：" + _CANNED_REFUSAL + "。这里继续写完核查和口播稿。" * 3
    assert is_canned_model_refusal(_canned_response(content=quoted)) is False


def test_refusal_needs_empty_reasoning_and_no_cache_hit() -> None:
    assert is_canned_model_refusal(
        _canned_response(reasoning_content="先核查来源再写稿。")
    ) is False
    assert is_canned_model_refusal(
        _canned_response(usage_metadata=_refusal_usage(reasoning_tokens=12))
    ) is False
    assert is_canned_model_refusal(
        _canned_response(usage_metadata=_refusal_usage(cache_read_tokens=128))
    ) is False
    assert is_canned_model_refusal(
        _canned_response(usage_metadata=_refusal_usage(cache_read_tokens=0))
    ) is True


@pytest.mark.asyncio
async def test_canned_refusal_with_tool_call_is_left_alone() -> None:
    rail, _ = _rail({"collect": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))
    call = ToolCall(id="call-search", type="function", name="web_flash", arguments="{}")

    ctx = await _final_turn(
        rail, session, _CANNED_REFUSAL, tool_calls=[call], usage_metadata=_refusal_usage()
    )

    assert ctx.finished is None
    assert ctx.inputs.response.tool_calls[0].name == "web_flash"
    assert "llm_output" not in session.events

@pytest.mark.asyncio
async def test_refusal_text_with_reasoning_is_not_stopped() -> None:
    rail, _ = _rail({})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())

    ctx = await _final_turn(
        rail,
        session,
        _CANNED_REFUSAL,
        reasoning_content="这段是正常推理。",
        usage_metadata=_refusal_usage(),
    )

    assert ctx.finished is None
    assert ctx.inputs.response.content == _CANNED_REFUSAL
    assert "llm_output" not in session.events


@pytest.mark.asyncio
async def test_nudge_tool_is_skipped_with_flash_tool_wording() -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))
    nudge_ctx = _todo_ctx(session, "", call_id=f"{TODO_CLOSE_NUDGE_ID_PREFIX}abc")

    await rail.before_tool_call(nudge_ctx)

    assert nudge_ctx.extra["_skip_tool"] is True
    msg = nudge_ctx.inputs.tool_result
    assert "[TODO_CLOSE_REQUIRED]" in msg
    assert "id=deliver" in msg
    assert "todo(action=modify)" in msg
    assert "todo_modify" not in msg
    assert nudge_ctx.inputs.tool_msg.tool_call_id == f"{TODO_CLOSE_NUDGE_ID_PREFIX}abc"
    rail.engines["list"].statuses = {}
    await rail.after_tool_call(nudge_ctx)
    assert [todo_id for todo_id, _ in rail._open_todos] == ["deliver"]


@pytest.mark.asyncio
async def test_closed_todos_release_text_without_nudge() -> None:
    rail, engine = _rail({"deliver": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))
    engine.statuses = {"deliver": TodoStatus.COMPLETED}
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    ctx = await _final_turn(rail, session, "全部完成。")

    assert ctx.inputs.response.tool_calls == []
    assert session.events == ["refusal_scrub_hold", "refusal_scrub_release"]


@pytest.mark.asyncio
async def test_stale_todos_without_write_this_invoke_are_ignored() -> None:
    rail, _ = _rail({"old": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "list"))

    ctx = await _final_turn(rail, session, "普通问答的回复。")

    assert ctx.inputs.response.tool_calls == []
    assert session.events == ["refusal_scrub_hold", "refusal_scrub_release"]


@pytest.mark.asyncio
async def test_tool_call_turn_releases_held_text() -> None:
    rail, _ = _rail({"regen": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))
    real_call = ToolCall(id="call-bash", type="function", name="bash", arguments="{}")
    ctx = _model_ctx(session, "继续生成页面。", tool_calls=[real_call])

    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == [real_call]
    assert ctx.inputs.response.content == "继续生成页面。"
    assert session.events == ["todo_close_hold", "todo_close_release"]


@pytest.mark.asyncio
async def test_truncated_or_last_iteration_ending_is_not_nudged() -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    truncated = _model_ctx(session, "被截断的回复")
    truncated.inputs.response.finish_reason = "length"
    await rail.after_model_call(truncated)
    assert truncated.inputs.response.tool_calls == []

    for iteration in (4, 5):
        tail = await _final_turn(rail, session, "收尾回复", react_iteration=iteration)
        assert tail.inputs.response.tool_calls == []
        assert tail.inputs.response.content == "收尾回复"
    assert rail._close_gate.used is False
    assert session.events == [
        "refusal_scrub_hold",
        "refusal_scrub_release",
        "refusal_scrub_hold",
        "refusal_scrub_release",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_iterations", "nudged"), [(3, False), (4, True)])
async def test_nudge_needs_rounds_to_close_todo_and_rewrite_reply(max_iterations, nudged) -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS})
    rail._agent._react_agent._config.max_iterations = max_iterations
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    ctx = await _final_turn(rail, session, "PPT 已生成，下载链接：/files/a.pptx", react_iteration=2)

    if nudged:
        assert ctx.inputs.response.content == ""
        assert session.events == ["todo_close_hold", "todo_close_drop"]
    else:
        assert ctx.inputs.response.tool_calls == []
        assert ctx.inputs.response.content == "PPT 已生成，下载链接：/files/a.pptx"
        assert session.events == ["refusal_scrub_hold", "refusal_scrub_release"]


@pytest.mark.asyncio
async def test_load_failure_and_new_invoke_reset_state() -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS}, error=RuntimeError("todo.json missing"))
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "create"))
    ctx = await _final_turn(rail, session, "读取失败时不拦。")
    assert ctx.inputs.response.tool_calls == []

    rail.engines["list"].error = None
    await rail.after_tool_call(_todo_ctx(session, "modify"))
    await _final_turn(rail, session, "第一次被拦。")
    assert rail._close_gate.used is True
    await rail.before_invoke(SimpleNamespace())
    assert rail._close_gate.used is False
    assert rail._open_todos == []


@pytest.mark.asyncio
async def test_gate_retries_failed_release_before_next_hold() -> None:
    gate = TodoCloseGate("todo")
    session = _FakeSession()
    await gate.arm(session, can_nudge=True)
    session.fail_writes = 1
    await gate.finish(session, dropped=False)
    assert session.events == ["todo_close_hold"]

    await gate.arm(session, can_nudge=True)
    assert session.events == ["todo_close_hold", "todo_close_release", "todo_close_hold"]


@pytest.mark.asyncio
async def test_gate_failed_drop_is_retried_and_never_released() -> None:
    gate = TodoCloseGate("todo")
    session = _FakeSession()
    await gate.arm(session, can_nudge=True)
    assert gate.attach(AssistantMessage(content="草稿", tool_calls=[]), can_nudge=True) is True
    session.fail_writes = 1
    await gate.finish(session, dropped=True)
    assert session.events == ["todo_close_hold"]

    await gate.finish(session, dropped=False)
    assert session.events == ["todo_close_hold", "todo_close_drop"]


def test_rail_is_not_inherited_by_general_purpose_subagent() -> None:
    from openjiuwen.harness.factory import _inject_general_purpose_subagent

    todo_rail = FlashTodoRail()
    specs = _inject_general_purpose_subagent(
        [],
        add_general_purpose_agent=True,
        resolved_language="cn",
        rails=[todo_rail],
        system_prompt=None,
        tools=None,
        mcps=None,
        model=None,
        skills=None,
    )

    gp_rails = list(getattr(specs[0], "rails", None) or [])
    assert gp_rails
    assert all(rail is not todo_rail for rail in gp_rails)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [None, SimpleNamespace(_react_agent=None)])
async def test_unknown_iteration_limit_never_nudges(agent) -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS})
    rail._agent = agent
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    # Whether "jiuwenswarm" propagates depends on logging setup, so caplog may
    # see each record 0 or 2 times; count through our own handler instead.
    rail_logger = logging.getLogger(flash_todo_rail.__name__)
    handler = _RecordingHandler()
    old_level = rail_logger.level
    rail_logger.addHandler(handler)
    rail_logger.setLevel(logging.WARNING)
    try:
        first = await _final_turn(rail, session, "第一次收尾")
        second = await _final_turn(rail, session, "第二次收尾")
    finally:
        rail_logger.removeHandler(handler)
        rail_logger.setLevel(old_level)

    assert first.inputs.response.content == "第一次收尾"
    assert second.inputs.response.tool_calls == []
    assert session.events == [
        "refusal_scrub_hold",
        "refusal_scrub_release",
        "refusal_scrub_hold",
        "refusal_scrub_release",
    ]
    assert len([r for r in handler.records if "react iteration unknown" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_call_outside_react_loop_is_not_nudged() -> None:
    rail, _ = _rail({"deliver": TodoStatus.IN_PROGRESS})
    session = _FakeSession()
    await rail.before_invoke(SimpleNamespace())
    await rail.after_tool_call(_todo_ctx(session, "modify"))

    ctx = await _final_turn(rail, session, "循环外的模型调用", react_iteration=0)

    assert ctx.inputs.response.tool_calls == []
    assert session.events == ["refusal_scrub_hold", "refusal_scrub_release"]
