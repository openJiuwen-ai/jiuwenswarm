"""ppt_gen_root 收尾感知 P10 delivery_status=failed，不再宣称"任务流执行完成"。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import (
    PPTGenRootNode,
)


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
    """非流式：delivery_status=failed → status=error，不再宣称执行完成。"""
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
    """流式：delivery_status=failed → 尾 chunk status=error。"""

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
