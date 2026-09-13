from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.delivery import DeliveryNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.delivery_summary import (
    DELIVERY_SUMMARY_START,
    build_delivery_summary_skeleton,
    is_backup_listing_path,
    parse_outline_pages,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import PPTGenRootNode
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    PPT_TURBO_UNCONFIRMED_FINISH_TEXT,
    _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER,
    _wrap_skill_turbo_result,
    clear_pending_ppt_delivery_summary,
    emit_pending_ppt_delivery_summary,
    take_pending_ppt_delivery_summary,
    visible_ppt_turbo_finish_text,
)

_SAMPLE_OUTLINE = """# 大纲：杭州旅游

**受众**：游客
**总页数**：3
**叙事主线**：一日游路线与必看景点
**输入类型**：topic
**搜索模式**：off

## 页面规划

### P1: 封面
- **类型**：cover
- **研究需求**：❌
- **标题**：杭州一日游
- **内容概要**：西湖与城市印象
- **研究查询**：-
- **数据需求**：-

### P2: 行程
- **类型**：agenda
- **研究需求**：❌
- **标题**：上午西湖下午灵隐
- **内容概要**：按时间排列的游览顺序
- **研究查询**：-
- **数据需求**：-

### P3: 结束
- **类型**：ending
- **研究需求**：❌
- **标题**：感谢聆听
- **内容概要**：欢迎再次到访
- **研究查询**：-
- **数据需求**：-
"""


def test_backup_listing_path_detects_nested_backup_html() -> None:
    assert is_backup_listing_path("pages/_backup/page-1.pptx.html") is True
    assert is_backup_listing_path(r"pages\\_backup\\page-1.pptx.html") is True
    assert is_backup_listing_path("pages/page-1.pptx.html") is False
    assert is_backup_listing_path("_backup") is True


def test_parse_listing_drops_backup_pages() -> None:
    node = DeliveryNode()
    files = node._parse_listing(
        [
            "pages/page-1.pptx.html",
            "pages/_backup/page-1.pptx.html",
            "pages/_backup/page-2.pptx.html",
            "pages/_backup/page-3.pptx.html",
            "pages/page-2.pptx.html",
            "pages/page-3.pptx.html",
        ]
    )
    assert files == ["page-1.pptx.html", "page-2.pptx.html", "page-3.pptx.html"]


@pytest.mark.asyncio
async def test_check_pages_ignores_backup_html(monkeypatch: pytest.MonkeyPatch) -> None:
    node = DeliveryNode()
    monkeypatch.setattr(node, "has_tool", lambda name: name == "glob")

    async def call_tool(_name: str, **_kwargs: Any) -> list[str]:
        return [
            "pages/page-1.pptx.html",
            "pages/_backup/page-1.pptx.html",
            "pages/_backup/page-2.pptx.html",
            "pages/_backup/page-3.pptx.html",
            "pages/page-2.pptx.html",
            "pages/page-3.pptx.html",
        ]

    monkeypatch.setattr(node, "call_tool", call_tool)
    assert await node._check_pages("/tmp/pages", 3) is True


def test_parse_outline_pages_reads_titles() -> None:
    pages = parse_outline_pages(_SAMPLE_OUTLINE)
    assert pages[0] == (1, "杭州一日游", "西湖与城市印象")
    assert pages[-1][1] == "感谢聆听"


def test_build_delivery_summary_skeleton_uses_literal_start_and_real_pages() -> None:
    text = build_delivery_summary_skeleton(
        pptx_filename="杭州旅游.pptx",
        total_pages=3,
        delivery_status="ok",
        send_file_status="sent",
        pages_ok=True,
        outline_text=_SAMPLE_OUTLINE,
        topic="杭州旅游",
        style_id="business-classic",
        speaker_notes_status="skipped",
        need_speaker_notes=False,
        has_documents=False,
        image_map_path="",
        export_status="ok",
    )
    assert text.startswith(DELIVERY_SUMMARY_START)
    assert "《杭州旅游.pptx》" in text
    assert "共 3 页" in text
    assert "一日游路线与必看景点" in text
    assert "- P1：杭州一日游" in text
    assert "- P2：上午西湖下午灵隐" in text
    assert "西湖与城市印象" not in text
    assert "按时间排列的游览顺序" not in text
    assert "未请求演讲备注" in text
    assert "文件位置" not in text
    assert "D:/" not in text and "C:/" not in text


def test_build_delivery_summary_keeps_only_real_pages_when_fewer_than_three() -> None:
    outline = """### P1: 封面
- **标题**：仅一页
- **内容概要**：杭州
"""
    text = build_delivery_summary_skeleton(
        pptx_filename="杭州旅游.pptx",
        total_pages=1,
        delivery_status="ok",
        send_file_status="sent",
        pages_ok=True,
        outline_text=outline,
        topic="杭州旅游",
        style_id="tech-minimal",
        export_status="ok",
    )
    assert "- P1：仅一页" in text
    assert "- P1：仅一页 - 杭州" not in text
    assert "- P2：" not in text
    assert "- P3：" not in text


def test_build_delivery_summary_omits_long_outline_page_bodies() -> None:
    long_core = (
        "以数据卡片+ECharts图表呈现2025年杭州旅游核心成果与2026年趋势研判。"
        "第一区块规模与增长：全域游客23580.3万人次。"
    )
    outline = f"""### P1: 封面
- **标题**：杭州旅游工作汇报
- **内容概要**：封面展示汇报主题
### P2: 数据
- **标题**：杭州旅游经济稳中向好
- **内容概要**：{long_core}
"""
    text = build_delivery_summary_skeleton(
        pptx_filename="杭州旅游.pptx",
        total_pages=2,
        delivery_status="ok",
        send_file_status="sent",
        pages_ok=True,
        outline_text=outline,
        topic="杭州旅游",
        style_id="business-classic",
        export_status="ok",
    )
    assert "- P1：杭州旅游工作汇报" in text
    assert "- P2：杭州旅游经济稳中向好" in text
    assert long_core not in text
    assert "封面展示汇报主题" not in text


@pytest.mark.asyncio
async def test_p10_emits_skeleton_when_send_succeeds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pptx = tmp_path / "杭州旅游.pptx"
    pptx.write_bytes(b"dummy-pptx")
    (tmp_path / "outline.md").write_text(_SAMPLE_OUTLINE, encoding="utf-8")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    node = DeliveryNode()
    monkeypatch.setattr(
        node,
        "has_tool",
        lambda name: name in {"send_file_to_user", "glob", "read_file"},
    )

    async def call_tool(name: str, **kwargs: Any) -> Any:
        if name == "send_file_to_user":
            return "成功发送 1 个文件"
        if name == "glob":
            return [
                str(pages_dir / "page-1.pptx.html"),
                str(pages_dir / "_backup" / "page-1.pptx.html"),
            ]
        if name == "read_file":
            return SimpleNamespace(success=True, data={"content": _SAMPLE_OUTLINE})
        raise AssertionError(name)

    monkeypatch.setattr(node, "call_tool", call_tool)
    result = await node._execute(
        {
            "output_dir": str(tmp_path),
            "pages_dir": str(pages_dir),
            "pptx_path": str(pptx),
            "pptx_filename": "杭州旅游.pptx",
            "export_status": "ok",
            "page_count": 0,
            "total_pages": 1,
            "topic": "杭州旅游",
            "style_id": "business-classic",
        }
    )
    assert result["delivery_status"] == "ok"
    assert result["send_file_status"] == "sent"
    assert str(result["delivery_summary"]).startswith(DELIVERY_SUMMARY_START)
    assert result["__artifact__"]["delivery_summary"].startswith(DELIVERY_SUMMARY_START)
    assert result["__artifact__"]["info"]["total_pages"] == 1
    assert "页数：1" in result["summary"]


@pytest.mark.asyncio
async def test_p10_does_not_emit_skeleton_when_send_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pptx = tmp_path / "杭州旅游.pptx"
    pptx.write_bytes(b"dummy-pptx")
    node = DeliveryNode()
    monkeypatch.setattr(node, "has_tool", lambda name: name in {"send_file_to_user", "glob"})

    async def call_tool(name: str, **_kwargs: Any) -> Any:
        if name == "send_file_to_user":
            return "发送文件失败"
        if name == "glob":
            return ["page-1.pptx.html"]
        raise AssertionError(name)

    monkeypatch.setattr(node, "call_tool", call_tool)
    result = await node._execute(
        {
            "output_dir": str(tmp_path),
            "pages_dir": str(tmp_path / "pages"),
            "pptx_path": str(pptx),
            "pptx_filename": "杭州旅游.pptx",
            "export_status": "ok",
            "page_count": 1,
            "total_pages": 1,
        }
    )
    assert result["send_file_status"] == "failed"
    assert result["delivery_summary"] == ""


@pytest.mark.asyncio
async def test_root_stream_does_not_emit_summary_delta_after_flow_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """骨架不得在计划结束后以无 task_id 的 chat.delta 发出（会与过程尾同桶）。"""
    root = PPTGenRootNode()
    summary = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"

    async def should_skip(_subplan, _inputs) -> bool:
        return False

    async def run_subplan_stream(subplan, inputs, results, **_kwargs):
        if subplan is root._p1:
            inputs["has_documents"] = True
        if subplan is root._p3:
            inputs["doc_parse_ok"] = True
        if subplan is root._delivery:
            inputs["delivery_summary"] = summary
        result = {"node": subplan.plan_name, "status": "ok"}
        results.append({"node": subplan.plan_name, "status": "ok", "result": result})
        yield result

    monkeypatch.setattr(root, "should_skip_subplan", should_skip)
    monkeypatch.setattr(root, "_run_subplan_stream", run_subplan_stream)

    chunks = [chunk async for chunk in root._execute_stream({})]
    assert chunks[-1]["message"] == "PPT生成任务流执行完成"
    assert all(chunk.get("event_type") != "chat.delta" for chunk in chunks)
    assert all(
        DELIVERY_SUMMARY_START not in str(chunk.get("content") or "")
        for chunk in chunks
    )


def test_wrap_skill_turbo_result_queues_ppt_skeleton_for_post_tool_emit() -> None:
    clear_pending_ppt_delivery_summary()
    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
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
    assert DELIVERY_SUMMARY_START not in wrapped["result"]
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
    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
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
    assert written[0].payload["content"].startswith(DELIVERY_SUMMARY_START)
    assert take_pending_ppt_delivery_summary() == ""


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


@pytest.mark.asyncio
async def test_delivery_summary_rail_emits_after_skill_acceleration_exec() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.rails.delivery_summary_rail import (
        SkillTurboDeliverySummaryRail,
    )
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        set_pending_ppt_delivery_summary,
    )

    clear_pending_ppt_delivery_summary()
    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
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
    assert written[0].payload["content"].startswith(DELIVERY_SUMMARY_START)
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
    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
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
    set_pending_ppt_delivery_summary(f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成\n")
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


def _holder_with_skeleton(skeleton: str) -> dict[str, Any]:
    return {
        "p2_requirement_collect": {"info": {"content_pages": 1, "total_pages": 3}},
        "p10_delivery": {
            "info": {"send_file_status": "sent", "delivery_summary_emitted": True},
            "files": [{"path": "杭州旅游.pptx"}],
            "delivery_summary": skeleton,
        },
    }


def test_visible_finish_text_prefers_p10_skeleton() -> None:
    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"
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
        _holder_with_skeleton(f"{DELIVERY_SUMMARY_START}\n"),
        success=False,
        detail="SkillAccelerationExec 未处理: boom",
    )
    assert text == "SkillAccelerationExec 未处理: boom"
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in text


def test_requirement_artifact_uses_content_and_total_pages() -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import (
        _set_requirement_artifact,
    )

    ctx: dict[str, Any] = {
        "topic": "杭州旅游",
        "page_count": 1,
        "style_id": "business-classic",
        "audience": "企业高管",
        "presentation_purpose": "产品展示",
    }
    _set_requirement_artifact(ctx)
    info = ctx["__artifact__"]["info"]
    assert info["content_pages"] == 1
    assert info["total_pages"] == 3
    assert "page_count" not in info


@pytest.mark.asyncio
async def test_resume_stream_emits_skeleton_not_artifact_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    skeleton = f"{DELIVERY_SUMMARY_START}\n\n✅ 已完成：PPT 生成\n"

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
    assert deltas[-1].startswith(DELIVERY_SUMMARY_START)
    assert _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER not in deltas[-1]
    assert finals
    assert str(finals[-1]).strip() == ""
    assert chunks[-1].is_complete is True
