# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""退出路径 E1-E12 的帧级终态收口契约（S_01 执行路径全景 / S_08 事件顺序约束）。

防护意图（对应历史事故）：
- E8/E10/E11/E9 的帧序列曾是"前端永久转圈 / 面板停滞 in_progress / 暂停被判完成"
  类事故的高发区（根因"事件流与终态收口"，16 个 bugfix PR）；
- E10 外层"不发 chat.error"是防前端 activeRequestIdRef 抢跑终结的显式契约；
- E5/E6 的"无 plan.finished 不对称帧"与 E1 的"正常完整序列"此前均无任何
  帧级断言（13 条退出路径 0 条有完整帧级测试）。

本文件锁定每条退出路径"发出/不发出哪些终态帧"，是退出路径对称防护的基线：
改任何异常分支前，先跑本文件对照 E 表语义。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import (
    FallbackLimitExceededError,
    PlanCodeLoadError,
    PlanCodeValidationError,
    SkillTurboExecutor,
)
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    FallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
    )
    return SkillTurboExecutor(environment=env)


def _patch_outer_loop(monkeypatch, ex, *, root=None, prepare_raises=None):
    """把 execute_plan_stream 的外围依赖替换为最小桩，聚焦帧序列断言。"""
    ex._env.register_tools = AsyncMock()
    monkeypatch.setattr(ex, "_merge_env_config_to_inputs", lambda inputs: inputs)
    monkeypatch.setattr(ex, "_build_tool_loader_context", lambda *a, **k: {})
    monkeypatch.setattr(ex, "_setup_execution_context", lambda *a, **k: {})
    monkeypatch.setattr(ex, "_clear_stale_node_artifacts", AsyncMock())
    if prepare_raises is not None:
        def _raise_prepare(_code):
            raise prepare_raises

        monkeypatch.setattr(ex, "_prepare_root_node", _raise_prepare)
    else:
        monkeypatch.setattr(ex, "_prepare_root_node", lambda _code: root)
    monkeypatch.setattr(ex, "_initialize_pending_tasks", AsyncMock())
    monkeypatch.setattr(ex, "_reset_execution_context", lambda _tokens: None)
    monkeypatch.setattr(ex, "_finish_trace", AsyncMock())

    async def _no_task_events():
        return
        yield  # pragma: no cover - async generator marker

    monkeypatch.setattr(ex, "_drain_task_event_chunks", _no_task_events)
    return ex


async def _collect(stream) -> list[Any]:
    return [chunk async for chunk in stream]


def _event_types(chunks) -> list[str | None]:
    return [
        chunk.payload.get("event_type") if isinstance(chunk.payload, dict) else None
        for chunk in chunks
    ]


# ───────────────────── 外层 execute_plan_stream 帧序列 ─────────────────────


@pytest.mark.asyncio
async def test_e1_normal_completion_frame_sequence(monkeypatch):
    """E1 正常完成：plan.started 必须最先、plan.finished(final) + complete 收尾、无 chat.error。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        yield ex._make_chunk("r", "c", {"event_type": "chat.delta", "content": "A"})

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    chunks = await _collect(ex.execute_plan_stream("from fake import root", {}, "r", "c"))

    types = _event_types(chunks)
    assert types[0] == "plan.started", "plan.started 必须最先发出（前端依赖它建根节点）"
    assert types[-1] is None and chunks[-1].is_complete is True, "complete 终结帧必须是最后且 is_complete=True"

    finished = [c for c in chunks if isinstance(c.payload, dict) and c.payload.get("event_type") == "plan.finished"]
    assert len(finished) == 1
    assert finished[0].payload.get("final") is True
    assert "status" not in finished[0].payload, "正常完成不应携带 failed 状态"
    assert "chat.error" not in types


@pytest.mark.asyncio
async def test_e5_validation_error_frames_without_plan_finished(monkeypatch):
    """E5 校验失败：chat.error + complete 收口且【无 plan.finished】——不对称帧是既定契约，防止被"顺手补齐"。"""
    ex = _make_executor()
    _patch_outer_loop(
        monkeypatch, ex, prepare_raises=PlanCodeValidationError(["禁止 import os"])
    )
    monkeypatch.setattr(ex, "_execute_node_stream", MagicMock())

    chunks = await _collect(ex.execute_plan_stream("from fake import root", {}, "r", "c"))
    types = _event_types(chunks)

    assert types[0] == "plan.started"
    assert "chat.error" in types, "校验失败必须发 chat.error 告知原因"
    assert "plan.finished" not in types, "E5 帧序列不对称（无 plan.finished）是 S_01 记录的既定行为"
    assert types[-1] is None and chunks[-1].is_complete is True


@pytest.mark.asyncio
async def test_e6_load_error_frames_match_e5(monkeypatch):
    """E6 加载失败：与 E5 同构（chat.error + complete、无 plan.finished、不 raise）。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, prepare_raises=PlanCodeLoadError("规划代码执行失败: boom"))
    monkeypatch.setattr(ex, "_execute_node_stream", MagicMock())

    chunks = await _collect(ex.execute_plan_stream("from fake import root", {}, "r", "c"))
    types = _event_types(chunks)

    assert "chat.error" in types
    assert "plan.finished" not in types
    assert types[-1] is None and chunks[-1].is_complete is True


@pytest.mark.asyncio
async def test_e8_abort_error_no_terminal_frames(monkeypatch):
    """E8 HITL 中断：直接 raise，零终态帧（无 plan.finished/complete/chat.error）——终态归 adapter 的 invocation_paused。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        yield ex._make_chunk("r", "c", {"event_type": "chat.delta", "content": "A"})
        raise AbortError("ask_user 中断")

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    with pytest.raises(AbortError):
        await _collect(ex.execute_plan_stream("from fake import root", {}, "r", "c"))


@pytest.mark.asyncio
async def test_e8_abort_error_stream_has_no_terminal_chunk_payload(monkeypatch):
    """E8 中断瞬间已产出的帧里不得混入终态帧（流在 raise 前被收集的部分）。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))
    captured: list[Any] = []

    async def node_stream(*_args, **_kwargs):
        yield ex._make_chunk("r", "c", {"event_type": "chat.delta", "content": "A"})
        raise AbortError("ask_user 中断")

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    with pytest.raises(AbortError):
        async for chunk in ex.execute_plan_stream("from fake import root", {}, "r", "c"):
            captured.append(chunk)

    types = _event_types(captured)
    assert types == ["plan.started", "chat.delta"], (
        f"E8 只允许 plan.started 与中断前业务帧，实际: {types}（终态帧会毒化前端暂停收口，PR 5149 事故形态）"
    )


@pytest.mark.asyncio
async def test_e9_cancelled_error_propagates_without_terminal_frames(monkeypatch):
    """E9 用户取消：CancelledError 直接穿透（不进任何 except Exception 分支），无终态帧。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        raise asyncio.CancelledError()
        yield  # pragma: no cover - 异步生成器标记

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    captured: list[Any] = []
    with pytest.raises(asyncio.CancelledError):
        async for chunk in ex.execute_plan_stream("from fake import root", {}, "r", "c"):
            captured.append(chunk)

    assert _event_types(captured) == ["plan.started"]


@pytest.mark.asyncio
async def test_e10_fallback_contract_error_frames(monkeypatch):
    """E10 契约失败：plan.finished(failed) + complete 后 raise，【不发 chat.error】防前端抢跑终结。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        raise FallbackContractError(node_name="p4_outline", reason="交付物缺失")
        yield  # pragma: no cover - 异步生成器标记

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    captured: list[Any] = []
    with pytest.raises(FallbackContractError):
        async for chunk in ex.execute_plan_stream("from fake import root", {}, "r", "c"):
            captured.append(chunk)

    types = _event_types(captured)
    assert "chat.error" not in types, "降级路径禁发 chat.error（S_08 边界13：前端会抢先终结本轮）"
    finished = [c for c in captured if isinstance(c.payload, dict) and c.payload.get("event_type") == "plan.finished"]
    assert finished and finished[0].payload.get("status") == "failed"
    assert types[-1] is None and captured[-1].is_complete is True


@pytest.mark.asyncio
async def test_e11_fallback_limit_exceeded_frames(monkeypatch):
    """E11 fallback 超限：与 E10 同构（首次覆盖 FLEE 外层分支——3 次兜底耗尽的事故形态，问题70）。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        raise FallbackLimitExceededError("Fallback 次数超过限制: 3/3")
        yield  # pragma: no cover - 异步生成器标记

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    captured: list[Any] = []
    with pytest.raises(FallbackLimitExceededError):
        async for chunk in ex.execute_plan_stream("from fake import root", {}, "r", "c"):
            captured.append(chunk)

    types = _event_types(captured)
    assert "chat.error" not in types
    finished = [c for c in captured if isinstance(c.payload, dict) and c.payload.get("event_type") == "plan.finished"]
    assert finished and finished[0].payload.get("status") == "failed"
    assert types[-1] is None and captured[-1].is_complete is True


@pytest.mark.asyncio
async def test_e12_unknown_exception_frames(monkeypatch):
    """E12 未知异常：chat.error + plan.finished(failed) + complete，事件化吞掉不上抛。"""
    ex = _make_executor()
    _patch_outer_loop(monkeypatch, ex, root=MagicMock(name="root"))

    async def node_stream(*_args, **_kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover - 异步生成器标记

    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)
    chunks = await _collect(ex.execute_plan_stream("from fake import root", {}, "r", "c"))
    types = _event_types(chunks)

    assert "chat.error" in types
    finished = [c for c in chunks if isinstance(c.payload, dict) and c.payload.get("event_type") == "plan.finished"]
    assert finished and finished[0].payload.get("status") == "failed"
    assert types[-1] is None and chunks[-1].is_complete is True


# ───────────────────── 内层 _execute_node_stream 事件化分支 ─────────────────────


class _ProgressStageNode(PlanNode):
    """depth=1 stage：yield 一条进度后抛出指定异常（驱动各异常分支）。"""

    def __init__(self, exc_factory) -> None:
        super().__init__(plan_name="p0_pipeline_init", instruction="stage", sub_plans=[], depth=1)
        self._exc_factory = exc_factory

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}

    async def _execute_stream(self, inputs: dict[str, Any]):
        yield {"node": self.plan_name, "status": "progress", "message": "正在执行"}
        raise self._exc_factory()


class _StageRootNode(PlanNode):
    """depth=0 根节点：经 execute_subplan_stream 驱动二层 stage（触发任务回调链）。"""

    def __init__(self, stage: PlanNode) -> None:
        super().__init__(plan_name="ppt_gen_root", instruction="root", sub_plans=[stage], depth=0)

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return {"node": self.plan_name, "status": "ok"}

    async def _execute_stream(self, inputs: dict[str, Any]):
        async for chunk in self.execute_subplan_stream(self.sub_plans[0], inputs):
            yield chunk


async def _drive_inner(ex, root, *, disable_fallback: bool = False):
    """驱动内层流并返回 (chunks, raised, queue)，ContextVar 按模板测试管理。

    disable_fallback：置空节点 fallback 回调，用于直达 E3"异常逃逸 run_stream"
    分支（生产中对应 fallback 关闭或兜底自身失败的场景）。
    """
    from jiuwenswarm.server.runtime.skill_turbo import executor as executor_mod

    ex._bind_node_callbacks(root)
    if disable_fallback:
        stack = [root]
        while stack:
            node = stack.pop()
            node._fallback_callback = None
            node._fallback_stream_callback = None
            stack.extend(getattr(node, "sub_plans", []) or [])
    queue: list[dict] = []
    task_states: dict[str, dict[str, Any]] = {}
    token_queue = executor_mod._task_events_queue_var.set(queue)
    token_states = executor_mod._task_states_var.set(task_states)
    chunks: list[Any] = []
    raised: BaseException | None = None
    try:
        try:
            async for chunk in ex._execute_node_stream(root, {}, "request-1", "channel-1"):
                chunks.append(chunk)
        except BaseException as e:  # noqa: BLE001 - 测试需捕获全部异常分支
            raised = e
    finally:
        executor_mod._task_states_var.reset(token_states)
        executor_mod._task_events_queue_var.reset(token_queue)
    return chunks, raised, queue


@pytest.mark.asyncio
async def test_e3_node_error_eventized_and_stream_continues():
    """E3 节点普通异常（fallback 关闭场景）：转 node_error 事件 + task.complete(failed) 随流发出，流不终止。"""
    ex = _make_executor()
    root = _StageRootNode(_ProgressStageNode(lambda: RuntimeError("node boom")))
    chunks, raised, queue = await _drive_inner(ex, root, disable_fallback=True)

    assert raised is None, "普通节点异常必须事件化（node_error），不得终止流"
    payloads = [c.payload for c in chunks if isinstance(c.payload, dict)]
    node_errors = [p for p in payloads if p.get("event_type") == "chat.error"]
    # _execute_node_stream 直接驱动的是 root 节点，node_error 帧归属 root
    assert node_errors and node_errors[0].get("plan_name") == "ppt_gen_root"
    assert "node boom" in node_errors[0].get("error", "")
    task_completes = [p for p in payloads if p.get("event_type") == "task.complete"]
    assert task_completes and task_completes[-1].get("status") == "failed", (
        "失败 stage 的 task.complete(failed) 必须随流发出，否则前端面板停滞 in_progress"
    )
    assert queue == []


@pytest.mark.asyncio
async def test_e8_inner_hitl_flushes_task_start_keeps_in_progress():
    """E8 内层 HITL：task.start 快照 flush 后随流发出、stage 保持 in_progress（不发 complete）——resume 续跑契约。"""
    ex = _make_executor()
    root = _StageRootNode(_ProgressStageNode(lambda: AbortError("ask_user 中断")))
    chunks, raised, queue = await _drive_inner(ex, root)

    assert isinstance(raised, AbortError)
    payloads = [c.payload for c in chunks if isinstance(c.payload, dict)]
    task_starts = [p for p in payloads if p.get("event_type") == "task.start"]
    assert task_starts, "中断瞬间必须 flush 已入队的 task.start（防丢最新任务快照）"
    task_completes = [p for p in payloads if p.get("event_type") == "task.complete"]
    assert not task_completes, "HITL 中断不得发 task.complete（execute_subplan 不调 after），否则 resume 无法续跑"
    assert queue == []


@pytest.mark.asyncio
async def test_e9_inner_cancel_does_not_drain_task_events():
    """E9 内层取消：不 drain 任务事件队列直接 raise——唯一不 drain 的异常分支，锁定该例外语义。

    现行为（S_01 边界2）：task.start 已随进度 chunk 入流（enqueue_chunk 默认
    flush），execute_subplan_stream 的 finally 仍触发 after 回调（task.complete
    入队），但消费循环 CancelledError 分支不 drain——complete 滞留队列，前端
    面板可能滞留，由上层终止流收口。若改此行为请同步修订 spec。
    """
    ex = _make_executor()
    root = _StageRootNode(_ProgressStageNode(asyncio.CancelledError))
    chunks, raised, queue = await _drive_inner(ex, root)

    assert isinstance(raised, asyncio.CancelledError)
    payloads = [c.payload for c in chunks if isinstance(c.payload, dict)]
    assert not [p for p in payloads if p.get("event_type") == "task.complete"], (
        "E9 取消分支不 drain 队列——task.complete 不得随流发出（与 E8/E10/E11 的行为差异点）"
    )
    # 队列内原始事件未过 _normalize_task_event_type，无 event_type；
    # complete 事件以 payload 含 status（且非 task.update 的 tasks 列表）识别
    queued_completes = [
        e
        for e in queue
        if isinstance(e, dict)
        and "status" in e.get("payload", {})
        and "tasks" not in e.get("payload", {})
    ]
    assert queued_completes, "after 回调入队的 task.complete 应滞留队列未被 drain"
    assert queued_completes[0]["payload"]["status"] in ("completed", "failed")
