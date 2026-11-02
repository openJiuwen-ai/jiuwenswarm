# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AbortError 全链不吞守卫点（S_06 N2/N3，架构铁律 1）。

防护意图：HITL 中断被宽泛 except 吞掉是历史"中断失效/用户中断被降级成
契约失败"类事故的根因形态（PR 5306：deep_research 宽泛 except 吞
AbortError 导致 ppt skill 注册中断）。14 处守卫点此前无逐点验证——
本文件锁定其中最易回归的 6 处核心守卫：
  plan_node.run / run_stream / execute_subplan（异常分流层）
  executor._run_rail_hook（rail 钩子层）
  executor.call_llm / stream_llm（LLM 重试循环层）

新增任何宽泛 except 前先跑本文件；改 tcid/HITL 链路时对照 S_06 P1 步 6 全表。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    ExecutorConfig,
    SkillTurboExecutor,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode


class _AbortLeaf(PlanNode):
    """叶子节点：执行即抛 AbortError（模拟 ask_user HITL 中断）。"""

    def __init__(self) -> None:
        super().__init__(plan_name="p2_ask", instruction="leaf", sub_plans=[])

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise AbortError("ask_user 中断")

    async def _execute_stream(self, inputs: dict[str, Any]):
        yield {"status": "progress"}
        raise AbortError("ask_user 中断")


class _NoopNode(PlanNode):
    """最小具体节点：仅作编排父节点/签名占位。"""

    def __init__(self, name: str = "root") -> None:
        super().__init__(plan_name=name, instruction="noop", sub_plans=[])

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}


def _leaf_with_fallback_spies() -> tuple[PlanNode, AsyncMock, AsyncMock]:
    node = _AbortLeaf()
    fallback_cb = AsyncMock(return_value={"node": "x", "status": "fallback"})
    fallback_stream_cb = AsyncMock()
    node._fallback_callback = fallback_cb
    node._fallback_stream_callback = fallback_stream_cb
    return node, fallback_cb, fallback_stream_cb


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = ("skill_codes",)
    env.model_client = None
    with (
        patch.object(SkillTurboExecutor, "_build_ask_user_rail", return_value=None),
        patch.object(SkillTurboExecutor, "_build_llm_retry_rail", return_value=None),
    ):
        return SkillTurboExecutor(env, config=ExecutorConfig())


# ───────────────────── plan_node 异常分流层守卫 ─────────────────────


@pytest.mark.asyncio
async def test_run_reraises_abort_error_without_fallback():
    """plan_node.run：AbortError 直通，不得进入节点 fallback（中断≠业务失败）。"""
    node, fallback_cb, _ = _leaf_with_fallback_spies()
    with pytest.raises(AbortError):
        await node.run({})
    assert fallback_cb.await_count == 0, "AbortError 进入 fallback 会把用户中断洗白成业务失败"


@pytest.mark.asyncio
async def test_run_stream_reraises_abort_error_without_fallback():
    """plan_node.run_stream：AbortError 直通，不进流式 fallback。"""
    node, _, fallback_stream_cb = _leaf_with_fallback_spies()
    with pytest.raises(AbortError):
        async for _ in node.run_stream({}):
            pass
    fallback_stream_cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_subplan_skips_after_callback_on_abort_error():
    """execute_subplan（非流式）：AbortError 透传且【不触发 after 回调】——防前端把中断 stage 误标完成。"""
    parent = _NoopNode("root")
    child = _AbortLeaf()
    before_cb = AsyncMock()
    after_cb = AsyncMock()
    parent._before_subplan_execute = before_cb
    parent._after_subplan_execute = after_cb

    with pytest.raises(AbortError):
        await parent.execute_subplan(child, {})

    before_cb.assert_awaited_once()
    assert after_cb.await_count == 0, "after 回调会把中断 stage 标记完成，破坏 resume 续跑（S_06 N12）"


@pytest.mark.asyncio
async def test_execute_subplan_calls_after_callback_on_business_error():
    """对照组：普通业务异常仍触发 after（failed 收口）——证明上一用例的中断抑制不是漏实现。"""
    parent = _NoopNode("root")

    class _BrokenLeaf(PlanNode):
        def __init__(self) -> None:
            super().__init__(plan_name="broken", instruction="leaf", sub_plans=[])

        async def _execute(self, inputs: dict[str, Any]) -> Any:
            raise RuntimeError("业务失败")

    child = _BrokenLeaf()
    after_cb = AsyncMock()
    parent._after_subplan_execute = after_cb

    with pytest.raises(RuntimeError):
        await parent.execute_subplan(child, {})
    after_cb.assert_awaited_once()


# ───────────────────── executor rail 钩子层守卫 ─────────────────────


@pytest.mark.asyncio
async def test_run_rail_hook_reraises_abort_error():
    """_run_rail_hook：rail 抛 AbortError 必须透传（HITL 经 rail 触发的主路径）。"""
    ex = _make_executor()
    rail = MagicMock()
    rail.before_tool_call = AsyncMock(side_effect=AbortError("权限中断"))
    ex._rails = [rail]

    with pytest.raises(AbortError):
        await ex._run_rail_hook("before_tool_call", SimpleNamespace())


@pytest.mark.asyncio
async def test_run_rail_hook_swallows_other_exceptions():
    """_run_rail_hook：rail 普通异常只告警不中断（rail 故障不得拖垮主流程）。"""
    ex = _make_executor()
    rail = MagicMock()
    rail.before_tool_call = AsyncMock(side_effect=RuntimeError("rail 内部错误"))
    ex._rails = [rail]

    await ex._run_rail_hook("before_tool_call", SimpleNamespace())  # 不应抛出


# ───────────────────── executor LLM 重试循环层守卫 ─────────────────────


def _abort_client(kind: str) -> tuple[MagicMock, list[int]]:
    """Fake model_client：invoke/stream 抛 AbortError，并统计调用次数。"""
    call_count = [0]

    async def invoke(messages, max_tokens=0, **kwargs):
        call_count[0] += 1
        raise AbortError("模型调用被中断")

    async def stream(messages, max_tokens=0, **kwargs):
        call_count[0] += 1
        raise AbortError("模型流被中断")
        yield  # pragma: no cover - 异步生成器标记

    client = MagicMock()
    client.invoke = invoke if kind == "invoke" else MagicMock(side_effect=AssertionError("invoke not expected"))
    client.stream = stream if kind == "stream" else MagicMock(side_effect=AssertionError("stream not expected"))
    return client, call_count


@pytest.mark.asyncio
async def test_call_llm_aborts_without_retry():
    """call_llm：AbortError 立即上抛、不消费重试额度（即使挂着重试 rail 语义）。"""
    client, call_count = _abort_client("invoke")
    ex = _make_executor()
    ex._env.model_client = client

    with pytest.raises(AbortError):
        await ex.call_llm("问题", node_name="p4_outline")

    assert call_count[0] == 1, f"AbortError 不得重试，实际调用 {call_count[0]} 次"


@pytest.mark.asyncio
async def test_stream_llm_aborts_without_retry():
    """stream_llm：AbortError 立即上抛、不重试。"""
    client, call_count = _abort_client("stream")
    ex = _make_executor()
    ex._env.model_client = client

    with pytest.raises(AbortError):
        async for _ in ex.stream_llm("问题", node_name="p4_outline"):
            pass

    assert call_count[0] == 1
