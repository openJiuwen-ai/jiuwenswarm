"""针对最小修复 B+D 的回归测试。

方案 B：P8 汇总用盘面事实清洗 missing_pages（head 指纹投票误报自愈）。
方案 D：ppt_gen_root 收尾感知 P10 delivery_status=failed，不再宣称"任务流执行完成"。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import (
    PPTGenRootNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PPTPageGenNode,
)


def _make_p8_node() -> PPTPageGenNode:
    return PPTPageGenNode()


def _outline_block(page_type: str) -> str:
    """构造可被 _detect_page_type 识别的 outline 页片段。"""
    return f"- **类型**：{page_type}"


def _fake_subplan_results(
    *,
    final_page_files: list[str],
    worker_missing: list[int],
    outline_pages: dict[int, str] | None = None,
) -> dict[str, type]:
    """构造 P8.0/P8.1/P8.2 的 execute_subplan 假实现映射（按 plan_name 分发）。"""
    if outline_pages is None:
        # 默认 P1=cover、P2=agenda（结构页，投票易误报的场景）
        outline_pages = {1: _outline_block("cover"), 2: _outline_block("agenda")}
    prepare_result: dict[str, Any] = {
        "prepare_status": "ok",
        "outline_pages": outline_pages,
        "research_pages": {},
        "outline_text": "t",
        "style_text": "s",
        "all_pages": sorted(outline_pages.keys()),
        "total_pages": len(outline_pages),
    }
    worker_result: dict[str, Any] = {
        "page_files": [
            f"page-{p}.pptx.html" for p in outline_pages if p not in worker_missing
        ],
        "missing_pages": list(worker_missing),
        "low_density_pages": [],
        "density_report": {},
        "outline_text": "t",
        "style_text": "s",
    }
    qa_result: dict[str, Any] = {
        "qa_status": "ok",
        "final_page_files": list(final_page_files),
        "fix_report": "",
    }

    async def fake_execute_subplan(subplan: Any, inputs: dict[str, Any]) -> dict[str, Any]:
        name = getattr(subplan, "plan_name", "")
        if name == "p8_0_prepare":
            return prepare_result
        if name == "p8_1_page_worker":
            return worker_result
        if name == "p8_2_qa_fix":
            return qa_result
        return {}

    return fake_execute_subplan  # type: ignore[return-value]


_P8_INPUTS: dict[str, Any] = {
    "output_dir": "/o",
    "pages_dir": "/p",
    "style_file_path": "/s/style.md",
    "style_id": "tech-minimal",
    "page_count": 2,
    "total_pages": 2,
}


@pytest.mark.asyncio
async def test_p8_cleans_missing_for_structural_page_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构页投票误报自愈：agenda 在盘被投票判 missing → 汇总清掉，不再计缺页。"""
    node = _make_p8_node()
    fake = _fake_subplan_results(
        final_page_files=["page-1.pptx.html", "page-2.pptx.html"],
        worker_missing=[2],
    )
    monkeypatch.setattr(node, "execute_subplan", fake)

    result = await node._execute(dict(_P8_INPUTS))

    assert result["missing_pages"] == []
    assert result["ppt_gen_status"] == "ok"
    assert result["__artifact__"]["info"]["missing_count"] == 0


@pytest.mark.asyncio
async def test_p8_keeps_missing_for_content_page_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容页在盘被投票判 missing 保留：可能是流式输出损坏（bad case 35/46），
    P9 缺页硬门禁需继续拦截——清洗不得让一致性校验失效。"""
    node = _make_p8_node()
    fake = _fake_subplan_results(
        final_page_files=["page-1.pptx.html", "page-2.pptx.html"],
        worker_missing=[2],
        outline_pages={1: _outline_block("cover"), 2: _outline_block("content")},
    )
    monkeypatch.setattr(node, "execute_subplan", fake)

    result = await node._execute(dict(_P8_INPUTS))

    assert result["missing_pages"] == [2]
    assert result["ppt_gen_status"] == "partial"
    assert result["__artifact__"]["info"]["missing_count"] == 1


@pytest.mark.asyncio
async def test_p8_keeps_missing_when_page_truly_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真缺页（未落盘）不受清洗影响：P9 硬门禁仍生效。"""
    node = _make_p8_node()
    fake = _fake_subplan_results(
        final_page_files=["page-1.pptx.html"],
        worker_missing=[2],
    )
    monkeypatch.setattr(node, "execute_subplan", fake)

    result = await node._execute(dict(_P8_INPUTS))

    assert result["missing_pages"] == [2]
    assert result["ppt_gen_status"] == "partial"
    assert result["__artifact__"]["info"]["missing_count"] == 1


async def _noop_run_subplan(
    subplan: Any, inputs: dict[str, Any], results: list[dict[str, Any]]
) -> None:
    return None


async def _noop_run_p3_and_p2(
    inputs: dict[str, Any], results: list[dict[str, Any]]
) -> None:
    return None


@pytest.mark.asyncio
async def test_root_execute_reports_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方案 D（非流式）：delivery_status=failed → status=error，不再宣称执行完成。"""
    node = PPTGenRootNode()
    monkeypatch.setattr(node, "_run_subplan", _noop_run_subplan)
    monkeypatch.setattr(node, "_run_p3_and_p2", _noop_run_p3_and_p2)

    result = await node._execute(
        {"delivery_status": "failed", "summary": "PPT 生成失败，HTML 页面目录：/x"}
    )

    assert result["status"] == "error"
    assert result["message"] == "PPT生成任务流执行失败：PPTX 导出或交付未成功"
    # 用户可见 message 不得透传 summary 等动态内容（含本地路径等内部信息）
    assert "/x" not in result["message"]


@pytest.mark.asyncio
async def test_root_execute_ok_when_delivery_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：delivery_status 非 failed 时收尾保持原行为（status=ok + 完成文案）。"""
    node = PPTGenRootNode()
    monkeypatch.setattr(node, "_run_subplan", _noop_run_subplan)
    monkeypatch.setattr(node, "_run_p3_and_p2", _noop_run_p3_and_p2)

    result = await node._execute({"delivery_status": "ok"})

    assert result["status"] == "ok"
    assert result["message"] == "PPT生成任务流执行完成"


@pytest.mark.asyncio
async def test_root_stream_reports_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方案 D（流式）：delivery_status=failed → 尾 chunk status=error。"""

    async def _empty_stream(*args: Any, **kwargs: Any):
        return
        yield  # pragma: no cover

    async def _not_skipped(subplan: Any, inputs: dict[str, Any]) -> bool:
        return False

    node = PPTGenRootNode()
    monkeypatch.setattr(node, "_run_subplan_stream", _empty_stream)
    monkeypatch.setattr(node, "_run_p3_and_p2_stream", _empty_stream)
    monkeypatch.setattr(node, "should_skip_subplan", _not_skipped)

    chunks = [
        chunk
        async for chunk in node._execute_stream(
            {"delivery_status": "failed", "summary": "PPT 生成失败，HTML 页面目录：/x"}
        )
    ]

    assert chunks, "应至少产出一个收尾 chunk"
    assert chunks[-1]["status"] == "error"
    # 固定中性文案，不透传 summary（含路径）等动态内容
    assert chunks[-1]["message"] == "PPT生成任务流执行失败：PPTX 导出或交付未成功"
    assert "/x" not in chunks[-1]["message"]
