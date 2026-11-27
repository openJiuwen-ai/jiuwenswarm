# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillDevContext — 每个阶段的执行上下文.

Context 不是 Agent，它是每阶段 StageHandler 的运行环境：
- 持有 deps（外部依赖）和 state（运行时状态）的引用
- 提供 emit() 向前端推送事件
- 提供 create_stage_agent() 为当前阶段创建隔离的 ReActAgent
- 提供 run_stage_agent() 经 Runner.run_agent 一次性执行并返回文本（invoke 路径）

每阶段独立 Agent 的核心价值：
    - 工具隔离：PLAN 只有搜索，GENERATE 才有文件写入
    - Prompt 隔离：每阶段有焦点明确的专属 system prompt
    - 内存隔离：阶段结束 Agent 即释放，无残留上下文
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, AsyncIterator, Dict, Type

from openjiuwen.core.foundation.llm import Model, ModelRequestConfig, ModelClientConfig
from openjiuwen.core.single_agent import AgentCard

from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.workspace.workspace import Workspace
from openjiuwen.harness.tools.code import CodeTool
from openjiuwen.harness.tools.filesystem import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
)
from openjiuwen.harness.tools.web_tools import WebPaidSearchTool

from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenclaw.agentserver.deep_agent.rails import JiuClawStreamEventRail
from jiuwenclaw.agentserver.skilldev.guarded_bash_tool import GuardedBashTool
from jiuwenclaw.agentserver.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
    JiuwenHarnessFreeSearchTool,
)
from jiuwenclaw.agentserver.skilldev.deps import SkillDevDeps
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEvent, SkillDevEventType, SkillDevState

logger = logging.getLogger(__name__)

# SkillDev 流式 AGENT_THINKING 批量字符阈值（与 interface_deep 默认 stream_character_threshold 2000 对齐；不读全局配置）
_STREAM_CHAR_THRESHOLD = 2000

# Harness tool mapping: tool_name -> Tool Class
HARNESS_TOOL_CLASSES: Dict[str, Type] = {
    "file_read": ReadFileTool,
    "file_write": WriteFileTool,
    "file_edit": EditFileTool,
    "file_glob": GlobTool,
    "file_grep": GrepTool,
    "file_listdir": ListDirTool,
    "shell": GuardedBashTool,
    "code_execute": CodeTool,
    "web_search_free": JiuwenHarnessFreeSearchTool,
    "web_search_paid": WebPaidSearchTool,
    "web_fetch": JiuwenHarnessFetchWebpageTool,
}

# Stage-level tool isolation: stage_name -> allowed tool names
STAGE_TOOL_WHITELIST: Dict[str, list[str]] = {
    "INIT": ["file_read", "file_glob", "file_listdir"],
    # Clarification phase: read-only workspace awareness
    "CLARIFY": ["file_read", "file_glob", "file_listdir"],
    # Generation phase: file read/write, no shell
    "GENERATE": ["file_read", "file_write", "file_edit", "file_glob", "file_grep", 
        "file_listdir", "web_search_free", "web_fetch", "shell", "code_execute"],
    # Validation phase: read-only
    "VALIDATE": ["file_read", "file_glob", "file_grep", "shell"],
    # Test design phase: read skill files + search for best practices
    "TEST_DESIGN": ["file_read", "file_glob", "file_listdir", "web_search_free",
        "web_search_paid", "shell", "file_write"],
    # Test run phase: can execute code and shell commands
    "TEST_RUN": ["file_read", "file_write", "shell", "code_execute", "file_glob",
        "shell", "web_search_free", "web_search_paid"],
    # Evaluation phase: read-only
    "EVALUATE": ["file_read", "file_grep", "file_listdir", "file_glob", "shell"],
    # Improvement phase: file modifications allowed
    "IMPROVE": ["file_read", "file_write", "file_edit", "file_glob", "file_grep", "shell"],
    # Packaging phase: shell commands for building
    "PACKAGE": ["file_read", "file_glob", "shell"],
    # Description optimization: minimal tools
    "DESC_OPTIMIZE": ["file_read"],
}


class _ModelErrorCapture(AgentRail):
    """临时注入的 Rail，捕获框架内部静默吞掉的 LLM 调用异常.

    ReActAgent._inner_stream 的 stream_process() 会用 except Exception 捕获所有
    LLM 异常并仅打 log，导致 run_agent_streaming() 正常耗尽而不抛出。
    本 Rail 在异常被吞之前（ON_MODEL_EXCEPTION 在 except 之前触发）将其存入
    captured，供调用方在流结束后检查并重新 raise。
    """

    def __init__(self) -> None:
        self.captured: list[Exception] = []

    async def on_model_exception(self, ctx) -> None:
        if ctx.exception is not None:
            self.captured.append(ctx.exception)


def _chunk_to_frontend_delta(chunk_type: str, payload: dict) -> str:
    """将 streaming chunk 统一规整成可展示文本（含 tool_call / tool_result）."""
    if chunk_type in ("llm_output", "content_chunk"):
        content = payload.get("content")
        if content in (None, ""):
            content = payload.get("output", "")
        return str(content) if content is not None else ""

    if chunk_type == "tool_call":
        tool_call = payload.get("tool_call", {})
        if isinstance(tool_call, dict):
            tool_name = str(tool_call.get("name", "")).strip()
            tool_args = tool_call.get("arguments", "")
            if isinstance(tool_args, (dict, list)):
                tool_args = json.dumps(tool_args, ensure_ascii=False)
            return f"\n[tool_call] {tool_name}\nargs: {tool_args}\n"
        return f"\n[tool_call] {tool_call}\n"

    if chunk_type == "tool_result":
        tool_result = payload.get("tool_result", {})
        if isinstance(tool_result, dict):
            tool_name = str(tool_result.get("tool_name", "")).strip()
            result = tool_result.get("result", "")
            return f"\n[tool_result] {tool_name}\n{result}\n"
        return f"\n[tool_result] {tool_result}\n"

    # 兼容部分 runtime 以 interaction 承载工具事件
    if chunk_type == "interaction":
        event_type = payload.get("type")
        event_payload = payload.get("payload")
        if isinstance(event_payload, dict):
            if event_type == "tool_call" and "tool_call" in event_payload:
                return _chunk_to_frontend_delta("tool_call", event_payload)
            if event_type == "tool_result" and "tool_result" in event_payload:
                return _chunk_to_frontend_delta("tool_result", event_payload)
    return ""


def _normalize_tool_call(payload: dict) -> dict | None:
    """将 runtime 的 tool_call payload 规整为前端可消费结构."""
    tool_call = payload.get("tool_call", payload)
    if not isinstance(tool_call, dict):
        return None
    tool_id = str(
        tool_call.get("id")
        or tool_call.get("tool_call_id")
        or tool_call.get("call_id")
        or ""
    ).strip()
    name = str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
    args = tool_call.get("arguments", {})
    if not isinstance(args, dict):
        args = {"raw": args}
    if not tool_id and name:
        tool_id = f"{name}_{int(time.time() * 1000)}"
    if not tool_id or not name:
        return None
    return {
        "tool_call_id": tool_id,
        "tool_name": name,
        "arguments": args,
    }


def _normalize_tool_result(payload: dict) -> dict | None:
    """将 runtime 的 tool_result payload 规整为前端可消费结构."""
    result = payload.get("tool_result", payload)
    if not isinstance(result, dict):
        return None
    tool_call_id = str(
        result.get("tool_call_id")
        or result.get("toolCallId")
        or result.get("id")
        or ""
    ).strip()
    tool_name = str(result.get("tool_name") or result.get("toolName") or "").strip()
    success = bool(result.get("success", True))
    result_text = result.get("result", "")
    if isinstance(result_text, (dict, list)):
        result_text = json.dumps(result_text, ensure_ascii=False)
    result_text = str(result_text) if result_text is not None else ""
    if not tool_call_id and tool_name:
        tool_call_id = f"{tool_name}_{int(time.time() * 1000)}"
    if not tool_call_id:
        return None
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "result": result_text,
        "success": success,
    }


class SkillDevContext:
    """阶段执行上下文.

    由 Pipeline 在每阶段入口创建，传递给 StageHandler.execute()。
    """

    def __init__(
        self,
        task_id: str,
        deps: SkillDevDeps,
        state: SkillDevState,
        workspace: Path,
        event_queue: asyncio.Queue,
    ) -> None:
        self.task_id = task_id
        self.deps = deps
        self.state = state
        self.workspace = workspace
        self._event_queue = event_queue
        # agent_id -> registered tool_id set (only records successful add_tool entries)
        self._agent_tool_ids: dict[str, set[str]] = {}

    @staticmethod
    def _resolve_agent_id(agent_or_agent_id: object) -> str | None:
        """Resolve agent id from agent object or raw id string."""
        if isinstance(agent_or_agent_id, str):
            return agent_or_agent_id.strip() or None
        card = getattr(agent_or_agent_id, "card", None)
        if card is None:
            return None
        card_id = getattr(card, "id", None)
        if isinstance(card_id, str) and card_id.strip():
            return card_id.strip()
        return None

    def _track_agent_tool(self, agent_id: str, tool_id: str) -> None:
        if not agent_id or not tool_id:
            return
        if agent_id not in self._agent_tool_ids:
            self._agent_tool_ids[agent_id] = set()
        self._agent_tool_ids[agent_id].add(tool_id)

    def release_agent_tools(self, agent_or_agent_id: object) -> None:
        """Release registered tools for an agent from Runner.resource_mgr.

        This is an explicit lifecycle API. Callers can invoke it at any time
        they decide the stage agent should be cleaned up.
        """
        from openjiuwen.core.runner import Runner

        agent_id = self._resolve_agent_id(agent_or_agent_id)
        if not agent_id:
            logger.warning(
                "[session=%s] [SkillDevContext] release_agent_tools: cannot resolve agent_id from input=%s",
                self.task_id,
                type(agent_or_agent_id).__name__,
            )
            return

        tool_ids = sorted(self._agent_tool_ids.get(agent_id, set()))
        if not tool_ids:
            logger.debug(
                "[session=%s] [SkillDevContext] release_agent_tools: no tracked tools for agent_id=%s",
                self.task_id,
                agent_id,
            )
            return

        removed = 0
        failed = 0
        try:
            remove_results = Runner.resource_mgr.remove_tool(tool_id=tool_ids)
            result_list = remove_results if isinstance(remove_results, list) else [remove_results]
            for result in result_list:
                is_ok = bool(getattr(result, "is_ok", lambda: False)())
                if is_ok:
                    removed += 1
                else:
                    failed += 1
            logger.info(
                "[session=%s] [SkillDevContext] release_agent_tools: agent_id=%s attempted=%d removed=%d failed=%d",
                self.task_id,
                agent_id,
                len(tool_ids),
                removed,
                failed,
            )
        except Exception as exc:
            logger.warning(
                "[session=%s] [SkillDevContext] release_agent_tools failed: agent_id=%s tools=%s err=%s",
                self.task_id,
                agent_id,
                tool_ids,
                exc,
            )
        finally:
            # Always clear local tracking to keep release idempotent.
            self._agent_tool_ids.pop(agent_id, None)

    async def emit(self, event_type: SkillDevEventType, payload: dict) -> None:
        """向前端推送一个事件（放入 Pipeline 的事件队列）."""
        event = SkillDevEvent(
            event_type=event_type,
            payload={"task_id": self.task_id, **payload},
            task_id=self.task_id,
        )
        await self._event_queue.put(event)

    async def run_stage_agent_streaming(
        self,
        agent: object,
        *,
        stage_name: str,
        query: str,
        emit_thinking: bool = True,
        emit_output: bool = True,
        capture_thinking: bool = False,
        capture_trace: bool = False,
    ) -> "str | tuple[str, str] | tuple[str, str, str]":
        """流式执行 Agent，将推理与正文分别经缓冲后推送到不同事件通道.

        - llm_reasoning  → AGENT_THINKING（受 emit_thinking 控制）
        - llm_output / content_chunk → AGENT_OUTPUT（受 emit_output 控制）
        - tool_call / tool_result → 结构化事件
        - answer / final_output → 未流式推送过正文时补推 AGENT_OUTPUT

        正文返回值只含 llm_output/answer 内容，不含 reasoning，
        保证 clarify/plan/evaluate 等阶段的 JSON 解析不受 reasoning 污染。

        Args:
            emit_thinking:    是否将推理 chunk 推送为 AGENT_THINKING 事件（默认 True）。
            emit_output:      是否将正文 chunk 推送为 AGENT_OUTPUT 事件（默认 True）。
                              并发 / 缓冲场景下可设为 False，由调用方在合适时机
                              手动 emit 完整文本，避免多任务输出按 chunk 交织。
            capture_thinking: 若为 True，同时收集推理文本并随返回值返回。
            capture_trace:    若为 True，同时收集结构化执行轨迹（工具调用 / 工具结果）。

        Returns:
            按以下规则确定返回形态：
            - 默认：``output: str``
            - capture_thinking=True：``(output, thinking)``
            - capture_trace=True：``(output, trace)``
            - 两者都为 True：``(output, thinking, trace)``
        """
        from openjiuwen.core.runner import Runner

        # 注入错误捕获 Rail：防止框架静默报错并返回空字段，影响下游执行
        # 将异常存入 captured，供流结束后检查并重新 raise，还原正确的错误信息。
        error_capture = _ModelErrorCapture()
        agent.add_rail(error_capture)

        inputs = {
            "conversation_id": f"skilldev_{self.task_id}_{stage_name}",
            "query": query,
        }
        # output_parts 只收集正文（llm_output / content_chunk / answer），不含推理
        output_parts: list[str] = []
        # thinking_parts 收集完整推理文本，仅在 capture_thinking=True 时被消费
        thinking_parts: list[str] = []
        accumulated_text = ""      # 正文缓冲区
        accumulated_reasoning = "" # 推理缓冲区
        final_output: str | None = None
        # 是否已通过 llm_output/content_chunk 流式推送过正文，用于判断 answer 是否需要补推
        has_streamed_output = False
        trace_parts: list[str] = []

        async def push_reasoning_delta(delta: str) -> None:
            """将推理 delta 推送为 AGENT_THINKING 事件并累计到 thinking_parts."""
            if not delta:
                return
            thinking_parts.append(delta)
            if emit_thinking:
                await self.emit(
                    SkillDevEventType.AGENT_THINKING,
                    {"delta": delta, "stage": stage_name},
                )

        async def push_output_delta(delta: str) -> None:
            """将正文 delta 推送为 AGENT_OUTPUT 事件，并累计到 output_parts.

            是否实时推送由 emit_output 控制；并发 / 缓冲场景下调用方可关闭推送、
            自行在合适时机一次性 emit 完整文本，避免多任务输出按 chunk 交织。
            """
            if not delta:
                return
            output_parts.append(delta)
            if emit_output:
                await self.emit(
                    SkillDevEventType.AGENT_OUTPUT,
                    {"delta": delta, "stage": stage_name},
                )

        async def flush_text() -> None:
            nonlocal accumulated_text
            if accumulated_text:
                await push_output_delta(accumulated_text)
                accumulated_text = ""

        async def flush_reasoning() -> None:
            nonlocal accumulated_reasoning
            if accumulated_reasoning:
                await push_reasoning_delta(accumulated_reasoning)
                accumulated_reasoning = ""

        try:
            async for chunk in Runner.run_agent_streaming(agent, inputs=inputs):
                if not hasattr(chunk, "type") or not hasattr(chunk, "payload"):
                    logger.info(
                        "[session=%s] [SkillDevContext] stream chunk skipped: "
                        "missing type/payload (stage=%s, chunk=%s)",
                        self.task_id,
                        stage_name,
                        type(chunk).__name__,
                    )
                    continue

                chunk_type = str(chunk.type)
                raw = chunk.payload
                payload = raw if isinstance(raw, dict) else {}

                if chunk_type == "llm_reasoning":
                    # 先冲刷正文缓冲，保持时序边界
                    await flush_text()
                    if isinstance(raw, dict):
                        piece = raw.get("content", "") or raw.get("output", "")
                    else:
                        piece = str(raw) if raw is not None else ""
                    piece = str(piece) if piece else ""
                    if piece:
                        accumulated_reasoning += piece
                        if len(accumulated_reasoning) >= _STREAM_CHAR_THRESHOLD:
                            await flush_reasoning()
                        trace_parts.append(piece)
                    continue

                if chunk_type in ("llm_output", "content_chunk"):
                    # 先冲刷推理缓冲，保持时序边界
                    await flush_reasoning()
                    if isinstance(raw, dict):
                        c = raw.get("content")
                        if c in (None, ""):
                            c = raw.get("output", "")
                        text_piece = str(c) if c is not None else ""
                    else:
                        text_piece = str(raw) if raw is not None else ""
                    if text_piece:
                        has_streamed_output = True
                        accumulated_text += text_piece
                        if len(accumulated_text) >= _STREAM_CHAR_THRESHOLD:
                            await flush_text()
                        trace_parts.append(text_piece)
                    continue

                if chunk_type in ("tool_call", "tool_result"):
                    await flush_text()
                    await flush_reasoning()
                    delta_text = _chunk_to_frontend_delta(chunk_type, payload)
                    if delta_text:
                        trace_parts.append(delta_text)
                    if chunk_type == "tool_call":
                        event_payload = _normalize_tool_call(payload)
                        if event_payload:
                            await self.emit(
                                SkillDevEventType.TOOL_CALL,
                                {"stage": stage_name, **event_payload},
                            )
                        elif delta_text:
                            await push_output_delta(delta_text)
                    else:
                        event_payload = _normalize_tool_result(payload)
                        if event_payload:
                            await self.emit(
                                SkillDevEventType.TOOL_RESULT,
                                {"stage": stage_name, **event_payload},
                            )
                        elif delta_text:
                            await push_output_delta(delta_text)
                    continue

                if chunk_type == "interaction":
                    await flush_text()
                    await flush_reasoning()
                    event_type = payload.get("type")
                    event_payload = payload.get("payload")
                    if isinstance(event_payload, dict):
                        if event_type == "tool_call":
                            normalized = _normalize_tool_call(event_payload)
                            if normalized:
                                tc_delta = _chunk_to_frontend_delta("tool_call", event_payload)
                                if tc_delta:
                                    trace_parts.append(tc_delta)
                                await self.emit(
                                    SkillDevEventType.TOOL_CALL,
                                    {"stage": stage_name, **normalized},
                                )
                                continue
                        if event_type == "tool_result":
                            normalized = _normalize_tool_result(event_payload)
                            if normalized:
                                tr_delta = _chunk_to_frontend_delta("tool_result", event_payload)
                                if tr_delta:
                                    trace_parts.append(tr_delta)
                                await self.emit(
                                    SkillDevEventType.TOOL_RESULT,
                                    {"stage": stage_name, **normalized},
                                )
                                continue
                    delta_text = _chunk_to_frontend_delta(chunk_type, payload)
                    if delta_text:
                        trace_parts.append(delta_text)
                        await push_output_delta(delta_text)
                    continue

                if chunk_type in ("answer", "final_output"):
                    await flush_text()
                    await flush_reasoning()
                    out = payload.get("output") or payload.get("content")
                    if isinstance(out, dict):
                        content = out.get("output", str(out))
                    elif out is not None:
                        content = str(out)
                    else:
                        content = ""
                    if content:
                        final_output = content
                        # 若正文从未流式推送，补推一次（非流式 fallback）；
                        # 流式模式下 llm_output 已逐片写入 trace_parts，不重复追加
                        if not has_streamed_output:
                            trace_parts.append(content)
                            await push_output_delta(content)
                    continue

                if chunk_type in (
                    "todo.updated",
                    "context.compressed",
                    "thinking",
                    "controller_output",
                    "error",
                ):
                    continue

                delta_text = _chunk_to_frontend_delta(chunk_type, payload)
                if delta_text:
                    await flush_text()
                    await flush_reasoning()
                    trace_parts.append(delta_text)
                    await push_output_delta(delta_text)
                else:
                    logger.info(
                        "[session=%s] [SkillDevContext] stream chunk not emitted: stage=%s type=%s",
                        self.task_id,
                        stage_name,
                        chunk_type,
                    )

            await flush_text()
            await flush_reasoning()

        # runner正常上抛异常的情况：捕获并上抛
        except Exception as e:
            await flush_text()
            await flush_reasoning()
            logger.error(
                "[session=%s] [SkillDevContext] Agent execution failed: stage=%s error=%s",
                self.task_id,
                stage_name,
                e,
            )
            raise e

        # 返回值只包含正文，不含推理（避免污染后续 JSON 解析等处理）
        output = final_output if final_output is not None else "".join(output_parts)

        # runner静默吞掉异常的情况：通过自定义的rail获取原始报错并上抛
        # 仅在输出为空时才上抛，有正常输出说明 agent 已从瞬时错误中恢复
        if error_capture.captured:
            if not output.strip():
                e = error_capture.captured[-1]
                logger.error(
                    "[session=%s] [SkillDevContext] LLM call failed silently, output is empty: "
                    "stage=%s error=%s",
                    self.task_id,
                    stage_name,
                    e,
                )
                raise e
            else:
                logger.warning(
                    "[session=%s] [SkillDevContext] LLM had transient errors but agent recovered: "
                    "stage=%s errors=%d last_error=%s",
                    self.task_id,
                    stage_name,
                    len(error_capture.captured),
                    error_capture.captured[-1],
                )
        if capture_thinking and capture_trace:
            return output, "".join(thinking_parts), "".join(trace_parts)
        if capture_thinking:
            return output, "".join(thinking_parts)
        if capture_trace:
            return output, "".join(trace_parts)
        return output

    def create_stage_agent(
        self,
        stage_name: str,
        system_prompt: str,
        tools: list[str] | None = None,
        max_iterations: int = 20,
        whitelist_key: str | None = None,
    ):
        """为当前阶段创建隔离的 DeepAgent（带 StreamEventRail）.

        Args:
            stage_name:     阶段标识，用于 agent 命名（调试/日志用）
            system_prompt:  该阶段专属的 system prompt
            tools:          工具名白名单，如 ["file_read", "file_write", "web_search"]
            max_iterations: ReAct 最大循环次数

        Returns:
            配置完毕的 DeepAgent 实例（尚未执行）
        """
        logger.info(
            "[session=%s] [SkillDevContext] create_stage_agent: stage=%s tools=%s max_iterations=%d",
            self.task_id,
            stage_name,
            tools,
            max_iterations,
        )

        agent_card = AgentCard(
            name=f"skilldev_{self.task_id}_{stage_name}",
            description=f"SkillDev agent for stage {stage_name}",
        )
        model_client_config = ModelClientConfig(**self.deps.model_client_config)
        model_request_kwargs = dict(self.deps.model_config_obj or {})
        model_request_kwargs.setdefault("model", self.deps.model_name)

        # # Structured-analysis stages benefit from JSON-constrained outputs.
        # # Keep GENERATE / IMPROVE / TEST_RUN free-form to avoid over-constraining text/code generation.
        # structured_output_stages = {"CLARIFY", "TEST_DESIGN"}
        # if stage_name.upper() in structured_output_stages:
        #     model_request_kwargs.setdefault("response_format", {"type": "json_object"})

        model_request_config = ModelRequestConfig(**model_request_kwargs)
        model = Model(
            model_client_config=model_client_config,
            model_config=model_request_config,
        )
        stream_event_rail = JiuClawStreamEventRail()
        agent = create_deep_agent(
            model=model,
            card=agent_card,
            system_prompt=system_prompt,
            tools=[],
            rails=[stream_event_rail],
            max_iterations=max_iterations,
            workspace=Workspace(
                root_path=str(self.workspace),
                language="cn",
            ),
            auto_create_workspace=False,
        )

        if tools:
            self._register_tools(agent, tools, stage_name, whitelist_key=whitelist_key)

        return agent

    def _register_tools(
        self,
        agent,
        tool_names: list[str],
        stage_name: str,
        whitelist_key: str | None = None,
    ) -> None:
        """根据工具名白名单将工具注册到 Agent.

        Args:
            agent:          ReActAgent 实例
            tool_names:     工具名白名单
            stage_name:     阶段名称（用于构造唯一 tool_id）
        """
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.sys_operation import SysOperation, SysOperationCard, OperationMode, LocalWorkConfig

        # Get stage whitelist for tool isolation
        # whitelist_key 与 stage_name 分离：优先使用whitelist_key作为白名单查找
        # 未提供则直接使用stage_name
        if whitelist_key:
            lookup_upper = whitelist_key.upper()
        else:
            lookup_upper = stage_name.upper()
        allowed_tools = STAGE_TOOL_WHITELIST.get(lookup_upper, [])
        agent_id = self._resolve_agent_id(agent)

        if not allowed_tools:
            logger.warning(
                "[session=%s] [SkillDevContext] No tool whitelist defined for stage '%s' "
                "(lookup_key='%s'), skipping tool registration for safety",
                self.task_id,
                stage_name,
                lookup_upper,
            )
            return

        # 延迟初始化 SysOperation（只在需要文件系统工具时创建）
        sys_operation: SysOperation | None = None

        def _get_sys_operation() -> SysOperation:
            nonlocal sys_operation
            if sys_operation is None:
                # 优先使用注入的 sysop_config，否则自己创建
                if self.deps.sysop_config is not None:
                    sysop_card = self.deps.sysop_config
                else:
                    # 自己创建 SysOperationCard，根据阶段设置 shell 白名单
                    shell_allowlist = []
                    if lookup_upper in ("TEST_RUN", "PACKAGE"):
                        shell_allowlist = ["git", "python", "python3", "pip", "pytest"]
                    sysop_card = SysOperationCard(
                        id=f"skilldev_{self.task_id}_{stage_name}",
                        mode=OperationMode.LOCAL,
                        work_config=LocalWorkConfig(
                            work_dir=str(self.workspace),
                            shell_allowlist=shell_allowlist
                        )
                    )
                sys_operation = SysOperation(sysop_card)
            return sys_operation

        seen_tool_names: set[str] = set()
        for tool_name in tool_names:
            if tool_name in seen_tool_names:
                logger.debug(
                    "[session=%s] [SkillDevContext] Skip duplicated tool name '%s' in stage=%s",
                    self.task_id,
                    tool_name,
                    stage_name,
                )
                continue
            seen_tool_names.add(tool_name)
            # Stage-level tool isolation: check if tool is allowed for this stage
            if tool_name not in allowed_tools:
                logger.warning(
                    "[session=%s] [SkillDevContext] Tool '%s' is not allowed in stage '%s' "
                    "(allowed: %s), skipping",
                    self.task_id,
                    tool_name,
                    stage_name,
                    allowed_tools,
                )
                continue
            # 处理 harness 工具
            if tool_name in HARNESS_TOOL_CLASSES:
                try:
                    tool_cls = HARNESS_TOOL_CLASSES[tool_name]

                    # 实例化工具
                    if tool_name in ("web_search_free", "web_search_paid", "web_fetch"):
                        # Web 工具不需要 SysOperation
                        harness_tool = tool_cls(language="cn")
                    else:
                        # 文件系统工具需要 SysOperation
                        sys_op = _get_sys_operation()
                        harness_tool = tool_cls(sys_op, language="cn")

                    # 生成唯一 tool_id
                    tool_id = f"{tool_name}_{self.task_id}_{stage_name}"
                    harness_tool.card.id = tool_id

                    # 注册到 Runner.resource_mgr
                    add_result = Runner.resource_mgr.add_tool(harness_tool)
                    if not getattr(add_result, "is_ok", lambda: False)():
                        logger.warning(
                            "[session=%s] [SkillDevContext] Failed to add tool into "
                            "resource_mgr: %s (id=%s), skip ability add",
                            self.task_id,
                            tool_name,
                            tool_id,
                        )
                        continue

                    # 添加到 agent 的 ability_manager
                    agent.ability_manager.add(harness_tool.card)
                    if agent_id:
                        self._track_agent_tool(agent_id, tool_id)

                    logger.debug(
                        "[session=%s] [SkillDevContext] Registered harness tool: %s (id=%s)",
                        self.task_id,
                        tool_name,
                        tool_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "[session=%s] [SkillDevContext] Failed to register harness tool %s: %s",
                        self.task_id,
                        tool_name,
                        exc,
                    )
            else:
                logger.warning(
                    "[session=%s] [SkillDevContext] Unknown tool: %s (not in harness or MCP)",
                    self.task_id,
                    tool_name,
                )
