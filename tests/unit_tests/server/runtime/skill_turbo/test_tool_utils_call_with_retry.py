# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for tool_utils.call_tool_with_retry (transient tool-error retry).

背景：底层 per-file SQLite 读写锁存在 use-after-close 竞态
（"44.PPT任务未完成"案例），read_file/write_file 可能以
``success=False, error="Cannot operate on a closed database"`` 的形式
瞬态失败（也可能直接抛异常）；失败调用的清理路径会驱逐陈旧锁实例，
紧随其后的重试走全新锁实例即可成功。

红线（本文件逐条锁定）：
- AbortError（HITL 中断）必须透传且不重试；
- CancelledError 必须自然穿透（不吞取消信号）；
- 瞬态失败（异常或 success=False 瞬态错误）自动重试 1 次；
- 非瞬态业务失败（success=False 但错误不匹配瞬态模式）不重试，
  原样返回，由调用方按既有逻辑处理；
- 重试后仍失败：返回最终失败结果（不吞错误信息）或抛出最终异常。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.runtime.tool_utils import (
    call_tool_with_retry,
)


class _FakeNode:
    """按脚本顺序执行 call_tool 的桩节点。"""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        action = self.script.pop(0) if self.script else {"content": ""}
        if isinstance(action, BaseException):
            raise action
        return action


_CLOSED_DB = (
    "file system operation execution error, execution: write_file, "
    "reason: Cannot operate on a closed database."
)


@pytest.mark.asyncio
async def test_retry_success_after_transient_exception():
    node = _FakeNode(
        script=[RuntimeError("Cannot operate on a closed database"), {"content": "ok"}]
    )
    result = await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    assert result == {"content": "ok"}
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_retry_success_after_transient_success_false():
    # 2026-09-15 full.log L7560 实际形态：success=False 返回值（非异常）
    node = _FakeNode(
        script=[
            SimpleNamespace(success=False, error=_CLOSED_DB),
            SimpleNamespace(success=True, error=None),
        ]
    )
    result = await call_tool_with_retry(node, "write_file", log_prefix="[T]")
    assert result.success is True
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_retry_success_after_transient_success_false_dict():
    # dict 形态的显式失败（ToolOutput 兼容层）
    node = _FakeNode(
        script=[
            {"success": False, "error": _CLOSED_DB},
            {"content": "recovered"},
        ]
    )
    result = await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    assert result == {"content": "recovered"}
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_success_first_attempt_no_extra_retry():
    node = _FakeNode(script=[{"content": "ok"}])
    result = await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    assert result == {"content": "ok"}
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_abort_error_propagates_without_retry():
    node = _FakeNode(script=[AbortError("hitl interrupt")])
    with pytest.raises(AbortError):
        await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    # HITL 中断不得重试：仅一次调用
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_error_passes_through():
    node = _FakeNode(script=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    # 取消信号不吞、不重试
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_persistent_transient_exception_raises_final():
    node = _FakeNode(
        script=[
            RuntimeError("Cannot operate on a closed database"),
            RuntimeError("still closed"),
        ]
    )
    with pytest.raises(RuntimeError, match="still closed"):
        await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_persistent_transient_success_false_returns_final_result():
    # 重试后仍瞬态失败：返回最终结果（不吞错误信息），调用方按既有逻辑处理
    node = _FakeNode(
        script=[
            SimpleNamespace(success=False, error=_CLOSED_DB),
            SimpleNamespace(success=False, error="retry also closed"),
        ]
    )
    result = await call_tool_with_retry(node, "write_file", log_prefix="[T]")
    assert result.success is False
    assert result.error == "retry also closed"
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_non_transient_success_false_not_retried():
    # 非瞬态业务失败（如文件不存在）：不重试，原样返回
    node = _FakeNode(script=[SimpleNamespace(success=False, error="file not found")])
    result = await call_tool_with_retry(node, "read_file", log_prefix="[T]")
    assert result.success is False
    assert result.error == "file not found"
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_kwargs_passed_through():
    node = _FakeNode(script=[SimpleNamespace(success=True, error=None)])
    await call_tool_with_retry(
        node, "write_file", log_prefix="[T]", file_path="a.md", content="hello"
    )
    assert node.calls == [("write_file", {"file_path": "a.md", "content": "hello"})]
