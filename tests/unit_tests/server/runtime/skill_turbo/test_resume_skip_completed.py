# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HITL resume: skip real execution of completed depth-1 stages."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor
from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
    FallbackContractError,
    PlanNode,
)


class _LeafNode(PlanNode):
    def __init__(self, plan_name: str, depth: int = 1) -> None:
        super().__init__(plan_name=plan_name, instruction=plan_name, sub_plans=[], depth=depth)
        self.run_calls = 0
        self.run_stream_calls = 0

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        self.run_calls += 1
        return {"node": self.plan_name, "status": "ok", "ran": True}

    async def _execute_stream(self, inputs: dict[str, Any]):
        self.run_stream_calls += 1
        yield {"node": self.plan_name, "status": "ok", "ran": True}


class TestPlanNodeResumeSkip:
    @pytest.mark.asyncio
    async def test_execute_subplan_skips_run_when_callback_true(self):
        parent = _LeafNode("root", depth=0)
        child = _LeafNode("p0_pipeline_init", depth=1)
        before_n = after_n = 0

        async def before(_sp: PlanNode, _inp: dict[str, Any]) -> None:
            nonlocal before_n
            before_n += 1

        async def after(_sp: PlanNode, _inp: dict[str, Any], result: Any) -> None:
            nonlocal after_n
            after_n += 1
            assert result.get("resume_skip") is True
            assert result.get("skipped") is True

        async def should_skip(_sp: PlanNode, _inp: dict[str, Any]) -> bool:
            return True

        parent.set_runtime_callbacks(
            before_subplan_execute=before,
            after_subplan_execute=after,
            should_skip_subplan_execute=should_skip,
        )

        result = await parent.execute_subplan(child, {})
        assert child.run_calls == 0
        assert before_n == 1 and after_n == 1
        assert result["resume_skip"] is True
        assert result["message"] == "resume skip completed stage"

    @pytest.mark.asyncio
    async def test_execute_subplan_stream_skips_run_stream(self):
        parent = _LeafNode("root", depth=0)
        child = _LeafNode("p1_intent_classify", depth=1)
        after_n = 0

        async def after(_sp: PlanNode, _inp: dict[str, Any], result: Any) -> None:
            nonlocal after_n
            after_n += 1
            assert result.get("resume_skip") is True

        async def should_skip(_sp: PlanNode, _inp: dict[str, Any]) -> bool:
            return True

        parent.set_runtime_callbacks(
            after_subplan_execute=after,
            should_skip_subplan_execute=should_skip,
        )

        chunks = [c async for c in parent.execute_subplan_stream(child, {})]
        assert chunks == []
        assert child.run_stream_calls == 0
        assert after_n == 1

    @pytest.mark.asyncio
    async def test_execute_subplan_runs_when_callback_false(self):
        parent = _LeafNode("root", depth=0)
        child = _LeafNode("p2_requirement_collect", depth=1)

        async def should_skip(_sp: PlanNode, _inp: dict[str, Any]) -> bool:
            return False

        parent.set_runtime_callbacks(should_skip_subplan_execute=should_skip)
        result = await parent.execute_subplan(child, {})
        assert child.run_calls == 1
        assert result.get("ran") is True

    @pytest.mark.asyncio
    async def test_should_skip_subplan_delegates_to_callback(self):
        parent = _LeafNode("root", depth=0)
        child = _LeafNode("p0_pipeline_init", depth=1)

        async def should_skip(_sp: PlanNode, _inp: dict[str, Any]) -> bool:
            return _sp.plan_name == "p0_pipeline_init"

        parent.set_runtime_callbacks(should_skip_subplan_execute=should_skip)
        assert await parent.should_skip_subplan(child, {}) is True
        assert await parent.should_skip_subplan(
            _LeafNode("p2_requirement_collect", depth=1), {}
        ) is False


def _make_executor() -> SkillTurboExecutor:
    env = MagicMock()
    env.config = {}
    env.skill_code_import_prefixes = (
        "skill_turbo_codes_ppt.ppt",
    )
    return SkillTurboExecutor(environment=env)


def test_bubble_progress_marker_is_internal_executor_metadata() -> None:
    ex = _make_executor()
    node = _LeafNode("ppt_gen_root", depth=0)

    chunk = ex._make_node_delta_chunk(
        "request-1",
        "channel-1",
        node,
        {
            "node": "ppt_gen_root",
            "status": "progress",
            "message": "开始执行 Stage 1（1/14）",
            "_bubble_progress": True,
        },
        None,
    )

    assert chunk.payload["_bubble_progress"] is True
    assert "_bubble_progress" not in chunk.payload["data"]


@pytest.mark.asyncio
async def test_deferred_task_start_flushes_after_start_banner() -> None:
    """开始横幅入队后才释放延期的 task.start，避免 start 反超横幅。"""
    from jiuwenswarm.server.runtime.skill_turbo import executor as executor_mod

    ex = _make_executor()
    queue: list[dict] = []
    token = executor_mod._task_events_queue_var.set(queue)
    try:
        subplan = _LeafNode("stage_x", depth=1)
        task_states = {
            "task_1": {
                "task_id": "task_1",
                "task_name": "stage_x",
                "status": "pending",
                "task_index": 1,
            }
        }
        ex._task_states_holder.update(task_states)
        ex._get_or_create_task_state = lambda sp, states: ("task_1", states["task_1"])  # type: ignore[method-assign]

        await ex._before_subplan_execute(subplan, {})
        assert queue == []
        assert len(ex._deferred_task_lifecycle_events) == 2
        assert (
            ex._deferred_task_lifecycle_events[0]["payload"].get("event_type")
            == "task.start"
            or "task_id" in ex._deferred_task_lifecycle_events[0]["payload"]
        )

        ex._flush_deferred_task_lifecycle_events()
        assert len(ex._deferred_task_lifecycle_events) == 0
        assert len(queue) == 2
        assert queue[0]["payload"].get("event_type") in (None, "task.start") or queue[
            0
        ]["payload"].get("task_id") == "task_1"
        assert queue[1]["payload"]["event_type"] == "task.update"
    finally:
        executor_mod._task_events_queue_var.reset(token)


@pytest.mark.asyncio
async def test_after_subplan_flushes_deferred_start_before_complete() -> None:
    """无中间 chunk 时，after 必须先 flush deferred start，再入队 complete。"""
    from jiuwenswarm.server.runtime.skill_turbo import executor as executor_mod

    ex = _make_executor()
    queue: list[dict] = []
    token_queue = executor_mod._task_events_queue_var.set(queue)
    token_ctx = executor_mod._current_task_context_var.set(
        {"task_id": "task_1", "start_time": 1.0}
    )
    try:
        subplan = _LeafNode("stage_x", depth=1)
        task_states = {
            "task_1": {
                "task_id": "task_1",
                "task_name": "stage_x",
                "status": "pending",
                "task_index": 1,
            }
        }
        ex._task_states_holder.update(task_states)
        ex._get_or_create_task_state = lambda sp, states: ("task_1", states["task_1"])  # type: ignore[method-assign]

        await ex._before_subplan_execute(subplan, {})
        assert queue == []
        assert len(ex._deferred_task_lifecycle_events) == 2

        await ex._after_subplan_execute(subplan, {}, {"status": "ok"})
        assert len(ex._deferred_task_lifecycle_events) == 0
        event_types = [
            evt.get("payload", {}).get("event_type")
            or ("task.start" if "task_id" in evt.get("payload", {}) and "status" not in evt.get("payload", {}) else None)
            for evt in queue
        ]
        # start (+update) must precede complete (+update)
        start_idx = next(
            i
            for i, evt in enumerate(queue)
            if evt.get("payload", {}).get("task_id") == "task_1"
            and "status" not in evt.get("payload", {})
            and evt.get("payload", {}).get("event_type") != "task.update"
        )
        complete_idx = next(
            i
            for i, evt in enumerate(queue)
            if evt.get("payload", {}).get("event_type") == "task.complete"
            or (
                evt.get("payload", {}).get("task_id") == "task_1"
                and evt.get("payload", {}).get("status") in ("completed", "failed")
            )
        )
        assert start_idx < complete_idx, event_types
    finally:
        executor_mod._current_task_context_var.reset(token_ctx)
        executor_mod._task_events_queue_var.reset(token_queue)


@pytest.mark.asyncio
async def test_bubble_progress_bypasses_delta_buffer(monkeypatch) -> None:
    """横幅单独即时发送，不能与已缓冲的 stage 文本合并。"""
    ex = _make_executor()
    root = _LeafNode("ppt_gen_root", depth=0)
    ex._env.register_tools = AsyncMock()

    monkeypatch.setattr(ex, "_merge_env_config_to_inputs", lambda inputs: inputs)
    monkeypatch.setattr(ex, "_build_tool_loader_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(ex, "_setup_execution_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(ex, "_clear_stale_node_artifacts", AsyncMock())
    monkeypatch.setattr(ex, "_prepare_root_node", lambda _code: root)
    monkeypatch.setattr(ex, "_initialize_pending_tasks", AsyncMock())
    monkeypatch.setattr(ex, "_reset_execution_context", lambda _tokens: None)
    monkeypatch.setattr(ex, "_finish_trace", AsyncMock())

    async def no_task_events():
        return
        yield  # pragma: no cover - async generator marker

    async def node_stream(*_args, **_kwargs):
        yield ex._make_chunk(
            "request-1", "channel-1", {"event_type": "chat.delta", "content": "A"}
        )
        yield ex._make_chunk(
            "request-1", "channel-1", {"event_type": "chat.delta", "content": "B"}
        )
        yield ex._make_chunk(
            "request-1",
            "channel-1",
            {
                "event_type": "chat.delta",
                "content": "C",
                "_bubble_progress": True,
            },
        )

    monkeypatch.setattr(ex, "_drain_task_event_chunks", no_task_events)
    monkeypatch.setattr(ex, "_execute_node_stream", node_stream)

    chunks = [
        chunk
        async for chunk in ex.execute_plan_stream(
            "from fake import root", {}, "request-1", "channel-1"
        )
    ]
    deltas = [
        chunk.payload
        for chunk in chunks
        if isinstance(chunk.payload, dict)
        and chunk.payload.get("event_type") == "chat.delta"
    ]

    assert [payload["content"] for payload in deltas] == ["A", "B", "C"]
    assert "_bubble_progress" not in deltas[0]
    assert "_bubble_progress" not in deltas[1]
    assert deltas[2]["_bubble_progress"] is True


class TestExecutorShouldSkipSubplanExecute:
    @pytest.mark.asyncio
    async def test_resume_skips_completed_depth1_only(self):
        ex = _make_executor()
        ex._resume_replay = True
        ex._task_states_holder = {
            "task_p0": {
                "task_id": "task_p0",
                "task_content": "p0_pipeline_init",
                "status": "completed",
            },
            "task_p1": {
                "task_id": "task_p1",
                "task_content": "p1_intent_classify",
                "status": "completed",
            },
            "task_p3": {
                "task_id": "task_p3",
                "task_content": "p3_document_parse",
                "status": "completed",
            },
            "task_p2": {
                "task_id": "task_p2",
                "task_content": "p2_requirement_collect",
                "status": "in_progress",
            },
        }

        p0 = _LeafNode("p0_pipeline_init", depth=1)
        p2 = _LeafNode("p2_requirement_collect", depth=1)
        deep = _LeafNode("p0_child", depth=2)

        assert await ex._should_skip_subplan_execute(p0, {}) is True
        assert await ex._should_skip_subplan_execute(p2, {}) is False
        assert await ex._should_skip_subplan_execute(deep, {}) is False

    @pytest.mark.asyncio
    async def test_non_resume_never_skips(self):
        ex = _make_executor()
        ex._resume_replay = False
        ex._task_states_holder = {
            "task_p0": {
                "task_id": "task_p0",
                "task_content": "p0_pipeline_init",
                "status": "completed",
            },
        }
        p0 = _LeafNode("p0_pipeline_init", depth=1)
        assert await ex._should_skip_subplan_execute(p0, {}) is False


class TestExecutorSuppressSubplanStartBanner:
    @pytest.mark.asyncio
    async def test_resume_suppresses_in_progress_only(self):
        ex = _make_executor()
        ex._resume_replay = True
        ex._task_states_holder = {
            "task_p2": {
                "task_id": "task_p2",
                "task_content": "p2_requirement_collect",
                "status": "in_progress",
            },
            "task_p5": {
                "task_id": "task_p5",
                "task_content": "p3_5_template_context",
                "status": "pending",
            },
        }
        p2 = _LeafNode("p2_requirement_collect", depth=1)
        p5 = _LeafNode("p3_5_template_context", depth=1)

        assert await ex._should_suppress_subplan_start_banner(p2, {}) is True
        assert await ex._should_suppress_subplan_start_banner(p5, {}) is False

    @pytest.mark.asyncio
    async def test_non_resume_never_suppresses(self):
        ex = _make_executor()
        ex._resume_replay = False
        ex._task_states_holder = {
            "task_p2": {
                "task_id": "task_p2",
                "task_content": "p2_requirement_collect",
                "status": "in_progress",
            },
        }
        p2 = _LeafNode("p2_requirement_collect", depth=1)
        assert await ex._should_suppress_subplan_start_banner(p2, {}) is False


class TestFallbackContractErrorSemantics:
    """FallbackContractError 语义（自 test_ppt_skill_code_registration 迁入，纯引擎）。"""

    @pytest.mark.asyncio
    async def test_run_rethrows_fallback_contract_without_llm_fallback(self):
        """PlanNode.run 对 FallbackContractError 原样重抛，不消耗 fallback 预算。"""

        class _RejectNode(PlanNode):
            def __init__(self) -> None:
                super().__init__(plan_name="t", instruction="t", sub_plans=[])

            async def _execute(self, inputs: dict) -> dict:
                raise FallbackContractError(node_name="t", reason="early reject")

        called = {"n": 0}

        async def _fallback(_node, _inputs, _err):
            called["n"] += 1
            return {"status": "fallback"}

        node = _RejectNode()
        node.set_runtime_callbacks(fallback=_fallback)
        with pytest.raises(FallbackContractError):
            await node.run({})
        assert called["n"] == 0
