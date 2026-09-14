# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for PptCommon.read_file_with_retry (transient read-error retry).

背景：底层 per-file SQLite 读写锁存在 use-after-close 竞态
（"44.PPT任务未完成"案例），read_file 可能瞬时报
"Cannot operate on a closed database"；失败调用的清理路径会驱逐陈旧锁
实例，紧随其后的重试走全新锁实例即可成功。

红线（本文件逐条锁定）：
- AbortError（HITL 中断）必须透传且不重试；
- CancelledError 必须自然穿透（不吞取消信号）；
- 重试后仍失败返回 ""，与既有各节点 _read_file 行为一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon


class _FakeNode:
    """按脚本顺序执行 call_tool 的桩节点。"""

    def __init__(
        self,
        *,
        tools: set[str] | None = None,
        script: list[Any] | None = None,
    ) -> None:
        self.tools = tools if tools is not None else {"read_file"}
        self.script = list(script or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        action = self.script.pop(0) if self.script else {"content": ""}
        if isinstance(action, BaseException):
            raise action
        return action


@pytest.mark.asyncio
async def test_retry_success_after_transient_error():
    node = _FakeNode(
        script=[
            RuntimeError("Cannot operate on a closed database"),
            {"content": "hello outline"},
        ]
    )
    text = await PptCommon.read_file_with_retry(node, "a.md", log_prefix="[T]")
    assert text == "hello outline"
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_success_first_attempt_no_extra_retry():
    node = _FakeNode(script=[{"content": "ok"}])
    text = await PptCommon.read_file_with_retry(node, "a.md")
    assert text == "ok"
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_abort_error_propagates_without_retry():
    node = _FakeNode(script=[AbortError("hitl interrupt")])
    with pytest.raises(AbortError):
        await PptCommon.read_file_with_retry(node, "a.md")
    # HITL 中断不得重试：仅一次调用
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_error_passes_through():
    node = _FakeNode(script=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await PptCommon.read_file_with_retry(node, "a.md")
    # 取消信号不吞、不重试
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_persistent_failure_returns_empty():
    node = _FakeNode(
        script=[
            RuntimeError("Cannot operate on a closed database"),
            RuntimeError("still closed"),
        ]
    )
    text = await PptCommon.read_file_with_retry(node, "a.md")
    assert text == ""
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_empty_path_and_missing_tool_short_circuit():
    node = _FakeNode()
    assert await PptCommon.read_file_with_retry(node, "") == ""
    assert node.calls == []

    node_no_tool = _FakeNode(tools=set())
    assert await PptCommon.read_file_with_retry(node_no_tool, "a.md") == ""
    assert node_no_tool.calls == []


@pytest.mark.asyncio
async def test_failed_result_parsed_as_empty():
    # parse_tool_file_content 对 success=False 的返回值返回空串（既有契约不变）
    node = _FakeNode(script=[{"success": False}])
    text = await PptCommon.read_file_with_retry(node, "a.md")
    assert text == ""
