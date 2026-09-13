# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PlanNode 基类 -- 规划代码动态生成的递归执行节点。

PlanNode 与 skill code 的契约 (v1):

1. 每个节点必须继承 PlanNode，并实现 async def _execute(self, inputs: dict) -> Any。
2. 禁止覆盖 run()，run() 由框架统一负责异常捕获和 fallback。
3. 节点初始化必须显式提供 plan_name(str)、instruction(str)、sub_plans(list[PlanNode])。
4. 节点输入统一为 dict[str, Any]，节点输出推荐为 dict，至少包含 node/status/result。
5. 复合节点通过遍历 self.sub_plans 并 await child.run(ctx) 完成子节点调度。
6. 节点访问外部能力仅可通过 self.has_tool / self.call_tool / self.call_llm / self.stream_llm。
7. 节点禁止直接 import os/subprocess 等系统模块，仅可 import skill_codes 内部模块。
8. 节点失败应直接 raise 异常，框架会自动触发 fallback，不要在节点内吞异常。
9. 每个 skill_code 必须有一个入口文件，且文件中必须暴露 root: PlanNode。
10. plan_name 在同一 skill 内应唯一，便于日志、trace 和 fallback 定位节点。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Union

# ─────────────────────────────────────────────────────────────────────────────
# AbortError 唯一触点：openjiuwen 的 HITL 中断异常。
# 整个 skill_turbo 与所有 skill_code 一律从本模块取 AbortError，
# 禁止在 plan_node 之外的任何地方再 `from openjiuwen... import AbortError`。
# skill_code 的静态白名单（validator）只放行 plan_node，不放行 openjiuwen，
# 故必须经此 re-export 才能让 skill_code 合法引用 AbortError。
# 注意：这是纯 re-export（同一类对象），不重新定义；rail 抛出的 AbortError
# 实例与本模块导出的 AbortError 是同一个类，isinstance / except 行为不变。
# ─────────────────────────────────────────────────────────────────────────────
from openjiuwen.core.runner.callback import AbortError

# ─────────────────────────────────────────────────────────────────────────────
# FallbackContractError 唯一触点（对 skill_code）：终止性降级信号。
# skill_code 白名单不放行 fallback_handler，须经本模块 re-export 引用。
# 定义仍在 fallback_handler；此处为同一类对象，except / isinstance 行为不变。
# ─────────────────────────────────────────────────────────────────────────────
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    FallbackContractError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AbortError",
    "DisableThinkingMixin",
    "FallbackContractError",
    "PlanNode",
]


class PlanNode(ABC):
    """规划节点 -- 递归结构，子类实现 async _execute，run 自带 fallback。"""

    def __init__(
        self,
        plan_name: str,
        instruction: str,
        sub_plans: list[PlanNode] | None = None,
        depth: int = 0,  # 节点深度（root=0, 二层节点=1, 三层节点=2...）
    ):
        self.plan_name = plan_name
        self.instruction = instruction
        self.depth = depth
        self.sub_plans = sub_plans or []
        
        # 先设置 self.depth，然后再为子节点设置深度（确保使用正确的 depth）
        self._update_subplans_depth()
        
        self._has_tool_callback: Callable[[str], bool] | None = None
        self._call_tool_callback: Callable[..., Awaitable[Any]] | None = None
        self._call_llm_callback: Callable[..., Awaitable[str]] | None = None
        self._stream_llm_callback: Callable[..., AsyncIterator[str]] | None = None
        self._fallback_callback: (
            Callable[[PlanNode, dict[str, Any], Exception], Awaitable[Any]] | None
        ) = None
        # 流式 fallback callback
        self._fallback_stream_callback: (
            Callable[[PlanNode, dict[str, Any], Exception], AsyncIterator[Any]] | None
        ) = None
        # JSON提取回调（新增）
        self._extract_json_callback: Callable[..., Any] | None = None
        self._log_callback: Callable[[PlanNode, str, str, tuple[Any, ...]], None] | None = None
        # 子节点执行回调（用于 task 事件追踪）
        self._before_subplan_execute: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[None]] | None
        ) = None
        self._after_subplan_execute: (
            Callable[[PlanNode, dict[str, Any], Any], Awaitable[None]] | None
        ) = None
        # HITL resume：已 completed 的二层 stage 短路跳过真实执行
        self._should_skip_subplan_execute: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[bool]] | None
        ) = None
        # HITL resume：in_progress 的二层 stage 重入时不广播「开始执行」横幅
        self._should_suppress_subplan_start_banner: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[bool]] | None
        ) = None

    def _update_subplans_depth(self) -> None:
        """递归更新所有子节点的深度。

        使用迭代方式遍历所有后代节点，仅访问公开属性 ``depth`` / ``sub_plans``，
        避免在其他实例上调用受保护方法（G.CLS.11）。
        """
        pending = [(subplan, self.depth + 1) for subplan in self.sub_plans]
        while pending:
            node, depth = pending.pop()
            node.depth = depth
            pending.extend((sub, depth + 1) for sub in node.sub_plans)

    def set_runtime_callbacks(
        self,
        *,
        has_tool: Callable[[str], bool] | None = None,
        use_tool: Callable[..., Awaitable[Any]] | None = None,
        call_llm: Callable[..., Awaitable[str]] | None = None,
        stream_llm: Callable[..., AsyncIterator[str]] | None = None,
        fallback: Callable[[PlanNode, dict[str, Any], Exception], Awaitable[Any]] | None = None,
        fallback_stream: Callable[[PlanNode, dict[str, Any], Exception], AsyncIterator[Any]] | None = None,
        extract_json: Callable[..., Any] | None = None,
        log: Callable[[PlanNode, str, str, tuple[Any, ...]], None] | None = None,
        before_subplan_execute: Callable[[PlanNode, dict[str, Any]], Awaitable[None]] | None = None,
        after_subplan_execute: Callable[[PlanNode, dict[str, Any], Any], Awaitable[None]] | None = None,
        should_skip_subplan_execute: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[bool]] | None
        ) = None,
        should_suppress_subplan_start_banner: (
            Callable[[PlanNode, dict[str, Any]], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self._has_tool_callback = has_tool
        self._call_tool_callback = use_tool
        self._call_llm_callback = call_llm
        self._stream_llm_callback = stream_llm
        self._fallback_callback = fallback
        self._fallback_stream_callback = fallback_stream
        self._extract_json_callback = extract_json
        self._log_callback = log
        self._before_subplan_execute = before_subplan_execute
        self._after_subplan_execute = after_subplan_execute
        self._should_skip_subplan_execute = should_skip_subplan_execute
        self._should_suppress_subplan_start_banner = should_suppress_subplan_start_banner
        for node in self.sub_plans:
            node.set_runtime_callbacks(
                has_tool=has_tool,
                use_tool=use_tool,
                call_llm=call_llm,
                stream_llm=stream_llm,
                fallback=fallback,
                fallback_stream=fallback_stream,
                extract_json=extract_json,
                log=log,
                before_subplan_execute=before_subplan_execute,
                after_subplan_execute=after_subplan_execute,
                should_skip_subplan_execute=should_skip_subplan_execute,
                should_suppress_subplan_start_banner=should_suppress_subplan_start_banner,
            )

    def log(self, level: str, message: str, *args: Any) -> None:
        """输出受控节点日志，供 plan_code 调试使用。"""
        if self._log_callback is None:
            return
        self._log_callback(self, level, message, args)

    def log_debug(self, message: str, *args: Any) -> None:
        self.log("debug", message, *args)

    def log_info(self, message: str, *args: Any) -> None:
        self.log("info", message, *args)

    def log_warning(self, message: str, *args: Any) -> None:
        self.log("warning", message, *args)

    def log_error(self, message: str, *args: Any) -> None:
        self.log("error", message, *args)

    def has_tool(self, tool_name: str) -> bool:
        if self._has_tool_callback is None:
            raise RuntimeError("PlanNode has_tool 回调未初始化")
        return self._has_tool_callback(tool_name)

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        if self._call_tool_callback is None:
            raise RuntimeError("PlanNode call_tool 回调未初始化")
        return await self._call_tool_callback(tool_name, **kwargs)

    async def call_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str | None = None,
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> str:
        """
        调用 LLM（子类无需覆盖，由 Executor 注入回调）。

        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            node_name: 节点名称（可选，默认使用 plan_name）
            concurrent: 是否处于并发上下文中。True 时 Executor 自动生成
                stream_source_id 并注入到本次产生的 llm_reasoning / llm_usage
                事件，方便前端按调用分桶。
            thinking: 可选语义 thinking（None=不注入，与改前请求体一致）
        """
        if self._call_llm_callback is None:
            raise RuntimeError("PlanNode call_llm 回调未初始化")
        return await self._call_llm_callback(
            prompt,
            system_prompt=system_prompt,
            node_name=node_name or self.plan_name,
            concurrent=concurrent,
            thinking=thinking,
        )

    async def stream_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str | None = None,
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> AsyncIterator[str]:
        """
        流式调用 LLM（子类无需覆盖，由 Executor 注入回调）。
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            node_name: 节点名称（可选，默认使用 plan_name）
            concurrent: 是否处于并发上下文中。True 时 Executor 自动生成
                stream_source_id 并注入到本次产生的 llm_reasoning / llm_usage
                事件，方便前端按调用分桶。
            thinking: 可选语义 thinking（None=不注入，与改前请求体一致）
        
        Yields:
            str: LLM 响应片段
        """
        if self._stream_llm_callback is None:
            raise RuntimeError("PlanNode stream_llm 回调未初始化")
        # 调用回调并逐个 yield
        async for chunk in self._stream_llm_callback(
            prompt,
            system_prompt=system_prompt,
            node_name=node_name or self.plan_name,
            concurrent=concurrent,
            thinking=thinking,
        ):
            yield chunk

    async def stream_llm_collect(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str | None = None,
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> str:
        """
        流式调用 LLM 并收集完整文本（协程，可用于 asyncio.gather）。
        """
        collected: list[str] = []
        async for chunk in self.stream_llm(
            prompt,
            system_prompt=system_prompt,
            node_name=node_name,
            concurrent=concurrent,
            thinking=thinking,
        ):
            collected.append(chunk)
        return "".join(collected)

    def extract_json(
        self,
        raw: Union[str, dict, list],
        expected_type: type = dict,
    ) -> Any:
        """
        从LLM返回值中健壮地提取JSON。
        
        通过回调机制调用Executor注入的JSON提取函数。
        这样可以保持与其他方法（use_tool、call_llm）的架构一致性，
        并且为未来扩展（日志、追踪、自定义解析）提供灵活性。
        
        Args:
            raw: LLM返回的原始数据
            expected_type: 期望的JSON类型（dict或list）
        
        Returns:
            解析后的JSON对象
        
        Raises:
            RuntimeError: 回调未初始化时抛出
            ValueError: 无法解析JSON时抛出
        
        Example:
            >>> response = await self.call_llm("生成JSON配置")
            >>> config = self.extract_json(response, expected_type=dict)
        """
        if self._extract_json_callback is None:
            raise RuntimeError("PlanNode extract_json 回调未初始化")
        return self._extract_json_callback(raw, expected_type)

    @abstractmethod
    async def _execute(self, inputs: dict[str, Any]) -> Any:
        """
        非流式执行核心逻辑（子类必须实现）。
        
        Args:
            inputs: 输入参数字典
        
        Returns:
            Any: 执行结果
        
        Note:
            此方法用于非流式场景（run()），返回普通值。
            如果需要流式输出，请覆盖 _execute_stream 方法。
        """
        ...

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[Any]:
        """
        流式执行核心逻辑（子类可选覆盖）。
        
        Args:
            inputs: 输入参数字典
        
        Yields:
            Any: 流式输出片段
        
        Note:
            默认实现调用 _execute 并 yield 结果。
            子类可以覆盖此方法实现真正的流式输出：
            
            Example:
                async def _execute_stream(self, inputs):
                    yield {"status": "progress", "message": "开始..."}
                    result = await self.call_llm(...)
                    yield {"status": "progress", "message": "完成"}
                    yield {"status": "ok", "result": result}
        """
        # 默认实现：调用非流式方法并 yield 结果
        result = await self._execute(inputs)
        yield result

    async def run(self, inputs: dict[str, Any]) -> Any:
        """非流式执行入口 -- 固定模板方法，不可覆盖，自带 fallback。

        重要：``AbortError``（PermissionInterruptRail HITL 中断）不进入 fallback，
        必须直接向上抛给 SkillTurbo / DeepAdapter，由其转 HITL 三件套。
        ``FallbackContractError`` 为终止性降级信号（如 P2 早拒未支持能力），
        同样不进入节点 LLM fallback，交由 executor 包成 SkillTurboNotHandled。
        """
        try:
            return await self._execute(inputs)
        except AbortError:
            raise
        except FallbackContractError:
            raise
        except Exception as e:
            logger.warning(
                "[PlanNode] node failed name=%s instruction=%s error=%r",
                self.plan_name,
                self.instruction,
                e,
            )
            if self._fallback_callback is None:
                raise
            return await self._fallback_callback(self, inputs, e)

    async def run_stream(self, inputs: dict[str, Any]) -> AsyncIterator[Any]:
        """
        流式执行入口 -- 固定模板方法，自带流式 fallback。

        Yields:
            Any: 流式输出片段
        """
        try:
            async for chunk in self._execute_stream(inputs):
                yield chunk
        except AbortError:
            # HITL 中断不走 fallback
            raise
        except FallbackContractError:
            # 终止性降级：不走节点 LLM fallback
            raise
        except Exception as e:
            if self._fallback_stream_callback is None:
                raise
            async for chunk in self._fallback_stream_callback(self, inputs, e):
                yield chunk

    @staticmethod
    def _resume_skip_result(subplan: "PlanNode") -> dict[str, Any]:
        return {
            "node": subplan.plan_name,
            "status": "ok",
            "message": "resume skip completed stage",
            "skipped": True,
            "resume_skip": True,
        }

    async def should_skip_subplan(
        self,
        subplan: "PlanNode",
        inputs: dict[str, Any],
    ) -> bool:
        """HITL resume 重放时，编排层可查询是否应静默跳过该二层 stage。"""
        if self._should_skip_subplan_execute is None:
            return False
        return await self._should_skip_subplan_execute(subplan, inputs)

    async def should_suppress_subplan_start_banner(
        self,
        subplan: "PlanNode",
        inputs: dict[str, Any],
    ) -> bool:
        """HITL resume 重放时，编排层可查询是否抑制「开始执行」进度横幅。"""
        if self._should_suppress_subplan_start_banner is None:
            return False
        return await self._should_suppress_subplan_start_banner(subplan, inputs)

    async def _maybe_skip_subplan_execute(
        self,
        subplan: "PlanNode",
        inputs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """若回调判定应跳过真实执行，触发 after 并返回 skip result；否则返回 None。"""
        if not await self.should_skip_subplan(subplan, inputs):
            return None
        result = self._resume_skip_result(subplan)
        if self._after_subplan_execute is not None:
            await self._after_subplan_execute(subplan, inputs, result)
        return result

    async def execute_subplan(self, subplan: PlanNode, inputs: dict[str, Any]) -> Any:
        """
        执行子节点，带有回调钩子。

        Args:
            subplan: 子节点
            inputs: 输入参数

        Returns:
            子节点执行结果
        """
        # 执行前回调
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        skipped = await self._maybe_skip_subplan_execute(subplan, inputs)
        if skipped is not None:
            return skipped

        try:
            result = await subplan.run(inputs)

            # 执行后回调（成功）
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, result)

            return result
        except AbortError:
            # HITL 中断：不调用 after_subplan_execute（避免被前端误判 task 完成），
            # 直接向上抛，让 executor / agent / adapter 处理。
            raise
        except Exception as e:
            # 执行后回调（失败）
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(subplan, inputs, e)
            raise

    async def skip_subplan(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        *,
        message: str = "已跳过",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """跳过子节点执行，仍触发 task 回调并将其标记为 completed。"""
        skip_result: dict[str, Any] = {
            "node": subplan.plan_name,
            "status": "ok",
            "message": message,
            "skipped": True,
        }
        if extra:
            skip_result.update(extra)

        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        if self._after_subplan_execute is not None:
            await self._after_subplan_execute(subplan, inputs, skip_result)

        return skip_result

    async def skip_subplan_stream(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        *,
        message: str = "已跳过",
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """流式跳过子节点：yield 一条跳过结果并完成任务追踪。"""
        skip_result: dict[str, Any] = {
            "node": subplan.plan_name,
            "status": "ok",
            "message": message,
            "skipped": True,
        }
        if extra:
            skip_result.update(extra)

        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        yield skip_result

        if self._after_subplan_execute is not None:
            await self._after_subplan_execute(subplan, inputs, skip_result)

    async def execute_subplan_stream(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
    ) -> AsyncIterator[Any]:
        """
        流式执行子节点，带有回调钩子。

        Args:
            subplan: 子节点
            inputs: 输入参数

        Yields:
            Any: 子节点的流式输出片段
        """
        # 执行前回调
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)

        skipped = await self._maybe_skip_subplan_execute(subplan, inputs)
        if skipped is not None:
            return

        last_chunk: Any = None
        error: Exception | None = None
        hitl_interrupt = False
        try:
            # 流式执行子节点，累积最后一个 chunk 作为子节点最终 result
            async for chunk in subplan.run_stream(inputs):
                last_chunk = chunk
                yield chunk
        except AbortError:
            # HITL 中断：不调用 after_subplan_execute，避免 task 被误判完成
            hitl_interrupt = True
            raise
        except Exception as e:
            error = e
            raise
        finally:
            # 无论正常结束、外层 close、还是异常，都触发 after 回调
            # 但 HITL 中断不能触发，否则前端会把任务标完成
            if self._after_subplan_execute is not None and not hitl_interrupt:
                try:
                    await self._after_subplan_execute(
                        subplan,
                        inputs,
                        error if error is not None else last_chunk,
                    )
                except Exception:
                    logger.exception(
                        "[PlanNode] after_subplan_execute callback failed: %s",
                        subplan.plan_name,
                    )

    def __repr__(self) -> str:
        return f"PlanNode(name={self.plan_name!r}, sub_plans={len(self.sub_plans)})"


class DisableThinkingMixin:
    """窄 seam：挂载节点的 call_llm / stream_llm 强制 thinking=\"off\"。

    仅 opt-in：业务节点显式多重继承本 Mixin 才会注入；默认 PlanNode 仍
    thinking=None（零注入）。stream_llm_collect 经 stream_llm 透传，无需另覆盖。
    不改 ThinkingInjectRail / config；注入失败由 Executor 跳过或裸重试。
    """

    async def call_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str | None = None,
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> str:
        return await PlanNode.call_llm(
            self,
            prompt,
            system_prompt=system_prompt,
            node_name=node_name,
            concurrent=concurrent,
            thinking="off",
        )

    async def stream_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str | None = None,
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in PlanNode.stream_llm(
            self,
            prompt,
            system_prompt=system_prompt,
            node_name=node_name,
            concurrent=concurrent,
            thinking="off",
        ):
            yield chunk
