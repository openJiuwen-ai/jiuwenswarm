# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenClaw Deep Adapter - 基于 openjiuwen DeepAgent 的适配器实现.

此模块实现 AgentAdapter 协议，封装 Deep SDK 的所有专属逻辑。
公共编排逻辑（session 队列、Skills 路由、heartbeat 等）由 Facade 层处理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import importlib
import os
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from typing import Any, AsyncIterator, Callable, List, Self, Tuple
from sqlalchemy import text

from dotenv import load_dotenv
try:
    from openjiuwen.core.context_engine.active_skill_bodies import (
        DEFAULT_MAX_ACTIVE_SKILL_BODIES,
    )
    _UPSTREAM_HAS_ACTIVE_SKILL_BODIES = True
except ImportError:
    # Fallback for upstream openjiuwen versions without active_skill_bodies.
    # Remove once agent-core enterprise-dev includes the module.
    DEFAULT_MAX_ACTIVE_SKILL_BODIES = 1
    _UPSTREAM_HAS_ACTIVE_SKILL_BODIES = False
from openjiuwen.core.context_engine.context.session_memory_manager import SessionMemoryConfig
from openjiuwen.core.context_engine.schema.config import ContextEngineConfig
from openjiuwen.core.foundation.llm import ModelRequestConfig, ModelClientConfig, Model
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import AgentCard, ReActAgentConfig, create_agent_session
from openjiuwen.core.sys_operation import (
    SysOperation,
)
from openjiuwen.harness import (
    AudioModelConfig,
    DeepAgent,
    DeepAgentConfig,
    VisionModelConfig,
)
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.subagents.code_agent import create_code_agent
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import SkillUseRail, TaskPlanningRail, SecurityRail, SkillEvolutionRail
from openjiuwen.harness.rails.subagent_rail import SubagentRail
from openjiuwen.harness.rails.lsp_rail import LspRail
from openjiuwen.harness.rails.context_engineering_rail import ContextEngineeringRail
from openjiuwen.harness.rails.filesystem_rail import FileSystemRail
from openjiuwen.harness.rails.heartbeat_rail import HeartbeatRail
from openjiuwen.agent_evolving.signal import SignalDetector
from openjiuwen.harness.rails.memory_rail import MemoryRail
from openjiuwen.harness.rails.coding_memory_rail import CodingMemoryRail
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.code_agent import build_code_agent_config
from openjiuwen.harness.subagents.research_agent import build_research_agent_config
from openjiuwen.harness.tools import (
    WebPaidSearchTool,
    create_audio_tools,
    create_vision_tools,
)
from openjiuwen.harness.tools.todo import TodoStatus, TodoModifyTool
from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode
from jiuwenclaw.agentserver.deep_agent.rails.concurrent_safe_rails import (
    ConcurrentSafeFileSystemRail,
    ConcurrentSafeTaskPlanningRail,
)
from jiuwenclaw.agentserver.deep_agent.cron_runtime import CronRuntimeBridge
from jiuwenclaw.agentserver.deep_agent.ask_user_question_registry import (
    ASK_REQUEST_PREFIX,
    AskUserQuestionRegistry,
    ask_user_question_request_scope,
)
from jiuwenclaw.agentserver.llm_io_trace import (
    log_chat_final,
    log_invoke_input,
    log_invoke_output,
    log_reasoning_delta,
    log_stream_input,
    log_stream_output,
)
from jiuwenclaw.agentserver.deep_agent.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)
from jiuwenclaw.agentserver.deep_agent.prompt_builder import build_identity_prompt
from jiuwenclaw.agentserver.deep_agent.rails import (
    JiuClawContextEngineeringRail,
    JiuClawStreamEventRail,
    ResponsePromptRail,
    RuntimePromptRail,
    SkillComplianceRail,
    SkillProtocolPromptRail,
    TaskExecutionRail,
)
from jiuwenclaw.agentserver.deep_agent.rails.disabled_tools_rail import DisabledToolsRail
from jiuwenclaw.agentserver.deep_agent.rails.permission_rail import clear_session_interrupt_state
from jiuwenclaw.agentserver.deep_agent.rails.task_execution_rail import get_current_task_id
from jiuwenclaw.agentserver.deep_agent.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    setup_permission_context,
    cleanup_permission_context,
)
from jiuwenclaw.agentserver.permissions.core import init_permission_engine
from jiuwenclaw.agentserver.memory import clear_memory_manager_cache
from jiuwenclaw.agentserver.memory.config import (clear_config_cache, get_memory_mode, is_memory_enabled,
                                                  is_proactive_memory, clear_embed_config_db_cache)
from jiuwenclaw.agentserver.permissions.checker import TOOL_PERMISSION_CHANNEL_ID
from jiuwenclaw.agentserver.permissions.config_loader import (
    reset_permissions_session_scope,
    setup_permissions_session_scope,
)
from jiuwenclaw.agentserver.permissions.skill_authorization.subagent_approval_registry import (
    SubagentApprovalKind,
)
from jiuwenclaw.agentserver.cron_config import should_register_cron_tools
from jiuwenclaw.agentserver.skill_manager import SkillManager
from jiuwenclaw.agentserver.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    dedicated_multimodal_model_configured,
)
from jiuwenclaw.agentserver.tools.video_tools import video_understanding
from jiuwenclaw.agentserver.tools.harness_named_web_tools import build_jiuwen_harness_named_web_tools
from jiuwenclaw.agentserver.tool_registration import (
    ensure_tool_registered as _ensure_tool_registered,
)

from jiuwenclaw.agentserver.tools import SendFileToolkit, SkillToolkit
from jiuwenclaw.agentserver.tools.ask_user_question_tool import get_ask_user_question_tool
from jiuwenclaw.agentserver.tools.acp_output_tools import get_tools as get_acp_output_tools
from jiuwenclaw.agentserver.tools.acp_output_tools import get_acp_output_manager
from jiuwenclaw.agentserver.tools.deepresearch_tools import (
    push_deepresearch_route,
    reset_deepresearch_route,
    get_deepresearch_tools,
)
from jiuwenclaw.agentserver.tools.petal_search_tools import enable_petal_search, mcp_petal_search
from jiuwenclaw.agentserver.tools.multi_session_toolkits import MultiSessionToolkit
from jiuwenclaw.agentserver.memory.external_memory_config import is_builtin_memory_allowed
from jiuwenclaw.agentserver.tools.xiaoyi_phone_tools import (
    get_user_location,
    create_note,
    search_notes,
    modify_note,
    create_calendar_event,
    search_calendar_event,
    search_contact,
    search_photo_gallery,
    upload_photo,
    search_file,
    upload_file,
    call_phone,
    send_message,
    search_message,
    create_alarm,
    search_alarms,
    modify_alarm,
    delete_alarm,
    query_collection,
    add_collection,
    delete_collection,
    save_media_to_gallery,
    save_file_to_file_manager,
    convert_timestamp_to_utc8_time,
    view_push_result,
    xiaoyi_gui_agent,
    image_reading,
)
from jiuwenclaw.config import (
    get_config,
    get_default_models,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    resolve_env_vars,
)
from jiuwenclaw.agentserver.deep_agent.sysop_builder import (
    create_local_sysop_card,
    create_sandbox_sysop_card,
)
from jiuwenclaw.agentserver.stream_content_sanitize import strip_inline_tool_protocol
from jiuwenclaw.agentserver.stream_utils import tool_calls_payload_to_json_list
from jiuwenclaw.agentserver.extensions import get_rail_manager
from jiuwenclaw.agentserver.tools.cron_tool_context import (
    CRON_TOOL_CHANNEL_ID,
    CRON_TOOL_METADATA,
    CRON_TOOL_MODE,
    CRON_TOOL_SESSION_ID,
    get_cron_tool_channel_id,
    get_cron_tool_metadata,
    get_cron_tool_mode,
    get_cron_tool_session_id,
)
from jiuwenclaw.gateway.cron import CronTargetChannel
from jiuwenclaw.agentserver.team import get_team_manager
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.agentserver.skill_whitelist import (is_skill_whitelist_tenant, parse_agent_skill_whitelist,
                                                     SkillWhitelistSynchronizer)
from jiuwenclaw.utils import (
    get_agent_registered_skill_dirs,
    get_checkpoint_dir,
    get_env_file,
    get_agent_root_dir,
)
from jiuwenclaw.local_env_config import set_local_config

load_dotenv(dotenv_path=get_env_file())

_react_config = get_config().get("react", {})

_LLM_TRACE_SESSION_ID: ContextVar[str] = ContextVar(
    "llm_trace_session_id",
    default="",
)
_LLM_TRACE_REQUEST_ID: ContextVar[str] = ContextVar(
    "llm_trace_request_id",
    default="",
)
_LLM_TRACE_ITERATION: ContextVar[int | None] = ContextVar(
    "llm_trace_iteration",
    default=None,
)
_LLM_TRACE_MODEL_NAME: ContextVar[str] = ContextVar(
    "llm_trace_model_name",
    default="",
)

_REASONING_TRACE_LOG_BATCH = 5
_LLM_IO_TRACE_PATCH_APPLIED = False

logger = logging.getLogger(__name__)

_ACP_BLOCKED_DEFAULT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "code",
    }
)

#: 子 Agent 委托审批卡的 source 集合，从审批类型枚举派生，新增类型自动同步。
_SUBAGENT_APPROVAL_SOURCES = frozenset(
    f"subagent_{kind.value}"
    for kind in SubagentApprovalKind
)


@dataclass(slots=True)
class _RuntimeConfigParams:
    """`_update_runtime_config` 的具名入参封装（会话、模式与请求级上下文）。"""

    session_id: str | None
    mode: str = "agent.plan"
    request_id: str | None = None
    channel_id: str | None = None
    request_metadata: dict[str, Any] | None = None
    request_system_prompt: str | None = None

    @classmethod
    def from_agent_request(cls, request: AgentRequest, mode: str) -> Self:
        return cls(
            session_id=request.session_id,
            mode=mode,
            request_id=request.request_id,
            channel_id=request.channel_id,
            request_metadata=request.metadata,
            request_system_prompt=request.params.get("system_prompt"),
        )


def _parse_int(value: Any, default: int) -> int:
    """Parse integer-like values safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_iteration_from_obj(value: Any) -> int | None:
    """Best-effort parse iteration from chunk/payload/dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("iteration", "iter", "step", "round"):
            if key in value:
                parsed = _extract_iteration_from_obj(value.get(key))
                if parsed is not None:
                    return parsed
        for key in ("metadata", "meta", "extra", "context"):
            nested = value.get(key)
            if isinstance(nested, dict):
                parsed = _extract_iteration_from_obj(nested)
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
        return None
    return None


def _extract_iteration_from_chunk(chunk: Any) -> int | None:
    """Extract iteration from stream chunk object."""
    for attr in ("iteration", "iter", "step"):
        if hasattr(chunk, attr):
            parsed = _extract_iteration_from_obj(getattr(chunk, attr, None))
            if parsed is not None:
                return parsed
    payload = getattr(chunk, "payload", None)
    return _extract_iteration_from_obj(payload)


def _apply_llm_io_trace_patch() -> None:
    """Monkey Patch Model.invoke/stream 添加 LLM IO trace 日志."""
    global _LLM_IO_TRACE_PATCH_APPLIED
    if _LLM_IO_TRACE_PATCH_APPLIED:
        return

    try:
        original_invoke = Model.invoke
        original_stream = Model.stream

        async def _traced_invoke(
            self: Model,
            messages: List[Any],
            tools: List[Any] | None = None,
            model: str | None = None,
            **kwargs: Any,
        ) -> Any:
            model_name = model or getattr(self.model_config, "model_name", "") or ""
            trace_sid = _LLM_TRACE_SESSION_ID.get()
            trace_rid = _LLM_TRACE_REQUEST_ID.get()
            trace_iter = _LLM_TRACE_ITERATION.get()
            resolved_iter = (
                _extract_iteration_from_obj(kwargs) if trace_iter is None else trace_iter
            )
            log_invoke_input(
                session_id=trace_sid,
                request_id=trace_rid,
                iteration=resolved_iter,
                model_name=model_name,
                messages=messages,
                tools=tools,
                max_tokens=kwargs.get("max_tokens"),
                temperature=kwargs.get("temperature"),
                top_p=kwargs.get("top_p"),
                stop=kwargs.get("stop"),
                timeout=kwargs.get("timeout"),
            )
            result = await original_invoke(
                self, messages, tools=tools, model=model, **kwargs
            )
            log_invoke_output(
                session_id=trace_sid,
                request_id=trace_rid,
                iteration=resolved_iter,
                model_name=model_name,
                assistant_msg=result,
            )
            return result

        async def _traced_stream(
            self: Model,
            messages: List[Any],
            tools: List[Any] | None = None,
            model: str | None = None,
            **kwargs: Any,
        ) -> Any:
            model_name = model or getattr(self.model_config, "model_name", "") or ""
            trace_sid = _LLM_TRACE_SESSION_ID.get()
            trace_rid = _LLM_TRACE_REQUEST_ID.get()
            trace_iter = _LLM_TRACE_ITERATION.get()
            resolved_iter = (
                _extract_iteration_from_obj(kwargs) if trace_iter is None else trace_iter
            )
            log_stream_input(
                session_id=trace_sid,
                request_id=trace_rid,
                iteration=resolved_iter,
                model_name=model_name,
                messages=messages,
                tools=tools,
                max_tokens=kwargs.get("max_tokens"),
                temperature=kwargs.get("temperature"),
                top_p=kwargs.get("top_p"),
                stop=kwargs.get("stop"),
                timeout=kwargs.get("timeout"),
            )
            accumulated: Any = None
            reasoning_seq = 0
            reasoning_trace_pending: List[Tuple[int, str]] = []

            def emit_reasoning_trace_batch() -> None:
                if not reasoning_trace_pending:
                    return
                log_reasoning_delta(
                    session_id=trace_sid,
                    request_id=trace_rid,
                    iteration=trace_iter,
                    model_name=model_name,
                    reasoning_seq=reasoning_trace_pending[0][0],
                    fragment="".join(t[1] for t in reasoning_trace_pending),
                )
                reasoning_trace_pending.clear()

            try:
                async for chunk in original_stream(
                    self, messages, tools=tools, model=model, **kwargs
                ):
                    if accumulated is None:
                        accumulated = chunk
                    else:
                        try:
                            accumulated = accumulated + chunk
                        except Exception:
                            accumulated = chunk

                    reasoning_content = (
                        getattr(chunk, "reasoning_content", None)
                        or (
                            chunk.get("reasoning_content")
                            if isinstance(chunk, dict)
                            else None
                        )
                        or (
                            (chunk.payload.get("reasoning_content") or chunk.payload.get("reasoning"))
                            if isinstance(getattr(chunk, "payload", None), dict)
                            else None
                        )
                    )
                    if reasoning_content:
                        reasoning_trace_pending.append(
                            (reasoning_seq, str(reasoning_content))
                        )
                        if len(reasoning_trace_pending) >= _REASONING_TRACE_LOG_BATCH:
                            emit_reasoning_trace_batch()
                        reasoning_seq += 1

                    yield chunk

                emit_reasoning_trace_batch()

                if accumulated:
                    log_stream_output(
                        session_id=trace_sid,
                        request_id=trace_rid,
                        iteration=resolved_iter,
                        model_name=model_name,
                        assistant_msg=accumulated,
                    )
            except Exception:
                emit_reasoning_trace_batch()
                raise

        Model.invoke = _traced_invoke
        Model.stream = _traced_stream
        _LLM_IO_TRACE_PATCH_APPLIED = True
        logger.info("[JiuWenClawDeepAdapter] LLM IO trace patch applied")
    except Exception:
        logger.warning(
            "[JiuWenClawDeepAdapter] Failed to apply LLM IO trace patch", exc_info=True
        )


def _deep_agent_context_engine_config(react_cfg: dict[str, Any] | None) -> ContextEngineConfig:
    """供 ``create_deep_agent(..., context_engine_config=...)`` 使用（与 agent-core 集成测试方法二一致）。

    从 ``react.context_engine_config`` 合并与 ``ContextEngineConfig`` 同名的顶层字段（若 yaml 中出现），
    例如 ``enable_kv_cache_release``、``enable_reload``、``default_window_round_num`` 等；
    未出现的键保持 ``ReActAgentConfig`` 内置默认。
    """
    base = ReActAgentConfig().context_engine_config
    react_cfg = react_cfg or {}
    cec = react_cfg.get("context_engine_config")
    if not isinstance(cec, dict):
        return base
    cec_toplevel_keys = [
        "enable_kv_cache_release",
        "enable_reload",
        "enable_reload_prompt",
        "max_context_message_num",
        "default_window_message_num",
        "default_window_round_num",
        "active_skill_pin_target",
    ]
    if _UPSTREAM_HAS_ACTIVE_SKILL_BODIES:
        cec_toplevel_keys.append("max_active_skill_bodies")
    updates = {k: cec[k] for k in cec_toplevel_keys if k in cec}
    if not updates:
        return base
    return base.model_copy(update=updates)


# react.context_engine_config 键 -> openjiuwen _merge_processors 注册的处理器类名（预置链 B）
_CHAIN_B_OPTIONAL_PROCESSORS: Tuple[Tuple[str, str], ...] = (
    ("tool_result_budget_processor_config", "ToolResultBudgetProcessor"),
    ("micro_compact_processor_config", "MicroCompactProcessor"),
    ("full_compact_processor_config", "FullCompactProcessor"),
)


def _resolve_session_memory_for_context_rail(context_engine_cfg: dict[str, Any]) -> Any | None:
    """解析 ``react.context_engine_config.session_memory``，对应 openjiuwen 预置链 A / B。

    - 缺省或 ``null``：默认 **预置链 B**（SessionMemory + ToolResultBudget / MicroCompact / FullCompact）。
    - ``false``：显式 **预置链 A**（四类摘要/压缩处理器），并与 yaml 中 *_config 合并。
    - ``dict``：``SessionMemoryConfig.model_validate``；``{}`` 表示默认 SessionMemory 参数。
    """
    if "session_memory" not in context_engine_cfg:
        return SessionMemoryConfig()
    raw = context_engine_cfg["session_memory"]
    if raw is False:
        return None
    if raw is None:
        return SessionMemoryConfig()
    if isinstance(raw, dict):
        return SessionMemoryConfig.model_validate(raw)
    return raw


def _build_context_engineering_rail(config: dict[str, Any],
                                    mode: str = "agent.fast",
                                    minimal: bool = False) -> ContextEngineeringRail | None:
    """Build ContextEngineeringRail with user config merged into presets.

    用户提供的 processor 配置（dict 格式）会与预置配置做字段级别合并，
    只覆盖用户指定的字段，其他使用预置默认值。

    预置链 B 可选键（``react.context_engine_config``）：
    ``tool_result_budget_processor_config`` / ``micro_compact_processor_config`` /
    ``full_compact_processor_config``；Session Memory 笔记节奏见 ``session_memory``。

    Args:
        config: 配置字典
        mode: 模式，agent.plan 模式使用 preset=True 和 processors，其他模式使用 preset=False 和 processors=None
        minimal: F-REDUCE — 跳过 tools/context section 注入（用于 spawn/fork subagent）
    """
    try:
        if mode == "agent.plan":
            context_engine_cfg = config.get("context_engine_config", {})
            session_memory = _resolve_session_memory_for_context_rail(context_engine_cfg)

            user_processors: List[Tuple[str, dict]] = []
            if session_memory is None:
                # 预置链 A：四类处理器与用户 yaml 字段级合并
                offloader_cfg = context_engine_cfg.get("message_summary_offloader_config", {})
                if isinstance(offloader_cfg, dict) and offloader_cfg:
                    user_processors.append(("MessageSummaryOffloader", offloader_cfg))

                compressor_cfg = context_engine_cfg.get("dialogue_compressor_config", {})
                if isinstance(compressor_cfg, dict) and compressor_cfg:
                    user_processors.append(("DialogueCompressor", compressor_cfg))

                current_round_cfg = context_engine_cfg.get("current_round_compressor_config", {})
                if isinstance(current_round_cfg, dict) and current_round_cfg:
                    user_processors.append(("CurrentRoundCompressor", current_round_cfg))

                round_level_cfg = context_engine_cfg.get("round_level_compressor_config", {})
                if isinstance(round_level_cfg, dict) and round_level_cfg:
                    user_processors.append(("RoundLevelCompressor", round_level_cfg))
            else:
                # 预置链 B：三类处理器与用户 yaml 字段级合并（仅非空 dict 参与）
                for yaml_key, processor_name in _CHAIN_B_OPTIONAL_PROCESSORS:
                    proc_cfg = context_engine_cfg.get(yaml_key, {})
                    if isinstance(proc_cfg, dict) and proc_cfg:
                        user_processors.append((processor_name, proc_cfg))

            context_rail = JiuClawContextEngineeringRail(
                processors=user_processors if user_processors else None,
                preset=True,
                minimal=minimal,
                session_memory=session_memory,
            )
            chain = "B" if session_memory is not None else "A"
            logger.info(
                "[JiuWenClawDeepAdapter] JiuClawContextEngineeringRail create success for agent.plan mode, "
                "preset_chain=%s minimal=%s user_processors=%s",
                chain,
                minimal,
                [p[0] for p in user_processors] if user_processors else "none",
            )
        else:
            context_rail = JiuClawContextEngineeringRail(
                processors=None,
                preset=False,
                minimal=minimal,
            )
            logger.info(
                "[JiuWenClawDeepAdapter] JiuClawContextEngineeringRail create success for %s mode, "
                "preset=False minimal=%s",
                mode,
                minimal,
            )
        return context_rail
    except Exception as exc:
        logger.warning("[JiuWenClawDeepAdapter] ContextEngineeringRail create failed: %s", exc)
        return None


def _patch_compiler_for_on_conflict():
    """使 MySQL 和 PostgreSQL SQLAlchemy 编译器支持 SQLite 的 ON CONFLICT DO UPDATE.

    openjiuwen SDK 的 DbBasedKVStore 硬编码了 SQLite upsert 语法.
    此 patch 在 SQL 编译阶段将 ON CONFLICT ... DO UPDATE 翻译为对应数据库的语法,
    使 checkpoint 可以正常写入 MySQL/PostgreSQL.
    """
    try:
        from sqlalchemy.ext.compiler import compiles
        from sqlalchemy.dialects.sqlite.dml import OnConflictDoUpdate

        @compiles(OnConflictDoUpdate, "mysql")
        def _mysql_on_conflict_do_update(element, compiler, **kw):
            values = getattr(element, "_update_values", None)
            set_pairs = []
            if isinstance(values, dict):
                for col_key in values:
                    col_name = compiler.preparer.format_column(col_key)
                    set_pairs.append(f"{col_name} = VALUES({col_name})")
            if not set_pairs:
                set_pairs.append("value = VALUES(value)")
            return f"\nON DUPLICATE KEY UPDATE {', '.join(set_pairs)}"

        @compiles(OnConflictDoUpdate, "postgresql")
        def _postgresql_on_conflict_do_update(element, compiler, **kw):
            values = getattr(element, "_update_values", None)
            set_pairs = []
            if isinstance(values, dict):
                for col_key in values:
                    col_name = compiler.preparer.format_column(col_key)
                    set_pairs.append(f"{col_name} = EXCLUDED.{col_name}")
            if not set_pairs:
                set_pairs.append("value = EXCLUDED.value")
            return f" ON CONFLICT (key) DO UPDATE SET {', '.join(set_pairs)}"
    except Exception:
        pass


_patch_compiler_for_on_conflict()

_checkpoint_singleton_lock: asyncio.Lock | None = None
_shared_checkpoint_checkpointer: Any = None


def _get_checkpoint_singleton_lock() -> asyncio.Lock:
    global _checkpoint_singleton_lock
    if _checkpoint_singleton_lock is None:
        _checkpoint_singleton_lock = asyncio.Lock()
    return _checkpoint_singleton_lock


def reset_shared_checkpoint_for_tests() -> None:
    """Reset process-wide checkpoint singleton (tests only)."""
    global _shared_checkpoint_checkpointer, _checkpoint_singleton_lock
    _shared_checkpoint_checkpointer = None
    _checkpoint_singleton_lock = None


async def _get_shared_gateway_db_engine():
    """复用 GatewayDb 单例的 AsyncEngine（不新建连接池）。"""
    from jiuwenclaw.infrastructure.module_importer import (
        import_manager_ws_client_module,
    )

    db_mod = import_manager_ws_client_module("infrastructure.db")
    handler = await db_mod.ensure_db_handler(log_prefix="checkpoint")
    engine = handler.get_engine()
    if engine is None:
        raise RuntimeError("GatewayDb handler has no engine")
    return engine


async def _build_mysql_handler_engine():
    """获取 checkpoint MySQL AsyncEngine，复用 GatewayDb 连接池.

    未配置 GATEWAY_DB_HOST 时返回 None，由调用方决定是否回退到 SQLite。
    配置了 GATEWAY_DB_HOST 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        engine = await _get_shared_gateway_db_engine()
        logger.info(
            "[JiuWenClawDeepAdapter] checkpoint MySQL engine reused from GatewayDb: %s/%s",
            db_host,
            db_name,
        )

        # 确保 kv_store.value 列为 LONGTEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(text(
                "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'kv_store' AND COLUMN_NAME = 'value'"
            ), {"db": db_name})
            row = result.fetchone()
            if row is None:
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS kv_store ("
                    "`key` VARCHAR(512) PRIMARY KEY,"
                    "`value` LONGTEXT NOT NULL"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                ))
                logger.info(
                    "[JiuWenClawDeepAdapter] kv_store table created with value LONGTEXT"
                )
            elif row[0].upper() != "LONGTEXT":
                await conn.execute(text(
                    "ALTER TABLE kv_store MODIFY COLUMN `value` LONGTEXT NOT NULL"
                ))
                logger.info(
                    "[JiuWenClawDeepAdapter] kv_store.value altered to LONGTEXT"
                )

        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenClawDeepAdapter] failed to create checkpoint MySQL engine: %s",
            exc,
        )
        raise


async def _build_postgresql_handler_engine():
    """获取 checkpoint PostgreSQL AsyncEngine，复用 GatewayDb 连接池.

    未配置 GATEWAY_DB_HOST 时返回 None，由调用方决定是否回退到 SQLite。
    配置了 GATEWAY_DB_HOST 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        db_schema = os.getenv("GATEWAY_PG_SCHEMA", "public").strip()
        engine = await _get_shared_gateway_db_engine()
        logger.info(
            "[JiuWenClawDeepAdapter] checkpoint PostgreSQL engine reused from GatewayDb: "
            "%s/%s schema=%s",
            db_host,
            db_name,
            db_schema,
        )
        # 确保 kv_store.value 列为 TEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_catalog = :db AND table_name = 'kv_store' AND column_name = 'value'"
            ), {"db": db_name})
            row = result.fetchone()
            if row is None:
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS kv_store ("
                    "key VARCHAR(512) PRIMARY KEY,"
                    "value TEXT NOT NULL"
                    ")"
                ))
                logger.info(
                    "[JiuWenClawDeepAdapter] kv_store table created with value TEXT"
                )
            elif row[0].lower() != "text":
                await conn.execute(text(
                    "ALTER TABLE kv_store ALTER COLUMN value TYPE TEXT"
                ))
                logger.info(
                    "[JiuWenClawDeepAdapter] kv_store.value altered to TEXT"
                )
        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenClawDeepAdapter] failed to create checkpoint PostgreSQL engine: %s",
            exc,
        )
        raise


class _RuntimeCronToolContext:
    """Stable cron tool context proxy backed by per-task contextvars."""

    def __init__(self, tool_scope: str) -> None:
        self._tool_scope = tool_scope

    @property
    def channel_id(self) -> str:
        return get_cron_tool_channel_id()

    @property
    def session_id(self) -> str | None:
        return get_cron_tool_session_id()

    @property
    def metadata(self) -> dict[str, Any] | None:
        return get_cron_tool_metadata()

    @property
    def mode(self) -> str | None:
        return get_cron_tool_mode()

    @property
    def tool_scope(self) -> str:
        return self._tool_scope


class JiuWenClawDeepAdapter:
    """Deep SDK 适配器，实现 AgentAdapter 协议.

    封装所有 Deep SDK 专属逻辑：
    - DeepAgent 实例生命周期管理
    - Deep runtime tools 注册
    - Deep stream event 解析
    - Deep evolution 绑定
    - Deep interrupt / user_answer 处理
    """

    _sysop_cache: dict[str, tuple[str, "SysOperation"]] = {}

    def __init__(
        self,
        workspace_dir: str | None = None,
        agent_id: str | None = None,
        service_id: str | None = None,
        assembly_cache: Any | None = None,
    ) -> None:
        _apply_llm_io_trace_patch()
        # 租户级装配缓存(AgentManager 注入): ent_cfg/skill_sync 结果复用, None=旧行为
        self._assembly_cache = assembly_cache
        self._instance: DeepAgent | None = None
        self._workspace_dir: str = workspace_dir or str(get_agent_root_dir())
        self._agent_name: str = "main_agent"
        self._agent_id = agent_id
        self._service_id = service_id
        self._vision_tools_registered: bool = False
        self._audio_tools_registered: bool = False
        self._video_tool_registered: bool = False
        self._send_file_toolkit: SendFileToolkit | None = None
        self._model: Model | None = None
        self._model_client_config: ModelClientConfig | None = None
        self._model_request_config: ModelRequestConfig | None = None
        self._config_cache: dict[str, Any] = {}
        self._filesystem_rail: FileSystemRail | None = None
        self._skill_rail: SkillUseRail | None = None
        self._stream_event_rail: JiuClawStreamEventRail | None = None
        self._task_execution_rail: TaskExecutionRail | None = None
        self._task_planning_rail: TaskPlanningRail | None = None
        self._context_engineering_rail: ContextEngineeringRail | None = None
        self._context_engineering_rail_mode: str | None = None
        self._runtime_prompt_rail: RuntimePromptRail | None = None
        self._response_prompt_rail: ResponsePromptRail | None = None
        self._skill_protocol_prompt_rail: SkillProtocolPromptRail | None = None
        self._skill_compliance_rail: SkillComplianceRail | None = None
        self._skill_authorization_rail: Any = None
        self._security_rail: SecurityRail | None = None
        self._memory_rail: MemoryRail | None = None
        self._external_memory_rail: Any = None
        self._external_memory_rail_registered: bool = False
        self._lsp_rail: LspRail | None = None
        self._heartbeat_rail: HeartbeatRail | None = None
        self._skill_evolution_rail: SkillEvolutionRail | None = None
        self._subagent_rail: SubagentRail | None = None
        self._subagent_executor: Any = None
        self._disabled_tools_rail: DisabledToolsRail | None = None
        self._permission_rail: Any = None
        self._avatar_rail: Any = None
        self._tool_cards = None
        self._sys_operation = None
        self._vision_model_config: VisionModelConfig | None = None
        self._audio_model_config: AudioModelConfig | None = None
        self._video_model_config: bool = False
        self._vision_tools: list[Any] = []
        self._audio_tools: list[Any] = []
        self._instance_overrides: dict[str, Any] = {}
        self._xiaoyi_phone_tools_registered: bool = False
        self._paid_search_registered: bool = False
        self._paid_search_tool: WebPaidSearchTool | None = None
        self._petal_search_tools: list[Any] = []
        self._petal_search_registered: bool = False
        self._skill_manager: SkillManager | None = None
        self._cron_runtime = CronRuntimeBridge()
        self._runtime_cron_tool_context = _RuntimeCronToolContext(
            tool_scope=f"runtime_{id(self):x}",
        )
        self._is_proactive_memory: bool | None = None
        self._model_cache: dict[str, Model] = {}
        self._default_model_name: str = ""
        self._model_config_source: str = "config.yaml"
        self._enterprise_config: Any = None
        self._startup_config_base: dict[str, Any] | None = None
        self._multi_session_toolkit: MultiSessionToolkit | None = None
        # request_id -> toolkit；session_id -> 关联的 request_id 集合（interrupt 时按会话精确取消）
        self._request_session_toolkits: dict[str, MultiSessionToolkit] = {}
        self._session_toolkit_requests: dict[str, set[str]] = {}
        self._enabled_skills: list[str] | None = None
        self._prebuilt_skills: set[str] = set()

    def set_skill_manager(self, skill_manager: SkillManager) -> None:
        """Inject shared SkillManager from facade for tool reuse."""
        self._skill_manager = skill_manager

    def _resolve_skill_dirs(self, extra_skill_dir: str | None = None) -> list[str]:
        if is_skill_whitelist_tenant(self._agent_id, self._service_id):
            skills_dirs = [str(Path(self._workspace_dir) / "skills")]
        else:
            skills_dirs = [str(p) for p in get_agent_registered_skill_dirs()]
        if extra_skill_dir:
            skills_dirs.append(extra_skill_dir)
        return skills_dirs

    def _resolve_skill_trust(self, skill_dir: Path):
        """按企业安装账本识别预置 Skill；非企业场景保留包内置目录规则。"""
        from jiuwenclaw.agentserver.permissions.skill_authorization import SkillTrustLevel

        if is_skill_whitelist_tenant(self._agent_id, self._service_id):
            return (
                SkillTrustLevel.BUILTIN
                if Path(skill_dir).name in self._prebuilt_skills
                else SkillTrustLevel.OTHER
            )
        from jiuwenclaw.agentserver.deep_agent.rails.skill_authorization_rail import (
            _default_trust_resolver,
        )

        return _default_trust_resolver(skill_dir)

    async def _refresh_skill_identity(self, skill_name: str) -> None:
        """按安装账本校准当前企业 Skill 的预置身份，不改变 Skill 加载集合。"""
        if not is_skill_whitelist_tenant(self._agent_id, self._service_id):
            return
        name = str(skill_name or "").strip()
        if not name or Path(name).name != name:
            return

        from jiuwenclaw.agentserver.installed_skill import (
            SOURCE_PREBUILT,
            get_installed_skill,
        )

        try:
            installed = await get_installed_skill(
                service_id=self._service_id,
                agent_id=self._agent_id,
                skill_name=name,
            )
        except Exception:  # noqa: BLE001 — 查询失败保留实例已有快照，加载行为不变
            logger.warning(
                "[JiuWenClawDeepAdapter] refresh skill identity failed "
                "service_id=%s agent_id=%s skill=%s",
                self._service_id,
                self._agent_id,
                name,
                exc_info=True,
            )
            return

        if (
            installed is not None
            and str(installed.get("source_type") or "").strip() == SOURCE_PREBUILT
        ):
            self._prebuilt_skills.add(name)
        else:
            self._prebuilt_skills.discard(name)

    @staticmethod
    def _is_acp_tool_profile(config: dict[str, Any] | None = None) -> bool:
        if not isinstance(config, dict):
            return False
        tool_profile = str(config.get("tool_profile") or "").strip().lower()
        if tool_profile:
            return tool_profile == "acp"
        channel_id = str(config.get("channel_id") or "").strip().lower()
        return channel_id == "acp"

    def _filesystem_rail_enabled_for_profile(self) -> bool:
        raw = self._instance_overrides.get("enable_filesystem_rail", True)
        return bool(raw)

    def _skill_include_harness_fs_tools(self) -> bool:
        """Register harness read_file/code/bash via SkillUseRail (bundled with skill tools).

        When FileSystemRail is enabled, those file/shell tools live on FileSystemRail instead;
        use ``_skill_include_skill_body_tools`` so ``skill_tool`` / ``skill_complete`` still register.
        """
        if self._is_acp_tool_profile(self._instance_overrides):
            return False
        return not self._filesystem_rail_enabled_for_profile()

    def _skill_include_skill_body_tools(self) -> bool:
        """Expose ``skill_tool`` / ``skill_complete`` unless the session is an ACP tool profile."""
        return not self._is_acp_tool_profile(self._instance_overrides)

    @staticmethod
    def _resolve_prompt_channel(session_id: str | None = None) -> str:
        """Resolve prompt channel from session id."""
        if not session_id:
            return "web"

        channel = session_id.split("_", 1)[0]
        if channel == "sess":
            return "web"
        if channel in {"acp", "cron", "heartbeat", "feishu", "web", "dingtalk", "wecom"}:
            return channel
        return "web"

    @staticmethod
    def _resolve_prompt_language() -> str:
        """Resolve configured prompt language for builder input."""
        config_base = get_config()
        return str(config_base.get("preferred_language", "zh")).strip().lower()

    def _resolve_runtime_language(self) -> str:
        """Resolve normalized runtime language shared by rails and tools."""
        return resolve_language(self._resolve_prompt_language())

    def _resolve_model_name(self) -> str:
        """Resolve current model name from model request config."""
        if self._model_request_config and hasattr(self._model_request_config, 'model'):
            return self._model_request_config.model or "unknown"
        return "unknown"

    @staticmethod
    def _browser_runtime_enabled() -> bool:
        """Whether browser runtime support is enabled for DeepAgent subagent wiring."""
        # value = str(
        #     os.getenv("PLAYWRIGHT_RUNTIME_MCP_ENABLED")
        #     or os.getenv("BROWSER_RUNTIME_MCP_ENABLED")
        #     or ""
        # ).strip().lower()
        # return value in {"1", "true", "yes", "on"}

        # close browser subagent
        return False

    @staticmethod
    def _resolve_managed_browser_binary_from_config() -> str:
        """Resolve managed-browser binary from saved browser config."""
        config_base = get_config()
        if not isinstance(config_base, dict):
            return ""
        config = resolve_env_vars(config_base)
        browser_cfg = config.get("browser", {}) if isinstance(config, dict) else {}
        if not isinstance(browser_cfg, dict):
            return ""
        chrome_path = browser_cfg.get("chrome_path", "")
        if isinstance(chrome_path, str):
            return chrome_path.strip()
        if not isinstance(chrome_path, dict):
            return ""
        platform_map = {
            "win32": "windows",
            "cygwin": "windows",
            "darwin": "macos",
            "linux": "linux",
            "linux2": "linux",
        }
        os_key = platform_map.get(os.sys.platform, "default")
        for key in (os_key, "default"):
            value = chrome_path.get(key, "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _is_subagent_enabled(subagent_cfg: Any) -> bool:
        """Treat only explicit `enabled: true` as enabled."""
        return isinstance(subagent_cfg, dict) and bool(subagent_cfg.get("enabled", False))

    def _build_configured_subagents(
            self,
            model: Model,
            config: dict[str, Any],
            config_base: dict[str, Any] | None = None,
    ) -> list[Any] | None:
        """Build configured code/research subagents plus default browser subagent."""
        react_cfg = config if isinstance(config, dict) else {}
        subagents_cfg = react_cfg.get("subagents")

        resolved_language = self._resolve_runtime_language()
        workspace = self._workspace_dir or "./"
        subagents: list[Any] = []

        if isinstance(subagents_cfg, dict):
            code_agent_cfg = subagents_cfg.get("code_agent")
            if self._is_subagent_enabled(code_agent_cfg):
                code_agent_rails = None
                if get_memory_mode(get_config()) == "local":
                    coding_memory_rail = self._build_coding_memory_rail()
                    if coding_memory_rail is not None:
                        # FileSystemRail 是 create_code_agent 的默认 rail，传 rails 会覆盖默认值，需显式带上
                        code_agent_rails = [FileSystemRail(), coding_memory_rail]
                subagents.append(
                    build_code_agent_config(
                        model,
                        workspace=workspace,
                        language=resolved_language,
                        rails=code_agent_rails,
                        max_iterations=_parse_int(
                            code_agent_cfg.get("max_iterations"),
                            react_cfg.get("max_iterations", 15),
                        ),
                    )
                )

            research_agent_cfg = subagents_cfg.get("research_agent")
            if self._is_subagent_enabled(research_agent_cfg):
                subagents.append(
                    build_research_agent_config(
                        model,
                        workspace=workspace,
                        language=resolved_language,
                        max_iterations=_parse_int(
                            research_agent_cfg.get("max_iterations"),
                            react_cfg.get("max_iterations", 15),
                        ),
                        tools=build_jiuwen_harness_named_web_tools(
                            agent_id="research_agent",
                            language=resolved_language,
                        ),
                    )
                )

        browser_agent_cfg = subagents_cfg.get("browser_agent") if isinstance(subagents_cfg, dict) else {}
        browser_enabled = self._browser_runtime_enabled()
        if browser_enabled:
            if not str(os.getenv("BROWSER_DRIVER") or "").strip():
                os.environ["BROWSER_DRIVER"] = "managed"
                logger.info(
                    "[JiuWenClawDeepAdapter] browser subagent enabled without BROWSER_DRIVER; "
                    "defaulting to managed mode"
                )
            if not str(os.getenv("BROWSER_MANAGED_BINARY") or "").strip():
                chrome_path = self._resolve_managed_browser_binary_from_config()
                if chrome_path:
                    os.environ["BROWSER_MANAGED_BINARY"] = chrome_path
                    logger.info(
                        "[JiuWenClawDeepAdapter] using browser.chrome_path for managed browser: %s",
                        chrome_path,
                    )
            subagents.append(
                build_browser_agent_config(
                    model,
                    workspace=workspace,
                    language=resolved_language,
                    max_iterations=_parse_int(
                        browser_agent_cfg.get("max_iterations") if isinstance(browser_agent_cfg, dict) else None,
                        react_cfg.get("max_iterations", 15),
                    )
                )
            )
        elif isinstance(subagents_cfg, dict) and isinstance(browser_agent_cfg, dict) and browser_agent_cfg:
            logger.info(
                "[JiuWenClawDeepAdapter] browser_agent config detected but browser runtime is not enabled; "
                "skipping browser subagent registration"
            )

        return subagents

    def _build_vision_model_config(
            self,
            config_base: dict[str, Any],
    ) -> VisionModelConfig | None:
        """Build DeepAgent vision config from service config/env mapping."""
        if not dedicated_multimodal_model_configured(config_base, "vision"):
            logger.info(
                "[JiuWenClawDeepAdapter] vision tools skipped: models.vision has no dedicated "
                "api_key in config.yaml"
            )
            return None
        apply_vision_model_config_from_yaml(config_base)
        api_key = str(os.getenv("VISION_API_KEY", "")).strip()
        base_url = str(
            os.getenv("VISION_BASE_URL")
            or os.getenv("VISION_API_BASE")
            or ""
        ).strip()
        model_name = str(
            os.getenv("VISION_MODEL")
            or os.getenv("VISION_MODEL_NAME")
            or ""
        ).strip()
        if not api_key or not base_url or not model_name:
            logger.info(
                "[JiuWenClawDeepAdapter] vision tools skipped: incomplete config"
            )
            return None
        return VisionModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            max_retries=_parse_int(os.getenv("VISION_MAX_RETRIES"), 3),
        )

    def _build_audio_model_config(
            self,
            config_base: dict[str, Any],
    ) -> AudioModelConfig | None:
        """Build DeepAgent audio config from service config/env mapping."""
        if not dedicated_multimodal_model_configured(config_base, "audio"):
            logger.info(
                "[JiuWenClawDeepAdapter] skip full audio LLM config: models.audio has no "
                "dedicated api_key in config.yaml"
            )
            return None
        apply_audio_model_config_from_yaml(config_base)
        api_key = str(os.getenv("AUDIO_API_KEY", "")).strip()
        base_url = str(
            os.getenv("AUDIO_BASE_URL")
            or os.getenv("AUDIO_API_BASE")
            or ""
        ).strip()
        if not api_key or not base_url:
            logger.info(
                "[JiuWenClawDeepAdapter] audio tools skipped: incomplete config"
            )
            return None
        transcription_model = str(
            os.getenv("AUDIO_TRANSCRIPTION_MODEL")
            or os.getenv("AUDIO_MODEL_NAME")
            or ""
        ).strip()
        question_answering_model = str(
            os.getenv("AUDIO_QUESTION_ANSWERING_MODEL")
            or os.getenv("AUDIO_MODEL_NAME")
            or ""
        ).strip()
        config_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": _parse_int(os.getenv("AUDIO_MAX_RETRIES"), 3),
            "http_timeout": _parse_int(os.getenv("AUDIO_HTTP_TIMEOUT"), 20),
            "max_audio_bytes": _parse_int(
                os.getenv("AUDIO_MAX_AUDIO_BYTES"),
                25 * 1024 * 1024,
            ),
        }
        acr_access_key = str(os.getenv("ACR_ACCESS_KEY", "")).strip()
        acr_access_secret = str(os.getenv("ACR_ACCESS_SECRET", "")).strip()
        acr_base_url = str(os.getenv("ACR_BASE_URL", "")).strip()
        if acr_access_key:
            config_kwargs["acr_access_key"] = acr_access_key
        if acr_access_secret:
            config_kwargs["acr_access_secret"] = acr_access_secret
        if acr_base_url:
            config_kwargs["acr_base_url"] = acr_base_url
        if transcription_model:
            config_kwargs["transcription_model"] = transcription_model
        if question_answering_model:
            config_kwargs[
                "question_answering_model"
            ] = question_answering_model
        return AudioModelConfig(**config_kwargs)

    def _build_video_model_config(
            self,
            config_base: dict[str, Any],
    ) -> bool:
        """Build DeepAgent video config from service config/env mapping."""
        apply_video_model_config_from_yaml(config_base)
        if not dedicated_multimodal_model_configured(config_base, "video"):
            logger.info(
                "[JiuWenClawDeepAdapter] skip video_understanding: models.video has no "
                "dedicated api_key in config.yaml"
            )
            return False
        if not os.getenv("VIDEO_API_KEY"):
            logger.info(
                "[JiuWenClawDeepAdapter] video tools skipped: incomplete config"
            )
            return False
        return True

    def _iter_runtime_audio_tools(self, agent_id: str | None) -> list[Any]:
        """可注册的音频工具：须先在 config 中为 ``models.audio`` 配置独立 ``api_key``。

        与 vision / video 一致，无该 key 时不挂载任何音频工具（含 ``audio_metadata``）。
        已配置 key 且 ``_audio_model_config`` 完整时注册全部 harness 音频工具；否则仅保留
        ``audio_metadata``（ACRCloud，仍依赖 ``ACR_*`` 环境变量在运行时识别曲库）。
        """
        config_base = get_config()
        if not dedicated_multimodal_model_configured(config_base, "audio"):
            logger.info(
                "[JiuWenClawDeepAdapter] skip all audio tools (incl. audio_metadata): "
                "models.audio 未配置独立 api_key"
            )
            return []
        lang = self._resolve_runtime_language()
        cfg = self._audio_model_config if self._audio_model_config else None
        tools = list(
            create_audio_tools(
                language=lang,
                audio_model_config=cfg,
                agent_id=agent_id,
            )
        )
        if self._audio_model_config:
            return tools
        filtered = [t for t in tools if t.card.name == "audio_metadata"]
        if len(tools) > len(filtered):
            logger.info(
                "[JiuWenClawDeepAdapter] skip audio_transcription & audio_question_answering: "
                "incomplete audio LLM config (metadata only)"
            )
        return filtered

    def _refresh_multimodal_configs(
            self,
            config_base: dict[str, Any],
    ) -> None:
        """Refresh cached multimodal configs and live tool instances."""
        self._vision_model_config = self._build_vision_model_config(config_base)
        self._audio_model_config = self._build_audio_model_config(config_base)
        self._video_model_config = self._build_video_model_config(config_base)

        for tool in self._vision_tools:
            tool.vision_model_config = self._vision_model_config
        for tool in self._audio_tools:
            tool.audio_model_config = self._audio_model_config

    def _sync_tool_group(
            self,
            *,
            current_tools: list[Any],
            registered: bool,
            enabled: bool,
            create_fn: Callable[[], list[Any]],
            warn_label: str,
    ) -> tuple[list[Any], bool]:
        """统一处理一组工具的热更新：启用时注册，禁用时移除。

        Returns:
            (updated_tools, updated_registered)
        """
        if not enabled:
            if registered:
                self._remove_registered_tools(current_tools)
                self._prune_tool_cards({t.card.name for t in current_tools})
            return [], False
        if not registered:
            try:
                new_tools = create_fn()
                for tool in new_tools:
                    Runner.resource_mgr.add_tool(tool)
                    self._append_tool_card(tool.card)
                    if self._instance is not None and hasattr(self._instance, "ability_manager"):
                        self._instance.ability_manager.add(tool.card)
                return new_tools, bool(new_tools)
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] %s reload failed: %s", warn_label, exc
                )
                return [], False
        return current_tools, registered

    @staticmethod
    def _purge_skill_use_rail_owned_tools(rail: SkillUseRail | None) -> None:
        """Drop SkillUseRail-owned harness tools from global ``Runner.resource_mgr``.

        ``SkillUseRail.uninit`` only removes cards from ``ability_manager``; stale
        ``SkillTool`` instances remain in ``resource_mgr`` and block hot refresh
        because ``SkillUseRail.init`` skips ``add_tool`` when the id already exists.
        """
        if rail is None:
            return
        owned_ids = getattr(rail, "_owned_tool_ids", None)
        if not owned_ids:
            return
        for tool_id in list(owned_ids):
            try:
                Runner.resource_mgr.remove_tool(tool_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenClawDeepAdapter] purge SkillUseRail tool failed id=%s: %s",
                    tool_id,
                    exc,
                )

    def _remove_registered_tools(self, tools: list[Any]) -> None:
        """Remove tool instances from ability manager and resource manager."""
        if not tools:
            return
        for tool in tools:
            try:
                Runner.resource_mgr.remove_tool(tool.card.id)
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] remove tool failed: %s",
                    exc,
                )
            if self._instance is not None and hasattr(
                    self._instance,
                    "ability_manager",
            ):
                try:
                    self._instance.ability_manager.remove(tool.card.name)
                except Exception:
                    logger.debug(
                        "[JiuWenClawDeepAdapter] ability remove skipped for %s",
                        tool.card.name,
                        exc_info=True,
                    )

    def _append_tool_card(self, card: ToolCard) -> None:
        """Append tool card if it is not already tracked."""
        if self._tool_cards is None:
            self._tool_cards = []
        existing_names = {
            item.card.name if hasattr(item, "card") else item.name
            for item in self._tool_cards
        }
        if card.name not in existing_names:
            self._tool_cards.append(card)

    def _prune_tool_cards(self, tool_names: set[str]) -> None:
        """Remove tracked tool cards by tool name."""
        if not self._tool_cards:
            return
        self._tool_cards = [
            item
            for item in self._tool_cards
            if (
                   item.card.name if hasattr(item, "card") else item.name
               ) not in tool_names
        ]

    def _sync_multimodal_tools_for_runtime(self) -> None:
        """Sync multimodal tool registration after config reload."""
        agent_id = self._instance.card.id if self._instance else None
        self._vision_tools, self._vision_tools_registered = self._sync_tool_group(
            current_tools=self._vision_tools,
            registered=self._vision_tools_registered,
            enabled=self._vision_model_config is not None,
            create_fn=lambda: create_vision_tools(
                language=self._resolve_runtime_language(),
                vision_model_config=self._vision_model_config,
                agent_id=agent_id,
            ),
            warn_label="vision tools",
        )

        self._audio_tools, self._audio_tools_registered = self._sync_tool_group(
            current_tools=self._audio_tools,
            registered=self._audio_tools_registered,
            enabled=True,
            create_fn=lambda: self._iter_runtime_audio_tools(agent_id),
            warn_label="audio tools",
        )

        _, self._video_tool_registered = self._sync_tool_group(
            current_tools=[video_understanding],
            registered=self._video_tool_registered,
            enabled=bool(self._video_model_config),
            create_fn=lambda: [video_understanding],
            warn_label="video tool",
        )

    def _sync_paid_search_tool_for_runtime(self) -> None:
        """Sync paid-search tool registration after config reload."""
        agent_id = self._instance.card.id if self._instance else None
        tools, self._paid_search_registered = self._sync_tool_group(
            current_tools=[self._paid_search_tool] if self._paid_search_tool else [],
            registered=self._paid_search_registered,
            enabled=any(
                os.environ.get(key)
                for key in ("BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY")
            ),
            create_fn=lambda: [WebPaidSearchTool(language=self._resolve_runtime_language(), agent_id=agent_id)],
            warn_label="paid search tool",
        )
        self._paid_search_tool = tools[0] if tools else None

    def _sync_petal_search_tool_for_runtime(self) -> None:
        """热更新后同步 Petal 搜索工具注册状态。"""
        self._petal_search_tools, self._petal_search_registered = self._sync_tool_group(
            current_tools=self._petal_search_tools,
            registered=self._petal_search_registered,
            enabled=enable_petal_search(),
            create_fn=lambda: [mcp_petal_search],
            warn_label="petal search tool",
        )

    @staticmethod
    async def set_checkpoint():
        global _shared_checkpoint_checkpointer
        async with _get_checkpoint_singleton_lock():
            if _shared_checkpoint_checkpointer is not None:
                CheckpointerFactory.set_default_checkpointer(_shared_checkpoint_checkpointer)
                return
            PersistenceCheckpointerProvider()
            checkpoint_path = get_checkpoint_dir()
            conf = {"db_type": "sqlite", "db_path": f"{checkpoint_path}/checkpoint"}

            db_type = os.getenv("GATEWAY_DB_TYPE", "").strip().lower()
            if db_type == "mysql":
                mysql_engine = await _build_mysql_handler_engine()
                if mysql_engine is not None:
                    conf["db_client"] = mysql_engine
                    logger.info("[JiuWenClawDeepAdapter] use mysql db_client from SDK")
            elif db_type in ("postgresql", "postgres", "pg"):
                postgresql_engine = await _build_postgresql_handler_engine()
                if postgresql_engine is not None:
                    conf["db_client"] = postgresql_engine
                    logger.info("[JiuWenClawDeepAdapter] use postgresql db_client from SDK")
            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf=conf)
            )
            _shared_checkpoint_checkpointer = checkpointer
            CheckpointerFactory.set_default_checkpointer(checkpointer)


    @staticmethod
    def _normalize_model_client_config_dict(mcc: dict) -> dict:
        """YAML / 环境变量替换可能把 dict 字段写成空串，避免 ModelClientConfig 校验失败。"""
        out = dict(mcc)
        ch = out.get("custom_headers")
        if ch == "":
            out["custom_headers"] = None
        elif ch is not None and not isinstance(ch, dict):
            logger.warning(
                "[JiuWenClawDeepAdapter] model_client_config.custom_headers 须为 dict 或省略，当前为 %r，已按 None 处理",
                ch,
            )
            out["custom_headers"] = None
        return out

    @staticmethod
    def _build_model_from_entry(mcc: dict, mco: dict) -> Model:
        """根据单个模型条目的 model_client_config / model_config_obj 构建 Model 实例。"""
        mcc = JiuWenClawDeepAdapter._normalize_model_client_config_dict(mcc)
        name = mcc.get("model_name", "")
        m_config = ModelRequestConfig(
            model=name,
            temperature=mco.get("temperature", 0.95),
        )
        mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
        return Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config)

    def _build_model_cache_from_defaults(self, config: dict) -> None:
        """从 models.defaults 列表构建模型缓存。"""
        for entry in get_default_models(config):
            mcc = entry.get("model_client_config") or {}
            # 将claw_config的配置传入到model的扩展字段中, 方便注册的model实例使用
            mcc["claw_config"] = config
            if not mcc.get("model_name"):
                continue
            self._model_cache[mcc["model_name"]] = self._build_model_from_entry(
                mcc, entry.get("model_config_obj") or {},
            )

    def _build_model_cache_legacy(self, config: dict) -> None:
        """回退到旧格式（models.default / react 段）构建单条目缓存。"""
        default_model_config = config.get("models", {}).get("default", {})
        react_config = config.get("react", {})

        mcc = dict(default_model_config.get("model_client_config") or react_config.get("model_client_config") or {})
        model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
        if "model_name" not in mcc:
            mcc["model_name"] = model_name

        mco = default_model_config.get("model_config_obj") or react_config.get("model_config_obj") or {}
        self._model_cache[model_name] = self._build_model_from_entry(mcc, mco)

    def _create_model(self, config: dict) -> Model:
        self._model_cache.clear()
        self._build_model_cache_from_defaults(config)
        if not self._model_cache:
            self._build_model_cache_legacy(config)

        first_name = next(iter(self._model_cache))
        self._default_model_name = first_name
        self._model = self._model_cache[first_name]
        self._model_client_config = self._model.model_client_config
        self._model_request_config = self._model.model_config
        return self._model

    def _get_task_id(self) -> str | None:
        if self._task_execution_rail is not None:
            return self._task_execution_rail.get_current_task_id()
        return get_current_task_id()

    @staticmethod
    def _resolve_skill_mode(config: dict[str, Any]) -> str:
        """Validate configured skill mode and fallback safely on invalid values."""
        raw_skill_mode = config.get("skill_mode", SkillUseRail.SKILL_MODE_ALL)
        valid_modes = {
            SkillUseRail.SKILL_MODE_AUTO_LIST,
            SkillUseRail.SKILL_MODE_ALL,
        }
        if isinstance(raw_skill_mode, str) and raw_skill_mode in valid_modes:
            return raw_skill_mode

        logger.warning(
            "[JiuWenClawDeepAdapter] invalid skill_mode=%r, fallback to %s",
            raw_skill_mode,
            SkillUseRail.SKILL_MODE_ALL,
        )
        return SkillUseRail.SKILL_MODE_ALL

    @staticmethod
    def _build_response_prompt_rail() -> ResponsePromptRail | None:
        """Build ResponsePromptRail so message rules keep priority ordering."""
        try:
            rail = ResponsePromptRail()
            logger.info("[JiuWenClawDeepAdapter] ResponsePromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] ResponsePromptRail create failed: %s", exc)
            rail = None
        return rail

    def _create_sys_operation(self) -> SysOperation | None:
        """Create a sys operation with workspace as working directory."""
        try:
            endpoint = get_sandbox_endpoint()
            runtime = get_sandbox_runtime()
            work_dir = self._workspace_dir or str(get_agent_root_dir())
            sandbox_url = endpoint.get("url") or ""
            sandbox_type = endpoint.get("type") or ""
            sandbox_enabled = bool(runtime.get("enabled"))
            if sandbox_enabled and sandbox_url and sandbox_type:
                logger.info(
                    "[JiuWenClawDeepAdapter] sandbox mode: url=%s type=%s "
                    "startup_mode=%s idle_ttl_seconds=%s idle_check_interval=%s fallback_on_failure=%s",
                    sandbox_url,
                    sandbox_type,
                    endpoint.get("startup_mode"),
                    runtime.get("idle_ttl_seconds"),
                    runtime.get("idle_check_interval"),
                    runtime.get("fallback_on_failure"),
                )
                sysop_card = create_sandbox_sysop_card(
                    sandbox_url,
                    sandbox_type,
                    self._agent_id,
                    shared_dir=Path(self._workspace_dir).resolve().parent.parent,
                    files_runtime=runtime.get("files"),
                    excluded_commands=runtime.get("excluded_commands"),
                    idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
                    idle_check_interval=runtime.get("idle_check_interval"),
                    fallback_on_failure=runtime.get("fallback_on_failure"),
                )
            else:
                if sandbox_enabled and not (sandbox_url and sandbox_type):
                    missing = []
                    if not sandbox_url:
                        missing.append("JIUWENCLAW_SANDBOX_URL")
                    if not sandbox_type:
                        # TYPE 已有默认值, 真触发说明用户显式设了空串, 罕见
                        missing.append("JIUWENCLAW_SANDBOX_TYPE")
                    logger.warning(
                        "[JiuWenClawDeepAdapter] sandbox enabled but missing %s; "
                        "falling back to local sys_operation. set the env var(s) "
                        "and restart agent-server to actually use jiuwenbox.",
                        ", ".join(missing),
                    )
                else:
                    logger.info(
                        "[JiuWenClawDeepAdapter] local mode (sandbox %s)",
                        "disabled" if not sandbox_enabled else "url/type empty",
                    )
                sysop_card = create_local_sysop_card(work_dir=work_dir)
            if sysop_card is None:
                logger.warning("[JiuWenClawDeepAdapter] add sys_operation failed: sysop_card is None")
                return None

            cache_key = self._agent_id or sysop_card.id
            cached = JiuWenClawDeepAdapter._sysop_cache.get(cache_key)
            if cached is not None:
                cached_id, cached_sysop = cached
                existing = Runner.resource_mgr.get_sys_operation(cached_id)
                if existing is not None:
                    logger.info(
                        "[JiuWenClawDeepAdapter] reuse cached sys_operation: id=%s agent_id=%s",
                        cached_id, cache_key,
                    )
                    return existing
                JiuWenClawDeepAdapter._sysop_cache.pop(cache_key, None)

            result = Runner.resource_mgr.add_sys_operation(sysop_card)
            if result.is_err():
                error_msg = result.msg()
                logger.warning("[JiuWenClawDeepAdapter] add sys_operation failed: %s", error_msg)
                
                # 防护机制：如果错误是因为隔离键已存在，尝试复用现有的 sys_operation
                if "already registered" in str(error_msg) and "operation '" in str(error_msg):
                    import re
                    # 从错误信息中提取已存在的 operation ID
                    match = re.search(r"by operation '([a-f0-9]+)'", str(error_msg))
                    if match:
                        existing_op_id = match.group(1)
                        logger.info(
                            "[JiuWenClawDeepAdapter] 检测到隔离键冲突，尝试复用现有 sys_operation: id=%s",
                            existing_op_id
                        )
                        # 尝试获取已存在的 sys_operation
                        existing_op = Runner.resource_mgr.get_sys_operation(existing_op_id)
                        if existing_op is not None:
                            logger.info(
                                "[JiuWenClawDeepAdapter] 成功复用现有 sys_operation: id=%s",
                                existing_op_id
                            )
                            return existing_op
                        else:
                            logger.warning(
                                "[JiuWenClawDeepAdapter] 无法获取已存在的 sys_operation: id=%s",
                                existing_op_id
                            )
                
                return None
            sysop_obj = Runner.resource_mgr.get_sys_operation(sysop_card.id)
            if sysop_obj is not None:
                JiuWenClawDeepAdapter._sysop_cache[cache_key] = (sysop_card.id, sysop_obj)
            return sysop_obj
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] add sys_operation failed: %s", exc)
            return None

    def _build_filesystem_rail(self) -> FileSystemRail | None:
        """Build FileSystemRail."""
        try:
            fs_rail = ConcurrentSafeFileSystemRail()
            logger.info("[JiuWenClawDeepAdapter] FileSystemRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] FileSystemRail create failed: %s", exc)
            fs_rail = None
        return fs_rail

    def _build_skill_rail(
        self,
        config: dict[str, Any],
        *,
        include_tools: bool = False,
        include_skill_body_tools: bool = True,
        extra_skill_dir: str | None = None,
    ) -> SkillUseRail | None:
        """Build SkillUseRail.
        
        Args:
            config: React config dict
            include_tools: Whether to include harness read_file/code/bash tools
            include_skill_body_tools: Whether to include skill_tool/skill_complete tools
            extra_skill_dir: Optional extra skill directory from extension hook
        """
        try:
            skill_mode = self._resolve_skill_mode(config)
            logger.info("[JiuWenClawDeepAdapter] current skill_mode: %s", skill_mode)
            # Must match react.context_engine_config.max_active_skill_bodies (ContextEngineConfig);
            # otherwise SkillUseRail.init overwrites the merged yaml cap with the rail default (1).
            react_cec = (config.get("react") or {}).get("context_engine_config")
            max_bodies = DEFAULT_MAX_ACTIVE_SKILL_BODIES
            if isinstance(react_cec, dict) and react_cec.get("max_active_skill_bodies") is not None:
                try:
                    max_bodies = int(react_cec["max_active_skill_bodies"])
                except (TypeError, ValueError):
                    max_bodies = DEFAULT_MAX_ACTIVE_SKILL_BODIES
            
            skills_dirs = self._resolve_skill_dirs(extra_skill_dir)
            if extra_skill_dir:
                logger.info("[JiuWenClawDeepAdapter] extra_skill_dir added: %s", extra_skill_dir)

            enabled_skills = self._enabled_skills
            if is_skill_whitelist_tenant(self._agent_id, self._service_id) and enabled_skills is None:
                enabled_skills = []

            skill_rail_kwargs: dict[str, Any] = dict(
                skills_dir=skills_dirs,
                skill_mode=skill_mode,
                include_tools=include_tools,
            )
            if enabled_skills is not None:
                skill_rail_kwargs["enabled_skills"] = enabled_skills
            if _UPSTREAM_HAS_ACTIVE_SKILL_BODIES:
                skill_rail_kwargs["include_skill_body_tools"] = include_skill_body_tools
                skill_rail_kwargs["max_active_skill_bodies"] = max_bodies
            skill_rail = SkillUseRail(**skill_rail_kwargs)
            logger.info("[JiuWenClawDeepAdapter] SkillUseRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SkillUseRail create failed: %s", exc)
            skill_rail = None
        return skill_rail

    async def refresh_enabled_skills_from_db(self) -> None:
        """账本变更后直读 DB 刷新 ``_enabled_skills`` 并热替换 ``SkillUseRail``（D11 轻量路径）。

        不全量 ``create_instance``：不重建模型/工具卡，仅更新启用集与技能 Rail。
        刷新前先做盘→库对账，避免「盘有库无」导致永久 Skill not found。
        """
        if not is_skill_whitelist_tenant(self._agent_id, self._service_id):
            return
        if self._instance is None:
            logger.debug(
                "[JiuWenClawDeepAdapter] refresh_enabled_skills_from_db skipped: instance not ready"
            )
            return

        try:
            recon = await SkillWhitelistSynchronizer(
                self._workspace_dir,
                service_id=str(self._service_id or ""),
                agent_id=str(self._agent_id or ""),
            ).reconcile_disk_into_ledger()
            if recon.errors:
                logger.warning(
                    "[JiuWenClawDeepAdapter] disk→ledger reconcile warnings: %s",
                    recon.errors,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenClawDeepAdapter] disk→ledger reconcile failed: %s",
                exc,
            )

        from jiuwenclaw.agentserver.installed_skill import (
            SOURCE_PREBUILT,
            list_installed_skills,
        )

        try:
            rows = await list_installed_skills(
                service_id=str(self._service_id or ""),
                agent_id=str(self._agent_id or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenClawDeepAdapter] list_installed_skills failed: %s",
                exc,
            )
            return

        enabled_skills: list[str] = []
        prebuilt_skills: set[str] = set()
        skills_dir = Path(self._workspace_dir) / "skills"
        from jiuwenclaw.agentserver.skill_whitelist import skill_dir_ready

        for row in rows:
            skill_name = str(row.get("skill_name") or "").strip()
            if not skill_name:
                continue
            if not skill_dir_ready(skills_dir, skill_name):
                continue
            enabled_skills.append(skill_name)
            if str(row.get("source_type") or "").strip() == SOURCE_PREBUILT:
                prebuilt_skills.add(skill_name)
        self._enabled_skills = enabled_skills
        self._prebuilt_skills = prebuilt_skills

        extra_skill_dir: str | None = None
        try:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            from jiuwenclaw.schema.hooks_context import SystemPromptHookContext
            from jiuwenclaw.schema import AgentServerHookEvents

            context = SystemPromptHookContext()
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.BEFORE_SYSTEM_PROMPT_BUILD, context
            )
            extra_skill_dir = context.skill_dir
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenClawDeepAdapter] refresh_enabled_skills hook skipped: %s",
                exc,
            )

        config = self._config_cache if isinstance(self._config_cache, dict) else {}
        old_rail = self._skill_rail
        new_rail = self._build_skill_rail(
            config,
            include_tools=self._skill_include_harness_fs_tools(),
            include_skill_body_tools=self._skill_include_skill_body_tools(),
            extra_skill_dir=extra_skill_dir,
        )
        if new_rail is None:
            logger.warning(
                "[JiuWenClawDeepAdapter] refresh_enabled_skills_from_db: SkillUseRail rebuild failed"
            )
            return

        if old_rail is not None:
            # Capture before uninit clears _owned_tool_ids; purge resource_mgr so
            # the replacement rail can register fresh SkillTool instances.
            self._purge_skill_use_rail_owned_tools(old_rail)
            try:
                await self._instance.unregister_rail(old_rail)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenClawDeepAdapter] unregister old SkillUseRail failed: %s",
                    exc,
                )

        self._skill_rail = new_rail
        try:
            await self._instance.register_rail(new_rail)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenClawDeepAdapter] register new SkillUseRail failed: %s",
                exc,
            )
            return

        logger.info(
            "[JiuWenClawDeepAdapter] enabled_skills refreshed from DB: count=%s",
            len(self._enabled_skills),
        )

    def _build_skill_evolution_rail(self, config: dict[str, Any]) -> SkillEvolutionRail | None:
        """Build SkillEvolutionRail."""
        try:
            _env_auto_scan = os.getenv("EVOLUTION_AUTO_SCAN")
            if _env_auto_scan is not None:
                evolution_auto_scan: bool = _env_auto_scan.lower() in ("true", "1", "yes")
            else:
                evolution_auto_scan = config.get("evolution", {}).get("auto_scan", False)
            skill_evolution_rail = SkillEvolutionRail(
                skills_dir=self._resolve_skill_dirs(),
                llm=self._model,
                model=config.get("model_name", "gpt-4"),
                auto_scan=evolution_auto_scan,
                auto_save=False
            )
            self._skill_evolution_rail = skill_evolution_rail
            logger.info("[JiuWenClaw] SkillEvolutionRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClaw] SkillEvolutionRail create failed: %s", exc)
            skill_evolution_rail = None
        return skill_evolution_rail

    def _build_stream_event_rail(self) -> JiuClawStreamEventRail | None:
        """Build JiuClawStreamEventRail."""
        try:
            stream_event_rail = JiuClawStreamEventRail()
            logger.info("[JiuWenClawDeepAdapter] JiuClawStreamEventRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] JiuClawStreamEventRail create failed: %s", exc)
            stream_event_rail = None
        return stream_event_rail

    @staticmethod
    def _build_task_execution_rail() -> TaskExecutionRail | None:
        """Build TaskExecutionRail."""
        try:
            task_execution_rail = TaskExecutionRail()
            logger.info("[JiuWenClawDeepAdapter] TaskExecutionRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TaskExecutionRail create failed: %s", exc)
            task_execution_rail = None
        return task_execution_rail

    @staticmethod
    def _build_telemetry_rail() -> Any | None:
        """Build TelemetryRail for OpenTelemetry instrumentation."""
        try:
            from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail
            rail = TelemetryRail()
            logger.info("[JiuWenClawDeepAdapter] TelemetryRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TelemetryRail create failed: %s", exc)
            rail = None
        return rail

    @staticmethod
    def _build_extension_config_debug_rail() -> Any | None:
        """Build ExtensionConfigDebugRail for extension config end-to-end debugging."""
        try:
            from jiuwenclaw.agentserver.deep_agent.rails.extension_config_debug_rail import (
                ExtensionConfigDebugRail,
            )
            rail = ExtensionConfigDebugRail()
            logger.info("[JiuWenClawDeepAdapter] ExtensionConfigDebugRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] ExtensionConfigDebugRail create failed: %s", exc)
            rail = None
        return rail

    @staticmethod
    def _load_extra_rails_from_env() -> list[Any]:
        """Load extra DeepAgentRails from AGENT_EXTRA_RAILS env var.

        Env format (semicolon-separated module paths):
            AGENT_EXTRA_RAILS=path.to.module1;path.to.module2

        Each module must expose a ``register_rails()`` function that returns
        a list of DeepAgentRail instances.
        """
        env_value = os.getenv("AGENT_EXTRA_RAILS", "").strip()
        if not env_value:
            return []

        extra_rails: list[Any] = []
        for module_path in [p.strip() for p in env_value.split(";") if p.strip()]:
            try:
                mod = importlib.import_module(module_path)
                register_fn = getattr(mod, "register_rails", None)
                if register_fn is None:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] Extra rail module '%s' has no register_rails(), skipping",
                        module_path,
                    )
                    continue
                rails = register_fn()
                if rails:
                    if not isinstance(rails, list):
                        rails = [rails]
                    extra_rails.extend(rails)
                    logger.info(
                        "[JiuWenClawDeepAdapter] Loaded %d rail(s) from '%s'",
                        len(rails), module_path,
                    )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] Failed to load extra rails from '%s': %s",
                    module_path, exc,
                )
        return extra_rails

    def _build_task_planning_rail(self) -> TaskPlanningRail | None:
        """Build TaskPlanningRail."""
        try:
            task_planning_rail = ConcurrentSafeTaskPlanningRail()
            logger.info("[JiuWenClawDeepAdapter] TaskPlanningRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TaskPlanningRail create failed: %s", exc)
            task_planning_rail = None
        return task_planning_rail

    @staticmethod
    def _build_subagent_rail() -> SubagentRail | None:
        """Build SubagentRail for subagent delegation."""
        try:
            subagent_rail = SubagentRail()
            logger.info("[JiuWenClawDeepAdapter] SubagentRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SubagentRail create failed: %s", exc)
            subagent_rail = None
        return subagent_rail

    def _build_security_rail(self) -> SecurityRail | None:
        """Build SecurityPromptRail."""
        try:
            security_prompt_rail = SecurityRail()
            logger.info("[JiuWenClawDeepAdapter] SecurityPromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SecurityPromptRail create failed: %s", exc)
            security_prompt_rail = None
        return security_prompt_rail

    def _build_memory_rail(self, mode: str) -> MemoryRail | None:
        try:
            from jiuwenclaw.agentserver.memory.config import get_embed_config
            config = get_config()
            embed_config = get_embed_config()
            has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
            has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
            has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning("[JiuWenClawDeepAdapter] MemoryRail create failed: No available embedding config")
            self._is_proactive_memory = is_proactive_memory(mode, config)
            memory_rail = MemoryRail(
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("model"),
                    base_url=embed_config.get("base_url"),
                    api_key=embed_config.get("api_key")
                ),
                is_proactive=self._is_proactive_memory
            )
            logger.info("[JiuWenClawDeepAdapter] MemoryRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] MemoryRail create failed: %s", exc)
            memory_rail = None
        return memory_rail

    def _build_coding_memory_rail(self) -> CodingMemoryRail | None:
        """构建 CodingMemoryRail.

        Returns:
            CodingMemoryRail 实例，失败返回 None
        """
        try:
            from jiuwenclaw.agentserver.memory.config import get_embed_config
            config = get_config()
            embed_config = get_embed_config()

            # 检查 embedding 配置
            has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
            has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
            has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning("[JiuWenClawDeepAdapter] CodingMemoryRail: no embedding config, skipping")
                return None

            # 获取语言和 workspace 目录
            language = config.get("preferred_language", "zh")
            coding_memory_dir = os.path.join(self._workspace_dir, "coding_memory")

            # 确保目录存在
            os.makedirs(coding_memory_dir, exist_ok=True)

            # 创建 CodingMemoryRail
            coding_memory_rail = CodingMemoryRail(
                coding_memory_dir=coding_memory_dir,
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("model"),
                    base_url=embed_config.get("base_url"),
                    api_key=embed_config.get("api_key"),
                ),
                language="cn" if language == "zh" else "en",
            )
            logger.info("[JiuWenClawDeepAdapter] CodingMemoryRail create success")
            return coding_memory_rail

        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] CodingMemoryRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_lsp_rail() -> LspRail | None:
        """Build LspRail."""
        try:
            lsp_rail = LspRail()
            logger.info("[JiuWenClawDeepAdapter] LspRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] LspRail create failed: %s", exc)
            lsp_rail = None
        return lsp_rail

    def _build_heartbeat_rail(self) -> HeartbeatRail | None:
        """Build HeartbeatRail."""
        try:
            heartbeat_rail = HeartbeatRail()
            logger.info("[JiuWenClawDeepAdapter] HeartbeatRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] HeartbeatRail create failed: %s", exc)
            heartbeat_rail = None
        return heartbeat_rail

    @staticmethod
    def _build_avatar_rail() -> Any | None:
        """Build AvatarPromptRail for digital avatar mode."""
        try:
            from jiuwenclaw.agentserver.deep_agent.rails.avatar_rail import AvatarPromptRail
            rail = AvatarPromptRail()
            logger.info("[JiuWenClawDeepAdapter] AvatarPromptRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] AvatarPromptRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_skill_protocol_prompt_rail() -> SkillProtocolPromptRail | None:
        """Build SkillProtocolPromptRail: skills 段（skill_step）。"""
        try:
            rail = SkillProtocolPromptRail()
            logger.info(
                "[JiuWenClawDeepAdapter] SkillProtocolPromptRail create success "
                "(plan: skill_step)"
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillProtocolPromptRail create failed: %s", exc
            )
            return None

    @staticmethod
    def _build_skill_compliance_rail() -> SkillComplianceRail | None:
        """Build SkillComplianceRail：硬绑 skill_step.md / skill_step。"""
        try:
            rail = SkillComplianceRail()
            logger.info(
                "[JiuWenClawDeepAdapter] SkillComplianceRail create success "
                "(skill_step.md / skill_step)"
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillComplianceRail create failed: %s", exc
            )
            return None

    def _build_skill_authorization_rail(self):
        """Build SkillAuthorizationRail；rail 内部按实时 feature flag 惰性启停。"""
        try:
            from jiuwenclaw.agentserver.deep_agent.rails.skill_authorization_rail import (
                SkillAuthorizationRail,
                build_skill_registry_resolver,
            )

            rail = SkillAuthorizationRail(
                engine=self._permission_rail.engine
                if self._permission_rail is not None else None,
                skill_resolver=build_skill_registry_resolver(
                    # 延迟到调用时取最新实例：热更新/重建会替换 self._skill_rail，
                    # 若绑定构建期实例，可能拿到未扫盘的旧实例导致 manifest_unresolved
                    lambda: (
                        self._skill_rail.get_skills_meta()
                        if self._skill_rail is not None
                        else []
                    ),
                    skill_dirs_provider=lambda: self._resolve_skill_dirs(),
                ),
                trust_resolver=self._resolve_skill_trust,
                skill_identity_refresher=self._refresh_skill_identity,
            )
            logger.info("[JiuWenClawDeepAdapter] SkillAuthorizationRail create success")
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillAuthorizationRail create failed: %s", exc
            )
            return None

    def _build_runtime_prompt_rail(
        self,
        custom_home_dir: str | None = None,
    ) -> RuntimePromptRail | None:
        """Build RuntimePromptRail for per-model-call time/channel/runtime injection.
        
        Args:
            custom_home_dir: Optional custom home directory for SOUL.md loading
        """
        try:
            default_channel = (
                "acp" if self._is_acp_tool_profile(self._instance_overrides)
                else self._resolve_prompt_channel()
            )
            rail = RuntimePromptRail(
                language=self._resolve_runtime_language(),
                channel=default_channel,
                agent_name=self._agent_name,
                model_name=self._resolve_model_name(),
                workspace_dir=self._workspace_dir,
                agent_id=self._agent_id,
                service_id=self._service_id,
                custom_home_dir=custom_home_dir,
            )
            if custom_home_dir:
                logger.info("[JiuWenClawDeepAdapter] custom_home_dir configured: %s", custom_home_dir)
            logger.info("[JiuWenClawDeepAdapter] RuntimePromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] RuntimePromptRail create failed: %s", exc)
            rail = None
        return rail

    def _build_disabled_tools_rail(self, config: dict[str, Any]) -> DisabledToolsRail | None:
        """Build DisabledToolsRail to filter out disabled tools based on config."""
        try:
            disabled_list = config.get("disabled_tools", [])
            rail = DisabledToolsRail(disabled_tools=disabled_list)
            logger.info(
                "[JiuWenClawDeepAdapter] DisabledToolsRail create success, disabled_tools: %s",
                disabled_list,
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] DisabledToolsRail create failed: %s", exc)
            rail = None
        return rail

    def _build_agent_rails(self, config: dict[str, Any], config_base: dict[str, Any], *,
                           mode: str = "agent.plan",
                           extra_skill_dir: str | None = None,
                           custom_home_dir: str | None = None) -> list[Any]:
        """Build DeepAgent rails consistently for cold start and hot reload.
        
        Args:
            config: React config dict
            config_base: Full config dict
            mode: Agent mode (agent.plan, agent.fast, code)
            extra_skill_dir: Optional extra skill directory from extension hook
            custom_home_dir: Optional custom home directory from extension hook (for SOUL.md)
        """

        @dataclass
        class _RailBuildInfo:
            attr_name: str
            build_func: callable
            params: dict = None

            def __post_init__(self):
                self.params = self.params or {}

        rail_infos = [
            # TelemetryRail - lowest priority, runs first for full coverage
            _RailBuildInfo("_telemetry_rail", self._build_telemetry_rail),
            _RailBuildInfo(
                "_runtime_prompt_rail",
                self._build_runtime_prompt_rail,
                {"custom_home_dir": custom_home_dir},
            ),
            # an example to use extension rail
            # _RailBuildInfo("_extension_config_debug_rail", self._build_extension_config_debug_rail),
            _RailBuildInfo("_response_prompt_rail", self._build_response_prompt_rail),
            _RailBuildInfo("_task_execution_rail", self._build_task_execution_rail),
            _RailBuildInfo("_stream_event_rail", self._build_stream_event_rail),
            _RailBuildInfo("_task_planning_rail", self._build_task_planning_rail),
            _RailBuildInfo("_security_rail", self._build_security_rail),
            _RailBuildInfo("_heartbeat_rail", self._build_heartbeat_rail),
            _RailBuildInfo("_avatar_rail", self._build_avatar_rail),
            _RailBuildInfo("_subagent_rail", self._build_subagent_rail),
            _RailBuildInfo("_permission_rail", build_permission_rail, {"config": config_base, "llm": self._model,
                                                                       "model_name": config_base.get("models", {}).get(
                                                                           "default", {}).get("model_client_config",
                                                                                              {}).get("model_name",
                                                                                                      "gpt-4")}),
            _RailBuildInfo("_skill_authorization_rail", self._build_skill_authorization_rail),
            # DisabledToolsRail - highest priority (100), runs last to filter disabled tools
            _RailBuildInfo("_disabled_tools_rail", self._build_disabled_tools_rail, {"config": config}),
        ]
        # ContextEngineeringRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销

        # SkillEvolutionRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销
        # 智能模式下关闭自演进，plan 模式下按配置启用

        # MemoryRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销

        # LspRail 仅在 code 模式下挂载
        if mode == "code":
            rail_infos.append(_RailBuildInfo("_lsp_rail", self._build_lsp_rail))

        # Skill 合规相关 rail 仅在 plan 模式下挂载；agent 模式不注入，避免改变既有行为。
        # team 模式由 build_member_rails 自行挂载，不在这里处理。
        if mode == "agent.plan":
            rail_infos.append(
                _RailBuildInfo("_skill_protocol_prompt_rail", self._build_skill_protocol_prompt_rail)
            )
            rail_infos.append(
                _RailBuildInfo("_skill_compliance_rail", self._build_skill_compliance_rail)
            )
        else:
            self._skill_protocol_prompt_rail = None
            self._skill_compliance_rail = None

        if self._filesystem_rail_enabled_for_profile():
            rail_infos.insert(1, _RailBuildInfo("_filesystem_rail", self._build_filesystem_rail))
        else:
            self._filesystem_rail = None
        rail_infos.insert(
            2 if self._filesystem_rail_enabled_for_profile() else 1,
            _RailBuildInfo(
                "_skill_rail",
                self._build_skill_rail,
                {
                    "config": config,
                    "include_tools": self._skill_include_harness_fs_tools(),
                    "include_skill_body_tools": self._skill_include_skill_body_tools(),
                    "extra_skill_dir": extra_skill_dir,
                },
            ),
        )

        rails_list = []
        for info in rail_infos:
            logger.info("[JiuWenClawDeepAdapter] Building rail: %s with params: %s", info.attr_name, info.params)
            rail_instance = info.build_func(**info.params)
            if rail_instance is not None:
                setattr(self, info.attr_name, rail_instance)
                rails_list.append(rail_instance)
                logger.info("[JiuWenClawDeepAdapter] Rail %s built successfully and added to rails_list",
                            info.attr_name)
            else:
                logger.warning("[JiuWenClawDeepAdapter] Rail %s build returned None", info.attr_name)
        logger.info("[JiuWenClawDeepAdapter] Total rails built: %d, rail names: %s", len(rails_list),
                    [type(r).__name__ for r in rails_list])

        # 从环境变量加载额外 Rails（非侵入式扩展）
        extra_rails = self._load_extra_rails_from_env()
        if extra_rails:
            rails_list.extend(extra_rails)
            logger.info("[JiuWenClawDeepAdapter] Extra rails loaded from env: %s",
                        [type(r).__name__ for r in extra_rails])

        if self._task_execution_rail is None:
            logger.warning("[JiuWenClawDeepAdapter] TaskExecutionRail missing after _build_agent_rails")
        else:
            logger.info("[JiuWenClawDeepAdapter] TaskExecutionRail attached to adapter")
        return rails_list

    def _make_deep_agent_config(
            self,
            *,
            model: Model,
            config: dict[str, Any],
            agent_card: AgentCard,
            tool_cards: list[Any],
            rails: list[Any] | None = None,
    ) -> DeepAgentConfig:
        """与 create_deep_agent() 中 DeepAgentConfig 构造保持一致."""
        resolved_language = self._resolve_runtime_language()
        config_base = get_config()
        workspace_obj = Workspace(
            root_path=self._workspace_dir or "./",
            language=resolved_language
        )
        normalized_tool_cards = [
            tool.card if hasattr(tool, "card") else tool
            for tool in (tool_cards or [])
        ]
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            system_prompt=build_identity_prompt(
                mode="agent.fast",
                language=self._resolve_prompt_language(),
                channel=(
                    "acp" if self._is_acp_tool_profile(self._instance_overrides)
                    else self._resolve_prompt_channel()
                ),
            ),
            context_engine_config=_deep_agent_context_engine_config(config),
            enable_task_loop=config.get("enable_task_loop", True),
            max_iterations=config.get("max_iterations", 15),
            subagents=self._build_configured_subagents(model, config, config_base),
            tools=normalized_tool_cards,
            workspace=workspace_obj,
            skills=None,
            backend=None,
            sys_operation=self._sys_operation,
            language=resolved_language,
            prompt_mode=None,
            rails=rails,
            vision_model_config=self._vision_model_config,
            audio_model_config=self._audio_model_config,
            completion_timeout=config.get("completion_timeout", 21600.0),
        )

    def _update_permission_rail(self, config_base: dict[str, Any] | None) -> None:
        """原地更新已有 PermissionRail 配置，或在首次启用时新建。"""
        from jiuwenclaw.agentserver.permissions.config_loader import get_effective_permissions_config

        permission_config = get_effective_permissions_config()
        model_name = (config_base or {}).get("models", {}).get(
            "default", {}).get("model_client_config", {}).get("model_name", "gpt-4")
        if self._permission_rail is not None:
            self._permission_rail.update_config(
                permission_config,
                llm=self._model,
                model_name=model_name,
            )
            logger.info("[JiuWenClawDeepAdapter] _permission_rail config hot-updated")
        else:
            self._permission_rail = build_permission_rail(
                config=config_base, llm=self._model,
                model_name=model_name,
            )
            if self._permission_rail is not None:
                logger.info("[JiuWenClawDeepAdapter] _permission_rail newly created on hot-reload")

    async def _get_current_agent_rails(
        self,
        config: dict[str, Any],
        config_base: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Return rail instances that need to be re-initialized on hot reload.

        SkillUseRail, ContextEngineeringRail, and MemoryRail are rebuilt on config reload.
        All other rails read language dynamically from system_prompt_builder.language
        and are updated in-place where needed — they are NOT passed to configure()
        so their existing registered state is preserved without an uninit/init cycle.
        """
        # 触发钩子获取扩展目录（与 create_instance 保持一致）
        extra_skill_dir: str | None = None
        custom_home_dir: str | None = None
        try:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            from jiuwenclaw.schema.hooks_context import SystemPromptHookContext
            from jiuwenclaw.schema import AgentServerHookEvents
            
            context = SystemPromptHookContext()
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.BEFORE_SYSTEM_PROMPT_BUILD, context
            )
            extra_skill_dir = context.skill_dir
            custom_home_dir = context.home_dir
            
            logger.info(
                "[JiuWenClawDeepAdapter] reload_agent_config: BEFORE_SYSTEM_PROMPT_BUILD triggered, "
                "skill_dir=%s, home_dir=%s",
                extra_skill_dir, custom_home_dir
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] reload_agent_config hook trigger failed: %s", exc)

        # Apply in-place updates to skill_evolution_rail (no re-init needed).
        if self._skill_evolution_rail is not None:
            self._skill_evolution_rail.update_llm(self._model, config.get("model_name", "gpt-4"))
            _env_auto_scan = os.getenv("EVOLUTION_AUTO_SCAN")
            if _env_auto_scan is not None:
                self._skill_evolution_rail.auto_scan = _env_auto_scan.lower() in ("true", "1", "yes")

        self._skill_rail = self._build_skill_rail(
            config,
            include_tools=self._skill_include_harness_fs_tools(),
            include_skill_body_tools=self._skill_include_skill_body_tools(),
            extra_skill_dir=extra_skill_dir,
        )

        # 更新 RuntimePromptRail 的 custom_home_dir（原地更新，无需重建）
        if self._runtime_prompt_rail is not None:
            self._runtime_prompt_rail.set_custom_home_dir(custom_home_dir)
            if custom_home_dir:
                logger.info(
                    "[JiuWenClawDeepAdapter] RuntimePromptRail custom_home_dir updated on hot-reload: %s",
                    custom_home_dir
                )

        if not self._filesystem_rail_enabled_for_profile():
            self._filesystem_rail = None

        self._update_permission_rail(config_base)

        # Update disabled_tools_rail config in-place (no re-init needed)
        disabled_tools_rail_newly_created = False
        if self._disabled_tools_rail is not None:
            disabled_list = config.get("disabled_tools", [])
            self._disabled_tools_rail.update_config(disabled_list)
        else:
            # 使用统一的 build 方法创建（与冷启动行为一致）
            self._disabled_tools_rail = self._build_disabled_tools_rail(config)
            if self._disabled_tools_rail is not None:
                disabled_tools_rail_newly_created = True
                logger.info("[JiuWenClawDeepAdapter] _disabled_tools_rail newly created on hot-reload")

        rails_list = []
        if self._skill_rail is not None:
            rails_list.append(self._skill_rail)
        if self._context_engineering_rail is not None:
            rails_list.append(self._context_engineering_rail)
        if self._memory_rail is not None:
            rails_list.append(self._memory_rail)
        if self._lsp_rail is not None:
            rails_list.append(self._lsp_rail)
        if self._avatar_rail is not None:
            rails_list.append(self._avatar_rail)
        if self._permission_rail is not None:
            rails_list.append(self._permission_rail)
        # core会先卸载与rails_list同类的已注册rail，再加载rails_list中的rail。
        # 但需要注意，这里不能传一个与已注册的rail相同的对象。否则core只会进行卸载，不会进行加载。
        # 如果你要更新rail，就传一个新的对象；如果不要更新，就不传；如果需要仅卸载，就传原来的rail对象。
        if disabled_tools_rail_newly_created and self._disabled_tools_rail is not None:
            rails_list.append(self._disabled_tools_rail)
        return rails_list

    async def _get_tool_cards(self, agent_id: str, *, mode: str = "agent.plan"):
        """Get tool cards."""
        tool_cards = []

        for tool in build_jiuwen_harness_named_web_tools(
                agent_id=agent_id,
                language=self._resolve_runtime_language(),
        ):
            registered = _ensure_tool_registered(tool)
            tool_cards.append(registered.card)

        # 付费搜索工具：有任意一个付费 key 就注册
        if any(
                os.environ.get(key)
                for key in ("BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY")
        ):
            self._paid_search_tool = WebPaidSearchTool(language=self._resolve_runtime_language(), agent_id=agent_id)
            registered = _ensure_tool_registered(self._paid_search_tool)
            self._paid_search_tool = registered
            tool_cards.append(registered.card)
            self._paid_search_registered = True

        self._petal_search_tools = []
        self._petal_search_registered = False
        if enable_petal_search():
            try:
                registered = _ensure_tool_registered(mcp_petal_search)
                tool_cards.append(registered.card)
                self._petal_search_tools = [registered]
                self._petal_search_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] petal search tool registration failed: %s",
                    exc,
                )

        self._vision_tools = []
        self._vision_tools_registered = False
        if self._vision_model_config is not None:
            try:
                for tool in create_vision_tools(
                        language=self._resolve_runtime_language(),
                        vision_model_config=self._vision_model_config,
                        agent_id=agent_id
                ):
                    registered = _ensure_tool_registered(tool)
                    tool_cards.append(registered.card)
                    self._vision_tools.append(registered)
                self._vision_tools_registered = bool(self._vision_tools)
            except Exception as exc:
                self._vision_tools = []
                logger.warning(
                    "[JiuWenClawDeepAdapter] vision tools registration failed: %s",
                    exc,
                )

        self._audio_tools = []
        self._audio_tools_registered = False
        try:
            audio_tools = self._iter_runtime_audio_tools(agent_id)
            self._audio_tools = []
            for tool in audio_tools:
                registered = _ensure_tool_registered(tool)
                tool_cards.append(registered.card)
                self._audio_tools.append(registered)
            self._audio_tools_registered = bool(self._audio_tools)
        except Exception as exc:
            self._audio_tools = []
            logger.warning(
                "[JiuWenClawDeepAdapter] audio tools registration failed: %s",
                exc,
            )

        self._video_tool_registered = False
        if self._video_model_config:
            try:
                registered = _ensure_tool_registered(video_understanding)
                tool_cards.append(registered.card)
                self._video_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] video tool registration failed: %s",
                    exc,
                )

        # 小艺手机端工具：由 channels.xiaoyi.phone_tools_enabled 控制
        config_base = get_config() or {}
        xiaoyi_phone_tools_enabled = (
            config_base.get("channels", {}).get("xiaoyi", {}).get("phone_tools_enabled", False)
        )
        if xiaoyi_phone_tools_enabled and not self._xiaoyi_phone_tools_registered:
            _xiaoyi_tools = [
                get_user_location,
                create_note, search_notes, modify_note,
                create_calendar_event, search_calendar_event,
                search_contact,
                search_photo_gallery, upload_photo,
                search_file, upload_file,
                call_phone,
                send_message, search_message,
                create_alarm, search_alarms, modify_alarm, delete_alarm,
                query_collection, add_collection, delete_collection,
                save_media_to_gallery, save_file_to_file_manager,
                convert_timestamp_to_utc8_time,
                view_push_result,
                image_reading,
                xiaoyi_gui_agent,
            ]
            try:
                for xt in _xiaoyi_tools:
                    registered = _ensure_tool_registered(xt)
                    tool_cards.append(registered.card)
                self._xiaoyi_phone_tools_registered = True
                logger.info(
                    "[JiuWenClawDeepAdapter] %d xiaoyi phone tools registered", len(_xiaoyi_tools)
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] xiaoyi phone tools registration failed: %s", exc
                )

        try:
            skill_toolkit = SkillToolkit(
                manager=self._skill_manager,
                service_id=self._service_id,
                agent_id=self._agent_id,
                on_installed_skills_changed=self.refresh_enabled_skills_from_db,
            )
            skill_tool_names: list[str] = []
            for tool in skill_toolkit.get_tools():
                registered = _ensure_tool_registered(tool)
                tool_cards.append(registered.card)
                skill_tool_names.append(registered.card.name)
            logger.info(
                "[JiuWenClawDeepAdapter] SkillToolkit registered: tools=%s",
                skill_tool_names,
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] skill tools registration failed: %s", exc)

        # AskUserQuestion 工具：用于 LLM 主动结构化追问并等待用户回答
        try:
            ask_tool = get_ask_user_question_tool()
            registered = _ensure_tool_registered(ask_tool)
            tool_cards.append(registered.card)
            logger.info("[JiuWenClawDeepAdapter] AskUserQuestion tool registered")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] AskUserQuestion tool registration failed: %s", exc)

        # DeepResearch 执行工具
        try:
            for tool in get_deepresearch_tools():
                registered = _ensure_tool_registered(tool)
                tool_cards.append(registered.card)
            logger.info(
                "[JiuWenClawDeepAdapter] deepresearch tools registered successfully",
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] deepresearch tools registration failed: %s",
                exc,
            )

        return tool_cards

    def _build_cron_tools(self) -> list[Any]:
        """Build cron tools from the shared runtime bridge."""
        if not should_register_cron_tools():
            logger.info("[JiuWenClawDeepAdapter] skip cron tool build: disabled by env")
            return []
        agent_id = self._instance.card.id if self._instance else None
        return self._cron_runtime.build_tools(context=self._runtime_cron_tool_context, agent_id=agent_id)

    async def _proc_context_compaction(self) -> None:
        """Backward-compatible no-op hook for tests and legacy call sites."""
        return None

    @staticmethod
    def _mask_model_secret(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "(empty)"
        if len(text) <= 8:
            return "***"
        return f"{text[:4]}***{text[-2:]}"

    def _log_active_model_on_startup(self, *, phase: str = "create_instance") -> None:
        """记录四类模型槽位的 source / template_name / template_id / model_name（启动日志）。"""
        from jiuwenclaw.agentserver.enterprise_config.apply_models import (
            SLOT_TO_CONFIG_KEY,
        )
        from jiuwenclaw.agentserver.enterprise_config.loader import TemplateRefSlot

        empty = "(empty)"
        config = self._startup_config_base if isinstance(self._startup_config_base, dict) else {}
        models_section = config.get("models") if isinstance(config.get("models"), dict) else {}
        enterprise_models: dict[str, Any] = {}
        if self._enterprise_config is not None:
            enterprise_models = getattr(self._enterprise_config, "models", None) or {}

        default_entry: dict[str, Any] = {}
        entries = get_default_models(config) if config else []
        if entries and isinstance(entries[0], dict):
            default_entry = entries[0]

        parts = [f"source={self._model_config_source or empty}"]
        for slot in (
            TemplateRefSlot.DEFAULT_MODEL,
            TemplateRefSlot.VISION_MODEL,
            TemplateRefSlot.AUDIO_MODEL,
            TemplateRefSlot.VIDEO_MODEL,
        ):
            config_key = SLOT_TO_CONFIG_KEY[slot]
            template_name = ""
            template_id = ""
            model_name = ""

            slot_entities = enterprise_models.get(slot.value)
            entity: dict[str, Any] | None = None
            if isinstance(slot_entities, list) and slot_entities:
                first = slot_entities[0]
                entity = first if isinstance(first, dict) else None
            elif isinstance(slot_entities, dict):
                entity = slot_entities
            if isinstance(entity, dict):
                template_name = str(entity.get("template_name") or "").strip()
                template_id = str(entity.get("template_id") or "").strip()
                model_name = str(entity.get("model_id") or "").strip()

            if config_key == "default":
                template_name = template_name or str(default_entry.get("template_name") or "").strip()
                template_id = template_id or str(default_entry.get("template_id") or "").strip()
                default_mcc = default_entry.get("model_client_config")
                if isinstance(default_mcc, dict):
                    model_name = model_name or str(default_mcc.get("model_name") or "").strip()

            section = models_section.get(config_key)
            if isinstance(section, dict):
                template_name = template_name or str(section.get("template_name") or "").strip()
                template_id = template_id or str(section.get("template_id") or "").strip()
                section_mcc = section.get("model_client_config")
                if isinstance(section_mcc, dict):
                    model_name = (
                        str(section_mcc.get("model_name") or "").strip() or model_name
                    )

            parts.append(
                f"{config_key}(template_name={template_name or empty}, "
                f"template_id={template_id or empty}, "
                f"model_name={model_name or empty})"
            )

        logger.info(
            "[JiuWenClawDeepAdapter] Agent 已启动(%s)，当前使用模型: %s",
            phase,
            "; ".join(parts),
        )

    def _merge_enterprise_models_into_config(
        self, config_base: dict[str, Any]
    ) -> dict[str, Any]:
        """若已加载 ``_enterprise_config``，将其模型槽位覆盖到 config 快照上。"""
        if self._enterprise_config is None:
            return config_base
        from jiuwenclaw.agentserver.enterprise_config.apply_models import (
            apply_enterprise_models_to_config,
        )

        merged, applied = apply_enterprise_models_to_config(
            config_base, self._enterprise_config
        )
        if applied:
            self._model_config_source = "enterprise_policy"
            logger.info(
                "[JiuWenClawDeepAdapter] using enterprise model config: slots=%s",
                list(self._enterprise_config.models),
            )
        return merged

    async def _load_enterprise_config(self, request: AgentRequest) -> None:
        """按当前请求的 ``params`` 从 Gateway DB 加载生效企业策略到 ``self._enterprise_config``。"""
        self._enterprise_config = await self._do_load_enterprise_config(request)

    async def _do_load_enterprise_config(self, request: AgentRequest) -> Any | None:
        """执行企业配置加载并返回结果(不赋值, 供缓存版复用)。"""
        try:
            from jiuwenclaw.agentserver.enterprise_config import (
                DEFAULT_AGENT_LOAD_SLOTS,
                load_effective_enterprise_config,
            )
        except ImportError as exc:
            logger.error("[JiuWenClawDeepAdapter] enterprise_config unavailable: %s", exc)
            return None

        loaded = await load_effective_enterprise_config(
            request,
            DEFAULT_AGENT_LOAD_SLOTS,
        )
        if loaded is None:
            p = request.params
            logger.warning(
                "[JiuWenClawDeepAdapter] no effective enterprise config loaded "
                "(group_id=%s bot_id=%s user_id=%s)",
                p.get("group_id"),
                p.get("bot_id"),
                p.get("user_id"),
            )
            return None

        logger.info(
            "[JiuWenClawDeepAdapter] enterprise config loaded: template_ref=%s models=%s",
            loaded.template_ref,
            list(loaded.models),
        )
        return loaded

    async def _load_enterprise_config_cached(self, request: AgentRequest) -> None:
        """租户缓存版: 同路由键 TTL 内复用, 首次构建单飞(消灭并发新 session 重复查库)。"""
        from jiuwenclaw.agentserver.deep_agent.tenant_assembly import routing_cache_key

        async def _build() -> Any | None:
            return await self._do_load_enterprise_config(request)

        loaded, cache_hit = await self._assembly_cache.get_enterprise_config(
            routing_cache_key(request), _build
        )
        self._enterprise_config = loaded
        logger.info(
            "[AgentPerf] assembly ent_cfg: hit=%s request_id=%s",
            cache_hit, getattr(request, "request_id", ""),
        )

    def _runtime_agent_scope_id(self) -> str:
        agent_id = str(self._agent_id or "").strip()
        service_id = str(self._service_id or "").strip()
        if service_id and agent_id:
            return f"{service_id}_{agent_id}"
        return agent_id or "jiuwenclaw"

    async def _sync_skills_cached(self, skill_config: Any) -> Any:
        """租户缓存版白名单同步: 签名未变直接复用, 变更后重建(单飞)。"""
        from jiuwenclaw.agentserver.deep_agent.tenant_assembly import skill_whitelist_signature

        async def _build() -> Any:
            return await SkillWhitelistSynchronizer(
                self._workspace_dir,
                service_id=self._service_id,
                agent_id=self._agent_id,
            ).sync(skill_config)

        sync_result, cache_hit = await self._assembly_cache.get_skill_sync(
            skill_whitelist_signature(self._workspace_dir, skill_config), _build
        )
        logger.info("[AgentPerf] assembly skill_sync: hit=%s", cache_hit)
        return sync_result

    async def create_instance(self, config: dict[str, Any] | None = None, *, mode: str = "agent.plan") -> None:
        """初始化 DeepAgent 实例.

        Args:
            config: 可选配置，支持以下字段：
                - agent_name: Agent 名称，默认 "main_agent"。
                - workspace_dir: 工作区目录，默认 "workspace/agent"。
                - enterprise_routing: 企业策略路由上下文（group_id/bot_id/user_id 等）。
                - enabled_skills: Skill 白名单目录名
                - request: 可选 AgentRequest（创建时按 ``params`` 加载企业配置并合并模型）。
                - 其余字段透传给 DeepAgentConfig。
            mode: 实例化模式，支持 "claw"（默认，使用 create_deep_agent）和 "code"（使用 create_code_agent）。
        """
        _t_total = time.monotonic()
        await self.set_checkpoint()
        _t_ckpt = time.monotonic()

        self._instance_overrides = dict(config) if isinstance(config, dict) else {}
        config_base = get_config()
        bootstrap_request = self._instance_overrides.pop("request", None)
        _rid = getattr(bootstrap_request, "request_id", "") or "?"
        _t_ent0 = time.monotonic()
        if bootstrap_request is not None:
            if self._assembly_cache is not None:
                await self._load_enterprise_config_cached(bootstrap_request)
            else:
                await self._load_enterprise_config(bootstrap_request)
        _t_ent = time.monotonic()
        config_base = self._merge_enterprise_models_into_config(config_base)
        self._refresh_multimodal_configs(config_base)
        self._startup_config_base = config_base
        self._log_active_model_on_startup(phase=f"create_instance:{mode}")
        config = config_base.get('react', {}).copy()
        self._config_cache = config.copy()
        self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))

        _t_skill0 = time.monotonic()
        if is_skill_whitelist_tenant(self._agent_id, self._service_id):
            enterprise_skills: list[dict[str, Any]] = []
            if self._enterprise_config is not None:
                enterprise_skills = getattr(self._enterprise_config, "skill_whitelist", None)
            skill_config = parse_agent_skill_whitelist(self._agent_id, self._service_id, enterprise_skills)
            if self._assembly_cache is not None:
                sync_result = await self._sync_skills_cached(skill_config)
            else:
                sync_result = await SkillWhitelistSynchronizer(
                    self._workspace_dir,
                    service_id=self._service_id,
                    agent_id=self._agent_id,
                ).sync(skill_config)
            if sync_result.errors:
                logger.warning(
                    "[SkillWhitelist] sync partial errors: agent_id=%s service_id=%s errors=%s",
                    self._agent_id,
                    self._service_id,
                    sync_result.errors,
                )
            if sync_result.enabled_skill_dirs is not None:
                self._enabled_skills = [str(name) for name in sync_result.enabled_skill_dirs if str(name).strip()]
            self._prebuilt_skills = {
                str(name) for name in sync_result.prebuilt_skill_dirs if str(name).strip()
            }
        _t_skill = time.monotonic()
        # Keep constructor-injected tenant workspace by default.
        # Only override when request explicitly provides workspace_dir.
        configured_workspace = self._instance_overrides.get("workspace_dir")
        if configured_workspace is not None:
            self._workspace_dir = configured_workspace

        try:
            _t_model0 = time.monotonic()
            model = self._create_model(config_base)
            _t_model = time.monotonic()
        except Exception as exc:
            logger.error(
                "[JiuWenClawDeepAdapter] create_instance 模型初始化失败(%s): %s",
                mode,
                exc,
            )
            raise
        agent_card = AgentCard(name=self._agent_name, id=self._runtime_agent_scope_id())

        _t_tools0 = time.monotonic()
        tool_cards = await self._get_tool_cards(agent_card.id, mode=mode)
        _t_tools = time.monotonic()
        logger.info("[JiuWenClawDeepAdapter] Agent card id: %s", agent_card.id)
        self._tool_cards = tool_cards

        from jiuwenclaw.agentserver.permissions.config_loader import get_effective_permissions_config

        _t_perm0 = time.monotonic()
        permissions_cfg = get_effective_permissions_config()
        init_permission_engine(permissions_cfg)
        _t_perm = time.monotonic()
        logger.info(
            "[JiuWenClawDeepAdapter] Permission engine initialized: enabled=%s",
            permissions_cfg.get("enabled", True),
        )

        # 触发 BEFORE_SYSTEM_PROMPT_BUILD 钩子获取扩展目录
        _t_hook0 = time.monotonic()
        extra_skill_dir: str | None = None
        custom_home_dir: str | None = None
        try:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            from jiuwenclaw.schema.hooks_context import SystemPromptHookContext
            from jiuwenclaw.schema import AgentServerHookEvents
            
            context = SystemPromptHookContext()
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.BEFORE_SYSTEM_PROMPT_BUILD, context
            )
            extra_skill_dir = context.skill_dir
            custom_home_dir = context.home_dir
            
            logger.info(
                "[JiuWenClawDeepAdapter] BEFORE_SYSTEM_PROMPT_BUILD triggered: "
                "skill_dir=%s, home_dir=%s",
                extra_skill_dir, custom_home_dir
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] hook trigger failed: %s", exc)
        _t_hook = time.monotonic()

        _t_rails0 = time.monotonic()
        rails_list = self._build_agent_rails(
            config, config_base, mode=mode,
            extra_skill_dir=extra_skill_dir,
            custom_home_dir=custom_home_dir,
        )
        _t_rails = time.monotonic()

        _t_sysop0 = time.monotonic()
        sys_operation = self._create_sys_operation()
        if sys_operation is None:
            raise RuntimeError("sys_operation is not available, maybe task is not running")

        self._sys_operation = sys_operation
        _t_sysop = time.monotonic()
        configured_subagents = self._build_configured_subagents(model, config, config_base)
        _t_subagents = time.monotonic()
        common_kwargs = dict(
            model=model,
            card=agent_card,
            system_prompt=build_identity_prompt(
                mode="agent.fast",
                language=self._resolve_prompt_language(),
                channel=(
                    "acp" if self._is_acp_tool_profile(self._instance_overrides)
                    else self._resolve_prompt_channel()
                ),
            ),
            tools=tool_cards if tool_cards else [],
            subagents=configured_subagents,
            rails=rails_list if rails_list else [],
            enable_task_loop=config.get("enable_task_loop", True),
            max_iterations=config.get("max_iterations", 15),
            workspace=Workspace(
                root_path=self._workspace_dir or "./",
                language=self._resolve_runtime_language(),
            ),
            sys_operation=sys_operation,
            language=self._resolve_runtime_language(),
        )

        _t_agent0 = time.monotonic()
        if mode == "code":
            self._instance = create_code_agent(**common_kwargs)
        else:
            self._instance = create_deep_agent(
                **common_kwargs,
                context_engine_config=_deep_agent_context_engine_config(config),
                vision_model_config=self._vision_model_config,
                audio_model_config=self._audio_model_config,
                completion_timeout=config.get("completion_timeout", 21600.0),
            )
        _t_agent = time.monotonic()
        logger.info("[JiuWenClawDeepAdapter] 初始化完成: agent_name=%s", self._agent_name)

        # 动态加载用户自定义的 Rail 扩展
        _t_urails0 = time.monotonic()
        await self.load_user_rails()
        _t_urails = time.monotonic()

        # Initialize fork_agent tools
        self._init_subagent_tools()
        _t_subtools = time.monotonic()
        logger.info(
            "[AgentPerf] deep_create_instance: request_id=%s agent=%s total_ms=%.1f "
            "ckpt_ms=%.1f ent_cfg_ms=%.1f skill_sync_ms=%.1f model_ms=%.1f "
            "tool_cards_ms=%.1f perm_ms=%.1f hook_ms=%.1f rails_ms=%.1f "
            "sysop_ms=%.1f subagents_ms=%.1f create_agent_ms=%.1f "
            "user_rails_ms=%.1f subagent_tools_ms=%.1f",
            _rid, self._agent_name, (_t_subtools - _t_total) * 1000,
            (_t_ckpt - _t_total) * 1000,
            (_t_ent - _t_ent0) * 1000,
            (_t_skill - _t_skill0) * 1000,
            (_t_model - _t_model0) * 1000,
            (_t_tools - _t_tools0) * 1000,
            (_t_perm - _t_perm0) * 1000,
            (_t_hook - _t_hook0) * 1000,
            (_t_rails - _t_rails0) * 1000,
            (_t_sysop - _t_sysop0) * 1000,
            (_t_subagents - _t_sysop) * 1000,
            (_t_agent - _t_agent0) * 1000,
            (_t_urails - _t_urails0) * 1000,
            (_t_subtools - _t_urails) * 1000,
        )

    def _init_subagent_tools(self) -> None:
        """Initialize fork_agent and spawn_subagent tools for creating subagents."""
        try:
            from openjiuwen.core.runner import Runner as RunnerClass
            from jiuwenclaw.agentserver.tools.subagent_executor import init_subagent_executor
            from jiuwenclaw.agentserver.tools.subagent_tools import fork_agent, spawn_subagent

            # Initialize the subagent executor with parent agent and model
            self._subagent_executor = init_subagent_executor(
                self._instance,
                model=self._model,  # Pass the model instance
                default_role_prompts=None,  # Can be customized later
                skill_dirs_provider=lambda: self._resolve_skill_dirs(),
                enabled_skills_provider=lambda: self._enabled_skills,
            )

            # Register fork_agent tool (ignore if already exists)
            if not RunnerClass.resource_mgr.get_tool(fork_agent.card.id):
                RunnerClass.resource_mgr.add_tool(fork_agent)
            self._instance.ability_manager.add(fork_agent.card)

            # Register spawn_subagent tool (ignore if already exists)
            if not RunnerClass.resource_mgr.get_tool(spawn_subagent.card.id):
                RunnerClass.resource_mgr.add_tool(spawn_subagent)
            self._instance.ability_manager.add(spawn_subagent.card)

            logger.info("[JiuWenClawDeepAdapter] Fork agent and spawn_subagent tools initialized")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] Failed to initialize subagent tools: %s", exc)

    async def load_user_rails(self) -> None:
        """动态加载用户自定义的 Rail 扩展."""
        try:
            manager = get_rail_manager()

            # 设置 agent 实例到 rail_manager，用于热更新
            manager.set_agent_instance(self._instance)

            extensions = manager.get_extensions()

            # 只加载配置中启用的 rail 扩展
            for ext in extensions:
                if ext["enabled"]:
                    try:
                        await manager.hot_reload_rail(ext["name"], True)
                    except Exception as e:
                        logger.error(
                            "[JiuWenClawDeepAdapter] 用户 Rail 扩展加载失败: %s, 错误: %s",
                            ext["name"],
                            e,
                        )
        except Exception as e:
            logger.error("[JiuWenClawDeepAdapter] 加载用户 Rail 扩展时发生错误: %s", e)

    async def reload_agent_config(
            self,
            config_base: dict[str, Any] | None = None,
            env_overrides: dict[str, Any] | None = None,
    ) -> None:
        """从 config.yaml 重新加载配置，通过 DeepAgent.configure() 热更新当前实例（不新建 DeepAgent）。

        DeepAgent.configure() 现在自动处理 rail 生命周期：保留旧已注册 rails 的注销上下文，
        并在下次 _ensure_initialized() 时先卸载旧回调，再注册新的 rails。

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
        """
        if self._instance is None:
            raise RuntimeError("JiuWenClawDeepAdapter 未初始化，请先调用 create_instance()")
        clear_config_cache()
        clear_embed_config_db_cache()
        clear_memory_manager_cache()

        if env_overrides is not None:
            if not isinstance(env_overrides, dict):
                raise TypeError("env_overrides must be a dict when provided")
            for env_key, env_value in env_overrides.items():
                set_local_config(env_key, env_value)

        if config_base is None:
            config_base = get_config()
        elif not isinstance(config_base, dict):
            raise TypeError("config_base must be a dict when provided")
        else:
            config_base = resolve_env_vars(config_base)

        # 同步扩展配置到 ExtensionRegistry
         # Gateway 已解密 extension_security_configs，AgentServer 直接使用明文
        try:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            registry = ExtensionRegistry.get_instance()
            registry.update_config(config_base)
            logger.info("[JiuWenClaw] Extension config synced to Registry")
        except Exception as exc:
            logger.warning("[JiuWenClaw] ExtensionRegistry update failed: %s", exc)

        config_base = self._merge_enterprise_models_into_config(config_base)
        self._refresh_multimodal_configs(config_base)

        config = config_base.get('react', {}).copy()
        self._config_cache = config.copy()

        model = self._create_model(config_base)
        self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))
        agent_card = AgentCard(name=self._agent_name, id='jiuwenclaw')
        self._sync_multimodal_tools_for_runtime()
        self._sync_paid_search_tool_for_runtime()
        self._sync_petal_search_tool_for_runtime()

        if not self._filesystem_rail_enabled_for_profile() and self._filesystem_rail is not None:
            try:
                await self._instance.unregister_rail(self._filesystem_rail)
            except Exception as exc:
                logger.warning("[JiuWenClawDeepAdapter] ACP filesystem rail unregister failed: %s", exc)
            self._filesystem_rail = None

        rails_list = await self._get_current_agent_rails(config, config_base)

        # 加载用户自定义的 Rail 扩展
        await self.load_user_rails()

        deep_cfg = self._make_deep_agent_config(
            model=model,
            config=config,
            agent_card=agent_card,
            tool_cards=self._tool_cards if self._tool_cards else [],
            rails=rails_list,
        )
        self._instance.configure(deep_cfg)

        logger.info("[JiuWenClawDeepAdapter] 配置已热更新（configure），未重启进程")

    def _bind_runtime_cron_context(
            self,
            *,
            channel_id: str | None,
            session_id: str | None,
            metadata: dict[str, Any] | None,
            request_id: str | None,
            mode: str | None,
            params: dict[str, Any] | None = None,
    ) -> tuple[Token[str], Token[str | None], Token[dict[str, Any] | None], Token[str | None], Any]:
        from jiuwenclaw.agentserver import plan_todo_context as _plan_todo
        from jiuwenclaw.gateway.cron.enterprise_gate import extract_routing_triple

        normalized_channel = str(channel_id or "").strip() or CronTargetChannel.WEB.value
        normalized_mode = str(mode).strip() if isinstance(mode, str) and mode.strip() else None
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else None
        if normalized_metadata is None:
            normalized_metadata = {}
        if isinstance(request_id, str) and request_id.strip():
            normalized_metadata["request_id"] = request_id.strip()
        # 企业三元组：params → metadata → metadata.query（与 enterprise_config 同源）
        g, b, u = extract_routing_triple(params or {}, normalized_metadata)
        if g:
            normalized_metadata["group_id"] = g
        if b:
            normalized_metadata["bot_id"] = b
        if u:
            normalized_metadata["user_id"] = u
        # 设置 DeepResearch 路由上下文
        dr_token = push_deepresearch_route(
            request_id=request_id or "",
            channel_id=normalized_channel,
            session_id=session_id or "",
        )
        return (
            CRON_TOOL_CHANNEL_ID.set(normalized_channel),
            CRON_TOOL_SESSION_ID.set(session_id),
            CRON_TOOL_METADATA.set(normalized_metadata),
            CRON_TOOL_MODE.set(normalized_mode),
            _plan_todo.PLAN_TODO_SESSION_ID.set(session_id or "default"),
            dr_token,
        )

    @staticmethod
    def _reset_runtime_cron_context(
            tokens: tuple[Token[str], Token[str | None], Token[dict[str, Any] | None], Token[str | None], Any],
    ) -> None:
        from jiuwenclaw.agentserver import plan_todo_context as _plan_todo

        channel_token, session_token, metadata_token, mode_token, todo_token, dr_token = tokens
        _plan_todo.PLAN_TODO_SESSION_ID.reset(todo_token)
        CRON_TOOL_MODE.reset(mode_token)
        CRON_TOOL_METADATA.reset(metadata_token)
        CRON_TOOL_SESSION_ID.reset(session_token)
        CRON_TOOL_CHANNEL_ID.reset(channel_token)
        # 重置 DeepResearch 路由上下文
        reset_deepresearch_route(dr_token)

    async def _update_rails_for_mode(self, mode: str) -> None:
        """按 mode 注册或卸载 rails。"""
        if mode == "agent.plan":
            await self._update_plan_mode_rails()
        else:
            await self._update_agent_mode_rails()

    async def _update_plan_mode_rails(self) -> None:
        """plan 模式：注册 plan 专属 rails，卸载 agent 专属资源。"""
        if self._task_planning_rail is None:
            self._task_planning_rail = self._build_task_planning_rail()
            if self._task_planning_rail is not None:
                await self._instance.register_rail(self._task_planning_rail)
                logger.info("[JiuWenClawDeepAdapter] TaskPlanningRail registered for plan mode")
        # 卸载 multi-session 工具
        for existing in list(self._instance.ability_manager.list() or []):
            if getattr(existing, "name", "").startswith(("session_new", "session_cancel", "session_list")):
                self._instance.ability_manager.remove(existing.name)
        # plan 模式，根据config选择是否注册或者卸载memory rail
        await self._handle_memory_rail_by_config("plan")
        # 外接记忆 rail（mode-independent，注册一次，跨 reload 持久）
        await self._handle_external_memory_rail_by_config()
        # 恢复上下文 rail（仅配置启用时）
        if self._config_cache.get("context_engine_config", {}).get("enabled", False):
            if self._context_engineering_rail is not None and self._context_engineering_rail_mode != "agent.plan":
                await self._instance.unregister_rail(self._context_engineering_rail)
                self._context_engineering_rail = None
                self._context_engineering_rail_mode = None
            if self._context_engineering_rail is None:
                self._context_engineering_rail = _build_context_engineering_rail(
                    self._config_cache, mode="agent.plan")
                if self._context_engineering_rail is not None:
                    self._context_engineering_rail_mode = "agent.plan"
                    await self._instance.register_rail(self._context_engineering_rail)
        elif self._context_engineering_rail is not None:
            await self._instance.unregister_rail(self._context_engineering_rail)
            self._context_engineering_rail = None
            self._context_engineering_rail_mode = None
            logger.info("[JiuWenClawDeepAdapter] ContextEngineeringRail unregistered for plan mode (disabled)")
        # 恢复自演进 rail（仅配置启用时）
        if self._skill_evolution_rail is None and self._config_cache.get("evolution", {}).get("enabled", False):
            self._skill_evolution_rail = self._build_skill_evolution_rail(self._config_cache)
            if self._skill_evolution_rail is not None:
                await self._instance.register_rail(self._skill_evolution_rail)
                logger.info("[JiuWenClawDeepAdapter] SkillEvolutionRail registered for plan mode")
        # 已使用subagent tool替代subagent rail
        if self._subagent_rail is None:
            self._subagent_rail = self._build_subagent_rail()
            if self._subagent_rail is not None:
                await self._instance.unregister_rail(self._subagent_rail)
                logger.info("[JiuWenClawDeepAdapter] SubagentRail unregistered for plan mode")
        # plan 模式下注册 skill 合规相关 rail
        if self._skill_protocol_prompt_rail is None:
            self._skill_protocol_prompt_rail = self._build_skill_protocol_prompt_rail()
            if self._skill_protocol_prompt_rail is not None:
                await self._instance.register_rail(self._skill_protocol_prompt_rail)
                logger.info(
                    "[JiuWenClawDeepAdapter] SkillProtocolPromptRail registered for plan mode"
                )
        if self._skill_compliance_rail is None:
            self._skill_compliance_rail = self._build_skill_compliance_rail()
            if self._skill_compliance_rail is not None:
                await self._instance.register_rail(self._skill_compliance_rail)
                logger.info(
                    "[JiuWenClawDeepAdapter] SkillComplianceRail registered for plan mode"
                )

    async def _update_agent_mode_rails(self) -> None:
        """agent 模式：卸载 plan 专属 rails，按需注册 agent 专属 rails。"""
        for attr, label in (
                ("_task_planning_rail", "TaskPlanningRail"),
                ("_skill_evolution_rail", "SkillEvolutionRail"),
                ("_subagent_rail", "SubagentRail"),
                ("_skill_protocol_prompt_rail", "SkillProtocolPromptRail"),
                ("_skill_compliance_rail", "SkillComplianceRail"),
        ):
            rail = getattr(self, attr)
            if rail is not None:
                await self._instance.unregister_rail(rail)
                setattr(self, attr, None)
                logger.info("[JiuWenClawDeepAdapter] %s unregistered for agent mode", label)
        # agent 模式，根据config选择是否注册或者卸载memory rail
        await self._handle_memory_rail_by_config("fast")
        # 外接记忆 rail（mode-independent，注册一次，跨 reload 持久）
        await self._handle_external_memory_rail_by_config()
        # agent/智能模式：恢复上下文 rail（仅配置启用时）
        if self._config_cache.get("context_engine_config", {}).get("enabled", False):
            if self._context_engineering_rail is not None and self._context_engineering_rail_mode == "agent.plan":
                await self._instance.unregister_rail(self._context_engineering_rail)
                self._context_engineering_rail = None
                self._context_engineering_rail_mode = None
            if self._context_engineering_rail is None:
                self._context_engineering_rail = _build_context_engineering_rail(
                    self._config_cache, mode="agent.fast")
                if self._context_engineering_rail is not None:
                    self._context_engineering_rail_mode = "agent.fast"
                    await self._instance.register_rail(self._context_engineering_rail)

    @staticmethod
    def _acp_runtime_tools_enabled(
            request_metadata: dict[str, Any] | None,
    ) -> tuple[bool, bool]:
        caps = (
            dict(request_metadata.get("acp_client_capabilities") or {})
            if isinstance(request_metadata, dict)
            else {}
        )
        logger.info(
            "[ACP] _acp_runtime_tools_enabled: metadata_keys=%s caps=%s",
            list((request_metadata or {}).keys()),
            caps,
        )

        fs_raw = caps.get("fs")
        if fs_raw is True:
            fs_enabled = True
        elif isinstance(fs_raw, dict):
            fs_enabled = bool(fs_raw.get("readTextFile") or fs_raw.get("writeTextFile"))
        else:
            fs_enabled = False

        terminal_raw = caps.get("terminal")
        if terminal_raw is True:
            terminal_enabled = True
        elif isinstance(terminal_raw, dict):
            terminal_enabled = bool(
                terminal_raw.get("create")
                or terminal_raw.get("output")
                or terminal_raw.get("waitForExit")
                or terminal_raw.get("release")
            )
        else:
            terminal_enabled = False

        return fs_enabled, terminal_enabled

    async def _update_tools_for_mode(self, mode: str, session_id: str | None, request_id: str | None) -> None:
        """按 mode 注册或卸载 multi-session 工具。"""
        if mode != "agent.fast":
            return
        if not (request_id and session_id and self._model_client_config is not None):
            return
        try:
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith(("session_new", "session_cancel", "session_list")):
                    self._instance.ability_manager.remove(existing.name)
            sub_agent_config = ReActAgentConfig(
                model_client_config=self._model_client_config,
                model_config_obj=self._model_request_config,
            )
            self._multi_session_toolkit = MultiSessionToolkit(
                session_id=session_id,
                channel_id=get_cron_tool_channel_id(),
                request_id=request_id,
                sub_agent_config=sub_agent_config,
            )
            if request_id:
                self._track_session_toolkit(request_id, session_id, self._multi_session_toolkit)
            for ms_tool in self._multi_session_toolkit.get_tools():
                if not Runner.resource_mgr.get_tool(ms_tool.card.id):
                    Runner.resource_mgr.add_tool(ms_tool)
                self._instance.ability_manager.add(ms_tool.card)
            logger.info("[JiuWenClawDeepAdapter] MultiSessionToolkit registered for agent mode")
        except Exception as exc:
            logger.error("[JiuWenClawDeepAdapter] MultiSessionToolkit 注册失败: %s", exc)

    async def _update_session_tools(
            self,
            session_id: str | None,
            request_id: str | None,
            channel_id: str | None = None,
    ) -> None:
        """注册 cron 和 send_file 工具（与 mode 无关，每次请求刷新）。"""
        # 定时工具：按当前 session 的 channel 注册（contextvar 已由 _bind_runtime_cron_context 设置）
        normalized_session_id = session_id or ""
        if (
                should_register_cron_tools()
                and not (normalized_session_id.startswith("heartbeat") or normalized_session_id.startswith("cron"))
        ):
            try:
                cron_tools = self._build_cron_tools()
                if cron_tools:
                    logger.info("[JiuWenClawDeepAdapter] Registering %d cron tools", len(cron_tools))
                    for cron_tool in cron_tools:
                        if not Runner.resource_mgr.get_tool(cron_tool.card.id):
                            Runner.resource_mgr.add_tool(cron_tool)
                        self._instance.ability_manager.add(cron_tool.card)
                    logger.info("[JiuWenClawDeepAdapter] Cron tools registered successfully")
            except Exception as exc:
                logger.error("[JiuWenClawDeepAdapter] 定时工具注册失败: %s", exc)
        elif not (normalized_session_id.startswith("heartbeat") or normalized_session_id.startswith("cron")):
            logger.info("[JiuWenClawDeepAdapter] skip cron tools registration: disabled by env")
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith("cron_"):
                    self._instance.ability_manager.remove(existing.name)

        # send_file 工具：由 channels.<channel>.send_file_allowed 控制，每次请求重新注册
        # channel_id/metadata 由调用前的 _bind_runtime_cron_context 已写入 contextvar
        config_base = get_config()
        channel = str(channel_id or self._resolve_prompt_channel(session_id) or "web").strip() or "web"
        send_file_enabled = config_base.get("channels", {}).get(channel, {}).get("send_file_allowed", False)
        
        # 如果 AGENT_RUNTIME 环境变量存在（非空），则优先使用企业配置中的 send_file_allowed
        agent_runtime_env = os.getenv("AGENT_RUNTIME", "").strip()
        if agent_runtime_env:
            logger.info(
                "[JiuWenClawDeepAdapter] AGENT_RUNTIME detected: %s, using enterprise send_file config",
                agent_runtime_env,
            )
            send_file_enabled = True
            if self._enterprise_config is not None:
                send_file_enabled = bool(self._enterprise_config.send_file_allowed)
        
        send_file_channel_allowed = send_file_enabled or channel == "officeclaw"
        has_send_file_request_context = bool(request_id and session_id)
        if send_file_channel_allowed and has_send_file_request_context:
            # send_file_to_user 工具用稳定 id 注册一次即可；每次请求只刷新 per-request 上下文。
            # send_file 在执行时从 contextvar（_bind_runtime_cron_context 设置）读取
            # request_id/session_id/channel_id/metadata，并发安全，不会串话。
            if self._send_file_toolkit is None:
                self._send_file_toolkit = SendFileToolkit(
                    request_id=request_id,
                    session_id=session_id,
                    channel_id=get_cron_tool_channel_id(),
                    metadata=get_cron_tool_metadata(),
                )
                for sf_tool in self._send_file_toolkit.get_tools(tool_id="send_file_to_user"):
                    if not Runner.resource_mgr.get_tool(sf_tool.card.id):
                        Runner.resource_mgr.add_tool(sf_tool)
                    self._instance.ability_manager.add(sf_tool.card)
            else:
                # 后续请求：只刷新 toolkit 的 fallback 上下文，不重建/不重注册，消除并发竞态
                self._send_file_toolkit.update_runtime_context(
                    request_id=request_id,
                    session_id=session_id,
                    channel_id=get_cron_tool_channel_id(),
                    metadata=get_cron_tool_metadata(),
                )
        else:
            # 当前 channel 不允许 send_file：卸载本 agent 上遗留的 send_file 工具卡
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith("send_file_to_user"):
                    self._instance.ability_manager.remove(existing.name)

    def _refresh_acp_runtime_tools(
            self,
            session_id: str | None,
            request_id: str | None,
            channel_id: str | None,
            request_metadata: dict[str, Any] | None,
    ) -> None:
        """Refresh ACP tools for the current request based on client capabilities."""
        acp_tool_names = (
            "read_text_file",
            "write_text_file",
            "create_terminal",
            "read_terminal_output",
            "wait_for_terminal_exit",
            "release_terminal",
        )
        if channel_id == "acp":
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "") in _ACP_BLOCKED_DEFAULT_TOOL_NAMES:
                    self._instance.ability_manager.remove(existing.name)
        for existing in list(self._instance.ability_manager.list() or []):
            if getattr(existing, "name", "") in acp_tool_names:
                self._instance.ability_manager.remove(existing.name)

        fs_enabled, terminal_enabled = self._acp_runtime_tools_enabled(request_metadata)
        has_runtime_capability = fs_enabled or terminal_enabled
        can_register_acp_runtime_tools = self._should_register_acp_runtime_tools(
            channel_id=channel_id,
            request_id=request_id,
            session_id=session_id,
            has_runtime_capability=has_runtime_capability,
        )
        if can_register_acp_runtime_tools:
            for tool in get_acp_output_tools(session_id=session_id, request_id=request_id):
                if tool.card.name in {"read_text_file", "write_text_file"}:
                    if not fs_enabled:
                        continue
                elif not terminal_enabled:
                    continue
                Runner.resource_mgr.add_tool(tool)
                self._instance.ability_manager.add(tool.card)

        if channel_id == "acp":
            ability_names = sorted(
                self._collect_registered_ability_names()
            )
            runtime_tool_candidates = (
                "read_text_file",
                "write_text_file",
                "create_terminal",
                "read_terminal_output",
                "wait_for_terminal_exit",
                "release_terminal",
            )
            acp_runtime_names = self._select_registered_runtime_tool_names(
                runtime_tool_candidates,
                ability_names,
            )
            logger.info(
                "[ACP] runtime tool snapshot: session_id=%s request_id=%s fs_enabled=%s terminal_enabled=%s "
                "acp_runtime_tools=%s ability_count=%d abilities=%s",
                session_id,
                request_id,
                fs_enabled,
                terminal_enabled,
                acp_runtime_names,
                len(ability_names),
                ability_names,
            )

    def _update_prompt_for_mode(self, mode: str, resolved_language: str) -> None:
        """同步 system_prompt_builder 的语言。"""
        if self._instance.system_prompt_builder is not None:
            self._instance.system_prompt_builder.language = resolved_language
        if self._instance.deep_config is not None:
            self._instance.deep_config.language = resolved_language

    async def _update_runtime_config(self, params: _RuntimeConfigParams) -> None:
        """Register per-request tools for current agent execution."""
        if self._instance is None:
            raise RuntimeError("JiuWenClawDeepAdapter 未初始化，请先调用 create_instance()")

        resolved_language = self._resolve_runtime_language()

        md = params.request_metadata or {}
        v = md.get("effective_project_dir")
        logger.info(f"get effect project dir:{v}, ori dir: {self._workspace_dir}")
        if isinstance(v, str) and v.strip():
            resolved_workspace_dir = v.strip()
        else:
            resolved_workspace_dir = self._workspace_dir

        from openjiuwen.core.sys_operation.cwd import set_cwd
        from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
            set_effective_request_workspace_dir,
        )
        from jiuwenclaw.agentserver.tools.subagent_executor import (
            set_fork_agent_executor,
        )

        set_effective_request_workspace_dir(resolved_workspace_dir)
        set_fork_agent_executor(self._subagent_executor)

        # Sync the tool CWD layer to the client-provided workspace dir so that
        # relative file paths in tool calls resolve against the correct base.
        try:
            set_cwd(resolved_workspace_dir)
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] set_cwd(%s) failed: %s", resolved_workspace_dir, exc)

        if self._runtime_prompt_rail:
            self._runtime_prompt_rail.set_language(resolved_language)
            resolved_channel = (
                str(params.channel_id or self._resolve_prompt_channel(params.session_id) or "web").strip() or "web"
            )
            self._runtime_prompt_rail.set_channel(resolved_channel)
            self._runtime_prompt_rail.set_request_system_prompt(params.request_system_prompt)
            self._runtime_prompt_rail.set_workspace_dir(resolved_workspace_dir)

        await self._update_rails_for_mode(params.mode)
        await self._update_tools_for_mode(params.mode, params.session_id, params.request_id)
        await self._update_session_tools(params.session_id, params.request_id, channel_id=params.channel_id)
        self._refresh_acp_runtime_tools(
            params.session_id,
            params.request_id,
            params.channel_id,
            params.request_metadata,
        )
        self._update_prompt_for_mode(params.mode, resolved_language)

        # user_todos 工具注册（工具只注册一次，channel_id 每次请求由 ContextVar 更新）
        try:
            from jiuwenclaw.agentserver.tools.user_todo_tool import (
                get_decorated_tools as _get_user_todo_tools,
                set_global_workspace_dir as _set_user_todo_workspace,
                set_global_channel_id as _set_user_todo_channel_id,
            )
            _set_user_todo_workspace(self._workspace_dir)
            _set_user_todo_channel_id(get_cron_tool_channel_id())
            for tool in _get_user_todo_tools():
                if not Runner.resource_mgr.get_tool(tool.card.id):
                    Runner.resource_mgr.add_tool(tool)
                self._instance.ability_manager.add(tool.card)
        except ImportError:
            pass

        # 处理两种场景的记忆工具移除：
        # 1. 群聊数字分身模式（group_digital_avatar=True + avatar_mode=True）：移除写入工具，但保留读取工具
        # 2. 记忆完全禁用（enable_memory=False + group_digital_avatar=True + avatar_mode=True）：移除所有记忆工具（读取和写入）
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        if perm_ctx is not None:
            # 判断是否为群聊数字分身模式
            is_group_digital_avatar = (
                    perm_ctx.group_digital_avatar
                    and perm_ctx.avatar_mode
            )

            # 判断是否为记忆完全禁用（三个条件同时满足）
            should_disable_memory = (
                    not perm_ctx.enable_memory
                    and perm_ctx.group_digital_avatar
                    and perm_ctx.avatar_mode
            )

            # 场景2：记忆完全禁用 - 移除所有记忆工具
            if should_disable_memory:
                _all_memory_tools = ("write_memory", "edit_memory", "read_memory", "memory_search", "memory_get")
                for tool_name in _all_memory_tools:
                    try:
                        self._instance.ability_manager.remove(tool_name)
                        logger.info("[JiuWenClawDeepAdapter] 记忆系统已禁用，移除 %s", tool_name)
                    except Exception:
                        pass
            # 场景1：群聊数字分身模式 - 只移除写入工具
            elif is_group_digital_avatar:
                for tool_name in ("write_memory", "edit_memory"):
                    try:
                        self._instance.ability_manager.remove(tool_name)
                        logger.info("[JiuWenClawDeepAdapter] 群聊模式下禁止写入记忆，移除 %s", tool_name)
                    except Exception:
                        pass
            # 非群聊数字分身且记忆启用时，恢复写入工具
            else:
                if is_builtin_memory_allowed(get_config()):
                    try:
                        from openjiuwen.core.memory.lite.memory_tools import (
                            get_decorated_tools as _get_sdk_memory_tools,
                        )
                        for tool in _get_sdk_memory_tools():
                            name = getattr(getattr(tool, "card", None), "name", "")
                            if name in ("write_memory", "edit_memory"):
                                self._instance.ability_manager.add(tool.card)
                    except ImportError:
                        logger.warning(
                            "[JiuWenClawDeepAdapter] 恢复写入记忆工具失败，SDK memory_tools 不可用"
                        )

    @staticmethod
    def _should_register_acp_runtime_tools(
            channel_id: str | None,
            request_id: str | None,
            session_id: str | None,
            has_runtime_capability: bool,
    ) -> bool:
        if channel_id != "acp":
            return False
        if not request_id or not session_id:
            return False
        return has_runtime_capability

    def _collect_registered_ability_names(self) -> set[str]:
        ability_names: set[str] = set()
        for card in self._instance.ability_manager.list() or []:
            ability_name = str(getattr(card, "name", "") or "").strip()
            if ability_name:
                ability_names.add(ability_name)
        return ability_names

    @staticmethod
    def _select_registered_runtime_tool_names(
            runtime_tool_candidates: tuple[str, ...],
            ability_names: set[str],
    ) -> list[str]:
        selected_names: list[str] = []
        for name in runtime_tool_candidates:
            if name in ability_names:
                selected_names.append(name)
        return selected_names

    @staticmethod
    def _revoke_main_skill_authorization(
        session_id: str | None,
        *,
        reason: str,
    ) -> None:
        try:
            from jiuwenclaw.agentserver.permissions.skill_authorization.runtime import (
                revoke_main_scope,
            )

            revoke_main_scope(session_id, reason=reason)
        except Exception:  # noqa: BLE001 — 不影响原取消/异常流程
            logger.warning(
                "[JiuWenClawDeepAdapter] dynamic authorization cleanup failed",
                exc_info=True,
            )

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """处理 interrupt 请求.

        根据 intent 分流：
        - pause: 暂停循环（不取消任务）
        - resume: 恢复已暂停的循环
        - cancel: 取消所有运行中的任务
        - supplement: 取消当前任务并清空 todo / task_plan，再启动新任务

        Args:
            request: AgentRequest，params 中可包含：
                - intent: 中断意图 ('pause' | 'cancel' | 'resume' | 'supplement')
                - new_input: 新的用户输入（用于切换任务）

        Returns:
            AgentResponse 包含 interrupt_result 事件数据
        """
        intent = request.params.get("intent", "cancel")
        new_input = request.params.get("new_input")

        success = True
        updated_todos = None

        if intent == "pause":
            # 暂停：通过 StreamEventRail 在下一个 model_call/tool_call checkpoint 阻塞
            if self._stream_event_rail is not None:
                self._stream_event_rail.pause()
                logger.info(
                    "[JiuWenClawDeepAdapter] interrupt: 已暂停执行 request_id=%s",
                    request.request_id,
                )
            message = "任务已暂停"

        elif intent == "resume":
            # 恢复：解除 StreamEventRail 的 pause 阻塞 + 清除 abort 标志
            if self._stream_event_rail is not None:
                self._stream_event_rail.resume()
                logger.info(
                    "[JiuWenClawDeepAdapter] interrupt: 已恢复执行 request_id=%s",
                    request.request_id,
                )
            message = "任务已恢复"

        elif intent == "supplement":
            # supplement: 停止当前执行并清空 todo / task_plan，避免 TaskScheduler 续跑旧计划
            # 1. 通过 rail abort 在 checkpoint 抛 CancelledError，打断当前内层执行
            if self._stream_event_rail is not None:
                self._stream_event_rail.abort()
            # 2. 终止 DeepAgent 外层 task loop
            if self._instance is not None:
                await self._instance.abort()
            # 3. 取消当前会话关联的 MultiSessionToolkit 子任务（按 request 跟踪，避免误停其它会话）
            await self._cancel_session_toolkits(request.session_id, "interrupt(supplement): ")
            self._revoke_main_skill_authorization(
                request.session_id,
                reason="task_supplemented",
            )
            AskUserQuestionRegistry.get_instance().cancel_for_session(str(request.session_id or ""))
            # 4. 标记未完成的 todo 为 cancelled 并清空 todo.json（与 cancel 一致）
            if request.session_id:
                try:
                    updated_todos = await self._cancel_pending_todos(request.session_id)
                except Exception as exc:
                    logger.warning("[JiuWenClawDeepAdapter] supplement 标记 todo cancelled 失败: %s", exc)
            await self._release_session_persistence_checkpoint(
                request.session_id,
                reason="interrupt(supplement)",
            )
            logger.info(
                "[JiuWenClawDeepAdapter] interrupt(supplement): 已停止执行并清空 todo/task_plan request_id=%s",
                request.request_id,
            )
            message = "任务已切换"

        else:
            # cancel（默认）：停止所有执行 + 清理 todo
            # 1. 通过 rail abort 在 checkpoint 抛 CancelledError，打断当前内层执行
            if self._stream_event_rail is not None:
                self._stream_event_rail.abort()
            # 2. 终止 DeepAgent 外层 task loop
            if self._instance is not None:
                await self._instance.abort()
            # 3. 取消当前会话关联的 MultiSessionToolkit 子任务（与其它 session 隔离）
            await self._cancel_session_toolkits(
                request.session_id,
                f"interrupt(cancel) request_id={request.request_id}: ",
            )
            self._revoke_main_skill_authorization(
                request.session_id,
                reason="task_cancelled",
            )
            AskUserQuestionRegistry.get_instance().cancel_for_session(str(request.session_id or ""))
            # 4. 标记未完成的 todo 为 cancelled（通知前端），并清空 todo.json
            updated_todos = None
            if request.session_id:
                try:
                    updated_todos = await self._cancel_pending_todos(request.session_id)
                except Exception as exc:
                    logger.warning("[JiuWenClawDeepAdapter] 标记 todo cancelled 失败: %s", exc)
            await self._release_session_persistence_checkpoint(
                request.session_id,
                reason="interrupt(cancel)",
            )

            logger.info(
                "[JiuWenClawDeepAdapter] interrupt(cancel): 已停止执行 request_id=%s",
                request.request_id,
            )
            if new_input:
                message = "已切换到新任务"
            else:
                message = "任务已取消"

        payload = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": success,
            "message": message,
        }

        if new_input:
            payload["new_input"] = new_input

        # cancel 后附带更新的 todo 列表，通知前端刷新
        if intent not in ("pause", "resume") and updated_todos is not None:
            payload["todos"] = updated_todos

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    @staticmethod
    def _plain_chat_should_clear_stale_interrupt(request: AgentRequest) -> bool:
        """普通用户消息（非权限/问答结构化回复）进入 Agent 前应清空 checkpoint 内工具中断。

        否则 OpenJiuwen 会把本轮 query 当作 ``ToolInterruptionState`` 的 resume 输入，
        PermissionRail 收到纯文本会报 Invalid permission confirmation payload。
        """
        sid = str(getattr(request, "session_id", "") or "")
        if sid.startswith("heartbeat"):
            return False
        params = request.params if isinstance(getattr(request, "params", None), dict) else {}
        q = params.get("query")
        if isinstance(q, InteractiveInput):
            return False
        answers = params.get("answers") or []
        if answers:
            return False
        return True

    async def _clear_session_persisted_interrupt_state(
        self,
        session_id: str | None,
        *,
        reason: str,
        clear_interrupt: bool = False,
        clear_task_plan: bool = False,
        clear_task_plan_if_todo_empty: bool = False,
    ) -> None:
        """Clear persisted session checkpoint state (interrupt / TaskPlan) in one pre_run."""
        if not session_id or self._instance is None:
            return

        should_clear_plan = clear_task_plan
        if clear_task_plan_if_todo_empty and not should_clear_plan:
            try:
                deep_config = self._instance.deep_config
                modify_tool = TodoModifyTool(
                    operation=deep_config.sys_operation,
                    workspace=str(deep_config.workspace.get_node_path(WorkspaceNode.TODO)),
                    language=self._resolve_runtime_language(),
                )
                file_path = modify_tool.file_path_for_session(session_id)
                if not os.path.isfile(file_path):
                    should_clear_plan = True
                else:
                    with open(file_path, encoding="utf-8") as f:
                        data = json.load(f)
                    should_clear_plan = not isinstance(data, list) or len(data) == 0
            except Exception:
                should_clear_plan = True

        if not clear_interrupt and not should_clear_plan:
            return

        try:
            session = create_agent_session(session_id=session_id, card=self._instance.card)
            await session.pre_run(inputs=None)
            if clear_interrupt:
                clear_session_interrupt_state(session)
            if should_clear_plan:
                state = self._instance.load_state(session)
                if state.task_plan is not None:
                    state.task_plan = None
                    self._instance.save_state(session, state)
                    session.update_state({"deep_agent_state": state.to_session_dict()})
            await session.post_run()
            cleared = []
            if clear_interrupt:
                cleared.append("interrupt")
            if should_clear_plan:
                cleared.append("task_plan")
            logger.info(
                "[JiuWenClawDeepAdapter] %s: cleared %s session_id=%s",
                reason,
                "+".join(cleared),
                session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] %s: clear persisted interrupt state failed session_id=%s error=%s",
                reason,
                session_id,
                exc,
            )

    async def _release_session_persistence_checkpoint(
        self,
        session_id: str | None,
        *,
        reason: str,
    ) -> None:
        """Release persistence checkpointer blobs for one session.

        After ``chat.interrupt(cancel|supplement)``, the next ``chat.send`` must not
        ``recover()`` an in-flight turn (tool calls / permission ASK) from KV storage.
        """
        sid = (session_id or "").strip()
        if not sid:
            return
        get_fn = getattr(CheckpointerFactory, "get_checkpointer", None)
        if not callable(get_fn):
            return
        try:
            checkpointer = get_fn()
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] %s: get checkpointer failed session_id=%s error=%s",
                reason,
                sid,
                exc,
            )
            return
        if checkpointer is None:
            return
        release_fn = getattr(checkpointer, "release", None)
        if not callable(release_fn):
            return

        agent_id = None
        if self._instance is not None:
            card = getattr(self._instance, "card", None)
            if card is not None:
                agent_id = (getattr(card, "id", None) or "").strip() or None

        try:
            if agent_id:
                await release_fn(sid, agent_id)
            else:
                await release_fn(sid)
            logger.info(
                "[JiuWenClawDeepAdapter] %s: persistence checkpoint released "
                "session_id=%s agent_id=%s",
                reason,
                sid,
                agent_id or "all",
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] %s: persistence checkpoint release failed "
                "session_id=%s error=%s",
                reason,
                sid,
                exc,
                exc_info=True,
            )

    async def abort_on_gateway_disconnect(self) -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时：与 interrupt(cancel) 同样中止 rail 与 DeepAgent 实例。"""
        if self._stream_event_rail is not None:
            self._stream_event_rail.abort()
        if self._instance is not None:
            try:
                await self._instance.abort()
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] abort_on_gateway_disconnect instance.abort failed: %s",
                    exc,
                )
        for sid in list(self._session_toolkit_requests.keys()):
            await self._cancel_session_toolkits(sid, "gateway_disconnect: ")

    def _track_session_toolkit(
        self,
        request_id: str,
        session_id: str | None,
        toolkit: MultiSessionToolkit,
    ) -> None:
        """登记本请求使用的 MultiSessionToolkit，供 interrupt / 结束时取消或解除跟踪。"""
        self._request_session_toolkits[request_id] = toolkit
        request_ids = self._session_toolkit_requests.setdefault(session_id, set())
        request_ids.add(request_id)

    def _untrack_session_toolkit(self, request_id: str) -> None:
        """请求结束后从跟踪表移除（不取消子协程；取消逻辑由 interrupt 或 toolkit 自身完成）。"""
        if not request_id:
            return
        toolkit = self._request_session_toolkits.pop(request_id, None)
        if toolkit is None:
            return
        sid = toolkit.session_id
        bucket = self._session_toolkit_requests.get(sid)
        if bucket is None:
            return
        bucket.discard(request_id)
        if not bucket:
            self._session_toolkit_requests.pop(sid, None)

    async def _cancel_session_toolkits(self, session_id: str | None, log_msg_prefix: str = "") -> None:
        """取消指定会话关联的各 request 下 MultiSessionToolkit 子协程，并解除跟踪。"""
        request_ids = list(self._session_toolkit_requests.get(session_id, set()))
        if not request_ids:
            return
        logger.info(
            "[JiuWenClawDeepAdapter] %s取消 session 子协程工具包: session_id=%s request_count=%d",
            log_msg_prefix,
            session_id,
            len(request_ids),
        )
        for rid in request_ids:
            toolkit = self._request_session_toolkits.get(rid)
            if toolkit is None:
                self._untrack_session_toolkit(rid)
                continue
            try:
                await toolkit.cancel_all_sessions()
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] %s取消 MultiSessionToolkit 失败: session_id=%s request_id=%s error=%s",
                    log_msg_prefix,
                    session_id,
                    rid,
                    exc,
                )
            finally:
                self._untrack_session_toolkit(rid)

    @staticmethod
    def _is_filled_model_credential(value: Any) -> bool:
        if value is None:
            return False
        text = str(value).strip()
        if not text:
            return False
        if text.startswith("${") and text.endswith("}"):
            return False
        return True

    @classmethod
    def _model_client_config_has_api_key(cls, mcc: Any) -> bool:
        if mcc is None:
            return False
        if isinstance(mcc, dict):
            return cls._is_filled_model_credential(mcc.get("api_key"))
        return cls._is_filled_model_credential(getattr(mcc, "api_key", None))

    def _has_valid_model_config(self) -> bool:
        """检查是否有有效的模型配置（.env、运行时 Model 或 config.yaml defaults）。"""
        if self._is_filled_model_credential(os.getenv("API_KEY")):
            return True

        if self._model_client_config_has_api_key(self._model_client_config):
            return True

        if self._model is not None and self._model_client_config_has_api_key(
            getattr(self._model, "model_client_config", None),
        ):
            return True

        for cached in self._model_cache.values():
            if self._model_client_config_has_api_key(getattr(cached, "model_client_config", None)):
                return True

        react_agent = None
        if self._instance is not None:
            react_agent = getattr(self._instance, "_react_agent", None) or getattr(
                self._instance, "react_agent", None,
            )
        if react_agent is not None:
            agent_config = getattr(react_agent, "_config", None)
            if agent_config is not None and self._model_client_config_has_api_key(
                getattr(agent_config, "model_client_config", None),
            ):
                return True

        try:
            for entry in get_default_models():
                mcc = entry.get("model_client_config") or {}
                if self._model_client_config_has_api_key(mcc):
                    return True
        except Exception:  # noqa: BLE001
            pass

        return False

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        """Handle chat.user_answer request with explicit source-based routing."""
        from jiuwenclaw.agentserver.permissions.skill_authorization.runtime import (
            resolve_subagent_approval,
        )

        params = request.params if isinstance(request.params, dict) else {}
        request_id = params.get("request_id", "")
        answers = params.get("answers", [])
        source = params.get("source", "")
        resolved = False
        if source == "skill_evolve":
            resolved = await self._handle_evolution_approval(request_id, answers)
        elif source == "skill_create":
            resolved = await self._handle_skill_create_approval(request_id, answers)
        elif source == "ask_tool":
            resolved = AskUserQuestionRegistry.get_instance().resolve(request_id, answers)
        elif source in {
            "subagent_skill_load",
            "subagent_tool_permission",
        }:
            resolved = resolve_subagent_approval(
                request_id=request_id,
                session_id=str(request.session_id or params.get("session_id") or ""),
                source=source,
                answers=answers,
                agent_scope_id=str(params.get("agent_scope_id") or ""),
            )
        else:
            # Backward compatibility: keep request_id-prefix routing for old channels/frontends.
            if request_id.startswith("skill_evolve_"):
                resolved = await self._handle_evolution_approval(request_id, answers)
            elif request_id.startswith("skill_create_"):
                resolved = await self._handle_skill_create_approval(request_id, answers)
            elif isinstance(request_id, str) and request_id.startswith(ASK_REQUEST_PREFIX):
                resolved = AskUserQuestionRegistry.get_instance().resolve(request_id, answers)
            elif isinstance(request_id, str) and (
                request_id.startswith("subagent_skill_load_")
                or request_id.startswith("subagent_tool_permission_")
            ):
                resolved = resolve_subagent_approval(
                    request_id=request_id,
                    session_id=str(request.session_id or params.get("session_id") or ""),
                    source="",
                    answers=answers,
                    agent_scope_id=str(params.get("agent_scope_id") or ""),
                )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"accepted": True, "resolved": resolved},
            metadata=request.metadata,
        )

    async def handle_heartbeat(self, request: AgentRequest) -> AgentResponse | None:
        """Handle heartbeat request. Returns None to continue normal flow.

        Injects a heartbeat prompt into the query to ensure the LLM receives
        a non-empty user message. Reading HEARTBEAT.md and injecting its content
        into the system prompt is handled by HeartbeatRail in before_model_call.
        """
        sid = str(request.session_id or "")
        if not sid.startswith("heartbeat"):
            return None

        request.params["query"] = "根据heartbeat section内容执行任务. 如果没有或内容为空, 仅回复HEARTBEAT_OK"
        logger.info(
            "[JiuWenClawDeepAdapter] heartbeat query injected:"
            " request_id=%s session_id=%s",
            request.request_id,
            request.session_id,
        )
        return None

    async def _handle_evolution_approval(self, request_id: str, answers: list) -> bool:
        """Handle evolution approval via SkillEvolutionRail.on_approve/on_reject.

        Uses the optimizer path: calls rail.on_approve() for accepted records
        which will flush to store and solidify, or rail.on_reject() to discard.
        """
        rail = self._skill_evolution_rail
        if rail is None:
            logger.warning("[JiuWenClaw] evolution approval failed: no SkillEvolutionRail")
            return False

        # Determine if user accepted (any answer contains "接收")
        accepted = any(
            isinstance(ans, dict) and "接收" in ans.get("selected_options", [])
            for ans in answers
        )

        if accepted:
            await rail.on_approve(request_id)
            logger.info("[JiuWenClaw] evolution approval accepted: request_id=%s", request_id)
        else:
            await rail.on_reject(request_id)
            logger.info("[JiuWenClaw] evolution approval rejected: request_id=%s", request_id)

        return True

    async def _handle_skill_create_approval(self, request_id: str, answers: list) -> bool:
        """Handle approval for new Skill creation proposals.

        Uses the optimizer path: calls rail.on_approve_new_skill() for accepted
        proposals which will create the skill, or rail.on_reject_new_skill() to discard.
        """
        rail = self._skill_evolution_rail
        if rail is None:
            logger.warning("[JiuWenClaw] skill create approval failed: no SkillEvolutionRail")
            return False

        # Determine if user accepted (any answer contains "Create")
        accepted = any(
            isinstance(ans, dict) and "Create" in ans.get("selected_options", [])
            for ans in answers
        )

        if accepted:
            await rail.on_approve_new_skill(request_id)
            logger.info("[JiuWenClaw] skill create accepted: request_id=%s", request_id)
        else:
            await rail.on_reject_new_skill(request_id)
            logger.info("[JiuWenClaw] skill create rejected: request_id=%s", request_id)

        return True

    # ------------------------------------------------------------------
    # /evolve, /evolve_list, /evolve_simplify & /solidify command handlers
    # ------------------------------------------------------------------

    async def _handle_evolve_command(self, query: str, session_id: str) -> dict[str, Any]:
        """/evolve [list | <skill_name>] handler using the optimizer path.

        Uses SkillEvolutionRail.generate_and_emit_experience to stage records
        in memory and emit approval events.

        Returns a result dict.  When evolution records are generated the dict
        includes an ``approval_chunks`` list so the caller can forward the
        approval event to the frontend.
        """
        rail = self._skill_evolution_rail
        assert rail is not None
        store = rail.store

        skill_names = store.list_skill_names()

        parts = query.split(maxsplit=1)
        skill_arg = parts[1].strip() if len(parts) > 1 else ""

        # --- /evolve list (or bare /evolve) ---
        if not skill_arg or skill_arg == "list":
            if not skill_names:
                return {
                    "output": "当前 skills_base_dir 下未找到任何 Skill 目录。",
                    "result_type": "answer",
                }
            summary = await store.list_pending_summary(skill_names)
            return {
                "output": f"**Skills 演进记录：**\n\n{summary}",
                "result_type": "answer",
            }

        # --- /evolve <skill_name> ---
        skill_name = skill_arg
        if skill_name not in skill_names:
            available = "、".join(skill_names) or "（无可用 Skill）"
            return {
                "output": (
                    f"在 skills_base_dir 下未找到 Skill '{skill_name}'。\n"
                    f"当前可用 Skill：{available}\n"
                    f"可使用 /evolve list 查看所有记录。"
                ),
                "result_type": "error",
            }

        # 1) Collect conversation messages from the context engine cache
        parsed_messages = self._collect_messages_for_evolve(session_id)
        if not parsed_messages:
            return {
                "output": "当前对话无可用消息，无法检测演进信号。请先与 Agent 进行对话后再执行 /evolve。",
                "result_type": "answer",
            }

        # 2) Detect signals (reuse rail's dedup set)
        existing_skills = {n for n in skill_names if store.skill_exists(n)}
        detector = SignalDetector(existing_skills=existing_skills)
        detected = detector.detect(parsed_messages)

        new_signals = [
            sig for sig in detected
            if (sig.signal_type, sig.excerpt[:100]) not in rail.processed_signal_keys
        ]
        for sig in new_signals:
            rail.processed_signal_keys.add((sig.signal_type, sig.excerpt[:100]))

        attributed = [s for s in new_signals if s.skill_name == skill_name]
        if not attributed:
            return {
                "output": "当前对话未发现明确的演进信号（无工具执行失败、无用户纠正）。\n",
                "result_type": "answer",
            }

        # 3) Generate experience records and emit approval event
        try:
            has_records = await rail.generate_and_emit_experience(
                skill_name, attributed, parsed_messages
            )
        except Exception as exc:
            logger.warning("[JiuWenClaw] evolve generate failed (skill=%s): %s", skill_name, exc)
            return {
                "output": f"演进经验生成失败：{exc}",
                "result_type": "error",
            }

        if not has_records:
            return {
                "output": "当前对话未发现明确的演进信号（无工具执行失败、无用户纠正）。\n",
                "result_type": "answer",
            }

        # 5) Drain the buffered approval event
        events = rail.drain_pending_approval_events()
        if not events:
            return {
                "output": "演进经验生成失败：无法创建审批事件。",
                "result_type": "error",
            }

        # 6) Build response with approval chunks
        event = events[0]
        payload = event.payload or {}
        request_id = payload.get("request_id", "")
        questions = payload.get("questions", [])

        # Build summary from questions
        summaries = "\n".join(
            f"  {i + 1}. {q.get('question', '')[:200]}"
            for i, q in enumerate(questions)
        )

        return {
            "output": (
                f"已为 Skill '{skill_name}' 生成 {len(questions)} 条演进经验，请审批：\n"
                f"{summaries}"
            ),
            "result_type": "answer",
            "approval_chunks": [
                {
                    "event_type": "chat.ask_user_question",
                    "request_id": request_id,
                    "questions": questions,
                }
            ],
        }

    def _collect_messages_for_evolve(self, session_id: str) -> list[dict]:
        """Retrieve and normalize cached conversation messages for /evolve."""
        if self._instance is None or self._instance.react_agent is None:
            return []

        context_engine = self._instance.react_agent.context_engine
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            return []

        try:
            raw_messages = list(context.get_messages())
        except Exception as exc:
            logger.debug("[JiuWenClaw] _collect_messages_for_evolve failed: %s", exc)
            return []

        return SkillEvolutionRail._parse_messages(raw_messages)

    async def _handle_solidify_command(self, query: str) -> dict[str, Any]:
        """/solidify <skill_name> handler using the new online EvolutionStore."""
        rail = self._skill_evolution_rail
        assert rail is not None
        store = rail.store

        parts = query.split(maxsplit=1)
        skill_name = parts[1].strip() if len(parts) > 1 else ""
        if not skill_name:
            return {
                "output": "请指定 Skill 名称：`/solidify <skill_name>`",
                "result_type": "error",
            }

        count = await store.solidify(skill_name)
        if count == 0:
            msg = f"Skill '{skill_name}' 没有待固化的演进经验。"
        else:
            msg = f"已将 {count} 条演进经验固化到 Skill '{skill_name}' 的 SKILL.md。"
        return {"output": msg, "result_type": "answer"}

    async def _handle_evolve_list_command(self, query: str) -> dict[str, Any]:
        """/evolve_list <skill_name> [--sort score] — show experiences with scores."""
        rail = self._skill_evolution_rail
        assert rail is not None
        store = rail.store

        parts = query.split()
        skill_name = parts[1] if len(parts) > 1 else ""
        if not skill_name or skill_name.startswith("--"):
            return {
                "output": "请指定 Skill 名称：`/evolve_list <skill_name>`",
                "result_type": "error",
            }

        if not store.skill_exists(skill_name):
            available = "、".join(store.list_skill_names()) or "（无可用 Skill）"
            return {
                "output": f"未找到 Skill '{skill_name}'。当前可用：{available}",
                "result_type": "error",
            }

        records = await store.get_records_by_score(skill_name)
        if not records:
            return {
                "output": f"Skill '{skill_name}' 暂无演进经验。",
                "result_type": "answer",
            }

        avg_score = sum(r.score for r in records) / len(records)

        lines = [
            f"📊 Skill \"{skill_name}\" — 经验库摘要\n",
            f"共 {len(records)} 条经验 | 平均分：{avg_score:.2f}\n",
            " #  │ Score │ Used    │ Effect  │ Section          │ Content (preview)",
            "────┼───────┼─────────┼─────────┼──────────────────┼──────────────────────────",
        ]
        for i, r in enumerate(records, 1):
            stats = r.usage_stats
            if stats:
                used_str = (
                    f"{stats.times_used}/{stats.times_presented}"
                    if stats.times_presented
                    else "0/0"
                )
                effect_str = f"+{stats.times_positive}/-{stats.times_negative}"
            else:
                used_str = "0/0"
                effect_str = "+0/-0"
            preview = r.change.content.split("\n")[0][:40]
            lines.append(
                f" {i:<2} │ {r.score:.2f}  │ {used_str:<7} │ {effect_str:<7} │ {r.change.section:<16} │ {preview}"
            )

        lines.append(f"\n提示：使用 /evolve_simplify {skill_name} 执行智能整理")
        return {
            "output": "\n".join(lines),
            "result_type": "answer",
        }

    async def _handle_evolve_simplify_command(self, query: str) -> dict[str, Any]:
        """/evolve_simplify <skill_name> [--dry-run] — LLM-based experience cleanup."""
        rail = self._skill_evolution_rail
        assert rail is not None
        store = rail.store
        scorer = rail.scorer

        parts = query.split()
        skill_name = parts[1] if len(parts) > 1 else ""
        dry_run = "--dry-run" in parts

        if not skill_name or skill_name.startswith("--"):
            return {
                "output": "请指定 Skill 名称：`/evolve_simplify <skill_name> [--dry-run]`",
                "result_type": "error",
            }

        if not store.skill_exists(skill_name):
            available = "、".join(store.list_skill_names()) or "（无可用 Skill）"
            return {
                "output": f"未找到 Skill '{skill_name}'。当前可用：{available}",
                "result_type": "error",
            }

        records = await store.get_records_by_score(skill_name)
        if not records:
            return {
                "output": f"Skill '{skill_name}' 暂无演进经验，无需整理。",
                "result_type": "answer",
            }

        skill_summary = await store.read_skill_content(skill_name)
        try:
            actions = await scorer.simplify(skill_name, skill_summary, records)
        except Exception as exc:
            logger.warning("[JiuWenClaw] evolve_simplify failed: %s", exc)
            return {
                "output": f"智能整理分析失败：{exc}",
                "result_type": "error",
            }

        if not actions:
            return {
                "output": f"Skill '{skill_name}' 经验库状态良好，无需整理。",
                "result_type": "answer",
            }

        summary_lines = []
        for action in actions:
            op = action.get("action", "KEEP")
            ids = action.get("target_ids", [])
            reason = action.get("reason", "")
            summary_lines.append(f"- **{op}** {', '.join(ids)}: {reason}")

        if dry_run:
            return {
                "output": (
                        f"**Skill '{skill_name}' 整理预览（dry-run，未执行）：**\n\n"
                        + "\n".join(summary_lines)
                ),
                "result_type": "answer",
            }

        result_text = await scorer.execute_simplify_actions(
            store, skill_name, actions
        )
        return {
            "output": (
                    f"**Skill '{skill_name}' 整理完成：** {result_text}\n\n"
                    f"**操作详情：**\n" + "\n".join(summary_lines)
            ),
            "result_type": "answer",
        }

    def _ensure_evolution_rail_for_slash(self, mode: str) -> str | None:
        """Check evolution availability for slash commands; lazily init rail if needed.

        Returns None when the rail is (or becomes) available, or an error message string.
        """
        if mode != "agent.plan":
            return "agent 模式下演进功能不可用。"
        if not self._config_cache.get("evolution", {}).get("enabled", False):
            return "演进功能未启用。"
        if self._skill_evolution_rail is None:
            self._skill_evolution_rail = self._build_skill_evolution_rail(self._config_cache)
        if self._skill_evolution_rail is None:
            return "演进功能初始化失败。"
        return None

    async def _handle_slash_command(
            self, query: str, session_id: str = "default", mode: str = "agent.plan",
    ) -> dict[str, Any] | None:
        """Intercept slash commands before agent invocation.

        Returns result dict if handled, None to proceed normally.
        The dict may contain an ``approval_chunks`` list that the caller
        should forward to the frontend as separate stream events.
        """
        stripped = query.strip()

        if stripped.startswith("/solidify"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_solidify_command(stripped)

        if stripped.startswith("/evolve_simplify"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_evolve_simplify_command(stripped)

        if stripped.startswith("/evolve_list"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_evolve_list_command(stripped)

        if stripped.startswith("/evolve"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_evolve_command(stripped, session_id)

        return None

    async def _cancel_pending_todos(self, session_id: str) -> list[dict] | None:
        """将未完成的 todo 项标记为 cancelled.

        Returns:
            更新后的 todo 列表（前端格式），用于附加到 interrupt_result 事件通知前端刷新。
            如果没有 todo 或操作失败，返回 None。
        """
        if self._instance is None:
            return None

        modify_tool = None
        try:
            tool_card = self._instance.ability_manager.get("todo_modify")
            registered_tool = Runner.resource_mgr.get_tool(tool_card.id)
            if isinstance(registered_tool, TodoModifyTool):
                modify_tool = registered_tool
        except Exception:
            pass

        if modify_tool is None:
            deep_config = self._instance.deep_config
            modify_tool = TodoModifyTool(
                operation=deep_config.sys_operation,
                workspace=str(deep_config.workspace.get_node_path(WorkspaceNode.TODO)),
                language=self._resolve_runtime_language(),
            )

        file_path = modify_tool.file_path_for_session(session_id)

        try:
            try:
                todos = await modify_tool.load_todos(file_path)
            except Exception as load_exc:
                logger.debug(
                    "[JiuWenClawDeepAdapter] session %s 无 todo 文件或加载失败: %s",
                    session_id,
                    load_exc,
                )
                return None

            if not todos:
                return None

            _DONE_STATUSES = {
                TodoStatus.COMPLETED.value,
                TodoStatus.CANCELLED.value,
            }

            ids_to_cancel = []
            for todo in todos:
                if todo.status.value not in _DONE_STATUSES:
                    ids_to_cancel.append(todo.id)

            if ids_to_cancel:
                await modify_tool.invoke(
                    {"action": "cancel", "ids": ids_to_cancel},
                    session_id=session_id,
                )
                logger.info(
                    "[JiuWenClawDeepAdapter] 已将 session %s 的未完成任务标记为 cancelled",
                    session_id,
                )

            # 重新加载并返回前端格式的 todo 列表，然后清空文件避免 TaskScheduler 续跑
            updated_todos = await modify_tool.load_todos(file_path)
            frontend = None
            if updated_todos and self._stream_event_rail is not None:
                frontend = JiuClawStreamEventRail.format_todos_for_frontend(updated_todos)
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write("[]\n")
            except Exception as clear_exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] 清空 session %s todo 文件失败: %s",
                    session_id,
                    clear_exc,
                )
            return frontend
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] 标记 todo cancelled 失败: %s", exc)
            return None

    async def process_message_impl(
            self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AgentResponse:
        """Execute a single non-streaming request and return the response.

        Args:
            request: AgentRequest 对象
            inputs: 已构建好的输入字典，包含 conversation_id 和 query

        Returns:
            AgentResponse 包含执行结果
        """
        if self._instance is None:
            raise RuntimeError("JiuWenClawDeepAdapter 未初始化，请先调用 create_instance()")

        if not self._has_valid_model_config():
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "模型未正确配置，请先配置模型信息"},
                metadata=request.metadata,
            )

        session_id = request.session_id or "default"
        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent.plan")

        if self._plain_chat_should_clear_stale_interrupt(request):
            await self._clear_session_persisted_interrupt_state(
                session_id,
                reason="plain_user_message_before_agent_run",
                clear_interrupt=True,
                clear_task_plan_if_todo_empty=True,
            )

        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(request.request_id or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self._model, "model_config", None) and getattr(self._model.model_config, "model_name", "") or ""
        )

        slash_result = await self._handle_slash_command(query, session_id, mode)
        if slash_result is not None:
            approval_chunks = slash_result.get("approval_chunks")
            if approval_chunks:
                payload: dict[str, Any] = {"approval_chunks": approval_chunks}
            else:
                content = slash_result.get("output", str(slash_result))
                payload = {"content": content}
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=slash_result.get("result_type") != "error",
                payload=payload,
                metadata=request.metadata,
            )

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
            params=request.params if isinstance(request.params, dict) else None,
        )
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)
        token_perm_sid = setup_permissions_session_scope(session_id)

        # Set telemetry context for OpenTelemetry span creation
        if self._telemetry_rail is not None:
            self._telemetry_rail.set_telemetry_context(
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                metadata=request.metadata,
            )

        try:
            await self._update_runtime_config(_RuntimeConfigParams.from_agent_request(request, mode))

            result = await Runner.run_agent(agent=self._instance, inputs=inputs)
        except asyncio.CancelledError:
            logger.info("[JiuWenClawDeepAdapter] Agent 任务被取消: request_id=%s session_id=%s", request.request_id,
                        session_id)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_cancelled",
            )
            raise
        except Exception as e:
            logger.error("[JiuWenClawDeepAdapter] Agent 任务执行异常: %s", e)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_error",
            )
            raise
        finally:
            TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
            cleanup_permission_context(token_perm)
            reset_permissions_session_scope(token_perm_sid)
            self._reset_runtime_cron_context(cron_context_tokens)
            _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
            _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
            _LLM_TRACE_ITERATION.reset(token_trace_iter)
            _LLM_TRACE_MODEL_NAME.reset(token_trace_model)
            if request.request_id:
                self._untrack_session_toolkit(request.request_id)

        content = result if isinstance(result, (str, dict)) else str(result)

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"content": content},
            metadata=request.metadata,
        )

    async def process_message_stream_impl(
            self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        """Execute a streaming request; yield response chunks.

        Args:
            request: AgentRequest 对象
            inputs: 已构建好的输入字典，包含 conversation_id 和 query

        Yields:
            AgentResponseChunk 流式响应块
        """
        if self._instance is None:
            raise RuntimeError("JiuWenClawDeepAdapter 未初始化，请先调用 create_instance()")

        if not self._has_valid_model_config():
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.error", "error": "模型未正确配置，请先配置模型信息"},
                is_complete=True,
            )
            return

        session_id = request.session_id or "default"
        rid = request.request_id
        cid = request.channel_id
        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent.plan")
        raw_interactive = request.params.get("interactive_ask", request.params.get("interactiveAsk"))
        # 未传参时默认为关闭：否则会沿用 session 内上次绑定的引导状态，导致关闭引导后仍弹结构化选择框。
        interactive_ask = bool(raw_interactive) if raw_interactive is not None else False
        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(rid or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self._model, "model_config", None) and getattr(self._model.model_config, "model_name", "") or ""
        )

        # Team 模式处理
        if mode == "team":
            from jiuwenclaw.agentserver.deep_agent.team_helpers import process_team_message_stream

            async for chunk in process_team_message_stream(request, inputs, self._instance):
                yield chunk
            _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
            _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
            _LLM_TRACE_ITERATION.reset(token_trace_iter)
            _LLM_TRACE_MODEL_NAME.reset(token_trace_model)
            return

        # 拦截斜杠命令
        slash_result = await self._handle_slash_command(query, session_id, mode)
        if slash_result is not None:
            approval_chunks = slash_result.get("approval_chunks", [])
            if approval_chunks:
                for chunk in approval_chunks:
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload=chunk,
                        is_complete=False,
                    )
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "chat.done"},
                    is_complete=True,
                )
            else:
                content = slash_result.get("output", str(slash_result))
                log_chat_final(
                    session_id=session_id,
                    request_id=rid or "",
                    iteration=_LLM_TRACE_ITERATION.get(),
                    model_name=_LLM_TRACE_MODEL_NAME.get(),
                )
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "chat.final", "content": content},
                    is_complete=True,
                )
            return

        if self._plain_chat_should_clear_stale_interrupt(request):
            await self._clear_session_persisted_interrupt_state(
                session_id,
                reason="plain_user_message_before_agent_run",
                clear_interrupt=True,
                clear_task_plan_if_todo_empty=True,
            )

        has_streamed_content = False
        accumulated_text = ""
        accumulated_reasoning = ""
        evolution_status_started = False
        evolution_status_ended = False
        usage_accumulator = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }
        hitl_pending_stream = False

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
            params=request.params if isinstance(request.params, dict) else None,
        )

        # Set telemetry context for OpenTelemetry span creation
        if self._telemetry_rail is not None:
            self._telemetry_rail.set_telemetry_context(
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                metadata=request.metadata,
            )
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)
        token_perm_sid = setup_permissions_session_scope(session_id)
        try:
            await self._update_runtime_config(_RuntimeConfigParams.from_agent_request(request, mode))

            if self._stream_event_rail is not None:
                self._stream_event_rail.reset_abort()
            async with ask_user_question_request_scope(
                interactive_ask=interactive_ask,
                session_id=session_id,
                stream_request_id=rid or "",
                channel_id=cid or "",
            ):
                async for chunk in Runner.run_agent_streaming(self._instance, inputs):
                    chunk_iteration = _extract_iteration_from_chunk(chunk)
                    if chunk_iteration is not None:
                        _LLM_TRACE_ITERATION.set(chunk_iteration)
                    if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
                        parsed = self._parse_stream_chunk(chunk)
                        if self._is_ask_user_payload(parsed):
                            hitl_pending_stream = True
                        if parsed is not None:
                            if accumulated_text:
                                delta_payload: dict[str, Any] = {"event_type": "chat.delta",
                                                                 "content": accumulated_text}
                                task_id = self._get_task_id()
                                if task_id:
                                    delta_payload["task_id"] = task_id
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                payload=delta_payload,
                                    is_complete=False,
                                )
                                accumulated_text = ""
                            if accumulated_reasoning:
                                reasoning_payload: dict[str, Any] = {
                                    "event_type": "chat.reasoning",
                                    "content": accumulated_reasoning,
                                }
                                task_id = self._get_task_id()
                                if task_id:
                                    reasoning_payload["task_id"] = task_id
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                    payload=reasoning_payload,
                                    is_complete=False,
                                )
                                accumulated_reasoning = ""
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=parsed,
                                is_complete=False,
                            )
                        continue

                    chunk_type = chunk.type

                    if chunk_type == "llm_usage":
                        logger.info(f"[JiuWenClawDeepAdapter] llm_usage chunk: {chunk}")
                        usage_meta = chunk.payload.get("usage_metadata", {}) if isinstance(chunk.payload, dict) else {}
                        if isinstance(usage_meta, dict):
                            for token in ("input_tokens", "output_tokens", "total_tokens"):
                                usage_accumulator[token] += usage_meta.get(token, 0) or 0
                            for cost in ("input_cost", "output_cost", "total_cost"):
                                usage_accumulator[cost] += usage_meta.get(cost, 0.0) or 0.0
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload={"event_type": "chat.usage_metadata", "metadata": chunk.payload,
                                     "session_id": session_id},
                            is_complete=False,
                        )
                        continue

                    if chunk_type == "llm_reasoning":
                        content = (
                            (chunk.payload.get("content", "") or chunk.payload.get("output", ""))
                            if isinstance(chunk.payload, dict)
                            else str(chunk.payload)
                        )
                        delta_payload: dict[str, Any] = {"event_type": "chat.reasoning", "content": content}
                        task_id = self._get_task_id()
                        if task_id:
                            delta_payload["task_id"] = task_id
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=delta_payload,
                            is_complete=False,
                        )
                        continue

                    if chunk_type == "llm_output":
                        has_streamed_content = True
                        if accumulated_reasoning:
                            reasoning_payload: dict[str, Any] = {
                                "event_type": "chat.delta",
                                "content": accumulated_reasoning,
                            }
                            task_id = self._get_task_id()
                            if task_id:
                                reasoning_payload["task_id"] = task_id
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=reasoning_payload,
                                is_complete=False,
                            )
                            accumulated_reasoning = ""
                        content = (
                            chunk.payload.get("content", "")
                            if isinstance(chunk.payload, dict)
                            else str(chunk.payload)
                        )
                        delta_payload: dict[str, Any] = {"event_type": "chat.delta", "content": content}
                        task_id = self._get_task_id()
                        if task_id:
                            delta_payload["task_id"] = task_id
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=delta_payload,
                            is_complete=False,
                        )
                        continue

                    if chunk_type == "answer":
                        if (
                                not evolution_status_started
                                and self._skill_evolution_rail is not None
                                and request.params.get("mode", "agent.plan") == "agent.plan"
                        ):
                            # Mark evolution phase start before after_invoke auto-evolution runs.
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload={"event_type": "chat.evolution_status", "status": "start"},
                                is_complete=False,
                            )
                            evolution_status_started = True
                        if accumulated_text:
                            delta_payload: dict[str, Any] = {"event_type": "chat.delta", "content": accumulated_text}
                            task_id = self._get_task_id()
                            if task_id:
                                delta_payload["task_id"] = task_id
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=delta_payload,
                                is_complete=False,
                            )
                            accumulated_text = ""
                        if accumulated_reasoning:
                            reasoning_payload: dict[str, Any] = {
                                "event_type": "chat.reasoning",
                                "content": accumulated_reasoning,
                            }
                            task_id = self._get_task_id()
                            if task_id:
                                reasoning_payload["task_id"] = task_id
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=reasoning_payload,
                                is_complete=False,
                            )
                            accumulated_reasoning = ""
                        if has_streamed_content:
                            parsed = self._parse_stream_chunk(chunk, _has_streamed_content=True)
                            if self._is_ask_user_payload(parsed):
                                hitl_pending_stream = True
                            if parsed is not None:
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                    payload=parsed,
                                    is_complete=False,
                                )
                            continue
                        parsed = self._parse_stream_chunk(chunk)
                        if self._is_ask_user_payload(parsed):
                            hitl_pending_stream = True
                        if parsed is not None:
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=parsed,
                                is_complete=False,
                            )
                            continue

                    if accumulated_text:
                        delta_payload: dict[str, Any] = {"event_type": "chat.delta", "content": accumulated_text}
                        task_id = self._get_task_id()
                        if task_id:
                            delta_payload["task_id"] = task_id
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=delta_payload,
                            is_complete=False,
                        )
                        accumulated_text = ""
                    if accumulated_reasoning:
                        reasoning_payload: dict[str, Any] = {
                            "event_type": "chat.reasoning",
                            "content": accumulated_reasoning,
                        }
                        task_id = self._get_task_id()
                        if task_id:
                            reasoning_payload["task_id"] = task_id
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=reasoning_payload,
                            is_complete=False,
                        )
                        accumulated_reasoning = ""
                    parsed = self._parse_stream_chunk(chunk)
                    if self._is_ask_user_payload(parsed):
                        hitl_pending_stream = True
                    if parsed is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=parsed,
                            is_complete=False,
                        )

            if accumulated_text:
                log_chat_final(
                    session_id=session_id,
                    request_id=rid or "",
                    iteration=_LLM_TRACE_ITERATION.get(),
                    model_name=_LLM_TRACE_MODEL_NAME.get(),
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.final", "content": accumulated_text},
                    is_complete=False,
                )
            if accumulated_reasoning:
                reasoning_payload: dict[str, Any] = {"event_type": "chat.reasoning", "content": accumulated_reasoning}
                task_id = self._get_task_id()
                if task_id:
                    reasoning_payload["task_id"] = task_id
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=reasoning_payload,
                    is_complete=False,
                )

            # after_invoke 在流关闭后触发，其中缓存的审批事件无法通过
            # session.write_stream 传递，需手动注入到 stream 输出
            if self._skill_evolution_rail is not None:
                for evt in self._skill_evolution_rail.drain_pending_approval_events():
                    parsed = self._parse_stream_chunk(evt)
                    if self._is_ask_user_payload(parsed):
                        hitl_pending_stream = True
                    if parsed is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=parsed,
                            is_complete=False,
                        )

            if evolution_status_started and not evolution_status_ended:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.evolution_status", "status": "end"},
                    is_complete=False,
                )
                evolution_status_ended = True
        except asyncio.CancelledError:
            logger.info("[JiuWenClawDeepAdapter] 流式任务被取消: request_id=%s session_id=%s", rid, session_id)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_cancelled",
            )
            raise
        except Exception as exc:
            logger.exception("[JiuWenClawDeepAdapter] 流式任务异常: %s", exc)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_error",
            )
            if evolution_status_started and not evolution_status_ended:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.evolution_status", "status": "end"},
                    is_complete=False,
                )
                evolution_status_ended = True
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={"event_type": "chat.error", "error": str(exc)},
                is_complete=False,
            )
        finally:
            TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
            cleanup_permission_context(token_perm)
            reset_permissions_session_scope(token_perm_sid)
            self._reset_runtime_cron_context(cron_context_tokens)
            _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
            _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
            _LLM_TRACE_ITERATION.reset(token_trace_iter)
            _LLM_TRACE_MODEL_NAME.reset(token_trace_model)
            if rid:
                self._untrack_session_toolkit(rid)

        summary = {
            "input_tokens": usage_accumulator["input_tokens"],
            "output_tokens": usage_accumulator["output_tokens"],
            "total_tokens": usage_accumulator["total_tokens"],
        }
        if usage_accumulator["input_cost"] > 0:
            summary["input_cost"] = round(usage_accumulator["input_cost"], 6)
        if usage_accumulator["output_cost"] > 0:
            summary["output_cost"] = round(usage_accumulator["output_cost"], 6)
        if usage_accumulator["total_cost"] > 0:
            summary["total_cost"] = round(usage_accumulator["total_cost"], 6)

        logger.info("[JiuWenClawDeepAdapter] llm_usage summary: request_id=%s session_id=%s usage=%s",
                    rid, session_id, summary)

        if usage_accumulator["total_tokens"] > 0:
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={
                    "event_type": "chat.usage_summary",
                    "session_id": session_id,
                    "usage": summary,
                },
                is_complete=False,
            )

        if hitl_pending_stream:
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={"event_type": "chat.invocation_paused", "awaiting_user_input": True},
                is_complete=True,
            )
        else:
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload=None,
                is_complete=True,
            )

    @staticmethod
    def _is_ask_user_payload(payload: Any) -> bool:
        if not isinstance(payload, dict) or payload.get("event_type") != "chat.ask_user_question":
            return False
        # 子 Agent委托审批在子协程内等待Future，不是主 Agent checkpoint。
        # 反向排除这些委托source，保留所有主Agent类型和缺source旧协议的HITL语义。
        return payload.get("source") not in _SUBAGENT_APPROVAL_SOURCES

    def _parse_stream_chunk(self, chunk, *, _has_streamed_content: bool = False) -> dict | None:
        """将 SDK OutputSchema 转为前端可消费的 payload dict.

        Args:
            chunk: OutputSchema 或 dict
            _has_streamed_content: 是否已通过 llm_output 流式发送过内容

        Returns:
            dict  – 含 event_type 的 payload，或 None（需跳过的帧）。
        """
        try:
            if hasattr(chunk, "type") and hasattr(chunk, "payload"):
                chunk_type = chunk.type
                payload = chunk.payload

                if chunk_type == "controller_output" and payload is not None:
                    inner_t = getattr(payload, "type", None)
                    inner_val = (
                        getattr(inner_t, "value", inner_t) if inner_t is not None else None
                    )
                    if inner_val == "task_completion":
                        return None
                    if inner_val == "task_failed":
                        error = next((item.text for item in payload.data if hasattr(item, "text")), "任务执行失败")
                        return {"event_type": "chat.error", "error": error}

                if chunk_type == "llm_output":
                    content = (
                        payload.get("content", "")
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    if not content:
                        return None
                    result: dict[str, Any] = {"event_type": "chat.delta", "content": content}
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "llm_reasoning":
                    content = (
                        (payload.get("content", "") or payload.get("output", ""))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    if not content:
                        return None
                    result: dict[str, Any] = {"event_type": "chat.reasoning", "content": content}
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "content_chunk":
                    content = (
                        payload.get("content", "")
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    if not content:
                        return None
                    result: dict[str, Any] = {"event_type": "chat.delta", "content": content}
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "answer":
                    if isinstance(payload, dict):
                        if payload.get("result_type") == "error":
                            return {
                                "event_type": "chat.error",
                                "error": payload.get("output", "未知错误"),
                            }
                        output = payload.get("output", {})
                        if isinstance(output, dict) and output.get("result_type") == "error":
                            logger.warning(
                                "[interface_deep] nested_answer_error_detected output=%s",
                                output.get("output", "未知错误"),
                            )
                            return {
                                "event_type": "chat.error",
                                "error": output.get("output", "未知错误"),
                            }
                        content = (
                            output.get("output", "")
                            if isinstance(output, dict)
                            else str(output)
                        )
                        is_chunked = (
                            output.get("chunked", False)
                            if isinstance(output, dict)
                            else False
                        )
                    else:
                        content = str(payload)
                        is_chunked = False

                    # Belt-and-suspenders: strip any residual inline tool protocol
                    # fragments (todo_insert / function<tool_sep>... etc.) before
                    # exposing answer content to the frontend.
                    content = strip_inline_tool_protocol(content)

                    if _has_streamed_content and not is_chunked:
                        # When llm_output has already streamed the full user-facing text,
                        # keep chat.final as a completion marker only to avoid duplicating
                        # the final answer block downstream.
                        log_chat_final(
                            session_id=_LLM_TRACE_SESSION_ID.get(),
                            request_id=_LLM_TRACE_REQUEST_ID.get(),
                            iteration=_LLM_TRACE_ITERATION.get(),
                            model_name=_LLM_TRACE_MODEL_NAME.get(),
                        )
                        return {"event_type": "chat.final", "content": ""}

                    if not content:
                        return None
                    if is_chunked:
                        result: dict[str, Any] = {"event_type": "chat.delta", "content": content}
                        task_id = self._get_task_id()
                        if task_id:
                            result["task_id"] = task_id
                        return result
                    log_chat_final(
                        session_id=_LLM_TRACE_SESSION_ID.get(),
                        request_id=_LLM_TRACE_REQUEST_ID.get(),
                        iteration=_LLM_TRACE_ITERATION.get(),
                        model_name=_LLM_TRACE_MODEL_NAME.get(),
                    )
                    return {"event_type": "chat.final", "content": content}

                if chunk_type == "tool_calls.delta":
                    if isinstance(payload, dict):
                        result = {
                            "event_type": "chat.tool_calls.delta",
                            "tool_calls": tool_calls_payload_to_json_list(
                                payload.get("tool_calls", [])
                            ),
                        }
                        if "source" in payload:
                            result["source"] = payload.get("source")
                        task_id = self._get_task_id()
                        if task_id:
                            result["task_id"] = task_id
                        return result
                    result = {
                        "event_type": "chat.tool_calls.delta",
                        "tool_calls": tool_calls_payload_to_json_list(payload),
                    }
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "tool_call":
                    tool_info = (
                        payload.get("tool_call", payload)
                        if isinstance(payload, dict)
                        else payload
                    )
                    result = {"event_type": "chat.tool_call", "tool_call": tool_info}
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "tool_update":
                    if isinstance(payload, dict):
                        update_info = payload.get("tool_update", payload)
                        update_payload = (
                            dict(update_info)
                            if isinstance(update_info, dict)
                            else {"content": str(update_info)}
                        )
                    else:
                        update_payload = {"content": str(payload)}
                    result = {
                        "event_type": "chat.tool_update",
                        **update_payload,
                    }
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "tool_result":
                    if isinstance(payload, dict):
                        result_info = payload.get("tool_result", payload)
                        result_payload = {
                            "result": result_info.get("result", str(result_info))
                            if isinstance(result_info, dict)
                            else str(result_info),
                        }
                        if isinstance(result_info, dict):
                            result_payload["tool_name"] = (
                                    result_info.get("tool_name")
                                    or result_info.get("name")
                            )
                            result_payload["tool_call_id"] = (
                                    result_info.get("tool_call_id")
                                    or result_info.get("toolCallId")
                            )
                            raw_output = result_info.get("raw_output")
                            if raw_output is None:
                                raw_output = result_info.get("rawOutput")
                            if raw_output is not None:
                                result_payload["raw_output"] = raw_output
                    else:
                        result_payload = {"result": str(payload)}
                    result = {
                        "event_type": "chat.tool_result",
                        **result_payload,
                    }
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result

                if chunk_type == "error":
                    error_msg = (
                        payload.get("error", str(payload))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    return {"event_type": "chat.error", "error": error_msg}

                if chunk_type == "thinking":
                    return {
                        "event_type": "chat.processing_status",
                        "is_processing": True,
                        "current_task": "thinking",
                    }

                if chunk_type == "retry_notification":
                    if isinstance(payload, dict):
                        output = payload.get("output", {})
                        content = output.get("output", "") if isinstance(output, dict) else str(output)
                    else:
                        content = str(payload)
                    return {
                        "event_type": "chat.delta",
                        "content": content,
                        "source_chunk_type": chunk_type,
                    }

                if chunk_type == "todo.updated":
                    todos = (
                        payload.get("todos", [])
                        if isinstance(payload, dict)
                        else []
                    )
                    return {"event_type": "todo.updated", "todos": todos}

                if chunk_type == "context.compressed":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "context.compressed",
                            "rate": payload.get("rate", 0),
                            "before_compressed": payload.get("before_compressed"),
                            "after_compressed": payload.get("after_compressed"),
                        }
                    return {"event_type": "context.compressed", "rate": 0}

                if chunk_type == "context.usage":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "context.usage",
                            "used_tokens": payload.get("used_tokens"),
                            "limit_tokens": payload.get("limit_tokens"),
                            "usage_percent": payload.get("usage_percent"),
                            "input_tokens": payload.get("input_tokens"),
                            "output_tokens": payload.get("output_tokens"),
                            "total_tokens": payload.get("total_tokens"),
                        }
                    return {"event_type": "context.usage"}

                if chunk_type == "chat.ask_user_question":
                    return {
                        "event_type": "chat.ask_user_question",
                        **(payload if isinstance(payload, dict) else {}),
                    }

                if chunk_type == "chat.ask_user_question_expired":
                    return {
                        "event_type": "chat.ask_user_question_expired",
                        **(payload if isinstance(payload, dict) else {}),
                    }

                if chunk_type == "__interaction__":
                    return convert_interactions_to_ask_user_question([payload])

                if chunk_type == "task.start":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "task.start",
                            "task_id": payload.get("task_id"),
                            "task_content": payload.get("task_content"),
                            "task_index": payload.get("task_index"),
                            "total_tasks": payload.get("total_tasks"),
                            "parent_request_id": payload.get("parent_request_id"),
                            "timestamp": payload.get("timestamp"),
                        }
                    return None

                if chunk_type == "task.complete":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "task.complete",
                            "task_id": payload.get("task_id"),
                            "task_content": payload.get("task_content"),
                            "status": payload.get("status"),
                            "duration_ms": payload.get("duration_ms"),
                            "error": payload.get("error"),
                            "timestamp": payload.get("timestamp"),
                        }
                    return None

                if isinstance(payload, dict):
                    if "traceId" in payload or "invokeId" in payload:
                        return None
                    content = payload.get("content") or payload.get("output")
                    if not content:
                        return None
                else:
                    content = str(payload)
                result: dict[str, Any] = {"event_type": "chat.delta", "content": content}
                task_id = self._get_task_id()
                if task_id:
                    result["task_id"] = task_id
                return result

            if isinstance(chunk, dict):
                if "traceId" in chunk or "invokeId" in chunk:
                    return None
                if chunk.get("result_type") == "error":
                    logger.warning(
                        "[interface_deep] top_level_chunk_error_detected output=%s",
                        chunk.get("output", "未知错误"),
                    )
                    return {
                        "event_type": "chat.error",
                        "error": chunk.get("output", "未知错误"),
                    }
                output_payload = chunk.get("output")
                if isinstance(output_payload, dict) and output_payload.get("result_type") == "error":
                    logger.warning(
                        "[interface_deep] nested_chunk_error_detected output=%s",
                        output_payload.get("output", "未知错误"),
                    )
                    return {
                        "event_type": "chat.error",
                        "error": output_payload.get("output", "未知错误"),
                    }
                output = chunk.get("output", "")
                if output:
                    result: dict[str, Any] = {"event_type": "chat.delta", "content": str(output)}
                    task_id = self._get_task_id()
                    if task_id:
                        result["task_id"] = task_id
                    return result
                return None

        except Exception:
            logger.debug("[_parse_stream_chunk] 解析异常", exc_info=True)

        return None

    async def _handle_memory_rail_by_config(self, mode: str):
        config = get_config()
        if get_memory_mode(config) == "local":
            # 引擎门禁：memory.engine 未放行内置时，等同于禁用
            builtin_on = is_builtin_memory_allowed(config) and is_memory_enabled(mode, config)
            if builtin_on:
                # 开启记忆
                if self._memory_rail is not None:
                    cur_memory_type = is_proactive_memory(mode, config)
                    if self._is_proactive_memory != cur_memory_type:
                        # 当前记忆类型（主动/被动）和之前注册的不一致，重新注册
                        await self._instance.unregister_rail(self._memory_rail)
                        self._memory_rail = None
                    else:
                        # 已经注册，且记忆类型相同，无需其他操作
                        return
                if self._memory_rail is None:
                    self._memory_rail = self._build_memory_rail(mode)
                if self._memory_rail is not None:
                    await self._instance.register_rail(self._memory_rail)
                    logger.info(f"[JiuWenClawDeepAdapter] MemoryRail registered for {mode} mode")
            elif not builtin_on and self._memory_rail is not None:
                await self._instance.unregister_rail(self._memory_rail)
                self._memory_rail = None
                logger.info(f"[JiuWenClawDeepAdapter] MemoryRail unregistered for {mode} mode")

    def _build_external_memory_rail(self):
        from jiuwenclaw.agentserver.memory.external_memory_builder import (
            build_external_memory_rail,
        )
        return build_external_memory_rail(
            config=get_config(),
            workspace_dir=self._workspace_dir,
        )

    async def _handle_external_memory_rail_by_config(self):
        """Register / unregister ExternalMemoryRail based on config.

        External memory is mode-independent — configured once and active for
        both plan and fast modes. `_external_memory_rail_registered` dedups
        calls from both _update_plan_mode_rails() and _update_agent_mode_rails().
        Not part of `_get_current_agent_rails()`, so it is not torn down on
        config hot-reload (preserves prefetch cache + circuit breaker state).
        """
        from jiuwenclaw.agentserver.memory.external_memory_config import (
            is_external_memory_enabled,
        )
        config = get_config()
        if is_external_memory_enabled(config):
            if self._external_memory_rail_registered:
                return
            if self._external_memory_rail is None:
                self._external_memory_rail = self._build_external_memory_rail()
            if self._external_memory_rail is None:
                return
            try:
                await self._instance.register_rail(self._external_memory_rail)
                self._external_memory_rail_registered = True
                logger.info("[JiuWenClawDeepAdapter] ExternalMemoryRail registered")
            except Exception as exc:
                logger.error(
                    "[JiuWenClawDeepAdapter] ExternalMemoryRail register failed: %s", exc
                )
                self._external_memory_rail = None
        elif self._external_memory_rail is not None and self._external_memory_rail_registered:
            # Call on_session_end BEFORE unregister_rail: unregister -> uninit()
            # is sync, and run_coroutine_threadsafe from the same event loop
            # thread would deadlock.
            provider = getattr(self._external_memory_rail, "_provider", None)
            if provider is not None and hasattr(provider, "on_session_end"):
                try:
                    await provider.on_session_end()
                except Exception as exc:
                    logger.debug(
                        "[JiuWenClawDeepAdapter] on_session_end failed: %s", exc
                    )
            try:
                await self._instance.unregister_rail(self._external_memory_rail)
                logger.info("[JiuWenClawDeepAdapter] ExternalMemoryRail unregistered")
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] ExternalMemoryRail unregister failed: %s", exc
                )
            self._external_memory_rail = None
            self._external_memory_rail_registered = False

    @classmethod
    def is_working(cls, session_tasks: dict[str, asyncio.Task],
                   session_queues: dict[str, asyncio.PriorityQueue]) -> bool:
        """返回 Agent 是否正在工作.

        用于沙箱保活校验，一旦发现任何活跃状态立即返回 True.

        判断维度：
        1. 非流式任务：_session_tasks 中正在执行的 Task
        2. 待处理消息：_session_queues 中队列的消息数
        3. Team 流式任务：TeamManager._stream_tasks 中正在执行的 Team 模式流式任务
        4. Team monitors：TeamManager._team_monitors 中正在运行的监控处理器
        5. ACP 待响应请求：AcpOutputManager._pending 中等待用户响应的 ACP 工具请求

        Returns:
            bool: 是否正在工作
        """
        # 检查正在执行的非流式任务
        for task in session_tasks.values():
            if task is not None and not task.done():
                return True

        # 检查队列中待处理的消息
        for queue in session_queues.values():
            if queue.qsize() > 0:
                return True

        # 检查 Team 模式下的流式任务和 monitors
        try:
            team_manager = get_team_manager()
            for task in team_manager._stream_tasks.values():  # pylint: disable=protected-access
                if task is not None and not task.done():
                    return True
            for monitor in team_manager._team_monitors.values():  # pylint: disable=protected-access
                if monitor is not None and monitor.is_running:
                    return True
        except Exception:
            pass

        # 检查 ACP 待响应请求
        try:
            if len(get_acp_output_manager()._pending) > 0:  # pylint: disable=protected-access
                return True
        except Exception:
            pass

        return False
