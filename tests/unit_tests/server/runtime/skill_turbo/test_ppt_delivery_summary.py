# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""引擎侧 PPT 交付总结握手：收尾诚实性 + 骨架投递链路（纯引擎测试）。

覆盖 skill_turbo_tools / delivery_summary_rail / interface resume 三段：
- ``_wrap_skill_turbo_result``：P10 骨架排队、未确认中立短句、失败不谎报；
- ``emit_pending_ppt_delivery_summary`` / ``SkillTurboDeliverySummaryRail``：
  外层 tool_result 之后流式发出骨架、非加速工具忽略、HITL 中断清 pending；
- ``visible_ppt_turbo_finish_text``：优先骨架、无骨架未确认短句、失败不含产物账本；
- ``_make_skill_turbo_resume_stream``：resume 终稿发骨架而非产物 dump。

骨架起始标记（真实常量归 ppt code 所有，经动态包 ``skill_turbo_codes_ppt``
取得）在此打桩为本地常量——引擎函数只消费 marker 字符串做 startswith
判定，等价替换后本文件不依赖外部 turbo 布局（ppt code 侧行为测试已随
turbo code 迁出本仓维护）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    PPT_TURBO_UNCONFIRMED_FINISH_TEXT,
    _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER,
    _wrap_skill_turbo_result,
    clear_pending_ppt_delivery_summary,
    emit_pending_ppt_delivery_summary,
    take_pending_ppt_delivery_summary,
    visible_ppt_turbo_finish_text,
)

# 骨架起始标记打桩（见模块 docstring）：与真实常量同角色，仅做 startswith 判定。
_STUB_DELIVERY_SUMMARY_START = "【PPT 交付总结】"


@pytest.fixture(autouse=True)
def _stub_delivery_summary_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools

    monkeypatch.setattr(
        skill_turbo_tools,
        "_ppt_delivery_summary_start",
        lambda: _STUB_DELIVERY_SUMMARY_START,
    )


def _make_tool_call_ctx(
    *,
    tool_name: str,
    tool_result: Any = None,
    session: Any = None,
    exception: Exception | None = None,
) -> Any:
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    return SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=None,
            tool_name=tool_name,
            tool_args={},
            tool_result=tool_result,
            tool_msg=None,
        ),
        session=session,
        exception=exception,
    )


def _holder_with_skeleton(skeleton: str) -> dict[str, Any]:
    return {
        "p2_requirement_collect": {"info": {"content_pages": 1, "total_pages": 3}},
        "p10_delivery": {
            "info": {"send_file_status": "sent", "delivery_summary_emitted": True},
            "files": [{"path": "杭州旅游.pptx"}],
            "delivery_summary": skeleton,
        },
    }


def test_wrap_skill_turbo_result_queues_ppt_skeleton_for_post_tool_emit() -> None:
    clear_pending_ppt_delivery_summary()
    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
    wrapped = _wrap_skill_turbo_result(
        {"success": True, "result": "任务已完成"},
        {
            "p10_delivery": {
                "info": {"send_file_status": "sent", "delivery_summary_emitted": True},
                "files": [],
                "delivery_summary": skeleton,
            }
        },
    )
    assert _STUB_DELIVERY_SUMMARY_START not in wrapped["result"]
    assert "流式通道" in wrapped["result"]
    assert "无需在本回合重复输出" in wrapped["result"]
    assert "You should now summarize" not in wrapped["result"]
    assert take_pending_ppt_delivery_summary() == skeleton.strip()
    assert take_pending_ppt_delivery_summary() == ""


def test_wrap_skill_turbo_result_keeps_generic_hint_without_ppt_summary() -> None:
    clear_pending_ppt_delivery_summary()
    wrapped = _wrap_skill_turbo_result({"success": True, "result": "任务已完成"}, {})
    assert "任务已完成" in wrapped["result"]
    assert "You should now summarize" in wrapped["result"]
    assert "did NOT confirm" in wrapped["result"]
    assert "ALREADY been sent" not in wrapped["result"]
    assert "send_file_to_user" in wrapped["result"]
    assert "逐字输出" not in wrapped["result"]
    assert take_pending_ppt_delivery_summary() == ""


def test_ppt_delivery_failed_error_detects_p10_failed() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        _ppt_delivery_failed_error,
    )

    assert _ppt_delivery_failed_error({}) == ""
    assert (
        _ppt_delivery_failed_error(
            {
                "p10_delivery": {
                    "info": {"delivery_status": "ok", "task_completed": True},
                }
            }
        )
        == ""
    )
    assert (
        _ppt_delivery_failed_error(
            {
                "p10_delivery": {
                    "info": {"delivery_status": "partial", "task_completed": True},
                }
            }
        )
        == ""
    )
    err = _ppt_delivery_failed_error(
        {
            "p10_delivery": {
                "info": {
                    "delivery_status": "failed",
                    "task_completed": False,
                    "send_file_status": "skipped",
                }
            }
        }
    )
    assert "PPT 生成失败" in err
    assert "不要告知用户已经生成成功" in err


def test_wrap_skill_turbo_result_marks_failure_when_p10_delivery_failed() -> None:
    """P10 failed 时 tool result 不得再包装成「任务已完成」。"""
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        _ppt_delivery_failed_error,
    )

    holder = {
        "p10_delivery": {
            "info": {
                "delivery_status": "failed",
                "task_completed": False,
                "send_file_status": "skipped",
            }
        }
    }
    err = _ppt_delivery_failed_error(holder)
    wrapped = _wrap_skill_turbo_result({"success": False, "error": err}, holder)
    assert wrapped["success"] is False
    assert "PPT 生成失败" in wrapped["error"]
    assert "任务已完成" not in wrapped.get("result", "")
    assert "You should now summarize" not in wrapped.get("error", "")


@pytest.mark.asyncio
async def test_emit_pending_ppt_delivery_summary_writes_llm_output() -> None:
    clear_pending_ppt_delivery_summary()
    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    set_pending_ppt_delivery_summary(skeleton)
    written: list[Any] = []

    class _Session:
        async def write_stream(self, output: Any) -> None:
            written.append(output)

    assert await emit_pending_ppt_delivery_summary(_Session()) is True
    assert len(written) == 1
    assert written[0].type == "llm_output"
    assert written[0].payload["content"].startswith(_STUB_DELIVERY_SUMMARY_START)
    assert take_pending_ppt_delivery_summary() == ""


@pytest.mark.asyncio
async def test_emit_pending_ppt_delivery_summary_skips_invalid_skeleton() -> None:
    """骨架不携带起始标记（非法/残留文本）时不发出，静默丢弃。"""
    clear_pending_ppt_delivery_summary()
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    set_pending_ppt_delivery_summary("普通文本，不是交付骨架")
    written: list[Any] = []

    class _Session:
        async def write_stream(self, output: Any) -> None:
            written.append(output)

    assert await emit_pending_ppt_delivery_summary(_Session()) is False
    assert written == []
    assert take_pending_ppt_delivery_summary() == ""


@pytest.mark.asyncio
async def test_delivery_summary_rail_emits_after_skill_acceleration_exec() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
        SkillTurboDeliverySummaryRail,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    clear_pending_ppt_delivery_summary()
    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
    set_pending_ppt_delivery_summary(skeleton)
    written: list[Any] = []

    class _Session:
        async def write_stream(self, output: Any) -> None:
            written.append(output)

    rail = SkillTurboDeliverySummaryRail()
    assert rail.priority > 80
    await rail.after_tool_call(
        _make_tool_call_ctx(
            tool_name="skill_acceleration_exec",
            tool_result={"success": True},
            session=_Session(),
        )
    )
    assert len(written) == 1
    assert written[0].payload["content"].startswith(_STUB_DELIVERY_SUMMARY_START)
    assert take_pending_ppt_delivery_summary() == ""


@pytest.mark.asyncio
async def test_delivery_summary_rail_ignores_other_tools() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
        SkillTurboDeliverySummaryRail,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    clear_pending_ppt_delivery_summary()
    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
    set_pending_ppt_delivery_summary(skeleton)

    await SkillTurboDeliverySummaryRail().after_tool_call(
        _make_tool_call_ctx(tool_name="bash", tool_result={"ok": True}, session=object())
    )
    assert take_pending_ppt_delivery_summary() == skeleton.strip()


@pytest.mark.asyncio
async def test_delivery_summary_rail_clears_pending_on_tool_interrupt() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
        SkillTurboDeliverySummaryRail,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    class ToolInterruptException(Exception):
        def __init__(self) -> None:
            self.request = object()

    clear_pending_ppt_delivery_summary()
    set_pending_ppt_delivery_summary(f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成\n")
    written: list[Any] = []

    class _Session:
        async def write_stream(self, output: Any) -> None:
            written.append(output)

    await SkillTurboDeliverySummaryRail().after_tool_call(
        _make_tool_call_ctx(
            tool_name="skill_acceleration_exec",
            tool_result=ToolInterruptException(),
            session=_Session(),
        )
    )
    assert written == []
    assert take_pending_ppt_delivery_summary() == ""


def test_visible_finish_text_prefers_p10_skeleton() -> None:
    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
    text = visible_ppt_turbo_finish_text(_holder_with_skeleton(skeleton), success=True)
    assert text == skeleton.strip()
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in text
    assert "任务已完成" not in text


def test_visible_finish_text_uses_unconfirmed_text_without_skeleton() -> None:
    text = visible_ppt_turbo_finish_text(
        {"p8_ppt_page_gen": {"info": {"total_pages": 3}}},
        success=True,
    )
    assert text == PPT_TURBO_UNCONFIRMED_FINISH_TEXT
    assert "已生成并交付" not in text
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in text


def test_visible_finish_text_failure_omits_artifact_dump() -> None:
    text = visible_ppt_turbo_finish_text(
        _holder_with_skeleton(f"{_STUB_DELIVERY_SUMMARY_START}\n"),
        success=False,
        detail="SkillAccelerationExec 未处理: boom",
    )
    assert text == "SkillAccelerationExec 未处理: boom"
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in text


@pytest.mark.asyncio
async def test_resume_stream_emits_skeleton_not_artifact_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    skeleton = f"{_STUB_DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"

    class _FakeTurbo:
        def __init__(self, _config: Any) -> None:
            self.artifact_holder = _holder_with_skeleton(skeleton)

        async def resume_stream(self, **_kwargs: Any):
            if False:
                yield None

    async def _async_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
        _FakeTurbo,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_clear_resume_ctx",
        _async_none,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._skill_turbo_clear_resume_in_flight",
        _async_none,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = None
    adapter.build_skill_turbo_config = lambda: {}  # type: ignore[method-assign]
    adapter._log_and_make_usage_summary_chunk = lambda **_k: None  # type: ignore[method-assign]
    adapter._rewrite_skill_turbo_usage_chunk = lambda chunk, **_k: (chunk, None)  # type: ignore[method-assign]

    class _Session:
        async def post_run(self) -> None:
            return None

    request = AgentRequest(
        request_id="req-resume",
        channel_id="officeclaw",
        session_id="sess-resume",
        req_method=ReqMethod.CHAT_SEND,
        params={"source": "ask_user_interrupt", "answers": [{"question": "风格"}]},
    )
    stream = adapter._make_skill_turbo_resume_stream(
        request=request,
        inputs={},
        session=_Session(),
        resume_ctx={"plan_code": "x", "pending_tool_call_id": "tc-1", "inputs": {}},
        answers=[{"question": "风格", "selected_options": ["商务经典"]}],
    )
    assert stream is not None
    chunks = [chunk async for chunk in stream]
    deltas = [
        chunk.payload.get("content", "")
        for chunk in chunks
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.delta"
    ]
    finals = [
        chunk.payload.get("content", "")
        for chunk in chunks
        if isinstance(chunk.payload, dict) and chunk.payload.get("event_type") == "chat.final"
    ]
    assert deltas
    assert deltas[-1].startswith(_STUB_DELIVERY_SUMMARY_START)
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in deltas[-1]
    assert finals
    assert str(finals[-1]).strip() == ""
    assert chunks[-1].is_complete is True
