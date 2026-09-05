# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboExecutor —— 规划代码校验、加载与异步执行。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib
import json
import logging
import os
import secrets
import sys
import time
import uuid
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from openjiuwen.core.single_agent import create_agent_session
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.agent_adapter.llm_io_trace import (
    begin_tool_trace_event,
    end_tool_trace_event,
    log_tool_call_input,
    log_tool_call_output,
)
from jiuwenswarm.server.runtime.skill_turbo.rails import (
    SkillTurboArtifactRail,
    SkillTurboAskUserRail,
)
from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import (
    clear_node_artifacts,
    save_node_artifacts,
)
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_RESUME_CTX_KEY,
    build_tool_ctx,
    clear_resume_ctx,
    extract_tool_interrupt,
    save_resume_ctx,
    set_skill_turbo_id,
)
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk
from jiuwenswarm.server.runtime.skill_turbo.json_utils import extract_llm_json
from jiuwenswarm.server.runtime.skill_turbo.markdown_stream import (
    markdown_stream_incoming,
    terminate_dangling_markdown_fence,
)
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import FallbackContractError
from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
    resolve_interactive_ask_from_inputs,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.validator import (
    PlanCodeValidationError,
    PlanCodeValidator,
)

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment
    from jiuwenswarm.server.runtime.skill_turbo.evolver import SkillTurboEvolver
    from jiuwenswarm.common.schema.agent import AgentResponseChunk

# ──────────────────────── NOT FOUND 依赖的内联兜底 ────────────────────────
# STREAM_SOURCE_ID_FIELD: 目标仓库 stream_utils 未导出此常量，在此内联定义
STREAM_SOURCE_ID_FIELD = "stream_source_id"

# _retry_session: jiuwen_core_patch 在目标仓库不存在，使用内联 ContextVar 替代
_retry_session: ContextVar[Any] = ContextVar("retry_session", default=None)

try:
    from jiuwenswarm.interface_resp import track_tool_resp
except ImportError:
    @contextlib.asynccontextmanager
    async def track_tool_resp(tool_name: str, *, session_id: str | None = None):
        yield

try:
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        set_send_file_request_context,
        reset_send_file_request_context,
        set_effective_request_workspace_dir,
        get_effective_request_workspace_dir,
        get_effective_request_output_dir,
        set_interactive_ask,
        reset_interactive_ask,
    )
except ImportError:
    # Fallback no-ops for environments without subagent_executor
    def set_send_file_request_context(*args, **kwargs):
        pass

    def reset_send_file_request_context(*args, **kwargs):
        pass

    def set_effective_request_workspace_dir(*args, **kwargs):
        pass

    def get_effective_request_workspace_dir(*args, **kwargs):
        return None

    def get_effective_request_output_dir(*args, **kwargs):
        return None

    def set_interactive_ask(*args, **kwargs):
        pass

    def reset_interactive_ask(*args, **kwargs):
        pass

logger = logging.getLogger(__name__)

# LLM 最大输出 token 数；可通过 LLM_MAX_TOKENS 环境变量覆盖，默认 65536
_DEFAULT_LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "65536"))


def resolve_skill_turbo_thinking_kwargs(
    thinking: str | None,
    model_client: Any,
) -> dict[str, Any]:
    """Optional thinking inject for SkillTurbo LLM calls.

    ``thinking is None`` → empty dict (no adapt, identical to legacy invoke/stream).
    Unsupported / degraded → empty dict (never abort).
    """
    if thinking is None:
        return {}
    from jiuwenswarm.common.thinking.adapter import adapt_thinking
    from jiuwenswarm.common.thinking.types import kwargs_digest, thaw_llm_call_kwargs

    profile = adapt_thinking(thinking, model_client)
    if not profile.injected or not profile.llm_call_kwargs:
        if profile.degraded:
            logger.info(
                "[SkillTurboExecutor] thinking not injected thinking=%s model=%r reason=%s",
                profile.thinking,
                profile.model_name,
                profile.reason,
            )
        return {}
    kwargs = thaw_llm_call_kwargs(profile.llm_call_kwargs)
    logger.info(
        "[SkillTurboExecutor] thinking inject thinking=%s model=%r digest=%s",
        profile.thinking,
        profile.model_name,
        kwargs_digest(kwargs),
    )
    return kwargs


def is_skill_turbo_thinking_param_error(exc: BaseException) -> bool:
    """Heuristic: API/client rejected thinking-related call kwargs."""
    if isinstance(exc, TypeError):
        return True
    msg = str(exc).lower()
    needles = (
        "extra_body",
        "thinking",
        "enable_thinking",
        "reasoning_effort",
        "unexpected keyword",
        "invalid_request",
        "bad request",
        "validation error",
        "unrecognized",
    )
    return any(n in msg for n in needles)


# ──────────────────────── 全局上下文变量 ────────────────────────
# Session管理（用于发送事件）
_session_var: ContextVar[Session | None] = ContextVar("skill_turbo_session", default=None)

# 流式输出缓冲层：对 chat.delta / chat.reasoning 累加后再 flush，
# 减少高频小 chunk 对前端的消息压力。与 subagent_executor 的
# _SUBAGENT_STREAM_FLUSH_INTERVAL_SECONDS 语义一致。
_SKILL_TURBO_STREAM_FLUSH_INTERVAL_SECONDS: float = 3.0

# 需要缓冲的事件类型
_BUFFERABLE_EVENT_TYPES: frozenset[str] = frozenset({"chat.delta", "chat.reasoning"})

# Request上下文
_request_id_var: ContextVar[str] = ContextVar("skill_turbo_request_id", default="")
_channel_id_var: ContextVar[str] = ContextVar("skill_turbo_channel_id", default="")

# Task执行上下文（用于二层节点追踪）
_current_task_context_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "skill_turbo_current_task_context", default=None
)
_current_task_holder_var: ContextVar[dict[str, str | None] | None] = ContextVar(
    "skill_turbo_current_task_holder", default=None
)

# Task事件队列（用于收集 task.start/task.complete/task.update 事件）
_task_events_queue_var: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "skill_turbo_task_events_queue", default=None
)

# Task状态字典（用于维护所有任务的状态）
_task_states_var: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "skill_turbo_task_states", default=None
)


# ──────────────────────── 简单的ToolCall类 ────────────────────────
@dataclass
class ToolCall:
    """简单的ToolCall对象，用于Rail回调。"""
    name: str
    arguments: dict[str, Any]
    id: str


# ──────────────────────── 安全的内置函数白名单 ────────────────────────
# 移除了 type, getattr, setattr, globals, locals, vars, dir 等可能被滥用的函数
# 保留 isinstance（安全且常用），以及常用异常类供 plan_code 使用
_SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "sorted": sorted,
    "any": any,
    "all": all,
    "round": round,
    "reversed": reversed,
    "isinstance": isinstance,
    "map": map,
    "filter": filter,
    "repr": repr,
    "chr": chr,
    "ord": ord,
    "hex": hex,
    "oct": oct,
    "bin": bin,
    "divmod": divmod,
    "iter": iter,
    "next": next,
    "slice": slice,
    "True": True,
    "False": False,
    "None": None,
    # 常用异常类（plan_code 需要 raise 异常时使用）
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "RuntimeError": RuntimeError,
    "NotImplementedError": NotImplementedError,
    "StopIteration": StopIteration,
}


# ──────────────────────── 异常类型 ────────────────────────
class PlanCodeLoadError(Exception):
    """规划代码加载或根节点提取失败。"""


class ExecutionTimeoutError(Exception):
    """执行超时异常。"""


class FallbackLimitExceededError(Exception):
    """Fallback 次数超过限制。"""


# ──────────────────────── 执行配置 ────────────────────────
@dataclass
class ExecutorConfig:
    """Executor 配置项。"""

    execution_timeout: float = 300.0  # 执行超时时间（秒）
    max_fallback_count: int = 3  # 最大 fallback 次数
    enable_fallback: bool = True  # 是否启用 fallback 机制
    enable_trace: bool = True  # 是否启用执行追踪


# LLM 并发槽位排队等待超过该阈值（毫秒）时，升级日志级别到 INFO，
# 便于在生产环境观测排队拥堵；低于阈值的快速排队仅打 DEBUG。
_LLM_QUEUE_WAIT_LOG_THRESHOLD_MS: float = 1000.0


# ──────────────────────── 执行追踪 ────────────────────────
@dataclass
class ExecutionTrace:
    """单次执行追踪记录。"""

    plan_code_hash: str = ""
    input_keys: list[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    success: bool = False
    error: str | None = None
    fallback_count: int = 0
    node_execution_order: list[str] = field(default_factory=list)


@dataclass
class TaskCompleteEventData:
    """task.complete 事件构建所需的具名参数集合（G.FNM.03）。"""

    subplan: PlanNode
    task_id: str
    status: str
    timestamp: float
    duration_ms: int
    error: Any | None


def _is_subplan_business_failure(result: Any) -> bool:
    """子节点返回业务失败状态时，也应标记 task.complete 为 failed。"""
    if not isinstance(result, dict):
        return False
    if result.get("export_status") == "failed":
        return True
    if result.get("status") == "error":
        return True
    return False


@dataclass
class _StreamBufferBucket:
    """单个 (stream_source_id, event_type) 桶的缓冲状态。

    并发节点会携带不同的 stream_source_id，每个 source_id 的 delta 和
    reasoning 各占一个桶，互不干扰。串行节点 source_id 为 None，所有
    delta 共享一个桶、reasoning 共享一个桶。
    """

    parts: list[str] = field(default_factory=list)
    since: float = 0.0
    first_chunk_sent: bool = False
    plan_name: str | None = None


@dataclass
class _StreamBufferState:
    """流式缓冲层状态，按 (source_id, event_type) 分桶管理。

    bucket_key = (source_id, event_type)
    - source_id: 并发节点隔离维度，串行节点为 None
    - event_type: "chat.delta" 或 "chat.reasoning"
    """

    buckets: dict[tuple[str | None, str], _StreamBufferBucket] = field(default_factory=dict)
    # Survives bucket pop/flush so the next chat.delta can be separated from a
    # fence that already went out (frontend concatenates adjacent streamText).
    last_emitted: dict[tuple[str | None, str], str] = field(default_factory=dict)

    def get_bucket(
        self, source_id: str | None, event_type: str
    ) -> _StreamBufferBucket:
        key = (source_id, event_type)
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = _StreamBufferBucket()
            self.buckets[key] = bucket
        return bucket

    def all_buckets(self) -> list[tuple[tuple[str | None, str], _StreamBufferBucket]]:
        return list(self.buckets.items())

    def clear(self) -> None:
        self.buckets.clear()


_SKILL_TURBO_TC_PREFIX = "skill_turbo-tc-"


def _parse_tool_name_from_call_id(tool_call_id: str) -> str | None:
    """从 ``skill_turbo-tc-{tool_name}-{args_hash}-{idx}`` 解析 tool_name。

    tool_name 自身可能含 ``-``（如 ``ask_user``），所以从尾部剥掉 ``-{idx}``
    和 ``-{args_hash}``（各 8/16 进制 hex 段），剩余即 tool_name。

    解析失败返回 ``None``（如非本格式或过短）。
    """
    if not isinstance(tool_call_id, str) or not tool_call_id.startswith(
        _SKILL_TURBO_TC_PREFIX
    ):
        return None
    body = tool_call_id[len(_SKILL_TURBO_TC_PREFIX):]
    if not body:
        return None
    last_dash = body.rfind("-")
    if last_dash <= 0:
        return None
    body = body[:last_dash]  # 去掉末段 idx
    last_dash = body.rfind("-")
    if last_dash <= 0:
        return None
    name = body[:last_dash]  # 去掉 args_hash
    return name or None


def _parse_call_idx_from_call_id(tool_call_id: Any) -> int | None:
    """从 ``skill_turbo-tc-{tool_name}-{args_hash}-{idx}`` 解析末段 idx。

    idx 是同一 ``(tool_name, args_hash)`` 组合的第几次调用（0-based）。
    解析失败返回 ``None``。
    """
    if not isinstance(tool_call_id, str):
        return None
    last_dash = tool_call_id.rfind("-")
    if last_dash <= 0 or last_dash == len(tool_call_id) - 1:
        return None
    try:
        return int(tool_call_id[last_dash + 1:])
    except ValueError:
        return None


class SkillTurboExecutor:
    """规划代码运行时引擎。"""

    def __init__(
        self,
        environment: SkillTurboEnvironment,
        trace_collector: SkillTurboEvolver | None = None,
        config: ExecutorConfig | None = None,
    ):
        self._env = environment
        self._validator = PlanCodeValidator(
            allowed_import_prefixes=environment.skill_code_import_prefixes
        )
        self._trace_collector = trace_collector
        self._config = config or ExecutorConfig()

        # ──────────────────────── Rail机制初始化 ────────────────────────
        # 引入核心Rail：StreamEventRail（用于发送事件）
        self._stream_event_rail = JiuSwarmStreamEventRail()
        self._artifact_rail = SkillTurboArtifactRail(self)

        # 复用 DeepAgent 已有的 PermissionInterruptRail（基于权限引擎做 ALLOW/DENY/ASK）。
        # ASK 决策会抛出 AbortError(cause=ToolInterruptException(request, tool_call))，
        # 由 _run_rail_hook → use_tool 透传出去，最终被 adapter 转成 HITL 三件套 chunk。
        permission_rail = self._build_permission_rail()
        # 保存引用：replay-skip 路径需跳过权限 rail，但保留事件发射类 rail
        self._permission_rail = permission_rail

        # 结构化 ask_user rail：拦截 skill_code 的 call_tool("ask_user", questions=...)，
        # 首次调用抛 AbortError 触发 HITL（前端弹 ask_user_question 卡片），
        # resume 时把用户作答按前端 answers 结构回填给 skill_code。
        self._ask_user_rail = self._build_ask_user_rail()

        # Rail列表（按优先级排序）
        self._rails = [self._stream_event_rail]
        if self._ask_user_rail is not None:
            self._rails.append(self._ask_user_rail)
        if permission_rail is not None:
            self._rails.append(permission_rail)
        self._rails.append(self._artifact_rail)

        # 按优先级排序（priority越小越先执行）
        self._rails.sort(key=lambda r: getattr(r, 'priority', 0))

        logger.debug(
            "[SkillTurboExecutor] Rails initialized: %s",
            [type(r).__name__ for r in self._rails]
        )

        # 执行状态追踪（每个请求独立）
        # 并发安全说明：SkillTurbo 每次请求创建新实例，SkillTurboExecutor 作为其实例属性也是请求隔离的，
        # 因此实例属性不会跨请求共享，不存在并发问题。
        # _current_trace_var: ContextVar[ExecutionTrace | None] = ContextVar("skill_turbo_current_trace", default=None)
        # _fallback_count_var: ContextVar[int] = ContextVar("skill_turbo_fallback_count", default=0)
        self._current_trace: ExecutionTrace | None = None
        self._fallback_count = 0
        self._execution_inputs: dict[str, Any] = {}
        self._current_plan_code: str = ""
        # use_tool 计数器：(tool_name, canonical_args) → call_index，用于生成
        # 重放确定性 tool_call_id（Step 8 中真正落实算法，这里先准备容器）。
        self._tool_call_counter: dict[str, int] = {}
        # resume 模式下，adapter 在 run_stream 入口注入；executor 在 use_tool
        # 命中目标 tool_call_id 时一次性消费。
        self._pending_resume: dict[str, Any] | None = None
        # HITL resume 重放：对 task_states 已 completed 的二层 stage 跳过真实执行
        self._resume_replay: bool = False

        # 当前任务状态（用于跨协程共享当前 task_id，解决 asyncio.create_task 复制 ContextVar 的问题）
        self._current_task_id_holder: dict[str, str | None] = {"task_id": None}
        self._task_states_holder: dict[str, dict[str, Any]] = {}

        # 节点产物记录 holder：plan_name → 产物 dict。
        # 运行期在 _after_subplan_execute 中累积，仅在中断前/流末 finally 落盘，
        # 避免每节点 post_run 的 IO 开销与事件流冲突。
        self._node_artifacts_holder: dict[str, dict[str, Any]] = {}

        # ──────────────────────── LLM 并发限制 ────────────────────────
        # 实例级 Semaphore，限制本 Executor 内 call_llm / stream_llm 的并发数。
        # 配置来源：environment.config["llm_concurrency_limit"]，<=0 表示不限制。
        # 注意：必须延迟到首次使用时再创建 Semaphore，否则会绑定到 __init__ 所在
        # 的事件循环（Executor 通常在 agent 初始化阶段构造，可能与 run 时事件循环不同）。
        self._llm_concurrency_limit: int = self._read_llm_concurrency_limit()
        self._llm_semaphore: asyncio.Semaphore | None = None

        # 节点显示名映射：由 skill 的 root 节点通过 display_names 类属性提供。
        # _prepare_root_node 时从 root 节点读取并填充此处。
        # 缺省为空 dict，_display_name 将原样返回 plan_name。
        self._display_names: dict[str, str] = {}

    def _display_name(self, plan_name: str) -> str:
        """将内部 plan_name 转为界面上展示的名称，未映射时原样返回。"""
        return self._display_names.get(plan_name, plan_name)

    def _replace_plan_names_in_text(self, text: str) -> str:
        """将文本中出现的已映射 plan_name 替换为对应的显示名。"""
        if not text or not self._display_names:
            return text
        result = text
        for raw, display in self._display_names.items():
            if raw in result:
                result = result.replace(raw, display)
        return result

    def validate(self, plan_code: str) -> list[str]:
        """校验规划代码。"""
        return self._validator.validate(plan_code)

    @property
    def node_artifacts(self) -> dict[str, dict[str, Any]]:
        """返回节点产物 holder，供外部构建产物摘要。"""
        return self._node_artifacts_holder

    def _merge_env_config_to_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """将环境配置合并到 inputs，供 skill_code 使用。

        skill_code（如 pipeline_init.py）需要通过 inputs 获取外部资源路径，
        例如 skill_root 用于定位 pptx-craft 等技能目录。
        这些路径由 Environment 在初始化时解析，需要注入到执行上下文中。
        """
        merged = dict(inputs)
        # 注入 skill_root（技能根目录）
        skill_root = self._env.skill_root
        if skill_root and "skill_root" not in merged:
            merged["skill_root"] = skill_root
            logger.debug(
                "[SkillTurboExecutor] merged skill_root from env: %s", skill_root
            )
        # [TEMP-EXTERNAL-SKILL] 注入 skill_name（外部 skill 目录名）
        skill_name = self._env.skill_name
        if skill_name and "skill_name" not in merged:
            merged["skill_name"] = skill_name
        # [TEMP-EXTERNAL-SKILL] 注入 skill_checksum（SHA256 校验值）
        skill_checksum = self._env.skill_checksum
        if skill_checksum and "skill_checksum" not in merged:
            merged["skill_checksum"] = skill_checksum
        # [TEMP-EXTERNAL-SKILL] 注入 skill_checksum_ok（框架层预计算的校验结果）
        if "skill_checksum_ok" not in merged:
            merged["skill_checksum_ok"] = self._env.skill_checksum_ok
        return merged

    def _build_tool_loader_context(
        self,
        inputs: dict[str, Any],
        *,
        request_id: str = "",
        channel_id: str = "",
    ):
        metadata = inputs.get("metadata")
        logger.info(f"_build_tool_loader_context metadata: {metadata}")
        return self._env.build_tool_loader_context(
            request_id=request_id or str(inputs.get("request_id") or ""),
            # session_id 优先取 inputs["session_id"]（skill_turbo_tools 从 request_metadata 稳定写入），
            # 回退 conversation_id（旧路径：parent_session 存在时写入）。原实现只读 conversation_id，
            # 在 parent_session 为 None 时 session_id 恒为空，导致 _load_jw_send_file 返回空、send_file_to_user 无法注册。
            session_id=str(inputs.get("session_id") or inputs.get("conversation_id") or ""),
            channel_id=channel_id or str(inputs.get("channel_id") or ""),
            request_metadata=metadata if isinstance(metadata, dict) else None,
        )

    async def execute_plan(self, plan_code: str, inputs: dict[str, Any]) -> Any:
        """
        执行规划代码的完整流程。

        Args:
            plan_code: 规划代码字符串
            inputs: 输入参数字典

        Returns:
            执行结果

        Raises:
            PlanCodeValidationError: 代码校验失败
            PlanCodeLoadError: 代码加载失败
            ExecutionTimeoutError: 执行超时
            FallbackLimitExceededError: Fallback 次数超过限制
        """
        start = time.monotonic()
        merged_inputs = self._merge_env_config_to_inputs(inputs)
        await self._env.register_tools(
            self._build_tool_loader_context(merged_inputs)
        )
        self._execution_inputs = merged_inputs
        context_tokens = self._setup_execution_context(plan_code, merged_inputs, start)

        logger.info(
            "[SkillTurboExecutor] execute_plan start plan_code_len=%s input_keys=%s",
            len(plan_code),
            list(merged_inputs.keys()),
        )

        try:
            root = self._prepare_root_node(plan_code)
            result = await asyncio.wait_for(
                root.run(merged_inputs),
                timeout=self._config.execution_timeout,
            )
            if self._current_trace:
                self._current_trace.success = True
        except asyncio.TimeoutError as e:
            if self._current_trace:
                self._current_trace.error = f"Execution timeout after {self._config.execution_timeout}s"
            logger.error(
                "[SkillTurboExecutor] execution timeout after %ss",
                self._config.execution_timeout,
            )
            raise ExecutionTimeoutError(
                f"执行超时: 超过 {self._config.execution_timeout} 秒"
            ) from e
        except FallbackLimitExceededError:
            raise
        except Exception as e:
            if self._current_trace:
                self._current_trace.error = str(e)
            logger.error("[SkillTurboExecutor] execution failed: %s", e, exc_info=True)
            raise
        finally:
            self._reset_execution_context(context_tokens)
            self._execution_inputs = {}
            await self._finish_trace(start, "execute_plan")

        return result

    async def execute_plan_stream(
        self,
        plan_code: str,
        inputs: dict[str, Any],
        request_id: str,
        channel_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """
        流式执行规划代码。

        Args:
            plan_code: 规划代码字符串
            inputs: 输入参数字典
            request_id: 请求ID（用于AgentResponseChunk）
            channel_id: 渠道ID（用于AgentResponseChunk）

        Yields:
            AgentResponseChunk: 流式响应片段（与 DeepAgent 兼容）
        """
        from jiuwenswarm.common.schema.agent import AgentResponseChunk

        start = time.monotonic()

        merged_inputs = self._merge_env_config_to_inputs(inputs)
        await self._env.register_tools(
            self._build_tool_loader_context(
                merged_inputs,
                request_id=request_id,
                channel_id=channel_id,
            )
        )
        self._execution_inputs = merged_inputs
        context_tokens = self._setup_execution_context(
            plan_code,
            merged_inputs,
            start,
            request_id=request_id,
            channel_id=channel_id,
            enable_task_tracking=True,
        )

        # 新一轮 SkillTurbo 执行：清除上轮残留的 node_artifacts，避免全新任务误复用旧产物。
        # holder 此时必为空（每次请求新建 Executor），清的是 session 中持久化的旧记录。
        # HITL resume 重放时保留产物（执行跳过主要靠 resume inputs；清盘会误伤可复用记录）。
        if not self._resume_replay:
            await self._clear_stale_node_artifacts()

        logger.info(
            "[SkillTurboExecutor] execute_plan_stream start plan_code_len=%s input_keys=%s resume_replay=%s",
            len(plan_code),
            list(merged_inputs.keys()),
            self._resume_replay,
        )
        # plan.started 必须最先发出，前端依赖它创建本次规划流的根节点。
        yield self._make_plan_started_chunk(request_id, channel_id)

        try:
            try:
                root = self._prepare_root_node(plan_code)
            except PlanCodeValidationError as e:
                yield self._make_error_chunk(
                    request_id,
                    channel_id,
                    f"规划代码校验失败: {e.errors}",
                )
                yield self._make_complete_chunk(request_id, channel_id)
                return
            except PlanCodeLoadError as e:
                yield self._make_error_chunk(
                    request_id,
                    channel_id,
                    f"规划代码加载失败: {e.__cause__ or e}",
                )
                yield self._make_complete_chunk(request_id, channel_id)
                return

            # ⑥ 预先扫描 root 的子节点，初始化所有任务状态（状态为 pending）
            await self._initialize_pending_tasks(root)
            # 立即排出初始化快照，避免等首个 node 输出才看到任务列表
            # （首跑经 write_stream 转发时尤其明显；resume 复用同路径）。
            async for task_chunk in self._drain_task_event_chunks():
                yield task_chunk

            # ⑦ 流式执行规划
            # 注意：底层 LLM HTTP 调用和工具调用已有各自超时，上层暂不加整体超时
            plan_failed = False
            plan_error: str | None = None
            buffer_state = _StreamBufferState()

            async for chunk in self._execute_node_stream(
                root, merged_inputs, request_id, channel_id
            ):
                payload = getattr(chunk, "payload", None)
                if not isinstance(payload, dict):
                    # 非标准 payload（如 None 的 complete chunk）：先 flush 再透传
                    async for flushed in self._flush_all_buffer_chunks(
                        buffer_state, request_id, channel_id
                    ):
                        yield flushed
                    yield chunk
                    continue

                event_type = payload.get("event_type")

                if event_type == "chat.error":
                    plan_failed = True
                    plan_error = str(payload.get("error") or "") or None

                # 非缓冲事件类型：先 flush 所有缓冲，再透传当前事件
                if event_type not in _BUFFERABLE_EVENT_TYPES:
                    async for flushed in self._flush_all_buffer_chunks(
                        buffer_state, request_id, channel_id
                    ):
                        yield flushed
                    yield chunk
                    continue

                # ── 缓冲事件类型 (chat.delta / chat.reasoning) ──
                content = payload.get("content", "")
                if not content:
                    continue

                source_id = payload.get(STREAM_SOURCE_ID_FIELD)

                # 事件类型切换时 flush 同一 source_id 的旧桶
                # （如 reasoning → delta），保证及时交付
                other_event = (
                    "chat.reasoning" if event_type == "chat.delta" else "chat.delta"
                )
                other_key = (source_id, other_event)
                if other_key in buffer_state.buckets:
                    async for flushed in self._flush_bucket_chunks(
                        buffer_state, other_key, request_id, channel_id
                    ):
                        yield flushed

                bucket = buffer_state.get_bucket(source_id, event_type)
                bucket_key = (source_id, event_type)
                piece = markdown_stream_incoming(
                    buffer_state.last_emitted.get(bucket_key, ""),
                    str(content),
                )
                buffer_state.last_emitted[bucket_key] = piece

                # 首个 chunk 立即发送，保证低首字延迟（与 subagent_executor 一致）
                if not bucket.first_chunk_sent:
                    bucket.first_chunk_sent = True
                    if piece != str(content) and isinstance(chunk.payload, dict):
                        chunk.payload = {**chunk.payload, "content": piece}
                    if bucket.plan_name is None:
                        bucket.plan_name = payload.get("plan_name")
                    yield chunk
                    continue

                # 累加到缓冲桶
                bucket.parts.append(piece)
                bucket.since = bucket.since or time.monotonic()
                if bucket.plan_name is None:
                    bucket.plan_name = payload.get("plan_name")

                # 到期 flush
                if time.monotonic() - bucket.since >= _SKILL_TURBO_STREAM_FLUSH_INTERVAL_SECONDS:
                    async for flushed in self._flush_bucket_chunks(
                        buffer_state, (source_id, event_type), request_id, channel_id
                    ):
                        yield flushed

            # 流结束：flush 所有剩余缓冲
            async for flushed in self._flush_all_buffer_chunks(
                buffer_state, request_id, channel_id
            ):
                yield flushed

            if plan_failed:
                yield self._make_plan_finished_chunk(
                    request_id,
                    channel_id,
                    status="failed",
                    error=plan_error,
                )
            else:
                yield self._make_plan_finished_chunk(request_id, channel_id)
            yield self._make_complete_chunk(request_id, channel_id)

        except asyncio.TimeoutError:
            error = f"执行超时: 超过 {self._config.execution_timeout} 秒"
            yield self._make_error_chunk(request_id, channel_id, error)
            yield self._make_plan_finished_chunk(
                request_id,
                channel_id,
                status="failed",
                error=error,
            )
            yield self._make_complete_chunk(request_id, channel_id)
        except AbortError:
            # HITL 中断必须透传到 SkillTurbo.run_stream / adapter，
            # 由后者发出 ask_user_question + invocation_paused，不要在此包成 chat.error。
            raise
        except (FallbackContractError, FallbackLimitExceededError) as e:
            # fallback 终止性失败（契约未达成 / 次数超限）：终止本 plan，但不发 chat.error。
            # chat.error 会被 tool 转发到父会话，前端 useWebSocket 收到后会清空 activeRequestIdRef
            # 并渲染"系统异常"，抢先终结本轮，LLM 来不及按 skill_prompt_rail 转 skill_tool 降级。
            # 这里只发 plan.finished(failed) + complete（前端不处理 plan.*，不抢跑），然后向上
            # raise，由 agent.py 包成 SkillTurboNotHandled，tool 层返回 success=false 给 LLM，
            # LLM 再转 skill_tool 走标准流程——新一轮 request 前端会重新建 activeRequestIdRef。
            logger.error(
                "[SkillTurboExecutor] execute_plan_stream fallback terminal failure, degrading: %s",
                e,
            )
            yield self._make_plan_finished_chunk(
                request_id,
                channel_id,
                status="failed",
                error=str(e),
            )
            yield self._make_complete_chunk(request_id, channel_id)
            raise
        except Exception as e:
            error = str(e)
            yield self._make_error_chunk(request_id, channel_id, error)
            yield self._make_plan_finished_chunk(
                request_id,
                channel_id,
                status="failed",
                error=error,
            )
            yield self._make_complete_chunk(request_id, channel_id)
        finally:
            # 流末兜底落盘节点产物记录（中断路径已在 use_tool 中提前落盘，此处覆盖正常/异常完成）。
            # 必须在 _reset_execution_context 之前执行，否则 session ContextVar 已被重置。
            session = _session_var.get()
            if session is not None and self._node_artifacts_holder:
                await self._persist_node_artifacts(session)
            self._reset_execution_context(context_tokens)
            self._execution_inputs = {}
            self._resume_replay = False
            await self._finish_trace(start)

    def _build_permission_rail(self) -> Any | None:
        """构建 PermissionInterruptRail；权限被禁用或构建失败时返回 None。

        复用 DeepAgent 的 ``build_permission_rail`` 工厂，配置取自
        ``environment.config['permissions']``，model 句柄取 ``model_client``。
        """
        try:
            from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
                build_permission_rail,
            )
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] build_permission_rail import failed: %s", exc
            )
            return None

        try:
            cfg = self._env.config if isinstance(self._env.config, dict) else {}
        except Exception:
            cfg = {}
            logger.debug("[SkillTurboExecutor] read env config failed", exc_info=True)

        model = self._env.model_client
        model_name = None
        try:
            mc = getattr(model, "model_config", None)
            model_name = getattr(mc, "model_name", None) if mc is not None else None
        except Exception:
            model_name = None
            logger.debug("[SkillTurboExecutor] read model_name failed", exc_info=True)

        try:
            return build_permission_rail(cfg, llm=model, model_name=model_name)
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] build_permission_rail failed: %s", exc
            )
            return None

    def _build_ask_user_rail(self) -> SkillTurboAskUserRail | None:
        """构建结构化 ask_user rail；构建失败时返回 None（不阻塞执行）。

        语言取自 environment.config["language"]，与工具描述语言保持一致。
        """
        try:
            cfg = self._env.config if isinstance(self._env.config, dict) else {}
            language = str(cfg.get("language") or "zh")
            return SkillTurboAskUserRail(language=language)
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] build_ask_user_rail failed: %s", exc
            )
            return None

    async def _run_rail_hook(
        self,
        hook_name: str,
        ctx: AgentCallbackContext,
        *,
        skip_rails: set[Any] | None = None,
    ) -> None:
        """按 Rail 优先级执行 hook。

        关键：``AbortError``（PermissionInterruptRail HITL 中断）必须向上抛出，
        否则护栏会被悄悄吞掉，前端永远收不到审批请求。
        其它普通 ``Exception`` 仍按"单 Rail 失败不影响主链"打 warning 后继续。

        ``skip_rails``：需跳过的 rail 实例集合。replay-skip 路径用此参数
        跳过 ``PermissionInterruptRail``，但仍执行事件发射类 rail 的 ``before_tool_call``，
        以补发 ``chat.tool_call`` / ``chat.tool_update`` 事件。
        """
        for rail in self._rails:
            if skip_rails and rail in skip_rails:
                continue
            hook = getattr(rail, hook_name, None)
            if hook is None:
                continue
            try:
                logger.debug(
                    "[SkillTurboExecutor] Running Rail %s hook %s",
                    type(rail).__name__,
                    hook_name,
                )
                await hook(ctx)
            except AbortError:
                # HITL 中断：让上层 use_tool / PlanNode / Agent / Adapter 看到
                raise
            except Exception as e:
                logger.warning(
                    "[SkillTurboExecutor] Rail %s failed: %s - %s",
                    hook_name,
                    type(rail).__name__,
                    e,
                )

    def _setup_execution_context(
        self,
        plan_code: str,
        inputs: dict[str, Any],
        start: float,
        *,
        request_id: str = "",
        channel_id: str = "",
        enable_task_tracking: bool = False,
    ) -> dict[str, Any]:
        """初始化请求级 ContextVar，并返回 token 供 finally 中按原上下文恢复。"""
        # 优先使用外部 session_id（与 _try_skill_turbo_resume 的 request.session_id 一致），
        # 保证 HITL 中断/恢复两侧用同一 checkpointer key。
        # fallback 到 conversation_id（parent_session ID），兼容旧路径。
        session_id = str(
            inputs.get("session_id") or inputs.get("conversation_id") or ""
        ).strip()
        if not session_id:
            logger.warning(
                "[SkillTurboExecutor] inputs 缺失 session_id/conversation_id，checkpointer 将"
                "回退为随机 UUID sid；HITL 断点保存后按 request.session_id 恢复将无法命中"
                "（需检查 skill_turbo_tools 的 metadata 绑定链路）。query=%s",
                str(inputs.get("query", ""))[:120],
            )
        # 使用 env.card 创建 session，pre_run/post_run 需要 card.id 初始化 checkpointer。
        # card=None 会导致 'NoneType' object has no attribute 'id'，resume_ctx 无法持久化。
        card = self._env.card
        session = (
            create_agent_session(session_id=session_id, card=card)
            if session_id
            else create_agent_session(card=card)
        )
        # 用 __skill_turbo agent_id 隔离 checkpointer key，避免 DeepAgent post_run 覆盖
        set_skill_turbo_id(session, card)
        tokens: dict[str, Any] = {
            "session": _session_var.set(session),
            "request": _request_id_var.set(request_id),
            "channel": _channel_id_var.set(channel_id),
        }

        # 绑定 send_file 路由上下文（请求级，按 async 上下文隔离）。
        # send_file_to_user 工具按全局名注册成单例，并发请求会互相覆盖实例字段；
        # 工具执行时优先读此 ContextVar，避免会话串扰。
        try:
            metadata = inputs.get("metadata")
            tokens["send_file_ctx"] = set_send_file_request_context(
                request_id=request_id,
                session_id=session_id,
                channel_id=channel_id,
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] set send_file request context failed: %s", exc
            )

        # Guided mode / outline-review skip 判定读 get_interactive_ask() ContextVar。
        # DeepAgent 的 StreamEventRail 与 Executor 自己的 rail 不是同一实例；
        # 必须在本执行上下文绑定，并注入 executor rail，供 before_tool_call 转绑。
        metadata = inputs.get("metadata")
        meta_copy = dict(metadata) if isinstance(metadata, dict) else {}
        interactive_ask = resolve_interactive_ask_from_inputs(inputs)
        meta_copy["interactive_ask"] = interactive_ask
        self._stream_event_rail.set_skill_turbo_request_metadata(meta_copy)
        try:
            tokens["interactive_ask"] = set_interactive_ask(interactive_ask)
            logger.info(
                "[SkillTurboExecutor] interactive_ask ContextVar bound: %s",
                interactive_ask,
            )
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] set interactive_ask context failed: %s", exc
            )

        effective_project_dir = inputs.get("effective_project_dir")
        if isinstance(effective_project_dir, str) and effective_project_dir.strip():
            try:
                from openjiuwen.core.sys_operation.cwd import init_cwd, set_cwd

                resolved_workspace_dir = effective_project_dir.strip()
                set_effective_request_workspace_dir(resolved_workspace_dir)
                if self._stream_event_rail is not None:
                    self._stream_event_rail.set_runtime_cwd_paths(
                        cwd=resolved_workspace_dir,
                        project_root=resolved_workspace_dir,
                        workspace=resolved_workspace_dir,
                    )
                try:
                    init_cwd(
                        resolved_workspace_dir,
                        project_root=resolved_workspace_dir,
                        workspace=resolved_workspace_dir,
                    )
                except Exception:
                    set_cwd(resolved_workspace_dir)
                logger.debug(
                    "[SkillTurboExecutor] effective request workspace set: %s",
                    resolved_workspace_dir,
                )
            except Exception as exc:
                logger.warning(
                    "[SkillTurboExecutor] set effective request workspace failed: %s",
                    exc,
                )

        if enable_task_tracking:
            tokens["task_events_queue"] = _task_events_queue_var.set([])
            tokens["task_states"] = _task_states_var.set({})
            tokens["current_task_holder"] = _current_task_holder_var.set({"task_id": None})

        self._current_trace = ExecutionTrace(
            plan_code_hash=self._hash_code(plan_code),
            input_keys=list(inputs.keys()),
            start_time=start,
        )
        self._fallback_count = 0
        self._current_plan_code = plan_code
        # 每次执行重置计数器：保证「重放同一 plan_code 时，相同顺序的 (name,args)
        # 调用得到相同 call_index → 相同 tool_call_id」的不变量。
        self._tool_call_counter = {}
        return tokens

    @staticmethod
    def _reset_execution_context(tokens: dict[str, Any]) -> None:
        if "current_task_holder" in tokens:
            _current_task_holder_var.reset(tokens["current_task_holder"])
        if "task_states" in tokens:
            _task_states_var.reset(tokens["task_states"])
        if "task_events_queue" in tokens:
            _task_events_queue_var.reset(tokens["task_events_queue"])
        if "send_file_ctx" in tokens:
            try:
                reset_send_file_request_context(tokens["send_file_ctx"])
            except Exception as exc:
                logger.warning(
                    "[SkillTurboExecutor] reset send_file request context failed: %s", exc
                )
        if "interactive_ask" in tokens:
            try:
                reset_interactive_ask(tokens["interactive_ask"])
            except Exception as exc:
                logger.warning(
                    "[SkillTurboExecutor] reset interactive_ask context failed: %s", exc
                )
        _channel_id_var.reset(tokens["channel"])
        _request_id_var.reset(tokens["request"])
        _session_var.reset(tokens["session"])

    async def _finish_trace(self, start: float, log_prefix: str | None = None) -> None:
        end = time.monotonic()
        if not self._current_trace:
            return

        self._current_trace.end_time = end
        self._current_trace.duration_ms = (end - start) * 1000
        self._current_trace.fallback_count = self._fallback_count

        if log_prefix:
            logger.info(
                "[SkillTurboExecutor] %s done duration_ms=%.1f success=%s fallback_count=%d",
                log_prefix,
                self._current_trace.duration_ms,
                self._current_trace.success,
                self._fallback_count,
            )

        if self._trace_collector and self._config.enable_trace:
            await self._send_trace()

    async def _clear_stale_node_artifacts(self) -> None:
        """新一轮 SkillTurbo 执行前，清除 session 中残留的上轮 node_artifacts。

        场景：上轮"做西湖PPT"中断落盘了 artifacts，本轮"做长城PPT"被 planner 判定为
        全新任务进入 SkillTurbo。若本轮在首个节点产出前就失败（如 plan_code 编译失败），
        finally 因 holder 为空跳过落盘，旧 artifacts 残留 → 下轮误复用。

        注意：必须使用独立 session 而非主 session（_session_var.get()），因为
        post_run() 会调用 close_stream() 关闭 stream emitter，导致后续
        _execute_node_stream 的 drain_session_stream 读不到 tool_call 等事件。
        """
        session = _session_var.get()
        if session is None:
            return
        sid = getattr(session, "session_id", None) or getattr(session, "_session_id", None)
        card = self._env.card
        clear_session = None
        try:
            clear_session = create_agent_session(
                session_id=sid, card=card
            ) if sid else create_agent_session(card=card)
            # 必须与 _setup_execution_context 一致，使用 __skill_turbo agent_id 隔离
            # checkpointer key，否则 clear_node_artifacts 命中默认 key（card.id），
            # 残留产物存储在 {card.id}__skill_turbo key 下永远无法被清除。
            set_skill_turbo_id(clear_session, card)
            await clear_session.pre_run(inputs=None)
            await clear_node_artifacts(clear_session)
        except Exception:
            logger.debug(
                "[SkillTurboExecutor] clear stale node artifacts failed", exc_info=True
            )
        finally:
            # post_run 必须执行以关闭 stream emitter，即使 clear 抛异常也不能漏
            if clear_session is not None:
                try:
                    await clear_session.post_run()
                except Exception:
                    logger.debug(
                        "[SkillTurboExecutor] clear_session post_run failed",
                        exc_info=True,
                    )

    async def _persist_node_artifacts(
        self, session: Any, *, skip_post_run: bool = False
    ) -> None:
        """将内存中的节点产物记录落盘到 session state（checkpointer 持久化）。

        幂等：多次调用安全，每次用 holder 当前快照覆盖写入。
        落盘后不清空 holder，允许后续节点继续累积。
        holder 在 _setup_execution_context 间接通过 __init__ 之外不会被重置；
        真正清空发生在下一次请求创建新 Executor 实例时。

        Args:
            skip_post_run: 仅 update_state 不 post_run，由调用方随后统一 post_run。
                中断路径与 save_resume_ctx 合并落盘时使用，避免重复 post_run。
        """
        if not self._node_artifacts_holder:
            return
        skill = self._env.skill_name or ""
        try:
            await save_node_artifacts(
                session,
                skill=skill,
                nodes=dict(self._node_artifacts_holder),
                skip_post_run=skip_post_run,
            )
        except Exception as exc:
            logger.warning(
                "[SkillTurboExecutor] persist_node_artifacts failed: %s", exc, exc_info=True
            )

    @staticmethod
    def _build_tool_call_context(
        tool_name: str,
        kwargs: dict[str, Any],
    ) -> AgentCallbackContext:
        tool_call = ToolCall(
            name=tool_name,
            arguments=kwargs,
            id=f"tool_{uuid.uuid4().hex[:8]}",
        )
        return AgentCallbackContext(
            agent=None,
            session=_session_var.get(),
            inputs=ToolCallInputs(
                tool_name=tool_name,
                tool_call=tool_call,
                tool_args=kwargs,
            ),
            context=None,
            extra={},
        )

    @staticmethod
    def _build_model_call_context() -> AgentCallbackContext:
        return AgentCallbackContext(
            agent=None,
            session=_session_var.get(),
            inputs=ModelCallInputs(
                messages=[],
                tools=None,
                model_context=None,
            ),
            context=None,
            extra={},
        )

    @staticmethod
    def _serialize_usage_metadata(usage_metadata: Any) -> dict[str, Any]:
        if isinstance(usage_metadata, dict):
            return usage_metadata
        if hasattr(usage_metadata, "model_dump"):
            return usage_metadata.model_dump()
        if hasattr(usage_metadata, "dict"):
            return usage_metadata.dict()
        result: dict[str, Any] = {}
        for key in (
            "model_name",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_cost",
            "output_cost",
            "total_cost",
        ):
            value = getattr(usage_metadata, key, None)
            if value is not None:
                result[key] = value
        return result

    async def _emit_llm_usage(
        self,
        session: Session | None,
        usage_metadata: Any,
        *,
        node_name: str | None = None,
        stream_source_id: str | None = None,
    ) -> None:
        if not usage_metadata or session is None:
            return
        usage_payload = self._serialize_usage_metadata(usage_metadata)
        if not usage_payload:
            return
        payload: dict[str, Any] = {"usage_metadata": usage_payload}
        if node_name is not None:
            payload["plan_name"] = node_name
        if stream_source_id is not None:
            payload[STREAM_SOURCE_ID_FIELD] = stream_source_id
        try:
            await session.write_stream(
                OutputSchema(
                    type="llm_usage",
                    index=0,
                    payload=payload,
                )
            )
        except Exception as e:
            logger.warning(
                "[SkillTurboExecutor] Failed to send llm_usage event: %s",
                e,
            )

    def _interface_log_session_id(self) -> str:
        conversation_id = self._execution_inputs.get("conversation_id", "")
        if conversation_id:
            return str(conversation_id)
        return self._session_id(_session_var.get())

    @staticmethod
    def _session_id(session: Session | None) -> str:
        if session is not None and callable(getattr(session, "get_session_id", None)):
            sid = session.get_session_id()
            return str(sid) if sid else ""
        return ""

    @staticmethod
    def _set_llm_interface_log_session() -> Any | None:
        try:
            return _retry_session.set(_session_var.get())
        except Exception:
            logger.debug("[SkillTurboExecutor] set LLM interface log session failed", exc_info=True)
            return None

    @staticmethod
    def _reset_llm_interface_log_session(token: Any | None) -> None:
        if token is None:
            return
        try:
            _retry_session.reset(token)
        except Exception:
            logger.debug("[SkillTurboExecutor] reset LLM interface log session failed", exc_info=True)

    def has_tool(self, tool_name: str) -> bool:
        """
        检查工具是否存在。

        Args:
            tool_name: 工具名称

        Returns:
            bool: 工具是否存在
        """
        return self._env.has_tool(tool_name)

    def current_task_id(self) -> str | None:
        return self._current_task_id()

    def get_workspace_base_path(self) -> Path | None:
        value = self._execution_inputs.get("effective_project_dir")
        if value:
            try:
                return Path(str(value)).expanduser().resolve()
            except Exception:
                logger.debug("[SkillTurboExecutor] invalid effective_project_dir for artifact detection: %s", value)
        try:
            from jiuwenswarm.common.utils import resolve_tenant_agent_workspace_dir
            return resolve_tenant_agent_workspace_dir()
        except TypeError:
            return None
        except Exception as e:
            logger.warning("[SkillTurboExecutor] 获取agent工作空间目录失败，将返回None: %s", e)
            return None

    # ──────────────────────── Resume 支持 ────────────────────────

    def set_pending_resume(
        self,
        *,
        expected_tool_call_id: str,
        user_input: Any,
        task_states: list[dict[str, Any]] | None = None,
    ) -> None:
        """adapter 在 resume 路径开始执行前调用，注入用户审批回复。

        ``use_tool`` 重放到与 ``expected_tool_call_id`` 相同的 tool_call_id 时，
        将一次性把 ``user_input`` 注入 ctx.extra[RESUME_USER_INPUT_KEY]，
        让 ``PermissionInterruptRail`` 完成 approve / reject 决策。
        其它 tool_call 不带 resume 输入（首次行为）。

        ``task_states``：中断时保存的任务快照；``_initialize_pending_tasks`` 会
        一次性取出并复用其中的 ``task_id``（不消费 ``user_input``）。
        """
        if not expected_tool_call_id:
            return
        self._resume_replay = True
        self._pending_resume = {
            "expected_tool_call_id": expected_tool_call_id,
            "expected_tool_name": _parse_tool_name_from_call_id(
                expected_tool_call_id
            ),
            "user_input": user_input,
            "task_states": copy.deepcopy(task_states) if task_states else None,
        }

    def _take_resume_task_states(self) -> list[dict[str, Any]] | None:
        """从 pending_resume 取出任务快照（只取一次，不影响 user_input 消费）。"""
        pending = self._pending_resume
        if not pending:
            return None
        states = pending.pop("task_states", None)
        if not isinstance(states, list) or not states:
            return None
        return states

    def _snapshot_task_states(self) -> list[dict[str, Any]]:
        """中断时导出当前二层任务快照，供 resume_ctx 持久化。"""
        if self._task_states_holder:
            return [copy.deepcopy(state) for state in self._task_states_holder.values()]
        task_states = _task_states_var.get()
        if task_states:
            return [copy.deepcopy(state) for state in task_states.values()]
        return []

    def _consume_pending_resume_input(
        self, current_tool_call_id: str, current_tool_name: str
    ) -> tuple[Any | None, str]:
        """取出对当前 tool_call 的用户回复。

        返回 ``(user_input, effective_tool_call_id)``：

        - 精确命中：``current_tool_call_id == pending.expected_tool_call_id``，
          返回 ``(user_input, current_tool_call_id)`` 并清空 pending。
        - 回退命中（resume 重放）：``current_tool_call_id`` 因重放时 ask_user
          等工具的非确定性参数（如 LLM 生成的候选项）而与中断时不一致，但
          ``current_tool_name == pending.expected_tool_name`` 且两者末段 idx
          一致时，返回 ``(user_input, expected_tool_call_id)`` 并清空 pending
          ——用中断时的 ``tool_call_id`` 覆盖本次，使 rail 能把 ``user_input``
          注入到与中断点对齐的 ``ctx.extra[RESUME_USER_INPUT_KEY]``，避免
          再次中断形成死循环。

          idx 段比较用于区分"同一 stage 内多个同名工具调用"：idx 是同
          ``(tool_name, args_hash)`` 组合的调用次序，中断点与重放点的 idx
          一致才回退命中，避免把 user_input 误注入到非中断点的同名调用。
        - 未命中：返回 ``(None, current_tool_call_id)``，pending 保留待后续匹配。

        ``current_tool_name`` 由调用方 ``use_tool`` 传入，避免在本方法里再解析
        一次 ``current_tool_call_id``。
        """
        pending = self._pending_resume
        if not pending:
            return None, current_tool_call_id
        expected_id = pending.get("expected_tool_call_id")
        if expected_id == current_tool_call_id:
            user_input = pending.get("user_input")
            self._pending_resume = None
            return user_input, current_tool_call_id
        expected_name = pending.get("expected_tool_name")
        current_idx = _parse_call_idx_from_call_id(current_tool_call_id)
        expected_idx = _parse_call_idx_from_call_id(expected_id)
        idx_match = current_idx is not None and current_idx == expected_idx
        fallback_match = (
            bool(expected_name)
            and current_tool_name == expected_name
            and pending.get("user_input") is not None
            and idx_match
        )
        if fallback_match:
            user_input = pending.get("user_input")
            logger.warning(
                "[SkillTurboExecutor] resume tool_call_id mismatch fallback: "
                "current_tcid=%s expected_tcid=%s tool_name=%s; "
                "aligning to expected_tcid to inject user_input "
                "(likely non-deterministic ask_user args on replay)",
                current_tool_call_id,
                expected_id,
                current_tool_name,
            )
            self._pending_resume = None
            aligned_id = (
                expected_id if isinstance(expected_id, str) else current_tool_call_id
            )
            return user_input, aligned_id
        return None, current_tool_call_id

    def _next_tool_call_id(self, tool_name: str, kwargs: dict[str, Any]) -> str:
        """生成确定性 tool_call_id：基于 (tool_name, canonical_args, call_index) 哈希。

        - canonical_args：``json.dumps(sort_keys, default=str)`` 后取 sha1[:8]
        - call_index：本次执行内同 (name, args) 的第几次调用（从 0 起算）

        重放时只要 plan_code+inputs 一致，相同顺序的同名同参调用必然得到同样的 id；
        与 ``PermissionInterruptRail`` 的 ``user_inputs[tool_call_id]`` 对应即可命中。
        """
        try:
            args_canonical = json.dumps(kwargs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_canonical = repr(sorted(kwargs.items()))
        args_hash = hashlib.sha1(args_canonical.encode("utf-8")).hexdigest()[:8]
        key = f"{tool_name}|{args_hash}"
        idx = self._tool_call_counter.get(key, 0)
        self._tool_call_counter[key] = idx + 1
        return f"skill_turbo-tc-{tool_name}-{args_hash}-{idx}"

    async def use_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """
        调用工具（带 PermissionInterruptRail 护栏）。

        流程：
        1. 生成确定性 tool_call_id（重放时与中断时一致）。
        2. 若处于 resume 模式且 id 命中，注入用户审批载荷到 ctx.extra。
        3. ``before_tool_call`` 跑 rail 链；rail 抛出 ``AbortError`` 表示需要 HITL。
           捕获后保存 ``__skill_turbo_resume_ctx__`` 并向上抛出，让 adapter 转 HITL chunks。
        4. rail 通过 ``ctx.extra['_skip_tool']`` 标记 reject 时，直接返回 rail 注入的
           tool_result，不真正执行工具。
        5. 否则执行工具并跑 ``after_tool_call``。
        """
        session = _session_var.get()
        tool_call_id = self._next_tool_call_id(tool_name, kwargs)
        resume_input, tool_call_id = self._consume_pending_resume_input(
            tool_call_id, tool_name
        )

        # ─── 工具调用 trace 上下文 ───
        # begin_tool_trace_event 在本次 use_tool 调用范围内分配独立 event_id，
        # tool_call_request 与 tool_call_output 共享同一 event_id，便于在
        # full.log 通过 event_id grep 还原一次工具调用的完整入参与出参。
        trace_token = begin_tool_trace_event()
        trace_session_id = self._tool_trace_session_id()
        trace_request_id = _request_id_var.get()
        log_tool_call_input(
            session_id=trace_session_id,
            request_id=trace_request_id,
            iteration=None,
            agent="skill_turbo",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=kwargs,
        )
        trace_start = time.monotonic()
        trace_status: str = "ok"
        trace_result: Any = None
        trace_error: str | None = None

        def _emit_trace_output() -> None:
            duration_ms = (time.monotonic() - trace_start) * 1000.0
            try:
                log_tool_call_output(
                    session_id=trace_session_id,
                    request_id=trace_request_id,
                    iteration=None,
                    agent="skill_turbo",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    status=trace_status,
                    duration_ms=duration_ms,
                    result=trace_result,
                    error=trace_error,
                )
            except Exception:
                logger.debug(
                    "[SkillTurboExecutor] log_tool_call_output failed name=%s tcid=%s",
                    tool_name,
                    tool_call_id,
                    exc_info=True,
                )

        try:
            ctx = build_tool_ctx(
                session=session,
                tool_name=tool_name,
                tool_args=kwargs,
                tool_call_id=tool_call_id,
                resume_user_input=resume_input,
            )

            # skill_turbo 外层统一审批：审批已在 deepagent 层对 skill_turbo 工具
            # 整体完成（config: skill_turbo: ask），内部工具调用不再逐个审批，
            # 始终跳过 PermissionInterruptRail，直接放行。
            # 仍执行事件发射类 rail（stream_event_rail 等）的 before_tool_call，
            # 以补发 chat.tool_call / chat.tool_update 事件，避免前端工具结果
            # 凭空出现、缺调用上下文。
            await self._run_rail_hook(
                "before_tool_call",
                ctx,
                skip_rails=(
                    {self._permission_rail}
                    if self._permission_rail is not None
                    else None
                ),
            )

            # rail 通过 _skip_tool 标记 reject，已经在 ctx.inputs.tool_result 写入结果
            if ctx.extra.get("_skip_tool"):
                logger.debug(
                    "[SkillTurboExecutor] use_tool skipped by rail name=%s tcid=%s",
                    tool_name,
                    tool_call_id,
                )
                trace_status = "skipped"
                trace_result = ctx.inputs.tool_result
                return ctx.inputs.tool_result

            # rail approve（含 resume approve）：清掉断点 ctx，正常执行
            if resume_input is not None:
                try:
                    clear_resume_ctx(session)
                except Exception:
                    logger.debug(
                        "[SkillTurboExecutor] clear_resume_ctx after approve failed", exc_info=True
                    )

            # 获取工具函数
            tool_fn = self._env.get_tool_function(tool_name)
            if tool_fn is None:
                trace_status = "error"
                trace_error = f"未知工具: {tool_name}"
                raise ValueError(f"未知工具: {tool_name}")

            logger.debug(
                "[SkillTurboExecutor] use_tool name=%s tcid=%s kwargs_keys=%s",
                tool_name,
                tool_call_id,
                list(kwargs.keys()),
            )

            if self._current_trace:
                self._current_trace.node_execution_order.append(f"tool:{tool_name}")

            try:
                async with track_tool_resp(tool_name, session_id=self._interface_log_session_id()):
                    result = await tool_fn(**kwargs)
                logger.debug(
                    "[SkillTurboExecutor] use_tool done name=%s result_type=%s",
                    tool_name,
                    type(result).__name__,
                )

                ctx.inputs.tool_result = result
                await self._run_rail_hook("after_tool_call", ctx)
                trace_status = "ok"
                trace_result = result
                return result
            except Exception as e:
                if isinstance(e, AbortError):
                    # HITL 中断：透传，不作为普通工具错误处理
                    trace_status = "interrupted"
                    trace_error = repr(e)
                    raise
                logger.exception(
                    "[SkillTurboExecutor] use_tool failed name=%s err=%r", tool_name, e
                )
                ctx.inputs.tool_result = f"Error: {e}"
                await self._run_rail_hook("after_tool_call", ctx)
                trace_status = "error"
                trace_error = repr(e)
                raise
        except AbortError:
            # HITL 中断（rail before_tool_call 或工具执行抛出）统一在此保存断点上下文
            # （plan_code/inputs/pending_tool_call_id），供 resume 时重放执行并命中
            # 同一 tool_call_id 恢复。透传给上层发 HITL 三件套。
            trace_status = "interrupted"
            try:
                await save_resume_ctx(
                    _session_var.get(),
                    plan_code=self._current_plan_code,
                    inputs=self._execution_inputs,
                    pending_tool_call_id=tool_call_id,
                    task_states=self._snapshot_task_states(),
                )
                logger.info(
                    "[SkillTurboExecutor] HITL interrupt resume_ctx saved tcid=%s",
                    tool_call_id,
                )
            except Exception as save_exc:
                logger.warning(
                    "[SkillTurboExecutor] save resume_ctx failed: %s", save_exc
                )
            raise
        finally:
            _emit_trace_output()
            end_tool_trace_event(trace_token)

    def _tool_trace_session_id(self) -> str:
        """工具 trace 用的 session_id，优先复用 LLM trace 的 ContextVar，保持口径一致。"""
        try:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _LLM_TRACE_SESSION_ID

            sid = _LLM_TRACE_SESSION_ID.get()
            if sid:
                return str(sid)
        except Exception:
            # interface_deep 不可用时回退到本模块自有 session_id 解析。
            pass
        return self._interface_log_session_id()

    def _read_llm_concurrency_limit(self) -> int:
        """从 environment.config 读取 LLM 并发上限。

        约定 key 为 ``llm_concurrency_limit``，<=0 或缺省/非法值表示不限制。
        """
        try:
            cfg = self._env.config if isinstance(self._env.config, dict) else {}
        except Exception:
            cfg = {}
        raw = cfg.get("llm_concurrency_limit", 0)
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[SkillTurboExecutor] invalid llm_concurrency_limit=%r, fallback to 0 (unlimited)",
                raw,
            )
            return 0
        return limit if limit > 0 else 0

    def _llm_concurrency_guard(self) -> AbstractAsyncContextManager[None]:
        """返回 LLM 并发限制的异步上下文管理器。

        - 当未配置（limit<=0）时，返回 no-op 上下文，零开销。
        - 首次进入时按当前事件循环懒初始化 ``asyncio.Semaphore``，避免与
          Executor 构造时所在事件循环不一致导致的 ``RuntimeError``。
        - 当槽位已满需要排队时，会打 INFO 日志输出当前队列长度和等待耗时，
          便于在生产环境观测排队拥堵情况。
        """
        if self._llm_concurrency_limit <= 0:
            return contextlib.nullcontext()
        if self._llm_semaphore is None:
            self._llm_semaphore = asyncio.Semaphore(self._llm_concurrency_limit)
            logger.debug(
                "[SkillTurboExecutor] llm semaphore initialized limit=%d",
                self._llm_concurrency_limit,
            )
        return self._acquire_llm_slot(self._llm_semaphore)

    @contextlib.asynccontextmanager
    async def _acquire_llm_slot(
        self, sem: asyncio.Semaphore
    ) -> AsyncIterator[None]:
        """带排队拥堵日志的 Semaphore 包装。

        - 抢占前若 Semaphore 已满（``locked()`` 为 True），打 INFO 日志记录入队，
          其中 ``queued`` 反映当前已挂起的等待者数量（含本次）。
        - 真正拿到槽位后，如果等过超过 ``_LLM_QUEUE_WAIT_LOG_THRESHOLD_MS`` 毫秒，
          额外打一条 INFO 报告排队耗时；否则仅 DEBUG。
        - 离开 with 块时释放槽位，调用方与原先 ``async with sem`` 行为一致。
        """
        queued = sem.locked()
        if queued:
            # asyncio.Semaphore 内部使用 _waiters 双端队列存放挂起者；
            # 这里只读不改，作为可观测指标使用，缺失时回退到 -1。
            waiters = getattr(sem, "_waiters", None)
            waiting_count = (len(waiters) + 1) if waiters is not None else -1
            logger.info(
                "[SkillTurboExecutor] llm slot queued limit=%d waiting=%d",
                self._llm_concurrency_limit,
                waiting_count,
            )
        wait_start = time.monotonic() if queued else 0.0
        await sem.acquire()
        try:
            if queued:
                waited_ms = (time.monotonic() - wait_start) * 1000.0
                if waited_ms >= _LLM_QUEUE_WAIT_LOG_THRESHOLD_MS:
                    logger.info(
                        "[SkillTurboExecutor] llm slot acquired after wait=%.1fms",
                        waited_ms,
                    )
                else:
                    logger.debug(
                        "[SkillTurboExecutor] llm slot acquired after wait=%.1fms",
                        waited_ms,
                    )
            yield
        finally:
            sem.release()

    @staticmethod
    def _gen_stream_source_id(node_name: str) -> str:
        """生成并发场景下的 stream_source_id。

        前缀 ``skill_turbo:`` 用于与 subagent (``sess_xxx_subagent_…``) 区分；
        ``node_name`` 提升可读性；4 字节 hex 保证同一 node_name 多次并发不碰撞。
        """
        return f"skill_turbo:{node_name}:{secrets.token_hex(4)}"

    async def call_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        node_name: str = "unknown",
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> str:
        """
        调用 LLM（使用Rail机制）。

        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            node_name: 节点名称（用于日志与 source id 可读性）
            concurrent: 是否处于并发上下文中。True 时 Executor 自动生成
                stream_source_id，并注入到本次产生的 llm_reasoning / llm_usage
                事件，方便前端按调用分桶。
            thinking: 可选语义 thinking；None 时不调用 adapt、请求体与改前一致。

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 模型客户端未配置
        """
        ctx = self._build_model_call_context()
        session = ctx.session
        # 获取模型客户端
        client = self._env.model_client
        if client is None:
            raise RuntimeError("model_client 未配置")

        # 仅并发时生成 id；串行场景保持 None，行为不变
        source_id = self._gen_stream_source_id(node_name) if concurrent else None

        logger.debug(
            "[SkillTurboExecutor] call_llm prompt_len=%s system_len=%s node=%s source_id=%s",
            len(prompt),
            len(system_prompt),
            node_name,
            source_id,
        )

        # 记录节点执行
        if self._current_trace:
            self._current_trace.node_execution_order.append(f"llm:call:{node_name}")

        # 构建消息列表
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        thinking_kwargs = resolve_skill_turbo_thinking_kwargs(thinking, client)

        trace_session_token = self._set_llm_interface_log_session()
        try:
            await self._run_rail_hook('before_model_call', ctx)

            # 使用 Model.invoke() 调用 LLM（受实例级 Semaphore 限流保护）
            logger.debug("[SkillTurboExecutor] call_llm max_tokens=%s node=%s", _DEFAULT_LLM_MAX_TOKENS, node_name)
            async with self._llm_concurrency_guard():
                try:
                    response = await client.invoke(
                        messages,
                        max_tokens=_DEFAULT_LLM_MAX_TOKENS,
                        **thinking_kwargs,
                    )
                except Exception as inv_exc:
                    if thinking_kwargs and is_skill_turbo_thinking_param_error(inv_exc):
                        logger.warning(
                            "[SkillTurboExecutor] thinking params rejected, bare retry once: %s",
                            inv_exc,
                        )
                        response = await client.invoke(
                            messages,
                            max_tokens=_DEFAULT_LLM_MAX_TOKENS,
                        )
                    else:
                        raise

            # llm_usage 事件注入 source_id
            await self._emit_llm_usage(
                session,
                getattr(response, "usage_metadata", None),
                node_name=node_name,
                stream_source_id=source_id,
            )

            # AssistantMessage 有两个属性：
            # - content: 普通文本内容
            # - reasoning_content: 推理过程内容（与 DeepAgent 保持一致）

            # 处理 reasoning_content（推理过程）并注入 source_id
            reasoning_content = getattr(response, "reasoning_content", None)
            if reasoning_content and session:
                payload: dict[str, Any] = {
                    "content": str(reasoning_content),
                    "plan_name": self._display_name(node_name),
                }
                if source_id is not None:
                    payload[STREAM_SOURCE_ID_FIELD] = source_id
                try:
                    await session.write_stream(
                        OutputSchema(type="llm_reasoning", index=0, payload=payload)
                    )
                except Exception as e:
                    logger.warning(
                        "[SkillTurboExecutor] Failed to send llm_reasoning event: %s",
                        e,
                    )

            # AssistantMessage.content 是响应文本
            result = response.content

            # 处理正文内容并注入 source_id（与 reasoning 保持一致）
            if result and session:
                output_payload: dict[str, Any] = {
                    "content": str(result),
                    "plan_name": self._display_name(node_name),
                }
                if source_id is not None:
                    output_payload[STREAM_SOURCE_ID_FIELD] = source_id
                try:
                    await session.write_stream(
                        OutputSchema(type="llm_output", index=0, payload=output_payload)
                    )
                except Exception as e:
                    logger.warning(
                        "[SkillTurboExecutor] Failed to send llm_output event: %s",
                        e,
                    )

            # 设置response（供after回调使用）
            ctx.inputs.response = result

            return result
        except Exception as e:
            ctx.exception = e
            await self._run_rail_hook('on_model_exception', ctx)
            raise
        finally:
            self._reset_llm_interface_log_session(trace_session_token)
            await self._run_rail_hook('after_model_call', ctx)

    async def stream_llm(
        self,
        prompt: str,
        system_prompt: str = "",
        node_name: str = "unknown",
        concurrent: bool = False,
        thinking: str | None = None,
    ) -> AsyncIterator[str]:
        """
        流式调用 LLM（使用Rail机制）。
        
        注意：reasoning_content 会自动处理并发送事件，业务代码无需关心。

        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            node_name: 节点名称（用于标识来源）
            concurrent: 是否处于并发上下文中。True 时 Executor 自动生成
                stream_source_id，并注入到本次产生的 llm_reasoning / llm_usage
                事件，方便前端按调用分桶。
            thinking: 可选语义 thinking；None 时不调用 adapt、请求体与改前一致。

        Yields:
            str: 流式文本片段（普通文本内容）
            
        内部机制：
            - reasoning 事件会通过 session stream 实时发送
            - 业务代码只看到普通文本，无需关心 reasoning
        """
        ctx = self._build_model_call_context()
        session = ctx.session
        # 获取模型客户端
        client = self._env.model_client
        if client is None:
            raise RuntimeError("model_client 未配置")

        # 仅并发时生成 id；串行场景保持 None，行为不变
        source_id = self._gen_stream_source_id(node_name) if concurrent else None

        logger.debug(
            "[SkillTurboExecutor] stream_llm prompt_len=%s system_len=%s node=%s source_id=%s",
            len(prompt),
            len(system_prompt),
            node_name,
            source_id,
        )

        # 记录节点执行
        if self._current_trace:
            self._current_trace.node_execution_order.append(f"llm:stream:{node_name}")

        # 构建消息列表
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        thinking_kwargs = resolve_skill_turbo_thinking_kwargs(thinking, client)

        accumulated_message = ""
        trace_session_token = self._set_llm_interface_log_session()
        try:
            await self._run_rail_hook('before_model_call', ctx)
            
            # 使用 Model.stream() 流式调用 LLM
            # 流式调用整个生命周期都占用一个 LLM "槽位"，因此用 Semaphore 包裹整个流。
            async with self._llm_concurrency_guard():
                async def _iter_chunks():
                    try:
                        async for chunk in client.stream(
                            messages,
                            max_tokens=_DEFAULT_LLM_MAX_TOKENS,
                            **thinking_kwargs,
                        ):
                            yield chunk
                    except Exception as stream_exc:
                        if thinking_kwargs and is_skill_turbo_thinking_param_error(stream_exc):
                            logger.warning(
                                "[SkillTurboExecutor] thinking params rejected, "
                                "bare retry once: %s",
                                stream_exc,
                            )
                            async for chunk in client.stream(
                                messages,
                                max_tokens=_DEFAULT_LLM_MAX_TOKENS,
                            ):
                                yield chunk
                        else:
                            raise

                async for chunk in _iter_chunks():
                    # usage_metadata 通常只在最后一个 chunk 有值，避免对每个 chunk 都调用
                    chunk_usage = getattr(chunk, "usage_metadata", None)
                    if chunk_usage:
                        await self._emit_llm_usage(
                            session,
                            chunk_usage,
                            node_name=node_name,
                            stream_source_id=source_id,
                        )
                    # AssistantMessageChunk 有两个属性：
                    # - content: 普通文本内容
                    # - reasoning_content: 推理过程内容
                    
                    # ──────────────────────── 自动处理 reasoning_content ────────────────────────
                    # 框架层面自动发送 reasoning 事件，业务代码无需关心
                    reasoning_content = getattr(chunk, "reasoning_content", None)
                    if reasoning_content:
                        if session:
                            reasoning_payload: dict[str, Any] = {
                                "content": str(reasoning_content),
                                "plan_name": self._display_name(node_name),
                            }
                            if source_id is not None:
                                reasoning_payload[STREAM_SOURCE_ID_FIELD] = source_id
                            try:
                                await session.write_stream(
                                    OutputSchema(
                                        type="llm_reasoning",
                                        index=0,
                                        payload=reasoning_payload,
                                    )
                                )
                            except Exception as e:
                                logger.warning(
                                    "[SkillTurboExecutor] Failed to send stream llm_reasoning event: %s",
                                    e,
                                )
                    
                    # ──────────────────────── 处理普通文本内容 ────────────────────────
                    text_chunk = chunk.content
                    if text_chunk:
                        # 累积消息（供after回调使用）
                        accumulated_message += text_chunk

                        # 通过 session stream 发送正文 delta（与 reasoning 保持一致）
                        if session:
                            output_payload: dict[str, Any] = {
                                "content": str(text_chunk),
                                "plan_name": self._display_name(node_name),
                            }
                            if source_id is not None:
                                output_payload[STREAM_SOURCE_ID_FIELD] = source_id
                            try:
                                await session.write_stream(
                                    OutputSchema(
                                        type="llm_output",
                                        index=0,
                                        payload=output_payload,
                                    )
                                )
                            except Exception as e:
                                logger.warning(
                                    "[SkillTurboExecutor] Failed to send stream llm_output event: %s",
                                    e,
                                )

                        # 返回普通文本（业务代码只看到普通文本）
                        yield text_chunk
            
            # 设置response（供after回调使用）
            ctx.inputs.response = accumulated_message
            
        except Exception as e:
            ctx.exception = e
            await self._run_rail_hook('on_model_exception', ctx)
            raise
        finally:
            self._reset_llm_interface_log_session(trace_session_token)
            await self._run_rail_hook('after_model_call', ctx)

    async def fallback(
        self,
        node: PlanNode,
        inputs: dict[str, Any],
        error: Exception,
    ) -> Any:
        """
        节点执行失败时委托 fallback_handler 兜底。

        Args:
            node: 失败的节点
            inputs: 输入参数
            error: 异常对象

        Returns:
            Fallback 执行结果（status="degraded"）

        Raises:
            FallbackLimitExceededError: Fallback 次数超过限制
            RuntimeError: Fallback handler 未初始化
        """
        self._check_fallback_limit(error)
        self._record_fallback_call(node, "fallback")

        handler = self._env.fallback_handler
        if handler is None:
            logger.error(
                "[SkillTurboExecutor] fallback_handler missing, re-raising error plan_name=%s error=%s",
                node.plan_name,
                error,
            )
            raise error

        logger.warning(
            "[SkillTurboExecutor] node fallback via handler plan_name=%s error=%s fallback_count=%d",
            node.plan_name,
            error,
            self._fallback_count,
        )
        return await handler.fallback(
            node_name=node.plan_name,
            instruction=node.instruction or "",
            inputs=inputs,
            error=error,
            parent_session=_session_var.get(),
        )

    async def fallback_stream(
        self,
        node: PlanNode,
        inputs: dict[str, Any],
        error: Exception,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        节点执行失败时委托 fallback_handler 兜底（流式版本）。

        Args:
            node: 失败的节点
            inputs: 输入参数
            error: 异常对象

        Yields:
            dict[str, Any]: Fallback Agent 的流式输出chunk，包含fallback标识事件和内容chunk

        Raises:
            FallbackLimitExceededError: Fallback 次数超过限制
            RuntimeError: Fallback handler 未初始化
        """
        self._check_fallback_limit(error)
        self._record_fallback_call(node, "fallback_stream")

        handler = self._env.fallback_handler
        if handler is None:
            logger.error(
                "[SkillTurboExecutor] fallback_handler missing, re-raising error plan_name=%s error=%s",
                node.plan_name,
                error,
            )
            raise error

        logger.warning(
            "[SkillTurboExecutor] node fallback_stream via handler plan_name=%s error=%s fallback_count=%d",
            node.plan_name,
            error,
            self._fallback_count,
        )
        async for chunk in handler.fallback_stream(
            node_name=node.plan_name,
            instruction=node.instruction or "",
            inputs=inputs,
            error=error,
            parent_session=_session_var.get(),
        ):
            yield chunk

    async def _execute_node_stream(
        self,
        node: PlanNode,
        inputs: dict[str, Any],
        request_id: str,
        channel_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """流式执行单个节点，并实时转发工具等框架事件。"""
        from jiuwenswarm.common.schema.agent import AgentResponseChunk

        logger.debug("[SkillTurboExecutor] _execute_node_stream start")
        output_queue: asyncio.Queue[AgentResponseChunk | BaseException | None] = asyncio.Queue()
        session = _session_var.get()

        async def enqueue_chunk(chunk: Any) -> None:
            async for task_chunk in self._drain_task_event_chunks():
                await output_queue.put(task_chunk)
            current_task_id = self._current_task_id()

            # 处理 fallback_stream 返回的 dict 格式（包含 event_type）
            if isinstance(chunk, dict) and "event_type" in chunk:
                await output_queue.put(
                    self._make_event_chunk(request_id, channel_id, chunk, current_task_id)
                )
            else:
                await output_queue.put(
                    self._make_node_delta_chunk(request_id, channel_id, node, chunk, current_task_id)
                )

        async def drain_session_stream() -> None:
            if session is None:
                return
            async for stream_chunk in session.stream_iterator():
                # 先发送 task 事件（确保 task.start 在 chat 事件之前）
                async for task_chunk in self._drain_task_event_chunks():
                    await output_queue.put(task_chunk)
                payload = parse_stream_chunk(stream_chunk)
                if payload is None:
                    continue
                await output_queue.put(
                    self._make_session_event_chunk(request_id, channel_id, payload, self._current_task_id())
                )

        async def produce_node_output() -> None:
            try:
                await output_queue.put(self._make_node_started_chunk(
                    request_id, channel_id, node, self._current_task_id()
                ))
                async for chunk in node.run_stream(inputs):
                    await enqueue_chunk(chunk)
                async for task_chunk in self._drain_task_event_chunks():
                    await output_queue.put(task_chunk)
                await output_queue.put(self._make_node_finished_chunk(
                    request_id, channel_id, node, self._current_task_id()
                ))
            except BaseException as e:
                await output_queue.put(e)
            finally:
                await output_queue.put(None)

        producer = asyncio.create_task(produce_node_output())
        session_drain_task = asyncio.create_task(drain_session_stream())
        try:
            while True:
                item = await output_queue.get()
                if item is None:
                    break
                if isinstance(item, FallbackLimitExceededError):
                    # fallback 超限：不发 node_error chat.error（会抢跑前端，详见 execute_plan_stream
                    # 的 except FallbackContractError 注释），直接向上 raise 走不发 chat.error 的降级路径。
                    logger.error(
                        "[SkillTurboExecutor] _execute_node_stream FallbackLimitExceededError: %s",
                        item,
                    )
                    async for task_chunk in self._drain_task_event_chunks():
                        yield task_chunk
                    raise item
                if isinstance(item, FallbackContractError):
                    # fallback 契约失败：不在内层转成 node_error chat.error（会抢跑前端），
                    # 直接向上 raise，交给 execute_plan_stream 的 except FallbackContractError
                    # 走"不发 chat.error、只 plan.finished(failed)+complete"的降级路径。
                    logger.error(
                        "[SkillTurboExecutor] _execute_node_stream FallbackContractError, propagating: %s",
                        item,
                    )
                    raise item
                if isinstance(item, BaseException):
                    # cancel 中断（asyncio.CancelledError / KeyboardInterrupt）必须向上抛，
                    # 不能当作普通节点错误处理 —— 否则节点会被误判 completed，
                    # 产物记录为"已成功执行（无产物）"，DeepAgent 误以为步骤已完成而跳过。
                    if isinstance(item, (asyncio.CancelledError, KeyboardInterrupt)):
                        logger.warning(
                            "[SkillTurboExecutor] _execute_node_stream propagate cancel interrupt: %s",
                            type(item).__name__,
                        )
                        raise item
                    # HITL 中断（PermissionInterruptRail.AbortError）必须向上抛，
                    # 不能转成 node_error_chunk —— 否则 SkillTurbo.run_stream 看不到中断，
                    # adapter 也就拿不到 AbortError 来发 HITL 三件套。
                    # 先排空已入队的 task.*，避免中断瞬间丢最新任务快照。
                    if isinstance(item, AbortError) or extract_tool_interrupt(item) is not None:
                        logger.info(
                            "[SkillTurboExecutor] _execute_node_stream propagate HITL AbortError: %s",
                            item,
                        )
                        async for task_chunk in self._drain_task_event_chunks():
                            yield task_chunk
                        raise item
                    logger.error("[SkillTurboExecutor] _execute_node_stream error: %s", item)
                    async for task_chunk in self._drain_task_event_chunks():
                        yield task_chunk
                    yield self._make_node_error_chunk(
                        request_id,
                        channel_id,
                        node,
                        str(item),
                        self._current_task_id(),
                    )
                    continue
                yield item
        finally:
            session_drain_task.cancel()
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, session_drain_task, return_exceptions=True)

    async def _flush_bucket_chunks(
        self,
        buffer_state: _StreamBufferState,
        bucket_key: tuple[str | None, str],
        request_id: str,
        channel_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """flush 单个缓冲桶，yield 合并后的 chunk。

        Args:
            buffer_state: 缓冲层状态
            bucket_key: (source_id, event_type)
            request_id: 请求ID
            channel_id: 渠道ID
        """
        source_id, event_type = bucket_key
        bucket = buffer_state.buckets.pop(bucket_key, None)
        if bucket is None or not bucket.parts:
            return

        merged_content = terminate_dangling_markdown_fence("".join(bucket.parts))
        if not merged_content:
            return
        buffer_state.last_emitted[bucket_key] = merged_content

        payload: dict[str, Any] = {
            "event_type": event_type,
            "content": merged_content,
        }
        # 保留 plan_name（取桶内首个 chunk 的值，同桶内通常一致）
        if bucket.plan_name:
            payload["plan_name"] = bucket.plan_name
        if source_id is not None:
            payload[STREAM_SOURCE_ID_FIELD] = source_id
        task_id = self._current_task_id()
        if task_id:
            payload["task_id"] = task_id

        yield self._make_chunk(request_id, channel_id, payload)

    async def _flush_all_buffer_chunks(
        self,
        buffer_state: _StreamBufferState,
        request_id: str,
        channel_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """flush 所有缓冲桶，按 FIFO 顺序 yield 合并后的 chunk。

        用于非缓冲事件前、流结束、异常退出等场景的统一 flush。
        """
        for bucket_key, _ in buffer_state.all_buckets():
            async for chunk in self._flush_bucket_chunks(
                buffer_state, bucket_key, request_id, channel_id
            ):
                yield chunk
        buffer_state.clear()

    def _check_fallback_limit(self, error: Exception) -> None:
        """检查 fallback 前置条件：是否启用、是否超限。不修改计数。"""
        if not self._config.enable_fallback:
            logger.error(
                "[SkillTurboExecutor] fallback disabled, re-raising error: %s",
                error,
            )
            raise error

        if self._fallback_count >= self._config.max_fallback_count:
            logger.error(
                "[SkillTurboExecutor] fallback limit exceeded: %d/%d",
                self._fallback_count,
                self._config.max_fallback_count,
            )
            raise FallbackLimitExceededError(
                f"Fallback 次数超过限制: {self._fallback_count}/{self._config.max_fallback_count}。最后错误: {error}"
            ) from error

    def _record_fallback_call(self, node: PlanNode, trace_prefix: str) -> None:
        self._fallback_count += 1
        if self._current_trace:
            self._current_trace.node_execution_order.append(f"{trace_prefix}:{node.plan_name}")

    @staticmethod
    def _make_chunk(
        request_id: str,
        channel_id: str,
        payload: dict[str, Any] | None,
        is_complete: bool = False,
    ) -> AgentResponseChunk:
        """集中构造流式响应，避免各事件分支的基础字段出现漂移。"""
        from jiuwenswarm.common.schema.agent import AgentResponseChunk

        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=is_complete,
        )

    def _make_complete_chunk(self, request_id: str, channel_id: str) -> AgentResponseChunk:
        return self._make_chunk(request_id, channel_id, None, is_complete=True)

    def _make_error_chunk(
        self,
        request_id: str,
        channel_id: str,
        error: str,
    ) -> AgentResponseChunk:
        return self._make_chunk(
            request_id,
            channel_id,
            {"event_type": "chat.error", "error": error},
        )

    def _make_plan_started_chunk(
        self,
        request_id: str,
        channel_id: str,
    ) -> AgentResponseChunk:
        return self._make_chunk(
            request_id,
            channel_id,
            {
                "event_type": "plan.started",
                "plan_name": "root",
                "content": "规划执行开始:\n",
            },
        )

    def _make_plan_finished_chunk(
        self,
        request_id: str,
        channel_id: str,
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> AgentResponseChunk:
        payload = {
            "event_type": "plan.finished",
            "plan_name": "root",
            "final": True,
        }
        if status:
            payload["status"] = status
        if error:
            payload["error"] = error
        return self._make_chunk(request_id, channel_id, payload)

    def _make_event_chunk(
        self,
        request_id: str,
        channel_id: str,
        payload: dict[str, Any],
        task_id: str | None,
    ) -> AgentResponseChunk:
        if task_id and "task_id" not in payload and str(payload.get("event_type", "")).startswith("chat."):
            payload = {**payload, "task_id": task_id}
        return self._make_chunk(request_id, channel_id, payload)

    def _make_session_event_chunk(
        self,
        request_id: str,
        channel_id: str,
        payload: dict[str, Any],
        task_id: str | None,
    ) -> AgentResponseChunk:
        return self._make_event_chunk(request_id, channel_id, payload, task_id)

    def _make_task_event_chunk(self, task_event: dict[str, Any]) -> AgentResponseChunk:
        return self._make_chunk(
            task_event["request_id"],
            task_event["channel_id"],
            task_event["payload"],
            is_complete=task_event.get("is_complete", False),
        )

    def _make_node_started_chunk(
        self,
        request_id: str,
        channel_id: str,
        node: PlanNode,
        task_id: str | None,
    ) -> AgentResponseChunk:
        display = self._display_name(node.plan_name)
        payload = {
            "event_type": "node.started",
            "plan_name": display,
            "content": f"节点 {display} 开始执行\n",
        }
        if task_id:
            payload["task_id"] = task_id
        return self._make_chunk(request_id, channel_id, payload)

    def _make_node_finished_chunk(
        self,
        request_id: str,
        channel_id: str,
        node: PlanNode,
        task_id: str | None,
    ) -> AgentResponseChunk:
        payload = {
            "event_type": "node.finished",
            "plan_name": self._display_name(node.plan_name),
        }
        if task_id:
            payload["task_id"] = task_id
        return self._make_chunk(request_id, channel_id, payload)

    def _make_node_delta_chunk(
        self,
        request_id: str,
        channel_id: str,
        node: PlanNode,
        content: Any,
        task_id: str | None,
    ) -> AgentResponseChunk:
        # 提取实际内容和正确的 plan_name
        actual_content = content
        plan_name = node.plan_name  # 默认使用传入的 node
        data_payload = content if isinstance(content, dict) else None
        
        if isinstance(content, dict):
            # 从 chunk 中提取正确的 plan_name（如果存在），并转为显示名
            chunk_plan_name = content.get("node") or content.get("plan_name")
            if chunk_plan_name:
                plan_name = self._display_name(chunk_plan_name)

            # 提取实际内容：优先 content 字段，其次 message 字段，最后空字符串
            # 不再把整个 dict 作为 content 回退，避免语义不清
            actual_content = content.get("content", "")
            if not actual_content:
                actual_content = content.get("message", "")

            # 将消息文本中出现的内部 plan_name 替换为中文显示名
            actual_content = self._replace_plan_names_in_text(actual_content)

            # 进度/完成类消息（来自 message 字段）追加换行，避免前端拼接成一行
            chunk_status = content.get("status")
            if chunk_status in ("progress", "ok") and actual_content:
                actual_content = actual_content.rstrip("\n") + "\n"

            # 将 data_payload 中的 current_node 转为显示名（浅拷贝避免修改原始 chunk）
            if data_payload is not None and "current_node" in data_payload:
                data_payload = dict(data_payload)
                data_payload["current_node"] = self._display_name(data_payload["current_node"])
        
        payload = {
            "event_type": "chat.delta",
            "content": actual_content,
            "plan_name": plan_name,
        }
        if data_payload is not None:
            payload["data"] = data_payload
        if task_id:
            payload["task_id"] = task_id
        return self._make_chunk(request_id, channel_id, payload)

    def _make_node_error_chunk(
        self,
        request_id: str,
        channel_id: str,
        node: PlanNode,
        error: str,
        task_id: str | None,
    ) -> AgentResponseChunk:
        payload = {
            "event_type": "chat.error",
            "error": error,
            "plan_name": self._display_name(node.plan_name),
        }
        if task_id:
            payload["task_id"] = task_id
        return self._make_chunk(request_id, channel_id, payload)

    def _current_task_id(self) -> str | None:
        # 优先读取实例属性（跨协程共享，解决 asyncio.create_task 复制 ContextVar 的问题）
        holder_task_id = self._current_task_id_holder.get("task_id")
        if holder_task_id:
            return holder_task_id
        # Fallback: 从实例属性的任务状态里找 in_progress 的任务
        for task_id, state in self._task_states_holder.items():
            if state.get("status") == "in_progress":
                return task_id
        # 最后 fallback 到 ContextVar（兼容旧逻辑）
        task_context = _current_task_context_var.get()
        if task_context:
            return task_context.get("task_id")
        current_task_holder = _current_task_holder_var.get()
        if current_task_holder and current_task_holder.get("task_id"):
            return current_task_holder.get("task_id")
        task_states = _task_states_var.get()
        if not task_states:
            return None
        for task_id, state in task_states.items():
            if state.get("status") == "in_progress":
                return task_id
        return None

    async def _drain_task_event_chunks(self) -> AsyncIterator[AgentResponseChunk]:
        """将 PlanNode 回调期间暂存的 task 事件按 FIFO 顺序转成前端 chunk。"""
        task_events_queue = _task_events_queue_var.get()
        if not task_events_queue:
            return
        while task_events_queue:
            task_event = task_events_queue.pop(0)
            self._normalize_task_event_type(task_event)
            yield self._make_task_event_chunk(task_event)

    @staticmethod
    def _normalize_task_event_type(task_event: dict[str, Any]) -> None:
        payload = task_event.get("payload", {})
        if "event_type" in payload:
            return
        payload["event_type"] = "task.complete" if "status" in payload else "task.start"

    async def _initialize_pending_tasks(self, root: PlanNode) -> None:
        """预置二层任务列表，让前端在第一个 task.start 前就能展示完整待办。

        首次执行：为每个 sub_plan 分配新 ``task_id``。
        HITL resume：复用 ``resume_ctx.task_states`` 中的 ``task_id`` / 状态，
        避免前端 taskRuns 因新 UUID 叠出第二套 Stage。
        """
        task_states = _task_states_var.get()
        if task_states is None or not getattr(root, "sub_plans", None):
            return

        # 清空实例属性（新请求开始）
        self._task_states_holder.clear()
        self._current_task_id_holder["task_id"] = None

        saved = self._take_resume_task_states()
        if saved is not None:
            restored = self._restore_task_states_from_snapshot(root, saved)
            for task_id, task_state in restored.items():
                task_states[task_id] = task_state
                self._task_states_holder[task_id] = task_state
            await self._emit_task_update_event()
            logger.info(
                "[SkillTurboExecutor] execute_plan_stream restored %d tasks from resume snapshot",
                len(task_states),
            )
            return

        for idx, subplan in enumerate(root.sub_plans):
            task_id = f"task_{uuid.uuid4().hex[:8]}"
            task_state = {
                "task_id": task_id,
                "task_content": self._display_name(subplan.plan_name),
                "task_index": idx,
                "source": "skill_turbo",
                "status": "pending",
            }
            task_states[task_id] = task_state
            # 同时写入实例属性（跨协程共享）
            self._task_states_holder[task_id] = task_state

        await self._emit_task_update_event()
        logger.info(
            "[SkillTurboExecutor] execute_plan_stream initialized %d pending tasks",
            len(task_states),
        )

    def _restore_task_states_from_snapshot(
        self,
        root: PlanNode,
        saved: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """按 display name / index 对齐 resume 快照，复用原 ``task_id``。"""
        by_content: dict[str, dict[str, Any]] = {}
        by_index: dict[int, dict[str, Any]] = {}
        for item in saved:
            if not isinstance(item, dict):
                continue
            content = item.get("task_content")
            if isinstance(content, str) and content.strip():
                by_content[content] = item
            idx = item.get("task_index")
            if isinstance(idx, int):
                by_index[idx] = item

        restored: dict[str, dict[str, Any]] = {}
        used_ids: set[str] = set()
        for idx, subplan in enumerate(root.sub_plans):
            display = self._display_name(subplan.plan_name)
            snap = by_content.get(display) or by_index.get(idx)
            task_id: str | None = None
            if snap is not None:
                raw_id = snap.get("task_id")
                if isinstance(raw_id, str) and raw_id.strip() and raw_id not in used_ids:
                    task_id = raw_id.strip()

            if task_id is not None and snap is not None:
                state = copy.deepcopy(snap)
                state["task_id"] = task_id
                state["task_content"] = display
                state["task_index"] = idx
                state.setdefault("source", "skill_turbo")
                if not isinstance(state.get("status"), str):
                    state["status"] = "pending"
                used_ids.add(task_id)
            else:
                task_id = f"task_{uuid.uuid4().hex[:8]}"
                state = {
                    "task_id": task_id,
                    "task_content": display,
                    "task_index": idx,
                    "source": "skill_turbo",
                    "status": "pending",
                }
            restored[task_id] = state
        return restored

    def _live_task_states(self) -> dict[str, dict[str, Any]] | None:
        """运行中任务表：优先实例 holder（跨 create_task 共享），再回退 ContextVar。"""
        if self._task_states_holder:
            return self._task_states_holder
        return _task_states_var.get()

    async def _emit_task_update_event(self) -> None:
        """发送 task.update 事件（全量任务状态快照）。

        优先读 ``_task_states_holder``：``asyncio.create_task`` 会复制 ContextVar，
        仅依赖 ``_task_states_var`` 可能读到空/旧 dict，把错误的 pending 快照推给前端。
        """
        events_queue = _task_events_queue_var.get()
        task_states = self._live_task_states()

        if task_states is None or events_queue is None:
            logger.debug(
                "[SkillTurboExecutor] _emit_task_update_event skip: no task_states or events_queue"
            )
            return

        # 深拷贝任务状态（避免后续修改影响已发送的事件）；按 index 排序保证前端稳定
        all_tasks = sorted(
            (copy.deepcopy(state) for state in task_states.values()),
            key=lambda t: (
                t.get("task_index") if isinstance(t.get("task_index"), int) else 10**9,
                str(t.get("task_id") or ""),
            ),
        )

        # 计算统计信息
        total = len(all_tasks)
        completed = sum(1 for t in all_tasks if t.get("status") == "completed")
        in_progress = sum(1 for t in all_tasks if t.get("status") == "in_progress")
        pending = sum(1 for t in all_tasks if t.get("status") == "pending")
        failed = sum(1 for t in all_tasks if t.get("status") == "failed")

        # 构建 payload
        payload = {
            "event_type": "task.update",
            "tasks": all_tasks,
            "total_tasks": total,
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "pending_tasks": pending,
            "failed_tasks": failed,
            "parent_request_id": _request_id_var.get(),
            "timestamp": time.time(),
        }

        status_preview = [
            f"{t.get('task_index')}:{t.get('status')}"
            for t in all_tasks[:8]
        ]
        logger.info(
            "[SkillTurboExecutor] task.update: %d tasks - "
            "%d completed, %d in_progress, %d pending, %d failed; preview=%s",
            total,
            completed,
            in_progress,
            pending,
            failed,
            status_preview,
        )

        # 添加到事件队列
        task_update_event = {
            "request_id": _request_id_var.get(),
            "channel_id": _channel_id_var.get(),
            "payload": payload,
            "is_complete": False,
        }
        events_queue.append(task_update_event)

    async def _should_skip_subplan_execute(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
    ) -> bool:
        """HITL resume 重放时，跳过已 completed 的二层 Stage 真实执行。

        in_progress / pending 节点仍完整重入，以便命中同一 tool_call_id 注入 answers。
        """
        del inputs  # 接口与 PlanNode 回调一致；跳过判定只依赖 task_states
        if not self._resume_replay:
            return False
        if subplan.depth != 1:
            return False
        task_states = self._live_task_states()
        if not task_states:
            return False
        found = self._find_task_state_by_plan_name(subplan.plan_name, task_states)
        if found is None:
            return False
        _task_id, task_state = found
        if task_state.get("status") != "completed":
            return False
        logger.info(
            "[SkillTurboExecutor] resume skip execute completed stage plan_name=%s task_id=%s",
            subplan.plan_name,
            _task_id,
        )
        return True

    async def _should_suppress_subplan_start_banner(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
    ) -> bool:
        """HITL resume 重放时，抑制二层 stage 的「开始执行」进度横幅。

        completed stage 已由 ``_should_skip_subplan_execute`` 静默跳过；
        in_progress stage 仍需重入（命中 ask_user tool_call_id），但不应再刷横幅。
        """
        del inputs
        if not self._resume_replay:
            return False
        if subplan.depth != 1:
            return False
        task_states = self._live_task_states()
        if not task_states:
            return False
        found = self._find_task_state_by_plan_name(subplan.plan_name, task_states)
        if found is None:
            return False
        _task_id, task_state = found
        if task_state.get("status") != "in_progress":
            return False
        logger.info(
            "[SkillTurboExecutor] suppress start banner for in-progress stage "
            "(HITL resume replay): plan_name=%s task_id=%s",
            subplan.plan_name,
            _task_id,
        )
        return True

    async def _before_subplan_execute(
        self, subplan: PlanNode, inputs: dict[str, Any]
    ) -> None:
        """
        子节点执行前回调 - 收集 task.start 事件数据。

        Args:
            subplan: 子节点（二层节点）
            inputs: 输入参数
        """
        # 只有二层节点（depth=1）才发送 task 事件
        if subplan.depth != 1:
            logger.debug(
                "[SkillTurboExecutor] skip task events for depth=%d (not二层节点): plan_name=%s",
                subplan.depth,
                subplan.plan_name
            )
            return
        
        logger.debug(
            "[SkillTurboExecutor] _before_subplan_execute: plan_name=%s depth=%d",
            subplan.plan_name,
            subplan.depth,
        )
        
        events_queue = _task_events_queue_var.get()
        task_states = self._live_task_states()

        if events_queue is None or task_states is None:
            logger.debug(
                "[SkillTurboExecutor] _before_subplan_execute skip: no events_queue or task_states"
            )
            return

        task_id, task_state = self._get_or_create_task_state(subplan, task_states)
        timestamp = time.time()
        self._set_current_task_context(subplan, task_id, timestamp)

        # HITL resume 会从根节点重放 plan：已 completed 的 stage 跳过真实执行
        # （见 ``_should_skip_subplan_execute``），此处仅跳过 task.start UI，
        # 避免与快照里仍 in_progress 的中断 stage 同时“运行”。
        if task_state.get("status") == "completed":
            logger.info(
                "[SkillTurboExecutor] skip task.start for already-completed stage "
                "(resume replay): task_id=%s plan_name=%s",
                task_id,
                subplan.plan_name,
            )
            return

        # HITL 中断的 stage 在 resume 快照里仍为 in_progress。重放时若再次
        # 发 task.start，前端会看到重复的 start（日志中“3 次 start 1 次
        # complete”的根因）。跳过重复的 task.start 事件发送，但保留 current
        # task context，让执行继续走到中断的 tool_call_id 命中 resume input。
        if task_state.get("status") == "in_progress":
            logger.info(
                "[SkillTurboExecutor] skip duplicate task.start for in-progress stage "
                "(HITL resume replay): task_id=%s plan_name=%s",
                task_id,
                subplan.plan_name,
            )
            return

        # 兜底：启动较早 stage 时，把更靠后仍 in_progress 的 stage 收成 pending，
        # 保证右侧任务列表同一时刻只有一个 running。
        self._park_later_in_progress_tasks(task_state, task_states)

        self._update_task_state_on_start(task_state, timestamp)

        logger.debug(
            "[SkillTurboExecutor] task.start: task_id=%s task_name=%s status=in_progress depth=%d",
            task_id,
            subplan.plan_name,
            subplan.depth,
        )

        events_queue.append(
            self._build_task_start_event(subplan, task_id, task_state, task_states, timestamp)
        )
        await self._emit_task_update_event()

    async def _after_subplan_execute(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        result_or_error: Any,
    ) -> None:
        """
        子节点执行后回调 - 收集 task.complete 事件数据。

        Args:
            subplan: 子节点（二层节点）
            inputs: 输入参数
            result_or_error: 执行结果或异常对象
        """
        # 只有二层节点（depth=1）才发送 task 事件
        if subplan.depth != 1:
            logger.debug(
                "[SkillTurboExecutor] skip task events for depth=%d (not二层节点): plan_name=%s",
                subplan.depth,
                subplan.plan_name
            )
            return
        
        logger.debug(
            "[SkillTurboExecutor] _after_subplan_execute: plan_name=%s depth=%d",
            subplan.plan_name,
            subplan.depth,
        )
        
        task_context = _current_task_context_var.get()
        events_queue = _task_events_queue_var.get()
        task_states = self._live_task_states()

        if events_queue is None or task_context is None or task_states is None:
            logger.debug(
                "[SkillTurboExecutor] _after_subplan_execute skip: no events_queue or task_context or task_states"
            )
            return

        task_id = task_context["task_id"]
        timestamp = time.time()
        duration_ms = int((timestamp - task_context.get("start_time", time.time())) * 1000)
        is_error = isinstance(result_or_error, Exception) or _is_subplan_business_failure(
            result_or_error
        )
        status = "failed" if is_error else "completed"
        prior_status = (
            task_states[task_id].get("status") if task_id in task_states else None
        )

        # Resume 重放已完成 stage：before 已跳过 start，这里也跳过 complete/UI，
        # 仍收集产物并清理 context。
        if prior_status == "completed" and not is_error:
            logger.info(
                "[SkillTurboExecutor] skip task.complete for already-completed stage "
                "(resume replay): task_id=%s plan_name=%s",
                task_id,
                subplan.plan_name,
            )
            _current_task_context_var.set(None)
            self._current_task_id_holder["task_id"] = None
            current_task_holder = _current_task_holder_var.get()
            if current_task_holder is not None:
                current_task_holder["task_id"] = None
            self._collect_node_artifact(subplan, result_or_error, task_id, timestamp, is_error)
            return

        logger.debug(
            "[SkillTurboExecutor] task.complete: task_id=%s task_name=%s status=%s duration_ms=%d depth=%d",
            task_id,
            subplan.plan_name,
            status,
            duration_ms,
            subplan.depth,
        )

        if task_id in task_states:
            self._update_task_state_on_complete(
                task_states[task_id],
                status,
                timestamp,
                duration_ms,
                result_or_error if is_error else None,
            )

        events_queue.append(
            self._build_task_complete_event(
                TaskCompleteEventData(
                    subplan=subplan,
                    task_id=task_id,
                    status=status,
                    timestamp=timestamp,
                    duration_ms=duration_ms,
                    error=result_or_error if is_error else None,
                )
            )
        )
        await self._emit_task_update_event()
        _current_task_context_var.set(None)
        # 同时清空实例属性（跨协程共享）
        self._current_task_id_holder["task_id"] = None
        current_task_holder = _current_task_holder_var.get()
        if current_task_holder is not None:
            current_task_holder["task_id"] = None

        self._collect_node_artifact(subplan, result_or_error, task_id, timestamp, is_error)

    def _collect_node_artifact(
        self,
        subplan: PlanNode,
        result_or_error: Any,
        task_id: str,
        timestamp: float,
        is_error: bool,
    ) -> None:
        """节点产物收集。

        成功路径：仅当节点声明了 __artifact__ 时记录。
        异常路径：仍需节点在异常对象上挂 _partial_artifact 才记录。
        此处只更新内存 holder，落盘由流末 finally 统一完成。
        读完立即从 result 中清除（pop）。
        """
        if is_error:
            # 异常路径：仅记录带 partial artifact 的中断
            artifact = getattr(result_or_error, "_partial_artifact", None)
            if not isinstance(artifact, dict):
                return
            node_status = "interrupted"
            info = artifact.get("info") if isinstance(artifact.get("info"), dict) else {}
            files = artifact.get("files") if isinstance(artifact.get("files"), list) else []
        else:
            # 成功路径：仅记录带 __artifact__ 的节点
            artifact = None
            if isinstance(result_or_error, dict):
                artifact = result_or_error.pop("__artifact__", None)
            if not isinstance(artifact, dict):
                return
            node_status = "completed"
            info = artifact.get("info") if isinstance(artifact.get("info"), dict) else {}
            files = artifact.get("files") if isinstance(artifact.get("files"), list) else []
        artifact_entry: dict[str, Any] = {
            "task_id": task_id,
            "status": node_status,
            "info": info,
            "files": files,
            "finished_at": timestamp,
        }
        delivery_summary = artifact.get("delivery_summary")
        if isinstance(delivery_summary, str) and delivery_summary.strip():
            artifact_entry["delivery_summary"] = delivery_summary.strip()
        self._node_artifacts_holder[subplan.plan_name] = artifact_entry
        logger.debug(
            "[SkillTurboExecutor] node_artifact collected: plan_name=%s status=%s has_artifact=%s",
            subplan.plan_name,
            node_status,
            bool(info or files),
        )

    def _get_or_create_task_state(
        self,
        subplan: PlanNode,
        task_states: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """优先复用预置任务；兜底支持运行时出现的动态子节点。"""
        task = self._find_task_state_by_plan_name(subplan.plan_name, task_states)
        if task is not None:
            return task

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task_state = {
            "task_id": task_id,
            "task_content": self._display_name(subplan.plan_name),
            "task_index": len(task_states),
            "source": "skill_turbo",
            "status": "pending",
        }
        task_states[task_id] = task_state
        # 同时写入实例属性（跨协程共享）
        self._task_states_holder[task_id] = task_state
        logger.debug(
            "[SkillTurboExecutor] dynamic task added: task_id=%s task_name=%s",
            task_id,
            subplan.plan_name,
        )
        return task_id, task_state

    def _find_task_state_by_plan_name(
        self,
        plan_name: str,
        task_states: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        display = self._display_name(plan_name)
        for task_id, state in task_states.items():
            if state.get("task_content") == display:
                return task_id, state
        return None

    def _set_current_task_context(
        self,
        subplan: PlanNode,
        task_id: str,
        timestamp: float,
    ) -> None:
        _current_task_context_var.set(
            {
                "task_id": task_id,
                "task_name": self._display_name(subplan.plan_name),
                "depth": subplan.depth,
                "start_time": timestamp,
            }
        )
        # 同时更新实例属性（跨协程共享）
        self._current_task_id_holder["task_id"] = task_id
        current_task_holder = _current_task_holder_var.get()
        if current_task_holder is not None:
            current_task_holder["task_id"] = task_id

    @staticmethod
    def _park_later_in_progress_tasks(
        current: dict[str, Any],
        task_states: dict[str, dict[str, Any]],
    ) -> None:
        """把 index 更大且仍 in_progress 的 stage 收成 pending。"""
        cur_idx = current.get("task_index")
        if not isinstance(cur_idx, int):
            return
        for other in task_states.values():
            if other is current:
                continue
            if other.get("status") != "in_progress":
                continue
            other_idx = other.get("task_index")
            if isinstance(other_idx, int) and other_idx > cur_idx:
                other["status"] = "pending"
                other.pop("start_time", None)

    @staticmethod
    def _update_task_state_on_start(
        task_state: dict[str, Any],
        timestamp: float,
    ) -> None:
        task_state["status"] = "in_progress"
        task_state["start_time"] = timestamp

    @staticmethod
    def _update_task_state_on_complete(
        task_state: dict[str, Any],
        status: str,
        timestamp: float,
        duration_ms: int,
        error: Any | None,
    ) -> None:
        task_state["status"] = status
        task_state["end_time"] = timestamp
        task_state["duration_ms"] = duration_ms
        if error is not None:
            task_state["error"] = str(error)

    def _build_task_start_event(
        self,
        subplan: PlanNode,
        task_id: str,
        task_state: dict[str, Any],
        task_states: dict[str, dict[str, Any]],
        timestamp: float,
    ) -> dict[str, Any]:
        request_id = _request_id_var.get()
        return {
            "request_id": request_id,
            "channel_id": _channel_id_var.get(),
            "payload": {
                "task_id": task_id,
                "task_content": self._display_name(subplan.plan_name),
                "task_index": task_state.get("task_index", 0),
                "total_tasks": len(task_states),
                "parent_request_id": request_id,
                "timestamp": timestamp,
                "source": "skill_turbo",
            },
            "is_complete": False,
        }

    def _build_task_complete_event(
        self,
        data: TaskCompleteEventData,
    ) -> dict[str, Any]:
        event = {
            "request_id": _request_id_var.get(),
            "channel_id": _channel_id_var.get(),
            "payload": {
                "task_id": data.task_id,
                "task_content": self._display_name(data.subplan.plan_name),
                "status": data.status,
                "duration_ms": data.duration_ms,
                "timestamp": data.timestamp,
            },
            "is_complete": False,
        }
        if data.error is not None:
            event["payload"]["error"] = str(data.error)
        return event

    def _prepare_root_node(self, plan_code: str) -> PlanNode:
        """统一非流式/流式入口的 plan_code 加载流程，确保安全校验只维护一处。"""
        errors = self._validator.validate(plan_code)
        if errors:
            logger.warning("[SkillTurboExecutor] validation failed errors=%s", errors)
            raise PlanCodeValidationError(errors)

        namespace = self._load_plan_namespace(plan_code)
        root = self._extract_root_node(namespace)
        # 深拷贝:plan_code 通过 `from {module} import root` 导入的是模块级单例,
        # 多个并发请求共享同一棵节点树。_bind_node_callbacks 会递归覆盖回调为
        # 当前 executor 的绑定方法,若不拷贝,后启动的请求会覆盖先启动请求的回调,
        # 导致先启动请求的节点产物(_after_subplan_execute)写入错误 executor 的 holder。
        root = copy.deepcopy(root)
        self._bind_node_callbacks(root)
        # 从 root 节点读取 skill 提供的显示名映射（可选，缺省为空 dict）。
        self._display_names = getattr(root, "display_names", None) or {}
        return root

    def _load_plan_namespace(self, plan_code: str) -> dict[str, Any]:
        self._ensure_skill_code_import_path()
        namespace = self._build_namespace()
        try:
            exec(plan_code, namespace)  # noqa: S102
        except Exception as e:
            logger.error("[SkillTurboExecutor] plan code load failed: %s", e, exc_info=True)
            raise PlanCodeLoadError(f"规划代码执行失败: {e}") from e
        return namespace

    # fromlist 中禁止导入的名称：这些名称可能被用于获取危险内建对象
    _DENIED_FROMLIST_NAMES: frozenset[str] = frozenset({
        "__import__", "__builtins__", "__build_class__",
        "exec", "eval", "compile", "open", "globals", "locals",
        "vars", "dir", "getattr", "setattr", "delattr", "type",
    })

    def _safe_import(
        self,
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level:
            raise ImportError("SkillTurbo plan_code 禁止相对 import")
        if not any(name.startswith(prefix) for prefix in self._env.skill_code_import_prefixes):
            raise ImportError(f"SkillTurbo plan_code 禁止 import: {name}")
        # 校验 fromlist：防止 from skill_codes.xxx import __import__ 等绕过
        for item_name in fromlist:
            if item_name in self._DENIED_FROMLIST_NAMES:
                raise ImportError(
                    f"SkillTurbo plan_code 禁止从 {name} 导入: {item_name}"
                )
        # 使用 importlib.import_module 替代 __import__（G.IMP.03）
        # 复刻 __import__ 语义：fromlist 非空时返回 name 指定的模块；
        # fromlist 为空时返回顶层包（即 name 的第一段）。
        module = importlib.import_module(name)
        if fromlist:
            # 确保子模块属性可访问（from x.y import a 语义）
            for item_name in fromlist:
                if not hasattr(module, item_name):
                    try:
                        importlib.import_module(f"{name}.{item_name}")
                    except ImportError:
                        # 非子模块（普通属性）时忽略，与 __import__ 行为一致
                        pass
            return module
        # fromlist 为空：import x.y 返回顶层包 x
        top = name.split(".")[0]
        return sys.modules[top] if "." in name else module

    def _build_namespace(self) -> dict[str, Any]:
        """
        构建受限执行命名空间。

        安全措施：
        1. 清空 __builtins__，只暴露白名单内置函数
        2. 移除 type, isinstance 等可能被滥用的函数
        
        注意：
        - plan_code 定义 PlanNode 子类，在 _execute 中通过 self.xxx 调用方法
        - use_tool、call_llm、stream_llm、extract_json 都是 PlanNode 的方法
        - 不在命名空间中直接暴露这些函数，保持API一致性
        """
        builtins = dict(_SAFE_BUILTINS)
        builtins["__import__"] = self._safe_import
        return {
            "__builtins__": builtins,
            "PlanNode": PlanNode,
        }

    @staticmethod
    def _extract_root_node(namespace: dict[str, Any]) -> PlanNode:
        """从命名空间提取根 PlanNode 实例。"""
        root = namespace.get("root")
        if isinstance(root, PlanNode):
            return root

        # 回退：未找到显式 root 变量时，取命名空间中最后一个 PlanNode 实例。
        # 这是一种脆弱的兜底，建议 plan_code 显式赋值 root = PlanNode(...)。
        last_node: PlanNode | None = None
        plan_node_count = 0
        for val in namespace.values():
            if isinstance(val, PlanNode):
                last_node = val
                plan_node_count += 1

        if last_node is None:
            raise ValueError("规划代码未生成 PlanNode 实例")
        if plan_node_count > 1:
            raise PlanCodeValidationError(
                [
                    f"规划代码定义了 {plan_node_count} 个 PlanNode 实例但未显式赋值 root 变量，"
                    "请在 plan_code 末尾添加: root = PlanNode(...)"
                ]
            )
        return last_node

    def _bind_node_callbacks(self, root: PlanNode) -> None:
        root.set_runtime_callbacks(
            has_tool=self.has_tool,
            use_tool=self.use_tool,
            call_llm=self.call_llm,
            stream_llm=self.stream_llm,
            fallback=self.fallback,
            fallback_stream=self.fallback_stream,
            extract_json=extract_llm_json,
            log=self._log_from_node,
            before_subplan_execute=self._before_subplan_execute,
            after_subplan_execute=self._after_subplan_execute,
            should_skip_subplan_execute=self._should_skip_subplan_execute,
            should_suppress_subplan_start_banner=self._should_suppress_subplan_start_banner,
        )

    @staticmethod
    def _log_from_node(
        node: PlanNode,
        level: str,
        message: str,
        args: tuple[Any, ...],
    ) -> None:
        """输出节点受控日志。"""
        log_level = level.lower()
        if log_level not in {"debug", "info", "warning", "error"}:
            log_level = "info"
        log_fn = getattr(logger, log_level)
        try:
            message_text = message % args if args else message
        except Exception:
            message_text = f"{message} args={args!r}"
        session = _session_var.get()
        session_id = getattr(session, "id", "") if session is not None else ""
        log_fn(
            "[PlanNodeLog] request_id=%s session_id=%s node=%s message=%s",
            _request_id_var.get(),
            session_id,
            node.plan_name,
            message_text,
        )

    def _ensure_skill_code_import_path(self) -> None:
        """确保 skill_code 导入路径在 sys.path 中。"""
        parent = self._env.skill_codes_parent_dir
        if not parent:
            return
        # 规范化路径后再去重，避免不同字符串形式（如尾斜杠）导致重复插入
        normalized = str(parent)
        if normalized not in sys.path:
            sys.path.append(normalized)
            logger.debug("[SkillTurboExecutor] added to sys.path: %s", normalized)

    @staticmethod
    def _hash_code(code: str) -> str:
        """计算代码哈希（用于追踪）。"""
        return hashlib.md5(code.encode()).hexdigest()[:8]

    async def _send_trace(self) -> None:
        """发送执行追踪数据到 Evolver。"""
        if self._trace_collector and self._current_trace:
            try:
                # 这里调用 Evolver 的接口发送追踪数据
                # 具体实现取决于 Evolver 的接口设计
                logger.debug(
                    "[SkillTurboExecutor] sending trace to evolver: fallback_count=%d",
                    self._current_trace.fallback_count,
                )
            except Exception as e:
                logger.warning("[SkillTurboExecutor] failed to send trace: %s", e)
