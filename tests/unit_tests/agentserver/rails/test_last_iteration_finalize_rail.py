# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the last-iteration partial-result finalizer."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import ModelCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.last_iteration_finalize_rail import (
    LastIterationFinalizeRail,
)


def _ctx(iteration: int, max_iterations: int, *, tool_calls: bool = True):
    agent = SimpleNamespace(
        _react_agent=SimpleNamespace(
            _config=SimpleNamespace(max_iterations=max_iterations)
        )
    )
    response = SimpleNamespace(tool_calls=[SimpleNamespace()] if tool_calls else [])
    return SimpleNamespace(
        agent=agent,
        inputs=ModelCallInputs(react_iteration=iteration, response=response),
        request_force_finish=MagicMock(),
    )


@pytest.mark.asyncio
async def test_no_finish_before_last_iteration() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=2, max_iterations=3)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_last_iteration_with_model_call_inputs_returns_actionable_error() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "error"
    assert payload["finish_reason"] == "max_iterations"
    assert "最大思考轮数" in payload["output"]
    assert "建议" in payload["output"]


@pytest.mark.asyncio
async def test_text_only_response_does_not_force_finish() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3, tool_calls=False)
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_set_language_switches_to_english() -> None:
    rail = LastIterationFinalizeRail()
    ctx = _ctx(iteration=3, max_iterations=3)
    await rail.before_invoke(ctx)
    rail.set_language("en")

    await rail.after_react_iteration(ctx)

    payload = ctx.request_force_finish.call_args[0][0]
    assert "maximum number of reasoning iterations" in payload["output"]


@pytest.mark.asyncio
async def test_reads_max_iterations_from_agent_config_fallback() -> None:
    rail = LastIterationFinalizeRail()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(max_iterations=3)),
        inputs=ModelCallInputs(
            react_iteration=3,
            response=SimpleNamespace(tool_calls=[SimpleNamespace()]),
        ),
        request_force_finish=MagicMock(),
    )
    await rail.before_invoke(ctx)

    await rail.after_react_iteration(ctx)

    ctx.request_force_finish.assert_called_once()


class _FakeAttachmentManager:
    def __init__(self) -> None:
        self.sections: dict[tuple[str, str], dict] = {}

    async def add_section(self, *, session_id, section, content, **kwargs):
        self.sections[(session_id, section)] = {"content": content, **kwargs}

    async def clear_section(self, *, session_id, section):
        return 1 if self.sections.pop((session_id, section), None) else 0


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id


def _live_ctx(session_id: str = "s1", max_iterations: int = 100):
    manager = _FakeAttachmentManager()
    builder = MagicMock()
    agent = SimpleNamespace(
        _react_agent=SimpleNamespace(_config=SimpleNamespace(max_iterations=max_iterations)),
        prompt_attachment_manager=manager,
        system_prompt_builder=builder,
    )
    ctx = SimpleNamespace(
        agent=agent,
        session=_FakeSession(session_id),
        extra={},
        inputs=SimpleNamespace(query="帮我做一个ppt"),
        request_force_finish=MagicMock(),
    )
    return ctx, manager, builder


def _invoke_envelope(tool_name: str, body: dict | str, meta: dict | None = None) -> dict:
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if meta is not None:
        text += f"\n<session-meta>{json.dumps(meta)}</session-meta>"
    return {"success": True, "tool_name": tool_name, "result": {"result": text}, "status": "success"}


async def _tool(rail, ctx, tool_name: str, result, *, via_invoke: bool = True) -> None:
    if via_invoke:
        call = SimpleNamespace(name="invoke_tool", arguments=json.dumps({"tool_name": tool_name}))
    else:
        call = SimpleNamespace(name=tool_name, arguments="{}")
    ctx.inputs = SimpleNamespace(tool_call=call, tool_result=result)
    await rail.after_tool_call(ctx)


async def _finish(rail, ctx, iteration: int = 100) -> str:
    ctx.inputs = ModelCallInputs(
        react_iteration=iteration, response=SimpleNamespace(tool_calls=[SimpleNamespace()])
    )
    await rail.after_react_iteration(ctx)
    return ctx.request_force_finish.call_args[0][0]["output"]


_DOC = "doc:8e6bb63f-1557-4a17-9c19-8bc3a6f996ea"
_PATH = "D:\\ws\\916班九年级家长会.pptx"


async def _open_and_edit(rail, ctx, revision: int = 44) -> None:
    await _tool(rail, ctx, "switch_file", _invoke_envelope(
        "switch_file", {"status": "ready", "workingPath": _PATH, "documentId": _DOC, "revision": 0}
    ))
    await _tool(rail, ctx, "apply_ops", _invoke_envelope(
        "apply_ops", {"applied": True, "status": "applied", "saved": None},
        {"documentId": _DOC, "revision": revision},
    ))


@pytest.mark.asyncio
async def test_budget_reminder_is_tail_attachment_not_system_prompt() -> None:
    rail = LastIterationFinalizeRail()
    ctx, manager, builder = _live_ctx()
    await rail.before_invoke(ctx)

    ctx.inputs = ModelCallInputs(react_iteration=85)
    await rail.before_model_call(ctx)
    assert not manager.sections

    ctx.inputs = ModelCallInputs(react_iteration=86)
    await rail.before_model_call(ctx)
    entry = manager.sections[("s1", "task_finalization_budget")]
    assert "含本轮还剩 15 轮" in entry["content"]
    assert "最后一轮" not in entry["content"]
    # The cached system prompt must stay untouched.
    builder.add_section.assert_not_called()
    builder.remove_section.assert_not_called()
    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_budget_reminder_reports_unsaved_state_and_last_round() -> None:
    rail = LastIterationFinalizeRail()
    ctx, manager, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=44)

    ctx.inputs = ModelCallInputs(react_iteration=100)
    await rail.before_model_call(ctx)

    content = manager.sections[("s1", "task_finalization_budget")]["content"]
    assert "未保存的修改（版本 44）" in content
    assert "这是最后一轮" in content


@pytest.mark.asyncio
async def test_budget_attachment_cleared_after_invoke() -> None:
    rail = LastIterationFinalizeRail()
    ctx, manager, _ = _live_ctx()
    await rail.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=95)
    await rail.before_model_call(ctx)
    assert manager.sections

    ctx.inputs = SimpleNamespace(result={"result_type": "answer"})
    await rail.after_invoke(ctx)

    assert not manager.sections


@pytest.mark.asyncio
async def test_live_edits_without_save_are_reported_as_unsaved() -> None:
    """Issue7: 100 rounds of live edits, apply_ops saved=null, no save_pptx."""
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=44)

    output = await _finish(rail, ctx)

    assert output.startswith("max_iterations_reached")
    assert "未保存的修改：版本 44 尚未写入文件" in output
    assert _PATH in output
    assert "最近一次已确认保存的草稿" not in output


@pytest.mark.asyncio
async def test_save_receipt_is_reported_with_relay_compatible_phrase() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=43)
    await _tool(rail, ctx, "save_pptx", _invoke_envelope("save_pptx", {
        "status": "saved", "documentId": _DOC, "sourceRevision": 43, "revision": 43,
        "dirty": False, "workingPath": "D:\\ws\\out.pptx",
    }, {"documentId": _DOC, "revision": 43}))

    output = await _finish(rail, ctx)

    assert "最近一次已确认保存的草稿：D:\\ws\\out.pptx（版本 43）" in output
    assert "未保存" not in output


@pytest.mark.asyncio
async def test_edits_after_last_save_are_flagged() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=40)
    await _tool(rail, ctx, "save_pptx", _invoke_envelope("save_pptx", {
        "status": "saved", "documentId": _DOC, "revision": 40, "workingPath": _PATH,
    }))
    await _tool(rail, ctx, "apply_ops", _invoke_envelope(
        "apply_ops", {"applied": True, "saved": None}, {"documentId": _DOC, "revision": 44}
    ))

    output = await _finish(rail, ctx)

    assert "最近一次已确认保存的草稿" in output and "版本 40" in output
    assert "版本 44 尚未写入文件" in output


@pytest.mark.asyncio
async def test_fill_plan_saved_receipt_direct_mcp_name() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _tool(
        rail, ctx, "pptx-mcp__execute_fill_plan",
        {"result": json.dumps({"status": "complete", "saved": {"path": "/draft.pptx", "revision": 3}})},
        via_invoke=False,
    )

    output = await _finish(rail, ctx)

    assert "最近一次已确认保存的草稿：/draft.pptx（版本 3）" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, result, via_invoke",
    [
        ("save_pptx", {"success": False, "result": {"status": "saved", "workingPath": "/bad"}}, True),
        ("save_pptx", _invoke_envelope("save_pptx", {"error": {"code": "INTERNAL_ERROR"}}), True),
        ("save_pptx", "saved file /untrusted", True),
        ("bash", json.dumps({"status": "saved", "documentId": "d", "workingPath": "/bash"}), False),
        ("apply_ops", _invoke_envelope("apply_ops", {"applied": True}), True),
    ],
)
async def test_untrusted_or_failed_results_are_not_document_state(tool_name, result, via_invoke) -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _tool(rail, ctx, tool_name, result, via_invoke=via_invoke)

    output = await _finish(rail, ctx)

    assert output.endswith("建议：缩小任务范围，或补充数据源后重试。")


@pytest.mark.asyncio
async def test_state_survives_interrupt_but_not_a_new_request() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=5)

    # Permission interrupt: run continues later with the same budget.
    ctx.inputs = SimpleNamespace(result={"result_type": "interrupt"})
    await rail.after_invoke(ctx)
    ctx.inputs = SimpleNamespace(query=object())  # InteractiveInput-like resume
    await rail.before_invoke(ctx)
    assert "版本 5 尚未写入文件" in await _finish(rail, ctx)

    ctx.request_force_finish.reset_mock()
    ctx.inputs = SimpleNamespace(result={"result_type": "error"})
    await rail.after_invoke(ctx)
    ctx.inputs = SimpleNamespace(query="新的请求")
    await rail.before_invoke(ctx)
    assert "未保存" not in await _finish(rail, ctx)


@pytest.mark.asyncio
async def test_first_iteration_of_new_run_drops_previous_document_state() -> None:
    """Invoke-level hooks may lack a session; iteration 1 marks a fresh run."""
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=9)

    ctx.inputs = ModelCallInputs(react_iteration=1)
    await rail.before_model_call(ctx)

    assert "未保存" not in await _finish(rail, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, body, saved_path",
    [
        ("save_pptx", {"status": "saved", "documentId": _DOC, "workingPath": "D:\\ws\\out.pptx"}, "D:\\ws\\out.pptx"),
        ("execute_fill_plan", {"status": "complete", "saved": {"path": "D:\\ws\\out.pptx"}}, "D:\\ws\\out.pptx"),
    ],
)
async def test_save_receipt_without_revision_counts_as_saving_current_edits(tool_name, body, saved_path) -> None:
    """A receipt without revision must not read as both saved and unsaved."""
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=44)
    await _tool(rail, ctx, tool_name, _invoke_envelope(tool_name, body))

    output = await _finish(rail, ctx)

    # Falls back to the live revision the save tool wrote.
    assert f"最近一次已确认保存的草稿：{saved_path}（版本 44）。" in output
    assert "None" not in output
    assert "未保存" not in output


@pytest.mark.asyncio
async def test_save_receipt_without_revision_and_no_live_state_has_no_version() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _tool(rail, ctx, "save_pptx", _invoke_envelope(
        "save_pptx", {"status": "saved", "workingPath": "/x.pptx"}
    ))

    output = await _finish(rail, ctx)

    assert "最近一次已确认保存的草稿：/x.pptx。" in output
    assert "None" not in output and "未保存" not in output


@pytest.mark.asyncio
async def test_opened_document_without_edits_is_not_reported_unsaved() -> None:
    """open_pptx on a reused live document reports its current revision (43)."""
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _tool(rail, ctx, "open_pptx", _invoke_envelope(
        "open_pptx", {"status": "ready", "workingPath": _PATH, "documentId": _DOC, "revision": 43, "reused": True}
    ))
    # Read tools also carry <session-meta> with the unchanged revision.
    await _tool(rail, ctx, "get_deck", _invoke_envelope(
        "get_deck", {"slides": []}, {"documentId": _DOC, "revision": 43}
    ))

    assert "未保存" not in await _finish(rail, ctx)

    ctx.request_force_finish.reset_mock()
    await _tool(rail, ctx, "apply_ops", _invoke_envelope(
        "apply_ops", {"applied": True, "saved": None}, {"documentId": _DOC, "revision": 44}
    ))
    assert "版本 44 尚未写入文件" in await _finish(rail, ctx)


@pytest.mark.asyncio
async def test_bind_payload_revision_does_not_roll_back_live_revision() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=46)
    # A late/out-of-order bind payload for the same document.
    await _tool(rail, ctx, "open_pptx", _invoke_envelope(
        "open_pptx", {"status": "ready", "workingPath": _PATH, "documentId": _DOC, "revision": 30, "reused": True}
    ))

    assert "版本 46 尚未写入文件" in await _finish(rail, ctx)


@pytest.mark.asyncio
async def test_streaming_interrupt_without_interrupt_result_keeps_save_receipt() -> None:
    """Streaming invokes fold only answer chunks into result; result_type is not 'interrupt'."""
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=1)
    await rail.before_model_call(ctx)
    await _open_and_edit(rail, ctx, revision=44)
    await _tool(rail, ctx, "save_pptx", _invoke_envelope("save_pptx", {
        "status": "saved", "documentId": _DOC, "revision": 44, "workingPath": "D:\\ws\\out.pptx",
    }))

    # Permission interrupt ends the streaming invoke without result_type=interrupt.
    ctx.inputs = SimpleNamespace(result={"output": "", "result_type": "answer"})
    await rail.after_invoke(ctx)
    # Resume: InteractiveInput query, ReAct continues at a later iteration.
    ctx.inputs = SimpleNamespace(query=object())
    await rail.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=60)
    await rail.before_model_call(ctx)
    await _tool(rail, ctx, "get_deck", _invoke_envelope(
        "get_deck", {"slides": []}, {"documentId": _DOC, "revision": 44}
    ))

    output = await _finish(rail, ctx)

    assert "最近一次已确认保存的草稿：D:\\ws\\out.pptx（版本 44）" in output
    assert "未保存" not in output


@pytest.mark.asyncio
async def test_new_run_reports_only_documents_it_touched_but_keeps_save_knowledge() -> None:
    rail = LastIterationFinalizeRail()
    ctx, _, _ = _live_ctx()
    await rail.before_invoke(ctx)
    await _open_and_edit(rail, ctx, revision=44)
    await _tool(rail, ctx, "save_pptx", _invoke_envelope("save_pptx", {
        "status": "saved", "documentId": _DOC, "revision": 44, "workingPath": "D:\\ws\\out.pptx",
    }))
    ctx.inputs = SimpleNamespace(result={"result_type": "answer"})
    await rail.after_invoke(ctx)

    # Next request ("继续"): the bound document is only read again.
    ctx.inputs = SimpleNamespace(query="继续")
    await rail.before_invoke(ctx)
    ctx.inputs = ModelCallInputs(react_iteration=1)
    await rail.before_model_call(ctx)
    assert "草稿" not in await _finish(rail, ctx)

    ctx.request_force_finish.reset_mock()
    await _tool(rail, ctx, "get_deck", _invoke_envelope(
        "get_deck", {"slides": []}, {"documentId": _DOC, "revision": 44}
    ))
    output = await _finish(rail, ctx)
    assert "未保存" not in output
    assert "（版本 44）" in output


@pytest.mark.asyncio
async def test_document_state_is_isolated_per_session() -> None:
    rail = LastIterationFinalizeRail()
    ctx_a, _, _ = _live_ctx("a")
    ctx_b, _, _ = _live_ctx("b")
    await rail.before_invoke(ctx_a)
    await rail.before_invoke(ctx_b)
    await _open_and_edit(rail, ctx_a, revision=7)

    assert "未保存" not in await _finish(rail, ctx_b)
    assert "版本 7 尚未写入文件" in await _finish(rail, ctx_a)


class _ToolCallModel:
    def __init__(self) -> None:
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid"
        )
        self.model_config = ModelRequestConfig(model_name="fake")

    async def stream(self, messages, **kwargs):
        yield AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", name="echo", arguments="{}")],
        )

    async def invoke(self, messages, **kwargs):
        return AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", name="echo", arguments="{}")],
        )


class _EchoTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(name="echo", description="echo"))

    async def invoke(self, inputs, **kwargs):
        return "ok"

    async def stream(self, inputs, **kwargs):
        yield "ok"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_react_loop_uses_model_call_inputs_on_last_iteration(tmp_path) -> None:
    await Runner.start()
    agent = create_deep_agent(
        model=_ToolCallModel(),
        tools=[_EchoTool()],
        system_prompt="last-iteration contract test",
        max_iterations=1,
        workspace=str(tmp_path),
        agent_ras=False,
        enable_security_rail=False,
        enable_llm_retry_rail=False,
        rails=[LastIterationFinalizeRail()],
    )
    await agent.start(session=Session(session_id="last-iter-" + uuid.uuid4().hex))
    try:
        result = await agent.invoke({"query": "run the echo tool"})
    finally:
        await agent.stop()

    assert result["result_type"] == "error"
    assert "最大思考轮数" in result["output"]
    assert "Max iterations reached without completion" not in result["output"]


class _RecordingSaveModel(_ToolCallModel):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list] = []

    def _reply(self, messages):
        self.calls.append(list(messages))
        return AssistantMessage(
            content="",
            tool_calls=[ToolCall(id=f"t{len(self.calls)}", type="function", name="save_pptx", arguments="{}")],
        )

    async def stream(self, messages, **kwargs):
        yield self._reply(messages)

    async def invoke(self, messages, **kwargs):
        return self._reply(messages)


class _SavePptxTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(name="save_pptx", description="save"))
        self.revision = 0

    async def invoke(self, inputs, **kwargs):
        self.revision += 1
        body = json.dumps({
            "status": "saved", "documentId": "doc:1", "revision": self.revision, "workingPath": "/out.pptx",
        })
        return {"result": f'{body}\n<session-meta>{{"documentId":"doc:1","revision":{self.revision}}}</session-meta>'}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


def _text_of(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_react_loop_budget_attachment_and_save_receipt(tmp_path) -> None:
    await Runner.start()
    model = _RecordingSaveModel()
    agent = create_deep_agent(
        model=model,
        tools=[_SavePptxTool()],
        system_prompt="finalize budget contract test",
        max_iterations=5,
        workspace=str(tmp_path),
        agent_ras=False,
        enable_security_rail=False,
        enable_llm_retry_rail=False,
        rails=[LastIterationFinalizeRail()],
    )
    await agent.start(session=Session(session_id="finalize-" + uuid.uuid4().hex))
    try:
        result = await agent.invoke({"query": "save the deck"})
    finally:
        await agent.stop()

    # Ignore side calls (e.g. the image-modality probe); keep ReAct turns only.
    react_calls = [
        call for call in model.calls
        if call and "finalize budget contract test" in _text_of(call[0])
    ]
    assert len(react_calls) == 5
    model.calls = react_calls
    texts = ["\n".join(_text_of(m) for m in call) for call in model.calls]
    # reserve = max(2, 5 // 5) = 2: only iterations 4 and 5 carry the reminder.
    assert ["轮数收尾预算" in t for t in texts] == [False, False, False, True, True]
    assert "这是最后一轮" in texts[4]
    # Reminder lives in the message tail; the cached system prompt is unchanged.
    systems = [_text_of(call[0]) for call in model.calls]
    assert len(set(systems)) == 1
    assert "轮数收尾预算" not in systems[0]
    assert result["result_type"] == "error"
    assert "最近一次已确认保存的草稿：/out.pptx（版本 5）" in result["output"]
    assert "未保存" not in result["output"]
