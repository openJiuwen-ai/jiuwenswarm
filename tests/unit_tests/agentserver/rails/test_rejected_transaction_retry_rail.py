# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flash-only retry guard for atomically rejected apply_ops transactions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail
from jiuwenswarm.agents.harness.flash.rails.rejected_transaction_retry_rail import (
    REJECTED_TRANSACTION_RETRY_TOOL_NAME,
    RejectedTransactionRetryRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def write_stream(self, schema) -> None:
        self.events.append(schema.type)


def _rejected_result() -> dict:
    return {
        "success": True,
        "tool_name": "apply_ops",
        "result": {
            "result": (
                '{"applied": false, "status": "rejected", '
                '"failures": [{"error": "op addElement needs target.slide"}]}'
            )
        },
    }


def _success_result() -> dict:
    return {
        "success": True,
        "tool_name": "apply_ops",
        "result": {"result": '{"applied": true, "status": "applied"}'},
    }


def _tool_ctx(tool_name: str, arguments: dict | None, result: object) -> SimpleNamespace:
    tool_call = ToolCall(
        id=f"call-{tool_name}",
        type="function",
        name=tool_name,
        arguments="{}",
    )
    inputs = ToolCallInputs(
        tool_call=tool_call,
        tool_name=tool_name,
        tool_args=(
            (
                arguments
                if isinstance(arguments, dict) and "tool_name" in arguments
                else {"tool_name": "apply_ops", "arguments": arguments}
            )
            if tool_name == "invoke_tool"
            else arguments
        ),
        tool_result=result,
    )
    return SimpleNamespace(inputs=inputs, extra={})


def _model_ctx(
    *,
    content: str = "我需要修正后重新提交事务",
    tool_calls: list[ToolCall] | None = None,
    react_iteration: int = 3,
    max_iterations: int = 40,
    finish_reason: str | None = None,
    session: _FakeSession | None = None,
) -> SimpleNamespace:
    response = AssistantMessage(content=content, tool_calls=tool_calls or [])
    if finish_reason:
        response.finish_reason = finish_reason
    agent = SimpleNamespace(
        _react_agent=SimpleNamespace(_config=SimpleNamespace(max_iterations=max_iterations))
    )
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(response=response, react_iteration=react_iteration),
        session=session,
        extra={},
        finished=None,
    )

    def request_force_finish(result: dict) -> None:
        ctx.finished = result

    ctx.request_force_finish = request_force_finish
    return ctx


def _rail() -> RejectedTransactionRetryRail:
    return RejectedTransactionRetryRail()


@pytest.mark.asyncio
async def test_rejected_apply_ops_text_only_turn_gets_one_retry_nudge() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))

    ctx = _model_ctx()
    await rail.after_model_call(ctx)

    assert ctx.finished is None
    assert ctx.inputs.response.content == ""
    assert len(ctx.inputs.response.tool_calls) == 1
    call = ctx.inputs.response.tool_calls[0]
    assert call.name == REJECTED_TRANSACTION_RETRY_TOOL_NAME
    assert str(call.id).startswith("flash-rejected-retry-")
    assert rail._retry_nudge_used is True


@pytest.mark.asyncio
async def test_retry_nudge_holds_and_drops_streamed_process_text() -> None:
    rail = _rail()
    session = _FakeSession()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))

    ctx = _model_ctx(session=session)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)

    assert session.events == ["todo_close_hold", "todo_close_drop"]
    assert ctx.inputs.response.tool_calls[0].name == (
        REJECTED_TRANSACTION_RETRY_TOOL_NAME
    )


@pytest.mark.asyncio
async def test_second_text_only_turn_forces_incomplete_error() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))
    await rail.after_model_call(_model_ctx())

    ctx = _model_ctx(content="仍然没有重新提交")
    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert ctx.finished is not None
    assert ctx.finished["result_type"] == "error"
    assert ctx.finished["finish_reason"] == "incomplete_transaction"
    assert "已完成的进度" in ctx.finished["output"]
    assert "可继续" in ctx.finished["output"]


@pytest.mark.asyncio
async def test_last_iteration_forces_incomplete_error_instead_of_false_success() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))

    ctx = _model_ctx(react_iteration=40, max_iterations=40)
    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert ctx.finished["result_type"] == "error"
    assert ctx.finished["finish_reason"] == "incomplete_transaction"


@pytest.mark.asyncio
async def test_successful_apply_ops_clears_rejected_state() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _success_result()))

    ctx = _model_ctx(content="PPT 已生成完成")
    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert ctx.inputs.response.content == "PPT 已生成完成"
    assert ctx.finished is None


@pytest.mark.asyncio
async def test_ordinary_text_answer_is_unchanged() -> None:
    rail = _rail()
    ctx = _model_ctx(content="普通问答不需要工具")

    await rail.after_model_call(ctx)

    assert ctx.inputs.response.tool_calls == []
    assert ctx.inputs.response.content == "普通问答不需要工具"
    assert ctx.finished is None


@pytest.mark.asyncio
async def test_real_tool_call_and_truncated_output_are_unchanged() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))
    real = ToolCall(id="call-real", type="function", name="invoke_tool", arguments="{}")
    ctx = _model_ctx(content="我会重试", tool_calls=[real])
    await rail.after_model_call(ctx)
    assert ctx.inputs.response.tool_calls == [real]
    assert ctx.finished is None

    truncated = _model_ctx(content="输出被截断", finish_reason="length")
    await rail.after_model_call(truncated)
    assert truncated.inputs.response.tool_calls == []
    assert truncated.finished is None


@pytest.mark.asyncio
async def test_non_apply_ops_and_unparsed_results_do_not_trigger() -> None:
    rail = _rail()
    await rail.after_tool_call(
        _tool_ctx(
            "invoke_tool",
            {"tool_name": "read_file"},
            _rejected_result(),
        )
    )
    ctx = _model_ctx()
    await rail.after_model_call(ctx)
    assert ctx.inputs.response.tool_calls == []

    await rail.after_tool_call(
        _tool_ctx("invoke_tool", {"tool_name": "apply_ops"}, None)
    )
    ctx = _model_ctx()
    await rail.after_model_call(ctx)
    assert ctx.inputs.response.tool_calls == []


@pytest.mark.asyncio
async def test_nudge_tool_is_skipped_and_returns_resend_instruction() -> None:
    rail = _rail()
    call = ToolCall(
        id="flash-rejected-retry-abc",
        type="function",
        name=REJECTED_TRANSACTION_RETRY_TOOL_NAME,
        arguments="{}",
    )
    ctx = SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=call,
            tool_name=REJECTED_TRANSACTION_RETRY_TOOL_NAME,
            tool_args={},
        ),
        extra={},
    )

    await rail.before_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    assert "applied=false" in str(ctx.inputs.tool_result)
    assert "重新提交整个 apply_ops 事务" in str(ctx.inputs.tool_result)
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.tool_call_id == "flash-rejected-retry-abc"


@pytest.mark.asyncio
async def test_before_invoke_resets_request_local_state() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))
    await rail.after_model_call(_model_ctx())
    rail._text_hold.used = True
    rail._text_hold._hold_pending = True

    await rail.before_invoke(SimpleNamespace(inputs=SimpleNamespace(), extra={}))

    assert rail._rejected_pending is False
    assert rail._retry_nudge_used is False
    assert rail._text_hold.used is False
    assert rail._text_hold._hold_pending is False


@pytest.mark.asyncio
async def test_final_iteration_after_rejected_tool_forces_incomplete_error() -> None:
    rail = _rail()
    await rail.after_tool_call(_tool_ctx("invoke_tool", {}, _rejected_result()))

    ctx = _model_ctx(react_iteration=40, max_iterations=40)
    await rail.after_react_iteration(ctx)

    assert ctx.finished is not None
    assert ctx.finished["result_type"] == "error"
    assert ctx.finished["finish_reason"] == "incomplete_transaction"


@pytest.mark.asyncio
async def test_retry_nudge_wins_over_flash_todo_close_nudge() -> None:
    retry_rail = _rail()
    todo_rail = FlashTodoRail()
    todo_rail._agent = SimpleNamespace(
        _react_agent=SimpleNamespace(
            _config=SimpleNamespace(max_iterations=40)
        )
    )
    todo_rail._unfinished_todos = [("todo-1", {"status": "in_progress"})]
    session = _FakeSession()
    await retry_rail.after_tool_call(
        _tool_ctx("invoke_tool", {}, _rejected_result())
    )

    ctx = _model_ctx(session=session, react_iteration=3, max_iterations=40)
    # Rails execute by descending priority: retry rail (91), then Flash todo
    # rail (90).  The retry nudge must survive the Flash todo rail's rewrite.
    await retry_rail.before_model_call(ctx)
    await todo_rail.before_model_call(ctx)
    await retry_rail.after_model_call(ctx)
    await todo_rail.after_model_call(ctx)

    calls = ctx.inputs.response.tool_calls
    assert len(calls) == 1
    assert calls[0].name == REJECTED_TRANSACTION_RETRY_TOOL_NAME
    assert ctx.inputs.response.content == ""
    assert "todo_close_drop" in session.events


def test_parser_accepts_json_string_and_nested_payload_shapes() -> None:
    rail = _rail()
    exact_history_shape = {
        "success": True,
        "tool_name": "apply_ops",
        "result": {"result": '{"applied": false, "status": "rejected"}'},
    }
    nested_shape = {"result": {"applied": False, "status": "rejected"}}
    pydantic_like = SimpleNamespace(
        data={"result": {"result": '{"applied": false, "status": "rejected"}'}}
    )

    assert rail._is_rejected_result(exact_history_shape) is True
    assert rail._is_rejected_result(nested_shape) is True
    assert rail._is_rejected_result(pydantic_like) is True
    assert rail._is_rejected_result({"result": "ordinary text"}) is False
    assert rail._is_rejected_result({"applied": False}) is False


def test_parser_does_not_let_an_earlier_non_rejected_payload_mask_a_nested_rejection() -> None:
    rail = _rail()
    result = {
        "result": {"applied": True, "status": "ignored"},
        "nested": {"result": {"applied": False, "status": "rejected"}},
    }

    assert rail._is_rejected_result(result) is True


def test_flash_adapter_mounts_rail_without_deep_profile_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    captured: dict = {}

    def fake_super_instantiate(self, rail_infos, config_base):
        captured["rail_infos"] = rail_infos
        rails = []
        for info in rail_infos:
            instance = info.build_func(**info.params)
            setattr(self, info.attr_name, instance)
            rails.append(instance)
        return rails

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_instantiate_rails",
        fake_super_instantiate,
    )
    monkeypatch.setattr(
        adapter,
        "_build_flash_skill_selection_rail",
        lambda config: None,
        raising=False,
    )

    rails = adapter._instantiate_rails([], {})

    attr = "_flash_rejected_transaction_retry_rail"
    assert attr in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert attr in [info.attr_name for info in captured["rail_infos"]]
    assert isinstance(getattr(adapter, attr), RejectedTransactionRetryRail)
    assert any(isinstance(rail, RejectedTransactionRetryRail) for rail in rails)
    # The hook is Flash-specific: the shared Deep adapter has no such builder.
    assert not hasattr(JiuWenSwarmDeepAdapter, "_build_flash_rejected_transaction_retry_rail")


def test_flash_adapter_returns_none_when_retry_rail_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    real_import = builtins.__import__

    def fail_retry_rail_import(name, *args, **kwargs):
        if name == "jiuwenswarm.agents.harness.flash.rails.rejected_transaction_retry_rail":
            raise RuntimeError("retry rail import unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_retry_rail_import)

    assert adapter._build_flash_rejected_transaction_retry_rail() is None


def test_code_and_team_profiles_do_not_declare_flash_retry_rail() -> None:
    """The retry guard is Flash-only; code and team rail sets stay unchanged."""
    attr = "_flash_rejected_transaction_retry_rail"

    code_adapter = JiuwenSwarmCodeAdapter()
    code_captured: list = []
    code_real_instantiate = code_adapter._instantiate_rails

    def capture_code_rails(rail_infos, _config_base):
        code_captured.extend(rail_infos)
        return []

    object.__setattr__(code_adapter, "_instantiate_rails", capture_code_rails)
    try:
        code_adapter._build_agent_rails(
            config={}, config_base={}, mode="code", sub_mode="normal"
        )
    finally:
        object.__setattr__(code_adapter, "_instantiate_rails", code_real_instantiate)

    team_adapter = JiuWenSwarmDeepAdapter()
    team_captured: list = []
    team_real_instantiate = team_adapter._instantiate_rails

    def capture_team_rails(rail_infos, _config_base):
        team_captured.extend(rail_infos)
        return []

    object.__setattr__(team_adapter, "_instantiate_rails", capture_team_rails)
    try:
        team_adapter._build_agent_rails(config={}, config_base={}, mode="team")
    finally:
        object.__setattr__(team_adapter, "_instantiate_rails", team_real_instantiate)

    assert attr not in {info.attr_name for info in code_captured}
    assert attr not in {info.attr_name for info in team_captured}
    assert not hasattr(JiuwenSwarmCodeAdapter, "_build_flash_rejected_transaction_retry_rail")
