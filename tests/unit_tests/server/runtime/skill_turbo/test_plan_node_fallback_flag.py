# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""节点级 fallback 恢复机制：``_needs_fallback`` 标志检测（C-1）。

三态节点（ok/partial/failed）以 dict 返回自身硬失败而非 raise；返回值携带
``_needs_fallback=True`` 时框架转入 fallback（与 raise 同一通道）。本文件守护
plan_node 两层检测与防循环 guard：

- ``run()``（非流式）：返回值检测 → ``_fallback_callback``；结果替换。
- ``execute_subplan_stream()``（流式）：last_chunk 检测 → subplan 的
  ``_fallback_stream_callback``；fallback 产出续接在流中、last_chunk 替换为
  修复后结果（after 回调看到修复后结果）。
- ``_fallback_triggered`` guard：同节点一次 run 至多触发一次（防 fallback
  结果再带标志形成循环）。
- 未标注节点行为与改造前逐位一致（零变化）。

外部侧标注（C-2）：随技能侧仓落地（pptx-craft：P8.0 预处理失败 / P8 qa 全败 /
P9 页面恢复与 convert 失败）；门禁类失败（必填空 / template_canvas /
上游拒绝）不标注。本文件验证引擎侧消费者行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import PlanNode


class _StubNode(PlanNode):
    """按预设结果返回的叶子节点（可后续替换 _result 模拟二次失败）。"""

    def __init__(self, result: dict[str, Any], plan_name: str = "p_fake") -> None:
        super().__init__(plan_name=plan_name, instruction=plan_name, sub_plans=[], depth=1)
        self._result = result

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return self._result

    async def _execute_stream(self, inputs: dict[str, Any]):
        result = await self._execute(inputs)
        yield {**result, "node": self.plan_name}


class _RaisingNode(PlanNode):
    """_execute 直接抛异常的节点（守护原异常路径不受标志机制影响）。"""

    def __init__(self) -> None:
        super().__init__(plan_name="p_raiser", instruction="p_raiser", sub_plans=[], depth=1)

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        raise ValueError("boom")

    async def _execute_stream(self, inputs: dict[str, Any]):
        yield await self._execute(inputs)


class _RootNode(PlanNode):
    """编排根节点：仅用于调用 execute_subplan_stream。"""

    def __init__(self) -> None:
        super().__init__(plan_name="root", instruction="root", sub_plans=[], depth=0)

    async def _execute(self, inputs: dict[str, Any]) -> Any:
        return inputs


class TestRunFallbackFlag:
    """run()（非流式）返回值标志检测。"""

    @pytest.mark.asyncio
    async def test_no_flag_passthrough(self):
        node = _StubNode({"status": "ok", "ppt_gen_status": "ok"})
        node._fallback_callback = None
        out = await node.run({})
        assert out == {"status": "ok", "ppt_gen_status": "ok"}

    @pytest.mark.asyncio
    async def test_flag_triggers_fallback_and_replaces_result(self):
        node = _StubNode(
            {"status": "failed", "ppt_gen_status": "failed", "_needs_fallback": True,
             "message": "qa all failed"}
        )
        seen: list[tuple[str, str]] = []

        async def fallback(node_arg, inputs, error):
            seen.append((node_arg.plan_name, str(error)))
            return {"status": "ok", "recovered": True}

        node._fallback_callback = fallback
        out = await node.run({})
        assert out == {"status": "ok", "recovered": True}
        # 合成 error 携带失败 message，fallback subagent 能看到失败原因
        assert len(seen) == 1
        assert seen[0][0] == "p_fake"
        assert "qa all failed" in seen[0][1]

    @pytest.mark.asyncio
    async def test_flag_guard_prevents_second_trigger(self):
        node = _StubNode({"_needs_fallback": True, "message": "first failure"})
        calls: list[str] = []

        async def fallback(node_arg, inputs, error):
            calls.append(str(error))
            return {"recovered": True}

        node._fallback_callback = fallback
        assert (await node.run({}))["recovered"] is True
        # 同节点二次失败：guard 拦截，不再调 fallback；标志剥离后透传
        # （内部协议键不泄入返回值 / after 回调结果）
        node._result = {"_needs_fallback": True, "message": "second failure"}
        out2 = await node.run({})
        assert "_needs_fallback" not in out2
        assert out2.get("message") == "second failure"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_flag_without_callback_raises(self):
        node = _StubNode({"_needs_fallback": True, "message": "no handler"})
        node._fallback_callback = None
        with pytest.raises(RuntimeError, match="reported failure"):
            await node.run({})

    @pytest.mark.asyncio
    async def test_exception_path_still_triggers_fallback(self):
        node = _RaisingNode()

        async def fallback(node_arg, inputs, error):
            assert isinstance(error, ValueError)
            return {"recovered": "from-exception"}

        node._fallback_callback = fallback
        out = await node.run({})
        assert out == {"recovered": "from-exception"}


class TestExecuteSubplanStreamFallbackFlag:
    """execute_subplan_stream()（流式）last_chunk 标志检测。"""

    @pytest.mark.asyncio
    async def test_no_flag_stream_passthrough(self):
        root = _RootNode()
        sub = _StubNode({"ppt_gen_status": "ok"})
        chunks = [c async for c in root.execute_subplan_stream(sub, {})]
        assert chunks == [{"ppt_gen_status": "ok", "node": "p_fake"}]

    @pytest.mark.asyncio
    async def test_flag_fallback_chunks_append_and_replace_last_chunk(self):
        root = _RootNode()
        sub = _StubNode(
            {"ppt_gen_status": "failed", "_needs_fallback": True, "message": "qa all failed"}
        )

        async def fallback_stream(node_arg, inputs, error):
            assert "qa all failed" in str(error)
            yield {"status": "progress", "message": "fallback recovering"}
            yield {"ppt_gen_status": "ok", "recovered": True, "node": node_arg.plan_name}

        sub._fallback_stream_callback = fallback_stream
        chunks = [c async for c in root.execute_subplan_stream(sub, {})]
        # 原失败 chunk 已流式转发（用户可见过程），fallback 进度续接其后
        assert chunks[0]["ppt_gen_status"] == "failed"
        assert chunks[1] == {"status": "progress", "message": "fallback recovering"}
        # last_chunk 替换为修复结果，且标志剥离（不会流入门禁信号/inputs）
        assert chunks[-1]["recovered"] is True
        assert "_needs_fallback" not in chunks[-1]

    @pytest.mark.asyncio
    async def test_flag_guard_second_run_no_trigger(self):
        root = _RootNode()
        sub = _StubNode({"ppt_gen_status": "failed", "_needs_fallback": True})
        triggered: list[str] = []

        async def fallback_stream(node_arg, inputs, error):
            triggered.append(str(error))
            yield {"ppt_gen_status": "ok", "recovered": True}

        sub._fallback_stream_callback = fallback_stream
        first = [c async for c in root.execute_subplan_stream(sub, {})]
        assert first[-1].get("recovered") is True
        # 二次执行：guard 拦截，标志剥离后透传（不泄入 after 回调 result）
        second = [c async for c in root.execute_subplan_stream(sub, {})]
        assert len(second) == 1
        assert "_needs_fallback" not in second[0]
        assert len(triggered) == 1

    @pytest.mark.asyncio
    async def test_flag_without_callback_raises(self):
        root = _RootNode()
        sub = _StubNode({"ppt_gen_status": "failed", "_needs_fallback": True})
        sub._fallback_stream_callback = None
        with pytest.raises(RuntimeError, match="reported failure"):
            async for _ in root.execute_subplan_stream(sub, {}):
                pass

    @pytest.mark.asyncio
    async def test_exception_fallback_consumes_one_shot_guard(self):
        """run() 异常通道进入 fallback 同样置位一次性 guard（公开访问器可见）。

        修复前异常通道不置位，与标志通道不对称：同实例后续标志检测仍可
        再触发一次 fallback，违反 [[S_07]] N18「同节点一次 run 至多触发一次」。
        """
        node = _RaisingNode()

        async def fallback(node_arg, inputs, error):
            return {"recovered": True}

        node._fallback_callback = fallback
        assert (await node.run({}))["recovered"] is True
        assert node.try_begin_fallback() is False

    @pytest.mark.asyncio
    async def test_exception_fallback_guard_blocks_second_flag_trigger(self):
        """异常通道 fallback 末 chunk 再带标志不得经父节点二次触发 fallback。

        复现路径（修复前）：_execute_stream 抛异常 → run_stream 异常通道
        fallback（未置位 guard）→ fallback 末 chunk 携带 _needs_fallback=True
        → 父节点 execute_subplan_stream 的 try_begin_fallback() 判定通过
        → 同一次 run 内 fallback 二次触发（双层子代理）。
        """
        root = _RootNode()
        sub = _RaisingNode()
        calls: list[str] = []

        async def fallback_stream(node_arg, inputs, error):
            calls.append(str(error))
            yield {
                "status": "failed",
                "_needs_fallback": True,
                "message": "recovered but still bad",
            }

        sub._fallback_stream_callback = fallback_stream
        chunks = [c async for c in root.execute_subplan_stream(sub, {})]

        assert len(calls) == 1
        assert chunks[-1]["message"] == "recovered but still bad"
        # guard 已由异常通道置位：标志剥离，不再二次进入 fallback
        assert "_needs_fallback" not in chunks[-1]

    @pytest.mark.asyncio
    async def test_guard_reject_strips_flag_before_after_callback(self):
        """guard 拒绝路径：标志剥离后才进 after 回调（内部协议键不外泄）。"""
        root = _RootNode()
        sub = _StubNode({"ppt_gen_status": "failed", "_needs_fallback": True})

        async def fallback_stream(node_arg, inputs, error):
            yield {"ppt_gen_status": "ok", "recovered": True}
            raise AssertionError("guard 已置位，不应再触发 fallback")

        sub._fallback_stream_callback = fallback_stream
        # 预置 guard：模拟同一次 run 已触发过 fallback（try_begin_fallback 原子置位）
        assert sub.try_begin_fallback() is True
        after_results: list[Any] = []

        async def after(sub_arg, inputs, result):
            after_results.append(result)

        root._after_subplan_execute = after
        chunks = [c async for c in root.execute_subplan_stream(sub, {})]

        assert len(chunks) == 1
        assert "_needs_fallback" not in chunks[0]
        assert after_results and "_needs_fallback" not in after_results[0]
