# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuWenSwarm Deep Adapter - 基于 openjiuwen DeepAgent 的适配器实现.

此模块实现 AgentAdapter 协议，封装 Deep SDK 的所有专属逻辑。
公共编排逻辑（session 队列、Skills 路由、heartbeat 等）由 Facade 层处理。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import operator
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, List, Optional, Tuple
from urllib.parse import quote_plus

import yaml
from openjiuwen.core.context_engine.schema.config import CompressionRecallConfig, ContextEngineConfig
from openjiuwen.core.foundation.kv_cache import KVCacheAffinityConfig
from openjiuwen.core.foundation.llm import ModelRequestConfig, ModelClientConfig, Model
from openjiuwen.core.foundation.llm.utils.provider_utils import is_openai_account_provider
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.foundation.tool import ToolCard, McpServerConfig
from openjiuwen.core.common.logging import server_logger
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.core.single_agent import AgentCard, ReActAgentConfig
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.core.sys_operation import (
    SysOperation,
    SysOperationCard,
    OperationMode,
)
from openjiuwen.core.sys_operation.cwd import init_cwd
from openjiuwen.harness import (
    AudioModelConfig,
    DeepAgent,
    DeepAgentConfig,
    VisionModelConfig,
)
from openjiuwen.harness.factory import (
    _inject_general_purpose_subagent,
    create_deep_agent,
)
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import (
    LLMRetryRail,
    SkillUseRail,
    TaskPlanningRail,
    SecurityRail,
    SkillEvolutionRail,
    EvolutionInterruptRail,
    SkillCreateRail,
    SubagentRail,
    SysOperationRail,
    HeartbeatRail,
    MemoryRail,
    configure_skill_evolution_runtime,
    unconfigure_skill_evolution,
)
from openjiuwen.harness.rails.evolution import EvolutionReviewRuntime
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import ContextAssembleRail
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
# FullCompact 仅用于溢出兜底（413/上下文溢出），日常压缩由 preset 链承担。
# 从 processor.compressor 包入口取 Config（__init__ 显式导出，跨版本稳定）；
# _merge_processors 只认 BaseModel 实例，不区分配置类来源
from openjiuwen.core.context_engine.processor.compressor import FullCompactProcessorConfig
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload as _SkillTurboConfirmPayload
try:
    from openjiuwen.agent_evolving.skill_self_evolution import resolve_skill_evolution_action
except ImportError:
    def resolve_skill_evolution_action(  # type: ignore[misc]
        skill_name: str,
        *,
        default_auto_save: bool = True,
        **_kwargs: Any,
    ) -> str:
        """Fallback when agent-core lacks skill_self_evolution."""
        return "auto" if default_auto_save else "suggest"
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.research_agent import build_research_agent_config
from openjiuwen.harness.tools import (
    create_audio_tools,
    create_vision_tools,
)
from openjiuwen.harness.goal.schema import GoalOperationError, GoalStatus
from openjiuwen.harness.schema.interaction import (
    InteractionEventType,
    InputDispatchMode,
    SendInputRequest,
)
from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode

from jiuwenswarm.agents.harness.common.tools.deepresearch import (
    get_deepresearch_tools,
    push_deepresearch_route,
    reset_deepresearch_route,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
    run_rewrite_fast_path,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlFollowupResult,
    RewriteHtmlTarget,
    decode_html_tool_result,
    is_html_followup_request,
    target_from_commit_result,
    target_from_state,
)

if TYPE_CHECKING:
    from openjiuwen.harness.schema.config import SubAgentConfig
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

GOAL_UPDATED_EVENT_TYPE = InteractionEventType.GOAL_UPDATED.value
_ERROR_EVENT = getattr(InteractionEventType, "EXECUTION_ERROR", None)
if _ERROR_EVENT is None:
    _ERROR_EVENT = getattr(InteractionEventType, "RUNTIME_ERROR")
ERROR_EVENT_TYPE = _ERROR_EVENT.value
# 与 skill_turbo/executor.STREAM_SOURCE_ID_FIELD 一致：并发节点用它标识 source，
# 前端据其路由到 subagent 行。adapter 转发 llm_reasoning / llm_output 时须原样透传。
STREAM_SOURCE_ID_FIELD = "stream_source_id"
_INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT = 20
_INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS = 0.05


# SkillTurbo 内部工具 id 后缀（如 BashTool_skill_turbo）。外层 ReAct 工具结果不含此后缀。
_SKILL_TURBO_TOOL_ID_SUFFIX = "_skill_turbo"


def _propagate_stream_source_id(
    src_payload: Any, result: dict[str, Any] | None
) -> dict[str, Any] | None:
    """将上游 payload 中的 stream_source_id / task_id 透传到输出帧（原地写入）。

    skill_turbo 并发节点用 stream_source_id 标识 source，前端据其路由到
    subagent 行。task_id 原样透传，供 RelayClaw 按需把 chat.delta 归入
    taskRuns；是否进正式正文由上层决定，本函数不裁剪 content。
    """
    if result is None or not isinstance(src_payload, dict):
        return result
    source_id = src_payload.get(STREAM_SOURCE_ID_FIELD)
    if source_id:
        result[STREAM_SOURCE_ID_FIELD] = source_id
    task_id = src_payload.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        result["task_id"] = task_id.strip()
    return result


def _tool_result_lookup(payload: Any, *keys: str) -> Any:
    """Read a field from a tool_result payload or its nested ``tool_result`` dict."""
    if not isinstance(payload, dict):
        return None
    nested = payload.get("tool_result")
    sources = (payload, nested) if isinstance(nested, dict) else (payload,)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _is_outer_react_tool_result(payload: Any) -> bool:
    """True when this tool_result belongs to the outer ReAct agent, not SkillTurbo internals.

    Inner PPT/bash/read_file results carry ``stream_source_id`` or a tool id ending
    in ``_skill_turbo``. Resetting ``has_streamed_content`` on those would leak
    nested non-streamed answers as ``chat.final``.
    """
    source_id = _tool_result_lookup(payload, STREAM_SOURCE_ID_FIELD)
    if isinstance(source_id, str) and source_id.strip():
        return False
    tool_id = _tool_result_lookup(payload, "tool_call_id", "tool_id", "toolCallId")
    if isinstance(tool_id, str) and tool_id.strip().endswith(_SKILL_TURBO_TOOL_ID_SUFFIX):
        return False
    return True


_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY = "deepresearch_rewrite_fast_path_replays"
_DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION = 1
_DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES = 32
_DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH = 256
_DEEPRESEARCH_REWRITE_REPLAY_MAX_BYTES = 256 * 1024
_DEEPRESEARCH_REWRITE_REPLAY_PATH_MAX_BYTES = 4096
_DEEPRESEARCH_REWRITE_REPLAY_TERMINAL_KINDS = frozenset(
    {"success", "report_delivery_failed", "publish_uncertain"}
)
_DEEPRESEARCH_REWRITE_SUCCESS_MESSAGE = (
    "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
    "如需继续改写，可直接选择下一处内容。"
)
_DEEPRESEARCH_REWRITE_DELIVERY_FAILURE_MESSAGE = (
    "改写版本已成功保留，但报告文件交付失败。"
)
_DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE = (
    "改写版本状态不确定，请刷新会话后重试。"
)
_DEEPRESEARCH_CHECKPOINT_UNCERTAIN = "DEEPRESEARCH_CHECKPOINT_UNCERTAIN"
_DEEPRESEARCH_CHECKPOINT_UNCERTAIN_MESSAGE = (
    "会话检查点状态不确定，请重试以重新加载会话。"
)

from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
    init_a2x_client,
    register_blank_agent_if_teammate,
    resolve_a2x_config,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import CronRuntimeBridge
from jiuwenswarm.server.runtime.agent_adapter.llm_io_trace import (
    begin_llm_trace_event,
    end_llm_trace_event,
    log_invoke_input,
    log_invoke_output,
    log_reasoning_delta,
    log_stream_input,
    log_stream_output,
)
from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    SKILL_EVOLUTION_APPROVAL_SCHEMA,
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.tools.todo_compat import (
    CompatibleTodoModifyTool,
    install_todo_modify_compat_patch,
)
from jiuwenswarm.common.openjiuwen_rail_compat import install_evolution_rail_kwargs_compat
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import build_agent_identity_prompt
from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import NotifyingLLMRetryRail
from jiuwenswarm.agents.harness.common.rails import (
    DeepResearchExecutionRail,
    JiuSwarmStreamEventRail,
    MultimodalImageRail,
    ResponsePromptRail,
    RuntimePromptRail,
    StructuredAskUserRail,
    SymphonyOrchestrationRail,
    TaskExecutionRail,
    ContextOverflowRecoveryRail,
)
from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import (
    DisabledToolsRail,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    SkillActiveStateRail,
    clear_session_skill_state,
)
from jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail import (
    SkillCredentialInjectionRail,
    coalesce_config_skill_envs,
    coalesce_skill_envs,
)
from jiuwenswarm.agents.harness.common.rails.concurrent_safe_rails import (
    ConcurrentSafeSysOperationRail,
    ConcurrentSafeTaskPlanningRail,
)
from jiuwenswarm.common.config import get_model_names
from jiuwenswarm.common.hooks_config import load_hooks_config
from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.common.stage_timer import StageTimer
from jiuwenswarm.common.tool_ownership import mark_stateless, register_tool, unregister_tool
from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    setup_permission_context,
    cleanup_permission_context,
)
from jiuwenswarm.agents.harness.common.memory.config import (
    clear_config_cache,
    clear_embed_config_db_cache,
    clear_memory_config_db_cache,
    get_embed_config,
    get_memory_mode,
    is_memory_enabled,
    is_proactive_memory,
    merge_memory_config_into_config,
    reload_memory_config_from_gateway_db,
    set_embed_config_db_cache,
)
from jiuwenswarm.agents.harness.common.memory.external_memory_config import is_builtin_memory_allowed
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import TOOL_PERMISSION_CHANNEL_ID
from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
    get_base_permissions_config,
    get_effective_permissions_config,
    merge_session_permissions_overlay,
    reset_permissions_agent_base,
    reset_permissions_session_scope,
    resolve_permissions_body_from_enterprise,
    setup_permissions_agent_base,
    setup_permissions_session_scope,
)
from jiuwenswarm.server.runtime.session.session_metadata import build_server_push_message
from jiuwenswarm.server.runtime.session.session_history import append_history_record, load_history_records
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.prompt_attachment_loader import PromptAttachmentLoader
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    EVOLUTION_ACCEPT_LABELS,
    EVOLUTION_EXECUTE_LABELS,
    EvolutionPushContext,
    REGULAR_EVOLUTION_SLASH_WARNING_PHRASES,
    TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
    TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES,
    TEAM_EVOLUTION_IDLE_SLEEP_SEC,
    TEAM_EVOLUTION_NOOP_STAGES,
    approve_evolution_records,
    answers_select_option,
    approved_record_ids_from_answers,
    build_evolution_status_update,
    event_payload_dict,
    evolution_meta_from_payload,
    evolution_outcome_from_event,
    evolution_meta_from_params,
    evolution_slash_command_name,
    evolution_slash_result,
    is_evolution_approval_event,
    is_evolution_outcome_event,
    merge_evolution_disabled_skills,
    push_evolution_event,
    push_evolution_progress,
    push_evolution_status,
    record_ids_from_pending_approval,
    reject_evolution_records,
    resolve_evolution_event_timeout_sec,
    sync_evolution_disabled_skills,
    team_evolution_terminal_progress,
    terminal_stage,
    visible_evolution_progress_from_events,
    visible_regular_evolution_start_progress,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_slash import (
    EvolutionSlashContext,
    handle_evolution_slash_command,
)
from jiuwenswarm.server.runtime.agent_adapter import evolution_version as evolution_version_ctl
from jiuwenswarm.server.utils.stream_utils import parse_ask_user_question_payload
from jiuwenswarm.common.local_env_config import (
    is_enterprise,
    bind_agent_env_ns,
    bind_task_env_overlay,
    build_effective_env_overlay,
    promote_staged_env,
    read_env,
    reset_agent_env_ns,
    reset_task_env_overlay,
)
from jiuwenswarm.server.runtime.reload_result import (
    ReloadResult,
    env_touches_shared_skills_dirs,
    env_touches_task_memory,
)
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_image_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    dedicated_multimodal_model_configured,
    complete_multimodal_model_configured,
    clear_multimodal_env_groups,
    MULTIMODAL_ENV_GROUP_KEYS,
    multimodal_env_anchor_present,
)
from jiuwenswarm.agents.harness.common.tools.task_tools import clear_task_memory_service
from jiuwenswarm.agents.harness.common.tools.video_tools import video_understanding
from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image

from jiuwenswarm.agents.harness.common.tools import (
    SendFileToolkit,
    SkillRetrievalToolkit,
    SkillToolkit,
    is_skill_retrieval_enabled,
    SymphonyToolkit,
)
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
    SkillRetrievalPromptRail,
)
from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.rails.a2a_outbound_toolkit_rail import (
    A2AOutboundToolkitRail,
)
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.agents.harness.common.tools.wiki_tools import wiki_ingest, wiki_query, wiki_lint
from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
    build_jiuwen_harness_named_web_tools,
)
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_tools as get_acp_output_tools
from jiuwenswarm.agents.harness.common.tools.acp_chat import acp_chat
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
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
from jiuwenswarm.common.config import (
    _get_evolution_config,
    get_config,
    get_default_models,
    get_evolution_enabled,
    get_evolution_review_trigger_enabled,
    get_evolution_signal_trigger_enabled,
    get_passive_skill_evolution_triggers,
    get_skill_evolution_enabled,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
    get_skill_create_enabled,
    resolve_env_vars,
    resolve_string_or_list_config,
)
from jiuwenswarm.common.mcp_config import (
    OfficeClawMcpRegistration,
    RequestScopedOfficeClawMcpTool,
    acquire_request_scoped_mcp_session,
    bind_active_office_claw_mcp_tools,
    build_mcp_server_config,
    clear_agent_office_claw_tool_ids,
    extract_enabled_mcp_server_entries,
    extract_office_claw_mcp,
    extract_request_mcp_servers,
    is_asyncio_outer_cancellation,
    list_office_claw_mcp_tools,
    list_request_mcp_server_tools,
    preflight_mcp_server_reachable,
    publish_live_office_claw_allowlist,
    register_live_office_claw_tool_instance,
    release_request_scoped_mcp_sessions,
    revoke_live_office_claw_allowlist,
    set_agent_office_claw_tool_ids,
    unregister_live_office_claw_tool_instance,
    validate_office_claw_mcp_config,
)
from jiuwenswarm.common.mcp_call_timeout_patch import apply_mcp_call_timeout_patch
from jiuwenswarm.perf.interface_hooks import (
    clear_perf_summary_context,
    finalize_perf_summary_request,
    mark_request_first_answer,
    mark_request_first_byte,
    merge_perf_summary_usage_fallback,
    maybe_mark_answer_first_byte,
    record_deepresearch_sdk_token_usage,
    set_perf_summary_context,
    snapshot_perf_summary_usage,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.usage import (
    normalize_workflow_llm_token_usage,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    create_local_sysop_card,
    create_sandbox_sysop_card,
)
from jiuwenswarm.agents.harness.common.auto_harness.service import _HARNESS_PACKAGES_FILE
from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    build_interaction_output_from_abort as _skill_turbo_build_interaction_output,
    clear_resume_ctx as _skill_turbo_clear_resume_ctx,
    extract_tool_interrupt as _skill_turbo_extract_tool_interrupt,
    load_resume_ctx as _skill_turbo_load_resume_ctx,
    set_skill_turbo_id as _skill_turbo_set_agent_id,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError as _SkillTurboAbortError
from jiuwenswarm.gateway.cron import CronTargetChannel
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.skill.skill_whitelist import (
    SkillWhitelistSynchronizer,
    is_skill_whitelist_tenant,
    parse_agent_skill_whitelist,
)
from jiuwenswarm.common.utils import (
    DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL,
    fill_template_defaults,
    get_agent_evolution_trajectories_dir,
    get_agent_root_dir,
    get_agent_skills_dir,
    get_agent_workspace_dir,
    collapse_nested_agent_workspace_dir,
    get_checkpoint_dir,
    get_default_project_session_workspace_dir,
    get_env_file,
    get_multi_tenant_user_workspace_dir,
    get_prompt_attachment_dir,
    get_runtime_state_path,
    resolve_tenant_sessions_dir,
    reset_free_search_runtime_flags,
    resolve_agent_registered_skill_dirs,
    merge_shared_skills_trusted_dirs,
    load_yaml_dict,
    resolve_shipped_template_config_path,
)
from jiuwenswarm.dotenv_early import load_dotenv_runtime

load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
reset_free_search_runtime_flags()
TodoModifyTool = CompatibleTodoModifyTool
install_todo_modify_compat_patch()
install_evolution_rail_kwargs_compat()

_react_config = get_config().get("react", {})

_CRON_TOOL_CHANNEL_ID: ContextVar[str] = ContextVar(
    "cron_tool_channel_id",
    default=CronTargetChannel.WEB.value,
)
_CRON_TOOL_SESSION_ID: ContextVar[str | None] = ContextVar(
    "cron_tool_session_id",
    default=None,
)
_CRON_TOOL_METADATA: ContextVar[dict[str, Any] | None] = ContextVar(
    "cron_tool_metadata",
    default=None,
)
_CRON_TOOL_MODE: ContextVar[str | None] = ContextVar(
    "cron_tool_mode",
    default=None,
)
_CRON_TOOL_BOUND: ContextVar[bool] = ContextVar(
    "cron_tool_bound",
    default=False,
)

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
# First Model.invoke/stream of this request → stage=3 pre_llm (jiuwenswarm-only).
_LATENCY_PRE_LLM_MARKED: ContextVar[bool] = ContextVar(
    "latency_pre_llm_marked",
    default=False,
)

_REASONING_TRACE_LOG_BATCH = 5
_LLM_IO_TRACE_PATCH_APPLIED = False


def _log_latency_pre_llm_once(request_id: str | None) -> None:
    """Mark ③ endpoint once per request, right before provider call."""
    if _LATENCY_PRE_LLM_MARKED.get():
        return
    _LATENCY_PRE_LLM_MARKED.set(True)
    logger.info(
        "[latency] stage=3 name=pre_llm request_id=%s",
        (request_id or "").strip() or "-",
    )


@dataclass(slots=True)
class _DeepResearchRouteContextToken:
    token: Token[dict[str, object] | None] | None


@dataclass(slots=True)
class _RuntimeCronContextTokens:
    channel: Token[str] | None
    session: Token[str | None] | None
    metadata: Token[dict[str, Any] | None] | None
    mode: Token[str | None] | None
    bound: Token[bool] | None
    shell: Token[str | None] | None
    deepresearch: _DeepResearchRouteContextToken | None
    send_file: Token | None = None


def get_runtime_tool_session_id() -> str | None:
    """Session id bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_SESSION_ID.get()


def get_runtime_tool_channel_id() -> str:
    """Channel id bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_CHANNEL_ID.get()


def get_runtime_tool_metadata() -> dict[str, Any] | None:
    """Request metadata bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_METADATA.get()

logger = logging.getLogger(__name__)

_PERSISTENT_CHECKPOINTER_LOCK: asyncio.Lock | None = None
_PERSISTENT_CHECKPOINTER_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_PERSISTENT_CHECKPOINTER_LOCK_INIT = threading.Lock()
_PERSISTENT_CHECKPOINTER_READY = False
_shared_checkpoint_checkpointer: Any = None
_shared_mysql_checkpoint_engine: Any = None
_shared_postgresql_checkpoint_engine: Any = None


def reset_shared_checkpoint_for_tests() -> None:
    """Reset process-wide checkpoint singleton (tests only)."""
    global _shared_checkpoint_checkpointer, _PERSISTENT_CHECKPOINTER_READY
    global _PERSISTENT_CHECKPOINTER_LOCK, _PERSISTENT_CHECKPOINTER_LOCK_LOOP
    global _shared_mysql_checkpoint_engine, _shared_postgresql_checkpoint_engine
    _shared_checkpoint_checkpointer = None
    _PERSISTENT_CHECKPOINTER_READY = False
    _PERSISTENT_CHECKPOINTER_LOCK = None
    _PERSISTENT_CHECKPOINTER_LOCK_LOOP = None
    _shared_mysql_checkpoint_engine = None
    _shared_postgresql_checkpoint_engine = None


def _gateway_db_pool_kwargs() -> dict[str, Any]:
    """SQLAlchemy 连接池参数（``GATEWAY_DB_POOL_*`` / ``RUNTIME_DB_POOL_*``）。"""

    def _int_env(*names: str, default: int) -> int:
        for name in names:
            raw = os.getenv(name, "").strip()
            if not raw:
                continue
            try:
                return int(raw)
            except ValueError:
                continue
        return default

    return {
        "pool_size": _int_env("GATEWAY_DB_POOL_SIZE", "RUNTIME_DB_POOL_SIZE", default=2),
        "max_overflow": _int_env("GATEWAY_DB_MAX_OVERFLOW", "RUNTIME_DB_MAX_OVERFLOW", default=20),
        "pool_timeout": _int_env("GATEWAY_DB_POOL_TIMEOUT", "RUNTIME_DB_POOL_TIMEOUT", default=30),
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _get_persistent_checkpointer_lock() -> asyncio.Lock:
    """Lazy-init or rebind the process-wide checkpointer lock to the running loop.

    ``asyncio.Lock`` binds to the loop of its first ``acquire()``; any later
    acquire from a different loop raises ``RuntimeError("... is bound to a
    different event loop")``. The first call may have happened on a transient
    loop (test harness, ephemeral worker) that is gone by the time AgentServer's
    main loop needs the lock. To stay correct under that drift we:

    1. Lazily construct the Lock inside the caller's running loop (guarded by a
       ``threading.Lock`` so concurrent first-acquires from different threads
       cannot create two Locks).
    2. Before returning, verify the Lock is still bound to the current loop. If
       it is bound to a dead/foreign loop, drop and rebuild it so the caller can
       re-acquire safely. This rebinding is also done under the threading guard.

    After ``_PERSISTENT_CHECKPOINTER_READY`` flips True no one acquires the lock,
    so rebinding is a no-op for the steady state.
    """
    global _PERSISTENT_CHECKPOINTER_LOCK, _PERSISTENT_CHECKPOINTER_LOCK_LOOP
    current = _running_loop()
    if current is None:
        raise RuntimeError(
            "_get_persistent_checkpointer_lock must be called from a running event loop"
        )
    with _PERSISTENT_CHECKPOINTER_LOCK_INIT:
        bound = _PERSISTENT_CHECKPOINTER_LOCK_LOOP
        if _PERSISTENT_CHECKPOINTER_LOCK is None or bound is None or bound is not current:
            if bound is not None and bound is not current and not bound.is_closed():
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] _PERSISTENT_CHECKPOINTER_LOCK rebound: "
                    "old_loop=%r new_loop=%r (lock repr=%r)",
                    bound,
                    current,
                    _PERSISTENT_CHECKPOINTER_LOCK,
                )
            _PERSISTENT_CHECKPOINTER_LOCK = asyncio.Lock()
            _PERSISTENT_CHECKPOINTER_LOCK_LOOP = current
    return _PERSISTENT_CHECKPOINTER_LOCK

# Persistence identity of the single-agent card. It is the entity segment of
# every checkpointer key ("{session_id}:agent:{card_id}:..."), so it must stay
# constant across restarts; tool ownership is scoped separately, see
# ``JiuWenSwarmDeepAdapter._tool_owner_id``.
_AGENT_CARD_ID = "jiuwenswarm"

# Holder count per registered SysOperation id, keyed process-wide because
# ``Runner.resource_mgr`` is a process-global singleton. A local sys operation is
# owned by exactly one adapter (its card id is a fresh uuid), but a sandbox one is
# shared by every adapter resolving the same isolation key, so a single adapter's
# cleanup must not drop a registration a live sibling still uses. The count is a
# multiset of acquisitions: an adapter that rebuilds its agent onto the same id
# retains before it releases, so the entry never dips to zero in between.
#
# Scope: only adapters count here. Code that registers a SysOperation directly on
# ``Runner.resource_mgr`` (``auto_memory.extraction_runner``) stays outside this
# table, which is safe today because those ids are private to their creator and
# never resolve through ``_resolve_sys_operation``'s isolation-key reuse. Any new
# registrar that could share an id with an adapter must take a reference here too.
_SYS_OPERATION_REFCOUNTS: dict[str, int] = {}
_SYS_OPERATION_REFCOUNT_LOCK = threading.Lock()


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
            _ev_token = begin_llm_trace_event()
            try:
                try:
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
                except Exception:
                    logger.debug("[llm_trace] log_invoke_input failed", exc_info=True)
                _log_latency_pre_llm_once(trace_rid)
                result = await original_invoke(
                    self, messages, tools=tools, model=model, **kwargs
                )
                try:
                    log_invoke_output(
                        session_id=trace_sid,
                        request_id=trace_rid,
                        iteration=resolved_iter,
                        model_name=model_name,
                        assistant_msg=result,
                    )
                except Exception:
                    logger.debug("[llm_trace] log_invoke_output failed", exc_info=True)
                return result
            finally:
                end_llm_trace_event(_ev_token)

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
            _ev_token = begin_llm_trace_event()
            try:
                try:
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
                except Exception:
                    logger.debug("[llm_trace] log_stream_input failed", exc_info=True)
                _log_latency_pre_llm_once(trace_rid)
                accumulated: Any = None
                reasoning_seq = 0
                reasoning_trace_pending: List[Tuple[int, str]] = []

                def emit_reasoning_trace_batch() -> None:
                    if not reasoning_trace_pending:
                        return
                    try:
                        log_reasoning_delta(
                            session_id=trace_sid,
                            request_id=trace_rid,
                            iteration=resolved_iter,
                            model_name=model_name,
                            reasoning_seq=reasoning_trace_pending[0][0],
                            fragment="".join(t[1] for t in reasoning_trace_pending),
                        )
                    except Exception:
                        logger.debug("[llm_trace] log_reasoning_delta failed", exc_info=True)
                    reasoning_trace_pending.clear()

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
                    try:
                        log_stream_output(
                            session_id=trace_sid,
                            request_id=trace_rid,
                            iteration=resolved_iter,
                            model_name=model_name,
                            assistant_msg=accumulated,
                        )
                    except Exception:
                        logger.debug("[llm_trace] log_stream_output failed", exc_info=True)
            except Exception:
                emit_reasoning_trace_batch()
                raise
            finally:
                end_llm_trace_event(_ev_token)

        Model.invoke = _traced_invoke
        Model.stream = _traced_stream
        _LLM_IO_TRACE_PATCH_APPLIED = True
        logger.info("[JiuWenSwarmDeepAdapter] LLM IO trace patch applied")
    except Exception:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] Failed to apply LLM IO trace patch", exc_info=True
        )


_ACP_BLOCKED_DEFAULT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "bash",
    }
)
_SKILL_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "skill_index_build",
        "skill_branch_explore",
        "skill_branch_peek",
    }
)
# Total ``_update_runtime_config`` cost above which its per-stage breakdown is
# worth an INFO line. It runs once per turn ahead of the model call, so anything
# at this scale is directly visible in time-to-first-token.
_SLOW_RUNTIME_CONFIG_MS = 50.0

# Rail construction reports unconditionally: it happens once per agent, not per
# turn, so a line per cold start is not noise, and cold start is precisely the
# budget nobody can otherwise account for. The earlier 100 ms bar was above the
# real cost, which meant the breakdown never appeared at INFO on a normal run.
_SLOW_RAIL_BUILD_MS = 0.0

# Profiling escape hatch: overrides every stage-breakdown threshold, in
# milliseconds. Set it to 0 to report all breakdowns at INFO. The thresholds
# above are tuned to stay quiet on a healthy run, which is the wrong setting
# when the question is "where did this un-slow half second go".
_STAGE_LOG_THRESHOLD_ENV = "JIUWENSWARM_SLOW_STAGE_MS"


def _stage_breakdown_logger(total_ms: float, threshold_ms: float) -> Callable[..., None]:
    """Pick the level a stage breakdown should be reported at.

    Both branches go through ``server_logger`` on purpose. The quiet branch
    used to go to this module's standard-logging logger, which put it in a
    different sink running at INFO — so the sub-threshold breakdown, the one
    needed to explain time that is not obviously slow, was never visible
    anywhere.

    Args:
        total_ms: Measured total for the stage group.
        threshold_ms: Site-specific bar above which the breakdown is INFO.

    Returns:
        The logging callable to emit the breakdown with.
    """
    override = os.environ.get(_STAGE_LOG_THRESHOLD_ENV)
    if override is not None:
        try:
            threshold_ms = float(override)
        except ValueError:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ignoring non-numeric %s=%s",
                _STAGE_LOG_THRESHOLD_ENV,
                override,
            )
    return server_logger.info if total_ms >= threshold_ms else server_logger.debug


@dataclass(frozen=True)
class _GitSnapshot:
    """Git state as of a conversation's first turn, held for its whole life.

    Attributes:
        head: Raw ``HEAD`` file contents when the snapshot was taken. Checking
            it costs a file read rather than a subprocess, which is what makes
            it affordable to verify on every turn.
        branch: Current branch name, or "HEAD" when detached.
        status: ``git status --short`` output, capped at 50 lines.
        recent_commits: ``git log --oneline -5`` output.
    """

    head: str
    branch: str
    status: str
    recent_commits: str


def _read_git_head(head_file: str) -> str:
    """Read the HEAD pointer directly, without spawning git.

    Changes whenever the checkout moves — switching branches rewrites the
    symbolic ref, and a detached checkout writes a different commit id — while
    staying untouched by ordinary edits to the working tree. That makes it a
    cheap way to tell "the conversation moved to a different checkout" apart
    from "the user edited a file", which need opposite treatment: the former
    must invalidate the snapshot, the latter must not.

    Args:
        head_file: Absolute path to the git directory's HEAD file.

    Returns:
        Stripped file contents, or an empty string when it cannot be read —
        in which case the caller degrades to holding the snapshot as-is.
    """
    if not head_file:
        return ""
    try:
        return Path(head_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class _StableGitFacts:
    """Git facts about a project that do not change between turns of a chat.

    Attributes:
        is_repo: Whether the directory is inside a git work tree.
        user_name: ``user.name`` from git config; empty when unset.
        main_branch: First of origin/main, origin/master, main, master that
            resolves; empty when none do.
        head_file: Absolute path to the git directory's HEAD file. Resolved via
            git so it is correct for a subdirectory, a worktree or a submodule,
            where it is nowhere near ``<project_dir>/.git/HEAD``.
    """

    is_repo: bool
    user_name: str
    main_branch: str
    head_file: str


_STABLE_GIT_FACTS: dict[str, _StableGitFacts] = {}
_STABLE_GIT_FACTS_LOCK = threading.Lock()


def _resolve_stable_git_facts(
    git_bin: str,
    project_dir: str,
    run_git: Callable[[list[str]], str],
) -> _StableGitFacts:
    """Resolve the per-project git facts once and reuse them afterwards.

    ``_write_runtime_state`` runs on every turn and used to spawn up to nine git
    subprocesses each time. Six of them answered questions whose answers cannot
    change while a conversation is in progress — whether the directory is a repo,
    who the committer is, and which of the four candidate names the main branch
    goes by — so they are resolved once per project directory. Branch, status and
    recent commits stay live, because those are exactly what changes while the
    user works.

    Args:
        git_bin: Resolved git executable, part of the cache key so a toolchain
            switch is not served a stale answer.
        project_dir: Directory the git commands run in.
        run_git: Callable running git in ``project_dir`` and returning stripped
            stdout, or an empty string on failure.

    Returns:
        The cached facts for this project directory.
    """
    cache_key = f"{git_bin}\0{project_dir}"
    with _STABLE_GIT_FACTS_LOCK:
        cached = _STABLE_GIT_FACTS.get(cache_key)
    if cached is not None:
        return cached

    is_repo = run_git(["rev-parse", "--is-inside-work-tree"]) == "true"
    user_name = ""
    main_branch = ""
    head_file = ""
    if is_repo:
        user_name = run_git(["config", "user.name"])
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if run_git(["rev-parse", "--verify", "--quiet", candidate]):
                main_branch = candidate
                break
        git_dir = run_git(["rev-parse", "--absolute-git-dir"])
        if git_dir:
            head_file = str(Path(git_dir) / "HEAD")

    facts = _StableGitFacts(
        is_repo=is_repo,
        user_name=user_name,
        main_branch=main_branch,
        head_file=head_file,
    )
    with _STABLE_GIT_FACTS_LOCK:
        _STABLE_GIT_FACTS[cache_key] = facts
    return facts


@dataclass
class _RailBuildInfo:
    """One rail's construction recipe, shared by the agent and code rail sets.

    Attributes:
        attr_name: Adapter attribute the built rail is assigned to. Also names
            the rail in the build-timing breakdown, minus its leading underscore.
        build_func: Callable returning the rail instance, or None to skip it.
        params: Keyword arguments for ``build_func``; empty when omitted.
    """

    attr_name: str
    build_func: Callable
    params: dict = None

    def __post_init__(self):
        """Normalize the optional params mapping to an empty dict."""
        self.params = self.params or {}

_CRON_TOOL_NAMES = frozenset(
    {
        "cron",
        "cron_list_jobs",
        "cron_get_job",
        "cron_create_job",
        "cron_update_job",
        "cron_delete_job",
        "cron_toggle_job",
        "cron_preview_job",
    }
)

_DEFAULT_PROGRESSIVE_EAGER_TOOLS = [
    "tools_search",
    "invoke_tool",
    "web_search",
    "fetch_webpage",
    "ask_user",
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "skill_tool",
    "skill_complete",
    "todo_create",
    "todo_list",
    "todo_modify",
]

_PROGRESSIVE_META_TOOL_NAMES = frozenset({"tools_search", "invoke_tool"})
_LEGACY_PROGRESSIVE_EAGER_TOOL_ALIASES = {
    "ask_user_question": "ask_user",
}


def _normalize_tool_names(value: Any, default: list[str] | None = None) -> list[str]:
    """Normalize comma-separated/list tool-name config values."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default or [])


def _ensure_progressive_meta_tools(eager_tools: list[str]) -> list[str]:
    """Ensure tools_search / invoke_tool are in the eager list."""
    if "tools_search" not in eager_tools:
        eager_tools.insert(0, "tools_search")
    if "invoke_tool" not in eager_tools:
        eager_tools.insert(1, "invoke_tool")
    return eager_tools


def _normalize_progressive_eager_tools(
    value: Any,
    default: list[str] | None = None,
) -> list[str]:
    """Normalize eager tools and migrate legacy registered names."""
    eager_tools: list[str] = []
    for tool_name in _normalize_tool_names(value, default):
        normalized_name = _LEGACY_PROGRESSIVE_EAGER_TOOL_ALIASES.get(
            tool_name,
            tool_name,
        )
        if normalized_name not in eager_tools:
            eager_tools.append(normalized_name)
    return _ensure_progressive_meta_tools(eager_tools)


def is_subagent_tool_lazy_load_enabled(react_config: dict[str, Any] | None) -> bool:
    """True when react.tool_lazy_load and subagents are both enabled."""
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        return False
    if not lazy_cfg.get("enabled", False):
        return False
    sub_cfg = lazy_cfg.get("subagents") or {}
    if not isinstance(sub_cfg, dict):
        return False
    return bool(sub_cfg.get("enabled", False))


def build_progressive_tool_rail_from_config(
    react_config: dict[str, Any],
    *,
    language: str,
    profile: str = "main",
    agent_id: str | None = None,
    agent_card_id: str | None = None,
    subagent_kind: str | None = None,
    deepresearch_context_provider: Callable[[], dict[str, str]] | None = None,
) -> ProgressiveToolRail | None:
    """Build ProgressiveToolRail from react.tool_lazy_load config.

    Fixed eager-tools schema; deferred tools are reached via tools_search +
    invoke_tool. For subagents, set profile="subagent" and configure
    react.tool_lazy_load.subagents.
    """
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        lazy_cfg = {}

    if not lazy_cfg.get("enabled", False):
        return None

    enable_for_models = _normalize_tool_names(
        lazy_cfg.get("enable_for_models", []), []
    )

    normalized_profile = (profile or "main").strip().lower()
    if normalized_profile == "subagent":
        sub_cfg = lazy_cfg.get("subagents") or {}
        if not isinstance(sub_cfg, dict):
            sub_cfg = {}
        if not sub_cfg.get("enabled", False):
            return None

        main_eager = _normalize_progressive_eager_tools(
            lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
            _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
        )
        if sub_cfg.get("inherit_parent_eager_tools", False):
            eager_tools = list(main_eager)
        else:
            eager_tools = _normalize_progressive_eager_tools(
                sub_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
                _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
            )
    else:
        eager_tools = _normalize_progressive_eager_tools(
            lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
            _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
        )
        # This tool owns native multi-step HITL. It must execute directly;
        # invoke_tool would hide the outer call from its lifecycle Rail.
        if "deepresearch_execute" not in eager_tools:
            eager_tools.insert(2, "deepresearch_execute")

    normalized_language = resolve_language(language)
    logger.info(
        "[ProgressiveToolRail] enabled profile=%s kind=%s eager_tools=%s "
        "agent_id=%s agent_card_id=%s enable_for_models=%s",
        normalized_profile,
        subagent_kind or "",
        eager_tools,
        agent_id,
        agent_card_id,
        enable_for_models,
    )

    return ProgressiveToolRail(
        enabled=True,
        eager_tools=eager_tools,
        language=normalized_language,
        agent_id=agent_id,
        agent_card_id=agent_card_id,
        enable_for_models=enable_for_models,
        deepresearch_context_provider=deepresearch_context_provider,
    )


def _set_skill_evolution_triggers(
    rail: Any,
    *,
    review_trigger: bool,
    signal_trigger: bool = True,
) -> None:
    rail.review_trigger = review_trigger
    rail.signal_trigger = signal_trigger


def _clean_heartbeat_content(content: str) -> str:
    """Remove HTML comments and blank lines from HEARTBEAT.md content."""
    cleaned_lines: list[str] = []
    for line in content.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("<!--") and stripped_line.endswith("-->"):
            continue
        if stripped_line:
            cleaned_lines.append(stripped_line)
    return "\n".join(cleaned_lines)


def init_permission_engine(*_args: Any, **_kwargs: Any) -> None:
    """Legacy shim for tests/older call sites.

    The project now relies on openjiuwen's PermissionInterruptRail and does not
    require a standalone permission engine initialization step.
    """
    return None


def _mcc_looks_usable(mcc: dict) -> bool:
    """检查 model_client_config 是否包含有效的 API 凭据。"""
    api_base = str(mcc.get("api_base", "") or "").strip()
    if not api_base or is_placeholder_api_base(api_base):
        return False

    provider = mcc.get("client_provider", "")
    provider = getattr(provider, "value", provider)
    if is_openai_account_provider(str(provider or "")):
        return True

    api_key = str(mcc.get("api_key", "") or "").strip()
    return bool(api_key)


def parse_int(value: Any, default: int) -> int:
    """Parse integer-like values safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_instance_config_base(config_base: dict[str, Any] | None) -> dict[str, Any]:
    if config_base is None:
        return get_config()
    if not isinstance(config_base, dict):
        raise TypeError("config_base must be a dict when provided")
    # 外部传入的 config_base（如企业同步的稀疏 override）与 shipped 模板做补缺型
    # 合并：模板补全缺失键（如 react.subagents），外部显式键与独有键全部保留，
    # 保证与 config_base=None 时 get_config() 的模板合并语义一致。
    template = load_yaml_dict(resolve_shipped_template_config_path())
    return resolve_env_vars(fill_template_defaults(config_base, template))


def _deep_agent_context_engine_config(react_cfg: dict[str, Any] | None) -> ContextEngineConfig:
    """供 ``create_deep_agent(..., context_engine_config=...)`` 使用（与 agent-core 集成测试方法二一致）。

    仅承接 ContextEngine 自身配置；KV cache affinity 由独立
    ``react.kv_cache_affinity_config`` 管理。
    """
    react_cfg = react_cfg or {}
    cec = react_cfg.get("context_engine_config")
    cec = cec if isinstance(cec, dict) else {}
    recall = cec.get("compression_recall_config")
    recall = recall if isinstance(recall, dict) else {}
    return ReActAgentConfig().context_engine_config.model_copy(
        update={
            "enable_reload": bool(cec.get("enable_reload", False)),
            "enable_openrouter_model_context_window_tokens": bool(
                cec.get("enable_openrouter_model_context_window_tokens", False)
            ),
            # 显式设置的上下文窗口上限；非法值回退 None（由 agent-core 按模型解析）
            "context_window_tokens": parse_int(cec.get("context_window_tokens"), None),
            # 上下文压缩 debug 落盘开关；目录缺省时由 agent-core 按
            # OPENJIUWEN_CONTEXT_DEBUG_DIR / workspace 默认路径解析
            "enable_context_debug": bool(cec.get("enable_context_debug", False)),
            "context_debug_dir": cec.get("context_debug_dir") or None,
            # 压缩召回：压缩时归档原始消息，供模型按需召回
            "compression_recall_config": CompressionRecallConfig(
                enabled=bool(recall.get("enabled", False)),
                chunk_size_tokens=parse_int(recall.get("chunk_size_tokens"), 3000),
                chunk_overlap_tokens=parse_int(recall.get("chunk_overlap_tokens"), 300),
            ),
        }
    )


def _model_provider(model: Any) -> str:
    for owner in (model, getattr(model, "_client", None)):
        model_client_config = getattr(owner, "model_client_config", None)
        provider = getattr(model_client_config, "client_provider", None)
        if provider is not None:
            return str(getattr(provider, "value", provider) or "").strip()
    return ""


def _deep_agent_kv_cache_affinity_config(
        react_cfg: dict[str, Any] | None,
        model: Model | None = None,
) -> KVCacheAffinityConfig:
    """Build the ReActAgent KV cache affinity config from jiuwenswarm config."""
    react_cfg = react_cfg or {}
    kv_cfg = react_cfg.get("kv_cache_affinity_config")
    kv_cfg = kv_cfg if isinstance(kv_cfg, dict) else {}
    affinity_enabled = bool(kv_cfg.get("enable_kv_cache_affinity", False))
    if affinity_enabled and model is not None:
        provider = _model_provider(model)
        if provider != "AscendAffinity":
            logger.warning(
                "[JiuWenSwarmDeepAdapter] KV cache affinity failed closed: "
                "model provider=%s requires=AscendAffinity",
                provider or "<empty>",
            )
            affinity_enabled = False
    return KVCacheAffinityConfig(
        enable_kv_cache_release=bool(kv_cfg.get("enable_kv_cache_release", False)),
        enable_kv_cache_affinity=affinity_enabled,
    )


def _build_context_assemble_rail() -> ContextAssembleRail | None:
    """Build ContextAssembleRail."""
    try:
        context_assemble_rail = ContextAssembleRail()
        logger.info("[JiuWenSwarmDeepAdapter] ContextAssembleRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] ContextAssembleRail create failed: %s", exc)
        context_assemble_rail = None
    return context_assemble_rail


def _resolve_session_memory_config(context_engine_cfg: dict[str, Any]) -> dict[str, Any] | None:
    raw_config = (
        context_engine_cfg.get("session_memory_config")
        or context_engine_cfg.get("session_memory")
    )
    if raw_config is True:
        return {}
    if isinstance(raw_config, dict):
        return raw_config
    return None


def _build_context_processor_rail(config: dict[str, Any]) -> ContextProcessorRail | None:
    """Build ContextProcessorRail with user config.

    从配置中读取 processor 配置，传递给 ContextProcessorRail。

    Args:
        config: 配置字典
    """
    try:
        user_processors: List[Tuple[str, dict]] = []
        raw_context_engine_cfg = config.get("context_engine_config", {})
        context_engine_cfg = raw_context_engine_cfg if isinstance(raw_context_engine_cfg, dict) else {}
        session_memory_cfg = _resolve_session_memory_config(context_engine_cfg)

        offloader_cfg = context_engine_cfg.get("message_summary_offloader_config", {})
        if isinstance(offloader_cfg, dict) and offloader_cfg:
            user_processors.append(("MessageSummaryOffloader", offloader_cfg))

        # 会话记忆：preset 链中默认为禁用，此处用配置覆盖启用
        session_memory_compressor_cfg = context_engine_cfg.get("session_memory_compressor_config", {})
        if isinstance(session_memory_compressor_cfg, dict) and session_memory_compressor_cfg:
            user_processors.append(("SessionMemoryCompressor", session_memory_compressor_cfg))

        compressor_cfg = context_engine_cfg.get("dialogue_compressor_config", {})
        if isinstance(compressor_cfg, dict) and compressor_cfg:
            user_processors.append(("DialogueCompressor", compressor_cfg))

        current_round_cfg = context_engine_cfg.get("current_round_compressor_config", {})
        if isinstance(current_round_cfg, dict) and current_round_cfg:
            user_processors.append(("CurrentRoundCompressor", current_round_cfg))

        round_level_cfg = context_engine_cfg.get("round_level_compressor_config", {})
        if isinstance(round_level_cfg, dict) and round_level_cfg:
            user_processors.append(("RoundLevelCompressor", round_level_cfg))

        # 兜底压缩：整窗口 token 超阈值时调一次 LLM 把历史压成 summary。
        # ContextOverflowRecoveryRail 在 413/上下文溢出时靠 set_force_compact 触发。
        # 注意：FullCompactProcessor 不在 0.1.16 preset 链中，_merge_processors 对非 preset
        # processor 不接受 dict（无 base_cfg 可合并），必须传完整 BaseModel 实例。
        full_compact_cfg = context_engine_cfg.get("full_compact_processor_config", {})
        if isinstance(full_compact_cfg, dict) and full_compact_cfg:
            user_processors.append(
                ("FullCompactProcessor", FullCompactProcessorConfig(**full_compact_cfg))
            )

        reasoning_loop_cfg = context_engine_cfg.get("reasoning_tool_loop_compact_config", {})
        if isinstance(reasoning_loop_cfg, dict) and reasoning_loop_cfg:
            reasoning_loop_cfg = {
                **reasoning_loop_cfg,
                "language": resolve_language(
                    str(config.get("preferred_language", "zh")).strip().lower()
                ),
            }
            user_processors.append(("ReasoningToolLoopCompactProcessor", reasoning_loop_cfg))

        context_rail = ContextProcessorRail(
            processors=user_processors if user_processors else None,
            preset=True,
            session_memory=session_memory_cfg,
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] ContextProcessorRail create success for agent mode, "
            "user_processors=%s session_memory=%s",
            [p[0] for p in user_processors] if user_processors else "none",
            "enabled" if isinstance(session_memory_cfg, dict) else "disabled",
        )
        return context_rail
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] ContextProcessorRail create failed: %s", exc)
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


async def _build_mysql_async_engine():
    """构建 / 复用 checkpoint MySQL AsyncEngine。

    连接参数从 ``GATEWAY_DB_*`` 读取；池参数见 ``GATEWAY_DB_POOL_*``。
    进程内复用同一 engine，避免每个 agent 再建独立连接池。
    未配置 ``GATEWAY_DB_HOST`` 或非企业版时返回 None，由调用方决定是否回退 SQLite。
    配置了 ``GATEWAY_DB_HOST`` 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite 不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    global _shared_mysql_checkpoint_engine
    if not is_enterprise():
        return None
    if _shared_mysql_checkpoint_engine is not None:
        return _shared_mysql_checkpoint_engine
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        db_port = os.getenv("GATEWAY_DB_PORT", "3306").strip()
        db_user = os.getenv("GATEWAY_DB_USER", "root").strip()
        db_password = os.getenv("GATEWAY_DB_PASSWORD", "").strip()
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        # URL-encode 用户名/密码，避免特殊字符（如 @ : / # %）破坏连接串解析
        _encoded_user = quote_plus(db_user)
        _encoded_password = quote_plus(db_password)
        # 先连接不指定库，自动建库
        server_url = (
            f"mysql+aiomysql://{_encoded_user}:{_encoded_password}"
            f"@{db_host}:{db_port}?charset=utf8mb4"
        )
        temp_engine = create_async_engine(server_url, echo=False)
        async with temp_engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        await temp_engine.dispose()

        # 再连接目标库
        db_url = (
            f"mysql+aiomysql://{_encoded_user}:{_encoded_password}"
            f"@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
        )
        pool_kwargs = _gateway_db_pool_kwargs()
        engine = create_async_engine(db_url, **pool_kwargs)
        logger.info(
            "[JiuWenSwarmDeepAdapter] checkpoint MySQL engine created: %s:%s/%s "
            "pool_size=%s max_overflow=%s pool_timeout=%s",
            db_host,
            db_port,
            db_name,
            pool_kwargs["pool_size"],
            pool_kwargs["max_overflow"],
            pool_kwargs["pool_timeout"],
        )

        # 确保 kv_store.value 列为 LONGTEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'kv_store' AND COLUMN_NAME = 'value'"
                ),
                {"db": db_name},
            )
            row = result.fetchone()
            if row is None:
                await conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS kv_store ("
                        "`key` VARCHAR(512) PRIMARY KEY,"
                        "`value` LONGTEXT NOT NULL"
                        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store table created with value LONGTEXT"
                )
            elif str(row[0]).upper() != "LONGTEXT":
                await conn.execute(
                    text(
                        "ALTER TABLE kv_store MODIFY COLUMN `value` LONGTEXT NOT NULL"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store.value altered to LONGTEXT"
                )

        _shared_mysql_checkpoint_engine = engine
        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenSwarmDeepAdapter] failed to create checkpoint MySQL engine: %s",
            exc,
        )
        raise


async def _build_postgresql_async_engine():
    """构建 / 复用 checkpoint PostgreSQL AsyncEngine。

    连接参数从 ``GATEWAY_DB_*`` 读取；池参数见 ``GATEWAY_DB_POOL_*``。
    进程内复用同一 engine，避免每个 agent 再建独立连接池。
    未配置 ``GATEWAY_DB_HOST`` 或非企业版时返回 None，由调用方决定是否回退 SQLite。
    配置了 ``GATEWAY_DB_HOST`` 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite 不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    global _shared_postgresql_checkpoint_engine
    if not is_enterprise():
        return None
    if _shared_postgresql_checkpoint_engine is not None:
        return _shared_postgresql_checkpoint_engine
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        from sqlalchemy import text
        from openjiuwen_runtime.foundation.db.postgresql_handler import PostgreSQLHandler

        db_port = os.getenv("GATEWAY_DB_PORT", "5432").strip()
        db_user = os.getenv("GATEWAY_DB_USER", "postgres").strip()
        db_password = os.getenv("GATEWAY_DB_PASSWORD", "").strip()
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        pg_schema = os.getenv("GATEWAY_PG_SCHEMA", "public").strip() or "public"
        handler = PostgreSQLHandler(
            host=db_host,
            port=int(db_port),
            database=db_name,
            schema=pg_schema,
            user=db_user,
            password=db_password,
        )
        # init_database 幂等：确保目标库与 schema 存在（schema 存在是 search_path 生效的前提）
        await handler.init_database()
        await handler.connect()
        engine = handler.get_engine()
        logger.info(
            "[JiuWenSwarmDeepAdapter] checkpoint PostgreSQL engine created: %s:%s/%s schema=%s",
            db_host,
            db_port,
            db_name,
            pg_schema,
        )

        # 确保 kv_store.value 列为 TEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_catalog = :db AND table_name = 'kv_store' "
                    "AND column_name = 'value'"
                ),
                {"db": db_name},
            )
            row = result.fetchone()
            if row is None:
                await conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS kv_store ("
                        "key VARCHAR(512) PRIMARY KEY,"
                        "value TEXT NOT NULL"
                        ")"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store table created with value TEXT"
                )
            elif str(row[0]).lower() != "text":
                await conn.execute(
                    text("ALTER TABLE kv_store ALTER COLUMN value TYPE TEXT")
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store.value altered to TEXT"
                )

        _shared_postgresql_checkpoint_engine = engine
        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenSwarmDeepAdapter] failed to create checkpoint PostgreSQL engine: %s",
            exc,
        )
        raise


async def ensure_persistent_checkpointer() -> None:
    """Ensure the process-wide default checkpointer uses sqlite / MySQL / PostgreSQL persistence."""
    global _PERSISTENT_CHECKPOINTER_READY, _shared_checkpoint_checkpointer

    if _PERSISTENT_CHECKPOINTER_READY:
        if _shared_checkpoint_checkpointer is not None:
            CheckpointerFactory.set_default_checkpointer(_shared_checkpoint_checkpointer)
        return

    t_cp_start = time.perf_counter()
    lock = await _get_persistent_checkpointer_lock()
    acquired = False
    try:
        try:
            await lock.acquire()
        except RuntimeError as acquire_exc:
            logger.exception(
                "[JiuWenSwarmDeepAdapter] _PERSISTENT_CHECKPOINTER_LOCK acquire failed: %s "
                "(lock repr=%r, running_loop=%r)",
                acquire_exc,
                lock,
                asyncio.get_event_loop(),
            )
            raise
        acquired = True
        if _PERSISTENT_CHECKPOINTER_READY:
            if _shared_checkpoint_checkpointer is not None:
                CheckpointerFactory.set_default_checkpointer(_shared_checkpoint_checkpointer)
            return

        try:
            PersistenceCheckpointerProvider()
            checkpoint_path = get_checkpoint_dir()
            conf: dict[str, Any] = {
                "db_type": "sqlite",
                "db_path": f"{checkpoint_path}/checkpoint",
            }

            # 企业版：CHECKPOINT_DB_TYPE=mysql|postgresql 时改用对应库（复用 GATEWAY_DB_*）
            checkpoint_db_type = os.getenv("CHECKPOINT_DB_TYPE", "").strip().lower()
            if is_enterprise():
                from openjiuwen_runtime.foundation.db.utils import is_mysql, is_postgresql
                if is_mysql(checkpoint_db_type):
                    mysql_engine = await _build_mysql_async_engine()
                    if mysql_engine is not None:
                        conf["db_client"] = mysql_engine
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] use mysql db_client for checkpointer"
                        )
                elif is_postgresql(checkpoint_db_type):
                    postgresql_engine = await _build_postgresql_async_engine()
                    if postgresql_engine is not None:
                        conf["db_client"] = postgresql_engine
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] use postgresql db_client for checkpointer"
                        )

            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf=conf),
            )
            _shared_checkpoint_checkpointer = checkpointer
            CheckpointerFactory.set_default_checkpointer(checkpointer)
            _PERSISTENT_CHECKPOINTER_READY = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] persistent checkpointer ready: %s elapsed_ms=%.1f",
                checkpoint_path / "checkpoint",
                (time.perf_counter() - t_cp_start) * 1000,
            )
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] fail to setup checkpoint due to: %s "
                "elapsed_ms=%.1f",
                exc,
                (time.perf_counter() - t_cp_start) * 1000,
            )
            raise RuntimeError("persistent checkpointer initialization failed") from exc
    finally:
        if acquired:
            lock.release()


_MODE_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "agent": {"cn": "智能体模式", "en": "Agent Mode"},
    # 历史 token 归一到合并后的 agent 显示名（兼容旧会话 / 旧请求）。
    "agent.plan": {"cn": "智能体模式", "en": "Agent Mode"},
    "agent.fast": {"cn": "智能体模式", "en": "Agent Mode"},
    "team": {"cn": "集群模式", "en": "Cluster Mode"},
    "team.plan": {"cn": "集群计划模式", "en": "Cluster Plan Mode"},
    "code.team": {"cn": "代码集群模式", "en": "Code Team Mode"},
}


def _try_add_cache_control(msg: Any) -> None:
    """Add cache_control to the last content block of a message.

    Only modifies dict-based content blocks (safe for openjiuwen message types
    where content is ``Union[str, List[Union[str, dict]]]``). If the last block
    is a dict, we add ``cache_control: {"type": "ephemeral"}`` to it.

    Mark the last pre-prompt message for prompt caching, 
    while the btw/recap prompt itself carries no marker
    (skipCacheWrite — the side response doesn't create a new cache entry).

    String content is left untouched — converting it to a list would change
    the wire format and break the byte-identical prefix needed for cache hits.
    """
    content = getattr(msg, "content", None)
    if content is None:
        return
    if isinstance(content, list) and len(content) > 0:
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}


class _RuntimeCronToolContext:
    """Stable cron tool context proxy backed by per-task contextvars."""

    def __init__(self, tool_scope: str) -> None:
        self._tool_scope = tool_scope
        self._fallback_channel_id = CronTargetChannel.WEB.value
        self._fallback_session_id: str | None = None
        self._fallback_metadata: dict[str, Any] | None = None
        self._fallback_mode: str | None = None

    def remember_current_binding(self) -> None:
        self._fallback_channel_id = _CRON_TOOL_CHANNEL_ID.get()
        self._fallback_session_id = _CRON_TOOL_SESSION_ID.get()
        metadata = _CRON_TOOL_METADATA.get()
        self._fallback_metadata = dict(metadata) if isinstance(metadata, dict) else None
        self._fallback_mode = _CRON_TOOL_MODE.get()

    @property
    def channel_id(self) -> str:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_CHANNEL_ID.get()
        return self._fallback_channel_id

    @property
    def session_id(self) -> str | None:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_SESSION_ID.get()
        return self._fallback_session_id

    @property
    def metadata(self) -> dict[str, Any] | None:
        if _CRON_TOOL_BOUND.get():
            metadata = _CRON_TOOL_METADATA.get()
            if isinstance(metadata, dict):
                return metadata
        return self._fallback_metadata

    @property
    def mode(self) -> str | None:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_MODE.get()
        return self._fallback_mode

    @property
    def tool_scope(self) -> str:
        return self._tool_scope


def _agent_ras_kwargs_from_config(config_base: dict[str, Any] | None) -> dict[str, Any]:
    """Thin YAML gate for create_deep_agent ``agent_ras``.

    Schema validation stays in agent-core. Missing / non-dict section → do
    not pass (core defaults to Agent RAS enabled). Explicit ``enabled: false``
    passes ``False`` to disable. Otherwise pass a deep-copied raw dict.
    """
    raw = (config_base or {}).get("agent_ras")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ignoring invalid agent_ras section: "
                "expected dict, got %s",
                type(raw).__name__,
            )
        return {}
    if raw.get("enabled", True) is not True:
        return {"agent_ras": False}
    return {"agent_ras": copy.deepcopy(raw)}


class JiuWenSwarmDeepAdapter:
    SESSION_ADAPTER_IDLE_TTL_SEC = 2 * 60 * 60
    SESSION_ADAPTER_EVICT_BATCH_SIZE = 3
    SESSION_ADAPTER_RELOAD_RETRY_INTERVAL_SEC = 30.0
    _RUNTIME_STATE_WRITE_LIMIT = threading.BoundedSemaphore(2)
    # Process-level cache for AGENT_EXTRA_TOOLS loads, keyed by module path
    # (see _load_extra_tools_from_env): session-scoped adapters rebuild per
    # session, and extra tools register process-shared under their card id,
    # so the same module must resolve to the same instances on every build.
    _EXTRA_TOOLS_CACHE: dict[str, list[Any]] = {}
    _EXTRA_TOOLS_CACHE_LOCK = threading.Lock()

    """Deep SDK 适配器，实现 AgentAdapter 协议.

    封装所有 Deep SDK 专属逻辑：
    - DeepAgent 实例生命周期管理
    - Deep runtime tools 注册
    - Deep stream event 解析
    - Deep evolution 绑定
    - Deep interrupt / user_answer 处理
    """

    def __init__(
        self,
        workspace_dir: str | None = None,
        agent_id: str | None = None,
        service_id: str | None = None,
    ) -> None:
        # Apply the MCP per-call timeout patch once per process: wraps
        # StreamableHttpClient/SseClient.call_tool & list_tools in
        # asyncio.wait_for and honors config ``timeout_s`` (--timeout_s), so a
        # killed remote MCP server fails fast instead of hanging on the MCP
        # SDK's 300s SSE read timeout. Idempotent (module-level _PATCHED guard).
        apply_mcp_call_timeout_patch()
        self._instance: DeepAgent | None = None
        self._project_dir: str | None = None
        # 企业多租户：企业版下可用外部传入的隔离 workspace / 租户 ID
        enterprise = is_enterprise()
        if workspace_dir and enterprise:
            self._workspace_dir: str = str(
                collapse_nested_agent_workspace_dir(workspace_dir)
            )
        else:
            self._workspace_dir: str = str(
                collapse_nested_agent_workspace_dir(get_agent_workspace_dir())
            )
        self._agent_id = agent_id if enterprise else None
        self._service_id = service_id if enterprise else None
        self._agent_name: str = "main_agent"
        # 是否是 code-agent 形态. 基类 (deep adapter) 默认 False, 由子类
        # JiuwenSwarmCodeAdapter 在 __init__ 里改成 True. 该字段透传给
        # sysop_builder 的 ``build_filesystem_policy`` / ``create_sandbox_
        # sysop_card``, 决定沙箱挂的"主写入根"是用户工程目录 (project_dir,
        # code-agent 场景) 还是 agent 自己的 workspace 目录 (deep agent
        # 场景). 单点 source-of-truth, 避免分布在多个方法里靠 isinstance
        # 或字符串嗅探 agent_name 反推。
        self._is_code_agent: bool = False
        self._vision_tools_registered: bool = False
        self._audio_tools_registered: bool = False
        self._video_tool_registered: bool = False
        self._image_gen_tool_registered: bool = False
        self._model: Model | None = None
        self._model_client_config: ModelClientConfig | None = None
        self._model_request_config: ModelRequestConfig | None = None
        self._config_base_cache: dict[str, Any] | None = None
        self._startup_config_base: dict[str, Any] | None = None
        self._model_config_source: str = "config.yaml"
        self._enterprise_config: Any = None
        self._config_cache: dict[str, Any] = {}
        self._filesystem_rail: SysOperationRail | None = None
        self._progressive_tool_rail: ProgressiveToolRail | None = None
        self._deepresearch_execution_rail: DeepResearchExecutionRail | None = None
        self._disabled_tools_rail: DisabledToolsRail | None = None
        self._skill_rail: SkillUseRail | None = None
        self._enabled_skills: list[str] | None = None
        self._skill_authorization_rail: Any = None
        # 企业预置 Skill 名快照（安装账本 source_type=prebuilt），供动态授权
        # trust 判定使用；由白名单同步/热刷新/_refresh_skill_identity 维护。
        self._prebuilt_skills: set[str] = set()
        self._stream_event_rail: JiuSwarmStreamEventRail | None = None
        self._task_execution_rail: TaskExecutionRail | None = None
        self._skill_turbo_prompt_rail: Any = None
        self._skill_protocol_prompt_rail: Any = None
        self._request_summary_rail: Any | None = None
        # Track session IDs currently executing on this adapter instance.
        # Used by process_interrupt to avoid aborting sessions that are not
        # the target of the interrupt request (cross-session contamination).
        # Counter (not set) so concurrent tasks with the same session_id
        # (e.g., supplement while previous task still winding down) don't
        # prematurely remove the entry when the first task finishes.
        self._active_session_ids: Counter[str] = Counter()
        # Provenance of assistant text currently forwarded on the shared
        # interaction stream. A late user-round chat.final must not be demoted
        # just because a goal round already became active.
        self._stream_content_run_kind: str | None = None
        # In-flight asyncio tasks per session (stream/non-stream agent runs).
        self._session_agent_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._task_planning_rail: TaskPlanningRail | None = None
        self._context_assemble_rail: ContextAssembleRail | None = None
        self._context_assemble_mode: str | None = None
        self._context_processor_rail: ContextProcessorRail | None = None
        self._runtime_prompt_rail: RuntimePromptRail | None = None
        self._response_prompt_rail: ResponsePromptRail | None = None
        self._prompt_attachment_loader: PromptAttachmentLoader | None = None
        self._security_rail: SecurityRail | None = None
        self._memory_rail: MemoryRail | None = None
        self._external_memory_rail: Any = None
        self._external_memory_rail_registered: bool = False
        # 记忆 embedding 配置指纹：用于检测 embed 段变化并据此重建 MemoryRail。
        # 重建 rail 才能让 _embedding_config 刷新；否则换 endpoint 时 rail 复用旧配置。
        self._memory_embedding_fingerprint: str = ""
        # 最近一次请求使用的 mode：reload 时无 runtime_config 上下文，靠它主动刷新 memory rail。
        self._last_mode: str | None = None
        # 延时重索引任务（debounce）：连续改多次 embedding 只在最后一次后跑一次。
        self._memory_reindex_task: asyncio.Task | None = None
        self._llm_retry_rail: LLMRetryRail | None = None
        self._heartbeat_rail: HeartbeatRail | None = None
        self._skill_evolution_rail: SkillEvolutionRail | None = None
        self._evolution_interrupt_rail: EvolutionInterruptRail | None = None
        self._pending_auto_rebuild_skills: list[str] = []
        self._skill_create_rail: SkillCreateRail | None = None
        self._subagent_rail: SubagentRail | None = None
        self._ask_user_rail: StructuredAskUserRail | None = None
        self._permission_rail: Any = None
        self._agent_permissions_body: dict[str, Any] | None = None
        self._skill_active_state_rail: SkillActiveStateRail | None = None
        self._skill_credential_injection_rail: SkillCredentialInjectionRail | None = None
        self._avatar_rail: Any = None
        self._tool_cards = None
        self._evolution_watcher_tasks: set[asyncio.Task] = set()
        self._sys_operation = None
        self._sys_operation_card: SysOperationCard | None = None
        # Ids of the sys operations this adapter currently holds a reference on,
        # in acquisition order. ``cleanup`` releases them so a disposed adapter
        # stops pinning its SysOperation (and the ~16 tools derived from it) in
        # the process-global resource manager.
        self._retained_sys_operation_ids: list[str] = []
        self._vision_model_config: VisionModelConfig | None = None
        self._audio_model_config: AudioModelConfig | None = None
        self._video_model_config: bool = False
        self._image_gen_model_config: bool = False
        self._vision_tools: list[Any] = []
        self._audio_tools: list[Any] = []
        self._instance_overrides: dict[str, Any] = {}
        self._is_session_scoped_adapter: bool = False
        self._parent_session_id: str | None = None
        # Active request-scoped OfficeClaw MCP registration for this session
        # adapter. Used for safe short-name cleanup and diagnostics; invoke
        # allowlisting is enforced via bind_active_office_claw_mcp_tools.
        self._active_office_claw_mcp: OfficeClawMcpRegistration | None = None
        # Tenant tip namespace for Track-B env reads (sync_agents_configs).
        # Defaults match unbound single-tenant; AgentManager / copy_tenant_env_bindings_from
        # overwrite for per-agent scopes.
        self._env_agent_id: str = "default"
        self._env_service_id: str = "default"
        self._workspace_key: str = "default"
        self._user_workspace_dir: Any | None = None
        self._checkpointer: Any | None = None
        # Eager lock: lazy None→Lock races let concurrent set_checkpoint callers
        # each create a distinct lock and double-init the checkpointer.
        self._checkpoint_init_lock = asyncio.Lock()
        # Root-adapter-only: its own DeepAgent is built on demand (see
        # ``ensure_instance``), so the chat path does not pay for an instance it
        # never runs on.
        self._root_instance_requested: bool = False
        self._root_instance_lock: asyncio.Lock | None = None
        self._session_adapters: dict[str, JiuWenSwarmDeepAdapter] = {}
        self._session_adapter_locks: dict[str, asyncio.Lock] = {}
        self._session_adapter_last_used: dict[str, float] = {}
        self._session_adapter_config_version: int = 0
        self._session_adapter_versions: dict[str, int] = {}
        self._session_adapter_reload_failures: dict[str, tuple[int, float]] = {}
        self._pending_session_reload_config_base: dict[str, Any] | None = None
        self._pending_session_reload_env_overrides: dict[str, Any] | None = None
        # Hot-reload deferral: (config_base, env_overrides) while this adapter is busy.
        self._pending_reload: tuple[dict[str, Any] | None, dict[str, Any] | None] | None = None
        self._chat_browser_runtime_pin: Any | None = None
        self._reload_lock = asyncio.Lock()
        self._working_checker: Callable[[], bool] | None = None
        self._session_instance_config: dict[str, Any] | None = None
        self._session_instance_mode: str = "agent"
        self._session_instance_sub_mode: str | None = None
        # Fail closed after a rewrite checkpoint rollback cannot be proven
        # durable.  The parent evicts this child immediately after the request;
        # until then, every direct message entry point refuses the stale state.
        self._deepresearch_rewrite_tx_uncertain: bool = False
        self._xiaoyi_phone_tools_registered: bool = False
        self._paid_search_registered: bool = False
        self._paid_search_tool: Any | None = None
        self._symphony_tools: list[Any] = []
        self._symphony_tools_registered: bool = False
        self._symphony_orchestration_rail = None
        self._skill_retrieval_tools_registered: bool = False
        self._skill_retrieval_tools: list[Any] = []
        self._skill_retrieval_prompt_rail: SkillRetrievalPromptRail | None = None
        self._context_overflow_recovery_rail: ContextOverflowRecoveryRail | None = None
        self._skill_manager: SkillManager | None = None
        self._a2x_client: Any | None = None
        self._a2x_config: dict[str, Any] = {}
        self._a2x_blank_service_id: str = ""
        self._a2x_blank_dataset: str = ""
        self._cron_runtime = CronRuntimeBridge()
        self._runtime_cron_tool_context = _RuntimeCronToolContext(
            tool_scope=f"runtime_{id(self):x}",
        )
        # Per-session-adapter snapshot of the current chat request's route,
        # captured from request.* at request entry. Used by
        # _get_deepresearch_tool_context() so deepresearch_stream progress
        # pushes bind to the originating session/request even when concurrent
        # requests leak module-level _CRON_TOOL_* bindings into background
        # tool tasks. Plain attribute (not ContextVar): per-session-scoped,
        # no propagation/reset concerns. Cron-triggered paths don't set this
        # and fall back to _runtime_cron_tool_context.
        self._current_request_route: dict[str, str] = {}
        # Language the currently registered cron tools were built for, or None
        # when they are not registered yet. Doubles as the rebuild condition.
        self._cron_tools_registered_language: str | None = None
        # Git snapshot per (project dir, session), taken on a conversation's
        # first turn. Held on the adapter rather than module-globally so it is
        # reclaimed with the session instead of accumulating for the life of
        # the process.
        self._session_git_snapshots: dict[str, _GitSnapshot] = {}
        self._is_proactive_memory: bool | None = None
        self._model_cache: dict[str, Model] = {}
        self._model_name_to_keys: dict[str, list[str]] = {}
        # Optional lite/pro mapping from models.defaults[].tier for task_tool.
        self._tier_model_cache: dict[str, Model] = {}
        # Cache system prompt to avoid re-building on every btw/recap call.
        # The system prompt is derived from project context (CLAUDE.md, skills, etc.)
        # which doesn't change within a session, so caching is safe.
        self._last_system_prompt: str = ""
        self._default_model_name: str = ""
        self._last_reload_model_fingerprint: str | None = None
        self._last_reload_system_prompt_fingerprint: str | None = None
        self._last_models_config_fingerprint: str | None = None
        self._registered_mcp_server_ids: set[str] = set()
        self._registered_mcp_servers: dict[str, McpServerConfig] = {}
        self._auto_harness_service: Optional[AutoHarnessService] = None
        self._dreaming_started = False
        self._dreaming_mode: str = "agent"
        self._send_file_toolkit: SendFileToolkit | None = None
        self._runtime_state_write_task: asyncio.Task[None] | None = None

    def _skill_envs_provider(self) -> dict[str, dict[str, str]]:
        if self._skill_credential_injection_rail is None:
            return {}
        return self._skill_credential_injection_rail.get_skill_envs()

    def _schedule_runtime_state_write(
        self,
        *,
        mode: str,
        language: str,
        channel: str,
        session_id: str | None,
        project_dir: str | None,
    ) -> None:
        """Persist diagnostic Git/runtime state without delaying chat handling."""
        # Some lightweight adapters are restored or constructed without the
        # full initializer (including focused rail tests). Treat the missing
        # diagnostic-task slot as idle; runtime-state persistence must never
        # break request configuration.
        current = getattr(self, "_runtime_state_write_task", None)
        if current is not None and not current.done():
            return

        async def _write() -> None:
            def _write_bounded() -> None:
                with self._RUNTIME_STATE_WRITE_LIMIT:
                    self._write_runtime_state(
                        mode=mode,
                        language=language,
                        channel=channel,
                        session_id=session_id,
                        project_dir=project_dir,
                    )

            await asyncio.to_thread(_write_bounded)

        task = asyncio.create_task(
            _write(),
            name=f"runtime-state-{session_id or 'default'}",
        )
        self._runtime_state_write_task = task

        def _clear(done: asyncio.Task[None]) -> None:
            if self._runtime_state_write_task is done:
                self._runtime_state_write_task = None
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] async runtime_state write failed",
                    exc_info=True,
                )

        task.add_done_callback(_clear)

    def set_skill_manager(self, skill_manager: SkillManager) -> None:
        """Inject shared SkillManager from facade for tool reuse."""
        self._skill_manager = skill_manager
        for adapter in getattr(self, "_session_adapters", {}).values():
            adapter.set_skill_manager(skill_manager)

    def _resolve_skill_dirs(self, extra_skill_dir: str | None = None) -> list[str]:
        """解析 SkillUseRail / evolution 使用的 skills 目录列表.

        Align with OfficeClaw tip: when ``JIUWEN*_SHARED_SKILLS_DIRS`` is set
        (e.g. office-claw-skills), those roots are used instead of only the
        empty workspace skills folder.
        """
        if is_skill_whitelist_tenant(self._agent_id, self._service_id):
            skills_dirs = [str(Path(self._workspace_dir) / "skills")]
        else:
            skills_dirs = [str(p) for p in resolve_agent_registered_skill_dirs()]
        if extra_skill_dir:
            skills_dirs.append(extra_skill_dir)
        return skills_dirs

    def _prebuilt_skill_dirs_snapshot(self) -> list[str]:
        """企业预置 Skill 目录快照（workspace/skills/<name>），供 trust 判定。"""
        base = Path(self._workspace_dir) / "skills"
        return [str(base / name) for name in sorted(self._prebuilt_skills) if name]

    def _skill_installation_snapshot(self) -> list[dict[str, Any]]:
        """读取当前 workspace 安装账本；动态授权不再依赖旧 Gateway DB。"""
        manager = self._skill_manager
        if manager is None:
            return []
        return manager.list_skill_installations()

    def _refresh_prebuilt_skill_snapshot(self) -> None:
        """从当前 workspace 安装账本同步预置 Skill 身份快照。"""
        prebuilt: set[str] = set()
        for row in self._skill_installation_snapshot():
            if str(row.get("source_type") or "").strip() != "prebuilt":
                continue
            name = str(row.get("name") or "").strip()
            if name:
                prebuilt.add(name)
        self._prebuilt_skills = prebuilt

    async def _refresh_skill_identity(self, skill_name: str) -> None:
        """按安装账本校准当前企业 Skill 的预置身份，不改变 Skill 加载集合。"""
        if not is_skill_whitelist_tenant(self._agent_id, self._service_id):
            return
        name = str(skill_name or "").strip()
        if not name or Path(name).name != name:
            return

        try:
            installed = next(
                (
                    row
                    for row in self._skill_installation_snapshot()
                    if str(row.get("name") or "").strip() == name
                ),
                None,
            )
        except Exception:  # noqa: BLE001 — 账本查询失败保留实例已有快照
            logger.warning(
                "[JiuWenSwarmDeepAdapter] refresh skill identity failed "
                "service_id=%s agent_id=%s skill=%s",
                self._service_id,
                self._agent_id,
                name,
                exc_info=True,
            )
            return

        if (
            installed is not None
            and str(installed.get("source_type") or "").strip() == "prebuilt"
        ):
            self._prebuilt_skills.add(name)
        else:
            self._prebuilt_skills.discard(name)

    @staticmethod
    def _session_adapter_key(session_id: str | None) -> str:
        sid = str(session_id or "").strip()
        return sid or "default"

    def copy_tenant_env_bindings_from(self, source: "JiuWenSwarmDeepAdapter") -> None:
        """Copy tenant tip namespace bindings from another adapter instance."""
        self._env_agent_id = getattr(source, "_env_agent_id", "default")
        self._env_service_id = getattr(source, "_env_service_id", "default")
        self._workspace_key = getattr(source, "_workspace_key", "default")
        user_ws = getattr(source, "_user_workspace_dir", None)
        if user_ws is not None:
            self._user_workspace_dir = user_ws
        # Share the parent's tenant checkpointer so session adapters do not open
        # a second sqlite handle on the same agent .checkpoint path.
        parent_cp = getattr(source, "_checkpointer", None)
        if parent_cp is not None:
            self._checkpointer = parent_cp

    def _bind_checkpointer_to_rails(self) -> None:
        """Wire adapter-local checkpointer into rails (align with test/jiuwenclaw)."""
        stream_rail = getattr(self, "_stream_event_rail", None)
        checkpointer = getattr(self, "_checkpointer", None)
        if stream_rail is not None and checkpointer is not None:
            stream_rail.set_checkpointer(checkpointer)

    def _get_adapter_checkpointer(self) -> Any:
        """Prefer instance checkpointer; fall back to process default only if unset."""
        if self._checkpointer is not None:
            return self._checkpointer
        return CheckpointerFactory.get_checkpointer()

    def _new_session_scoped_adapter(self, session_id: str) -> "JiuWenSwarmDeepAdapter":
        """Create a child adapter that owns one DeepAgent for a single session."""
        adapter = type(self)()
        # Keep subclass constructors polymorphic while preserving the trusted
        # tenant identity already established on the parent adapter.
        for attribute in ("_workspace_dir", "_agent_id", "_service_id"):
            value = getattr(self, attribute, None)
            if value is not None:
                setattr(adapter, attribute, value)
        adapter.mark_as_session_scoped(session_id)
        # Session adapters must inherit the parent's tenant env namespace. A fresh
        # adapter defaults to default/default and would read placeholder .env
        # model credentials instead of the synced per-agent tip bag.
        adapter.copy_tenant_env_bindings_from(self)
        if self._skill_manager is not None:
            adapter.set_skill_manager(self._skill_manager)
        return adapter

    def mark_as_session_scoped(self, session_id: str) -> None:
        self._is_session_scoped_adapter = True
        self._parent_session_id = session_id

    def _get_cached_session_adapter(self, session_id: str | None) -> "JiuWenSwarmDeepAdapter | None":
        sid = self._session_adapter_key(session_id)
        return self._session_adapters.get(sid)

    def _iter_session_adapters_for_reload(
        self,
        target_session_id: str | None = None,
    ) -> list[tuple[str, "JiuWenSwarmDeepAdapter"]]:
        target_sid = str(target_session_id or "").strip()
        if not target_sid:
            return list(self._session_adapters.items())
        adapter = self._session_adapters.get(self._session_adapter_key(target_sid))
        if adapter is None:
            return []
        return [(self._session_adapter_key(target_sid), adapter)]

    def _touch_session_adapter(self, session_id: str | None) -> None:
        self._session_adapter_last_used[self._session_adapter_key(session_id)] = time.time()

    def _drop_session_adapter_cache_entry(
        self,
        session_id: str,
        *,
        remove_lock: bool = True,
        remove_runtime_state: bool = True,
    ) -> None:
        self._session_adapters.pop(session_id, None)
        if remove_lock:
            self._session_adapter_locks.pop(session_id, None)
        self._session_adapter_last_used.pop(session_id, None)
        self._session_adapter_versions.pop(session_id, None)
        self._session_adapter_reload_failures.pop(session_id, None)
        clear_session_skill_state(session_id)
        if not remove_runtime_state:
            return
        try:
            get_runtime_state_path(session_id).unlink(missing_ok=True)
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] remove runtime_state failed: session_id=%s",
                session_id,
                exc_info=True,
            )

    def _mark_session_adapters_stale_for_reload(
        self,
        config_base: dict[str, Any],
        env_overrides: dict[str, Any] | None,
    ) -> None:
        self._session_adapter_config_version += 1
        self._pending_session_reload_config_base = copy.deepcopy(config_base)
        self._pending_session_reload_env_overrides = (
            copy.deepcopy(env_overrides) if isinstance(env_overrides, dict) else None
        )
        if self._session_adapters:
            logger.info(
                "[JiuWenSwarmDeepAdapter] marked %d session adapters stale for lazy reload "
                "(version=%d)",
                len(self._session_adapters),
                self._session_adapter_config_version,
            )

    async def _reload_session_adapter_if_stale(
        self,
        session_id: str,
        adapter: "JiuWenSwarmDeepAdapter",
    ) -> None:
        current_version = self._session_adapter_config_version
        if self._session_adapter_versions.get(session_id, 0) >= current_version:
            return
        config_base = self._pending_session_reload_config_base
        if not isinstance(config_base, dict):
            self._session_adapter_versions[session_id] = current_version
            self._session_adapter_reload_failures.pop(session_id, None)
            return
        failed = self._session_adapter_reload_failures.get(session_id)
        if failed is not None:
            failed_version, failed_at = failed
            if (
                failed_version == current_version
                and time.monotonic() - failed_at < self.SESSION_ADAPTER_RELOAD_RETRY_INTERVAL_SEC
            ):
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] lazy session adapter reload retry suppressed: "
                    "session_id=%s version=%s",
                    session_id,
                    current_version,
                )
                return
        try:
            await adapter.reload_agent_config(
                config_base,
                self._pending_session_reload_env_overrides,
                target_session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] lazy session adapter reload failed: session_id=%s error=%s",
                session_id,
                exc,
            )
            self._session_adapter_reload_failures[session_id] = (
                current_version,
                time.monotonic(),
            )
            return
        self._session_adapter_versions[session_id] = current_version
        self._session_adapter_reload_failures.pop(session_id, None)

    async def _evict_idle_session_adapters(self) -> None:
        if self._is_session_scoped_adapter:
            return

        now = time.time()
        evicted = 0
        for sid in list(self._session_adapters):
            if evicted >= self.SESSION_ADAPTER_EVICT_BATCH_SIZE:
                break
            adapter = self._session_adapters.get(sid)
            checkpoint_uncertain = bool(
                getattr(adapter, "_deepresearch_rewrite_tx_uncertain", False)
            )
            last_used = self._session_adapter_last_used.get(sid, 0.0)
            if (
                not checkpoint_uncertain
                and now - last_used < self.SESSION_ADAPTER_IDLE_TTL_SEC
            ):
                continue
            lock = self._session_adapter_locks.get(sid)
            if lock is not None and (
                lock.locked() or self._session_adapter_lock_has_waiters(lock)
            ):
                continue
            if await self.cleanup_session_adapter(sid):
                evicted += 1

    async def cleanup_session_adapter(self, session_id: str | None) -> bool:
        """Release an idle session-scoped adapter without deleting session history."""
        sid = self._session_adapter_key(session_id)
        if self._is_session_scoped_adapter:
            if self._session_adapter_key(self._parent_session_id) != sid:
                return False
            if self.is_session_active(sid) or self.is_deep_agent_executing_for_session(sid):
                return False
            await self.cleanup()
            return True

        lock = self._session_adapter_locks.get(sid)
        if lock is not None and (
            lock.locked() or self._session_adapter_lock_has_waiters(lock)
        ):
            async with lock:
                pass
            # A request that was creating/reloading this child may be about to mark it active.
            return False

        lock = self._session_adapter_locks.setdefault(sid, asyncio.Lock())
        cleaned = False
        remove_lock_after_release = False
        async with lock:
            adapter = self._session_adapters.get(sid)
            if adapter is None:
                self._session_adapter_last_used.pop(sid, None)
                self._session_adapter_versions.pop(sid, None)
                self._session_adapter_reload_failures.pop(sid, None)
                remove_lock_after_release = True
            else:
                if adapter.is_session_active(sid) or adapter.is_deep_agent_executing_for_session(sid):
                    return False
                try:
                    await adapter.cleanup()
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] session adapter cleanup failed: session_id=%s error=%s",
                        sid,
                        exc,
                    )
                    return False
                self._drop_session_adapter_cache_entry(sid, remove_lock=False)
                remove_lock_after_release = True
                cleaned = True
        if remove_lock_after_release and self._is_session_lock_idle(sid, lock):
            self._session_adapter_locks.pop(sid, None)
        if cleaned:
            logger.info("[JiuWenSwarmDeepAdapter] session scoped DeepAgent removed: session_id=%s", sid)
        return cleaned

    def _is_session_lock_idle(self, sid: str, lock: asyncio.Lock) -> bool:
        """Check whether the session lock is the current one and has no active holders or waiters."""
        return (
            self._session_adapter_locks.get(sid) is lock
            and not lock.locked()
            and not self._session_adapter_lock_has_waiters(lock)
        )

    @staticmethod
    def _session_adapter_lock_has_waiters(lock: asyncio.Lock) -> bool:
        # asyncio.Lock has no public waiter inspection; keep the lock if a reconnect is queued on it.
        waiters = getattr(lock, "_waiters", None)
        return any(not waiter.cancelled() for waiter in list(waiters or ()))

    async def _get_or_create_session_adapter(
        self,
        session_id: str | None,
        *,
        request: AgentRequest | None = None,
    ) -> "JiuWenSwarmDeepAdapter":
        """Return the session-owned adapter, creating and initializing it once."""
        if self._is_session_scoped_adapter:
            self._touch_session_adapter(session_id)
            return self

        sid = self._session_adapter_key(session_id)
        lock = self._session_adapter_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            existing = self._session_adapters.get(sid)
            if existing is not None:
                await self._reload_session_adapter_if_stale(sid, existing)
                self._touch_session_adapter(sid)
                return existing

            adapter = self._new_session_scoped_adapter(sid)
            config = (
                dict(self._session_instance_config)
                if isinstance(self._session_instance_config, dict)
                else {}
            )
            # 企业版：把当前请求带进 create_instance，才能按 bot_id 加载模型模板。
            if request is not None:
                config["request"] = request
            create_started_at = time.monotonic()
            await adapter.create_instance(
                config if config else None,
                mode=self._session_instance_mode,
                sub_mode=self._session_instance_sub_mode,
                config_base=self._config_base_cache,
            )
            instance_ready_at = time.monotonic()

            await adapter.start_interaction(session_id=sid)
            interaction_ready_at = time.monotonic()

            self._session_adapters[sid] = adapter
            # A brand-new session adapter is created from ``_session_instance_config``
            # (which may predate the latest global reload). If a global reload left a
            # pending ``config_base``, apply it now so the new session reflects the
            # same configuration as already-existing sessions that reload lazily.
            # ``_reload_session_adapter_if_stale`` owns the version bookkeeping
            # (including the no-pending case, where it silently catches up).
            await self._reload_session_adapter_if_stale(sid, adapter)
            self._touch_session_adapter(sid)
            # Cold-start cost of a session's first turn, split so a slow one can
            # be attributed to agent assembly vs. interaction startup.
            server_logger.info(
                "[AgentServer] session adapter created: session_id=%s create_instance_ms=%.1f"
                " start_interaction_ms=%.1f total_ms=%.1f",
                sid,
                (instance_ready_at - create_started_at) * 1000,
                (interaction_ready_at - instance_ready_at) * 1000,
                (time.monotonic() - create_started_at) * 1000,
            )
            return adapter

    @staticmethod
    def _get_a2x_config(config_base: dict[str, Any]) -> dict[str, Any]:
        """Resolve A2X config from ``react.a2x_registry`` with safe defaults."""
        return resolve_a2x_config(config_base)

    def _sync_a2x_runtime_state(self) -> None:
        """Expose A2X runtime state on the underlying DeepAgent instance."""
        if self._instance is None:
            return
        setattr(self._instance, "_jiuwen_a2x_client", self._a2x_client)
        setattr(self._instance, "_jiuwen_a2x_config", self._a2x_config)
        setattr(self._instance, "_jiuwen_a2x_blank_service_id", self._a2x_blank_service_id)
        setattr(self._instance, "_jiuwen_a2x_blank_dataset", self._a2x_blank_dataset)

    # -- _active_session_ids helpers (Counter-based, not set) --
    # Counter allows the same session_id to be registered by concurrent tasks
    # (e.g., supplement while previous task winds down). A set would collapse
    # duplicate adds and the first task's discard would evict the second.

    def _mark_session_active(self, session_id: str) -> None:
        """Increment the active-task count for *session_id*."""
        sid = self._resolve_interrupt_session_id(session_id)
        self._active_session_ids[sid] += 1

    def _unmark_session_active(self, session_id: str, *, cleanup_rail: bool = True) -> None:
        """Decrement the active-task count for *session_id*; remove when zero.

        When the count drops to zero, optionally cleans up per-session rail state.
        Skip ``cleanup_rail`` when the stream consumer was cancelled but AgentServer
        work may still be winding down — ``process_interrupt`` owns teardown then.
        """
        sid = self._resolve_interrupt_session_id(session_id)
        count = self._active_session_ids.get(sid, 0)
        if count <= 1:
            self._active_session_ids.pop(sid, None)
            if cleanup_rail and self._stream_event_rail is not None:
                try:
                    self._stream_event_rail.cleanup_session(sid)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup_session(%s) failed: %s",
                        sid, exc,
                    )
        else:
            self._active_session_ids[sid] = count - 1

    def _is_session_active(self, session_id: str) -> bool:
        """Return True if at least one task is running for *session_id*."""
        sid = self._resolve_interrupt_session_id(session_id)
        if self._active_session_ids.get(sid, 0) > 0:
            return True
        return self._session_has_registered_tasks(sid)

    def is_session_active(self, session_id: str) -> bool:
        return self._is_session_active(session_id)

    def set_working_checker(self, checker: Callable[[], bool] | None) -> None:
        """Inject callable returning whether this facade/session has in-flight work."""
        self._working_checker = checker

    def _adapter_is_working(self) -> bool:
        if self._working_checker is not None:
            try:
                return bool(self._working_checker())
            except Exception:
                return False
        if self._is_session_scoped_adapter:
            sid = self._parent_session_id
            return bool(sid and self._is_session_active(sid))
        return bool(self._active_session_ids) or any(
            self._session_has_registered_tasks(sid)
            for sid in list(self._session_adapters)
        )

    def _bind_request_env_overlay(
        self,
        env_overrides: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, Any]:
        """Seal Track-B reads for the current request (formula B tip ± extras)."""
        from jiuwenswarm.server.runtime.tenant_context import bind_workspace_key

        service_id = getattr(self, "_env_service_id", "default")
        agent_id = getattr(self, "_env_agent_id", "default")
        workspace_key = getattr(self, "_workspace_key", "default")
        ns_token = bind_agent_env_ns(service_id, agent_id)
        wk_token = bind_workspace_key(workspace_key)
        overlay = build_effective_env_overlay(
            env_overrides,
            service_id=service_id,
            agent_id=agent_id,
        )
        overlay_token = bind_task_env_overlay(overlay)
        # Pin browser runtime generation after tip/ns bind so this request keeps
        # the credential generation captured at start across mid-request reloads.
        try:
            from jiuwenswarm.agents.harness.common.tools.browser_tools import (
                pin_browser_runtime_generation,
            )

            self._chat_browser_runtime_pin = pin_browser_runtime_generation(
                service_id=service_id,
                agent_id=agent_id,
            )
        except Exception:
            self._chat_browser_runtime_pin = None
            logger.exception(
                "[JiuWenSwarmDeepAdapter] pin browser runtime generation failed"
            )
        return ns_token, overlay_token, wk_token

    def _reset_request_env_bindings(
        self,
        ns_token: Any,
        overlay_token: Any,
        wk_token: Any = None,
    ) -> None:
        pin = getattr(self, "_chat_browser_runtime_pin", None)
        if pin is not None:
            try:
                from jiuwenswarm.agents.harness.common.tools.browser_tools import (
                    reset_browser_runtime_generation,
                )

                reset_browser_runtime_generation(pin)
            except Exception:
                logger.exception(
                    "[JiuWenSwarmDeepAdapter] reset browser runtime generation failed"
                )
            self._chat_browser_runtime_pin = None
        from jiuwenswarm.server.runtime.tenant_context import reset_workspace_key

        if overlay_token is not None:
            reset_task_env_overlay(overlay_token)
        if ns_token is not None:
            reset_agent_env_ns(ns_token)
        if wk_token is not None:
            reset_workspace_key(wk_token)

    async def _maybe_apply_pending_reload(self) -> ReloadResult | None:
        if self._pending_reload is None or self._adapter_is_working():
            return None
        async with self._reload_lock:
            pending = self._pending_reload
            if pending is None or self._adapter_is_working():
                return None
            config_base, env_overrides = pending
            try:
                result = await self.reload_agent_config(
                    config_base,
                    env_overrides,
                    _force_apply=True,
                )
            except Exception:
                if self._pending_reload is None:
                    self._pending_reload = pending
                raise
            if result.applied:
                self._pending_reload = None
                promote_staged_env()
            return result

    def queue_pending_reload(
        self,
        config_base: dict[str, Any] | None,
        env_overrides: dict[str, Any] | None,
    ) -> None:
        """Defer a config reload until this adapter is idle."""
        self._pending_reload = (config_base, env_overrides)

    @staticmethod
    def supports_pending_reload() -> bool:
        """Whether this adapter can defer reload while busy."""
        return True

    async def apply_pending_reload_if_idle(self) -> ReloadResult | None:
        """Apply deferred reload when harness has verified no inflight work.

        Unlike ``_maybe_apply_pending_reload``, does not consult ``working_checker``
        (caller already verified idle). Also drains pending on session children.
        """
        applied_any = False
        deferred_any = False
        if self._pending_reload is not None:
            async with self._reload_lock:
                pending = self._pending_reload
                if pending is not None:
                    config_base, env_overrides = pending
                    try:
                        result = await self.reload_agent_config(
                            config_base,
                            env_overrides,
                            _force_apply=True,
                        )
                    except Exception:
                        if self._pending_reload is None:
                            self._pending_reload = pending
                        raise
                    if result.applied:
                        self._pending_reload = None
                        applied_any = True
                    elif result.deferred:
                        deferred_any = True

        if not self._is_session_scoped_adapter:
            for session_id, adapter in list(self._session_adapters.items()):
                apply_fn = getattr(adapter, "apply_pending_reload_if_idle", None)
                if not callable(apply_fn):
                    continue
                try:
                    child_result = await apply_fn()
                except Exception:
                    logger.exception(
                        "[JiuWenSwarmDeepAdapter] child pending reload failed: session=%s",
                        session_id,
                    )
                    continue
                if isinstance(child_result, ReloadResult):
                    if child_result.applied:
                        applied_any = True
                    elif child_result.deferred:
                        deferred_any = True

        if applied_any:
            promote_staged_env()
            return ReloadResult(applied=True)
        if deferred_any:
            return ReloadResult(deferred=True)
        return None

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        """Return whether this adapter still owns session runtime."""
        if session_id is not None:
            sid = self._session_adapter_key(session_id)
            if self._is_session_scoped_adapter:
                return self._session_adapter_key(self._parent_session_id) == sid
            return bool(
                sid in self._session_adapters
                or sid in self._session_adapter_locks
                or self._active_session_ids.get(sid, 0) > 0
                or sid in self._session_agent_tasks
            )
        if self._is_session_scoped_adapter:
            return True
        return bool(
            self._session_adapters
            or self._session_adapter_locks
            or self._active_session_ids
            or self._session_agent_tasks
        )

    def _session_has_registered_tasks(self, session_id: str) -> bool:
        tasks = getattr(self, "_session_agent_tasks", {}).get(session_id)
        return bool(tasks and any(not task.done() for task in tasks))

    def _deep_agent_loop_session_id(self) -> str | None:
        instance = getattr(self, "_instance", None)
        if instance is None:
            return None
        loop_session = getattr(instance, "_loop_session", None)
        if loop_session is None:
            return None
        loop_sid = ""
        get_session_id = getattr(loop_session, "get_session_id", None)
        if callable(get_session_id):
            try:
                loop_sid = str(get_session_id() or "")
            except Exception:
                loop_sid = ""
        if not loop_sid:
            loop_sid = str(getattr(loop_session, "session_id", "") or "")
        # Return None when the loop session_id is unknown — callers use None
        # as a conservative signal ("assume DeepAgent is executing").
        # Normalizing "" → "default" would defeat that check and could cause
        # _other_active_sessions to undercount, triggering a premature global abort.
        if not loop_sid or loop_sid == "default":
            return None
        return self._resolve_interrupt_session_id(loop_sid)

    async def _clear_pending_ask_user_interrupt_for_supplement(
        self,
        session_id: str | None,
    ) -> bool:
        """Drop a superseded pure ask_user round without leaving an open tool call."""
        instance = getattr(self, "_instance", None)
        loop_session = getattr(instance, "_loop_session", None)
        loop_sid = self._deep_agent_loop_session_id()
        target_sid = self._resolve_interrupt_session_id(session_id)
        if loop_session is None or loop_sid != target_sid:
            return False

        try:
            state = loop_session.get_state(INTERRUPTION_KEY)
            interrupted_tools = getattr(state, "interrupted_tools", None)
            if not isinstance(interrupted_tools, dict) or not interrupted_tools:
                return False
            if any(
                getattr(getattr(entry, "tool_call", None), "name", None) != "ask_user"
                for entry in interrupted_tools.values()
            ):
                return False

            ai_message = getattr(state, "ai_message", None)
            pending_calls = list(getattr(ai_message, "tool_calls", None) or [])
            if not pending_calls or any(
                getattr(tool_call, "name", None) != "ask_user"
                for tool_call in pending_calls
            ):
                return False

            react_agent = getattr(instance, "react_agent", None)
            context_engine = getattr(react_agent, "context_engine", None)
            context = (
                context_engine.get_context(session_id=target_sid)
                if context_engine is not None
                else None
            )
            messages = list(context.get_messages() or []) if context is not None else []
            last_calls = list(getattr(messages[-1], "tool_calls", None) or []) if messages else []
            pending_signature = [
                (getattr(tool_call, "id", None), getattr(tool_call, "name", None))
                for tool_call in pending_calls
            ]
            last_signature = [
                (getattr(tool_call, "id", None), getattr(tool_call, "name", None))
                for tool_call in last_calls
            ]
            if last_signature != pending_signature:
                return False

            context.pop_messages(1, with_history=True)
            loop_session.update_state({INTERRUPTION_KEY: None})
            try:
                await _skill_turbo_clear_resume_ctx(loop_session)
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] clear skill_turbo resume ctx failed",
                    exc_info=True,
                )
            await context_engine.save_contexts(loop_session)
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] interrupt(supplement): failed to inspect "
                "pending ask_user state session=%s",
                target_sid,
                exc_info=True,
            )
            return False

        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt(supplement): cleared pending "
            "ask_user state session=%s",
            target_sid,
        )
        return True

    def _is_deep_agent_executing_for_session(self, session_id: str) -> bool:
        """True when the shared DeepAgent still runs stream/task-loop work for *session_id*."""
        instance = getattr(self, "_instance", None)
        if instance is None or not getattr(instance, "_invoke_active", False):
            return False
        stream_task = getattr(instance, "_stream_process_task", None)
        if stream_task is not None and not stream_task.done():
            loop_sid = self._deep_agent_loop_session_id()
            if loop_sid is None:
                return True
            return self._is_related_session(session_id, loop_sid)
        return False

    def is_deep_agent_executing_for_session(self, session_id: str) -> bool:
        return self._is_deep_agent_executing_for_session(session_id)

    def _is_session_live(self, session_id: str) -> bool:
        sid = self._resolve_interrupt_session_id(session_id)
        return (
            self._is_session_active(sid)
            or self._is_deep_agent_executing_for_session(sid)
        )

    @staticmethod
    def _is_related_session(target_sid: str, other_sid: str) -> bool:
        """Return True when *other_sid* belongs to the same session tree as *target_sid*.

        Covers direct ancestor/descendant relationships only
        (e.g. ``A`` ↔ ``A_B``, ``A`` ↔ ``A_B_C``).  Siblings such as
        ``tui_a`` and ``tui_b`` are treated as unrelated — they are
        independent root sessions that share a channel-prefix convention,
        not sub-sessions of a common parent.
        """
        if not target_sid or not other_sid:
            return target_sid == other_sid
        if other_sid == target_sid:
            return True
        return other_sid.startswith(f"{target_sid}_") or target_sid.startswith(f"{other_sid}_")

    def _other_active_sessions(self, session_id: str) -> int:
        """Return live tasks for sessions unrelated to *session_id*."""
        normalized = self._resolve_interrupt_session_id(session_id)
        candidate_sids = set(self._active_session_ids.keys())
        candidate_sids.update(getattr(self, "_session_agent_tasks", {}).keys())
        loop_sid = self._deep_agent_loop_session_id()
        if loop_sid is not None:
            candidate_sids.add(loop_sid)
        total = 0
        for sid in candidate_sids:
            if self._is_related_session(normalized, sid):
                continue
            if self._is_session_live(sid):
                total += max(self._active_session_ids.get(sid, 0), 1)
        return total

    async def _halt_deep_agent_execution(self, reason: str) -> None:
        """Cooperatively abort DeepAgent and cancel in-flight scheduler tasks."""
        if self._instance is None:
            return
        # Cancel scheduler tasks FIRST so in-flight LLM HTTP requests raise
        # CancelledError promptly.  This allows the _stream_process background
        # task (which instance.abort() waits on via _cancel_stream_process_task)
        # to unwind quickly instead of blocking until the LLM call times out.
        #
        # Placed before instance.abort() to break the circular wait:
        #   instance.abort() → await _stream_process_task
        #   → _stream_process_task stuck in LLM request
        #   → LLM request cancelled by _cancel_scheduler_running_tasks()
        #   → but _cancel_scheduler_running_tasks() was AFTER abort() → deadlock.
        self._cancel_scheduler_running_tasks()
        try:
            try:
                # asyncio.shield protects abort() from re-injected CancelledError
                # when called from a CancelledError handler — a second task.cancel()
                # would otherwise interrupt abort() mid-execution, leaving the
                # DeepAgent in a partially-aborted state.  shield() ensures abort()
                # runs to completion even if the outer task is re-cancelled.
                await asyncio.shield(self._instance.abort())
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt(%s): 已终止 DeepAgent 任务循环",
                    reason,
                )
            except asyncio.CancelledError:
                # shield() absorbed the re-cancellation; abort() completed.
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt(%s): instance.abort 在 shield 下完成"
                    "（外层 task 被二次 cancel）",
                    reason,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] interrupt(%s): instance.abort 失败: %s",
                    reason,
                    exc,
                )
        finally:
            # Safety net: cancel again in case new scheduler tasks were spawned
            # between the first cancel and abort().
            self._cancel_scheduler_running_tasks()

    def _register_session_agent_task(self, session_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        sid = self._resolve_interrupt_session_id(session_id)
        self._session_agent_tasks.setdefault(sid, set()).add(task)

    def _unregister_session_agent_task(self, session_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        sid = self._resolve_interrupt_session_id(session_id)
        bucket = self._session_agent_tasks.get(sid)
        if bucket is None:
            return
        bucket.discard(task)
        if not bucket:
            self._session_agent_tasks.pop(sid, None)

    async def _cancel_session_agent_tasks(self, session_id: str) -> int:
        sid = self._resolve_interrupt_session_id(session_id)
        tasks_dict = getattr(self, "_session_agent_tasks", None)
        if not tasks_dict:
            return 0
        tasks = list(tasks_dict.pop(sid, set()))
        cancelled = 0
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
        if cancelled:
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt: cancelled %d agent asyncio task(s) session=%s",
                cancelled,
                sid,
            )
            # 等待被取消的任务完成清理，避免僵尸调用：
            # task.cancel() 只调度 CancelledError，不保证任务已停止。
            # 如果不等待，后续 interrupt 处理（rail.abort, instance.abort）
            # 可能与任务清理并发执行，且调用方可能在任务仍在运行时返回"成功"。
            await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)
        return cancelled

    def _clear_a2x_runtime_state(self) -> None:
        """Remove exposed A2X runtime state from the underlying DeepAgent instance."""
        if self._instance is None:
            return
        for attr, value in (
            ("_jiuwen_a2x_client", None),
            ("_jiuwen_a2x_config", {}),
            ("_jiuwen_a2x_blank_service_id", ""),
            ("_jiuwen_a2x_blank_dataset", ""),
        ):
            if hasattr(self._instance, attr):
                try:
                    setattr(self._instance, attr, value)
                except Exception:
                    pass

    async def _close_a2x_client(self) -> None:
        """Close the mounted A2X client if initialized."""
        if self._a2x_client is None:
            self._a2x_config = {}
            self._a2x_blank_service_id = ""
            self._a2x_blank_dataset = ""
            self._clear_a2x_runtime_state()
            return
        client = self._a2x_client
        config = self._a2x_config
        self._a2x_client = None
        self._a2x_config = {}
        self._a2x_blank_service_id = ""
        self._a2x_blank_dataset = ""
        self._clear_a2x_runtime_state()
        close_timeout_raw = config.get("close_timeout", 5.0)
        try:
            close_timeout = max(float(close_timeout_raw), 0.1)
        except (TypeError, ValueError):
            close_timeout = 5.0
        try:
            await asyncio.wait_for(client.aclose(), timeout=close_timeout)
            logger.info("[JiuWenSwarmDeepAdapter] A2X Client closed")
        except TimeoutError:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] A2X Client close timed out after %.1fs",
                close_timeout,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] A2X Client close failed: %s",
                exc,
                exc_info=True,
            )

    async def _init_a2x_client(self, config_base: dict[str, Any]) -> None:
        """Initialize and mount AsyncA2XRegistryClient on the adapter instance."""
        if self._a2x_client is not None:
            await self._close_a2x_client()

        client, a2x_config = await init_a2x_client(config_base)
        self._a2x_config = a2x_config
        self._a2x_client = client
        self._a2x_blank_service_id = ""
        self._a2x_blank_dataset = ""

    async def _try_init_a2x_client(self, config_base: dict[str, Any], *, reload: bool = False) -> None:
        """Best-effort A2X client init that never blocks agent startup."""
        try:
            resolved_config = self._get_a2x_config(config_base)
            if reload and self._a2x_client is not None and resolved_config == self._a2x_config:
                try:
                    await register_blank_agent_if_teammate(
                        self._a2x_client,
                        self._a2x_config,
                        source="deep-agent-reload",
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] A2X blank registration on reload failed; "
                        "reusing existing client: %s",
                        exc,
                    )
                registration = getattr(self._a2x_client, "_jiuwen_blank_agent_registration", {})
                if isinstance(registration, dict):
                    self._a2x_blank_service_id = str(registration.get("service_id") or "").strip()
                    self._a2x_blank_dataset = str(registration.get("dataset") or "").strip()
                self._sync_a2x_runtime_state()
                logger.info(
                    "[JiuWenSwarmDeepAdapter] A2X Client reused on reload: role=%s base_url=%s",
                    self._a2x_config.get("role", "teammate"),
                    self._a2x_config.get("base_url", ""),
                )
                return
            await self._init_a2x_client(config_base)
            await register_blank_agent_if_teammate(
                self._a2x_client,
                self._a2x_config,
                source="deep-agent-reload" if reload else "deep-agent-init",
            )
            registration = getattr(self._a2x_client, "_jiuwen_blank_agent_registration", {})
            if isinstance(registration, dict):
                self._a2x_blank_service_id = str(registration.get("service_id") or "").strip()
                self._a2x_blank_dataset = str(registration.get("dataset") or "").strip()
            self._sync_a2x_runtime_state()
            logger.info(
                "[JiuWenSwarmDeepAdapter] A2X Client %s: role=%s base_url=%s",
                "reinitialized on reload" if reload else "initialized successfully",
                self._a2x_config.get("role", "teammate"),
                self._a2x_config.get("base_url", ""),
            )
        except Exception as exc:
            self._a2x_client = None
            self._a2x_config = {}
            self._a2x_blank_service_id = ""
            self._a2x_blank_dataset = ""
            self._clear_a2x_runtime_state()
            logger.warning(
                "[JiuWenSwarmDeepAdapter] A2X Client %s failed, agent will continue to %s: %s",
                "reload initialization" if reload else "initialize",
                "run" if reload else "start",
                exc,
                exc_info=True,
            )

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

    def _task_execution_rail_enabled(self) -> bool:
        """Whether TaskExecutionRail (task.start/complete/update events) is enabled.

        Defaults to False — the jiuwenswarm frontend only consumes todo.updated.
        Enable for relayclaw frontend which consumes task.start/complete/update
        lifecycle events. Set in config.yaml:

            react:
              enable_task_execution_rail: true
        """
        return bool(self._config_cache.get("enable_task_execution_rail", False))

    def _skill_include_tools_for_profile(self) -> bool:
        if self._is_acp_tool_profile(self._instance_overrides):
            return False
        return self._filesystem_rail is None

    @staticmethod
    def _resolve_prompt_channel(session_id: str | None = None) -> str:
        """Resolve prompt channel from session id."""
        if not session_id:
            return "web"

        channel = session_id.split("_", 1)[0]
        if channel == "sess":
            return "web"
        if channel in {"acp", "cron", "heartbeat", "feishu", "web", "dingtalk", "wecom", "tui"}:
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
        if self._model_request_config and hasattr(self._model_request_config, "model_name"):
            return self._model_request_config.model_name or "unknown"
        return "unknown"

    def _resolve_session_git_snapshot(
        self,
        project_dir: str,
        session_id: str | None,
        head_file: str,
        run_git: Callable[[list[str]], str],
    ) -> _GitSnapshot:
        """Return this conversation's git snapshot, re-taking it if HEAD moved.

        The prompt these values feed states plainly that it is "the git status
        at the start of the conversation" which "will not update during the
        conversation" — but they used to be re-read on every turn, so editing a
        single file rewrote the system prompt mid-conversation. That breaks the
        prompt's own contract and, because the text sits in the cached prefix,
        invalidates the model's KV cache for every later turn.

        Working-tree edits therefore do not refresh it: the agent either made
        them itself (and has them in its history) or can run git on demand.
        Moving the checkout is different — a branch switch changes which
        branch a commit would land on, and invalidates status and recent
        commits wholesale — so HEAD is checked each turn and a move re-takes
        the snapshot. That check is a file read, not a subprocess, which is why
        it can run on the hot path at all.

        Args:
            project_dir: Directory the git commands run in.
            session_id: Conversation the snapshot belongs to; a new session
                takes a fresh one.
            head_file: Absolute path to the git directory's HEAD file.
            run_git: Callable running git in ``project_dir`` and returning
                stripped stdout, or an empty string on failure.

        Returns:
            The snapshot for this conversation and checkout.
        """
        cache_key = f"{project_dir}\0{session_id or ''}"
        head = _read_git_head(head_file)
        cached = self._session_git_snapshots.get(cache_key)
        if cached is not None and cached.head == head:
            return cached

        status_lines = run_git(["status", "--short"]).splitlines()
        snapshot = _GitSnapshot(
            head=head,
            branch=run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD",
            status="\n".join(status_lines[:50]),
            recent_commits=run_git(["log", "--oneline", "-5"]),
        )
        self._session_git_snapshots[cache_key] = snapshot
        return snapshot

    def _write_runtime_state(
        self,
        mode: str,
        language: str,
        channel: str,
        *,
        session_id: str | None = None,
        project_dir: str | None = None,
    ) -> None:
        """将当前运行时状态写入 config 目录下按 session 隔离的 runtime_state 文件。"""
        try:
            git_branch = "N/A"
            git_main_branch = ""
            git_status = ""
            git_recent_commits = ""
            git_user = ""

            git_bin = which("git")
            if git_bin and project_dir and os.path.isdir(project_dir):

                def _run_git(args: list[str]) -> str:
                    result = subprocess.run(
                        [git_bin, *args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=project_dir,
                    )
                    return result.stdout.strip() if result.returncode == 0 else ""

                try:
                    stable_facts = _resolve_stable_git_facts(git_bin, project_dir, _run_git)
                    if stable_facts.is_repo:
                        git_user = stable_facts.user_name
                        git_main_branch = stable_facts.main_branch
                        snapshot = self._resolve_session_git_snapshot(
                            project_dir,
                            session_id,
                            stable_facts.head_file,
                            _run_git,
                        )
                        git_branch = snapshot.branch
                        git_status = snapshot.status
                        git_recent_commits = snapshot.recent_commits
                except Exception:
                    pass

            mode_display = _MODE_DISPLAY_MAP.get(mode, {}).get(language, mode)

            state = {
                "model": self._resolve_model_name(),
                "available_models": get_model_names(),
                "mode": mode_display,
                "language": language,
                "channel": channel,
                "agent": self._agent_name,
                "platform": f"{platform.system()} {platform.machine()}",
                "python": platform.python_version(),
                "git_branch": git_branch,
                "git_main_branch": git_main_branch,
                "git_status": git_status,
                "git_recent_commits": git_recent_commits,
                "git_user": git_user,
            }
            path = get_runtime_state_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            logger.debug("[JiuWenSwarmDeepAdapter] write runtime_state failed: %s", exc)

    @staticmethod
    def _browser_runtime_enabled() -> bool:
        """Whether browser runtime support is enabled for DeepAgent subagent wiring."""
        value = (
            str(
                read_env("PLAYWRIGHT_RUNTIME_MCP_ENABLED")
                or read_env("BROWSER_RUNTIME_MCP_ENABLED")
                or ""
            )
            .strip()
            .lower()
        )
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _resolve_managed_browser_binary_from_config(
        config_base: dict[str, Any] | None = None,
    ) -> str:
        """Resolve managed-browser binary from saved browser config."""
        if config_base is None:
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
    def _resolve_headless_from_config(
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        """Read browser.headless from config (default True = headless)."""
        try:
            if config_base is None:
                config_base = get_config()
            if not isinstance(config_base, dict):
                return True
            browser_cfg = config_base.get("browser", {})
            if not isinstance(browser_cfg, dict):
                return True
            headless = browser_cfg.get("headless", True)
            return bool(headless) if isinstance(headless, bool) else True
        except Exception:
            return True

    def _sync_browser_runtime_environment(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """Synchronize browser launch settings before browser runtimes are built."""
        headless = self._resolve_headless_from_config(config_base)
        mcp_args_raw = (
            read_env("PLAYWRIGHT_MCP_ARGS") or "-y @playwright/mcp@latest"
        ).strip()
        mcp_args = (
            mcp_args_raw.split()
            if mcp_args_raw
            else ["-y", "@playwright/mcp@latest"]
        )
        mcp_args = [arg for arg in mcp_args if arg != "--headless"]
        if headless:
            mcp_args.append("--headless")
            os.environ["BROWSER_MANAGED_ARGS"] = "--headless=new"
        else:
            os.environ.pop("BROWSER_MANAGED_ARGS", None)
        os.environ["PLAYWRIGHT_MCP_ARGS"] = " ".join(mcp_args)
        chrome_path = self._resolve_managed_browser_binary_from_config(config_base)
        if chrome_path:
            os.environ["BROWSER_MANAGED_BINARY"] = chrome_path
        else:
            os.environ.pop("BROWSER_MANAGED_BINARY", None)

        logger.info(
            "[%s] browser runtime config: headless=%s, chrome_path=%s",
            type(self).__name__,
            headless,
            chrome_path or "<auto>",
        )

    @staticmethod
    def _is_subagent_enabled(subagent_cfg: Any) -> bool:
        """Treat only explicit `enabled: true` as enabled."""
        return isinstance(subagent_cfg, dict) and bool(subagent_cfg.get("enabled", False))

    @staticmethod
    def _is_subagent_default_enabled(subagent_cfg: Any) -> bool:
        """Default-enabled subagent: enabled unless explicitly set to false."""
        if not isinstance(subagent_cfg, dict):
            return True  # no config → default enabled
        return subagent_cfg.get("enabled", True) is not False

    def _build_configured_subagents(
        self,
        model: Model,
        config: dict[str, Any],
        config_base: dict[str, Any] | None = None,
    ) -> tuple[list[Any] | None, bool]:
        """Build configured research + browser subagents (agent 模式)."""
        react_cfg = config if isinstance(config, dict) else {}
        subagents_cfg = react_cfg.get("subagents")

        resolved_language = self._resolve_runtime_language()
        workspace = self._workspace_dir or "./"
        subagents: list[Any] = []
        should_add_general_purpose = False

        # Build progressive tool rail for subagents if enabled.
        subagent_rails: list[Any] | None = None
        if is_subagent_tool_lazy_load_enabled(react_cfg):
            progressive_rail = build_progressive_tool_rail_from_config(
                react_cfg,
                language=resolved_language,
                profile="subagent",
                agent_id=self._tool_owner_id(),
                agent_card_id=self._tool_owner_id(),
            )
            if progressive_rail is not None:
                subagent_rails = [progressive_rail]

        if isinstance(subagents_cfg, dict):
            general_agent_cfg = subagents_cfg.get("general_agent")
            if self._is_subagent_enabled(general_agent_cfg):
                should_add_general_purpose = True

            research_agent_cfg = subagents_cfg.get("research_agent")
            if self._is_subagent_enabled(research_agent_cfg):
                from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
                    get_agent_cache_registry,
                )

                subagents.append(
                    build_research_agent_config(
                        model,
                        workspace=workspace,
                        language=resolved_language,
                        max_iterations=parse_int(
                            research_agent_cfg.get("max_iterations"),
                            react_cfg.get("max_iterations", 15),
                        ),
                        rails=subagent_rails,
                        tools=build_jiuwen_harness_named_web_tools(
                            agent_id="research_agent",
                            language=resolved_language,
                            cache=get_agent_cache_registry().get_cache_sync(
                                "research_agent",
                            ),
                        ),
                    )
                )

        browser_agent_cfg = (
            subagents_cfg.get("browser_agent") if isinstance(subagents_cfg, dict) else {}
        )

        # Swarm members and the main browser subagent read these variables when
        # their browser runtimes are built.
        self._sync_browser_runtime_environment(config_base)

        browser_enabled = self._browser_runtime_enabled()
        if browser_enabled:
            if not str(read_env("BROWSER_DRIVER") or "").strip():
                os.environ["BROWSER_DRIVER"] = "managed"
                logger.info(
                    "[JiuWenSwarmDeepAdapter] browser subagent enabled without BROWSER_DRIVER; "
                    "defaulting to managed mode"
                )
            subagents.append(
                build_browser_agent_config(
                    model,
                    workspace=workspace,
                    language=resolved_language,
                    max_iterations=parse_int(
                        (
                            browser_agent_cfg.get("max_iterations")
                            if isinstance(browser_agent_cfg, dict)
                            else None
                        ),
                        react_cfg.get("max_iterations", 15),
                    ),
                    rails=subagent_rails,
                )
            )
        elif (
            isinstance(subagents_cfg, dict)
            and isinstance(browser_agent_cfg, dict)
            and browser_agent_cfg
        ):
            logger.info(
                "[JiuWenSwarmDeepAdapter] browser_agent config detected but browser runtime is not enabled; "
                "skipping browser subagent registration"
            )

        # ── 加载自定义 agent（.jiuwenswarm/agents/*.md）──
        try:
            subagents.extend(
                _load_custom_subagents(
                    self._workspace_dir, subagents_cfg, model, workspace,
                    __name__, model_cache=self._model_cache,
                )
            )
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] failed to load custom agents",
                exc_info=True,
            )

        return subagents or None, should_add_general_purpose

    @staticmethod
    def _build_mcp_server_config(entry: dict[str, Any]) -> McpServerConfig | None:
        # 传入 server_id_scope 生成稳定的 server_id，避免 reload 时重复注册导致进程泄漏
        return build_mcp_server_config(entry, server_id_scope="jiuwenswarm")

    @staticmethod
    def _extract_enabled_mcp_server_entries(config_base: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_enabled_mcp_server_entries(config_base)

    async def _register_mcp_server(self, cfg: McpServerConfig, *, tag: str) -> bool:
        if self._instance is None:
            return False
        # Pre-flight reachability check for HTTP-based MCP servers. If the host
        # is down we skip registration here instead of entering the mcp
        # streamable-http context — otherwise openjiuwen leaks orphaned anyio
        # background tasks on the failed initialize() and logs noisy
        # ``aclose()``/cancel-scope RuntimeErrors.
        reachable, reason = await preflight_mcp_server_reachable(cfg)
        if not reachable:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] MCP server unreachable, skipping registration: "
                "name=%s transport=%s path=%s reason=%s",
                cfg.server_name, cfg.client_type, cfg.server_path, reason,
            )
            return False
        # Remote MCP connect failures may surface as CancelledError: anyio
        # TaskGroup cancel()+uncancel() the host task, and CancelledError is not
        # an Exception subclass (Python 3.8+), so the bare ``except Exception``
        # below cannot isolate it. ResourceMgr isolation that keys off
        # connect_task.cancelled() also fails after uncancel(). Catch
        # CancelledError here and use cancelling() to tell apart:
        #   - cancelling()==0 → anyio-internal cancel → skip this server
        #   - cancelling()>0  → real outer cancel (interrupt / WS drop) → re-raise
        try:
            result = await Runner.resource_mgr.add_mcp_server(cfg, tag=tag)
            ok = True
            if result is not None:
                is_ok = getattr(result, "is_ok", None)
                if callable(is_ok):
                    ok = bool(is_ok())
                elif isinstance(result, bool):
                    ok = result
            if ok:
                server_id = str(getattr(cfg, "server_id", "") or "").strip()
                if not server_id:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] MCP server_id missing after registration: %s", cfg
                    )
                    return False
                self._instance.ability_manager.add(cfg)
                self._registered_mcp_server_ids.add(server_id)
                self._registered_mcp_servers[server_id] = cfg
                return True
            logger.warning(
                "[JiuWenSwarmDeepAdapter] MCP server register failed: %s", cfg.server_name
            )
            return False
        except asyncio.CancelledError:
            if is_asyncio_outer_cancellation():
                raise
            logger.warning(
                "[JiuWenSwarmDeepAdapter] MCP register cancelled (anyio internal), skip: "
                "name=%s transport=%s path=%s",
                cfg.server_name,
                cfg.client_type,
                cfg.server_path,
            )
            return False
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MCP server register failed: %s", exc)
            return False

    async def _unregister_mcp_server(self, server_id: str) -> None:
        if self._instance is None:
            return
        cfg = self._registered_mcp_servers.get(server_id)
        server_name = getattr(cfg, "server_name", "") if cfg is not None else ""
        try:
            await Runner.resource_mgr.remove_mcp_server(server_id)
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MCP server remove failed: %s", exc)
        if server_name:
            try:
                self._instance.ability_manager.remove(server_name)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] MCP ability remove failed: %s", exc)
        self._registered_mcp_server_ids.discard(server_id)
        self._registered_mcp_servers.pop(server_id, None)

    async def register_request_scoped_office_claw_mcp(
        self,
        request: AgentRequest,
    ) -> OfficeClawMcpRegistration | None:
        """Install Relay's legacy OfficeClaw MCP tools for one request only."""

        raw_config = extract_office_claw_mcp(request.params)
        request_mcp_servers = extract_request_mcp_servers(request.params)
        if raw_config is None and request_mcp_servers is None:
            # 无 MCP 载荷（既无 office_claw_mcp 也无 request_mcp_servers）：静默不注册。
            logger.info(
                "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP not registered: "
                "request_id=%s reason=no_mcp_payload",
                request.request_id,
            )
            return None
        if self._instance is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP skipped: "
                "request_id=%s agent is not initialized",
                request.request_id,
            )
            return None

        tool_ids: list[str] = []
        tool_names: list[str] = []
        registered_tools: list[RequestScopedOfficeClawMcpTool] = []
        invocation_id = "-"
        try:
            request_scope = hashlib.sha256(
                f"{request.session_id}:{request.request_id}".encode("utf-8")
            ).hexdigest()[:20]
            # seen_names 跨两源去重：Source1(可信自带 office-claw)重名→fail-fast；
            # Source2(用户连接器)重名→仅跳过该工具，不中断整次注册。
            seen_names: set[str] = set()

            # --- Source 1: 自带 office-claw MCP（identity-pinned）。 ---
            if raw_config is not None:
                params = validate_office_claw_mcp_config(raw_config)
                tool_defs = await list_office_claw_mcp_tools(params)
                for tool_def in tool_defs:
                    tool_name = str(tool_def.get("name") or "").strip()
                    if not tool_name or tool_name in seen_names:
                        raise RuntimeError("OfficeClaw MCP returned an invalid or duplicate tool name")
                    seen_names.add(tool_name)
                    tool_id = f"office-claw-request-{request_scope}.office-claw.{tool_name}"
                    card = ToolCard(
                        id=tool_id,
                        name=tool_name,
                        description=str(tool_def.get("description") or ""),
                        input_params=tool_def.get("input_params") or {},
                    )
                    tool = RequestScopedOfficeClawMcpTool(
                        card, params, request.request_id, "office-claw"
                    )
                    add_result = Runner.resource_mgr.add_tool(tool, tag="office-claw")
                    is_ok = getattr(add_result, "is_ok", None)
                    add_succeeded = True
                    if callable(is_ok):
                        add_succeeded = bool(is_ok())
                    elif isinstance(add_result, bool):
                        add_succeeded = add_result
                    if not add_succeeded:
                        raise RuntimeError(f"failed to register OfficeClaw MCP tool: {tool_name}")
                    # Track before touching the AbilityManager so partial failures
                    # are still fully removable by the common cleanup path.
                    tool_ids.append(tool_id)
                    tool_names.append(tool_name)
                    registered_tools.append(tool)
                    self._install_office_claw_ability_card(card)
                request_env = params.get("env") if isinstance(params.get("env"), dict) else {}
                invocation_id = str(request_env.get("OFFICE_CLAW_INVOCATION_ID") or "").strip() or "-"

            # --- Source 2: 用户连接器（request_mcp_servers）。 ---
            # 单连接器失败不中断整次注册（对齐 Relay 的 per-connector skip 语义）。
            if request_mcp_servers is not None:
                for server_name, server_config in request_mcp_servers.items():
                    # office-claw 已由 Source1 处理。
                    if server_name == "office-claw" and raw_config is not None:
                        continue
                    try:
                        connector_tool_defs, connector_params = (
                            await list_request_mcp_server_tools(
                                server_name, server_config
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] request-scoped MCP connector '%s' "
                            "discovery error: request_id=%s error=%s",
                            server_name,
                            request.request_id,
                            exc,
                        )
                        continue
                    if not connector_tool_defs:
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] request-scoped MCP connector '%s' "
                            "no tools: request_id=%s",
                            server_name,
                            request.request_id,
                        )
                        continue
                    # 单连接器失败不中断注册，但若其全部工具都注册失败则升 error（避免静默缺工具）。
                    _connector_registered = 0
                    for tool_def in connector_tool_defs:
                        tool_name = str(tool_def.get("name") or "").strip()
                        if not tool_name or tool_name in seen_names:
                            logger.warning(
                                "[JiuWenSwarmDeepAdapter] request-scoped MCP connector '%s' "
                                "duplicate/invalid tool name '%s' skipped (a tool "
                                "with this name is already registered by an "
                                "earlier connector/office-claw): request_id=%s",
                                server_name,
                                tool_name,
                                request.request_id,
                            )
                            continue
                        seen_names.add(tool_name)
                        tool_id = (
                            f"office-claw-request-{request_scope}."
                            f"{server_name}.{tool_name}"
                        )
                        card = ToolCard(
                            id=tool_id,
                            name=tool_name,
                            description=str(tool_def.get("description") or ""),
                            input_params=tool_def.get("input_params") or {},
                        )
                        # connector_params 是经 create_mcp_tool 安全层过滤的连接参数
                        # （stdio 启动参数，或 sse/streamable-http 连接描述 + _mcp_client_type）；
                        # 首次 invoke 按 (request_id, server_name) 起长生命周期进程/连接并复用。
                        tool = RequestScopedOfficeClawMcpTool(
                            card, connector_params, request.request_id, server_name
                        )
                        add_result = Runner.resource_mgr.add_tool(tool, tag="office-claw")
                        is_ok = getattr(add_result, "is_ok", None)
                        add_succeeded = True
                        if callable(is_ok):
                            add_succeeded = bool(is_ok())
                        elif isinstance(add_result, bool):
                            add_succeeded = add_result
                        if not add_succeeded:
                            logger.warning(
                                "[JiuWenSwarmDeepAdapter] request-scoped MCP connector '%s' "
                                "tool '%s' resource_mgr.add_tool failed: request_id=%s",
                                server_name,
                                tool_name,
                                request.request_id,
                            )
                            continue
                        tool_ids.append(tool_id)
                        tool_names.append(tool_name)
                        registered_tools.append(tool)
                        self._install_office_claw_ability_card(card)
                        _connector_registered += 1
                        if (
                            server_name == "office-claw"
                            and invocation_id == "-"
                        ):
                            connector_env = (
                                connector_params.get("env")
                                if isinstance(connector_params.get("env"), dict)
                                else {}
                            )
                            invocation_id = (
                                str(
                                    connector_env.get("OFFICE_CLAW_INVOCATION_ID") or ""
                                ).strip()
                                or "-"
                            )
                    if _connector_registered == 0:
                        logger.error(
                            "[JiuWenSwarmDeepAdapter] request-scoped MCP connector "
                            "'%s' registered 0/%d tools — all failed (duplicate "
                            "names or add_tool errors); invoke of its tools will "
                            "find nothing: request_id=%s",
                            server_name,
                            len(connector_tool_defs),
                            request.request_id,
                        )
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] request-scoped MCP connector '%s' "
                        "registered: request_id=%s tools=%s",
                        server_name,
                        request.request_id,
                        [str(t.get("name") or "") for t in connector_tool_defs],
                    )

            registration = OfficeClawMcpRegistration(
                request_id=request.request_id,
                tool_ids=tuple(tool_ids),
                tool_names=tuple(tool_names),
                tool_instances=tuple(registered_tools),
                invocation_id="" if invocation_id == "-" else invocation_id,
            )
            self._active_office_claw_mcp = registration
            # Store tool_ids on the agent's shared ability_manager so the
            # supervisor / round task (created before bind_active_office_claw_mcp_tools)
            # can re-bind the ContextVar before invoking OfficeClaw tools.
            set_agent_office_claw_tool_ids(self._instance, tool_ids)
            publish_live_office_claw_allowlist(registration.tool_ids)
            for registered_tool in registered_tools:
                register_live_office_claw_tool_instance(
                    registered_tool,
                    registration.tool_ids,
                )
            self._sync_office_claw_allowlist_to_progressive_rail(
                registration.tool_ids,
                delivery_thread_id=self._office_claw_thread_id_from_tools(
                    registered_tools
                ),
                invocation_id="" if invocation_id == "-" else invocation_id,
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP registered: "
                "request_id=%s session_id=%s invocation_id=%s tools=%s tool_ids=%s "
                "tool_instances=%d",
                request.request_id,
                request.session_id,
                invocation_id,
                tool_names,
                tool_ids,
                len(registered_tools),
            )
            logger.info("[latency] stage=2 name=mcp request_id=%s", request.request_id)
            return registration
        except asyncio.CancelledError:
            registration = OfficeClawMcpRegistration(
                request_id=request.request_id,
                tool_ids=tuple(tool_ids),
                tool_names=tuple(tool_names),
                tool_instances=tuple(registered_tools),
                invocation_id="" if invocation_id == "-" else invocation_id,
            )
            await self.cleanup_request_scoped_office_claw_mcp(registration)
            raise
        except Exception as exc:
            registration = OfficeClawMcpRegistration(
                request_id=request.request_id,
                tool_ids=tuple(tool_ids),
                tool_names=tuple(tool_names),
                tool_instances=tuple(registered_tools),
                invocation_id="" if invocation_id == "-" else invocation_id,
            )
            await self.cleanup_request_scoped_office_claw_mcp(registration)
            _raw_command = str(raw_config.get("command") or "").strip() if isinstance(raw_config, dict) else ""
            _connector_names = (
                list(request_mcp_servers.keys())
                if request_mcp_servers is not None
                else []
            )
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP registration failed; "
                "continuing without it: request_id=%s error=%s raw_command=%s connectors=%s",
                request.request_id,
                exc,
                _raw_command,
                _connector_names,
            )
            return None

    def _owned_office_claw_tool_ids(self) -> frozenset[str]:
        active = self._active_office_claw_mcp
        if active is None:
            return frozenset()
        return frozenset(active.tool_ids)

    def _install_office_claw_ability_card(self, card: ToolCard) -> None:
        """Register a request-scoped OfficeClaw tool card by short name.

        Same-id rebinds are allowed. A different id is replaced only when the
        existing card belongs to this adapter's active registration; otherwise
        registration fails hard so a concurrent request cannot silently steal
        the short-name mapping.
        """

        if self._instance is None:
            raise RuntimeError("OfficeClaw MCP ability install requires an initialized agent")
        ability_manager = self._instance.ability_manager
        tool_name = str(card.name or "").strip()
        tool_id = str(card.id or "").strip()
        if not tool_name or not tool_id:
            raise RuntimeError("OfficeClaw MCP tool card is missing name or id")

        existing = None
        getter = getattr(ability_manager, "get", None)
        if callable(getter):
            existing = getter(tool_name)
        existing_id = str(getattr(existing, "id", "") or "") if existing is not None else ""

        if existing is not None and existing_id and existing_id != tool_id:
            if existing_id in self._owned_office_claw_tool_ids():
                try:
                    ability_manager.remove(tool_name)
                except Exception as exc:
                    raise RuntimeError(
                        f"failed to replace owned OfficeClaw MCP ability: {tool_name}"
                    ) from exc
                try:
                    Runner.resource_mgr.remove_tool(existing_id)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] failed to remove replaced OfficeClaw MCP "
                        "resource: tool_id=%s error=%s",
                        existing_id,
                        exc,
                    )
            else:
                raise RuntimeError(
                    f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name} "
                    f"(existing_id={existing_id}, new_id={tool_id})"
                )

        ability_result = ability_manager.add(card)
        added = getattr(ability_result, "added", None) if ability_result is not None else None
        if added is False:
            # Conflict-aware AbilityManager kept the existing card. Retry once if
            # we own it; otherwise fail closed.
            existing = getter(tool_name) if callable(getter) else None
            existing_id = str(getattr(existing, "id", "") or "") if existing is not None else ""
            if existing_id and existing_id in self._owned_office_claw_tool_ids():
                ability_manager.remove(tool_name)
                ability_result = ability_manager.add(card)
                added = getattr(ability_result, "added", None) if ability_result is not None else None
            if added is False:
                raise RuntimeError(
                    f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name}"
                )

        # Legacy AbilityManager.add() returns None and may overwrite by name.
        # Always verify the short-name mapping lands on *this* card id so a
        # conflict-aware or raced manager cannot leave a foreign bind in place.
        installed = getter(tool_name) if callable(getter) else None
        installed_id = str(getattr(installed, "id", "") or "") if installed is not None else ""
        if installed_id != tool_id:
            raise RuntimeError(
                f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name} "
                f"(existing_id={installed_id or '-'}, new_id={tool_id})"
            )

    async def cleanup_request_scoped_office_claw_mcp(
        self,
        registration: OfficeClawMcpRegistration | None,
    ) -> None:
        """Best-effort removal of tools installed for one Relay request."""

        if registration is None:
            return
        for registered_tool in registration.tool_instances:
            unregister_live_office_claw_tool_instance(registered_tool)
        for tool_id in registration.tool_ids:
            try:
                Runner.resource_mgr.remove_tool(tool_id)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP resource cleanup failed: "
                    "request_id=%s tool_id=%s error=%s",
                    registration.request_id,
                    tool_id,
                    exc,
                )
        if self._instance is not None:
            ability_manager = self._instance.ability_manager
            getter = getattr(ability_manager, "get", None)
            owned_ids = frozenset(registration.tool_ids)
            for tool_name in registration.tool_names:
                try:
                    existing = getter(tool_name) if callable(getter) else None
                    existing_id = (
                        str(getattr(existing, "id", "") or "") if existing is not None else ""
                    )
                    # Only drop the short-name mapping when it still points at
                    # this request's tool id — never delete another request's bind.
                    if existing is None:
                        continue
                    if existing_id and existing_id not in owned_ids:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] skip OfficeClaw MCP ability cleanup; "
                            "short name rebound: request_id=%s tool=%s existing_id=%s",
                            registration.request_id,
                            tool_name,
                            existing_id,
                        )
                        continue
                    ability_manager.remove(tool_name)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP ability cleanup failed: "
                        "request_id=%s tool=%s error=%s",
                        registration.request_id,
                        tool_name,
                        exc,
                    )
        if (
            self._active_office_claw_mcp is not None
            and self._active_office_claw_mcp.request_id == registration.request_id
        ):
            self._active_office_claw_mcp = None
            self._sync_office_claw_allowlist_to_progressive_rail(None)
            # Only clear the shared AM allowlist when we are cleaning the *current*
            # active registration. A superseded request's cleanup must not wipe the
            # newer request's allowlist (ask_user resume race → active_id empty).
            clear_agent_office_claw_tool_ids(self._instance)
        elif self._active_office_claw_mcp is None:
            clear_agent_office_claw_tool_ids(self._instance)
            self._sync_office_claw_allowlist_to_progressive_rail(None)
        else:
            logger.info(
                "[JiuWenSwarmDeepAdapter] cleanup skipped clearing active OfficeClaw "
                "allowlist: cleaned_request=%s active_request=%s",
                registration.request_id,
                self._active_office_claw_mcp.request_id,
            )
        revoke_live_office_claw_allowlist(registration.tool_ids)
        # 销毁本请求池化的长生命周期 stdio session（如 chrome-devtools-mcp 浏览器进程），
        # 以免其活过 chat.send。best-effort，不阻塞清理。
        try:
            await release_request_scoped_mcp_sessions(registration.request_id)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped MCP session release failed: "
                "request_id=%s error=%s",
                registration.request_id,
                exc,
            )
        logger.info(
            "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP cleaned up: request_id=%s",
            registration.request_id,
        )

    def _sync_office_claw_allowlist_to_progressive_rail(
        self,
        tool_ids: tuple[str, ...] | list[str] | frozenset[str] | None,
        *,
        delivery_thread_id: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Keep ProgressiveToolRail's interaction-round allowlist in sync."""

        rail = getattr(self, "_progressive_tool_rail", None)
        setter = getattr(rail, "set_office_claw_active_tool_ids", None)
        if not callable(setter):
            return
        try:
            setter(
                tool_ids,
                delivery_thread_id=delivery_thread_id,
                invocation_id=invocation_id,
            )
        except TypeError:
            # Older rail signature without keyword binding metadata.
            setter(tool_ids)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] failed to sync OfficeClaw allowlist to "
                "ProgressiveToolRail: %s",
                exc,
            )

    @staticmethod
    def _office_claw_thread_id_from_tools(
        tools: list[RequestScopedOfficeClawMcpTool],
    ) -> str:
        for tool in tools:
            params = getattr(tool, "_params", None)
            if not isinstance(params, dict):
                continue
            env = params.get("env")
            if not isinstance(env, dict):
                continue
            thread_id = str(env.get("OFFICE_CLAW_THREAD_ID") or "").strip()
            if thread_id:
                return thread_id
        return ""

    async def _register_mcp_servers_from_config(
        self, config_base: dict[str, Any], *, tag: str = "agent.main"
    ) -> None:
        enabled_entries = self._extract_enabled_mcp_server_entries(config_base)
        for entry in enabled_entries:
            cfg = self._build_mcp_server_config(entry)
            if cfg is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skip invalid mcp server entry: %s",
                    entry.get("name", "<unknown>"),
                )
                continue
            await self._register_mcp_server(cfg, tag=tag)

    async def _sync_mcp_servers_for_runtime(
        self, config_base: dict[str, Any], *, tag: str = "agent.reload"
    ) -> None:
        if self._instance is None:
            return
        enabled_entries = self._extract_enabled_mcp_server_entries(config_base)
        desired_by_name: dict[str, McpServerConfig] = {}
        for entry in enabled_entries:
            cfg = self._build_mcp_server_config(entry)
            if cfg is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skip invalid mcp server entry: %s",
                    entry.get("name", "<unknown>"),
                )
                continue
            server_name = str(getattr(cfg, "server_name", "") or "").strip()
            if not server_name:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skip mcp server without server_name: %s",
                    entry.get("name", "<unknown>"),
                )
                continue
            desired_by_name[server_name] = cfg

        current_by_name: dict[str, tuple[str, McpServerConfig]] = {}
        for server_id, cfg in self._registered_mcp_servers.items():
            server_name = str(getattr(cfg, "server_name", "") or "").strip()
            if not server_name or server_name in current_by_name:
                continue
            current_by_name[server_name] = (server_id, cfg)

        current_names = set(current_by_name.keys())
        desired_names = set(desired_by_name.keys())
        to_remove = current_names - desired_names
        to_add = desired_names - current_names
        to_check = current_names & desired_names

        for server_name in to_remove:
            server_id = current_by_name[server_name][0]
            await self._unregister_mcp_server(server_id)

        for server_name in to_add:
            await self._register_mcp_server(desired_by_name[server_name], tag=tag)

        for server_name in to_check:
            server_id, current_cfg = current_by_name[server_name]
            desired_cfg = desired_by_name[server_name]
            current_sig = {
                "server_name": getattr(current_cfg, "server_name", None),
                "client_type": getattr(current_cfg, "client_type", None),
                "server_path": getattr(current_cfg, "server_path", None),
                "params": getattr(current_cfg, "params", None),
                "auth_headers": getattr(current_cfg, "auth_headers", None),
                "auth_query_params": getattr(current_cfg, "auth_query_params", None),
            }
            desired_sig = {
                "server_name": getattr(desired_cfg, "server_name", None),
                "client_type": getattr(desired_cfg, "client_type", None),
                "server_path": getattr(desired_cfg, "server_path", None),
                "params": getattr(desired_cfg, "params", None),
                "auth_headers": getattr(desired_cfg, "auth_headers", None),
                "auth_query_params": getattr(desired_cfg, "auth_query_params", None),
            }
            if json.dumps(current_sig, sort_keys=True, default=str) == json.dumps(
                desired_sig, sort_keys=True, default=str
            ):
                continue
            await self._unregister_mcp_server(server_id)
            await self._register_mcp_server(desired_cfg, tag=tag)

    @staticmethod
    def _build_vision_model_config(
        config_base: dict[str, Any],
    ) -> VisionModelConfig | None:
        """Build DeepAgent vision config from service config/env mapping."""
        if not dedicated_multimodal_model_configured(config_base, "vision"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] vision tools skipped: models.vision has no dedicated "
                "api_key in config.yaml"
            )
            return None
        apply_vision_model_config_from_yaml(config_base)
        api_key = str(read_env("VISION_API_KEY", "")).strip()
        base_url = str(read_env("VISION_BASE_URL") or read_env("VISION_API_BASE") or "").strip()
        model_name = str(read_env("VISION_MODEL") or read_env("VISION_MODEL_NAME") or "").strip()
        if not api_key or not base_url or not model_name:
            logger.info("[JiuWenSwarmDeepAdapter] vision tools skipped: incomplete config")
            return None
        return VisionModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            max_retries=parse_int(read_env("VISION_MAX_RETRIES"), 3),
        )

    @staticmethod
    def _build_audio_model_config(
        config_base: dict[str, Any],
    ) -> AudioModelConfig | None:
        """Build DeepAgent audio config from service config/env mapping."""
        if not complete_multimodal_model_configured(config_base, "audio"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] audio tools skipped: models.audio requires "
                "api_key, api_base, and model_name in config.yaml"
            )
            return None
        apply_audio_model_config_from_yaml(config_base)
        api_key = str(read_env("AUDIO_API_KEY", "")).strip()
        base_url = str(read_env("AUDIO_BASE_URL") or read_env("AUDIO_API_BASE") or "").strip()
        if not api_key or not base_url:
            logger.info("[JiuWenSwarmDeepAdapter] audio tools skipped: incomplete config")
            return None
        transcription_model = str(
            read_env("AUDIO_TRANSCRIPTION_MODEL") or read_env("AUDIO_MODEL_NAME") or ""
        ).strip()
        question_answering_model = str(
            read_env("AUDIO_QUESTION_ANSWERING_MODEL") or read_env("AUDIO_MODEL_NAME") or ""
        ).strip()
        config_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": parse_int(read_env("AUDIO_MAX_RETRIES"), 3),
            "http_timeout": parse_int(read_env("AUDIO_HTTP_TIMEOUT"), 20),
            "max_audio_bytes": parse_int(
                read_env("AUDIO_MAX_AUDIO_BYTES"),
                25 * 1024 * 1024,
            ),
        }
        acr_access_key = str(read_env("ACR_ACCESS_KEY", "")).strip()
        acr_access_secret = str(read_env("ACR_ACCESS_SECRET", "")).strip()
        acr_base_url = str(read_env("ACR_BASE_URL", "")).strip()
        if acr_access_key:
            config_kwargs["acr_access_key"] = acr_access_key
        if acr_access_secret:
            config_kwargs["acr_access_secret"] = acr_access_secret
        if acr_base_url:
            config_kwargs["acr_base_url"] = acr_base_url
        if transcription_model:
            config_kwargs["transcription_model"] = transcription_model
        if question_answering_model:
            config_kwargs["question_answering_model"] = question_answering_model
        return AudioModelConfig(**config_kwargs)

    @staticmethod
    def _build_video_model_config(
        config_base: dict[str, Any],
    ) -> bool:
        """Build DeepAgent video config from service config/env mapping."""
        apply_video_model_config_from_yaml(config_base)
        if not complete_multimodal_model_configured(config_base, "video"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] skip video_understanding: models.video requires "
                "api_key, api_base, and model_name in config.yaml"
            )
            return False
        video_api_key = str(read_env("VIDEO_API_KEY", "")).strip()
        video_api_base = str(read_env("VIDEO_API_BASE", "")).strip()
        video_model_name = str(read_env("VIDEO_MODEL_NAME", "")).strip()
        if not video_api_key or not video_api_base or not video_model_name:
            logger.info("[JiuWenSwarmDeepAdapter] video tools skipped: incomplete config")
            return False
        return True

    @staticmethod
    def _build_image_gen_model_config(
        config_base: dict[str, Any],
    ) -> bool:
        """Build DeepAgent image generation config from service config/env mapping."""
        apply_image_gen_model_config_from_yaml(config_base)
        if not read_env("IMAGE_GEN_API_KEY"):
            logger.info("[JiuWenSwarmDeepAdapter] image_gen tool skipped: incomplete config")
            return False
        return True

    def _iter_runtime_audio_tools(self, agent_id: str | None) -> list[Any]:
        """Return metadata-only audio tools unless a complete audio model is configured."""
        cfg = self._audio_model_config or AudioModelConfig()
        tools = list(
            create_audio_tools(
                language=self._resolve_runtime_language(),
                audio_model_config=cfg,
                agent_id=agent_id,
            )
        )
        if self._audio_model_config is not None:
            return tools
        return [tool for tool in tools if tool.card.name == "audio_metadata"]

    @staticmethod
    def _mask_model_secret(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "(empty)"
        if len(text) <= 8:
            return "***"
        return f"{text[:4]}***{text[-2:]}"

    def _resolve_default_model_mcc_for_log(self) -> tuple[dict[str, Any], dict[str, str]]:
        """与 ``_create_model`` 同源解析 default 槽位（``get_default_models`` + react 回退）。"""
        meta: dict[str, str] = {}
        config = self._startup_config_base if isinstance(self._startup_config_base, dict) else {}
        entries = get_default_models(config) if config else []
        section = entries[0] if entries and isinstance(entries[0], dict) else {}
        meta["template_name"] = str(section.get("template_name") or "").strip()
        meta["template_id"] = str(section.get("template_id") or "").strip()

        mcc = dict(section.get("model_client_config") or {})
        react = config.get("react") if isinstance(config.get("react"), dict) else {}
        react_mcc = react.get("model_client_config")
        react_mcc = react_mcc if isinstance(react_mcc, dict) else {}

        if not str(mcc.get("model_name") or "").strip():
            mcc["model_name"] = (
                react_mcc.get("model_name")
                or react.get("model_name")
                or read_env("MODEL_NAME", "")
                or "gpt-4"
            )
        if not str(mcc.get("client_provider") or "").strip():
            mcc["client_provider"] = (
                react_mcc.get("client_provider")
                or read_env("MODEL_PROVIDER", "")
            )
        if not str(mcc.get("api_base") or "").strip():
            mcc["api_base"] = react_mcc.get("api_base") or read_env("API_BASE", "")
        if not str(mcc.get("api_key") or "").strip():
            mcc["api_key"] = react_mcc.get("api_key") or read_env("API_KEY", "")
        return mcc, meta

    def _collect_default_model_log_fields(self) -> dict[str, str]:
        """收集 default 槽位字段，供启动日志单行输出（空值保留为占位）。"""
        fields: dict[str, str] = {"source": self._model_config_source}

        if self._model_client_config is not None:
            mcc_obj = self._model_client_config
            mcc = {
                "model_name": getattr(mcc_obj, "model_name", "") or "",
                "client_provider": getattr(mcc_obj, "client_provider", "")
                or getattr(mcc_obj, "provider", "")
                or "",
                "api_base": getattr(mcc_obj, "api_base", "") or "",
                "api_key": getattr(mcc_obj, "api_key", ""),
            }
            meta = {"template_name": "", "template_id": ""}
        else:
            mcc, meta = self._resolve_default_model_mcc_for_log()

        fields["template_name"] = meta.get("template_name", "")
        fields["template_id"] = meta.get("template_id", "")
        fields["model_id"] = str(mcc.get("model_name") or "").strip()
        fields["provider"] = str(
            mcc.get("client_provider") or mcc.get("provider") or ""
        ).strip()
        fields["api_base"] = str(mcc.get("api_base") or "").strip()
        fields["api_key"] = self._mask_model_secret(mcc.get("api_key"))
        return fields

    def _format_active_model_startup_log(self) -> str:
        """仅输出 default 槽位模型配置（启动日志）。"""
        fields = self._collect_default_model_log_fields()
        order = (
            "source",
            "template_name",
            "template_id",
            "model_id",
            "provider",
            "api_base",
            "api_key",
        )

        def _fmt_value(key: str, value: str) -> str:
            if key == "api_key":
                return value or "(empty)"
            return value if value else "(empty)"

        return "; ".join(f"{key}={_fmt_value(key, fields[key])}" for key in order)

    def _log_active_model_on_startup(self, *, phase: str = "create_instance") -> None:
        logger.info(
            "[JiuWenSwarmDeepAdapter] Agent 已启动(%s)，当前使用模型: %s",
            phase,
            self._format_active_model_startup_log(),
        )

    def _merge_enterprise_models_into_config(
        self, config_base: dict[str, Any]
    ) -> dict[str, Any]:
        """若已加载 ``_enterprise_config``，将其模型槽位覆盖到 config 快照上。"""
        if self._enterprise_config is None:
            clear_embed_config_db_cache()
            return config_base
        from jiuwenswarm.server.runtime.enterprise_config.apply_models import (
            apply_enterprise_models_to_config,
        )

        merged, applied = apply_enterprise_models_to_config(
            config_base, self._enterprise_config
        )
        set_embed_config_db_cache(
            getattr(self._enterprise_config, "embedding", None)
        )
        if applied:
            self._model_config_source = "enterprise_policy"
            logger.info(
                "[JiuWenSwarmDeepAdapter] using enterprise model config: slots=%s",
                list(self._enterprise_config.models),
            )
        return merged

    async def _load_enterprise_config(self, request: AgentRequest) -> None:
        """按当前请求的 ``params`` 从 Gateway DB 加载生效企业策略到 ``self._enterprise_config``。"""
        self._enterprise_config = None
        if not is_enterprise():
            return
        try:
            from jiuwenswarm.server.runtime.enterprise_config import (
                DEFAULT_AGENT_LOAD_SLOTS,
                load_effective_enterprise_config,
            )
        except ImportError as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] enterprise_config unavailable: %s", exc
            )
            return

        loaded = await load_effective_enterprise_config(
            request,
            DEFAULT_AGENT_LOAD_SLOTS,
        )
        self._enterprise_config = loaded
        self._agent_permissions_body = resolve_permissions_body_from_enterprise(loaded)
        if loaded is not None and self._skill_manager is not None:
            try:
                await self._skill_manager.apply_skill_source_configs(
                    getattr(loaded, "extension_config", None)
                )
            except Exception as exc:  # noqa: BLE001
                # SourceManager applies revisions atomically; a bad revision leaves
                # the last valid registry active for existing requests.
                logger.error(
                    "[JiuWenSwarmDeepAdapter] skill source config rejected: %s",
                    exc,
                )
        if loaded is None:
            from jiuwenswarm.common.request_identity import web_routing_identity

            identity = web_routing_identity(
                request.metadata if isinstance(request.metadata, dict) else None
            )
            logger.warning(
                "[JiuWenSwarmDeepAdapter] no effective enterprise config loaded "
                "(group_id=%s bot_id=%s user_id=%s)",
                identity.get("group_id"),
                identity.get("bot_id"),
                identity.get("user_id"),
            )

    async def prepare_skill_source_config(self, request: AgentRequest) -> None:
        """Load the effective policy before a source RPC, even before chat startup."""
        await self._load_enterprise_config(request)

    def _inject_extension_config_into_inputs(self, inputs: dict[str, Any]) -> None:
        """将企业策略中的 extension_config 注入 inputs（替代 ee gateway channel_context 透传）。"""
        if not is_enterprise():
            return
        if "extension_config" in inputs:
            return
        if self._enterprise_config is None:
            return
        ext_config = getattr(self._enterprise_config, "extension_config", None)
        if ext_config:
            inputs["extension_config"] = ext_config
            # Rails 从 InvokeInputs.run_context.extra 读取，同步写入 run.context.extra
            run_payload = inputs.get("run")
            if not isinstance(run_payload, dict):
                run_payload = {}
                inputs["run"] = run_payload
            context = run_payload.get("context")
            if not isinstance(context, dict):
                context = {}
                run_payload["context"] = context
            extra = context.get("extra")
            if not isinstance(extra, dict):
                extra = {}
                context["extra"] = extra
            extra["extension_config"] = ext_config
            logger.info(
                "[JiuWenSwarmDeepAdapter] extension_config injected from enterprise config: count=%s",
                len(ext_config) if isinstance(ext_config, list) else "?",
            )

    def _refresh_multimodal_configs(
        self,
        config_base: dict[str, Any],
    ) -> None:
        """Refresh cached multimodal configs and live tool instances."""
        for group in MULTIMODAL_ENV_GROUP_KEYS:
            if dedicated_multimodal_model_configured(config_base, group):
                continue
            if multimodal_env_anchor_present(group):
                continue
            clear_multimodal_env_groups([group])

        self._vision_model_config = self._build_vision_model_config(config_base)
        self._audio_model_config = self._build_audio_model_config(config_base)
        self._video_model_config = self._build_video_model_config(config_base)
        self._image_gen_model_config = self._build_image_gen_model_config(config_base)

        for tool in self._vision_tools:
            tool.vision_model_config = self._vision_model_config
        for tool in self._audio_tools:
            tool.audio_model_config = self._audio_model_config or AudioModelConfig()

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

        Ownership is read off each card (``ToolCard.stateless``), the same way
        ``_get_tool_cards`` registers the group on the create path, so a reload
        cannot re-register a group under a different id than it was built with.
        A group that must stay shared declares it inside its ``create_fn``.

        Args:
            current_tools: Currently registered instances of this group.
            registered: Whether the group is registered right now.
            enabled: Whether config wants the group enabled.
            create_fn: Builds fresh instances when the group turns on.
            warn_label: Group name used in the failure log line.

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
                owner_id = self._tool_owner_id()
                for tool in new_tools:
                    register_tool(tool, owner_id)
                    self._append_tool_card(tool.card)
                    if self._instance is not None and hasattr(self._instance, "ability_manager"):
                        self._instance.ability_manager.add(tool.card)
                return new_tools, bool(new_tools)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] %s reload failed: %s", warn_label, exc)
                return [], False
        return current_tools, registered

    def _remove_registered_tools(self, tools: list[Any]) -> None:
        """Remove tool instances from ability manager and resource manager.

        Only agent-owned registrations leave the process-global resource
        manager; a shared instance is dropped from this agent's ability manager
        alone, because other adapters may still be running on it.
        """
        if not tools:
            return
        for tool in tools:
            try:
                unregister_tool(tool)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] remove tool failed: %s",
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
                        "[JiuWenSwarmDeepAdapter] ability remove skipped for %s",
                        tool.card.name,
                        exc_info=True,
                    )

    def _append_tool_card(self, card: ToolCard) -> None:
        """Append tool card if it is not already tracked."""
        if self._tool_cards is None:
            self._tool_cards = []
        existing_names = {
            item.card.name if hasattr(item, "card") else item.name for item in self._tool_cards
        }
        if card.name not in existing_names:
            self._tool_cards.append(card)

    def _prioritize_paid_search_tool_card(self) -> None:
        """Keep paid_search before free_search when both cards are present."""
        if not self._tool_cards:
            return
        paid_cards = [
            item
            for item in self._tool_cards
            if (item.card.name if hasattr(item, "card") else item.name) == "paid_search"
        ]
        if not paid_cards:
            return
        remaining_cards = [
            item
            for item in self._tool_cards
            if (item.card.name if hasattr(item, "card") else item.name) != "paid_search"
        ]
        free_index = next(
            (
                idx
                for idx, item in enumerate(remaining_cards)
                if (item.card.name if hasattr(item, "card") else item.name) == "free_search"
            ),
            0,
        )
        self._tool_cards = remaining_cards[:free_index] + paid_cards + remaining_cards[free_index:]

    def _prune_tool_cards(self, tool_names: set[str]) -> None:
        """Remove tracked tool cards by tool name."""
        if not self._tool_cards:
            return
        self._tool_cards = [
            item
            for item in self._tool_cards
            if (item.card.name if hasattr(item, "card") else item.name) not in tool_names
        ]

    def _drop_tool_names_from_runtime(self, tool_names: set[str] | frozenset[str]) -> None:
        """Best-effort removal for tool cards that may predate tracked tool instances."""
        if not tool_names:
            return
        card_ids = set(tool_names)
        for item in self._tool_cards or []:
            card = item.card if hasattr(item, "card") else item
            if getattr(card, "name", None) in tool_names:
                card_ids.add(getattr(card, "id", None) or card.name)
        self._prune_tool_cards(set(tool_names))
        if self._instance is not None and hasattr(self._instance, "ability_manager"):
            for tool_name in tool_names:
                try:
                    self._instance.ability_manager.remove(tool_name)
                except Exception:
                    logger.debug(
                        "[JiuWenSwarmDeepAdapter] ability remove skipped for %s",
                        tool_name,
                        exc_info=True,
                    )
        for tool_id in card_ids:
            try:
                Runner.resource_mgr.remove_tool(tool_id)
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] resource remove skipped for %s",
                    tool_id,
                    exc_info=True,
                )

    def _create_skill_retrieval_tools(self) -> list[Any]:
        """Create Agentic skill retrieval tools using the current visible-skill provider.

        Declared shared: skill retrieval reads a process-wide skill index, and
        splitting it per session adapter would give each one its own view of a
        catalogue that is meant to be common.
        """
        if not is_skill_retrieval_enabled():
            logger.info("[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit skipped: disabled")
            return []
        skill_retrieval_toolkit = SkillRetrievalToolkit(
            manager=self._skill_manager,
            visible_skill_names=self._visible_skill_names_for_list_skill,
        )
        tools = mark_stateless(skill_retrieval_toolkit.get_tools())
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit built: tools=%s",
            [tool.card.name for tool in tools],
        )
        return tools

    @staticmethod
    def _skill_retrieval_tools_enabled_for_runtime(
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        """Return whether runtime tool sync should expose Agentic skill retrieval tools."""
        return is_skill_retrieval_enabled()

    def _sync_skill_retrieval_tools_for_runtime(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """Sync Agentic skill retrieval tool registration after config reload."""
        enabled = self._skill_retrieval_tools_enabled_for_runtime(config_base)
        tools, registered = self._sync_tool_group(
            current_tools=self._skill_retrieval_tools,
            registered=self._skill_retrieval_tools_registered,
            enabled=enabled,
            create_fn=self._create_skill_retrieval_tools,
            warn_label="skill retrieval tools",
        )
        self._skill_retrieval_tools = tools
        self._skill_retrieval_tools_registered = registered
        if not enabled:
            self._drop_tool_names_from_runtime(_SKILL_RETRIEVAL_TOOL_NAMES)

    async def _sync_skill_retrieval_prompt_rail_for_runtime(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """Sync Agentic skill retrieval prompt rail after config reload."""
        if self._instance is None:
            return

        enabled = self._skill_retrieval_tools_enabled_for_runtime(config_base)
        rail = self._skill_retrieval_prompt_rail
        if enabled:
            if rail is not None:
                return
            rail = self._build_skill_retrieval_prompt_rail()
            if rail is None:
                return
            try:
                await self._instance.register_rail(rail)
            except Exception as exc:
                self._skill_retrieval_prompt_rail = None
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] SkillRetrievalPromptRail reload failed: %s",
                    exc,
                )
                return
            self._skill_retrieval_prompt_rail = rail
            logger.info("[JiuWenSwarmDeepAdapter] SkillRetrievalPromptRail registered")
            return

        if rail is None:
            return
        try:
            await self._instance.unregister_rail(rail)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillRetrievalPromptRail unregister failed: %s",
                exc,
            )
        finally:
            self._skill_retrieval_prompt_rail = None

    def _sync_multimodal_tools_for_runtime(self) -> None:
        """Sync multimodal tool registration after config reload."""
        # The owner id, not ``card.id``: these instances are agent-owned, and a
        # reload must rebuild them under the same owner the create path used.
        agent_id = self._tool_owner_id()
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

        desired_audio_tools = self._iter_runtime_audio_tools(agent_id)
        if self._audio_tools_registered:
            current_names = {tool.card.name for tool in self._audio_tools}
            desired_names = {tool.card.name for tool in desired_audio_tools}
            if current_names != desired_names:
                self._remove_registered_tools(self._audio_tools)
                self._audio_tools = []
                self._audio_tools_registered = False
        self._audio_tools, self._audio_tools_registered = self._sync_tool_group(
            current_tools=self._audio_tools,
            registered=self._audio_tools_registered,
            enabled=bool(desired_audio_tools),
            create_fn=lambda: desired_audio_tools,
            warn_label="audio tools",
        )

        _, self._video_tool_registered = self._sync_tool_group(
            current_tools=mark_stateless([video_understanding]),
            registered=self._video_tool_registered,
            enabled=bool(self._video_model_config),
            create_fn=lambda: mark_stateless([video_understanding]),
            warn_label="video tool",
        )

        _, self._image_gen_tool_registered = self._sync_tool_group(
            current_tools=mark_stateless([generate_image]),
            registered=self._image_gen_tool_registered,
            enabled=bool(self._image_gen_model_config),
            create_fn=lambda: mark_stateless([generate_image]),
            warn_label="generate_image tool",
        )

    def _tools_in_ability_manager(self, tools: list[Any]) -> bool:
        if not tools or self._instance is None:
            return False
        ability_manager = getattr(self._instance, "ability_manager", None)
        if ability_manager is None:
            return False
        for tool in tools:
            name = str(getattr(getattr(tool, "card", None), "name", "") or "")
            if not name or ability_manager.get(name) is None:
                return False
        return True

    def _sync_preinstance_runtime_tools_to_ability_manager(self) -> None:
        """Sync tools registered before DeepAgent existed into ability_manager.

        ``_get_tool_cards`` registers multimodal instances into the resource
        manager before ``create_deep_agent`` exists, so the fresh
        AbilityManager does not know about them until they are added here.
        """
        if self._instance is None:
            return
        ability_manager = getattr(self._instance, "ability_manager", None)
        if ability_manager is None:
            return
        for tools in (
            list(self._vision_tools or []),
            list(self._audio_tools or []),
        ):
            if not tools or self._tools_in_ability_manager(tools):
                continue
            for tool in tools:
                card = getattr(tool, "card", None)
                if card is None:
                    continue
                try:
                    ability_manager.add(card)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] ability_manager add failed: tool=%s error=%s",
                        getattr(card, "name", ""),
                        exc,
                    )

    def _sync_paid_search_tool_for_runtime(self) -> None:
        """Legacy hook: unified ``web_search`` replaces separate paid-search sync."""
        self._paid_search_tool = None
        self._paid_search_registered = False

    def _sync_symphony_tools_for_runtime(self, config_base: dict[str, Any]) -> None:
        """Sync Symphony tool registration after config reload."""
        try:
            enabled = bool(load_symphony_config(config_base).enabled)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] symphony config reload failed: %s",
                exc,
            )
            enabled = False
        self._symphony_tools, self._symphony_tools_registered = self._sync_tool_group(
            current_tools=self._symphony_tools,
            registered=self._symphony_tools_registered,
            enabled=enabled,
            create_fn=lambda: mark_stateless(SymphonyToolkit().get_tools(config_base)),
            warn_label="symphony tools",
        )

    def _tenant_disk_ids(self) -> tuple[str, str]:
        """Return ``(service_id, agent_id)`` for on-disk tenant paths.

        Prefer request-side ``env_*`` ids so enterprise rewrite of
        ``self._agent_id`` (e.g. ``office_default``) does not divert checkpoint
        / prompt paths away from ``agent_office``.
        """
        from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

        service_id = TenantAgentPool.normalize_tenant_id(
            self._env_service_id if self._env_service_id is not None else self._service_id
        )
        agent_id = TenantAgentPool.normalize_tenant_id(
            self._env_agent_id if self._env_agent_id is not None else self._agent_id
        )
        return service_id, agent_id

    async def set_checkpoint(self) -> None:
        """Create / reuse a per-agent sqlite checkpointer under ``workspace_{key}/.checkpoint``."""
        if self._checkpointer is not None:
            self._bind_checkpointer_to_rails()
            return
        async with self._checkpoint_init_lock:
            if self._checkpointer is not None:
                self._bind_checkpointer_to_rails()
                return
            try:
                PersistenceCheckpointerProvider()
                user_ws = getattr(self, "_user_workspace_dir", None)
                if user_ws is not None:
                    workspace = Path(user_ws)
                else:
                    workspace = get_multi_tenant_user_workspace_dir("default")
                checkpoint_path = workspace / ".checkpoint"
                checkpoint_path.mkdir(parents=True, exist_ok=True)
                conf: dict[str, Any] = {
                    "db_type": "sqlite",
                    "db_path": f"{checkpoint_path}/checkpoint",
                }

                checkpoint_db_type = os.getenv("CHECKPOINT_DB_TYPE", "").strip().lower()
                if is_enterprise():
                    from openjiuwen_runtime.foundation.db.utils import is_mysql, is_postgresql

                    if is_mysql(checkpoint_db_type):
                        mysql_engine = await _build_mysql_async_engine()
                        if mysql_engine is not None:
                            conf["db_client"] = mysql_engine
                            logger.info(
                                "[JiuWenSwarmDeepAdapter] use mysql db_client for checkpointer"
                            )
                    elif is_postgresql(checkpoint_db_type):
                        postgresql_engine = await _build_postgresql_async_engine()
                        if postgresql_engine is not None:
                            conf["db_client"] = postgresql_engine
                            logger.info(
                                "[JiuWenSwarmDeepAdapter] use postgresql db_client "
                                "for checkpointer"
                            )

                checkpointer = await CheckpointerFactory.create(
                    CheckpointerConfig(type="persistence", conf=conf),
                )
                self._checkpointer = checkpointer
                # Do not set process-wide default here: concurrent agents must keep
                # using their own self._checkpointer (align with test/jiuwenclaw).
                self._bind_checkpointer_to_rails()
                logger.info(
                    "[JiuWenSwarmDeepAdapter] persistent checkpointer ready: "
                    "path=%s",
                    checkpoint_path / "checkpoint",
                )
            except Exception as exc:
                logger.error(
                    "[JiuWenSwarmDeepAdapter] fail to setup checkpoint due to: %s",
                    exc,
                )
                # Fall back to process-default checkpointer so create_instance can proceed.
                await ensure_persistent_checkpointer()
                if self._checkpointer is None:
                    self._checkpointer = _shared_checkpoint_checkpointer
                self._bind_checkpointer_to_rails()

    @classmethod
    def _normalize_reload_value(cls, value: Any) -> Any:
        """Normalize config-like values for stable hot-reload comparisons."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_reload_value(val)
                for key, val in sorted(value.items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalize_reload_value(item) for item in value]
        if isinstance(value, set):
            normalized_items = [cls._normalize_reload_value(item) for item in value]
            return sorted(normalized_items, key=repr)
        if hasattr(value, "model_dump"):
            try:
                return cls._normalize_reload_value(value.model_dump(mode="json"))
            except TypeError:
                return cls._normalize_reload_value(value.model_dump())
        if hasattr(value, "dict"):
            return cls._normalize_reload_value(value.dict())
        if hasattr(value, "value") and not isinstance(value, (bytes, bytearray)):
            return cls._normalize_reload_value(value.value)
        if hasattr(value, "__dataclass_fields__"):
            return cls._normalize_reload_value(
                {field: getattr(value, field) for field in value.__dataclass_fields__}
            )
        if hasattr(value, "__dict__"):
            return cls._normalize_reload_value(vars(value))
        return repr(value)

    @classmethod
    def _stable_reload_fingerprint(cls, value: Any) -> str:
        normalized = cls._normalize_reload_value(value)
        payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _model_reload_fingerprint(cls, deep_cfg: Any | None) -> str | None:
        if deep_cfg is None:
            return None
        model = getattr(deep_cfg, "model", None)
        if model is None:
            return None
        return cls._stable_reload_fingerprint(
            {
                "model": {
                    "model_client_config": getattr(model, "model_client_config", None),
                    "model_config": getattr(model, "model_config", None),
                },
                "enable_task_loop": getattr(deep_cfg, "enable_task_loop", False),
                "max_iterations": getattr(deep_cfg, "max_iterations", None),
                "context_engine_config": getattr(deep_cfg, "context_engine_config", None),
            }
        )

    @classmethod
    def _system_prompt_reload_fingerprint(cls, deep_cfg: Any | None) -> str | None:
        if deep_cfg is None:
            return None
        system_prompt = getattr(deep_cfg, "system_prompt", None)
        if system_prompt is None:
            return None
        return cls._stable_reload_fingerprint(
            {
                "system_prompt": system_prompt,
                "language": getattr(deep_cfg, "language", None),
                "prompt_mode": getattr(deep_cfg, "prompt_mode", None),
            }
        )

    def _previous_model_reload_fingerprint(self) -> str | None:
        if self._last_reload_model_fingerprint is not None:
            return self._last_reload_model_fingerprint
        previous_config = getattr(self._instance, "_deep_config", None)
        return self._model_reload_fingerprint(previous_config)

    def _previous_system_prompt_reload_fingerprint(self) -> str | None:
        if self._last_reload_system_prompt_fingerprint is not None:
            return self._last_reload_system_prompt_fingerprint
        previous_config = getattr(self._instance, "_deep_config", None)
        return self._system_prompt_reload_fingerprint(previous_config)

    def _omit_unchanged_reload_fields(
        self,
        deep_cfg: DeepAgentConfig,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        omitted_fields: dict[str, Any] = {}
        reload_fingerprints: dict[str, str] = {}

        model = getattr(deep_cfg, "model", None)
        model_fingerprint = self._model_reload_fingerprint(deep_cfg)
        if model_fingerprint is not None:
            previous_model_fingerprint = self._previous_model_reload_fingerprint()
            if previous_model_fingerprint == model_fingerprint:
                omitted_fields["model"] = model
                deep_cfg.model = None
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] skip unchanged model config during hot reload"
                )
            reload_fingerprints["model"] = model_fingerprint

        system_prompt = getattr(deep_cfg, "system_prompt", None)
        prompt_fingerprint = self._system_prompt_reload_fingerprint(deep_cfg)
        if prompt_fingerprint is not None:
            previous_prompt_fingerprint = self._previous_system_prompt_reload_fingerprint()
            if previous_prompt_fingerprint == prompt_fingerprint:
                omitted_fields["system_prompt"] = system_prompt
                deep_cfg.system_prompt = None
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] skip unchanged system prompt during hot reload"
                )
            reload_fingerprints["system_prompt"] = prompt_fingerprint

        return omitted_fields, reload_fingerprints

    @staticmethod
    def _restore_omitted_reload_fields(
        deep_cfg: DeepAgentConfig,
        omitted_fields: dict[str, Any],
    ) -> None:
        for field_name, field_value in omitted_fields.items():
            setattr(deep_cfg, field_name, field_value)

    def _commit_reload_fingerprints(self, reload_fingerprints: dict[str, str]) -> None:
        model_fingerprint = reload_fingerprints.get("model")
        if model_fingerprint is not None:
            self._last_reload_model_fingerprint = model_fingerprint
        prompt_fingerprint = reload_fingerprints.get("system_prompt")
        if prompt_fingerprint is not None:
            self._last_reload_system_prompt_fingerprint = prompt_fingerprint

    @staticmethod
    def _build_model_from_entry(mcc: dict, mco: dict) -> Model:
        """根据单个模型条目的 model_client_config / model_config_obj 构建 Model 实例。"""
        name = mcc.get("model_name", "")
        mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
        if not mcc_fields.get("client_provider"):
            mcc_fields["client_provider"] = "OpenAI"
        # OfficeClaw / Huawei MaaS syncs real auth via tip ``default_headers``
        # (Authorization: Basic ...). api_key is only the placeholder
        # ``huawei-maas-session``. Merge into custom_headers; llm_sse_patch keeps
        # Authorization through openjiuwen sanitize_headers (otherwise APIG.0303).
        tip_headers = None
        try:
            from jiuwenswarm.common.local_env_config import read_default_headers

            tip_headers = read_default_headers()
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] failed to read tip default_headers",
                exc_info=True,
            )
        if tip_headers:
            existing = mcc_fields.get("custom_headers")
            merged = dict(existing) if isinstance(existing, dict) else {}
            for header_name, header_value in tip_headers.items():
                key = str(header_name)
                # Tip credentials must win for Authorization (MaaS Basic auth).
                if key.lower() == "authorization":
                    merged[key] = str(header_value)
                else:
                    merged.setdefault(key, str(header_value))
            mcc_fields["custom_headers"] = merged
            has_authorization = any(
                str(k).lower() == "authorization" for k in merged
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] merged tip default_headers into "
                "custom_headers: keys=%s has_authorization=%s",
                sorted(str(k) for k in merged),
                has_authorization,
            )
        else:
            api_key = str(mcc_fields.get("api_key", "") or "").strip()
            if api_key == "huawei-maas-session":
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] tip default_headers empty; "
                    "Huawei MaaS calls may fail with APIG.0303"
                )
            else:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] tip default_headers empty "
                    "(api_key_present=%s)",
                    bool(api_key),
                )
        m_config = ModelRequestConfig(
            **build_reasoning_model_request_kwargs(
                model_client_config=mcc_fields,
                model_config_obj=mco,
                model_name=name,
            )
        )
        return Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config)

    def _register_model_cache_entry(
        self,
        entry: dict[str, Any],
        name_counter: dict[str, int],
    ) -> None:
        """Register one model entry into the request-selectable model cache."""
        mcc = entry.get("model_client_config") or {}
        if not mcc.get("model_name"):
            return
        model_name = mcc["model_name"]
        idx = name_counter.get(model_name, 0)
        name_counter[model_name] = idx + 1
        cache_key = f"{model_name}#{idx}"
        try:
            model = self._build_model_from_entry(
                mcc,
                entry.get("model_config_obj") or {},
            )
            self._model_cache[cache_key] = model
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] 跳过无效模型条目 %s: %s",
                model_name, exc,
            )
            return
        if model_name not in self._model_name_to_keys:
            self._model_name_to_keys[model_name] = []
        self._model_name_to_keys[model_name].append(cache_key)

        # 同时用纯 model_name 作为 key 指向 is_default=true 的条目
        if entry.get("is_default") is True:
            self._model_cache[model_name] = self._model_cache[cache_key]

        alias = entry.get("alias") or ""
        if alias and alias != model_name and alias not in self._model_cache:
            self._model_cache[alias] = self._model_cache[cache_key]

        tier_raw = entry.get("tier")
        if not tier_raw:
            return
        tier = str(tier_raw).strip().lower()
        if tier not in ("lite", "pro") or tier in self._tier_model_cache:
            return
        self._tier_model_cache[tier] = self._model_cache[cache_key]

    def _build_model_cache_from_defaults(self, config: dict) -> None:
        """从 models.defaults 列表构建模型缓存。

        key 使用 {model_name}#{index} 格式以支持同名模型共存。
        同时记录 _model_name_to_keys 映射以便按 model_name 查找。
        """
        self._model_name_to_keys.clear()
        self._tier_model_cache.clear()
        name_counter: dict[str, int] = {}

        for entry in get_default_models(config):
            self._register_model_cache_entry(entry, name_counter)

    def _build_model_cache_legacy(self, config: dict) -> None:
        """回退到旧格式（models.default / react 段）构建单条目缓存。"""
        default_model_config = config.get("models", {}).get("default", {})
        react_config = config.get("react", {})

        mcc = dict(
            default_model_config.get("model_client_config")
            or react_config.get("model_client_config")
            or {}
        )
        model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
        if "model_name" not in mcc:
            mcc["model_name"] = model_name

        mco = (
            default_model_config.get("model_config_obj")
            or react_config.get("model_config_obj")
            or {}
        )
        try:
            self._model_cache[model_name] = self._build_model_from_entry(mcc, mco)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] 跳过无效模型条目(legacy) %s: %s",
                model_name, exc,
            )

    @staticmethod
    def _inject_attribution_to_config(config: dict) -> None:
        """Inject OpenRouter attribution headers into all model_client_config entries in-place."""
        from jiuwenswarm.common.openrouter_attribution import inject_attribution_to_config
        inject_attribution_to_config(config)

    def _create_model(self, config: dict) -> Model:
        # 指纹比对：模型配置未变且缓存非空时跳过重建，避免热更新时反复销毁/重建 Model 实例
        new_fp = self._models_config_fingerprint(config)
        if (
            new_fp == self._last_models_config_fingerprint
            and self._model_cache
            and self._model is not None
        ):
            logger.debug(
                "[JiuWenSwarmDeepAdapter] skip model cache rebuild: config unchanged"
            )
            return self._model

        self._model_cache.clear()
        self._model_name_to_keys.clear()
        self._tier_model_cache.clear()
        self._inject_attribution_to_config(config)
        self._build_model_cache_from_defaults(config)
        if not self._model_cache:
            self._build_model_cache_legacy(config)

        if not self._model_cache:
            # config.yaml 占位条目无效时,回退到进程环境变量(API_BASE/API_KEY/MODEL_NAME)
            env_fallback_counter: dict[str, int] = {}
            for entry in get_default_models({"models": {}}):
                try:
                    self._register_model_cache_entry(entry, env_fallback_counter)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] 跳过无效环境变量模型条目: %s",
                        exc,
                    )

        if not self._model_cache:
            raise ValueError(
                "No valid model entries found in config — all entries failed validation. "
                "Check that api_key and api_base are set for at least one model."
            )

        # 优先取 is_default=true 的条目（纯 model_name key），否则取第一个
        default_name = None
        for name, keys in self._model_name_to_keys.items():
            if name in self._model_cache:
                default_name = name
                break
        if default_name is None:
            # 回退：取第一个 #index key
            for key in self._model_cache:
                if "#" in key:
                    default_name = key
                    break
        if default_name is None:
            default_name = next(iter(self._model_cache))

        self._default_model_name = default_name
        self._model = self._model_cache[default_name]
        self._model_client_config = self._model.model_client_config
        self._model_request_config = self._model.model_config
        self._last_models_config_fingerprint = new_fp
        return self._model

    @staticmethod
    def _models_config_fingerprint(config: dict) -> str:
        """计算模型配置段的指纹，用于判断是否需要重建模型缓存。"""
        models = config.get("models", {})
        react = config.get("react") or {}
        # Tip credentials are not in config.yaml; include fingerprints so MaaS
        # default_headers / API_KEY rotation rebuilds the OpenAI client.
        tip_api_key = ""
        tip_api_base = ""
        tip_headers_raw = ""
        try:
            from jiuwenswarm.common.local_env_config import read_default_headers_raw

            tip_api_key = str(read_env("API_KEY", "") or "")
            tip_api_base = str(read_env("API_BASE", "") or "")
            tip_headers_raw = str(read_default_headers_raw() or "")
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] tip credential fingerprint unavailable",
                exc_info=True,
            )

        def _fp(value: str) -> str:
            if not value:
                return ""
            return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

        payload = json.dumps(
            {
                "models": models,
                "react": {
                    "model_client_config": react.get("model_client_config"),
                    "model_name": react.get("model_name"),
                    "model_config_obj": react.get("model_config_obj"),
                },
                "tip": {
                    "api_key_fp": _fp(tip_api_key),
                    "api_base": tip_api_base,
                    "headers_fp": _fp(tip_headers_raw),
                },
            },
            sort_keys=True, default=str, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _resolve_model_by_name(self, requested_model_name: str = "") -> Model | None:
        """Resolve the exact model object that will be used."""
        requested = (requested_model_name or "").strip()
        if not requested:
            return self._model
        # 精确匹配（#index 格式，或已注册纯 model_name key 的默认模型）
        if requested in self._model_cache:
            return self._model_cache[requested]
        # 回退：非默认模型只以 {model_name}#{index} 注册在 _model_cache 里，没有纯
        # model_name 的 key；这里按 _model_name_to_keys 里登记的真实 cache key 去查，
        # 而不是重复判断上面已知为 False 的 `requested in self._model_cache`
        # （旧代码在此处写重了，导致非默认模型永远查不到，静默 fallback 回默认模型）。
        keys = self._model_name_to_keys.get(requested)
        if keys:
            resolved = self._model_cache.get(keys[0])
            if resolved is not None:
                return resolved
        return self._model

    def _lookup_model_by_name(self, requested_model_name: str = "") -> Model | None:
        """Look up a model by exact name/alias without falling back to default.

        Empty name returns None (caller decides fallback). Unknown name returns None.
        """
        requested = (requested_model_name or "").strip()
        if not requested:
            return None
        if requested in self._model_cache:
            return self._model_cache[requested]
        keys = self._model_name_to_keys.get(requested)
        if keys:
            return self._model_cache.get(keys[0])
        return None

    def _resolve_model(
        self,
        *,
        model_name: str = "",
        model_tier: str = "",
    ) -> tuple[Model, str | None]:
        """Resolve Model by model_name or model_tier; fall back to adapter default.

        Priority: valid model_name hit > valid model_tier (lite/pro) > self._model.
        Never hard-fails; second tuple element is reserved (always None today).
        """
        if self._model is None:
            raise RuntimeError("No model configured on adapter")

        name = (model_name or "").strip()
        if name:
            hit = self._lookup_model_by_name(name)
            if hit is not None:
                return hit, None
            logger.info(
                "[JiuWenSwarmDeepAdapter] 模型名无效 model_name=%r，尝试选用等级模型",
                model_name,
            )

        tier = (model_tier or "").strip().lower()
        if tier:
            if tier not in ("lite", "pro"):
                logger.info(
                    "[JiuWenSwarmDeepAdapter] 模型等级无效 model_tier=%r，回退主 Agent 默认模型",
                    model_tier,
                )
                return self._model, None
            model = self._tier_model_cache.get(tier)
            if model is None:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] 模型等级未配置 model_tier=%r，回退主 Agent 默认模型",
                    tier,
                )
                return self._model, None
            return model, None

        if name:
            logger.info(
                "[JiuWenSwarmDeepAdapter] 模型名无效且无可用 model_tier=%r，回退主 Agent 默认模型",
                model_tier or None,
            )
        return self._model, None

    def _resolve_model_for_subagent(
        self,
        *,
        model_name: str = "",
        model_tier: str = "",
    ) -> tuple[Model, str | None]:
        """TaskTool / sessions_spawn model selection entrypoint."""
        return self._resolve_model(model_name=model_name, model_tier=model_tier)

    def _bind_subagent_model_resolver(self) -> None:
        """Expose adapter resolve on the DeepAgent instance for TaskTool."""
        if self._instance is None:
            return
        self._instance.resolve_subagent_model = self._resolve_model_for_subagent

    def _bind_subagent_authorization_wiring(self) -> None:
        """安装子 Agent Skill 动态授权装配（包装 create_subagent，不改 agent-core）。

        仅当父 Context 为 main scope 且动态授权开关开启时，子 Agent 创建才会
        追加 SubagentSkillAuthorizationRail + SubagentPermissionRail 委托 rail 对；
        其余情况零侵入。装配失败不得击穿 Agent 创建。
        """
        if self._instance is None:
            return
        try:
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.authorization import (
                install_subagent_authorization_wiring,
            )

            install_subagent_authorization_wiring(self._instance)
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] subagent authorization wiring failed",
                exc_info=True,
            )

    def _resolve_model_for_request(self, request: AgentRequest) -> Model:
        """根据请求中的 model_name 参数查找对应模型（支持别名），未匹配则回退默认模型。

        支持两种格式：
        - 纯 model_name：查找 is_default=true 的条目
        - {model_name}#{index}：查找指定索引的条目
        """
        requested = (request.params.get("model_name") or "").strip()
        model = self._resolve_model_by_name(requested)
        if model is None:
            raise RuntimeError("No model configured for request")
        return model

    @staticmethod
    def _prepare_multimodal_image_inputs(
        request: AgentRequest,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
            extract_multimodal_image_files,
        )

        image_files = extract_multimodal_image_files(request.params)
        if not image_files:
            return inputs

        updated = dict(inputs)
        updated["_multimodal_image_files"] = image_files
        logger.info(
            "[JiuWenSwarmDeepAdapter] Prepared %d image attachment(s) "
            "for Core multimodal context-window injection",
            len(image_files),
        )
        return updated

    @staticmethod
    def _prepare_react_image_tool_prompt(
        request: AgentRequest,
        inputs: dict[str, Any],
        *,
        enable_read_image_multimodal: bool,
    ) -> dict[str, Any]:
        """Add image file paths to the ReAct prompt when native image input is off."""
        if enable_read_image_multimodal:
            return inputs

        query = inputs.get("query")
        if not isinstance(query, str) or "jiuwenswarm_image_tool_context" in query:
            return inputs

        from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
            extract_multimodal_image_files,
        )

        image_files = inputs.get("_multimodal_image_files")
        if not isinstance(image_files, list) or not image_files:
            image_files = extract_multimodal_image_files(request.params)
        if not image_files:
            return inputs

        params = request.params if isinstance(request.params, dict) else {}
        raw_question = params.get("query")
        if not isinstance(raw_question, str) or not raw_question.strip():
            raw_question = params.get("content")
        question = raw_question.strip() if isinstance(raw_question, str) else ""
        first_path = str(image_files[0].get("path") or "").strip()
        if not first_path:
            return inputs

        media_items = []
        for image_file in image_files:
            path = str(image_file.get("path") or "").strip()
            if not path:
                continue
            media_items.append(
                {
                    "type": "image",
                    "filename": image_file.get("filename") or Path(path).name,
                    "mediaPath": path,
                    "mimeType": image_file.get("mime_type") or image_file.get("mimeType"),
                }
            )
        if not media_items:
            return inputs

        tool_context = {
            "marker": "jiuwenswarm_image_tool_context",
            "mediaPath": first_path,
            "mediaItems": media_items,
            "question": question,
            "toolHint": (
                "当前主模型未启用原生图片输入。如果需要理解图片内容，请调用图片理解工具；"
                "优先使用 image_reading(local_url=mediaPath, prompt=question)，"
                "或使用 visual_question_answering(image_path_or_url=mediaPath, question=question)。"
            ),
        }

        updated = dict(inputs)
        updated.pop("_multimodal_image_files", None)
        updated["query"] = (
            query
            + "\n\n图片附件上下文（供 ReAct 选择图片理解工具使用）：\n"
            + json.dumps(tool_context, ensure_ascii=False)
        )
        return updated

    @staticmethod
    def _build_image_tool_fallback_notice(
        request: AgentRequest,
        *,
        enable_read_image_multimodal: bool,
        model: Any | None,
    ) -> dict[str, Any] | None:
        if enable_read_image_multimodal:
            return None

        from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
            extract_multimodal_image_files,
        )

        image_files = extract_multimodal_image_files(request.params)
        if not image_files:
            return None

        model_config = getattr(model, "model_config", None)
        model_name = str(getattr(model_config, "model_name", "") or "").strip()
        model_label = f"（{model_name}）" if model_name else ""
        content = f"当前模型{model_label}不支持原生图片理解，已切换为图片理解工具处理。"
        notice = {
            "event_type": "chat.notice",
            "notice_type": "image_tool_fallback",
            "level": "info",
            "content": content,
            "request_id": request.request_id,
            "session_id": request.session_id,
            "image_count": len(image_files),
        }
        if model_name:
            notice["model_name"] = model_name
        return notice

    def _native_image_input_enabled(self, config: dict[str, Any], model: Any | None) -> bool:
        # read_file 默认不读取图片：不把图片字节内联进主模型对话，改走 image_reading / VQA 工具。
        # 仅当 config.yaml 显式设置 enable_read_image_multimodal 时遵从配置；模型名启发式已移除。
        return self._resolve_enable_read_image_multimodal(config)

    @staticmethod
    def _resolve_enable_read_image_multimodal(config: dict[str, Any]) -> bool:
        configured = config.get("enable_read_image_multimodal")
        if isinstance(configured, bool):
            return configured
        return DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL

    def _apply_model_to_react_agent(self, model: Model) -> None:
        """将指定模型应用到 react_agent 实例（替换 _llm 和 _config 字段）。

        react_agent._railed_model_call 使用 self._config.model_name 作为 model= 参数，
        因此需要同时替换 _llm 和 _config 中的模型相关字段。
        """
        react_agent = getattr(self._instance, "_react_agent", None)
        if react_agent is None:
            return
        if callable(getattr(react_agent, "set_llm", None)):
            react_agent.set_llm(model)
        config = getattr(react_agent, "_config", None)
        if config is not None:
            config.model_name = model.model_config.model_name
            config.model_client_config = model.model_client_config
            config.model_config_obj = model.model_config
        self._model_request_config = model.model_config
        # Keep adapter default + DeepAgent parent model in sync so omitted
        # task_tool model_* selectors inherit the currently switched model.
        self._model = model
        deep_config = getattr(self._instance, "_deep_config", None) or getattr(
            self._instance, "deep_config", None
        )
        if deep_config is not None:
            try:
                deep_config.model = model
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] failed to sync deep_config.model",
                    exc_info=True,
                )

    @staticmethod
    def _resolve_skill_mode(config: dict[str, Any]) -> str:
        """Validate configured skill mode and fallback safely on invalid values."""
        if is_skill_retrieval_enabled():
            return SkillUseRail.SKILL_MODE_AUTO_LIST
        raw_skill_mode = config.get("skill_mode", SkillUseRail.SKILL_MODE_ALL)
        valid_modes = {
            SkillUseRail.SKILL_MODE_AUTO_LIST,
            SkillUseRail.SKILL_MODE_ALL,
        }
        if isinstance(raw_skill_mode, str) and raw_skill_mode in valid_modes:
            return raw_skill_mode

        logger.warning(
            "[JiuWenSwarmDeepAdapter] invalid skill_mode=%r, fallback to %s",
            raw_skill_mode,
            SkillUseRail.SKILL_MODE_ALL,
        )
        return SkillUseRail.SKILL_MODE_ALL

    def _visible_skill_names_for_list_skill(self) -> set[str]:
        """Return the skill names exposed by the matching SkillUseRail setup."""
        from jiuwenswarm.server.runtime.skill.skill_manager import (
            enabled_skills_from_environ,
        )

        skills_dirs = [Path(p) for p in self._resolve_skill_dirs()]
        disabled_skills = set(
            self._skill_manager.list_execution_disabled_skills()
            if self._skill_manager is not None
            else []
        )
        enabled = self._enabled_skills
        if is_skill_whitelist_tenant(self._agent_id, self._service_id) and enabled is None:
            enabled = []
        elif enabled is None:
            raw = enabled_skills_from_environ()
            if raw is not None:
                enabled = [
                    item.strip()
                    for item in str(raw).replace(";", ",").split(",")
                    if item.strip()
                ]
        enabled_set = set(enabled) if enabled is not None else None
        visible: set[str] = set()
        for skills_dir in skills_dirs:
            try:
                for child in sorted(skills_dir.iterdir(), key=lambda path: path.name.lower()):
                    if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
                        continue
                    if child.name in disabled_skills:
                        continue
                    if enabled_set is not None and child.name not in enabled_set:
                        continue
                    if (child / "SKILL.md").is_file():
                        visible.add(child.name)
            except OSError as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] failed to scan visible skills: %s", exc)
        return visible

    @staticmethod
    def _build_response_prompt_rail() -> ResponsePromptRail | None:
        """Build ResponsePromptRail so message rules keep priority ordering."""
        try:
            rail = ResponsePromptRail()
            logger.info("[JiuWenSwarmDeepAdapter] ResponsePromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] ResponsePromptRail create failed: %s", exc)
            rail = None
        return rail

    def _create_sandbox_sys_operation(
        self,
        sandbox_url: str,
        sandbox_type: str,
        *,
        runtime: dict[str, Any] | None = None,
        project_dir: str | None = None,
    ) -> SysOperationCard | None:
        """Create a sandbox SysOperationCard.

        Delegates the actual construction to ``sysop_builder.py`` so that both
        ``interface_deep.py`` and ``interface_code.py`` share one implementation.

        历史上这是 ``@staticmethod``——但 sysop_builder 现在需要知道适配器形态
        (``is_code_agent`` 决定是否把 ``project_dir`` 挂为 rw bind), 这个信号是
        instance state (``self._is_code_agent``, 基类默认 False, ``JiuwenSwarm
        CodeAdapter`` 子类 override 成 True), staticmethod 拿不到。 因此必须降
        成 instance method 才能透传; 调用方相应把 ``JiuWenSwarmDeepAdapter
        ._create_sandbox_sys_operation(...)`` 改成 ``self._create_sandbox_
        sys_operation(...)``, 走类 MRO 让 Code 子类覆写时也能命中。

        Args:
            sandbox_url: jiuwenbox HTTP base url.
            sandbox_type: provider 名 (jiuwenbox).
            runtime: ``sandbox`` 字段字典 (含 enabled / files / excluded_commands
                / idle_ttl_seconds / idle_check_interval), 来自
                ``get_sandbox_runtime``.
            project_dir: 用户项目目录 (一般是 ``trusted_dirs[0]``); 仅在
                ``self._is_code_agent=True`` 时被 :func:`build_filesystem_policy`
                消费作为 rw bind mount, 否则 (deep adapter 等通用形态) 完全
                忽略——sysop_builder 不会有 cwd / env 之类的 fallback 接管。
        """
        runtime = runtime or {}
        shared_dir: str | None = None
        if is_enterprise() and self._workspace_dir:
            # 企业多租户：挂载当前 workspace 根，修复下载路径权限
            shared_dir = str(Path(self._workspace_dir).resolve().parent.parent)
        return create_sandbox_sysop_card(
            sandbox_url,
            sandbox_type,
            files_runtime=runtime.get("files"),
            excluded_commands=runtime.get("excluded_commands"),
            idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
            idle_check_interval=runtime.get("idle_check_interval"),
            fallback_on_failure=bool(runtime.get("fallback_on_failure", False)),
            project_dir=project_dir,
            is_code_agent=self._is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
            shared_dir=shared_dir,
        )

    def _resolve_project_dir_for_sandbox(self) -> str | None:
        """Best-effort lookup of the user project directory for sandbox builds.

        Prefers ``self._project_dir``,
        then falls back to ``self._instance_overrides["project_dir"]`` which
        :meth:`AgentManager.get_agent` populates from ``trusted_dirs[0]``.
        Returning ``None`` lets :func:`build_filesystem_policy` use its own
        fallback chain, but the agent-server cwd usually isn't what we want
        so callers should treat ``None`` as "policy will mount cwd, which
        may shadow secrets" and at minimum log it.
        """
        direct = getattr(self, "_project_dir", None)
        if direct:
            return str(direct)
        overrides = getattr(self, "_instance_overrides", None)
        if isinstance(overrides, dict):
            value = overrides.get("project_dir")
            if value:
                return str(value)
        return None

    @staticmethod
    def _sys_operation_isolation_key(sysop_card: SysOperationCard) -> str | None:
        try:
            sys_operation = SysOperation(sysop_card)
            return sys_operation.isolation_key_template
        except Exception as exc:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] failed to resolve sys_operation isolation key: %s",
                exc,
            )
            return None

    @staticmethod
    def _get_registered_sys_operation_by_isolation_key(
        isolation_key_template: str | None,
    ) -> SysOperation | None:
        if not isolation_key_template:
            return None

        try:
            resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
            if resource_registry is None:
                return None
            sys_operation_mgr = resource_registry.sys_operation()
            owner_map = getattr(sys_operation_mgr, "_sandbox_key_owner_map", {})
            existing_op_id = owner_map.get(isolation_key_template)
            if not existing_op_id:
                return None
            return Runner.resource_mgr.get_sys_operation(existing_op_id)
        except Exception as exc:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] failed to get registered sys_operation: %s",
                exc,
            )
            return None

    def _create_sys_operation(self) -> SysOperation | None:
        """Resolve this adapter's sys operation and take a reference on it.

        Wraps :meth:`_resolve_sys_operation` with the bookkeeping that keeps
        ``Runner.resource_mgr`` from growing without bound: the resolved id is
        retained here and released in :meth:`cleanup`. The new reference is taken
        *before* the previous one is dropped, so rebuilding the agent (a skill or
        plugin install re-runs ``create_instance``) onto the same sandbox id never
        lets the refcount hit zero and unregister a resource still in use.

        Returns:
            The resolved SysOperation, or None when registration failed.
        """
        previously_retained = list(self._retained_sys_operation_ids)
        sys_operation = self._resolve_sys_operation()
        if sys_operation is not None:
            self._retain_sys_operation(str(sys_operation.id))
        self._release_sys_operations(previously_retained)
        return sys_operation

    def _retain_sys_operation(self, sys_operation_id: str) -> None:
        """Record one adapter-held reference on a registered sys operation.

        Args:
            sys_operation_id: Id of the sys operation this adapter now depends on.
        """
        self._retained_sys_operation_ids.append(sys_operation_id)
        with _SYS_OPERATION_REFCOUNT_LOCK:
            _SYS_OPERATION_REFCOUNTS[sys_operation_id] = (
                _SYS_OPERATION_REFCOUNTS.get(sys_operation_id, 0) + 1
            )

    def _release_sys_operations(self, sys_operation_ids: list[str] | None = None) -> None:
        """Drop adapter-held references and unregister the ones left unused.

        Removing a sys operation also removes the ~16 fs/shell/code tools derived
        from it (``ResourceMgr.remove_sys_operation``), which is exactly the state
        that used to survive every evicted session adapter. Failures are logged
        and swallowed: this runs on the cleanup path and must not stop it.

        Args:
            sys_operation_ids: Subset of this adapter's retained ids to release.
                Defaults to every id it still holds, which is what ``cleanup``
                wants.
        """
        if sys_operation_ids is None:
            ids_to_release = list(self._retained_sys_operation_ids)
        else:
            ids_to_release = list(sys_operation_ids)
        if not ids_to_release:
            return

        unused_ids: list[str] = []
        unheld_ids: list[str] = []
        with _SYS_OPERATION_REFCOUNT_LOCK:
            for sys_operation_id in ids_to_release:
                if sys_operation_id in self._retained_sys_operation_ids:
                    self._retained_sys_operation_ids.remove(sys_operation_id)
                held = _SYS_OPERATION_REFCOUNTS.get(sys_operation_id, 0)
                if held <= 0:
                    # Releasing a reference this adapter never took. Removing the
                    # resource here could pull it out from under a live holder,
                    # so leave it registered and surface the bookkeeping bug.
                    unheld_ids.append(sys_operation_id)
                    continue
                remaining = held - 1
                if remaining > 0:
                    _SYS_OPERATION_REFCOUNTS[sys_operation_id] = remaining
                    continue
                _SYS_OPERATION_REFCOUNTS.pop(sys_operation_id, None)
                unused_ids.append(sys_operation_id)

        for sys_operation_id in unheld_ids:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] sys_operation release skipped, no reference held: %s",
                sys_operation_id,
            )

        for sys_operation_id in unused_ids:
            try:
                # Quiet idempotence: only remove what is still registered, so a
                # second cleanup (or one after Runner.stop cleared everything) is
                # a no-op rather than an error.
                if Runner.resource_mgr.get_sys_operation(sys_operation_id) is None:
                    continue
                Runner.resource_mgr.remove_sys_operation(sys_operation_id)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] sys_operation released: %s",
                    sys_operation_id,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] sys_operation release failed: id=%s error=%s",
                    sys_operation_id,
                    exc,
                )

    def _resolve_sys_operation(self) -> SysOperation | None:
        """Create a sys operation.

        是否走沙箱由 ``config.yaml::sandbox.enabled`` 决定（同时要求
        ``sandbox.url`` / ``sandbox.type`` 已配置）。其他 sandbox 字段
        (``excluded_commands`` / ``files`` / ``idle_ttl_seconds`` /
        ``idle_check_interval``) 透传给 ``create_sandbox_sysop_card``,
        分别写入 ``launcher_config.extra_params`` 与 ``launcher_config`` 上
        的同名字段。

        注意: 每次都从 ``get_sandbox_endpoint()`` 读最新 sandbox.url/type, 因为
        ``/sandbox enable`` 会动态写入这两个字段; yuanrong 也会填默认占位 url。

        副作用: 在 ``self._sys_operation_card`` 保存生成或复用的 SysOperationCard，
        供 ``apply_sandbox_runtime_patch`` 等运行时热更使用。
        """
        try:
            endpoint = get_sandbox_endpoint()
            sandbox_url = endpoint.get("url") or None
            sandbox_type = endpoint.get("type") or None
            runtime = get_sandbox_runtime()
            sysop_card: SysOperationCard | None
            if runtime.get("enabled") and sandbox_url and sandbox_type:
                # 走 ``self.`` 而不是 ``JiuWenSwarmDeepAdapter.``——_create_sandbox_
                # sys_operation 已从 staticmethod 改成 instance method (要透传
                # ``self._is_code_agent``), 用类名直接调会绕过 MRO 把 Code 子类
                # 的 override (如果将来需要的话) 静默吃掉, 且 staticmethod 时代
                # 的 caller 风格不再适用。
                sysop_card = self._create_sandbox_sys_operation(
                    sandbox_url,
                    sandbox_type,
                    runtime=runtime,
                    project_dir=self._resolve_project_dir_for_sandbox(),
                )
            else:
                sysop_card = create_local_sysop_card()
            if sysop_card is None:
                logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: sysop_card is None")
                return None
            self._sys_operation_card = sysop_card
            isolation_key_template = JiuWenSwarmDeepAdapter._sys_operation_isolation_key(sysop_card)
            registered_sys_operation = (
                JiuWenSwarmDeepAdapter._get_registered_sys_operation_by_isolation_key(
                    isolation_key_template
                )
            )
            if registered_sys_operation is not None:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] reuse registered sys_operation: %s",
                    registered_sys_operation.id,
                )
                return registered_sys_operation

            result = Runner.resource_mgr.add_sys_operation(sysop_card)
            if result.is_err():
                registered_sys_operation = (
                    JiuWenSwarmDeepAdapter._get_registered_sys_operation_by_isolation_key(
                        isolation_key_template
                    )
                )
                if registered_sys_operation is not None:
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] reuse registered sys_operation after add failure: %s",
                        registered_sys_operation.id,
                    )
                    return registered_sys_operation
                logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: %s", result.msg())
                return None
            return Runner.resource_mgr.get_sys_operation(sysop_card.id)
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] add sys_operation failed: %s", exc)
            return None

    async def apply_sandbox_runtime_patch(
        self, runtime: dict[str, Any], *, files_changed: bool
    ) -> None:
        """轻量级热更新沙箱 runtime 参数（无需重建 agent）.

        - 通过 mutate 已构建 SysOperationCard 的 ``launcher_config.extra_params``
          字典让 provider 下次 exec 时读到新值（provider 持 dict 引用）。
        - ``files_changed=True`` 时:
          - jiuwenbox: 调用 ``force_recreate_jiuwenbox_sandbox``
          - yuanrong: 调用 ``delete_yuanrong_sandbox``，下次 op 懒创建新实例

        Args:
            runtime: ``get_sandbox_runtime()`` 当前完整 runtime。
            files_changed: 是否触发文件 policy 变更; 仅 files.* 子命令需要 True。
        """
        card = self._sys_operation_card
        if card is None or card.mode != OperationMode.SANDBOX:
            logger.info(
                "[JiuWenSwarmDeepAdapter] apply_sandbox_runtime_patch skipped: "
                "no active sandbox sys_operation"
            )
            return

        launcher = card.gateway_config.launcher_config if card.gateway_config else None
        if launcher is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] apply_sandbox_runtime_patch: missing launcher_config"
            )
            return

        sandbox_type = str(getattr(launcher, "sandbox_type", "") or "").strip().lower()
        if sandbox_type == "yuanrong":
            logger.info(
                "[JiuWenSwarmDeepAdapter] yuanrong runtime patch "
                "(files_changed=%s; policy/exclude ignored)",
                files_changed,
            )
            if files_changed:
                try:
                    from openjiuwen.extensions.sys_operation.sandbox.providers.yuanrong import (
                        build_yuanrong_shared_scope_key,
                        delete_yuanrong_sandbox,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] delete_yuanrong_sandbox import "
                        "failed: %s",
                        exc,
                    )
                    return
                try:
                    isolation_key = self._sys_operation_isolation_key(card)
                    shared_key = (
                        build_yuanrong_shared_scope_key(
                            str(launcher.base_url), str(isolation_key)
                        )
                        if isolation_key
                        else None
                    )
                    deleted = await delete_yuanrong_sandbox(
                        shared_key=shared_key,
                        base_url=(
                            str(launcher.base_url)
                            if launcher.base_url and not shared_key
                            else None
                        ),
                        reason="files_changed",
                    )
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] yuanrong sandbox deleted for "
                        "recreate: %s",
                        deleted,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] delete_yuanrong_sandbox failed: %s",
                        exc,
                    )
            return

        extra = launcher.extra_params or {}
        extra["excluded_commands"] = list(runtime.get("excluded_commands") or [])
        extra["fallback_on_failure"] = bool(runtime.get("fallback_on_failure", False))
        shared_dir: str | None = None
        if is_enterprise() and self._workspace_dir:
            shared_dir = str(Path(self._workspace_dir).resolve().parent.parent)
        new_policy, upload_list = build_filesystem_policy(
            runtime.get("files") or {},
            project_dir=self._resolve_project_dir_for_sandbox(),
            is_code_agent=self._is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
            shared_dir=shared_dir,
        )
        extra["policy"] = new_policy
        # provider 侧契约: 沙箱 sysop 永远带这两个 key, mode 固定 ``mount``,
        # upload_list 当前一定是空 list。
        extra["preserve_files_upload"] = upload_list
        extra["preserve_file_sharing_mode"] = "mount"
        extra.setdefault("policy_mode", "append")
        launcher.extra_params = extra
        logger.info(
            "[JiuWenSwarmDeepAdapter] sandbox runtime patched "
            "(exclude=%d, files_changed=%s, uploads=%d)",
            len(extra["excluded_commands"]),
            files_changed,
            len(upload_list),
        )

        if files_changed:
            try:
                from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import (
                    force_recreate_jiuwenbox_sandbox,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] force_recreate_jiuwenbox_sandbox import "
                    "failed: %s",
                    exc,
                )
                return
            try:
                new_sandbox_id = await force_recreate_jiuwenbox_sandbox(
                    launcher.base_url,
                    policy=new_policy,
                    policy_mode=extra.get("policy_mode", "append"),
                    preserve_files_upload=upload_list,
                )
                extra["sandbox_id"] = new_sandbox_id
                logger.info(
                    "[JiuWenSwarmDeepAdapter] sandbox instance recreated: %s",
                    new_sandbox_id,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] force_recreate_jiuwenbox_sandbox "
                    "failed: %s",
                    exc,
                )

    @staticmethod
    def _build_filesystem_rail() -> SysOperationRail | None:
        """Build SysOperationRail."""
        try:
            fs_rail = SysOperationRail()
            logger.info("[JiuWenSwarmDeepAdapter] SysOperationRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SysOperationRail create failed: %s", exc)
            fs_rail = None
        return fs_rail

    @staticmethod
    def _get_active_package_config_paths() -> list[str]:
        """Read harness-packages.json to get config_path from active packages.

        Returns:
            List of harness_config.yaml paths from active packages.
        """
        config_paths: list[str] = []
        try:
            if not _HARNESS_PACKAGES_FILE.exists():
                return config_paths

            data = json.loads(_HARNESS_PACKAGES_FILE.read_text(encoding="utf-8"))
            active_ids = data.get("active_package_ids", [])
            if not active_ids:
                return config_paths

            for pkg in data.get("packages", []):
                pkg_id = pkg.get("id", "")
                if pkg_id not in active_ids:
                    continue

                config_path = pkg.get("config_path", "")
                if not config_path:
                    continue

                config_file = Path(config_path)
                if config_file.exists():
                    config_paths.append(str(config_file))
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] Found active package config: %s (package=%s)",
                        config_path,
                        pkg_id,
                    )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] Failed to read active packages config paths: %s",
                exc,
            )

        return config_paths

    async def _load_active_packages(self) -> list[str]:
        """Load all active packages via load_harness_config.

        Called after agent instance is created to restore previously activated
        packages (skills, rails, tools) from harness-packages.json.

        Returns:
            List of loaded resource names.
        """
        if self._instance is None:
            return []

        config_paths = self._get_active_package_config_paths()
        if not config_paths:
            return []

        loaded: list[str] = []
        for config_path in config_paths:
            try:
                resources = await self._instance.load_harness_config(config_path)
                if resources:
                    loaded.extend(resources)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] Loaded active package from %s: %s",
                        config_path,
                        resources,
                    )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Failed to load active package %s: %s",
                    config_path,
                    exc,
                )

        return loaded

    async def apply_package_change(
        self, operation: str, config_path: str
    ) -> list[str] | None:
        """Load/unload a single harness package on this adapter's DeepAgent.

        Args:
            operation: "activate" or "deactivate".
            config_path: Absolute path to harness_config.yaml.

        Returns:
            Loaded/unloaded resource names, or ``None`` when there is no instance.
        """
        if self._instance is None:
            return None
        try:
            if operation == "deactivate":
                return await self._instance.unload_harness_config(config_path)
            return await self._instance.load_harness_config(config_path)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] apply_package_change(%s) failed on %s: %s",
                operation,
                config_path,
                exc,
            )
            return None

    async def apply_package_change_to_session_adapters(
        self,
        operation: str,
        config_path: str,
    ) -> None:
        """Propagate a harness package change to every live session adapter.

        Args:
            operation: "activate" or "deactivate".
            config_path: Absolute path to harness_config.yaml.
        """
        if self._is_session_scoped_adapter:
            # A session-scoped child only owns itself; nothing further to fan out.
            return
        if not self._session_adapters:
            return
        for sid, adapter in list(self._session_adapters.items()):
            if adapter is self:
                continue
            try:
                await adapter.apply_package_change(operation, config_path)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] session adapter %s %s failed: %s",
                    sid,
                    operation,
                    exc,
                )

    def _build_skill_rail(
        self,
        config: dict[str, Any],
        include_tools: bool = False,
        extra_skill_dir: str | None = None,
    ) -> SkillUseRail | None:
        """Build SkillUseRail."""
        try:
            from jiuwenswarm.server.runtime.skill.skill_manager import (
                enabled_skills_from_environ,
            )

            skill_mode = self._resolve_skill_mode(config)
            logger.info("[JiuWenSwarmDeepAdapter] current skill_mode: %s", skill_mode)
            skills_dirs = self._resolve_skill_dirs(extra_skill_dir)
            enabled_skills = self._enabled_skills
            if is_skill_whitelist_tenant(self._agent_id, self._service_id) and enabled_skills is None:
                enabled_skills = []
            elif enabled_skills is None:
                # OfficeClaw tip path: ENABLED_SKILLS from sync_agents_configs.
                enabled_skills = enabled_skills_from_environ()
            skill_rail_kwargs: dict[str, Any] = dict(
                skills_dir=skills_dirs,
                skill_mode=skill_mode,
                include_tools=include_tools,
                disabled_skills=self._skill_manager.list_execution_disabled_skills()
                if self._skill_manager is not None
                else [],
            )
            if enabled_skills is not None:
                skill_rail_kwargs["enabled_skills"] = enabled_skills
            try:
                skill_rail = SkillUseRail(**skill_rail_kwargs)
            except TypeError:
                skill_rail_kwargs.pop("enabled_skills", None)
                skill_rail = SkillUseRail(**skill_rail_kwargs)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillUseRail create success skills_dirs=%s enabled=%s",
                skills_dirs,
                enabled_skills,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillUseRail create failed: %s", exc)
            skill_rail = None
        return skill_rail

    def _build_skill_authorization_rail(self):
        """Build SkillAuthorizationRail；rail 内部按实时 feature flag 惰性启停。

        engine 复用 PermissionInterruptRail 实例的引擎；skill_resolver 延迟读取
        最新 SkillUseRail（热更新/重建会替换 self._skill_rail，绑定构建期实例
        可能拿到未扫盘的旧实例导致 manifest_unresolved）；trust 判定用 rail 默认
        实现 + 企业预置目录快照；_refresh_skill_identity 在审批前按安装账本
        校准预置身份（0708 607aa6146）。
        """
        try:
            from openjiuwen.harness.rails.skills.skill_authorization_rail import (
                SkillAuthorizationRail,
                build_skill_registry_resolver,
            )
            from jiuwenswarm.common.utils import get_builtin_skills_dir

            permission_rail = getattr(self, "_permission_rail", None)
            registry_resolver = build_skill_registry_resolver(
                # dev-stable SkillUseRail.skills_meta 是 property（0708 为方法）
                lambda: (
                    self._skill_rail.skills_meta
                    if self._skill_rail is not None
                    else []
                ),
                skill_dirs_provider=lambda: self._resolve_skill_dirs(),
            )

            def resolve_skill(skill_name: str):
                """目录由 SkillUseRail 解析，来源/版本由 workspace 账本补强。"""
                location = registry_resolver(skill_name)
                if location is None:
                    return None
                skill_dir, fallback_source, fallback_version = location
                try:
                    installation = next(
                        (
                            row
                            for row in self._skill_installation_snapshot()
                            if str(row.get("name") or "").strip() == skill_name
                        ),
                        None,
                    )
                except Exception:  # noqa: BLE001 — 账本异常时保留目录身份
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] resolve skill ledger identity failed "
                        "skill=%s",
                        skill_name,
                        exc_info=True,
                    )
                    installation = None
                if installation is None:
                    return location
                source = str(
                    installation.get("source_id")
                    or installation.get("source")
                    or installation.get("origin")
                    or fallback_source
                ).strip()
                version = str(
                    installation.get("version_id")
                    or installation.get("version")
                    or fallback_version
                    or ""
                ).strip()
                return skill_dir, source or fallback_source, version or None

            rail = SkillAuthorizationRail(
                engine=getattr(permission_rail, "_engine", None),
                skill_resolver=resolve_skill,
                prebuilt_dirs_provider=self._prebuilt_skill_dirs_snapshot,
                builtin_dirs_provider=lambda: [get_builtin_skills_dir()],
                skill_identity_refresher=self._refresh_skill_identity,
                config_provider=get_effective_permissions_config,
                session_config_merger=lambda config, session_id: (
                    merge_session_permissions_overlay(
                        config,
                        session_id=session_id,
                    )
                ),
                scene_bypass=lambda: bool(
                    TOOL_PERMISSION_CONTEXT.get() is not None
                    and TOOL_PERMISSION_CONTEXT.get().scene == "group_digital_avatar"
                ),
            )
            logger.info("[JiuWenSwarmDeepAdapter] SkillAuthorizationRail create success")
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillAuthorizationRail create failed: %s", exc
            )
            return None

    def _skill_rail_session_id(self) -> str | None:
        """Session id bound into skill rails for session-scoped adapters.

        OfficeClaw runs one DeepAgent per session; tool callbacks often lack
        ``conversation_id`` on ``ToolCallInputs``, so rails must carry the real
        ``officeclaw_…`` id instead of falling back to ``default``.
        Prefer ``_parent_session_id`` whenever set (not only when the scoped
        flag is already True) so early rail builds cannot miss the id.
        """
        sid = str(self._parent_session_id or "").strip()
        if sid:
            return sid
        if not self._is_session_scoped_adapter:
            return None
        return None

    def _build_skill_active_state_rail(self) -> SkillActiveStateRail | None:
        try:
            rail = SkillActiveStateRail(session_id=self._skill_rail_session_id())
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillActiveStateRail create success "
                "(session_id=%s)",
                self._skill_rail_session_id() or "-",
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillActiveStateRail create failed: %s",
                exc,
            )
            return None

    def _build_skill_credential_injection_rail(
        self,
        config: dict[str, Any],
    ) -> SkillCredentialInjectionRail | None:
        try:
            skill_envs = coalesce_skill_envs(config.get("skill_envs"), None)
            rail = SkillCredentialInjectionRail(
                skill_envs=skill_envs,
                preset_session_id=self._skill_rail_session_id(),
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillCredentialInjectionRail create success "
                "(skills=[%s], session_id=%s)",
                ", ".join(skill_envs.keys()) if skill_envs else "",
                self._skill_rail_session_id() or "-",
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillCredentialInjectionRail create failed: %s",
                exc,
            )
            return None

    def _build_skill_evolution_rail(self, config: dict[str, Any]) -> SkillEvolutionRail | None:
        """Build SkillEvolutionRail.

        Product contract: while the rail is mounted, generated experiences always
        persist (``rail.auto_save=True``). Automatic version merge after
        persistence is gated per-skill by ``resolve_skill_evolution_action``
        (``selfEvolution=auto`` only).
        """
        if not get_skill_evolution_enabled(config):
            return None
        from jiuwenswarm.agents.harness.observability_runtime import (
            get_trajectory_span_processor,
        )

        try:
            evolution_triggers = get_passive_skill_evolution_triggers(config)
            model_name = self._default_model_name or config.get("model_name", "gpt-4")
            skill_evolution_rail = SkillEvolutionRail(
                skills_dir=self._resolve_skill_dirs(),
                llm=self._model,
                model=model_name,
                review_runtime=EvolutionReviewRuntime(),
                review_trigger=evolution_triggers["review_trigger"],
                signal_trigger=evolution_triggers["signal_trigger"],
                auto_save=True,
                disabled_skills=merge_evolution_disabled_skills(
                    self._skill_manager.list_execution_disabled_skills()
                ),
                trajectory_span_processor=get_trajectory_span_processor(),
            )
            self._skill_evolution_rail = skill_evolution_rail
            logger.info("[JiuWenSwarmDeepAdapter] SkillEvolutionRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillEvolutionRail create failed: %s", exc)
            skill_evolution_rail = None
        return skill_evolution_rail

    async def _ensure_active_evolution_rails_registered(self) -> None:
        """Configure, register, and cache single-agent skill evolution rails."""
        if self._instance is None:
            return

        resolved_language = self._resolve_runtime_language()
        evolution_triggers = get_passive_skill_evolution_triggers(self._config_cache)
        if (
            self._skill_evolution_rail is not None
            and getattr(self._skill_evolution_rail, "_language", None) != resolved_language
        ):
            await self._unconfigure_active_evolution_rails()

        disabled_skills = merge_evolution_disabled_skills(
            self._skill_manager.list_execution_disabled_skills()
            if self._skill_manager is not None
            else []
        )
        from jiuwenswarm.agents.harness.observability_runtime import (
            get_trajectory_span_processor,
        )

        await configure_skill_evolution_runtime(
            self._instance,
            skills_dir=self._resolve_skill_dirs(),
            llm=self._model,
            model=self._default_model_name
            or self._config_cache.get("model_name", "gpt-4"),
            review_trigger=evolution_triggers["review_trigger"],
            signal_trigger=evolution_triggers["signal_trigger"],
            auto_save=True,
            disabled_skills=disabled_skills,
            language=resolved_language,
            trajectory_span_processor=get_trajectory_span_processor(),
        )
        self._refresh_active_evolution_rail_refs()
        if self._skill_evolution_rail is not None:
            sync_evolution_disabled_skills(self._skill_evolution_rail, disabled_skills)
            _set_skill_evolution_triggers(
                self._skill_evolution_rail,
                review_trigger=evolution_triggers["review_trigger"],
                signal_trigger=evolution_triggers["signal_trigger"],
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillEvolutionRail configured "
                "(review_trigger=%s, signal_trigger=%s)",
                evolution_triggers["review_trigger"],
                evolution_triggers["signal_trigger"],
            )

    async def refresh_enabled_skills_from_db(self) -> None:
        """workspace Skill 状态变更后刷新启用集并热替换 ``SkillUseRail``。"""
        if not is_skill_whitelist_tenant(self._agent_id, self._service_id):
            return
        if self._instance is None:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] refresh_enabled_skills_from_db skipped: instance not ready"
            )
            return

        try:
            manager = self._skill_manager or SkillManager(
                workspace_dir=self._workspace_dir,
                service_id=str(self._service_id or ""),
                agent_id=str(self._agent_id or ""),
            )
            names = manager.list_enabled_skill_names()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenSwarmDeepAdapter] list workspace enabled skills failed: %s",
                exc,
            )
            return

        self._enabled_skills = [str(name) for name in names if str(name).strip()]

        # 同步刷新预置身份快照（动态授权 trust 判定）；失败保留旧快照。
        try:
            self._refresh_prebuilt_skill_snapshot()
        except Exception:  # noqa: BLE001 — 身份快照刷新失败不阻断技能热刷新
            logger.warning(
                "[JiuWenSwarmDeepAdapter] refresh prebuilt skill identity failed",
                exc_info=True,
            )

        extra_skill_dir: str | None = None
        try:
            from jiuwenswarm.extensions.registry import ExtensionRegistry
            from jiuwenswarm.extensions.hooks_context import SystemPromptHookContext
            from jiuwenswarm.extensions.hook_event import AgentServerHookEvents

            context = SystemPromptHookContext()
            await ExtensionRegistry.get_instance().trigger(
                AgentServerHookEvents.BEFORE_SYSTEM_PROMPT_BUILD, context
            )
            extra_skill_dir = context.skill_dir
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] refresh_enabled_skills hook skipped: %s",
                exc,
            )

        config = self._config_cache if isinstance(self._config_cache, dict) else {}
        old_rail = self._skill_rail
        new_rail = self._build_skill_rail(
            config,
            include_tools=self._skill_include_tools_for_profile(),
            extra_skill_dir=extra_skill_dir,
        )
        if new_rail is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] refresh_enabled_skills_from_db: SkillUseRail rebuild failed"
            )
            return

        old_unregistered = False
        if old_rail is not None:
            try:
                await self._instance.unregister_rail(old_rail)
                old_unregistered = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] unregister old SkillUseRail failed: %s",
                    exc,
                )

        try:
            await self._instance.register_rail(new_rail)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenSwarmDeepAdapter] register new SkillUseRail failed: %s",
                exc,
            )
            if old_rail is not None and old_unregistered:
                try:
                    await self._instance.register_rail(old_rail)
                except Exception as restore_exc:  # noqa: BLE001
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] restore old SkillUseRail after refresh failure failed: %s",
                        restore_exc,
                    )
            return

        self._skill_rail = new_rail
        logger.info(
            "[JiuWenSwarmDeepAdapter] enabled_skills refreshed from DB: count=%s",
            len(self._enabled_skills),
        )

    async def _unconfigure_active_evolution_rails(self) -> None:
        """Remove cached single-agent evolution rails before rebuilding them."""
        if self._instance is None:
            return

        rails = [
            rail
            for rail in (self._skill_evolution_rail, self._evolution_interrupt_rail)
            if rail is not None
        ]
        try:
            unconfigure_skill_evolution(self._instance, team=False)
        except AttributeError:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] evolution bulk unconfigure unavailable"
            )
        unregister = getattr(self._instance, "unregister_rail", None)
        if callable(unregister):
            for rail in rails:
                await unregister(rail)
        stale_rails = getattr(self._instance, "_stale_rails", None)
        if isinstance(stale_rails, list):
            removed_ids = {id(rail) for rail in rails}
            self._instance._stale_rails = [  # pylint: disable=protected-access
                rail for rail in stale_rails if id(rail) not in removed_ids
            ]
        self._skill_evolution_rail = None
        self._evolution_interrupt_rail = None

    def _refresh_active_evolution_rail_refs(self) -> None:
        """Refresh cached rail references after agent-core runtime configure."""
        if self._instance is None:
            return
        find_rails = getattr(self._instance, "find_rails_by_type", None)
        if not callable(find_rails):
            return

        regular_rails = find_rails((SkillEvolutionRail,))
        self._skill_evolution_rail = None
        for rail in regular_rails:
            if isinstance(rail, SkillEvolutionRail) and not isinstance(
                rail, EvolutionInterruptRail
            ):
                self._skill_evolution_rail = rail
                break
        interrupt_rails = find_rails((EvolutionInterruptRail,))
        self._evolution_interrupt_rail = next(iter(interrupt_rails), None)
        subagent_rails = find_rails((SubagentRail,))
        self._subagent_rail = next(iter(subagent_rails), None)

    def _sync_active_evolution_review_agent_after_reload(self) -> None:
        """Restore SkillEvolutionRail-owned review subagent after DeepAgent hot reload."""
        if self._instance is None or self._skill_evolution_rail is None:
            return
        if not get_evolution_enabled(self._config_cache):
            return

        register_review_agent = getattr(
            self._skill_evolution_rail,
            "_register_evolution_review_agent",
            None,
        )
        if callable(register_review_agent):
            register_review_agent(self._instance)

        self._refresh_active_evolution_rail_refs()

    def _build_skill_create_rail(self, config: dict[str, Any]) -> SkillCreateRail | None:
        """Build SkillCreateRail for new skill creation proposals.

        SkillCreateRail requires task-loop mode (enable_task_loop=True) to function
        because it uses AFTER_TASK_ITERATION event and enqueue_follow_up().
        Config: ``react.evolution.skill_evolution`` enables the rail; legacy
        ``evolution.skill_create`` remains supported for backward compatibility.
        """
        try:
            skill_create_enabled = (
                get_skill_evolution_enabled(config)
                or get_skill_create_enabled(config)
            )
            if not skill_create_enabled:
                logger.debug("[JiuWenSwarmDeepAdapter] SkillCreateRail disabled by config")
                return None

            from jiuwenswarm.agents.harness.observability_runtime import (
                get_trajectory_span_processor,
            )

            language = config.get("language", "cn")
            skills_dirs = self._resolve_skill_dirs()
            rail = SkillCreateRail(
                skills_dir=skills_dirs[0] if skills_dirs else str(get_agent_skills_dir()),
                auto_trigger=True,
                language=language,
                trajectory_span_processor=get_trajectory_span_processor(),
            )
            self._skill_create_rail = rail
            logger.info("[JiuWenSwarmDeepAdapter] SkillCreateRail created with auto_trigger=True")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillCreateRail create failed: %s", exc)
            rail = None
        return rail

    @staticmethod
    def _build_stream_event_rail() -> JiuSwarmStreamEventRail | None:
        """Build JiuSwarmStreamEventRail."""
        try:
            stream_event_rail = JiuSwarmStreamEventRail()
            logger.info("[JiuWenSwarmDeepAdapter] JiuSwarmStreamEventRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] JiuSwarmStreamEventRail create failed: %s", exc)
            stream_event_rail = None
        return stream_event_rail

    def _build_deepresearch_execution_rail(
        self,
    ) -> DeepResearchExecutionRail | None:
        """Build the native HITL bridge for deepresearch_execute."""
        try:
            rail = DeepResearchExecutionRail(model_provider=lambda: self._model)
            logger.info(
                "[JiuWenSwarmDeepAdapter] DeepResearchExecutionRail create success"
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] DeepResearchExecutionRail create failed: %s",
                exc,
            )
            return None

    @staticmethod
    def _build_context_overflow_recovery_rail() -> ContextOverflowRecoveryRail | None:
        """Build ContextOverflowRecoveryRail."""
        try:
            recovery_rail = ContextOverflowRecoveryRail(max_recovery_attempts=3)
            logger.info("[JiuWenClawDeepAdapter] ContextOverflowRecoveryRail create success")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] ContextOverflowRecoveryRail create failed: %s", exc)
            recovery_rail = None
        return recovery_rail

    @staticmethod
    def _build_task_execution_rail() -> TaskExecutionRail | None:
        """Build TaskExecutionRail for task.start/complete/update lifecycle events."""
        try:
            task_rail = TaskExecutionRail()
            logger.info("[JiuWenSwarmDeepAdapter] TaskExecutionRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] TaskExecutionRail create failed: %s", exc)
            task_rail = None
        return task_rail

    @staticmethod
    def _build_request_summary_rail() -> Any | None:
        """Build RequestSummaryRail for per-request performance summaries."""
        try:
            from jiuwenswarm.perf.request_summary_rail import RequestSummaryRail

            # Parent adapter owns finalize; rail only records llm/tool events.
            rail = RequestSummaryRail(record_only=True)
            logger.info("[JiuWenSwarmDeepAdapter] RequestSummaryRail create success")
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] RequestSummaryRail create failed: %s",
                exc,
            )
            rail = None
        return rail

    @staticmethod
    def _build_extension_config_debug_rail() -> Any | None:
        """Build ExtensionConfigDebugRail for extension config end-to-end debugging."""
        try:
            from jiuwenswarm.agents.harness.common.rails.extension_config_debug_rail import (
                ExtensionConfigDebugRail,
            )
            rail = ExtensionConfigDebugRail()
            logger.info("[JiuWenSwarmDeepAdapter] ExtensionConfigDebugRail create success")
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ExtensionConfigDebugRail create failed: %s", exc
            )
            rail = None
        return rail

    @staticmethod
    def _load_extra_rails_from_env() -> list[Any]:
        """Load extra DeepAgentRails from AGENT_EXTRA_RAILS env var.

        Env format (semicolon-separated module paths):
            AGENT_EXTRA_RAILS=path.to.module1;path.to.module2

        Each module must expose a ``register_rails()`` function that returns
        a list of DeepAgentRail instances. Only honored under enterprise edition.
        """
        if not is_enterprise():
            return []
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
                        "[JiuWenSwarmDeepAdapter] Extra rail module '%s' has no register_rails(), skipping",
                        module_path,
                    )
                    continue
                rails = register_fn()
                if rails:
                    if not isinstance(rails, list):
                        rails = [rails]
                    extra_rails.extend(rails)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] Loaded %d rail(s) from '%s'",
                        len(rails),
                        module_path,
                    )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Failed to load extra rails from '%s': %s",
                    module_path,
                    exc,
                )
        return extra_rails

    @staticmethod
    def _load_extra_tools_from_env() -> list[Any]:
        """Load extra tools from AGENT_EXTRA_TOOLS env var.

        Env format (semicolon-separated module paths):
            AGENT_EXTRA_TOOLS=path.to.module1;path.to.module2

        Each module must expose a ``register_tools()`` function that returns
        a list of Tool instances. Only honored under enterprise edition.

        Results are cached per module for the process lifetime: session-scoped
        adapters rebuild per session, and extra tools register process-shared
        under their card id — without the cache, a factory returning fresh
        instances would register a new id on every build (registry leak,
        since stateless registrations are never torn down). Failures are not
        cached, so a transient failure retries on the next build.
        """
        if not is_enterprise():
            return []
        env_value = os.getenv("AGENT_EXTRA_TOOLS", "").strip()
        if not env_value:
            return []

        extra_tools: list[Any] = []
        for module_path in [p.strip() for p in env_value.split(";") if p.strip()]:
            try:
                # The lock spans cache lookup + import + factory call: concurrent
                # session builds must not race past a not-yet-written cache entry
                # and invoke register_tools() twice (double-checked locking cannot
                # close that gap when the factory runs between the checks).
                with JiuWenSwarmDeepAdapter._EXTRA_TOOLS_CACHE_LOCK:
                    cached = JiuWenSwarmDeepAdapter._EXTRA_TOOLS_CACHE.get(module_path)
                    if cached is None:
                        mod = importlib.import_module(module_path)
                        register_fn = getattr(mod, "register_tools", None)
                        if register_fn is None:
                            logger.warning(
                                "[JiuWenSwarmDeepAdapter] Extra tool module '%s' has no register_tools(), skipping",
                                module_path,
                            )
                            continue
                        tools = register_fn()
                        if tools and not isinstance(tools, list):
                            tools = [tools]
                        cached = tools or []
                        JiuWenSwarmDeepAdapter._EXTRA_TOOLS_CACHE[module_path] = cached
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] Loaded %d tool(s) from '%s'",
                            len(cached),
                            module_path,
                        )
                extra_tools.extend(cached)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Failed to load extra tools from '%s': %s",
                    module_path,
                    exc,
                )
        return extra_tools

    @staticmethod
    def _build_multimodal_image_rail(
        enable_image_multimodal: bool | None = None,
    ) -> MultimodalImageRail | None:
        """Build MultimodalImageRail."""
        try:
            multimodal_image_rail = MultimodalImageRail(
                enable_image_multimodal=enable_image_multimodal,
            )
            logger.info("[JiuWenSwarmDeepAdapter] MultimodalImageRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MultimodalImageRail create failed: %s", exc)
            multimodal_image_rail = None
        return multimodal_image_rail

    def _build_task_planning_rail(
        self, config: dict[str, Any] | None = None
    ) -> TaskPlanningRail | None:
        """Build TaskPlanningRail from ``react.task_planning`` config.

        ``config`` should be the react section (same shape as ``_config_cache``).
        """
        try:
            from openjiuwen.harness.rails.task_planning_rail import (
                resolve_task_planning_rail_kwargs,
            )

            react_cfg = config if config is not None else self._config_cache
            rail_kwargs = resolve_task_planning_rail_kwargs(react_cfg)
            task_planning_rail = ConcurrentSafeTaskPlanningRail(**rail_kwargs)
            logger.info(
                "[JiuWenSwarmDeepAdapter] TaskPlanningRail create success "
                "enable_progress_repeat=%s list_tool_call_interval=%s",
                task_planning_rail.enable_progress_repeat,
                task_planning_rail.list_tool_call_interval,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] TaskPlanningRail create failed: %s", exc)
            task_planning_rail = None
        return task_planning_rail

    @staticmethod
    def _build_subagent_rail() -> SubagentRail | None:
        """Build SubagentRail for subagent delegation."""
        try:
            subagent_rail = SubagentRail()
            logger.info("[JiuWenSwarmDeepAdapter] SubagentRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SubagentRail create failed: %s", exc)
            subagent_rail = None
        return subagent_rail

    def _build_structured_ask_user_rail(self) -> StructuredAskUserRail | None:
        """Build StructuredAskUserRail for agent mode clarification."""
        try:
            return StructuredAskUserRail(language=self._resolve_runtime_language())
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] StructuredAskUserRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_security_rail() -> SecurityRail | None:
        """Build SecurityPromptRail."""
        try:
            security_prompt_rail = SecurityRail()
            logger.info("[JiuWenSwarmDeepAdapter] SecurityPromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SecurityPromptRail create failed: %s", exc)
            security_prompt_rail = None
        return security_prompt_rail

    # 重索引延时（秒）：embedding 配置变更后，延后这段时间再跑一次全量重索引。
    # 配合 _schedule_memory_reindex 的 debounce，连续改多次只在最后一次后跑一次。
    _MEMORY_REINDEX_DELAY_SECONDS: float = 5.0
    _MEMORY_REINDEX_KEYS: set[tuple[str, str]] = set()
    _MEMORY_REINDEX_KEYS_LOCK = threading.Lock()

    @staticmethod
    def _embedding_config_fingerprint(config: dict | None) -> str:
        """计算 config.yaml embed 段的配置指纹，用于检测是否变化。

        归一化逻辑与 openjiuwen OpenAICompatibleEmbeddingProvider.normalize_base_url
        保持一致（去尾斜杠 + 去尾 /embeddings），使等价 endpoint 不会被误判为变化。
        api_key 取 sha256 截断，不明文比较。
        """
        embed = config.get("embed") if isinstance(config, dict) else None
        if not isinstance(embed, dict):
            return ""
        api_key = str(embed.get("embed_api_key") or "")
        base_url = str(embed.get("embed_base_url") or "").strip()
        # 与 provider 侧归一化一致：去尾 /embeddings 再去尾斜杠
        if base_url.endswith("/embeddings"):
            base_url = base_url.rsplit("/embeddings", 1)[0]
        while base_url.endswith("/"):
            base_url = base_url[:-1]
        model = str(embed.get("embed_model") or "")
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        return f"{model}:{base_url}:{api_key_hash}"

    def _schedule_memory_reindex(self) -> None:
        """延时后对记忆重新索引（debounce：多次触发只跑最后一次）。

        前提：调用前 MemoryRail 已按新 embedding 配置重建（_embedding_config 已刷新），
        且 openjiuwen lite 的 INDEX_CACHE 已清（aclose_memory_manager_cache）。
        这样延时到期时 init_memory_manager_async 会用新配置建新 manager + 新 provider。
        """
        workspace = os.path.normcase(os.path.abspath(self._workspace_dir or ""))
        reindex_key = (workspace, self._memory_embedding_fingerprint)
        with self._MEMORY_REINDEX_KEYS_LOCK:
            if reindex_key in self._MEMORY_REINDEX_KEYS:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] memory reindex coalesced: workspace=%s",
                    workspace,
                )
                return
            self._MEMORY_REINDEX_KEYS.add(reindex_key)
        if self._memory_reindex_task is not None and not self._memory_reindex_task.done():
            self._memory_reindex_task.cancel()
        self._memory_reindex_task = asyncio.create_task(
            self._do_memory_reindex(reindex_key)
        )

    async def _do_memory_reindex(self, reindex_key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._MEMORY_REINDEX_DELAY_SECONDS)
            rail = self._memory_rail
            if rail is None:
                return
            manager = await self._get_current_memory_manager()
            if manager is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] memory reindex skipped: manager unavailable"
                )
                return
            await manager.sync(reason="embed_config_changed", force=True)
            logger.info(
                "[JiuWenSwarmDeepAdapter] memory reindexed after embedding config change"
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[JiuWenSwarmDeepAdapter] memory reindex failed: %s", e)
        finally:
            with self._MEMORY_REINDEX_KEYS_LOCK:
                self._MEMORY_REINDEX_KEYS.discard(reindex_key)

    async def _get_current_memory_manager(self):
        """获取当前 MemoryRail 对应的 memory manager（必要时按新配置重建）。

        rail 重建后其 _embedding_config 已是最新值；但 manager 在 rail 首次 invoke
        前可能尚未 init，这里主动调 init_memory_manager_async 触发初始化/复用。
        """
        from openjiuwen.core.memory.lite.memory_tools import init_memory_manager_async

        rail = self._memory_rail
        if rail is None:
            return None
        embedding_config = getattr(rail, "_embedding_config", None)
        workspace = self._get_memory_workspace()
        agent_id = getattr(getattr(self._instance, "card", None), "id", None) or "default"
        try:
            return await init_memory_manager_async(
                workspace=workspace,
                agent_id=agent_id,
                embedding_config=embedding_config,
                sys_operation=self._sys_operation,
            )
        except Exception as e:
            logger.warning("[JiuWenSwarmDeepAdapter] init memory manager failed: %s", e)
            return None

    def _get_memory_workspace(self):
        """构造记忆用的 Workspace 对象（与 _make_deep_agent_config 中构造方式一致）。"""
        resolved_language = getattr(self, "_resolved_language", None) or "zh"
        return Workspace(root_path=self._workspace_dir or "./", language=resolved_language)

    def _build_memory_rail(self, mode: str) -> MemoryRail | None:
        try:
            config = (
                self._startup_config_base
                if isinstance(self._startup_config_base, dict)
                else get_config()
            )
            embed_config = get_embed_config()
            has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
            has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
            has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MemoryRail create failed: No available embedding config"
                )
            self._is_proactive_memory = is_proactive_memory(mode, config)
            memory_rail = MemoryRail(
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("model"),
                    base_url=embed_config.get("base_url"),
                    api_key=embed_config.get("api_key"),
                ),
                is_proactive=self._is_proactive_memory,
            )
            logger.info("[JiuWenSwarmDeepAdapter] MemoryRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MemoryRail create failed: %s", exc)
            memory_rail = None
        return memory_rail

    @staticmethod
    def _build_heartbeat_rail() -> HeartbeatRail | None:
        """Build HeartbeatRail."""
        try:
            heartbeat_rail = HeartbeatRail()
            logger.info("[JiuWenSwarmDeepAdapter] HeartbeatRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] HeartbeatRail create failed: %s", exc)
            heartbeat_rail = None
        return heartbeat_rail

    @staticmethod
    def _build_avatar_rail() -> Any | None:
        """Build AvatarPromptRail for digital avatar mode."""
        try:
            from jiuwenswarm.agents.harness.common.rails.avatar_rail import AvatarPromptRail

            rail = AvatarPromptRail()
            logger.info("[JiuWenSwarmDeepAdapter] AvatarPromptRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] AvatarPromptRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_llm_retry_rail(config_base: dict[str, Any] | None = None) -> NotifyingLLMRetryRail | None:
        try:
            config_base = config_base or get_config()
            guard_cfg = config_base.get("execution_guard", {}) if isinstance(config_base, dict) else {}
            retry_cfg = guard_cfg.get("llm_retry_rail", {}) if isinstance(guard_cfg, dict) else {}
            if retry_cfg.get("enabled", False) is not True:
                logger.info("[JiuWenSwarmDeepAdapter] LLMRetryRail disabled by config")
                return None
            rail = NotifyingLLMRetryRail(
                max_retries=retry_cfg.get("max_retries", 2),
                repeat_min_pattern_chars=retry_cfg.get("repeat_min_pattern_chars", 2),
                repeat_max_pattern_chars=retry_cfg.get("repeat_max_pattern_chars", 64),
                repeat_min_count=retry_cfg.get("repeat_min_count", 6),
                repeat_min_total_chars=retry_cfg.get("repeat_min_total_chars", 160),
                repeat_window_chars=retry_cfg.get("repeat_window_chars", 1024),
                single_char_repeat_count=retry_cfg.get("single_char_repeat_count", 100),
                retry_transient_invoke_errors=retry_cfg.get("retry_transient_invoke_errors", True),
                notify_user_on_retry=retry_cfg.get("notify_user_on_retry", True),
                notify_user_on_exhausted=retry_cfg.get("notify_user_on_exhausted", True),
            )
            logger.info("[JiuWenSwarmDeepAdapter] NotifyingLLMRetryRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] LLMRetryRail create failed: %s", exc)
            return None

    def _build_runtime_prompt_rail(self) -> RuntimePromptRail | None:
        """Build RuntimePromptRail for per-model-call time/channel/runtime injection."""
        try:
            default_channel = (
                "acp"
                if self._is_acp_tool_profile(self._instance_overrides)
                else self._resolve_prompt_channel()
            )
            rail = RuntimePromptRail(
                language=self._resolve_runtime_language(),
                channel=default_channel,
                agent_id=self._agent_id,
                service_id=self._service_id,
            )
            logger.info("[JiuWenSwarmDeepAdapter] RuntimePromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] RuntimePromptRail create failed: %s", exc)
            rail = None
        return rail

    def _build_skill_retrieval_prompt_rail(self) -> SkillRetrievalPromptRail | None:
        """Build lightweight agentic skill retrieval prompt guidance."""
        if not is_skill_retrieval_enabled():
            return None
        try:
            return SkillRetrievalPromptRail(
                manager=self._skill_manager,
                visible_skill_names=self._visible_skill_names_for_list_skill,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillRetrievalPromptRail create failed: %s", exc)
            return None

    def _build_a2a_outbound_toolkit_rail(self) -> A2AOutboundToolkitRail | None:
        if is_enterprise():
            return None
        return A2AOutboundToolkitRail(runtime_route=self._get_a2a_outbound_tool_route)

    def _get_a2a_outbound_tool_route(self) -> tuple[str, str]:
        """Return an adapter-owned route that survives DeepAgent task boundaries."""
        context_session = str(get_runtime_tool_session_id() or "").strip()
        if context_session:
            context_channel = str(get_runtime_tool_channel_id() or "").strip()
            return context_session, context_channel or "default"

        route = self._current_request_route
        session_id = str(route.get("session_id") or "").strip()
        channel_id = str(route.get("channel_id") or "").strip()
        if session_id:
            return session_id, channel_id or "default"

        runtime_context = self._runtime_cron_tool_context
        session_id = str(runtime_context.session_id or "").strip()
        channel_id = str(runtime_context.channel_id or "").strip()
        return session_id, channel_id or "default"

    def _build_progressive_tool_rail(
        self, config: dict[str, Any]
    ) -> ProgressiveToolRail | None:
        """Build progressive tool rail from react.tool_lazy_load config."""
        rail = build_progressive_tool_rail_from_config(
            config,
            language=self._resolve_runtime_language(),
            agent_id=self._tool_owner_id(),
            agent_card_id=self._tool_owner_id(),
            deepresearch_context_provider=self._get_deepresearch_tool_context,
        )
        if rail is not None:
            logger.info(
                "[JiuWenSwarmDeepAdapter] ProgressiveToolRail enabled (fixed schema mode)"
            )
        return rail

    @staticmethod
    def _build_disabled_tools_rail(
        config: dict[str, Any]
    ) -> DisabledToolsRail | None:
        """Build DisabledToolsRail to filter out disabled tools based on config.

        ``react.disabled_tools`` accepts two formats (same field, pick one):
          - YAML list:      ``disabled_tools: ["bash", "fork_agent"]``
          - Env var string: ``disabled_tools: ${DISABLED_TOOLS-defaults}``
            (set DISABLED_TOOLS=bash,fork_agent in .env)
        """
        try:
            disabled_list = resolve_string_or_list_config(config.get("disabled_tools"))
            if not disabled_list:
                return None
            rail = DisabledToolsRail(disabled_tools=disabled_list)
            logger.info(
                "[JiuWenSwarmDeepAdapter] DisabledToolsRail create success, "
                "disabled_tools: %s",
                disabled_list,
            )
            return rail
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] DisabledToolsRail create failed: %s", exc,
                exc_info=True,
            )
            # disabled_tools is a security boundary. Refuse to initialize or
            # hot-reload with the blacklist silently disabled (fail closed).
            raise

    def _get_deepresearch_tool_context(self) -> dict[str, str]:
        """Return the adapter-owned route that survives runner task boundaries.

        Prefers the per-session-adapter ``_current_request_route`` snapshot
        (captured from request.* at chat request entry) so deepresearch_stream
        progress pushes bind to the originating session/request even when
        concurrent requests leak module-level ``_CRON_TOOL_*`` bindings into
        background tool tasks. Falls back to ``_runtime_cron_tool_context``
        for cron-triggered paths that don't populate the snapshot.
        """
        route = self._current_request_route
        if route.get("session_id"):
            scope = RuntimeScopeKey.from_adapter(
                self, session_id=route["session_id"]
            )
            return {
                "request_id": str(route.get("request_id") or ""),
                "channel_id": str(route.get("channel_id") or ""),
                "session_id": scope.session_id,
                "service_id": scope.service_id,
                "agent_id": scope.agent_id,
                "workspace_key": scope.workspace_key,
                "output_dir": self._deepresearch_artifact_output_dir(
                    route.get("output_dir")
                ),
            }
        context = self._runtime_cron_tool_context
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        scope = RuntimeScopeKey.from_adapter(self, session_id=context.session_id)
        return {
            "request_id": str(metadata.get("request_id") or ""),
            "channel_id": str(context.channel_id or ""),
            "session_id": scope.session_id,
            "service_id": scope.service_id,
            "agent_id": scope.agent_id,
            "workspace_key": scope.workspace_key,
            "output_dir": self._deepresearch_artifact_output_dir(
                metadata.get("project_dir")
            ),
        }

    def _deepresearch_artifact_output_dir(
        self, request_workspace: str | None = None
    ) -> str:
        """Return the current session workspace, or the legacy artifact root."""
        normalized_request_workspace = (
            request_workspace.strip()
            if isinstance(request_workspace, str)
            else ""
        )
        session_workspace = normalized_request_workspace or str(
            getattr(self, "_project_dir", None) or ""
        ).strip()
        if session_workspace:
            return str(Path(session_workspace).expanduser().resolve())
        workspace = Path(
            getattr(self, "_workspace_dir", None) or get_agent_workspace_dir()
        ).expanduser().resolve()
        return str(workspace / "projects")

    async def refresh_skill_rails(self) -> None:
        """轻量刷新 skill 相关 rail，避免全量重建 agent 实例.

        uninstall 后调用：SkillUseRail.reload_skills() 重新扫描 skills_dir，
        增量移除已删除 skill 的缓存；同步更新 disabled_skills。
        """
        new_disabled = set(self._skill_manager.list_execution_disabled_skills())
        if self._skill_rail is not None:
            if self._skill_rail.disabled_skills != new_disabled:
                self._skill_rail.disabled_skills = new_disabled
            try:
                await self._skill_rail.reload_skills()
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] skill rail reload failed: %s", exc)
        if self._skill_evolution_rail is not None:
            sync_evolution_disabled_skills(self._skill_evolution_rail, new_disabled)

    def _build_symphony_orchestration_rail(
        self,
    ) -> SymphonyOrchestrationRail | None:
        """Build dynamic Symphony orchestration prompt guidance."""
        try:
            return SymphonyOrchestrationRail(
                config_base=lambda: self._config_base_cache,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SymphonyOrchestrationRail create failed: %s",
                exc,
            )
            return None

    def _instantiate_rails(
        self,
        rail_infos: list["_RailBuildInfo"],
        config_base: dict[str, Any],
    ) -> list[Any]:
        """Build each declared rail in order, then attach the two standing ones.

        Shared by the agent and code rail sets, which differ only in what they
        declare. Rail construction is the bulk of ``create_instance``, so each
        one is timed separately and the breakdown is logged slowest-first once
        the total crosses :data:`_SLOW_RAIL_BUILD_MS` — an aggregate over a
        dozen-plus rails says nothing about which to look at.

        Args:
            rail_infos: Rails to build, in the order they should be attached.
            config_base: Full config snapshot, used for the user hook rail.

        Returns:
            The successfully built rails. A rail whose builder returns None is
            skipped with a warning rather than failing the whole set.
        """
        log_prefix = f"[{type(self).__name__}]"
        stage_timer = StageTimer()
        rails_list = []
        for info in rail_infos:
            rail_instance = info.build_func(**info.params)
            stage_timer.mark(info.attr_name.lstrip("_"))
            if rail_instance is not None:
                setattr(self, info.attr_name, rail_instance)
                rails_list.append(rail_instance)
            else:
                logger.warning("%s Rail %s build returned None", log_prefix, info.attr_name)

        # 从环境变量加载额外 Rails（非侵入式扩展；仅企业版）
        extra_rails = self._load_extra_rails_from_env()
        if extra_rails:
            rails_list.extend(extra_rails)
            logger.info(
                "%s Extra rails loaded from env: %s",
                log_prefix,
                [type(r).__name__ for r in extra_rails],
            )
        stage_timer.mark("extra_rails")

        # 用户配置的 hooks（UserHookRail）
        try:
            hooks_config = load_hooks_config(config_base)
            if hooks_config.events:
                rails_list.append(UserHookRail(hooks_config))
                logger.info(
                    "%s UserHookRail loaded with %d event types",
                    log_prefix,
                    len(hooks_config.events),
                )
        except Exception as exc:
            logger.warning("%s Failed to load UserHookRail: %s", log_prefix, exc)
        stage_timer.mark("user_hook_rail")

        # Observability rail: opens an agent-layer span (agent.<name>.task_iteration.<n>
        # for task-loop runs, or agent.<name>.invoke for single-round) under the root
        # run span per iteration/round. It is the only thing that creates the
        # task_iteration / invoke spans that llm.call + tool.* nest under. It
        # self-disables (before_* returns early when get_team_span() is None), so
        # attaching it unconditionally is safe and also adapts to runtime
        # enable/disable of agent_observability without rebuilding the agent.
        try:
            from openjiuwen.agent_teams.observability.rail import ObservabilityRail
            from jiuwenswarm.agents.harness.agent_observability import (
                AgentTraceBindingRail,
            )

            rails_list.append(AgentTraceBindingRail())
            rails_list.append(ObservabilityRail())
        except Exception as exc:
            logger.warning("%s Failed to attach ObservabilityRail: %s", log_prefix, exc)
        stage_timer.mark("observability_rail")

        # Bind tenant checkpointer after rails exist (set_checkpoint runs earlier).
        self._bind_checkpointer_to_rails()

        total_ms = stage_timer.total_ms()
        log_rail_build = _stage_breakdown_logger(total_ms, _SLOW_RAIL_BUILD_MS)
        log_rail_build(
            "[AgentServer] agent rails built: adapter=%s count=%d total_ms=%.1f %s",
            type(self).__name__,
            len(rails_list),
            total_ms,
            stage_timer.render(slowest_first=True),
        )
        return rails_list

    def _build_agent_rails(
        self, config: dict[str, Any], config_base: dict[str, Any], *, mode: str = "agent"
    ) -> list[Any]:
        """Build DeepAgent rails consistently for cold start and hot reload."""
        rail_infos = [
            _RailBuildInfo("_request_summary_rail", self._build_request_summary_rail),
            _RailBuildInfo("_runtime_prompt_rail", self._build_runtime_prompt_rail),
            _RailBuildInfo("_response_prompt_rail", self._build_response_prompt_rail),
            _RailBuildInfo(
                "_multimodal_image_rail",
                self._build_multimodal_image_rail,
                {
                    "enable_image_multimodal": self._resolve_enable_read_image_multimodal(config),
                },
            ),
            _RailBuildInfo(
                "_deepresearch_execution_rail",
                self._build_deepresearch_execution_rail,
            ),
            _RailBuildInfo("_stream_event_rail", self._build_stream_event_rail),
            # an example to use extension rail (enterprise)
            # _RailBuildInfo("_extension_config_debug_rail", self._build_extension_config_debug_rail),
            _RailBuildInfo(
                "_task_planning_rail",
                self._build_task_planning_rail,
                {"config": config},
            ),
            _RailBuildInfo("_security_rail", self._build_security_rail),
            _RailBuildInfo("_heartbeat_rail", self._build_heartbeat_rail),
            _RailBuildInfo("_context_overflow_recovery_rail", self._build_context_overflow_recovery_rail),
            _RailBuildInfo(
                "_llm_retry_rail",
                self._build_llm_retry_rail,
                {"config_base": config_base},
            ),
            _RailBuildInfo("_avatar_rail", self._build_avatar_rail),
            _RailBuildInfo("_subagent_rail", self._build_subagent_rail),
            _RailBuildInfo(
                "_permission_rail",
                self._build_permission_rail_for_agent,
                {
                    "config": config_base,
                    "llm": self._model,
                    "model_name": config_base.get("models", {})
                    .get("default", {})
                    .get("model_client_config", {})
                    .get("model_name", "gpt-4"),
                },
            ),
            # 须在 _permission_rail 之后构建：engine 复用权限 Rail 实例的引擎。
            _RailBuildInfo("_skill_authorization_rail", self._build_skill_authorization_rail),
            _RailBuildInfo(
                "_context_processor_rail",
                _build_context_processor_rail,
                {"config": self._config_cache},
            ),
        ]

        # SkillEvolutionRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销
        # 智能模式下关闭自演进，plan 模式下按配置启用

        # MemoryRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销

        if self._filesystem_rail_enabled_for_profile():
            rail_infos.insert(1, _RailBuildInfo("_filesystem_rail", self._build_filesystem_rail))
        else:
            self._filesystem_rail = None
        rail_infos.insert(
            2 if self._filesystem_rail_enabled_for_profile() else 1,
            _RailBuildInfo(
                "_skill_rail",
                self._build_skill_rail,
                {"config": config, "include_tools": self._skill_include_tools_for_profile()},
            ),
        )
        rail_infos.insert(
            3 if self._filesystem_rail_enabled_for_profile() else 2,
            _RailBuildInfo("_skill_retrieval_prompt_rail", self._build_skill_retrieval_prompt_rail),
        )
        rail_infos.insert(
            4 if self._filesystem_rail_enabled_for_profile() else 3,
            _RailBuildInfo(
                "_a2a_outbound_toolkit_rail",
                self._build_a2a_outbound_toolkit_rail,
            ),
        )
        rail_infos.insert(
            5 if self._filesystem_rail_enabled_for_profile() else 4,
            _RailBuildInfo(
                "_symphony_orchestration_rail",
                self._build_symphony_orchestration_rail,
            ),
        )
        if isinstance(mode, str) and mode.startswith("agent"):
            rail_infos.append(_RailBuildInfo("_ask_user_rail", self._build_structured_ask_user_rail))
        # TaskExecutionRail 仅在冷启动时创建/注册，不参与热重载重建。
        # 切换 enable_task_execution_rail 需重启实例生效（by design）。
        if self._task_execution_rail_enabled():
            rail_infos.append(
                _RailBuildInfo("_task_execution_rail", self._build_task_execution_rail)
            )
        else:
            self._task_execution_rail = None

        # SkillTurboPromptRail: 注入 skill_acceleration_exec 使用指南
        # 仅在 skill_turbo.enabled = true 时生效
        rail_infos.append(
            _RailBuildInfo(
                "_skill_turbo_prompt_rail",
                self._build_skill_turbo_prompt_rail,
            )
        )

        # SkillProtocolPromptRail: 注入技能执行规范（skill_protocol 段），仅 agent 模式挂载。
        if isinstance(mode, str) and mode.startswith("agent"):
            rail_infos.append(
                _RailBuildInfo(
                    "_skill_protocol_prompt_rail",
                    self._build_skill_protocol_prompt_rail,
                )
            )
        else:
            self._skill_protocol_prompt_rail = None

        rail_infos.insert(
            0,
            _RailBuildInfo(
                "_skill_credential_injection_rail",
                self._build_skill_credential_injection_rail,
                {"config": config},
            ),
        )
        rail_infos.insert(
            1,
            _RailBuildInfo("_skill_active_state_rail", self._build_skill_active_state_rail),
        )

        rail_infos.append(
            _RailBuildInfo(
                "_progressive_tool_rail",
                self._build_progressive_tool_rail,
                {"config": config},
            )
        )

        # 统一工具开关（黑名单）：在其它工具注册后注销被禁用的工具。
        # priority=1 确保最后执行，此时工具已全部注册。
        rail_infos.append(
            _RailBuildInfo(
                "_disabled_tools_rail",
                self._build_disabled_tools_rail,
                {"config": config},
            )
        )

        return self._instantiate_rails(rail_infos, config_base)

    @staticmethod
    def _resolve_enable_task_loop(
        config: dict[str, Any], config_base: dict[str, Any] | None
    ) -> bool:
        """Resolve enable_task_loop considering evolution rail requirements.

        SkillCreateRail and review-trigger SkillEvolutionRail follow-ups require
        task-loop mode (enable_task_loop=True) because they use
        AFTER_TASK_ITERATION events and enqueue_follow_up().
        When skill_create=True or review_trigger=True, we force enable_task_loop=True
        regardless of user config.

        Args:
            config: The react config section.
            config_base: The full config base (contains evolution.skill_create).

        Returns:
            True if task-loop should be enabled, False otherwise.
        """
        config_base = config_base or get_config()
        skill_create_enabled = get_skill_create_enabled(config_base)
        evolution_review_trigger_enabled = get_evolution_review_trigger_enabled(config_base)
        configured_value = config.get("enable_task_loop", True)

        if skill_create_enabled:
            if not configured_value:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skill_create=True requires enable_task_loop=True; "
                    "overriding user config (enable_task_loop=%s -> True)",
                    configured_value,
                )
            return True
        if evolution_review_trigger_enabled:
            if not configured_value:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] evolution.review_trigger=True requires "
                    "enable_task_loop=True; overriding user config (enable_task_loop=%s -> True)",
                    configured_value,
                )
            return True
        return configured_value

    def _make_deep_agent_config(
        self,
        *,
        model: Model,
        config: dict[str, Any],
        config_base: dict[str, Any] | None = None,
        agent_card: AgentCard,
        tool_cards: list[Any],
        rails: list[Any] | None = None,
    ) -> DeepAgentConfig:
        """与 create_deep_agent() 中 DeepAgentConfig 构造保持一致."""
        resolved_language = self._resolve_runtime_language()
        config_base = config_base or get_config()
        workspace_obj = Workspace(root_path=self._workspace_dir or "./", language=resolved_language)
        normalized_tool_cards = [
            tool.card if hasattr(tool, "card") else tool for tool in (tool_cards or [])
        ]
        configured_subagents, should_add_general_agent = self._build_configured_subagents(model, config, config_base)
        # Hot reload uses configure(); factory inject does not run again.
        configured_subagents = _inject_general_purpose_subagent(
            configured_subagents,
            add_general_purpose_agent=should_add_general_agent,
            resolved_language=resolved_language,
            rails=rails,
            system_prompt=build_agent_identity_prompt(
                language=self._resolve_prompt_language(),
            ),
            tools=normalized_tool_cards,
            mcps=None,
            model=model,
            skills=None,
            workspace=workspace_obj,
            sys_operation=self._sys_operation,
        ) or None
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            tool_owner_id=self._tool_owner_id(),
            system_prompt=build_agent_identity_prompt(
                language=self._resolve_prompt_language(),
            ),
            context_engine_config=_deep_agent_context_engine_config(config),
            kv_cache_affinity_config=_deep_agent_kv_cache_affinity_config(config, model),
            enable_task_loop=self._resolve_enable_task_loop(config, config_base),
            max_iterations=config.get("max_iterations", 15),
            subagents=configured_subagents,
            add_general_purpose_agent=should_add_general_agent,
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
            enable_read_image_multimodal=self._resolve_enable_read_image_multimodal(config),
            completion_timeout=config.get("completion_timeout", 21600.0),
        )

    def _update_permission_rail(
        self,
        config_base: dict[str, Any] | None,
        *,
        session_id: str | None = None,
    ) -> None:
        """原地更新已有 PermissionRail 配置，或在首次启用时新建。

        企业版优先使用 Agent template_ref.permissions 模板 body（与模型槽位同级）。
        """
        permission_config = self._resolve_permission_config_for_agent(
            session_id=session_id
        )
        if self._permission_rail is not None:
            self._permission_rail.update_config(permission_config)
            logger.info("[JiuWenSwarmDeepAdapter] _permission_rail config hot-updated")
        elif permission_config.get("enabled", False):
            self._permission_rail = build_permission_rail(
                config=config_base or {},
                llm=self._model,
                model_name=config_base.get("models", {})
                .get("default", {})
                .get("model_client_config", {})
                .get("model_name", "gpt-4")
                if isinstance(config_base, dict)
                else "gpt-4",
                permission_config=self._agent_permissions_body,
            )
            if self._permission_rail is not None:
                logger.info("[JiuWenSwarmDeepAdapter] _permission_rail newly created on hot-reload")
                # 冷启动时权限系统未启用则授权 Rail 的 engine 为 None（fail-closed）；
                # 权限 Rail 热更新建后同步引擎，恢复 DENY 预判能力。
                # agent-core 的 SkillAuthorizationRail 未提供引擎写入口，
                # 用 setattr 做原地同步，避免重建 rail 丢失与运行中 Agent 的绑定。
                if self._skill_authorization_rail is not None:
                    setattr(
                        self._skill_authorization_rail,
                        "_engine",
                        getattr(self._permission_rail, "_engine", None),
                    )

    def _resolve_permission_config_for_agent(
        self,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """解析本 Agent 生效 permissions：企业模板 body 优先，否则 yaml/DB 回落。

        模板 body 仅作为本 Agent 基线，不写入进程级 permissions 缓存；
        企业版在提供 ``session_id`` 时仍叠加会话 overlay。
        """
        template_body = self._agent_permissions_body
        if template_body is None:
            template_body = resolve_permissions_body_from_enterprise(
                self._enterprise_config
            )
            if template_body is not None:
                self._agent_permissions_body = template_body
        if template_body is not None:
            if is_enterprise():
                return merge_session_permissions_overlay(
                    template_body, session_id=session_id
                )
            return copy.deepcopy(template_body)
        if is_enterprise() and session_id is None:
            return get_base_permissions_config()
        return get_effective_permissions_config(session_id=session_id)

    def _build_permission_rail_for_agent(
        self,
        config: dict[str, Any] | None = None,
        llm: Any = None,
        model_name: str | None = None,
    ) -> Any | None:
        """冷启动构建 permission rail：注入 Agent 级模板 body。"""
        return build_permission_rail(
            config=config or {},
            llm=llm if llm is not None else self._model,
            model_name=model_name,
            permission_config=self._agent_permissions_body,
        )

    def _bind_agent_permissions_base(self) -> Any:
        """将 Agent 模板 permissions body 绑定到当前 Task（供 snapshot/生效读路径）。"""
        return setup_permissions_agent_base(self._agent_permissions_body)

    def _get_current_agent_rails(
        self, config: dict[str, Any], config_base: dict[str, Any] | None = None
    ) -> tuple[list[Any], list[Any]]:
        """Return rail replacements and rails to retire after configure.

        SkillUseRail, ContextEngineeringRail, and MemoryRail are rebuilt on config reload.
        All other rails read language dynamically from system_prompt_builder.language
        and are updated in-place where needed — they are NOT passed to configure()
        so their existing registered state is preserved without an uninit/init cycle.
        """
        rails_to_unregister: list[Any] = []

        # Apply in-place updates to skill_evolution_rail (no re-init needed).
        if self._skill_evolution_rail is not None:
            self._skill_evolution_rail.update_llm(self._model, self._default_model_name)
            evolution_triggers = get_passive_skill_evolution_triggers(config)
            _set_skill_evolution_triggers(
                self._skill_evolution_rail,
                review_trigger=evolution_triggers["review_trigger"],
                signal_trigger=evolution_triggers["signal_trigger"],
            )

        # Reuse existing SkillUseRail to preserve dynamically loaded skills
        # from activate_package() / load_harness_config().  When agentic
        # retrieval is enabled, _resolve_skill_mode() forces AUTO_LIST; the
        # SkillRetrievalPromptRail then hides list_skill and the native skills
        # prompt while keeping skill_tool/read_file available.
        if self._skill_rail is None:
            self._skill_rail = self._build_skill_rail(
                config,
                include_tools=self._skill_include_tools_for_profile(),
            )
        else:
            # Update existing rail's skill_mode if changed.
            new_skill_mode = self._resolve_skill_mode(config)
            if self._skill_rail.skill_mode != new_skill_mode:
                self._skill_rail.skill_mode = new_skill_mode
            # Update disabled_skills.
            new_disabled = set(self._skill_manager.list_execution_disabled_skills())
            if self._skill_rail.disabled_skills != new_disabled:
                self._skill_rail.disabled_skills = new_disabled

        if (
            not self._filesystem_rail_enabled_for_profile()
            and self._filesystem_rail is not None
        ):
            rails_to_unregister.append(self._filesystem_rail)

        self._update_permission_rail(config_base)

        skill_credential_rail_newly_created = False
        incoming_skill_envs = config.get("skill_envs", {})
        current_skill_envs = (
            self._skill_credential_injection_rail.get_skill_envs()
            if self._skill_credential_injection_rail is not None
            else None
        )
        new_skill_envs = coalesce_skill_envs(incoming_skill_envs, current_skill_envs)
        if new_skill_envs is not incoming_skill_envs and new_skill_envs:
            logger.info(
                "[JiuWenSwarmDeepAdapter] keeping skill_envs skills=[%s]; "
                "empty YAML/overlay would wipe catalog credentials",
                ", ".join(new_skill_envs.keys()),
            )
            config["skill_envs"] = new_skill_envs
            if isinstance(config_base, dict):
                react_base = config_base.get("react")
                if isinstance(react_base, dict):
                    react_base["skill_envs"] = new_skill_envs
        if self._skill_credential_injection_rail is not None:
            self._skill_credential_injection_rail.update_skill_envs(new_skill_envs)
            logger.info(
                "[JiuWenSwarmDeepAdapter] skill_envs hot-updated for SkillCredentialInjectionRail"
            )
        else:
            self._skill_credential_injection_rail = self._build_skill_credential_injection_rail(
                config
            )
            if self._skill_credential_injection_rail is not None:
                skill_credential_rail_newly_created = True

        if self._skill_active_state_rail is None:
            self._skill_active_state_rail = self._build_skill_active_state_rail()

        progressive_tool_rail = None
        if "tool_lazy_load" in config:
            old_progressive_tool_rail = self._progressive_tool_rail
            progressive_tool_rail = self._build_progressive_tool_rail(config)
            if progressive_tool_rail is not None:
                self._progressive_tool_rail = progressive_tool_rail
                # Rebuild wipes rail-local OfficeClaw pins; re-sync from the
                # still-active request registration so concurrent schedule
                # creates do not invoke unbound / foreign MCP tools.
                active_mcp = self._active_office_claw_mcp
                if active_mcp is not None:
                    self._sync_office_claw_allowlist_to_progressive_rail(
                        active_mcp.tool_ids,
                        delivery_thread_id=self._office_claw_thread_id_from_tools(
                            list(active_mcp.tool_instances)
                        ),
                        invocation_id=active_mcp.invocation_id or None,
                    )
            elif old_progressive_tool_rail is not None:
                rails_to_unregister.append(old_progressive_tool_rail)

        # 统一工具开关热更新：重建式（与 ProgressiveToolRail 一致）。
        # 旧 rail uninit 时回滚它注销的工具（重新注册），新 rail init 再按新名单注销。
        disabled_tools_rail = None
        disabled_tools_configured = "disabled_tools" in config
        if disabled_tools_configured:
            old_disabled_tools_rail = self._disabled_tools_rail
            disabled_tools_rail = self._build_disabled_tools_rail(config)
            if old_disabled_tools_rail is not None:
                rails_to_unregister.append(old_disabled_tools_rail)
            self._disabled_tools_rail = disabled_tools_rail

        rails_list = []
        if self._skill_rail is not None:
            rails_list.append(self._skill_rail)
        if self._context_assemble_rail is not None:
            rails_list.append(self._context_assemble_rail)
        if self._context_processor_rail is not None:
            rails_list.append(self._context_processor_rail)
        if self._memory_rail is not None:
            rails_list.append(self._memory_rail)
        if self._avatar_rail is not None:
            rails_list.append(self._avatar_rail)
        if self._permission_rail is not None:
            rails_list.append(self._permission_rail)
        if progressive_tool_rail is not None:
            rails_list.append(progressive_tool_rail)
        if disabled_tools_rail is not None:
            rails_list.append(disabled_tools_rail)
        elif not disabled_tools_configured and self._disabled_tools_rail is not None:
            rails_list.append(self._disabled_tools_rail)
        if skill_credential_rail_newly_created and self._skill_credential_injection_rail is not None:
            rails_list.append(self._skill_credential_injection_rail)
        if self._skill_active_state_rail is not None:
            rails_list.append(self._skill_active_state_rail)
        return rails_list, rails_to_unregister

    def _runtime_agent_scope_id(self) -> str:
        """Return AgentCard / tool-owner base id for this adapter.

        Community keeps the stable ``_AGENT_CARD_ID`` so checkpointer keys stay
        constant. Enterprise scopes by service/agent to avoid
        cross-tenant card/tool collisions.
        """
        if not is_enterprise():
            return _AGENT_CARD_ID
        agent_id = str(self._agent_id or "").strip()
        service_id = str(self._service_id or "").strip()
        if service_id and agent_id:
            return f"{service_id}_{agent_id}"
        return agent_id or _AGENT_CARD_ID

    def _tool_owner_id(self) -> str:
        """Return the owner id qualifying this adapter's tool registrations.

        ``AgentCard.id`` is a persistence identity shared by every adapter, so
        using it as the tool owner made concurrent sessions register under the
        same ids and silently overwrite each other's instances. Scoping
        ownership by session keeps them apart and lets
        ``AbilityManager.teardown_tools`` reclaim exactly this adapter's share
        on cleanup, while checkpointer keys keep using the stable card id.

        Session ids come from clients, so the two kinds of owner live in
        separate namespaces rather than sharing one flat suffix: without the
        ``s`` marker a session literally named "root" would produce the root
        adapter's owner id and the two would overwrite each other's tools, which
        is the very collision this id exists to prevent.

        Returns:
            Owner id for this adapter: ``"<card id>_s_<session>"`` for a
            session-scoped adapter, ``"<card id>_root"`` for the root adapter.
        """
        card_id = self._runtime_agent_scope_id()
        if self._is_session_scoped_adapter:
            return f"{card_id}_s_{self._session_adapter_key(self._parent_session_id)}"
        return f"{card_id}_root"

    @staticmethod
    def _register_shared_tool(tool: Any) -> Any:
        """Declare a tool instance shared across adapters, then register it.

        Shared here means one instance serves every adapter in the process: the
        bare id is kept and a repeated registration is an idempotent no-op, so
        the first registrant wins instead of adapters racing to replace each
        other. Two kinds of tool take this path — module-level ``@tool``
        singletons, and toolkits whose backing manager is deliberately
        process-wide (skills, symphony), where per-session instances would split
        state that is meant to be shared.

        Args:
            tool: Tool instance to share process-wide.

        Returns:
            The process-global registered tool instance (may be an earlier one).
        """
        mark_stateless([tool])
        return register_tool(tool, None)

    @staticmethod
    def _register_agent_owned_tool(tool: Any, owner_id: str) -> Any:
        """Register a tool instance owned exclusively by this adapter's agent.

        ``_get_tool_cards`` runs before ``create_deep_agent``, so there is no
        AbilityManager to route through yet; :func:`register_tool` applies the
        contract it would have applied, which keeps two things true: rebuilding
        an agent rebinds the id to the fresh instance instead of failing as a
        duplicate, and the registration stays reclaimable by
        ``AbilityManager.teardown_tools`` on cleanup.

        A card that already declares itself shared wins over the call site: the
        two must agree or teardown would look for an id that was never
        registered, so the declaration on the card is the one that counts.

        Args:
            tool: Per-agent tool instance to register.
            owner_id: Owner id that this adapter's agent registers under.

        Returns:
            The registered tool instance.
        """
        if getattr(tool.card, "stateless", False):
            logger.debug(
                "[JiuWenSwarmDeepAdapter] tool %s is declared shared; registering it as shared"
                " despite the agent-owned call site",
                tool.card.name,
            )
        return register_tool(tool, owner_id)

    def _register_deepresearch_tool_cards(self, tool_cards: list[Any]) -> None:
        """Register the formal DeepResearch surface as process-shared tools."""
        registered_cards = [
            self._register_shared_tool(tool).card
            for tool in get_deepresearch_tools()
        ]
        tool_cards.extend(registered_cards)

    def _append_extra_tool_cards(self, tool_cards: list[Any]) -> None:
        """Append extra tools loaded from AGENT_EXTRA_TOOLS to ``tool_cards``.

        Non-invasive tool extension mirroring ``AGENT_EXTRA_RAILS`` (see
        ``_load_extra_tools_from_env``): enterprise only, semicolon-separated
        module paths, each exposing ``register_tools()``. Every tool is
        registered process-shared (``_register_shared_tool``) and its card
        joins ``tool_cards``, so it flows through the same config pipeline as
        builtin tools (model visibility, ``disabled_tools`` filtering).

        Isolation: a tool without a card name, a name conflicting with an
        already-collected card, or a failing registration is logged and
        skipped — one bad extension tool never blocks the agent build nor
        the remaining tools (same convention as the vision/audio/extension
        registration blocks above).

        Args:
            tool_cards: Cards collected so far by ``_get_tool_cards``; extra
                cards are appended in place.
        """
        existing_names = {card.name for card in tool_cards}
        for tool in self._load_extra_tools_from_env():
            tname = getattr(getattr(tool, "card", None), "name", "")
            if not tname:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] extra tool without card.name, skip"
                )
                continue
            if tname in existing_names:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] extra tool '%s' conflicts with existing tool, skip",
                    tname,
                )
                continue
            try:
                registered = self._register_shared_tool(tool)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] extra tool '%s' registration failed: %s",
                    tname,
                    exc,
                )
                continue
            tool_cards.append(registered.card)
            existing_names.add(tname)

    async def _get_tool_cards(self, agent_id: str):
        """Get tool cards."""
        tool_cards = []

        for wtool in [wiki_ingest, wiki_query, wiki_lint]:
            registered = self._register_shared_tool(wtool)
            tool_cards.append(registered.card)

        from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
            get_agent_cache_registry,
        )

        content_cache = await get_agent_cache_registry().get_cache(agent_id)
        for tool_instance in build_jiuwen_harness_named_web_tools(
            agent_id=agent_id,
            language=self._resolve_runtime_language(),
            cache=content_cache,
        ):
            registered = self._register_agent_owned_tool(tool_instance, agent_id)
            tool_cards.append(registered.card)

        self._vision_tools = []
        self._vision_tools_registered = False
        if self._vision_model_config is not None:
            try:
                for tool in create_vision_tools(
                    language=self._resolve_runtime_language(),
                    vision_model_config=self._vision_model_config,
                    agent_id=agent_id,
                ):
                    registered = self._register_agent_owned_tool(tool, agent_id)
                    tool_cards.append(registered.card)
                    self._vision_tools.append(registered)
                self._vision_tools_registered = bool(self._vision_tools)
            except Exception as exc:
                self._vision_tools = []
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] vision tools registration failed: %s",
                    exc,
                )

        self._audio_tools = []
        self._audio_tools_registered = False
        try:
            self._audio_tools = []
            for tool in self._iter_runtime_audio_tools(agent_id):
                registered = self._register_agent_owned_tool(tool, agent_id)
                tool_cards.append(registered.card)
                self._audio_tools.append(registered)
            self._audio_tools_registered = bool(self._audio_tools)
        except Exception as exc:
            self._audio_tools = []
            logger.warning(
                "[JiuWenSwarmDeepAdapter] audio tools registration failed: %s",
                exc,
            )

        self._video_tool_registered = False
        if self._video_model_config:
            try:
                registered = self._register_shared_tool(video_understanding)
                tool_cards.append(registered.card)
                self._video_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] video tool registration failed: %s",
                    exc,
                )

        # generate_image tool: use dedicated image_gen model config
        self._image_gen_tool_registered = False
        if self._image_gen_model_config:
            try:
                registered = self._register_shared_tool(generate_image)
                tool_cards.append(registered.card)
                self._image_gen_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] generate_image tool registration failed: %s",
                    exc,
                )

        # 小艺手机端工具：由 channels.xiaoyi.phone_tools_enabled 控制
        config_base = get_config()
        xiaoyi_phone_tools_enabled = (
            config_base.get("channels", {}).get("xiaoyi", {}).get("phone_tools_enabled", False)
        )
        if xiaoyi_phone_tools_enabled and not self._xiaoyi_phone_tools_registered:
            _xiaoyi_tools = [
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
                image_reading,
                xiaoyi_gui_agent,
            ]
            try:
                for xt in _xiaoyi_tools:
                    registered = self._register_shared_tool(xt)
                    tool_cards.append(registered.card)
                self._xiaoyi_phone_tools_registered = True
                logger.info(
                    "[JiuWenSwarmDeepAdapter] %d xiaoyi phone tools registered", len(_xiaoyi_tools)
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] xiaoyi phone tools registration failed: %s", exc
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
                registered = self._register_shared_tool(tool)
                tool_cards.append(registered.card)
                skill_tool_names.append(registered.card.name)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillToolkit registered: tools=%s",
                skill_tool_names,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] skill tools registration failed: %s", exc)

        if is_skill_retrieval_enabled():
            try:
                self._skill_retrieval_tools = self._create_skill_retrieval_tools()
                skill_retrieval_tool_names: list[str] = []
                for tool in self._skill_retrieval_tools:
                    registered = self._register_shared_tool(tool)
                    tool_cards.append(registered.card)
                    skill_retrieval_tool_names.append(registered.card.name)
                self._skill_retrieval_tools_registered = bool(self._skill_retrieval_tools)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit registered: tools=%s",
                    skill_retrieval_tool_names,
                )
            except Exception as exc:
                self._skill_retrieval_tools = []
                self._skill_retrieval_tools_registered = False
                logger.warning("[JiuWenSwarmDeepAdapter] skill retrieval tools registration failed: %s", exc)
        else:
            self._skill_retrieval_tools = []
            self._skill_retrieval_tools_registered = False
            logger.info("[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit skipped: disabled")

        try:
            symphony_toolkit = SymphonyToolkit()
            symphony_tool_names: list[str] = []
            symphony_tools = symphony_toolkit.get_tools(config_base)
            for tool in symphony_tools:
                registered = self._register_shared_tool(tool)
                tool_cards.append(registered.card)
                symphony_tool_names.append(registered.card.name)
            self._symphony_tools = list(symphony_tools)
            self._symphony_tools_registered = bool(symphony_tools)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SymphonyToolkit registered: tools=%s",
                symphony_tool_names,
            )
        except Exception as exc:
            self._symphony_tools = []
            self._symphony_tools_registered = False
            logger.warning(
                "[JiuWenSwarmDeepAdapter] orchestration tools registration failed: %s",
                exc,
            )

        # acp_chat: forward prompts to external stdio ACP agents (see acp_agents in config.yaml)
        try:
            acp_cfg = get_config().get("acp_agents")
            if isinstance(acp_cfg, dict) and acp_cfg:
                registered = self._register_shared_tool(acp_chat)
                tool_cards.append(registered.card)
                logger.info("[JiuWenSwarmDeepAdapter] acp_chat tool registered")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] acp_chat registration failed: %s", exc)

        self._register_deepresearch_tool_cards(tool_cards)

        # 动态加载环境变量配置的非侵入式工具扩展（AGENT_EXTRA_TOOLS，仅企业版）
        self._append_extra_tool_cards(tool_cards)

        return tool_cards

    def _build_cron_tools(self) -> list[Any]:
        """Build cron tools from the shared runtime bridge."""
        return self._cron_runtime.build_tools(
            context=self._runtime_cron_tool_context,
            agent_id=self._tool_owner_id(),
            language=self._resolve_runtime_language(),
            service_id=getattr(self, "_env_service_id", None),
            tenant_agent_id=getattr(self, "_env_agent_id", None),
        )

    async def _proc_context_compaction(self) -> None:
        """Backward-compatible no-op hook for tests and legacy call sites."""
        return None

    def _skip_own_instance_build(self) -> bool:
        """Return whether ``create_instance`` should stop before building a DeepAgent.

        On the chat path the root (non session-scoped) adapter is only a router
        and a template holder: every turn runs on a per-session child adapter
        that owns the live DeepAgent. Building one for the root as well doubles
        tool registration, rail setup, ``ensure_initialized`` and MCP server
        registration on the critical path. The root instance is therefore built
        lazily by :meth:`ensure_instance`, which the non-chat RPCs that need a
        DeepAgent handle call first.

        Returns:
            True when the caller should return after the cheap config-cache
            section, leaving ``self._instance`` unset.
        """
        return not self._is_session_scoped_adapter and not self._root_instance_requested

    async def ensure_instance(self) -> Any:
        """Return this adapter's own DeepAgent, building it on first use.

        Session-scoped adapters always have one already; the root adapter builds
        it here so callers that need a DeepAgent handle outside the chat path
        (harness packages, rail toggles, session fork, code plan state, ...) keep
        working without putting that cost on every chat turn.

        Returns:
            The DeepAgent instance, or None when it could not be built.
        """
        if self._instance is not None:
            return self._instance
        if self._root_instance_lock is None:
            self._root_instance_lock = asyncio.Lock()
        async with self._root_instance_lock:
            if self._instance is not None:
                return self._instance
            self._root_instance_requested = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] building root DeepAgent on demand: mode=%s sub_mode=%s",
                self._session_instance_mode,
                self._session_instance_sub_mode,
            )
            await self.create_instance(
                self._session_instance_config,
                mode=self._session_instance_mode or "agent",
                sub_mode=self._session_instance_sub_mode,
                config_base=self._config_base_cache,
            )
            return self._instance

    async def create_instance(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent",
        sub_mode: str = None,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """初始化 DeepAgent 实例.

        Args:
            config: 可选配置，支持以下字段：
                - agent_name: Agent 名称，默认 "main_agent"。
                - workspace_dir: 工作区目录，默认 "workspace/agent"。
                - enabled_skills: Skill 白名单目录名。
                - request: 可选 AgentRequest（创建时按 params 加载企业配置并合并模型）。
                - 其余字段透传给 DeepAgentConfig。
            mode: 实例化模式，默认 "agent"，使用 create_deep_agent。
            sub_mode: 子模式
        """
        self._session_instance_config = dict(config or {}) if isinstance(config, dict) else None
        self._session_instance_mode = mode
        self._session_instance_sub_mode = sub_mode

        await self.set_checkpoint()
        await asyncio.sleep(0)

        self._dreaming_mode = mode if mode and mode.startswith("agent") else "agent"
        self._instance_overrides = dict(config or {}) if isinstance(config, dict) else {}
        # Bind tenant tip before get_config/_create_model. Without this, session
        # adapters resolve ${API_KEY}/${MODEL_NAME} from process/.env placeholders
        # (your-model-name) instead of the synced office tip bag.
        ns_token, overlay_token, wk_token = self._bind_request_env_overlay()
        try:
            load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
            incoming_config_base = config_base
            config_base = _resolve_instance_config_base(config_base)
            config_base = coalesce_config_skill_envs(
                config_base,
                incoming_config_base if isinstance(incoming_config_base, dict) else self._config_base_cache,
            )
            # 企业版：create_instance 时可带 request，按 params 加载企业配置并合并模型
            bootstrap_request = self._instance_overrides.pop("request", None)
            if bootstrap_request is not None and is_enterprise():
                await self._load_enterprise_config(bootstrap_request)
            config_base = merge_memory_config_into_config(config_base)
            config_base = self._merge_enterprise_models_into_config(config_base)
            # 与模型槽位一致：Agent 级 permissions 模板在构建 rail 前绑定到 Task
            token_perm_agent = self._bind_agent_permissions_base()
            try:
                self._config_base_cache = config_base.copy()
                self._startup_config_base = config_base.copy()
                self._refresh_multimodal_configs(config_base)
                config = config_base.get("react", {}).copy()
                self._config_cache = config.copy()
                self._agent_name = self._instance_overrides.get(
                    "agent_name", config.get("agent_name", "main_agent")
                )

                if (
                    is_skill_whitelist_tenant(self._agent_id, self._service_id)
                    and self._enterprise_config is not None
                ):
                    enterprise_skills: list[dict[str, Any]] = (
                        getattr(self._enterprise_config, "skill_whitelist", None) or []
                    )
                    skill_config = parse_agent_skill_whitelist(
                        self._agent_id, self._service_id, enterprise_skills
                    )
                    sync_result = await SkillWhitelistSynchronizer(
                        self._workspace_dir,
                        self._service_id,
                        self._agent_id,
                        skill_manager=self._skill_manager,
                    ).sync(skill_config)
                    if sync_result.errors:
                        logger.warning(
                            "[SkillWhitelist] sync partial errors: agent_id=%s service_id=%s errors=%s",
                            self._agent_id,
                            self._service_id,
                            sync_result.errors,
                        )
                    if sync_result.enabled_skill_dirs is not None:
                        self._enabled_skills = [
                            str(name) for name in sync_result.enabled_skill_dirs if str(name).strip()
                        ]
                    # 预置 Skill 名快照：供动态授权 trust 判定（approve_session 仅内置可选）。
                    self._prebuilt_skills = {
                        str(name).strip()
                        for name in (sync_result.prebuilt_skill_dirs or [])
                        if str(name).strip()
                    }
                self._project_dir = self._instance_overrides.get(
                    "project_dir", config.get("project_dir")
                )
                # Keep constructor-injected tenant workspace by default.
                # Only override when request explicitly provides workspace_dir.
                configured_workspace = self._instance_overrides.get("workspace_dir")
                if configured_workspace is not None:
                    self._workspace_dir = configured_workspace
                self._prompt_attachment_loader = PromptAttachmentLoader(self._prompt_attachment_root())
                self._prompt_attachment_loader.ensure_layout()

                if self._skip_own_instance_build():
                    return

                self._log_active_model_on_startup(phase=f"create_instance:{mode}")
                try:
                    model = self._create_model(config_base)
                except Exception as exc:
                    logger.error(
                        "[JiuWenSwarmDeepAdapter] create_instance 模型初始化失败(%s): %s",
                        mode,
                        exc,
                    )
                    raise
                if self._is_session_scoped_adapter:
                    await self._try_init_a2x_client(config_base)
                agent_card = AgentCard(name=self._agent_name, id=self._runtime_agent_scope_id())

                tool_cards = await self._get_tool_cards(self._tool_owner_id())
                self._tool_cards = tool_cards
                logger.info("[JiuWenSwarmDeepAdapter] Agent card id: %s", agent_card.id)
                await asyncio.sleep(0)

                # 权限护栏由 openjiuwen PermissionInterruptRail + ToolPermissionHost 接管；
                # 无需初始化 jiuwenswarm 内置 PermissionEngine（已弃用）。

                rails_list = self._build_agent_rails(config, config_base, mode=mode)

                sys_operation = self._create_sys_operation()
                if sys_operation is None:
                    raise RuntimeError("sys_operation is not available, maybe task is not running")

                self._sys_operation = sys_operation
                configured_subagents, should_add_general_agent = self._build_configured_subagents(
                    model, config, config_base
                )
                should_enable_general_agent = should_add_general_agent and (
                    sub_mode == "plan" or (isinstance(mode, str) and mode.startswith("agent"))
                )
                from jiuwenswarm.agents.harness.observability_runtime import (
                    get_trajectory_span_processor,
                )

                common_kwargs = dict(
                    model=model,
                    card=agent_card,
                    tool_owner_id=self._tool_owner_id(),
                    system_prompt=build_agent_identity_prompt(
                        language=self._resolve_prompt_language(),
                    ),
                    tools=tool_cards if tool_cards else [],
                    subagents=configured_subagents,
                    rails=rails_list if rails_list else [],
                    enable_task_loop=self._resolve_enable_task_loop(config, config_base),
                    add_general_purpose_agent=should_enable_general_agent,
                    max_iterations=config.get("max_iterations", 15),
                    workspace=Workspace(
                        root_path=self._workspace_dir or "./",
                        language=self._resolve_runtime_language(),
                    ),
                    sys_operation=sys_operation,
                    language=self._resolve_runtime_language(),
                    auto_create_workspace=False,
                    trajectory_span_processor=get_trajectory_span_processor(),
                )

                # agent_ras YAML passthrough (Agent RAS owns loop detection / recovery).
                common_kwargs.update(_agent_ras_kwargs_from_config(config_base))

                self._instance = create_deep_agent(
                    **common_kwargs,
                    context_engine_config=_deep_agent_context_engine_config(config),
                    kv_cache_affinity_config=_deep_agent_kv_cache_affinity_config(config, model),
                    vision_model_config=self._vision_model_config,
                    audio_model_config=self._audio_model_config,
                    enable_read_image_multimodal=self._resolve_enable_read_image_multimodal(config),
                    enable_llm_retry_rail=((config_base.get("execution_guard") or {}).get("llm_retry_rail") or {}).get(
                        "enabled", False
                    ),
                    completion_timeout=config.get("completion_timeout", 21600.0),
                )
                self._bind_subagent_model_resolver()
                self._bind_subagent_authorization_wiring()

                _apply_llm_io_trace_patch()

                await asyncio.sleep(0)
                await self._instance.ensure_initialized()
                initial_runtime_workspace = self._project_dir or str(
                    get_default_project_session_workspace_dir()
                )
                await asyncio.to_thread(
                    self._ensure_project_gitignore_agent_history,
                    initial_runtime_workspace,
                )
                self._seed_runtime_cwd(initial_runtime_workspace, workspace=initial_runtime_workspace)
                setattr(self._instance, "_jiuwenswarm_project_dir", initial_runtime_workspace)

                self._sync_a2x_runtime_state()
                # Cron tools belong to the agent's standing toolset, not to any one
                # request; build them here so the first turn does not pay for it either.
                self._ensure_cron_tools_registered(self._parent_session_id)
                self._registered_mcp_server_ids.clear()
                self._registered_mcp_servers.clear()
                await self._register_mcp_servers_from_config(config_base, tag=f"agent.{mode}")
                logger.info(
                    "[JiuWenSwarmDeepAdapter] 初始化完成: agent_name=%s, mode=%s, sub_mode=%s",
                    self._agent_name,
                    mode,
                    sub_mode,
                )
                logger.info(
                    "[latency] stage=1 name=init request_id=%s",
                    _LLM_TRACE_REQUEST_ID.get() or "-",
                )

                # 加载已激活的 packages（skills, rails, tools）
                await self._load_active_packages()
                await asyncio.sleep(0)

                self._sync_preinstance_runtime_tools_to_ability_manager()
                self._sync_multimodal_tools_for_runtime()
                await asyncio.to_thread(self._init_skill_turbo_tool)

                # 动态加载用户自定义的 Rail 扩展
                await self.load_user_rails()
                self._register_extension_tools()
            finally:
                reset_permissions_agent_base(token_perm_agent)
        finally:
            self._reset_request_env_bindings(ns_token, overlay_token, wk_token)

    def _register_extension_tools(self) -> None:
        """将 ExtensionRegistry 登记的扩展本地工具挂到 Runner 与 ability_manager。"""
        if self._instance is None:
            return
        try:
            from jiuwenswarm.extensions.registry import ExtensionRegistry
            from jiuwenswarm.extensions.sdk.local_tool_builder import make_extension_tools

            ext_reg = ExtensionRegistry.get_instance()
            existing_ext_tool_names: set[str] = {
                ab.name
                for ab in self._instance.ability_manager.list()
                if isinstance(ab, ToolCard)
            }
            for ext_tool in make_extension_tools(ext_reg.extension_local_tool_entries):
                tname = ext_tool.card.name
                tid = ext_tool.card.id
                if tname in existing_ext_tool_names:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] extension tool name conflicts with existing tool, skip: %s",
                        tname,
                    )
                    continue
                try:
                    self._register_shared_tool(ext_tool)
                    self._instance.ability_manager.add(ext_tool.card)
                    existing_ext_tool_names.add(tname)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] extension tool add success, name: %s id: %s",
                        tname,
                        tid,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] extension tool register failed (%s): %s",
                        tname,
                        exc,
                    )
        except RuntimeError:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ExtensionRegistry unavailable, skip extension tools"
            )

    @staticmethod
    def _ensure_project_gitignore_agent_history(project_dir: str | None) -> None:
        """Ensure JiuwenSwarm's file operation logs stay out of project git diffs."""
        if not project_dir:
            return
        try:
            repo_probe = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if repo_probe.returncode != 0:
            return

        repo_root_text = repo_probe.stdout.strip()
        if not repo_root_text:
            return
        gitignore_path = Path(repo_root_text) / ".gitignore"
        try:
            existing_bytes = gitignore_path.read_bytes() if gitignore_path.exists() else b""
        except OSError as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] read .gitignore for agent history rule failed: %s",
                exc,
            )
            return

        if JiuWenSwarmDeepAdapter._gitignore_covers_agent_history(existing_bytes):
            return

        existing = existing_bytes.decode("utf-8", errors="replace")
        prefix = "" if not existing else ("\n" if existing.endswith(("\n", "\r")) else "\n\n")
        addition = (
            f"{prefix}# JiuwenSwarm runtime file operation logs\n.agent_history/\n"
        ).encode("utf-8")
        try:
            gitignore_path.write_bytes(existing_bytes + addition)
        except OSError as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ensure .agent_history gitignore failed: %s",
                exc,
            )

    @staticmethod
    def _gitignore_covers_agent_history(content: bytes) -> bool:
        """Return True if .gitignore content already ignores .agent_history/.

        Recognizes equivalent forms such as ``.agent_history``,
        ``.agent_history/``, ``.agent_history/*``, ``.agent_history/**``,
        ``.agent_history/**/*``, ``**/.agent_history`` and combinations
        thereof, so that the rule is idempotent across pre-existing project
        configurations.
        """
        text = content.decode("utf-8", errors="replace")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Skip negation rules; they re-include paths rather than ignore
            # the directory.
            if line.startswith("!"):
                continue
            # Strip leading glob prefix like `**/`.
            if line.startswith("**/"):
                line = line[3:]
            # Split into segments. The rule covers `.agent_history` if the
            # first segment matches and any following segments are wildcards
            # that target the directory contents.
            segments = [seg for seg in line.split("/") if seg]
            if not segments or segments[0] != ".agent_history":
                continue
            if all(seg in ("*", "**") for seg in segments[1:]):
                return True
        return False

    async def _sync_prompt_attachments_for_request(self, session_id: str) -> None:
        """Hot-load prompt attachment files for the current request.

        Prompt attachment loading must not block the user request path. Failures are
        logged and the original Runner flow continues without attachment injection.
        """

        if self._instance is None:
            return
        if self._prompt_attachment_loader is None:
            self._prompt_attachment_loader = PromptAttachmentLoader(self._prompt_attachment_root())
            self._prompt_attachment_loader.ensure_layout()
        try:
            await self._prompt_attachment_loader.sync_to_agent(
                self._instance,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] prompt attachment sync skipped: %s", exc)

    def _prompt_attachment_root(self) -> Path:
        if self._workspace_dir == str(get_agent_workspace_dir()):
            return get_prompt_attachment_dir()
        return Path(self._workspace_dir) / "prompt_attachment"

    async def load_user_rails(self) -> None:
        """动态加载用户自定义的 Rail 扩展."""
        try:
            manager = get_rail_manager(RuntimeScopeKey.from_adapter(self))

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
                            "[JiuWenSwarmDeepAdapter] 用户 Rail 扩展加载失败: %s, 错误: %s",
                            ext["name"],
                            e,
                        )
        except Exception as e:
            logger.error("[JiuWenSwarmDeepAdapter] 加载用户 Rail 扩展时发生错误: %s", e)

    async def _apply_reload_config_snapshot(
        self,
        config_base: dict[str, Any] | None,
        env_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Refresh the cached config snapshot shared by every reload path.

        Args:
            config_base: Optional full config snapshot; read from disk when None.
            env_overrides: Optional environment variable delta applied first; a
                None value removes the variable.

        Returns:
            The normalized config snapshot that was cached on this adapter.
        """
        clear_config_cache()
        clear_embed_config_db_cache()
        clear_memory_config_db_cache()
        # 清 MemoryRail 实际使用的 openjiuwen lite INDEX_CACHE（而非仓内并行实现的那份），
        # 并 close 旧实例（db 连接 / watchdog observer / 定时任务），使下次
        # init_memory_manager_async 用最新 embedding_config 创建新 manager + 新 provider。
        try:
            from openjiuwen.core.memory.lite.manager import aclose_memory_manager_cache

            await aclose_memory_manager_cache()
        except Exception as e:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] aclose openjiuwen memory cache failed: %s", e
            )
        clear_task_memory_service()

        # env_overrides 已由上层 agent_manager 通过 stage_env_overrides +
        # _bind_request_env_overlay 写入 tip bag / overlay，不再直接写入
        # os.environ，避免 Track B 值同时存在于 os.environ 和 tip bag 中。

        if config_base is None:
            config_base = get_config()
        elif not isinstance(config_base, dict):
            raise TypeError("config_base must be a dict when provided")
        else:
            config_base = resolve_env_vars(config_base)

        live_skill_envs = (
            self._skill_credential_injection_rail.get_skill_envs()
            if self._skill_credential_injection_rail is not None
            else None
        )
        previous_for_skill_envs = (
            {"react": {"skill_envs": live_skill_envs}}
            if live_skill_envs
            else self._config_base_cache
        )
        config_base = coalesce_config_skill_envs(config_base, previous_for_skill_envs)

        # 同步扩展配置到 ExtensionRegistry
        # Gateway 已解密 extension_security_configs，AgentServer 直接使用明文
        try:
            from jiuwenswarm.extensions.registry import ExtensionRegistry
            registry = ExtensionRegistry.get_instance()
            registry.update_config(config_base)
            logger.info("[JiuWenSwarm] Extension config synced to Registry")
        except Exception as exc:
            logger.warning("[JiuWenSwarm] ExtensionRegistry update failed: %s", exc)

        self._config_base_cache = config_base.copy()
        config_base = self._merge_enterprise_models_into_config(config_base)
        config_base = merge_memory_config_into_config(config_base)
        self._config_base_cache = config_base.copy()
        self._startup_config_base = config_base.copy()
        self._refresh_multimodal_configs(config_base)

        self._config_cache = config_base.get("react", {}).copy()
        return config_base

    async def _fan_out_reload_to_session_adapters(
        self,
        config_base: dict[str, Any],
        env_overrides: dict[str, Any] | None,
        target_sid: str | None,
    ) -> None:
        """Cascade a config reload to the live per-session adapters.

        No-op on a session-scoped adapter, which owns no children.

        Args:
            config_base: Normalized config snapshot to hand to the children.
            env_overrides: Environment variable delta passed through unchanged.
            target_sid: When set, only that session is reloaded eagerly; when
                None, every session adapter is marked stale for lazy reload.
        """
        if self._is_session_scoped_adapter:
            return
        if not target_sid:
            self._mark_session_adapters_stale_for_reload(config_base, env_overrides)
            return
        for session_id, adapter in self._iter_session_adapters_for_reload(target_sid):
            try:
                await adapter.reload_agent_config(
                    config_base,
                    env_overrides,
                    target_session_id=target_sid,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] session adapter reload failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )
            else:
                self._session_adapter_versions[session_id] = self._session_adapter_config_version
                self._session_adapter_reload_failures.pop(session_id, None)

    async def reload_agent_config(
        self,
        config_base: dict[str, Any] | None = None,
        env_overrides: dict[str, Any] | None = None,
        target_session_id: str | None = None,
        *,
        _force_apply: bool = False,
    ) -> ReloadResult:
        """从 config.yaml 重新加载配置，通过 DeepAgent.configure() 热更新当前实例（不新建 DeepAgent）。

        DeepAgent.configure() 现在自动处理 rail 生命周期：保留旧已注册 rails 的注销上下文，
        并在下次 _ensure_initialized() 时先卸载旧回调，再注册新的 rails。

        When the adapter is busy, stores a pending reload and returns deferred=True
        (unless ``_force_apply``). Env overrides live in tip bags; configure runs
        under a sealed task overlay so Track B is not written to os.environ.

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
            target_session_id: 可选的目标 session id；传入时仅级联热更新该 session adapter。
            _force_apply: Bypass busy check (pending drain / authoritative apply).
        """
        target_sid = str(target_session_id or "").strip() or None
        if self._is_session_scoped_adapter and target_sid:
            own_sid = self._session_adapter_key(self._parent_session_id)
            if own_sid != self._session_adapter_key(target_sid):
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] skip scoped reload for unrelated session: target=%s self=%s",
                    target_sid,
                    own_sid,
                )
                return ReloadResult(applied=True)

        # Session-scoped adapters that are mid-request defer configure.
        if self._is_session_scoped_adapter and not _force_apply:
            pending = (config_base, env_overrides)
            if self._adapter_is_working():
                self._pending_reload = pending
                logger.info(
                    "[JiuWenSwarmDeepAdapter] reload deferred (session busy): session=%s",
                    self._parent_session_id,
                )
                return ReloadResult(deferred=True)
            async with self._reload_lock:
                if self._adapter_is_working():
                    self._pending_reload = pending
                    return ReloadResult(deferred=True)
                self._pending_reload = None
        elif _force_apply:
            self._pending_reload = None

        if is_enterprise():
            try:
                await reload_memory_config_from_gateway_db()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] reload_memory_config_from_gateway_db failed: %s",
                    exc,
                )

        if self._instance is None:
            if self._is_session_scoped_adapter:
                raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")
            # Root adapter whose lazily-built DeepAgent nobody has needed yet:
            # it has nothing of its own to reconfigure, but the cached config
            # snapshot still has to move forward and the live session adapters
            # still have to be reloaded.
            ns_token, overlay_token, wk_token = self._bind_request_env_overlay(env_overrides)
            try:
                config_base = await self._apply_reload_config_snapshot(config_base, env_overrides)
                await self._fan_out_reload_to_session_adapters(config_base, env_overrides, target_sid)
            finally:
                self._reset_request_env_bindings(ns_token, overlay_token, wk_token)
            logger.info(
                "[JiuWenSwarmDeepAdapter] 配置已热更新（root 实例未构建，仅刷新缓存并级联 session adapter）"
            )
            return ReloadResult(applied=True)

        ns_token, overlay_token, wk_token = self._bind_request_env_overlay(env_overrides)
        try:
            # TaskMemory: clear when env keys that feed the fingerprint change.
            if env_touches_task_memory(env_overrides):
                clear_task_memory_service()

            # Shared skill dirs / ENABLED_SKILLS from tip: rebuild SkillUseRail so
            # OfficeClaw office-claw-skills become visible after sync/reload.
            if env_touches_shared_skills_dirs(env_overrides):
                self._skill_rail = None
                logger.info(
                    "[JiuWenSwarmDeepAdapter] shared skills env changed; SkillUseRail will rebuild"
                )

            config_base = await self._apply_reload_config_snapshot(config_base, env_overrides)
            config = self._config_cache.copy()

            model = self._create_model(config_base)
            if self._is_session_scoped_adapter:
                await self._try_init_a2x_client(config_base, reload=True)
                self._sync_a2x_runtime_state()
            self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))
            agent_card = AgentCard(name=self._agent_name, id=self._runtime_agent_scope_id())
            self._sync_multimodal_tools_for_runtime()
            self._sync_paid_search_tool_for_runtime()
            self._sync_symphony_tools_for_runtime(config_base)
            self._sync_skill_retrieval_tools_for_runtime(config_base)
            await self._sync_skill_retrieval_prompt_rail_for_runtime(config_base)

            rails_list, rails_to_unregister = self._get_current_agent_rails(
                config, config_base
            )

            # 加载用户自定义的 Rail 扩展
            await self.load_user_rails()

            # 双保险：即便 DisabledToolsRail 时序未注销，deep_cfg 的 tool_cards 也先剔除黑名单。
            disabled_names = set(
                resolve_string_or_list_config(config.get("disabled_tools"))
            )
            if disabled_names and self._tool_cards:
                filtered_tool_cards = [
                    card for card in self._tool_cards
                    if getattr(card, "name", None) not in disabled_names
                ]
            else:
                filtered_tool_cards = self._tool_cards if self._tool_cards else []

            deep_cfg = self._make_deep_agent_config(
                model=model,
                config=config,
                config_base=config_base,
                agent_card=agent_card,
                tool_cards=filtered_tool_cards,
                rails=rails_list,
            )
            omitted_fields, reload_fingerprints = self._omit_unchanged_reload_fields(deep_cfg)
            try:
                self._instance.configure(deep_cfg)
            finally:
                self._restore_omitted_reload_fields(deep_cfg, omitted_fields)

            # configure() 把旧 rail 移入 _stale_rails、新 rail 入 _pending_rails 并
            # 置 _initialized=False，但真正的换装（unregister stale + init pending
            # 进 dispatch 列表）在 _ensure_initialized 中懒触发，其入口仅在
            # invoke/stream。office 服务路径不走 invoke/stream，导致换装永不执行、
            # 旧 SkillUseRail 继续服务、新装 skill 对运行中会话不可见。此处显式
            # 触发换装，与创建期 ensure_initialized() 调用一致；_needs_workspace_init
            # 守卫重 init，已初始化的工作区不会重建，无长阻塞。
            await self._instance.ensure_initialized()

            first_unregister_error: Exception | None = None
            rail_cache_attrs = (
                "_progressive_tool_rail",
                "_filesystem_rail",
                "_disabled_tools_rail",
            )
            for rail in rails_to_unregister:
                try:
                    await self._instance.unregister_rail(rail)
                except Exception as exc:
                    if first_unregister_error is None:
                        first_unregister_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] %s unregister on reload failed: %s",
                        type(rail).__name__,
                        exc,
                    )
                    continue
                for attr_name in rail_cache_attrs:
                    if getattr(self, attr_name, None) is rail:
                        setattr(self, attr_name, None)
                        break
                logger.info(
                    "[JiuWenSwarmDeepAdapter] %s unregistered on reload",
                    type(rail).__name__,
                )

            await self._reconcile_evolution_rails()
            # configure() rebuilds ability_manager from tool_cards; multimodal tools
            # registered before configure are dropped — re-sync after configure.
            self._sync_multimodal_tools_for_runtime()
            self._commit_reload_fingerprints(reload_fingerprints)
            self._sync_active_evolution_review_agent_after_reload()

            await self._sync_mcp_servers_for_runtime(config_base, tag="agent.reload")

            await self._fan_out_reload_to_session_adapters(config_base, env_overrides, target_sid)

            # 主动刷新 memory rail（不等下次请求的 _update_rails_for_mode）：
            # 让 embedding 配置变更立即走指纹检测 + 重建 rail + 延时重索引。
            # 若从未处理过请求（_last_mode 为 None，如冷启动后首次 reload），退化为默认 agent。
            try:
                mode = self._last_mode or "agent"
                await self._handle_memory_rail_by_config(mode)
                await self._handle_external_memory_rail_by_config()
            except Exception as e:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] memory rail refresh on reload failed: %s", e
                )

            if first_unregister_error is not None:
                raise first_unregister_error

            logger.info("[JiuWenSwarmDeepAdapter] 配置已热更新（configure），未重启进程")
            return ReloadResult(applied=True)
        finally:
            self._reset_request_env_bindings(ns_token, overlay_token, wk_token)

    def _bind_runtime_cron_context(
        self,
        *,
        channel_id: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None,
        request_id: str | None,
        mode: str | None,
        project_dir: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> _RuntimeCronContextTokens:
        from openjiuwen.core.sys_operation.shell_process_registry import (
            set_shell_session_id,
        )
        from jiuwenswarm.common.request_identity import (
            apply_routing_metadata,
            web_routing_identity,
        )
        from jiuwenswarm.gateway.cron.enterprise_gate import extract_routing_triple

        normalized_channel = str(channel_id or "").strip() or CronTargetChannel.WEB.value
        normalized_mode = str(mode).strip() if isinstance(mode, str) and mode.strip() else None
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else None
        if normalized_metadata is None:
            normalized_metadata = {}
        if isinstance(request_id, str) and request_id.strip():
            normalized_metadata["request_id"] = request_id.strip()
        # 权威：顶层 user_id + metadata.routing；params 仅作本地 cron 已 merge 的补充。
        g, b, u = extract_routing_triple(normalized_metadata, params)
        routing = web_routing_identity(normalized_metadata)
        if g:
            routing["group_id"] = g
        if b:
            routing["bot_id"] = b
        if u:
            routing["user_id"] = u
        normalized_metadata = apply_routing_metadata(normalized_metadata, routing)

        session_metadata: dict[str, Any] = {}
        if isinstance(session_id, str) and session_id.strip():
            try:
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )

                loaded_metadata = get_session_metadata(session_id.strip(), cache_bust=True)
                if isinstance(loaded_metadata, dict):
                    session_metadata = loaded_metadata
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] failed to load session metadata for cron context: %s",
                    exc,
                )
        # 注入 project_dir 供 cron tool 路由解析任务归属项目（设计文档 §5.1）
        if isinstance(project_dir, str) and project_dir.strip():
            normalized_metadata.setdefault("project_dir", project_dir.strip())
        for key in ("project_id", "project_dir", "work_mode"):
            value = normalized_metadata.get(key)
            if isinstance(value, str) and value.strip():
                continue
            session_value = session_metadata.get(key)
            if isinstance(session_value, str) and session_value.strip():
                normalized_metadata[key] = session_value.strip()

        channel_token = _CRON_TOOL_CHANNEL_ID.set(normalized_channel)
        session_token = _CRON_TOOL_SESSION_ID.set(session_id)
        metadata_token = _CRON_TOOL_METADATA.set(normalized_metadata)
        mode_token = _CRON_TOOL_MODE.set(normalized_mode)
        bound_token = _CRON_TOOL_BOUND.set(True)
        shell_token = set_shell_session_id(session_id)

        # 绑定 send_file 专用路由 ContextVar（与 skill_turbo / test 仓对齐）。
        # 默认值为 None，不会像 _CRON_TOOL_CHANNEL_ID 那样在 reset 后回落成 web。
        send_file_token: Token | None = None
        try:
            from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
                set_send_file_request_context,
            )

            send_file_token = set_send_file_request_context(
                request_id=(
                    request_id.strip()
                    if isinstance(request_id, str) and request_id.strip()
                    else None
                ),
                session_id=session_id,
                channel_id=normalized_channel,
                metadata=normalized_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenSwarmDeepAdapter] set send_file request context failed: %s",
                exc,
            )

        scope = RuntimeScopeKey.from_adapter(self, session_id=session_id)
        try:
            deepresearch_token = push_deepresearch_route(
                request_id=request_id or "",
                channel_id=normalized_channel,
                session_id=scope.session_id,
                service_id=scope.service_id,
                agent_id=scope.agent_id,
                workspace_key=scope.workspace_key,
                output_dir=self._deepresearch_artifact_output_dir(
                    normalized_metadata.get("project_dir")
                ),
            )
        except BaseException:
            self._reset_runtime_cron_context(
                _RuntimeCronContextTokens(
                    channel=channel_token,
                    session=session_token,
                    metadata=metadata_token,
                    mode=mode_token,
                    bound=bound_token,
                    shell=shell_token,
                    deepresearch=None,
                    send_file=send_file_token,
                ),
                suppress_errors=True,
            )
            raise
        return _RuntimeCronContextTokens(
            channel=channel_token,
            session=session_token,
            metadata=metadata_token,
            mode=mode_token,
            bound=bound_token,
            shell=shell_token,
            deepresearch=_DeepResearchRouteContextToken(deepresearch_token),
            send_file=send_file_token,
        )

    @staticmethod
    def _reset_deepresearch_route_context(tokens: _RuntimeCronContextTokens) -> None:
        """Reset one DeepResearch route token at most once."""
        route_token = tokens.deepresearch
        if route_token is None or route_token.token is None:
            return
        token = route_token.token
        reset_deepresearch_route(token)
        route_token.token = None

    @staticmethod
    def _run_cleanup_steps(
        steps: list[tuple[str, Callable[[], None]]],
    ) -> BaseException | None:
        """Run every cleanup step, returning the first failure without secrets."""
        first_error: BaseException | None = None
        for label, cleanup in steps:
            try:
                cleanup()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] cleanup failed: step=%s error_type=%s",
                    label,
                    type(exc).__name__,
                )
        return first_error

    @classmethod
    def _reset_runtime_cron_context(
        cls,
        tokens: _RuntimeCronContextTokens,
        *,
        suppress_errors: bool = False,
    ) -> None:
        from openjiuwen.core.sys_operation.shell_process_registry import (
            reset_shell_session_id,
        )

        first_error: BaseException | None = None

        def _reset(name: str, reset: Callable[[Any], None]) -> None:
            nonlocal first_error
            token = getattr(tokens, name)
            if token is None:
                return
            try:
                reset(token)
            except BaseException as exc:  # cleanup must continue through cancellation
                if first_error is None:
                    first_error = exc
            else:
                setattr(tokens, name, None)

        route_token = tokens.deepresearch
        if route_token is not None and route_token.token is not None:
            token = route_token.token
            try:
                reset_deepresearch_route(token)
            except BaseException as exc:
                first_error = exc
            else:
                route_token.token = None
        if tokens.send_file is not None:
            from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
                reset_send_file_request_context,
            )

            _reset("send_file", reset_send_file_request_context)
        _reset("shell", reset_shell_session_id)
        _reset("bound", _CRON_TOOL_BOUND.reset)
        _reset("mode", _CRON_TOOL_MODE.reset)
        _reset("metadata", _CRON_TOOL_METADATA.reset)
        _reset("session", _CRON_TOOL_SESSION_ID.reset)
        _reset("channel", _CRON_TOOL_CHANNEL_ID.reset)
        if first_error is not None and not suppress_errors:
            raise first_error

    def _reset_stream_runtime_context(
        self,
        tokens: _RuntimeCronContextTokens,
        *,
        stream_consumer_cancelled: bool,
    ) -> None:
        """Always release DeepResearch routing without changing legacy cancel cleanup."""
        if stream_consumer_cancelled:
            self._reset_deepresearch_route_context(tokens)
            return
        self._reset_runtime_cron_context(tokens)

    async def _update_rails_for_mode(self, mode: str) -> None:
        """装配 agent 模式 rails。

        plan / fast 已合并为单一 ``agent`` 模式：统一挂载 plan 档能力
        （TaskPlanning / Subagent / 演进 rail 等），记忆固定为被动模式，
        不再按子模式分叉。历史 ``agent.plan`` / ``agent.fast`` 归一到此路径。
        """
        self._last_mode = mode
        await self._update_agent_rails()

    async def _reconcile_evolution_rails(self) -> None:
        """Apply evolution rail configuration through its runtime owner."""
        if get_evolution_enabled(self._config_cache):
            await self._ensure_active_evolution_rails_registered()
        elif (
            self._skill_evolution_rail is not None
            or self._evolution_interrupt_rail is not None
        ):
            await self._unconfigure_active_evolution_rails()

    @staticmethod
    def _user_interaction_rail_attribute() -> str:
        return "_ask_user_rail"

    async def _set_user_interaction_enabled(self, enabled: bool) -> None:
        """Expose ``ask_user`` only when the requesting client can answer it."""
        attr_name = self._user_interaction_rail_attribute()
        rail = getattr(self, attr_name, None)
        if enabled:
            if rail is None:
                rail = self._build_structured_ask_user_rail()
                if rail is not None:
                    await self._instance.register_rail(rail)
                    setattr(self, attr_name, rail)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] StructuredAskUserRail enabled "
                        "for interactive request"
                    )
            return

        if rail is not None:
            await self._instance.unregister_rail(rail)
            setattr(self, attr_name, None)
            logger.info(
                "[JiuWenSwarmDeepAdapter] StructuredAskUserRail disabled "
                "for non-interactive request"
            )

    async def _update_agent_rails(self) -> None:
        """agent 模式：注册 agent 专属 rails（原 plan 档能力并集）。"""
        if self._task_planning_rail is None:
            self._task_planning_rail = self._build_task_planning_rail()
            if self._task_planning_rail is not None:
                await self._instance.register_rail(self._task_planning_rail)
                logger.info("[JiuWenSwarmDeepAdapter] TaskPlanningRail registered for agent mode")
        if self._ask_user_rail is None:
            self._ask_user_rail = self._build_structured_ask_user_rail()
            if self._ask_user_rail is not None:
                await self._instance.register_rail(self._ask_user_rail)
                logger.info("[JiuWenSwarmDeepAdapter] StructuredAskUserRail registered for agent mode")
        # 卸载 multi-session 工具
        for existing in list(self._instance.ability_manager.list() or []):
            if getattr(existing, "name", "").startswith(
                ("session_new", "session_cancel", "session_list")
            ):
                self._instance.ability_manager.remove(existing.name)
        # agent 模式，根据config选择是否注册或者卸载memory rail（固定被动记忆）
        await self._handle_memory_rail_by_config("agent")
        # 外接记忆 rail（mode-independent，注册一次，跨 reload 持久）
        await self._handle_external_memory_rail_by_config()
        # 上下文 rail
        context_enabled = self._config_cache.get("context_engine_config", {}).get("enabled", False)

        if self._context_assemble_rail is None or self._context_assemble_mode != "agent":
            if self._context_assemble_rail is not None:
                await self._instance.unregister_rail(self._context_assemble_rail)
                self._context_assemble_rail = None
            self._context_assemble_rail = _build_context_assemble_rail()
            self._context_assemble_mode = "agent"
            await self._instance.register_rail(self._context_assemble_rail)
            logger.info(
                "[JiuWenSwarmDeepAdapter] %s registered for agent mode", "ContextAssembleRail"
            )

        # ContextProcessorRail
        if context_enabled:
            if self._context_processor_rail is None:
                self._context_processor_rail = _build_context_processor_rail(self._config_cache)
                if self._context_processor_rail is not None:
                    await self._instance.register_rail(self._context_processor_rail)
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] ContextProcessorRail registered for agent mode"
                    )
        else:
            if self._context_processor_rail is not None:
                await self._instance.unregister_rail(self._context_processor_rail)
                self._context_processor_rail = None
                logger.info(
                    "[JiuWenSwarmDeepAdapter] ContextProcessorRail unregistered for agent mode (disabled)"
                )

        # Evolution runtime configure owns the complete regular + interrupt rail set.
        await self._reconcile_evolution_rails()

        # SkillCreateRail
        skill_create_enabled = get_skill_create_enabled(self._config_cache)
        if skill_create_enabled:
            # Warn if task_loop is disabled
            deep_config = getattr(self._instance, "deep_config", None) if self._instance else None
            if deep_config is not None:
                if not deep_config.enable_task_loop:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] skill_create=true requires task_loop mode, "
                        "but enable_task_loop=False. SkillCreateRail may not function properly."
                    )
            if self._skill_create_rail is None:
                self._skill_create_rail = self._build_skill_create_rail(self._config_cache)
            if self._skill_create_rail is not None:
                await self._instance.register_rail(self._skill_create_rail)
                logger.info("[JiuWenSwarmDeepAdapter] SkillCreateRail registered for agent mode")
        else:
            # skill_create disabled: unregister if exists
            if self._skill_create_rail is not None:
                await self._instance.unregister_rail(self._skill_create_rail)
                self._skill_create_rail = None
                logger.info("[JiuWenSwarmDeepAdapter] SkillCreateRail unregistered (skill_create=false)")

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

    async def _update_tools_for_mode(
        self, mode: str, session_id: str | None, request_id: str | None
    ) -> None:
        """multi-session 工具装配。

        plan / fast 合并为单一 ``agent`` 模式后，多会话工具
        （session_new / session_cancel / session_list）不再注册：
        清理任何遗留的 session_* 工具后返回。
        """
        # 清理历史遗留的 multi-session 工具（旧 agent.fast 会话切换而来）
        try:
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith(
                    ("session_new", "session_cancel", "session_list")
                ):
                    self._instance.ability_manager.remove(existing.name)
        except Exception as exc:
            logger.debug("[JiuWenSwarmDeepAdapter] 清理 multi-session 工具失败: %s", exc)

    def _ensure_cron_tools_registered(self, session_id: str | None) -> None:
        """Register this agent's cron tools once, rebuilding only when they change.

        The tool instances carry no per-request state: their context object and
        owner id are fixed for the adapter's lifetime, and the target channel is
        read from a contextvar at call time (see ``_bind_runtime_cron_context``).
        Only the language is baked into the instances, so that is the whole
        rebuild condition. Registering them per request instead re-bound eight
        ids in the process-global resource manager every turn, each one a
        remove + add pair that logged a refresh warning.

        Args:
            session_id: Session the current turn belongs to. Heartbeat and cron
                sessions drive the scheduler themselves and get no cron tools.
        """
        if session_id is not None and session_id.startswith(("heartbeat", "cron")):
            return
        if os.getenv("JIUWENCLAW_DISABLE_CRON_TOOLS") == "1":
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "") in _CRON_TOOL_NAMES:
                    self._instance.ability_manager.remove(existing.name)
            self._cron_tools_registered_language = None
            logger.info(
                "[JiuWenSwarmDeepAdapter] skip cron tool registration: disabled by env"
            )
            return
        language = self._resolve_runtime_language()
        registered_names = {
            getattr(existing, "name", "")
            for existing in (self._instance.ability_manager.list() or [])
        }
        # The language fingerprint alone is not enough: rebuilding the agent (a
        # skill or plugin install re-runs ``create_instance``) hands this adapter
        # a fresh, empty AbilityManager while the fingerprint still reads as
        # registered, which would silently drop the cron tools for good.
        if self._cron_tools_registered_language == language and (registered_names & _CRON_TOOL_NAMES):
            return
        try:
            cron_tools = self._build_cron_tools()
            if not cron_tools:
                return
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "") in _CRON_TOOL_NAMES:
                    self._instance.ability_manager.remove(existing.name)
            for cron_tool in cron_tools:
                self._register_agent_owned_tool(cron_tool, self._tool_owner_id())
                self._instance.ability_manager.add(cron_tool.card)
            self._cron_tools_registered_language = language
            logger.info(
                "[JiuWenSwarmDeepAdapter] %d cron tools registered: language=%s",
                len(cron_tools),
                language,
            )
        except Exception as exc:
            logger.error("[JiuWenSwarmDeepAdapter] 定时工具注册失败: %s", exc)

    async def _update_session_tools(
        self,
        session_id: str | None,
        request_id: str | None,
        channel_id: str | None = None,
    ) -> None:
        """刷新每请求相关的 cron / send_file 工具运行时状态。

        cron 工具实例只建一次（见 ``_ensure_cron_tools_registered``）。
        send_file 对齐 test/jiuwenclaw：每次请求卸载旧工具并重建 toolkit，
        避免单例 ``update_runtime_context`` 被并发请求覆盖实例字段。
        """
        self._ensure_cron_tools_registered(session_id)

        # send_file 工具：由 channels.<channel>.send_file_allowed 控制。
        # channel_id/metadata 由调用前的 _bind_runtime_cron_context 已写入 contextvar
        # （含专用 send_file_request_context）。
        config_base = get_config()
        channel = (
            str(channel_id or self._resolve_prompt_channel(session_id) or "web").strip() or "web"
        )
        send_file_enabled = (
            config_base.get("channels", {}).get(channel, {}).get("send_file_allowed")
        )
        # web 默认 True；officeclaw 与 test 仓对齐默认允许；其它 channel 默认 False
        if send_file_enabled is None:
            send_file_enabled = channel in {"web", "officeclaw"}
        agent_runtime_env = is_enterprise()
        if agent_runtime_env:
            send_file_enabled = True
            if self._enterprise_config is not None:
                enterprise_allowed = getattr(
                    self._enterprise_config, "send_file_allowed", None
                )
                if enterprise_allowed is not None:
                    send_file_enabled = bool(enterprise_allowed)
                else:
                    agent_policy = getattr(
                        self._enterprise_config, "agent_policy", None
                    )
                    if isinstance(agent_policy, dict) and "send_file_allowed" in agent_policy:
                        send_file_enabled = bool(agent_policy["send_file_allowed"])
        if send_file_enabled and request_id and session_id:
            channel_for_tool = _CRON_TOOL_CHANNEL_ID.get()
            metadata_for_tool = _CRON_TOOL_METADATA.get()
            # 先卸载上一次请求遗留的 send_file 工具，再按本请求重建。
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith("send_file_to_user"):
                    remove_ability = getattr(
                        self._instance.ability_manager, "remove_ability", None
                    )
                    if callable(remove_ability):
                        remove_ability(existing.name)
                    else:
                        self._instance.ability_manager.remove(existing.name)
            self._send_file_toolkit = SendFileToolkit(
                request_id=request_id,
                session_id=session_id,
                channel_id=channel_for_tool,
                metadata=metadata_for_tool,
            )
            for sf_tool in self._send_file_toolkit.get_tools(tool_id="send_file_to_user"):
                self._register_agent_owned_tool(sf_tool, self._tool_owner_id())
                self._instance.ability_manager.add(sf_tool.card)

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
                # ``remove_ability`` (not ``remove``) so the previous request's
                # instance also leaves the process-global resource manager
                # instead of piling up under a dead id.
                self._instance.ability_manager.remove_ability(existing.name)

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
                self._register_agent_owned_tool(tool, self._tool_owner_id())
                self._instance.ability_manager.add(tool.card)

        if channel_id == "acp":
            ability_names = sorted(self._collect_registered_ability_names())
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

    def _seed_runtime_cwd(
        self, cwd: str | None = None, workspace: str | None = None
    ) -> None:
        """Seed Core's CwdState holder from the request/runtime cwd.

        ``workspace``: optional per-request workspace override. When set,
        becomes the workspace anchor for tools that read ``get_workspace()``
        (notably ``fs_operation``'s sandbox enforcement, which gates
        absolute-path writes by membership in the workspace tree). When
        unset, falls back to the agent's instance-level workspace.
        """
        workspace_root = str(
            collapse_nested_agent_workspace_dir(
                workspace or self._workspace_dir or self._project_dir or os.getcwd()
            )
        )
        runtime_cwd = str(cwd or "").strip()
        if not runtime_cwd or not os.path.isdir(runtime_cwd):
            runtime_cwd = str(self._project_dir or "").strip()
        if not runtime_cwd or not os.path.isdir(runtime_cwd):
            runtime_cwd = workspace_root
        runtime_cwd = str(collapse_nested_agent_workspace_dir(runtime_cwd))
        init_cwd(runtime_cwd, project_root=workspace_root, workspace=workspace_root)

    @dataclass
    class _RuntimeConfig:
        """Per-request runtime config bundle for _update_runtime_config."""

        session_id: str | None = None
        mode: str = "agent"
        request_id: str | None = None
        channel_id: str | None = None
        request_metadata: dict[str, Any] | None = None
        trusted_dirs: list[str] | None = None
        cwd: str | None = None
        workspace: str | None = None
        project_dir: str | None = None
        supports_user_interaction: bool = True
        interactive_ask: bool = False
        request_system_prompt: str | None = None

    @staticmethod
    def _extract_request_system_prompt(request: AgentRequest | None) -> str | None:
        """Read non-empty request.params.system_prompt."""
        if request is None or not isinstance(request.params, dict):
            return None
        raw = request.params.get("system_prompt")
        if isinstance(raw, str):
            value = raw.strip()
            if value:
                return value
        return None

    @staticmethod
    def _extract_request_interactive_ask(request: AgentRequest | None) -> bool:
        """Guided mode is params.interactive_ask opt-in; never infer from HITL capability."""
        from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
            extract_interactive_ask,
        )

        if request is None:
            return False
        params = request.params if isinstance(getattr(request, "params", None), dict) else None
        metadata = request.metadata if isinstance(getattr(request, "metadata", None), dict) else None
        return extract_interactive_ask(params, metadata)

    async def configure_session_runtime(
        self,
        *,
        session_id: str,
        channel_id: str,
        mode: str,
        project_dir: str | None = None,
    ) -> None:
        """Apply session-stable runtime state without request-bound capabilities."""
        await self._apply_runtime_config(
            self._RuntimeConfig(
                session_id=session_id,
                mode=mode,
                request_id=None,
                channel_id=channel_id,
                request_metadata=None,
                project_dir=project_dir,
                cwd=project_dir,
                workspace=project_dir,
                supports_user_interaction=True,
            ),
            bind_request=False,
        )

    async def _update_runtime_config(self, runtime_config: "_RuntimeConfig") -> None:
        """Register per-request tools for current agent execution.

        Runs on every turn and is the bulk of the ``prepare_ms`` reported when
        the message enters the runner, so each step is timed separately: the
        aggregate alone never says which one is slow. The breakdown is logged at
        INFO once the total crosses :data:`_SLOW_RUNTIME_CONFIG_MS`, at DEBUG
        otherwise, and in ``finally`` so a failing turn still reports how far it
        got.

        Args:
            runtime_config: Per-request runtime parameters for this turn.
        """
        await self._apply_runtime_config(runtime_config, bind_request=True)

    async def _apply_runtime_config(
        self,
        runtime_config: "_RuntimeConfig",
        *,
        bind_request: bool,
    ) -> None:
        if self._instance is None:
            raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")

        stage_timer = StageTimer()
        try:
            await self._apply_runtime_config_stages(
                runtime_config,
                stage_timer,
                bind_request=bind_request,
            )
        finally:
            total_ms = stage_timer.total_ms()
            log_runtime_config_stages = _stage_breakdown_logger(
                total_ms, _SLOW_RUNTIME_CONFIG_MS
            )
            log_runtime_config_stages(
                "[AgentServer] runtime config applied: session_id=%s mode=%s total_ms=%.1f %s",
                runtime_config.session_id,
                runtime_config.mode,
                total_ms,
                stage_timer.render(),
            )

    async def _apply_runtime_config_stages(
        self,
        runtime_config: "_RuntimeConfig",
        stage_timer: StageTimer,
        *,
        bind_request: bool,
    ) -> None:
        """Run the per-request runtime setup, marking each stage as it completes.

        Split out of :meth:`_update_runtime_config` so the timing wrapper stays
        readable; the stage sequence itself is unchanged.

        Args:
            runtime_config: Per-request runtime parameters for this turn.
            stage_timer: Timer marked at each stage boundary.
        """
        task_workspace = (
            runtime_config.workspace
            or runtime_config.project_dir
            or self._project_dir
            or str(get_default_project_session_workspace_dir(runtime_config.session_id))
        )
        task_cwd = runtime_config.cwd or task_workspace
        self._seed_runtime_cwd(task_cwd, workspace=task_workspace)
        # Persist paths on StreamEventRail so the interaction round task can
        # rebind openjiuwen CwdState (request-task init_cwd does not propagate).
        if self._stream_event_rail is not None:
            self._stream_event_rail.set_runtime_cwd_paths(
                cwd=task_cwd,
                project_root=runtime_config.project_dir
                or self._project_dir
                or task_workspace,
                workspace=task_workspace,
            )
        resolved_language = self._resolve_runtime_language()
        resolved_channel = (
            str(
                runtime_config.channel_id
                or self._resolve_prompt_channel(runtime_config.session_id)
                or "web"
            ).strip()
            or "web"
        )
        stage_timer.mark("cwd_seed")

        if self._runtime_prompt_rail:
            self._runtime_prompt_rail.set_language(resolved_language)
            self._runtime_prompt_rail.set_channel(resolved_channel)
            self._runtime_prompt_rail.set_trusted_dirs(
                runtime_config.trusted_dirs if bind_request else None
            )
            self._runtime_prompt_rail.set_runtime_paths(
                cwd=task_cwd,
                project_dir=runtime_config.project_dir or self._project_dir,
            )
            self._runtime_prompt_rail.set_model_name(self._resolve_model_name())
            self._runtime_prompt_rail.set_mode(runtime_config.mode)
            self._runtime_prompt_rail.set_session_id(runtime_config.session_id)
            self._runtime_prompt_rail.set_request_system_prompt(
                runtime_config.request_system_prompt if bind_request else None
            )
        if self._response_prompt_rail:
            self._response_prompt_rail.set_channel(resolved_channel)
        # PermissionInterruptRail: per-request trusted_dirs 注入，使 external_directory
        # 检查将这些子树视为 internal 而跳过 ask/deny（与 RuntimePromptRail 对齐）。
        # 用 getattr 兼容绕过 __init__ 的测试构造（_permission_rail 仅在 rail 构建流程赋值）。
        permission_rail = getattr(self, "_permission_rail", None)
        if permission_rail is not None and bind_request:
            try:
                permission_rail.set_trusted_dirs(runtime_config.trusted_dirs)
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] permission_rail.set_trusted_dirs failed",
                    exc_info=True,
                )
        stage_timer.mark("rail_setters")

        self._schedule_runtime_state_write(
            mode=runtime_config.mode,
            language=resolved_language,
            channel=resolved_channel,
            session_id=runtime_config.session_id,
            project_dir=runtime_config.project_dir
            or task_cwd
            or self._project_dir
            or str(get_default_project_session_workspace_dir(runtime_config.session_id)),
        )
        stage_timer.mark("runtime_state")

        await self._update_rails_for_mode(runtime_config.mode)
        stage_timer.mark("rails_for_mode")

        await self._set_user_interaction_enabled(runtime_config.supports_user_interaction)
        stage_timer.mark("user_interaction")

        await self._update_tools_for_mode(
            runtime_config.mode, runtime_config.session_id, runtime_config.request_id
        )
        stage_timer.mark("tools_for_mode")

        await self._update_session_tools(
            runtime_config.session_id,
            runtime_config.request_id,
            channel_id=runtime_config.channel_id,
        )
        stage_timer.mark("session_tools")

        if bind_request:
            self._refresh_acp_runtime_tools(
                runtime_config.session_id,
                runtime_config.request_id,
                runtime_config.channel_id,
                runtime_config.request_metadata,
            )
            # 注入 request_id / channel_id / session_id，供 skill_turbo 等工具从 metadata 直接读取
            try:
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    set_current_request_metadata,
                )
                meta = dict(runtime_config.request_metadata or {})
                # request_id / channel_id 用 runtime_config 权威值兜底（来源 request.request_id /
                # request.channel_id）。wire metadata 缺失这两个字段时（officeclaw 常见），
                # skill_turbo 的 _load_jw_send_file 会因 ctx.request_id/channel_id 为空而注册不了
                # send_file_to_user，导致 PPT 等 skill 产物无法自动发送。
                meta.setdefault("request_id", runtime_config.request_id or "")
                meta.setdefault("channel_id", runtime_config.channel_id or "")
                meta.setdefault("session_id", runtime_config.session_id)
                # effective_project_dir：优先 metadata 的 effective_project_dir，回退 task_workspace。
                # 与 effective_request_workspace_dir ContextVar 对齐，随 metadata 副本转绑到工具执行上下文。
                md_epd = (
                    meta.get("effective_project_dir")
                    if isinstance(meta.get("effective_project_dir"), str)
                    else None
                )
                meta["effective_project_dir"] = (
                    md_epd.strip() if md_epd and md_epd.strip() else task_workspace
                )
                # interactive_ask：引导模式显式 opt-in。不得回退 supports_user_interaction
                # （OfficeClaw 会话 HITL 能力恒为 True，否则未点引导模式也会走大纲审阅）。
                meta["interactive_ask"] = bool(runtime_config.interactive_ask)
                set_current_request_metadata(meta)
                # Align with test/jiuwenclaw: also bind effective workspace ContextVar
                # in the request task (skill_turbo / fork readers). Tool CwdState is
                # rebound separately via StreamEventRail.set_runtime_cwd_paths.
                try:
                    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                        set_effective_request_workspace_dir,
                    )

                    set_effective_request_workspace_dir(meta["effective_project_dir"])
                except Exception:
                    logger.debug(
                        "[AgentServer] set_effective_request_workspace_dir failed",
                        exc_info=True,
                    )
                # 同步副本到 StreamEventRail：工具在 harness 执行任务里运行，
                # 本任务设置的 ContextVar 不跨任务传播，rail 会在 before_tool_call
                # （工具执行上下文）重新绑定，否则 skill_turbo 的 HITL 断点会存到
                # 随机 UUID sid 下、恢复时无法命中（fallback DeepAgent 全量重跑）。
                if self._stream_event_rail is not None:
                    self._stream_event_rail.set_skill_turbo_request_metadata(meta)
            except Exception:
                logger.warning(
                    "[AgentServer] bind request metadata for skill_turbo failed: "
                    "session_id=%s",
                    runtime_config.session_id,
                    exc_info=True,
                )

        stage_timer.mark("acp_tools")

        self._update_prompt_for_mode(runtime_config.mode, resolved_language)
        stage_timer.mark("prompt_for_mode")

        # 处理两种场景的记忆工具移除：
        # 1. 群聊数字分身模式（group_digital_avatar=True + avatar_mode=True）：移除写入工具，但保留读取工具
        # 2. 记忆完全禁用（enable_memory=False + group_digital_avatar=True + avatar_mode=True）：移除所有记忆工具（读取和写入）
        perm_ctx = TOOL_PERMISSION_CONTEXT.get() if bind_request else None
        if perm_ctx is not None:
            # 判断是否为群聊数字分身模式
            is_group_digital_avatar = perm_ctx.group_digital_avatar and perm_ctx.avatar_mode

            # 判断是否为记忆完全禁用（三个条件同时满足）
            should_disable_memory = (
                not perm_ctx.enable_memory
                and perm_ctx.group_digital_avatar
                and perm_ctx.avatar_mode
            )

            # 场景2：记忆完全禁用 - 移除所有记忆工具
            if should_disable_memory:
                _all_memory_tools = (
                    "write_memory",
                    "edit_memory",
                    "read_memory",
                    "memory_search",
                    "memory_get",
                )
                for tool_name in _all_memory_tools:
                    try:
                        self._instance.ability_manager.remove(tool_name)
                        logger.info("[JiuWenSwarmDeepAdapter] 记忆系统已禁用，移除 %s", tool_name)
                    except Exception:
                        pass
            # 场景1：群聊数字分身模式 - 只移除写入工具
            elif is_group_digital_avatar:
                for tool_name in ("write_memory", "edit_memory"):
                    try:
                        self._instance.ability_manager.remove(tool_name)
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] 群聊模式下禁止写入记忆，移除 %s", tool_name
                        )
                    except Exception:
                        pass
            # 非群聊数字分身且记忆启用时，恢复写入工具
            else:
                try:
                    from openjiuwen.core.memory.lite.memory_tools import (
                        get_decorated_tools as _get_sdk_memory_tools,
                    )

                    for tool in _get_sdk_memory_tools():
                        name = getattr(getattr(tool, "card", None), "name", "")
                        if name in ("write_memory", "edit_memory"):
                            self._instance.ability_manager.add(tool.card)
                except ImportError:
                    pass
        stage_timer.mark("memory_tools")

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

    async def start_interaction(self, session_id: str) -> None:
        """Bind a product Session and start this adapter's DeepAgent interaction loop.

        Public entry for the facade/session-pool path. A missing instance or
        failed readiness check propagates so the warm pool cannot publish a
        partially initialized slot.
        """
        if self._instance is None:
            raise RuntimeError("DeepAgent instance is not initialized")
        from openjiuwen.core.session.agent import create_agent_session

        session = create_agent_session(
            session_id=session_id,
            card=getattr(self._instance, "card", None),
        )
        await session.pre_run(inputs={})
        # Bind env overlay so the DeepAgent supervisor task (created by
        # start() via asyncio.create_task) inherits the correct namespace.
        # Without this, the supervisor task's context copy lacks the overlay
        # and reads env from the wrong namespace (default/default).
        ns_token, overlay_token, wk_token = self._bind_request_env_overlay()
        try:
            await self._instance.start(session=session)
        finally:
            self._reset_request_env_bindings(ns_token, overlay_token, wk_token)
        if getattr(self._instance, "_interaction_started", True) is not True:
            raise RuntimeError(f"DeepAgent interaction did not become ready: {session_id}")
        logger.info(
            "[JiuWenSwarmDeepAdapter] start completed: session_id=%s",
            session_id,
        )

    async def prepare_session(
        self,
        *,
        session_id: str,
        channel_id: str,
        mode: str,
        project_dir: str | None = None,
    ) -> None:
        """Create a session child and apply stable runtime state without input."""
        adapter = await self._get_or_create_session_adapter(session_id)
        await adapter.configure_session_runtime(
            session_id=session_id,
            channel_id=channel_id,
            mode=mode,
            project_dir=project_dir,
        )

    @staticmethod
    def _cron_session_has_context(inner_session: Any) -> bool:
        """检测 AgentSession 的 global_state['context'] 是否已带 messages。

        跨 channel（cron 写 / web 读）恢复前用来判断当前 web adapter 的 session 是否
        已经持有 cron 历史——有则跳过 recover，无则强制从 checkpoint 重新恢复。
        """
        try:
            _state = inner_session.state().get_state(copied=False)
            if not isinstance(_state, dict):
                return False
            _ctx = (_state.get("global_state") or {}).get("context")
            if not isinstance(_ctx, dict):
                return False
            for _cst in _ctx.values():
                _msgs = _cst.get("messages") if isinstance(_cst, dict) else None
                if _msgs:
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _persist_cron_checkpoint(self, session_id: str, request_id: str) -> None:
        """非流式 cron 执行收尾补 commit：把 context_engine 内存 context 落盘到 checkpointer。

        根因：cron 走非流式 ``_inner_invoke``，其 commit 挂在 ``need_cleanup`` 下；而 adapter 在
        ``start_interaction`` 已提前绑定 session，``invoke`` 收到现成 session 致 ``need_cleanup=False``，
        commit 被跳过，导致 ``cron_*`` 在 checkpointer 恒为空、web 恢复读不到历史。此处对齐流式路径
        （``_inner_stream`` 的 ``is_agent_session`` 分支会自动 commit），显式补一次落盘。
        姿势参照 ``session_ops_service.rewind_session_context`` 的 save_contexts + post_run。
        """
        try:
            if self._instance is None:
                return
            react_agent = getattr(self._instance, "react_agent", None)
            if react_agent is None:
                return
            context_engine = react_agent.context_engine
            # 无可落盘的 context 则跳过，避免写空状态。
            _ctx = context_engine.get_context(session_id=session_id)
            if _ctx is None:
                return
            try:
                _ctx_msg_count = len(list(_ctx.get_messages() or []))
            except Exception:  # noqa: BLE001
                _ctx_msg_count = 0
            if _ctx_msg_count <= 0:
                return
            # 用运行中的 _interaction_session（而非新建 session）：
            # save_contexts 内部 _save_state_to_session 会把 context messages 存回该 session 的
            # global_state['context']，post_run 再落盘 checkpointer。跨 channel 的 web adapter
            # 复用本会话时会通过上方的 re-recover 从 checkpoint 读回这些 messages。
            session = getattr(self._instance, "_interaction_session", None)
            if session is None:
                logger.warning(
                    "[CRON-CTX] cron checkpoint skip: no active _interaction_session "
                    "session_id=%s request_id=%s",
                    session_id, request_id,
                )
                return
            await context_engine.save_contexts(session)
            await session.post_run()  # → post_agent_execute → _agent_storage.save 落盘
            logger.info(
                "[CRON-CTX] cron checkpoint committed: session_id=%s request_id=%s "
                "msg_count=%s",
                session_id, request_id, _ctx_msg_count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CRON-CTX] cron checkpoint commit failed: session_id=%s "
                "request_id=%s error=%s",
                session_id, request_id, exc,
            )

    async def _persist_session_checkpoint(self, session_id: str, request_id: str) -> None:
        """流式 chat 收尾补 commit：把 context_engine 内存 context 落盘到 checkpointer。

        流式 chat.send 走``_inner_stream``，其 ``session.commit()`` 挂在 ``need_cleanup`` 分支下；
        而 adapter 在 ``start_interaction`` 已提前绑定 session，``_inner_stream`` 收到现成 session 致
        ``need_cleanup=False``，commit 被跳过。于是 ``context_engine`` 内存里本轮新增的对话
        （user/assistant/tool 消息）从未经 ``save_contexts`` 回写到 ``session.global_state['context']``，
        checkpointer 存的 context 停在恢复时的旧值。重启或跨 channel 后，
        下一轮 ``pre_agent_execute`` 从 checkpointer restore 出来的就只有 上轮残留的残缺状态，历史对话丢失，相当于全新会话。
        """
        try:
            if self._instance is None:
                return
            react_agent = getattr(self._instance, "react_agent", None)
            if react_agent is None:
                return
            context_engine = react_agent.context_engine
            # 无可落盘的 context 则跳过，避免写空状态覆盖既有历史。
            _ctx = context_engine.get_context(session_id=session_id)
            if _ctx is None:
                return
            try:
                _ctx_msg_count = len(list(_ctx.get_messages() or []))
            except Exception:  # noqa: BLE001
                _ctx_msg_count = 0
            if _ctx_msg_count <= 0:
                return
            # 用运行中的 _interaction_session（而非新建 session）：
            # save_contexts 内部 _save_state_to_session 会把 context messages 存回该 session 的
            # global_state['context']，commit 再触发 post_agent_execute → _agent_storage.save 落盘。
            # 下一轮 pre_agent_execute 即可从 checkpointer restore 回这些 messages。
            session = getattr(self._instance, "_interaction_session", None)
            if session is None:
                logger.warning(
                    "[STREAM-CTX] checkpoint skip: no active _interaction_session "
                    "session_id=%s request_id=%s",
                    session_id, request_id,
                )
                return
            await context_engine.save_contexts(session)
            await session.commit()  # → post_agent_execute → _agent_storage.save 落盘
            logger.info(
                "[STREAM-CTX] checkpoint committed: session_id=%s request_id=%s "
                "msg_count=%s",
                session_id, request_id, _ctx_msg_count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[STREAM-CTX] checkpoint commit failed: session_id=%s "
                "request_id=%s error=%s",
                session_id, request_id, exc,
            )

    def _init_skill_turbo_tool(self) -> None:
        """Initialize skill_turbo tool for SkillTurbo integration."""
        if self._instance is None:
            return
        try:
            config_base = get_config()
            react_config = config_base.get("react", {}) if isinstance(config_base, dict) else {}
            skill_turbo_config = react_config.get("skill_turbo", {}) if isinstance(react_config, dict) else {}
            enabled = skill_turbo_config.get("enabled", False) if isinstance(skill_turbo_config, dict) else False
            if not enabled:
                logger.info("[JiuWenSwarmDeepAdapter] SkillTurbo disabled, skipping tool registration")
                return

            from openjiuwen.core.runner import Runner as RunnerClass
            from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import get_skill_turbo_tools

            for tool in get_skill_turbo_tools():
                try:
                    RunnerClass.resource_mgr.add_tool(tool)
                except Exception as e:
                    if "already exist" not in str(e):
                        logger.warning("[JiuWenSwarmDeepAdapter] Failed to register skill_turbo tool: %s", e)
                        continue
                self._instance.ability_manager.add(tool.card)

            # 注入 adapter 到 StreamEventRail
            if self._stream_event_rail is not None:
                self._stream_event_rail.set_skill_turbo_adapter(self)
                if self._checkpointer is not None:
                    self._stream_event_rail.set_checkpointer(self._checkpointer)

            logger.info("[JiuWenSwarmDeepAdapter] skill_turbo tool initialized")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] Failed to initialize skill_turbo tool: %s", exc)

    @staticmethod
    def _build_skill_protocol_prompt_rail() -> Any | None:
        """构建 SkillProtocolPromptRail: 注入技能执行规范提示词。"""
        try:
            from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
                SkillProtocolPromptRail,
            )
            rail = SkillProtocolPromptRail()
            logger.info("[JiuWenSwarmDeepAdapter] SkillProtocolPromptRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillProtocolPromptRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_skill_turbo_prompt_rail() -> Any | None:
        """构建 SkillTurboPromptRail: 注入 skill_acceleration_exec 使用指南。

        仅在 config.react.skill_turbo.enabled = true 时创建，否则返回 None。
        """
        try:
            config_base = get_config()
            react_config = config_base.get("react", {}) if isinstance(config_base, dict) else {}
            skill_turbo_config = react_config.get("skill_turbo", {}) if isinstance(react_config, dict) else {}
            enabled = skill_turbo_config.get("enabled", False) if isinstance(skill_turbo_config, dict) else False
            if not enabled:
                return None

            from jiuwenswarm.server.runtime.skill_turbo.rails.skill_prompt_rail import SkillTurboPromptRail
            rail = SkillTurboPromptRail()
            logger.info("[JiuWenSwarmDeepAdapter] SkillTurboPromptRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillTurboPromptRail create failed: %s", exc)
            return None

    # ──────────── SkillTurbo 集成 ────────────

    def build_skill_turbo_config(self) -> dict[str, Any]:
        """构建 SkillTurbo 配置."""
        tool_cards = []
        if self._instance is not None:
            ability_manager = getattr(self._instance, "ability_manager", None)
            if ability_manager is not None:
                tool_cards = ability_manager.list()

        fallback_handler = self._create_skill_turbo_fallback_handler()

        return {
            "skill_codes_dir": "jiuwenswarm.server.runtime.skill_turbo.skill_codes",
            "tool_cards": tool_cards,
            "model_client": self._model,
            "fallback_handler": fallback_handler,
            "card": self._instance.card if self._instance is not None else None,
            "llm_concurrency_limit": 20,
            "vision_model_config": self._vision_model_config,
            "audio_model_config": self._audio_model_config,
            "video_model_enabled": bool(self._video_model_config),
            "image_gen_enabled": bool(self._image_gen_model_config),
            "sys_operation": self._sys_operation,
        }

    def _create_skill_turbo_fallback_handler(self) -> Any:
        """创建 SkillTurbo 节点级 fallback handler。"""
        from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import DeepAgentFallbackHandler

        return DeepAgentFallbackHandler(
            adapter=self,
            request_id="",
            channel_id="",
            session_id="",
        )

    async def spawn_fallback(
        self,
        query: str,
        parent_session: Any | None = None,
    ) -> str:
        """执行 SkillTurbo fallback：以隔离 subagent 跑一次任务，返回子代理输出文本。

        复用 openjiuwen 原生 ``create_subagent("general-purpose", ...)``（继承父 agent
        工具与 SysOperationRail），经 ``invoke_subagent_with_trace`` 阻塞执行。
        fallback 子代理临时禁用权限审批 rail（spawn 结束后恢复共享实例状态），
        避免工具调用中断等待审批导致输出为空。
        抛异常表示子代理执行失败；返回字符串表示已跑完（是否达成节点契约由 handler 判定）。
        """
        from jiuwenswarm.server.runtime.debug_trace import invoke_subagent_with_trace

        parent_session_id = ""
        if parent_session is not None and hasattr(parent_session, "get_session_id"):
            parent_session_id = str(parent_session.get_session_id() or "")
        sub_session_id = (
            f"{parent_session_id or 'skill_turbo'}_skill_turbo_fallback_{uuid.uuid4().hex[:8]}"
        )

        subagent = self._instance.create_subagent("general-purpose", sub_session_id)
        restore_rails = self._suspend_subagent_permission_rails(subagent)
        try:
            result = await invoke_subagent_with_trace(
                subagent,
                inputs={"query": query, "conversation_id": sub_session_id},
                session=parent_session,
                source_label="subagent:skill_turbo_fallback",
            )
        finally:
            restore_rails()

        output = ""
        if isinstance(result, dict):
            output = result.get("output", "") or ""
        return str(output)

    @staticmethod
    def _suspend_subagent_permission_rails(subagent: Any) -> Callable[[], None]:
        """临时禁用 fallback 子代理继承的权限审批 rail，返回恢复函数。

        general-purpose 子代理与父 agent **共享同一个** PermissionInterruptRail 实例
        （factory 注入时按引用复制），直接改共享实例会污染父 agent 后续的权限审批，
        因此采用 suspend/restore：调用方在 spawn 结束后（finally 中）执行恢复。
        ``resolve_interrupt`` 首次工具调用时会用 ``host.get_permissions_snapshot``
        刷新配置覆盖本设置，因此该回调也需临时替换。
        """
        configured = (
            subagent.configured_rails()
            if hasattr(subagent, "configured_rails")
            else []
        )
        rail_names = [rail.__class__.__name__ for rail in configured or []]

        def _disabled_permission_snapshot() -> dict:
            """临时权限审批快照：返回禁用状态，避免工具调用被审批 rail 中断。"""
            return {"enabled": False}

        def _make_restore(
            rail: Any,
            saved_config: dict,
            host: Any,
            saved_snapshot: Any,
        ) -> Callable[[], None]:
            """构造恢复函数，捕获当前循环轮次 rail 的挂起状态。"""

            def _restore() -> None:
                try:
                    rail.update_config(saved_config)
                except Exception:
                    logger.warning(
                        "[spawn_fallback] failed to restore permission rail config",
                        exc_info=True,
                    )
                if host is not None:
                    host.get_permissions_snapshot = saved_snapshot

            return _restore

        restore_steps: list[Callable[[], None]] = []
        for rail in configured or []:
            cls_name = rail.__class__.__name__
            if cls_name not in (
                "PermissionInterruptRail",
                "SkillAuthorizationPermissionRail",
                "SkillTurboPermissionRail",
            ):
                continue
            try:
                saved_config = dict(getattr(rail, "_static_config", None) or {})
                host = getattr(rail, "_host", None)
                saved_snapshot = (
                    host.get_permissions_snapshot if host is not None else None
                )
                rail.update_config({"enabled": False})
                if host is not None:
                    host.get_permissions_snapshot = _disabled_permission_snapshot
                restore_steps.append(_make_restore(rail, saved_config, host, saved_snapshot))
            except Exception:
                logger.warning(
                    "[spawn_fallback] failed to suspend permission rail %s",
                    cls_name,
                    exc_info=True,
                )
        logger.info(
            "[spawn_fallback] subagent rails=%s suspended_permission_rails=%d",
            rail_names,
            len(restore_steps),
        )

        def restore() -> None:
            for step in restore_steps:
                step()

        return restore

    async def prepare_interrupt_artifacts_for_request(
        self, request: AgentRequest
    ) -> None:
        """兜底：中断后下一轮请求注入 SkillTurbo 节点产物摘要到 supplementary_info。

        dev-stable 没有 enterprise_dev 的 plan_pause / interrupt_resume prepare hook 链
        （依赖 openjiuwen.harness.tools.todo_resume，dev-stable 分支无此模块），
        所以这里只做最小子集：读取上一轮 SkillTurbo 中断时保存的节点产物，
        格式化成摘要，注入 request.params['supplementary_info']，让 LLM 知道
        「已完成的工作」而非盲目从头重跑。

        无 plan_pause / interrupt_resume 哨兵机制：dev-stable 不存在那两条路径，
        不会并发注入，故无需去重哨兵。若后续补齐 enterprise_dev 的完整 prepare
        hook 链，应在此处开头加 is_interrupt_recovery_injected(session) 早返回，
        避免重复注入。
        """
        if self._instance is None:
            return

        session_id = str(request.session_id or "").strip()
        if not session_id:
            return

        params = (
            request.params
            if isinstance(getattr(request, "params", None), dict)
            else None
        )
        if params is None:
            return

        from openjiuwen.core.session.agent import create_agent_session

        # SkillTurbo 节点产物存在独立 __skill_turbo checkpointer key 下，
        # 需用单独的 session 读写（与 _try_skill_turbo_resume 同一套 id 机制）。
        skill_turbo_session = create_agent_session(
            session_id=session_id, card=self._instance.card,
        )
        _skill_turbo_set_agent_id(skill_turbo_session, self._instance.card)
        # pre_run 在 try 内部：失败时直接 return，但仍走 finally 的 post_run，
        # 避免 checkpointer 状态泄漏（与 load_resume_ctx 的 pre_run 包裹策略一致）。
        try:
            await skill_turbo_session.pre_run(inputs=None)
            summary = await self._read_skill_turbo_node_artifacts_summary(skill_turbo_session)
            if not summary:
                return

            # 构建中断恢复提示词（内联，避免依赖 plan_pause_helpers）。
            language = self._resolve_runtime_language()
            template = (
                "[Interrupt recovery hint]\n"
                "The previous task was interrupted and cancelled. "
                "Here is a summary of completed work artifacts before the interruption:\n\n"
                "{summary}\n\n"
                "Based on this, judge the current task state:\n"
                "- If artifacts show the target file already exists with substantial content, "
                "read_file first before deciding to supplement or rebuild from scratch\n"
                "- If artifacts show the target file was not created or has minimal content, "
                "you may create it anew\n"
                "- Do not blindly write_file to rebuild a file that already exists and is complete"
            ) if language in ("en", "english") else (
                "【中断恢复提示】之前的任务被中断取消。"
                "以下是中断前已完成的工作产物摘要：\n\n"
                "{summary}\n\n"
                "请据此判断当前任务状态：\n"
                "- 如果产物显示目标文件已存在且内容较完整，请先 read_file 查看当前状态，"
                "再决定是补充完善还是从头重建\n"
                "- 如果产物显示目标文件尚未创建或内容很少，可以重新创建\n"
                "- 不要盲目从头 write_file 重建一个已存在的完整文件"
            )
            prompt = template.format(summary=summary.strip() or "(empty)")

            # 注入到 supplementary_info（与 enterprise_dev merge_supplementary_into_request_params
            # 行为一致：已存在则追加，不存在则设置）。
            supplementary = prompt.strip()
            if not supplementary:
                return
            existing = params.get("supplementary_info")
            if isinstance(existing, str) and existing.strip():
                params["supplementary_info"] = f"{existing.strip()}\n\n{supplementary}"
            else:
                params["supplementary_info"] = supplementary

            # 一次性使用：注入后清除 SkillTurbo 节点产物记录，避免下一轮再次注入。
            from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import (
                clear_node_artifacts,
            )
            await clear_node_artifacts(skill_turbo_session)

            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillTurbo interrupt artifacts summary "
                "injected session=%s",
                session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] prepare_interrupt_artifacts_for_request failed "
                "session_id=%s: %s",
                session_id,
                exc,
                exc_info=True,
            )
        finally:
            try:
                await skill_turbo_session.post_run()
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] prepare_interrupt_artifacts post_run failed",
                    exc_info=True,
                )

    @staticmethod
    async def _read_skill_turbo_node_artifacts_summary(session: Any) -> str | None:
        """读取 SkillTurbo 节点产物记录，格式化为可读摘要文本。"""
        from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import (
            load_node_artifacts,
        )
        state = await load_node_artifacts(session)
        if not state:
            return None
        nodes = state.get("nodes") or {}
        skill = state.get("skill", "unknown")
        summaries = JiuWenSwarmDeepAdapter._build_skill_turbo_artifacts_summary(nodes)
        if not summaries:
            return None
        lines = [f"[SkillAccelerationExec ({skill}) 已完成节点产物]"] + summaries
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillTurbo node artifacts found session=%s nodes=%d",
            getattr(session, "session_id", "?"),
            len(nodes),
        )
        return "\n".join(lines)

    @staticmethod
    def _build_skill_turbo_artifacts_summary(nodes: dict[str, Any]) -> list[str]:
        """将节点产物 nodes 构建为可读摘要列表。"""
        summaries: list[str] = []
        for plan_name, node_info in nodes.items():
            if not isinstance(node_info, dict):
                continue
            parts: list[str] = []
            info = node_info.get("info")
            if isinstance(info, dict) and info:
                parts.append(", ".join(
                    f"{k}={v}" for k, v in info.items() if v is not None
                ))
            files = node_info.get("files")
            if isinstance(files, list) and files:
                parts.append("文件: " + ", ".join(
                    f.get("path", "") for f in files
                    if isinstance(f, dict) and f.get("path")
                ))
            if parts:
                summaries.append(f"- {plan_name}: {' | '.join(parts)}")
        return summaries

    @staticmethod
    def _new_usage_accumulator() -> dict[str, Any]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_tokens": 0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }

    @staticmethod
    def _extract_usage_metadata(payload: Any) -> dict[str, Any] | None:
        """Pull GLM/OpenAI usage numbers out of a SkillTurbo or adapter payload."""
        if not isinstance(payload, dict):
            return None
        event_type = str(payload.get("event_type") or "")
        if event_type == "chat.usage_metadata":
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                inner = metadata.get("usage_metadata", metadata)
                return inner if isinstance(inner, dict) else None
            return None
        if event_type in {"chat.llm_usage", "llm_usage"}:
            inner = payload.get("usage_metadata")
            return inner if isinstance(inner, dict) else None
        return None

    @staticmethod
    def _accumulate_usage(acc: dict[str, Any], usage_meta: dict[str, Any]) -> None:
        for token in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
            acc[token] += usage_meta.get(token, 0) or 0
        for cost in ("input_cost", "output_cost", "total_cost"):
            acc[cost] += usage_meta.get(cost, 0.0) or 0.0

    @staticmethod
    def _extract_deepresearch_sdk_usage(
        payload: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(payload, dict):
            return None
        result_info = payload.get("tool_result", payload)
        if not isinstance(result_info, dict):
            return None
        tool_name = result_info.get("tool_name") or result_info.get("name")
        if tool_name != "deepresearch_execute":
            return None
        raw_output = result_info.get("raw_output")
        if raw_output is None:
            raw_output = result_info.get("rawOutput")
        if raw_output is None:
            raw_output = result_info.get("result")
        if isinstance(raw_output, str):
            try:
                raw_output = json.loads(raw_output)
            except (TypeError, ValueError):
                return None
        if not isinstance(raw_output, dict) or raw_output.get("kind") != "completed":
            return None
        usage = normalize_workflow_llm_token_usage(
            raw_output.get("workflow_llm_token_usage")
        )
        if usage is None or max(
            usage["total_tokens"],
            usage["input_tokens"] + usage["output_tokens"],
        ) <= 0:
            return None
        state = raw_output.get("state")
        conversation_id = (
            raw_output.get("conversation_id")
            or (state.get("conversation_id") if isinstance(state, dict) else None)
            or result_info.get("tool_call_id")
            or result_info.get("toolCallId")
        )
        usage_id = str(conversation_id or "").strip()
        if not usage_id:
            return None
        return usage_id, usage

    @classmethod
    def _account_deepresearch_sdk_usage(
        cls,
        payload: Any,
        *,
        request_id: str,
        usage_accumulator: dict[str, Any],
        accounted_usage_ids: set[str],
    ) -> bool:
        extracted = cls._extract_deepresearch_sdk_usage(payload)
        if extracted is None:
            return False
        usage_id, usage = extracted
        if usage_id in accounted_usage_ids:
            return False
        accounted_usage_ids.add(usage_id)
        cls._accumulate_usage(usage_accumulator, usage)
        record_deepresearch_sdk_token_usage(request_id, usage_id, usage)
        return True

    @staticmethod
    def _rewrite_skill_turbo_usage_chunk(
        chunk: AgentResponseChunk,
        *,
        session_id: str,
    ) -> tuple[AgentResponseChunk | None, dict[str, Any] | None]:
        """Map SkillTurbo ``chat.llm_usage`` onto the adapter's usage_metadata event.

        HITL resume yields AgentResponseChunk payloads directly and never passes
        through the DeepAgent ``type='llm_usage'`` consumer. Testers grep
        ``chat.usage_metadata`` / ``chat.usage_summary``, not ``chat.llm_usage``.
        """
        payload = chunk.payload
        usage_meta = JiuWenSwarmDeepAdapter._extract_usage_metadata(payload)
        if usage_meta is None:
            return None, None
        if (
            isinstance(payload, dict)
            and payload.get("event_type") == "chat.usage_metadata"
        ):
            return chunk, usage_meta
        return (
            AgentResponseChunk(
                request_id=chunk.request_id,
                channel_id=chunk.channel_id,
                payload={
                    "event_type": "chat.usage_metadata",
                    "metadata": payload,
                    "session_id": session_id,
                },
                is_complete=False,
                agent_ref=chunk.agent_ref,
                metadata=chunk.metadata,
            ),
            usage_meta,
        )

    @staticmethod
    def _format_llm_usage_summary(usage_accumulator: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "input_tokens": usage_accumulator["input_tokens"],
            "output_tokens": usage_accumulator["output_tokens"],
            "total_tokens": usage_accumulator["total_tokens"],
        }
        input_tokens = usage_accumulator["input_tokens"]
        if input_tokens > 0:
            cache_tokens = usage_accumulator["cache_tokens"]
            summary["cache_tokens"] = cache_tokens
            summary["cache_hit_rate"] = f"{cache_tokens / input_tokens:.1%}"
        if usage_accumulator["input_cost"] > 0:
            summary["input_cost"] = round(usage_accumulator["input_cost"], 6)
        if usage_accumulator["output_cost"] > 0:
            summary["output_cost"] = round(usage_accumulator["output_cost"], 6)
        if usage_accumulator["total_cost"] > 0:
            summary["total_cost"] = round(usage_accumulator["total_cost"], 6)
        return summary

    def _context_usage_fields(self, session_id: str) -> tuple[float | None, int | None]:
        context_usage_percent: float | None = None
        context_window_tokens: int | None = None
        try:
            if self._instance is not None:
                da_usage = self._instance.get_context_usage(session_id=session_id)
                if isinstance(da_usage, dict):
                    raw_pct = da_usage.get("usage_percent", None)
                    if raw_pct is not None:
                        context_usage_percent = float(raw_pct)
                    raw_cw = da_usage.get("context_window_tokens", None)
                    if raw_cw is not None:
                        context_window_tokens = int(raw_cw)
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] DeepAgent.get_context_usage in usage_summary failed",
                exc_info=True,
            )
        if context_window_tokens is None:
            try:
                from openjiuwen.core.context_engine.context.context_utils import ContextUtils
                model_name = (
                    getattr(self._model_request_config, "model_name", "") or ""
                    if self._model_request_config else ""
                )
                cw_fallback = ContextUtils.resolve_context_max(model_name=model_name)
                if cw_fallback > 0:
                    context_window_tokens = cw_fallback
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] ContextUtils.resolve_context_max fallback failed",
                    exc_info=True,
                )
        return context_usage_percent, context_window_tokens

    def _log_and_make_usage_summary_chunk(
        self,
        *,
        request_id: str,
        channel_id: str,
        session_id: str,
        usage_accumulator: dict[str, Any],
        perf_usage_fallback: dict[str, int] | None = None,
    ) -> AgentResponseChunk | None:
        if merge_perf_summary_usage_fallback(usage_accumulator, perf_usage_fallback):
            logger.info(
                "[JiuWenSwarmDeepAdapter] recovered missing llm_usage from perf summary: "
                "request_id=%s session_id=%s usage=%s",
                request_id,
                session_id,
                perf_usage_fallback,
            )
        summary = self._format_llm_usage_summary(usage_accumulator)
        logger.info(
            "[JiuWenSwarmDeepAdapter] llm_usage summary: request_id=%s session_id=%s usage=%s",
            request_id,
            session_id,
            summary,
        )
        if usage_accumulator["total_tokens"] <= 0:
            return None
        payload: dict[str, Any] = {
            "event_type": "chat.usage_summary",
            "session_id": session_id,
            "usage": summary,
            "model": self._resolve_model_name(),
        }
        context_usage_percent, context_window_tokens = self._context_usage_fields(session_id)
        if context_usage_percent is not None:
            payload["usage_percent"] = context_usage_percent
        if context_window_tokens is not None:
            payload["context_window_tokens"] = context_window_tokens
        return AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload=payload,
            is_complete=False,
        )

    def _deep_agent_has_skill_turbo_interrupt(self) -> bool:
        """True when DeepAgent still holds an outer skill_acceleration_exec HITL."""
        loop_session = getattr(getattr(self, "_instance", None), "_loop_session", None)
        if loop_session is None:
            return False
        try:
            state = loop_session.get_state(INTERRUPTION_KEY)
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] inspect skill_turbo interrupt failed; "
                "deferring resume to DeepAgent",
                exc_info=True,
            )
            return True
        interrupted = getattr(state, "interrupted_tools", None)
        if not isinstance(interrupted, dict) or not interrupted:
            return False
        return any(
            getattr(getattr(entry, "tool_call", None), "name", None)
            == "skill_acceleration_exec"
            for entry in interrupted.values()
        )

    async def _try_skill_turbo_resume(
        self,
        request: AgentRequest,
        inputs: dict[str, Any],
    ) -> AsyncIterator[AgentResponseChunk] | None:
        """检测 resume 请求并走 SkillTurbo resume 路径。"""
        params = request.params if isinstance(getattr(request, "params", None), dict) else {}
        answers: list = params.get("answers") or []
        if not answers:
            return None
        if str(params.get("source") or "").strip() != "ask_user_interrupt":
            return None
        if self._instance is None:
            return None
        if self._deep_agent_has_skill_turbo_interrupt():
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillTurbo resume deferred to DeepAgent "
                "(outer skill_acceleration_exec interrupt present)"
            )
            return None
        from openjiuwen.core.session.agent import create_agent_session

        session = create_agent_session(
            session_id=request.session_id or "default",
            card=self._instance.card,
        )
        _skill_turbo_set_agent_id(session, self._instance.card)
        resume_ctx = await _skill_turbo_load_resume_ctx(session)
        if resume_ctx is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillTurbo resume requested but resume_ctx is None; "
                "falling back to DeepAgent. session_id=%s",
                request.session_id,
            )
            try:
                await session.post_run()
            except Exception:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] skill_turbo resume post_run failed",
                    exc_info=True,
                )
            return None
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillTurbo resume detected: tcid=%s",
            resume_ctx.get("pending_tool_call_id"),
        )
        try:
            session.update_state({INTERRUPTION_KEY: None})
        except Exception as exc:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] clear ToolInterruptionState failed: %s", exc
            )
        if inputs is None:
            inputs = {}
        return self._make_skill_turbo_resume_stream(
            request=request,
            inputs=inputs,
            session=session,
            resume_ctx=resume_ctx,
            answers=answers,
        )

    def _make_skill_turbo_resume_stream(
        self,
        request: AgentRequest,
        inputs: dict[str, Any],
        session: Any,
        resume_ctx: dict[str, Any],
        answers: list,
    ) -> AsyncIterator[AgentResponseChunk] | None:
        """构造 resume 的流式 AsyncIterator。"""
        from jiuwenswarm.server.runtime.skill_turbo.agent import (
            SkillTurbo,
            SkillTurboNotHandled,
        )

        async def _resume_impl() -> AsyncIterator[AgentResponseChunk]:
            token_trace_sid = _LLM_TRACE_SESSION_ID.set(request.session_id or "default")
            token_trace_rid = _LLM_TRACE_REQUEST_ID.set(request.request_id or "")
            token_trace_iter = _LLM_TRACE_ITERATION.set(0)
            token_trace_model = _LLM_TRACE_MODEL_NAME.set(
                getattr(self, "_model", None)
                and getattr(self._model, "model_config", None)
                and getattr(self._model.model_config, "model_name", "")
                or ""
            )
            skill_turbo = SkillTurbo(self.build_skill_turbo_config())
            user_input = self._skill_turbo_answers_to_confirm_payload(answers, resume_ctx)
            params = request.params or {}
            raw_interactive = params.get(
                "interactive_ask", params.get("interactiveAsk")
            )
            from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
                apply_interactive_ask_to_inputs,
                resolve_resume_interactive_ask,
            )
            # 入站 answers payload 一般不携带 interactive_ask；此时从中断点
            # resume_ctx 恢复中断前的引导模式状态，避免 resume 后被强制
            # 设为 False、把带 preview 的内容确认类 ask_user 误判为 skipped，
            # 或让引导模式管线在 resume 阶段退化为非引导。
            raw_interactive = resolve_resume_interactive_ask(
                raw_interactive, resume_ctx.get("inputs")
            )
            resume_inputs = apply_interactive_ask_to_inputs(
                resume_ctx.get("inputs", inputs),
                raw_interactive,
            )
            usage_accumulator = self._new_usage_accumulator()
            usage_summary_emitted = False
            session_id = request.session_id or "default"
            rid = request.request_id
            cid = request.channel_id

            async def _emit_usage_summary() -> AsyncIterator[AgentResponseChunk]:
                nonlocal usage_summary_emitted
                if usage_summary_emitted:
                    return
                usage_summary_emitted = True
                summary_chunk = self._log_and_make_usage_summary_chunk(
                    request_id=rid,
                    channel_id=cid,
                    session_id=session_id,
                    usage_accumulator=usage_accumulator,
                    perf_usage_fallback=snapshot_perf_summary_usage(rid),
                )
                if summary_chunk is not None:
                    yield summary_chunk

            async def _clear_resume_ctx() -> None:
                await _skill_turbo_clear_resume_ctx(session)
                try:
                    await session.post_run()
                except Exception:
                    logger.debug(
                        "[JiuWenSwarmDeepAdapter] skill_turbo resume_stream post_run failed",
                        exc_info=True,
                    )

            def _finish_text(success: bool, detail: str = "") -> str:
                # Fallback only (no DeepAgent interrupt): template text, not a new LLM call.
                from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
                    _build_artifact_summary,
                )
                summary = _build_artifact_summary(
                    getattr(skill_turbo, "artifact_holder", None) or {}
                )
                head = detail or ("任务已完成" if success else "任务未完成")
                if summary:
                    return f"{head}\n\n{summary}"
                return head

            finish_text = ""
            try:
                async for chunk in skill_turbo.resume_stream(
                    plan_code=resume_ctx["plan_code"],
                    inputs=resume_inputs,
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    pending_tool_call_id=resume_ctx["pending_tool_call_id"],
                    user_input=user_input,
                    task_states=resume_ctx.get("task_states"),
                ):
                    rewritten, usage_meta = self._rewrite_skill_turbo_usage_chunk(
                        chunk, session_id=session_id
                    )
                    if rewritten is not None and usage_meta is not None:
                        self._accumulate_usage(usage_accumulator, usage_meta)
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] llm_usage chunk: %s",
                            rewritten.payload,
                        )
                        yield rewritten
                        continue
                    if getattr(chunk, "is_complete", False):
                        async for summary_chunk in _emit_usage_summary():
                            yield summary_chunk
                        continue
                    yield chunk
                await _clear_resume_ctx()
                finish_text = _finish_text(True)
            except _SkillTurboAbortError as e:
                async for summary_chunk in _emit_usage_summary():
                    yield summary_chunk
                async for hitl_chunk in self._emit_skill_turbo_hitl_chunks(
                    request, e
                ):
                    yield hitl_chunk
                return
            except SkillTurboNotHandled as exc:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] SkillTurbo resume not handled: %s",
                    exc,
                )
                await _clear_resume_ctx()
                finish_text = _finish_text(False, f"SkillAccelerationExec 未处理: {exc}")
            finally:
                _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
                _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
                _LLM_TRACE_ITERATION.reset(token_trace_iter)
                _LLM_TRACE_MODEL_NAME.reset(token_trace_model)

            async for summary_chunk in _emit_usage_summary():
                yield summary_chunk
            if finish_text:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.delta", "content": finish_text},
                    is_complete=False,
                )
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={"event_type": "chat.final", "content": finish_text or ""},
                is_complete=True,
            )

        return _resume_impl()

    @staticmethod
    def _skill_turbo_answers_to_confirm_payload(
        answers: list,
        resume_ctx: dict[str, Any],
    ) -> Any:
        """将前端 answers 转为 ConfirmPayload（权限审批）或结构化 answers（ask_user）。

        结构化 ask_user 中断（SkillTurboAskUserRail）：前端 ask_user_question 卡片
        提交 ``{question, selected_options, custom_input}``，原样返回给 rail 解析；
        权限审批中断：保持原逻辑转为 ConfirmPayload。
        """
        if answers and all(
            isinstance(a, dict) and ("question" in a or "selected_options" in a)
            for a in answers
        ):
            return answers
        text = ""
        for ans in answers:
            if isinstance(ans, dict):
                a = ans.get("answer") or ans.get("value") or ""
                if a:
                    text = str(a).strip()
                    break
        if not text and answers:
            first = answers[0]
            if isinstance(first, str):
                text = first
        if text == "拒绝":
            return _SkillTurboConfirmPayload(approved=False, feedback="user rejected")
        if text == "总是允许":
            return _SkillTurboConfirmPayload(
                approved=True, auto_confirm=True, persist_allow=True
            )
        return _SkillTurboConfirmPayload(approved=True)

    @staticmethod
    async def _emit_skill_turbo_hitl_chunks(
        request: AgentRequest,
        abort_exc: Any,
    ) -> AsyncIterator[AgentResponseChunk]:
        """AbortError → HITL 三件套 chunk。"""
        tic = _skill_turbo_extract_tool_interrupt(abort_exc)
        if tic is None:
            raise abort_exc
        tc_data = {
            "id": tic.tool_call.id if tic.tool_call else "",
            "name": tic.tool_call.name if tic.tool_call else "",
            "arguments": tic.tool_call.arguments if tic.tool_call else {},
        }
        rid = request.request_id
        cid = request.channel_id
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=cid,
            payload={
                "event_type": "chat.tool_call",
                "tool_call": tc_data,
                "status": "pending_approval",
                "source": "permission_interrupt",
            },
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=cid,
            payload={
                "event_type": "chat.tool_update",
                "tool_name": tc_data.get("name", ""),
                "tool_call_id": tc_data.get("id", ""),
                "arguments": tc_data.get("arguments", {}),
                "status": "in_progress",
                "source": "permission_interrupt",
            },
            is_complete=False,
        )
        interaction_output = _skill_turbo_build_interaction_output(abort_exc)
        if interaction_output is not None:
            try:
                raw_interaction = getattr(interaction_output, "payload", interaction_output)
                ask_payload = convert_interactions_to_ask_user_question([
                    raw_interaction
                ])
                if ask_payload:
                    if isinstance(ask_payload, dict):
                        ask_payload["request_id"] = rid
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=ask_payload,
                        is_complete=False,
                    )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] HITL ask_payload build failed: %s", exc,
                )
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=cid,
            payload={
                "event_type": "chat.invocation_paused",
                "awaiting_user_input": True,
            },
            is_complete=True,
        )

    async def stop_interaction(self) -> None:
        """Stop this adapter's DeepAgent interaction loop if it was started."""
        if self._instance is None:
            return
        await self._instance.stop()

    async def cleanup(self) -> None:
        """Release adapter-owned external runtime resources."""
        if not self._is_session_scoped_adapter:
            for adapter in list(self._session_adapters.values()):
                try:
                    await adapter.stop_interaction()
                    await adapter.cleanup()
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] session adapter cleanup failed: %s",
                        exc,
                    )
            self._session_adapters.clear()
            self._session_adapter_locks.clear()
            self._session_adapter_last_used.clear()
            self._session_adapter_versions.clear()
            self._session_adapter_reload_failures.clear()
        else:
            try:
                await self.stop_interaction()
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] stop failed during cleanup: %s",
                    exc,
                )
            try:
                from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
                    clear_sent_files_for_session,
                )

                clear_sent_files_for_session(self._parent_session_id)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] send_file dedup cleanup failed: session_id=%s error=%s",
                    self._parent_session_id,
                    exc,
                )
            clear_session_skill_state(str(self._parent_session_id or "").strip())
        self._teardown_agent_owned_tools()
        self._release_sys_operations()
        # 取消未到期的延时重索引 task，避免 adapter cleanup 后仍有孤儿 task
        # 去触发 manager.sync（此时 rail/manager 可能已失效）。
        if self._memory_reindex_task is not None and not self._memory_reindex_task.done():
            self._memory_reindex_task.cancel()
        self._memory_reindex_task = None
        await self._close_a2x_client()

    def _teardown_agent_owned_tools(self) -> None:
        """Drop this agent's stateful tool registrations from the global resource manager.

        ``Runner.resource_mgr`` is process-global, so without this a disposed
        adapter leaves its per-agent tool instances behind: the next adapter
        re-registers over the stale ids (one refresh warning per tool) and the
        residual instances keep holding a stale SysOperation reference. Shared
        singletons and externally-scoped ids are left alone.
        """
        if self._instance is None:
            return
        ability_manager = getattr(self._instance, "ability_manager", None)
        if ability_manager is None:
            return
        try:
            ability_manager.teardown_tools()
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] agent tool teardown failed: %s", exc
            )

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
    def _resolve_interrupt_session_id(session_id: str | None) -> str:
        return (session_id or "default").strip() or "default"

    async def _stop_session_interrupt_work(
        self,
        session_id: str | None,
        *,
        intent: str,
        reset_for_new_task: bool = False,
    ) -> list[dict[str, Any]]:
        """Per-session teardown: rail abort, shell kill, cancelled tool collection."""
        sid = self._resolve_interrupt_session_id(session_id)
        cancelled_tasks = await self._cancel_session_agent_tasks(sid)
        cancelled_tool_results = self._collect_cancelled_tools_for_session(
            session_id,
            reset_for_new_task=reset_for_new_task,
        )
        try:
            from openjiuwen.core.sys_operation.shell_process_registry import (
                kill_shell_processes_for_session_tree,
            )

            killed = kill_shell_processes_for_session_tree(sid)
            if killed:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt(%s): killed %d shell process(es) session=%s",
                    intent,
                    killed,
                    sid,
                )
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): kill_shell_processes failed",
                intent,
                exc_info=True,
            )
        if cancelled_tasks:
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): cancelled %d agent task(s) session=%s",
                intent,
                cancelled_tasks,
                sid,
            )
        return cancelled_tool_results

    def _collect_cancelled_tools_for_session(
        self,
        session_id: str | None,
        *,
        reset_for_new_task: bool = False,
    ) -> list[dict[str, Any]]:
        """Abort rail checkpoints and collect in-flight tools for *session_id*.

        Does not cancel asyncio stream producer tasks — safe for interaction
        cancel, which must keep owning the round via ``cancel_round``.
        """
        if self._stream_event_rail is None:
            return []
        sid = self._resolve_interrupt_session_id(session_id)
        self._stream_event_rail.abort(session_id or sid)
        self._stream_event_rail.collect_cancelled_tool_updates(session_id or sid)
        cancelled_tool_results = self._stream_event_rail.get_cancelled_tool_results(
            session_id or sid,
        )
        self._stream_event_rail.clear_cancelled_tool_results(session_id or sid)
        if reset_for_new_task:
            self._stream_event_rail.reset_for_new_task(session_id or sid)
        return cancelled_tool_results

    @staticmethod
    def _append_cancelled_tools_to_history(
        request: AgentRequest,
        cancelled_tool_results: list[dict[str, Any]],
    ) -> None:
        """Persist cancelled tool results so refresh does not leave spinners."""
        if not cancelled_tool_results:
            return
        mode = (
            request.params.get("mode", "unknown")
            if isinstance(request.params, dict)
            else "unknown"
        )
        for tool_info in cancelled_tool_results:
            append_history_record(
                session_id=request.session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="assistant",
                event_type="chat.tool_result",
                content=tool_info.get("result", ""),
                timestamp=time.time(),
                extra={
                    "tool_result": {
                        "tool_name": tool_info.get("tool_name", ""),
                        "tool_call_id": tool_info.get("tool_call_id", ""),
                        "result": tool_info.get("result", ""),
                        "status": tool_info.get("status", "error"),
                    },
                },
                mode=mode,
            )

    def _has_active_goal_round(self) -> bool:
        """Whether DeepAgent is currently executing a goal round.

        A goal round is a session-level persistent objective that must survive
        stray stream cancellations (``stream_cancel``) and ``chat.interrupt``.
        It is controlled exclusively via ``/goal pause | clear``, never by a
        generic abort — otherwise a new chat.send (which the frontend precedes
        with chat.interrupt cancel) would tear down the goal producer.
        """
        if self._instance is None:
            return False
        if not self._instance_interaction_started():
            return False
        active = self._instance.active_round
        return active is not None and getattr(active, "run_kind", None) == "goal"

    def _instance_interaction_started(self) -> bool:
        """Prefer the public ``interaction_started`` flag; fall back for older SDKs."""
        if self._instance is None:
            return False
        started = getattr(self._instance, "interaction_started", None)
        if isinstance(started, bool):
            return started
        return bool(getattr(self._instance, "_interaction_started", False))

    def _goal_record_is_active(self) -> bool:
        """Whether GoalRecord is ACTIVE (persistent objective still running).

        Unlike ``_has_active_goal_interaction``, this ignores an in-flight goal
        round.  Used when deciding whether to demote ``chat.final``: after user
        cancel/pause the record is no longer ACTIVE, so a terminal final must
        reach the frontend even while the aborted round is still unwinding.
        """
        if self._instance is None:
            return False
        manager = getattr(self._instance, "goal_manager", None)
        if manager is None:
            return False
        try:
            peek = getattr(manager, "peek", None)
            record = peek() if callable(peek) else manager.get_store().load()
        except Exception:
            logger.debug("[Goal] failed to inspect goal record status", exc_info=True)
            return False
        status = getattr(record, "status", None)
        status_value = getattr(status, "value", status)
        return status_value == "active"

    def _has_active_goal_interaction(self) -> bool:
        """Whether the shared DeepAgent still owns an active goal interaction."""
        if self._has_active_goal_round():
            return True
        return self._goal_record_is_active()

    def _should_demote_goal_intermediate_final(self) -> bool:
        """Whether an attempt-boundary ``chat.final`` must become intermediate.

        Demote only when GoalRecord is still ACTIVE **and** the text being
        closed belonged to a **goal** round.

        Prefer ``_stream_content_run_kind`` (stamped while forwarding deltas)
        over the live ``active_round``: a user-round final can still be in the
        queue after the scheduler has already started the next goal round.
        Falling back to ``_has_active_goal_round()`` covers goal attempts that
        emit a final with no prior delta on this consumer.
        """
        if not self._goal_record_is_active():
            return False
        content_kind = self._stream_content_run_kind
        if content_kind is not None:
            return content_kind == "goal"
        return self._has_active_goal_round()

    def _current_interaction_run_kind(self) -> str | None:
        if self._instance is None or not self._instance_interaction_started():
            return None
        active = self._instance.active_round
        if active is None:
            return None
        kind = getattr(active, "run_kind", None)
        if kind is None:
            return None
        return str(getattr(kind, "value", kind))

    def _begin_visible_chat_content(
        self, stream_is_user_originated: bool = False
    ) -> dict[str, Any] | None:
        """Stamp content provenance; inject a bubble-split final on user→goal.

        When the shared stream switches from user-round text to goal-round text
        without a terminal ``chat.final`` in between, emit an empty final so the
        frontend can ``stopStreaming`` and open a new bubble.

        ``stream_is_user_originated`` marks a *plain user chat* consumer stream
        (i.e. not a ``command.goal`` / attach-goal stream). Such a stream can be
        hijacked by a goal round **before** the user round emits its first
        visible token (slow first token + an early goal insert). In that timing
        ``prev`` is still ``None`` yet a boundary final is still required, so the
        goal answer opens its own bubble instead of merging into the (empty) user
        bubble. Non-user-originated streams keep the strict ``prev == "user"``
        rule so a pure goal stream never gets a spurious final at its head.
        """
        kind = self._current_interaction_run_kind()
        prev = self._stream_content_run_kind
        boundary: dict[str, Any] | None = None
        switched_from_user = prev == "user" and kind == "goal"
        hijacked_before_user_token = (
            stream_is_user_originated and kind == "goal" and prev is None
        )
        if switched_from_user or hijacked_before_user_token:
            boundary = {"event_type": "chat.final", "content": ""}
            self._stream_content_run_kind = None
        if kind is not None:
            self._stream_content_run_kind = kind
        return boundary

    def _adapt_goal_intermediate_final(self, parsed: dict | None) -> dict | None:
        if not isinstance(parsed, dict):
            return parsed
        if parsed.get("event_type") != "chat.final":
            return parsed
        if not self._should_demote_goal_intermediate_final():
            return parsed
        adapted = dict(parsed)
        adapted["event_type"] = "chat.delta"
        adapted["goal_intermediate"] = True
        return adapted

    def _should_emit_stream_end_chat_final(
        self,
        *,
        had_assistant_output: bool,
        emitted_terminal_chat_final: bool,
    ) -> bool:
        """Whether the host must synthesize a terminal ``chat.final``.

        pause→clear (and similar) cancels the in-flight goal round so the
        interaction iterator ends normally, but often without a model
        ``chat.final``. Gateway Goal streams also skip
        ``processing_status=false``.

        Emit even when no assistant tokens were forwarded yet: the frontend
        may already be ``isProcessing`` from goal.set before the first
        delta/reasoning arrives; without a final the stop control stays stuck.
        ``had_assistant_output`` is retained for call-site/tests but ignored.
        """
        del had_assistant_output  # intentionally unused; see docstring
        if emitted_terminal_chat_final:
            return False
        return not self._goal_record_is_active()

    @staticmethod
    def _approved_plan_exit_resume_tool_call_id(params: Any) -> str:
        """返回已批准 plan-exit resume 对应的工具调用 ID。

        Confirm interrupt 恢复后，运行时可能在模型已经给出结论的情况下只向
        输出流暴露工具事件。这一标记让流末尾能为该极窄路径补一个用户可见的
        完成消息；其他 confirm / permission resume 不受影响。
        """
        if not isinstance(params, dict):
            return ""
        if str(params.get("source") or "").strip() != "confirm_interrupt":
            return ""
        tool_call_id = str(params.get("request_id") or "").strip()
        answers = params.get("answers")
        if not tool_call_id or not isinstance(answers, list) or not answers:
            return ""
        answer = answers[0]
        if not isinstance(answer, dict):
            return ""
        options = answer.get("selected_options")
        if not isinstance(options, list) or not options:
            return ""
        approved_values = {"approve", "本次允许", "批准", "proceed", "开始执行"}
        selected = str(options[0] or "").strip().lower()
        return tool_call_id if selected in approved_values else ""

    def _plan_exit_fallback_content(self) -> str:
        if self._resolve_runtime_language() == "en":
            return "Plan approved. Exited plan mode and switched to code.normal mode."
        return "计划已获批准，已退出 plan 模式，当前处于 code.normal 模式。"

    def _reasoning_only_empty_reply_fallback(self) -> str:
        from jiuwenswarm.common.chat_final import reasoning_only_empty_reply_fallback_text

        return reasoning_only_empty_reply_fallback_text(self._resolve_runtime_language())

    @staticmethod
    def _should_fill_reasoning_only_empty_final(
        parsed: dict[str, Any] | None,
        *,
        had_reasoning_output: bool,
        visible_text_since_last_final: bool,
    ) -> bool:
        """Whether an empty chat.final should get the reasoning-only short reply."""
        if not isinstance(parsed, dict):
            return False
        if parsed.get("event_type") != "chat.final":
            return False
        if str(parsed.get("content") or "").strip():
            return False
        if not had_reasoning_output:
            return False
        return not visible_text_since_last_final

    def _fill_reasoning_only_empty_final_if_needed(
        self,
        parsed: dict[str, Any] | None,
        *,
        had_reasoning_output: bool,
        visible_text_since_last_final: bool,
    ) -> None:
        if not self._should_fill_reasoning_only_empty_final(
            parsed,
            had_reasoning_output=had_reasoning_output,
            visible_text_since_last_final=visible_text_since_last_final,
        ):
            return
        assert isinstance(parsed, dict)
        parsed["content"] = self._reasoning_only_empty_reply_fallback()

    @staticmethod
    def _iter_delta_for_unstreamed_final(
        *, content: str, chunk_payload: Any
    ) -> list[dict[str, Any]]:
        """终稿正文未被 chat.delta 真正流式送达时，构造补发 delta 的 payload 列表。

        非 team 前端只按 chat.delta 渲染正文，chat.final 仅作收尾标记（relay-claw
        会直接丢弃其 content）。当模型把正文只放进 answer chunk、而 llm_output
        只给出空白/极小片段时，这里补发 delta，保证前端能看到回复。
        """
        text = str(content or "")
        if not text.strip():
            return []
        payload = JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", text)
        if payload is None:
            return []
        _propagate_stream_source_id(chunk_payload, payload)
        logger.info(
            "[JiuWenSwarmDeepAdapter] rerouted final content into chat.delta: len=%s",
            len(text),
        )
        return [payload]

    @staticmethod
    def _drain_final_content_to_deltas(
        parsed: dict[str, Any],
        *,
        segment_streamed_text: str,
        chunk_payload: Any,
    ) -> list[dict[str, Any]]:
        """Move non-empty ``chat.final`` body onto ``chat.delta`` when needed.

        Always clears ``parsed["content"]`` once the body is known to be (or about
        to be) on the delta path. Leaving content on ``chat.final`` makes
        RelayClaw ``computeFinalTextDelta`` append another copy — especially after
        the post-tool ``\\n\\n`` round-break prefix — triplicating the bubble.
        """
        if parsed.get("event_type") != "chat.final":
            return []
        content = str(parsed.get("content") or "")
        stripped = content.strip()
        if not stripped:
            return []
        streamed = str(segment_streamed_text or "")
        if stripped in streamed or streamed.rstrip().endswith(stripped):
            parsed["content"] = ""
            return []
        streamed_visible = streamed.strip()
        # llm_output already delivered a substantial body this segment; keeping
        # final content would re-route a duplicate chat.delta.
        if streamed_visible and len(streamed_visible) >= max(8, len(stripped) // 4):
            parsed["content"] = ""
            return []
        deltas = JiuWenSwarmDeepAdapter._iter_delta_for_unstreamed_final(
            content=content,
            chunk_payload=chunk_payload,
        )
        # Body rides on delta; final is trailer-only for non-team hosts.
        parsed["content"] = ""
        return deltas

    @staticmethod
    def _apply_reasoning_only_empty_reply_fallback(
        *,
        has_streamed_content: bool,
        had_reasoning_output: bool,
        fallback_content: str,
        reasoning_only_fallback: str,
    ) -> str:
        """Fill empty terminal final content for reasoning-only completions.

        Skip the generic fill when this stream already forwarded visible
        ``chat.delta`` text — an empty final is then only a trailer.
        """
        if str(fallback_content or "").strip():
            # Preserve dedicated fallbacks (e.g. plan-exit) over the generic text.
            return fallback_content
        if has_streamed_content:
            return fallback_content
        if not had_reasoning_output:
            return fallback_content
        return reasoning_only_fallback

    async def _reattach_interrupt_output(self, session_id: str) -> Any | None:
        """Briefly wait for the interrupted consumer to release its output lease."""
        started = time.monotonic()
        for attempt in range(1, _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT + 1):
            await asyncio.sleep(_INTERRUPT_OUTPUT_ATTACH_RETRY_INTERVAL_SECONDS)
            interaction_stream = await self._instance.attach_output()
            if interaction_stream is not None:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt output reattached: "
                    "session_id=%s attempts=%s elapsed_ms=%.1f",
                    session_id,
                    attempt,
                    (time.monotonic() - started) * 1000,
                )
                return interaction_stream
        logger.info(
            "[JiuWenSwarmDeepAdapter] interrupt output still leased; returning ACK: "
            "session_id=%s attempts=%s elapsed_ms=%.1f",
            session_id,
            _INTERRUPT_OUTPUT_ATTACH_RETRY_COUNT,
            (time.monotonic() - started) * 1000,
        )
        return None

    @staticmethod
    def _resolve_input_dispatch_mode(params: Any) -> InputDispatchMode | None:
        """Map host ``input_mode`` / ``runtime_mode`` onto OpenJiuwen dispatch mode.

        Missing or unknown values keep pre-Goal Gateway replace semantics
        (``None`` → OpenJiuwen default FOLLOW_UP boundary when idle).
        """
        if not isinstance(params, dict):
            return None
        input_mode = str(
            params.get("input_mode") or params.get("runtime_mode") or ""
        ).strip().lower()
        if input_mode == "follow_up":
            return InputDispatchMode.FOLLOW_UP
        if input_mode == "steer":
            return InputDispatchMode.STEER
        return None

    @staticmethod
    def _wants_attach_goal(params: Any) -> bool:
        return isinstance(params, dict) and params.get("attach_goal") is True

    @staticmethod
    def _should_parse_tui_goal_slash(
        *,
        pending_goal_op: dict[str, Any] | None,
        attach_goal_request: bool,
        channel_id: Any,
        query: Any,
    ) -> bool:
        """Whether to parse chat text ``/goal ...`` (TUI only, when no structured op)."""
        if pending_goal_op is not None or attach_goal_request:
            return False
        if str(channel_id or "").strip().lower() != "tui":
            return False
        return isinstance(query, str)

    @staticmethod
    def _parse_goal_slash_intent(query: str) -> dict[str, Any] | None:
        """Parse ``/goal ...`` into an action dict without touching GoalManager."""
        text = query.strip()
        if not text.startswith("/goal"):
            return None
        args = text[5:].strip()
        if not args:
            return {"action": "get"}
        lower = args.lower()
        if lower in {"pause", "resume", "clear"}:
            return {"action": lower}
        if lower.startswith("set "):
            return {"action": "set", "objective": args[4:].strip()}
        if lower == "set":
            return {"action": "set", "objective": ""}
        return {"action": "set", "objective": args}

    def _is_ack_only_dispatch(self, params: Any) -> bool:
        """Steer / follow_up prefer an existing reader when one is present."""
        mode = self._resolve_input_dispatch_mode(params)
        return mode in (InputDispatchMode.STEER, InputDispatchMode.FOLLOW_UP)

    @staticmethod
    def _is_subagent_approval_answer(request_id: str, params: Any) -> bool:
        """子代理委托审批应答：显式 source 或 request_id 前缀（兼容旧通道）。"""
        source = str(params.get("source") or "").strip() if isinstance(params, dict) else ""
        if source in {"subagent_skill_load", "subagent_tool_permission"}:
            return True
        return isinstance(request_id, str) and request_id.startswith(
            ("subagent_skill_load_", "subagent_tool_permission_")
        )

    @staticmethod
    def _resolve_subagent_approval_answer(
        request: AgentRequest,
        request_id: str,
        answers: Any,
    ) -> bool:
        """把子代理委托审批应答路由回 SubagentApprovalRegistry 的 pending Future。"""
        params = request.params if isinstance(request.params, dict) else {}
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
                resolve_subagent_approval,
            )

            return resolve_subagent_approval(
                request_id=request_id,
                session_id=str(request.session_id or params.get("session_id") or ""),
                source=str(params.get("source") or ""),
                answers=answers,
                agent_scope_id=str(params.get("agent_scope_id") or ""),
            )
        except Exception:  # noqa: BLE001 — 应答路由失败不掩盖 user_answer 响应
            logger.warning(
                "[JiuWenSwarmDeepAdapter] subagent approval resolve failed: request_id=%s",
                request_id,
                exc_info=True,
            )
            return False

    @staticmethod
    def _is_interrupt_resume_dispatch(params: Any) -> bool:
        """HITL answers must inject into the existing interaction when possible.

        Permission / confirm / ask-user resumes arrive as ``chat.send`` with
        ``answers``.  When a Goal (or other) consumer already holds the output
        lease, ``attach_output`` returns ``None`` — we must still ``send_input``
        so InteractiveInput reaches DeepAgent; otherwise the tool stays blocked
        while Goal keeps running.
        """
        if not isinstance(params, dict):
            return False
        source = str(params.get("source") or "").strip()
        answers = params.get("answers")
        request_id = str(params.get("request_id") or "").strip()
        if (
            bool(request_id)
            and isinstance(answers, list)
            and source in {
                "ask_user_interrupt",
                "confirm_interrupt",
                "permission_interrupt",
                "evolution_interrupt",
            }
        ):
            return True
        from jiuwenswarm.gateway.message_handler.evolution_approval import (
            is_interrupt_evolution_approval_answer_payload,
        )

        return is_interrupt_evolution_approval_answer_payload(params)

    def _should_inject_into_existing_interaction(self, params: Any) -> bool:
        """Whether input must be sent even when this request cannot take the lease."""
        return self._is_ack_only_dispatch(params) or self._is_interrupt_resume_dispatch(
            params
        )

    @staticmethod
    def _structured_goal_op_from_request(
        request: AgentRequest,
    ) -> dict[str, Any] | None:
        """Map streaming ``command.goal`` set/resume onto the attach→control path.

        Plain chat text like ``/goal set ...`` is never parsed here — only an
        explicit ``command.goal`` method (Web/TUI structured API).
        """
        if request.req_method != ReqMethod.COMMAND_GOAL:
            return None
        raw = request.params if isinstance(request.params, dict) else {}
        action = str(raw.get("action", "get") or "get").strip().lower()
        if action not in {"set", "resume"}:
            return None
        op: dict[str, Any] = {"action": action}
        if action == "set":
            objective = raw.get("objective")
            op["objective"] = objective if isinstance(objective, str) else ""
            op["overwrite_confirmed"] = bool(raw.get("overwrite_confirmed", False))
            for key in ("token_budget", "max_attempts"):
                value = raw.get(key)
                if value is None or isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    op[key] = value
                elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    op[key] = int(value.strip())
        return op

    async def _abort_shared_agent_if_safe(self, normalized_sid: str, intent: str) -> None:
        """Global DeepAgent/scheduler abort when safe for unrelated sessions."""
        if self._instance is None:
            return
        # Never abort while a goal round is in flight.  The goal supervisor owns
        # its lifecycle; aborting the shared DeepAgent here (e.g. from a
        # stream_cancel triggered by the frontend's cancel-before-send) would
        # kill the goal producer and freeze the backend for the goal session.
        if self._has_active_goal_round():
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): active goal round -> "
                "skip instance.abort (goal controlled via /goal pause|clear): session=%s",
                intent,
                normalized_sid,
            )
            return
        other_count = self._other_active_sessions(normalized_sid)
        if other_count > 0:
            # instance.abort() is a global operation on the shared DeepAgent —
            # it aborts ALL sessions, not just the target.  When other sessions
            # are active, we must NOT call it.  Per-session teardown (rail abort,
            # _cancel_session_agent_tasks, shell process kill) is sufficient to
            # stop the target session's work without collateral damage.
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): 跳过 instance.abort/scheduler cancel，"
                "其他 session 仍活跃 (count=%d, active=%s)",
                intent,
                other_count,
                dict(self._active_session_ids),
            )
            return
        await self._halt_deep_agent_execution(intent)

    @staticmethod
    def _revoke_main_skill_authorization(
        session_id: str | None,
        *,
        reason: str,
    ) -> None:
        """任务终止路径回收主会话动态授权（Grant 失活 + 子代理委托审批取消）。"""
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
                revoke_main_scope,
            )

            revoke_main_scope(session_id, reason=reason)
        except Exception:  # noqa: BLE001 — 不影响原取消/异常流程
            logger.warning(
                "[JiuWenSwarmDeepAdapter] dynamic authorization cleanup failed",
                exc_info=True,
            )

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """处理 interrupt 请求.

        根据 intent 分流：
        - pause: 暂停循环（不取消任务）
        - resume: 恢复已暂停的循环
        - cancel: 为当前 session 生成取消结果与清理信息；真正停任务由 SessionManager 完成
        - supplement: 取消当前任务但保留 todo

        Args:
            request: AgentRequest，params 中可包含：
                - intent: 中断意图 ('pause' | 'cancel' | 'resume' | 'supplement')
                - new_input: 新的用户输入（用于切换任务）

        Returns:
            AgentResponse 包含 interrupt_result 事件数据
        """
        if not self._is_session_scoped_adapter:
            if isinstance(request.params, dict):
                intent_for_dispatch = request.params.get("intent", "cancel")
            else:
                intent_for_dispatch = "cancel"
            if intent_for_dispatch in ("cancel", "supplement"):
                # cancel/supplement：若该 session 尚无 session-scoped adapter（即没有 in-flight 流），
                # 则不创建——创建会跑完整 agent 初始化（17 rail + ensure_initialized，
                # 真实场景含 ProjectMemoryRail 扫描，耗时可达数十秒），把 cancel 处理本身卡住，
                # 后续 esc 的 interrupt 全堵在队列里 → AgentServer 响应超时。
                # 拿不到 cached adapter 即"无运行中任务"，直接 fallthrough 到主 adapter 的
                # cancel 逻辑（下方 `not _session_is_active` 分支会跑 per-session teardown 并返回 success）。
                cached = self._get_cached_session_adapter(request.session_id)
                if cached is None:
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] interrupt(%s): no cached session adapter for "
                        "session=%s, skip create (no in-flight stream) — fallthrough to shared teardown",
                        intent_for_dispatch,
                        request.session_id,
                    )
                else:
                    try:
                        return await cached.process_interrupt(request)
                    finally:
                        await self._evict_idle_session_adapters()
            else:
                # pause/resume：需要 session-scoped adapter 的 StreamEventRail，按原逻辑创建。
                session_adapter = await self._get_or_create_session_adapter(
                    request.session_id, request=request
                )
                try:
                    return await session_adapter.process_interrupt(request)
                finally:
                    await self._evict_idle_session_adapters()

        intent = request.params.get("intent", "cancel")
        new_input = request.params.get("new_input")

        # Interaction-managed sessions: route cancel/supplement through
        # DeepAgent.cancel_round instead of the global DeepAgent abort + raw
        # asyncio-task cancellation path.  The global path kills the shared
        # stream producer out-of-band, which leaves the interaction supervisor's
        # session stream consumer hanging forever and stops the
        # backend from responding to any further messages.  The owning output
        # stream closes with abort_active_round=True for stream requests (primary
        # path).  cancel_round below is the idempotent unary fallback: it aborts
        # the current user OR goal attempt but never clears GoalRecord.
        if (
            self._instance is not None
            and self._instance_interaction_started()
            and intent in ("cancel", "supplement")
        ):
            return await self._process_interaction_interrupt(request, intent, new_input)

        # Session guard: only execute interrupt operations if the target session
        # is currently active on this adapter. Without this guard, a shared adapter
        # (cached by mode:sub_mode:project_dir) would abort/pause ALL concurrent
        # sessions when any one session is interrupted.
        # Normalize session_id the same way process_message_*_impl does, so that
        # an empty-string or None request.session_id doesn't bypass the guard.
        _normalized_sid = self._resolve_interrupt_session_id(request.session_id)
        _session_is_active = self._is_session_active(_normalized_sid)
        if not _session_is_active and intent in ("pause", "resume"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s):",
                "session=%s not active on this adapter, ",
                "skipping pause/resume (active_sessions=%s)",
                intent,
                request.session_id,
                dict(self._active_session_ids),
            )
        elif not _session_is_active and intent in ("cancel", "supplement"):
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): session=%s not in active counter "
                "(stream may have unwound); still running per-session teardown "
                "(active_sessions=%s)",
                intent,
                request.session_id,
                dict(self._active_session_ids),
            )

        success = True
        updated_todos = None
        cancelled_tool_results = []

        if intent == "pause":
            # 暂停：通过 StreamEventRail 在下一个 model_call/tool_call checkpoint 阻塞
            if _session_is_active and self._stream_event_rail is not None:
                self._stream_event_rail.pause(request.session_id)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt: 已暂停执行 request_id=%s",
                    request.request_id,
                )
            message = "任务已暂停"

        elif intent == "resume":
            # 恢复：解除 StreamEventRail 的 pause 阻塞 + 清除 abort 标志
            if _session_is_active and self._stream_event_rail is not None:
                self._stream_event_rail.resume(request.session_id)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt: 已恢复执行 request_id=%s",
                    request.request_id,
                )
            message = "任务已恢复"

        elif intent == "supplement":
            # supplement: 停止当前执行，但保留 todo（新任务会根据 todo 待办继续执行）
            cancelled_tool_results = await self._stop_session_interrupt_work(
                request.session_id,
                intent="supplement",
            )
            if _session_is_active:
                # Global abort is safe only when this session has work in flight.
                # When inactive, another session may have just started — aborting
                # the shared DeepAgent would kill it as collateral damage.
                await self._abort_shared_agent_if_safe(_normalized_sid, "supplement")
            self._revoke_main_skill_authorization(
                request.session_id,
                reason="task_supplemented",
            )
            # 不清理 todo — 保留给新任务继续
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(supplement): 已停止执行 request_id=%s",
                request.request_id,
            )
            message = "任务已切换"

        else:
            # cancel（默认）：终止当前正在运行的 agent 任务 + 清理 todos。
            # 必须同时调用 rail.abort() 和 instance.abort()，否则流式模式下
            # DeepAgent 的 _run_task_loop_stream 后台 Task 不会停止
            # （stream_task.cancel() 只取消了 chunk 转发 Task，不影响 _stream_process）。
            # SessionManager.cancel_session_task 仅管理非流式队列 Task，对流式后台 Task 无效。
            cancelled_tool_results = await self._stop_session_interrupt_work(
                request.session_id,
                intent="cancel",
                reset_for_new_task=True,
            )
            if _session_is_active:
                # Global abort is safe only when this session has work in flight.
                # When inactive, another session may have just started — aborting
                # the shared DeepAgent would kill it as collateral damage.
                await self._abort_shared_agent_if_safe(_normalized_sid, "cancel")
            self._revoke_main_skill_authorization(
                request.session_id,
                reason="task_cancelled",
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(cancel): 已设置 abort 并解除 pause 阻塞"
            )

            updated_todos = None
            if request.session_id:
                try:
                    updated_todos = await self._cancel_pending_todos(request.session_id)
                except Exception as exc:
                    logger.warning("[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc)

                # Cancel auto_harness active run if exists
                try:
                    if self._auto_harness_service is not None \
                        and self._auto_harness_service.has_active_run(request.session_id):
                        self._auto_harness_service.cancel_session_run(request.session_id)
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] interrupt(cancel): cancelled auto_harness run for session=%s",
                            request.session_id,
                        )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] Failed to cancel auto_harness run: %s",
                        exc,
                    )

            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(cancel): 已停止执行 request_id=%s",
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
        if intent not in ("pause", "resume", "supplement") and updated_todos is not None:
            payload["todos"] = updated_todos

        # cancel 后附带被中断的工具执行结果，通知前端更新状态
        if cancelled_tool_results:
            payload["cancelled_tools"] = cancelled_tool_results
            # 写入历史记录，确保刷新网页后工具状态正确显示
            self._append_cancelled_tools_to_history(request, cancelled_tool_results)

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _process_interaction_interrupt(
        self,
        request: "AgentRequest",
        intent: str,
        new_input: Any,
    ) -> "AgentResponse":
        """Handle cancel/supplement for an interaction-managed session.

        Closing the owning interaction output stream is the primary shutdown
        path for streaming hosts.  This unary handler is the idempotent
        fallback: ``cancel_round`` aborts the current user or goal attempt
        without clearing GoalRecord.  For user cancel, pause an ACTIVE goal
        first so the GoalBar stops continuing after the round is aborted.
        """
        paused_goal_payload: dict[str, Any] | None = None
        if intent == "cancel":
            try:
                goal_manager = self._get_goal_manager()
                if goal_manager is not None:
                    record = await goal_manager.get()
                    status = getattr(record, "status", None) if record is not None else None
                    if status is GoalStatus.ACTIVE:
                        paused = await goal_manager.pause()
                        paused_goal_payload = self._goal_record_payload(paused)
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] interrupt(cancel): paused ACTIVE goal "
                            "session=%s",
                            request.session_id,
                        )
            except Exception:
                logger.exception(
                    "[JiuWenSwarmDeepAdapter] interrupt(cancel): goal pause failed "
                    "session=%s",
                    request.session_id,
                )

        cancelled = False
        # Collect in-flight tools without cancelling the stream producer task —
        # interaction cancel must keep round ownership in cancel_round().
        cancelled_tool_results = self._collect_cancelled_tools_for_session(
            request.session_id,
            reset_for_new_task=(intent == "cancel"),
        )
        try:
            cancelled = await self._instance.cancel_round(
                reason="user_cancel",
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): interaction round cancel "
                "cancelled=%s session=%s",
                intent,
                cancelled,
                request.session_id,
            )
        except Exception:
            logger.exception(
                "[JiuWenSwarmDeepAdapter] interrupt(%s): interaction cancel failed",
                intent,
            )
        # Interaction-managed 路径是流式宿主的主取消路径，同样回收动态授权。
        self._revoke_main_skill_authorization(
            request.session_id,
            reason="task_supplemented" if intent == "supplement" else "task_cancelled",
        )
        if intent == "supplement" and isinstance(new_input, str) and new_input.strip():
            await self._clear_pending_ask_user_interrupt_for_supplement(request.session_id)
        message = "任务已切换" if intent == "supplement" else "任务已取消"

        payload: dict[str, Any] = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": True,
            "message": message,
        }
        if new_input:
            payload["new_input"] = new_input
        if paused_goal_payload is not None:
            # Always return the paused snapshot on the interrupt response so
            # Web/TUI can refresh GoalBar even when no output lease remains
            # to receive goal.updated.
            payload["goal"] = paused_goal_payload
        if cancelled_tool_results:
            payload["cancelled_tools"] = cancelled_tool_results
            self._append_cancelled_tools_to_history(request, cancelled_tool_results)

        # Best-effort todo cancellation for user cancel (does not touch runtime).
        if cancelled and intent == "cancel" and request.session_id:
            try:
                updated_todos = await self._cancel_pending_todos(request.session_id)
                if updated_todos is not None:
                    payload["todos"] = updated_todos
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc,
                )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    def _cancel_scheduler_running_tasks(self) -> None:
        """Cancel in-flight asyncio.Tasks in the Controller's TaskScheduler.

        Cooperative abort (rail.abort + instance.abort) only stops at checkpoints,
        but in-flight LLM HTTP requests need CancelledError injected directly
        at the await point to abort immediately.
        """
        try:
            controller = getattr(self._instance, '_loop_controller', None)
            if controller is None:
                return
            scheduler = getattr(controller, '_task_scheduler', None)
            if scheduler is None:
                return
            running = getattr(scheduler, '_running_tasks', None)
            if not running:
                return
            cancelled_count = 0
            for _task_id, (_executor, exec_task) in list(running.items()):
                if exec_task is not None and not exec_task.done():
                    exec_task.cancel()
                    cancelled_count += 1
            if cancelled_count > 0:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] interrupt: 已取消 %d 个 TaskScheduler 运行中的任务",
                    cancelled_count,
                )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] _cancel_scheduler_running_tasks 失败: %s",
                exc,
            )

    async def abort_on_gateway_disconnect(self) -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时：与 interrupt(cancel) 同样中止 rail 与 DeepAgent 实例。

        Note: 这是基础设施级别的事件，会无条件 abort 共享 adapter 上的所有 session。
        与 process_interrupt 的 session guard 不同，gateway 断开意味着前端已无法接收响应，
        继续运行没有意义，因此不需要 other_sessions 保护。
        """
        if not self._is_session_scoped_adapter:
            for adapter in list(self._session_adapters.values()):
                await adapter.abort_on_gateway_disconnect()

        if self._stream_event_rail is not None:
            # Abort all active sessions on this shared adapter.
            # Use list() snapshot since abort() doesn't mutate the Counter.
            active = [sid for sid, count in self._active_session_ids.items() if count > 0]
            if active:
                for sid in active:
                    self._stream_event_rail.abort(sid)
            else:
                # Fallback: no active sessions tracked, abort default
                self._stream_event_rail.abort()
        # Cancel scheduler tasks FIRST to break the circular wait:
        #   instance.abort() awaits _stream_process_task, which may be stuck
        #   in an LLM HTTP request that won't be cancelled until
        #   _cancel_scheduler_running_tasks() runs.
        self._cancel_scheduler_running_tasks()
        if self._instance is not None:
            try:
                await self._instance.abort()
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] abort_on_gateway_disconnect instance.abort failed: %s",
                    exc,
                )
        # Safety net: cancel again in case new scheduler tasks were spawned
        # between the first cancel and abort().
        self._cancel_scheduler_running_tasks()

    @staticmethod
    def _model_looks_usable(model: Model | None) -> bool:
        if model is None:
            return False
        mcc_obj = getattr(model, "model_client_config", None)
        if not isinstance(mcc_obj, ModelClientConfig):
            return False
        return _mcc_looks_usable({
            "api_key": mcc_obj.api_key,
            "api_base": getattr(mcc_obj, "api_base", None),
            "client_provider": getattr(mcc_obj, "client_provider", None),
        })

    def _has_valid_model_config(self, requested_model_name: str = "") -> bool:
        """检查本次请求实际会使用的模型配置是否有效。"""
        return self._model_looks_usable(
            self._resolve_model_by_name(requested_model_name)
        )

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
            from jiuwenswarm.agents.harness.team import get_team_manager
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
            from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager
            if len(get_acp_output_manager()._pending) > 0:  # pylint: disable=protected-access
                return True
        except Exception:
            pass

        return False

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        """Handle chat.user_answer request."""
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id, request=request
            )
            try:
                return await session_adapter.handle_user_answer(request)
            finally:
                await self._evict_idle_session_adapters()

        request_id = (
            request.params.get("request_id", "") if isinstance(request.params, dict) else ""
        )
        answers = request.params.get("answers", []) if isinstance(request.params, dict) else []
        session_id = request.session_id
        resolved = False
        if request_id.startswith("team_skill_evolve_"):
            resolved = await self.handle_team_skill_evolve_approval(
                request_id,
                answers,
                session_id,
                request.channel_id,
            )
        elif request_id.startswith("evolve_simplify_"):
            resolved = await self._handle_simplify_approval(
                request_id,
                answers,
                session_id,
                request.channel_id,
                evolution_meta_from_params(request.params),
            )
        elif request_id.startswith("skill_evolve_"):
            resolved = await self._handle_evolution_approval(request_id, answers)
        elif self._is_interrupt_skill_evolution_approval_params(request_id, request.params):
            logger.warning(
                "[JiuWenSwarmDeepAdapter] interrupt evolution approval received via "
                "chat.user_answer; gateway should route it as chat.send: request_id=%s",
                request_id,
            )
        elif self._is_regular_skill_evolution_approval_params(request.params):
            resolved = await self._handle_evolution_approval(request_id, answers)
        elif self._is_subagent_approval_answer(request_id, request.params):
            # 子代理委托审批（Skill 加载/工具权限）：按 source 或 request_id 前缀
            # 路由回主会话的 SubagentApprovalRegistry pending Future。
            resolved = self._resolve_subagent_approval_answer(request, request_id, answers)

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"accepted": True, "resolved": resolved},
            metadata=request.metadata,
        )

    async def handle_swarmflow_reply(self, request: AgentRequest) -> AgentResponse:
        """Handle chat.swarmflow_reply — deliver a person's reply to a human turn.

        Builds a HumanAgentMessage addressed to ``swarmflow:<run_id>:<corr>``
        (run-scoped; falls back to ``swarmflow:<corr>`` when no run_id) and
        delivers it via ``TeamManager.interact`` — the agent-core thin route
        resolves the pending human-session future on the run's reply topic.
        """
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id, request=request
            )
            try:
                return await session_adapter.handle_swarmflow_reply(request)
            finally:
                await self._evict_idle_session_adapters()

        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id") or request.session_id or ""
        run_id = params.get("run_id")
        corr = params.get("correlation_id") or ""
        answer = params.get("answer") or ""
        logger.info(
            "[WF_DBG] chat.swarmflow_reply req channel_id=%s session_id=%s request_id=%s "
            "run_id=%s correlation_id=%s answer_len=%d",
            request.channel_id,
            session_id,
            request.request_id,
            run_id,
            corr,
            len(answer) if isinstance(answer, str) else 0,
        )
        if not session_id or not corr or answer == "":
            logger.warning(
                "[WF_DBG] chat.swarmflow_reply res ok=False session_id=%s correlation_id=%s error=missing_params",
                session_id,
                corr,
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"ok": False, "error": "missing session_id/correlation_id/answer"},
                metadata=request.metadata,
            )

        from jiuwenswarm.agents.harness.team import get_team_manager
        from openjiuwen.agent_teams.constants import USER_PSEUDO_MEMBER_NAME
        from openjiuwen.agent_teams.interaction.payload import HumanAgentMessage

        from openjiuwen.agent_teams.schema.events import format_swarmflow_human_reply_target

        target = format_swarmflow_human_reply_target(corr, run_id)
        msg = HumanAgentMessage(
            sender=USER_PSEUDO_MEMBER_NAME,
            target=target,
            body=answer,
        )
        try:
            team_manager = get_team_manager(request.channel_id)
            ok, reason = await team_manager.interact(session_id, msg)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] swarmflow reply delivery failed: "
                "session_id=%s corr=%s error=%s",
                session_id, corr, exc,
            )
            ok, reason = False, "exception"

        logger.log(
            logging.WARNING if not ok else logging.INFO,
            "[WF_DBG] chat.swarmflow_reply res ok=%s session_id=%s correlation_id=%s error=%s",
            ok,
            session_id,
            corr,
            None if ok else (reason or "failed"),
        )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=bool(ok),
            payload={"ok": True} if ok else {"ok": False, "error": reason or "failed"},
            metadata=request.metadata,
        )

    @staticmethod
    def _is_interrupt_skill_evolution_approval_params(request_id: str, params: Any) -> bool:
        if not isinstance(params, dict):
            return False
        if isinstance(request_id, str) and request_id.startswith("call_"):
            return JiuWenSwarmDeepAdapter._is_regular_skill_evolution_approval_params(params)
        evolution_meta = evolution_meta_from_params(params)
        return (
            evolution_meta.get("approval_transport") == "interrupt"
            and JiuWenSwarmDeepAdapter._is_regular_skill_evolution_approval_params(params)
        )

    @staticmethod
    def _is_regular_skill_evolution_approval_params(params: Any) -> bool:
        if not isinstance(params, dict):
            return False
        if params.get("source") == "skill_evolution_approval":
            return True
        if params.get("approval_schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA:
            return True
        approval_detail = params.get("approval_detail")
        if (
            isinstance(approval_detail, dict)
            and approval_detail.get("schema") == SKILL_EVOLUTION_APPROVAL_SCHEMA
        ):
            return True
        evolution_meta = evolution_meta_from_params(params)
        if evolution_meta.get("event_kind") != "approval":
            return False
        approval_kind = evolution_meta.get("approval_kind")
        rail_kind = evolution_meta.get("rail_kind")
        return approval_kind in (None, "", "evolve") and rail_kind in (None, "", "regular")

    async def handle_heartbeat(self, request: AgentRequest) -> AgentResponse | None:
        """Handle heartbeat request. Returns None to continue normal flow.

        Injects a heartbeat prompt into the query to ensure the LLM receives
        a non-empty user message. Reading HEARTBEAT.md and injecting its content
        into the system prompt is handled by HeartbeatRail in before_model_call.
        """
        sid = str(request.session_id or "")
        if not sid.startswith("heartbeat"):
            return None
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id, request=request
            )
            try:
                return await session_adapter.handle_heartbeat(request)
            finally:
                await self._evict_idle_session_adapters()

        content = ""
        try:
            deep_config = getattr(self._instance, "deep_config", None) if self._instance else None
            workspace = getattr(deep_config, "workspace", None)
            sys_operation = getattr(deep_config, "sys_operation", None) or self._sys_operation
            if workspace is not None and sys_operation is not None:
                heartbeat_path = str(workspace.get_node_path(WorkspaceNode.HEARTBEAT_MD))
                read_res = await sys_operation.fs().read_file(heartbeat_path, mode="text")
                if read_res.code == 0:
                    content = _clean_heartbeat_content(read_res.data.content)
                else:
                    logger.warning("[JiuWenSwarmDeepAdapter] heartbeat failed to read HEARTBEAT.md")
            else:
                logger.warning("[JiuWenSwarmDeepAdapter] heartbeat workspace/sys_operation not available")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] heartbeat failed to prepare HEARTBEAT.md content: %s", exc)

        request.params["query"] = (
            "这是一次心跳请求任务，请根据 <heartbeat_user_task> 标签中的内容进行回复。\n"
            "<heartbeat_user_task>\n"
            f"{content}\n"
            "</heartbeat_user_task>"
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] heartbeat query injected:" " request_id=%s session_id=%s",
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
            logger.warning("[JiuWenSwarmDeepAdapter] evolution approval failed: no SkillEvolutionRail")
            return False

        record_ids_by_index = record_ids_from_pending_approval(rail, request_id)
        accepted, approved_record_ids = approved_record_ids_from_answers(
            answers,
            EVOLUTION_ACCEPT_LABELS,
            record_ids_by_index,
        )

        if accepted:
            pending_snapshots = getattr(rail, "_pending_approval_snapshots", None)
            pending = (
                pending_snapshots.get(request_id)
                if isinstance(pending_snapshots, dict)
                else None
            )
            skill_for_rebuild = str(getattr(pending, "skill_name", "") or "").strip()
            await approve_evolution_records(
                rail,
                request_id,
                approved_record_ids,
                legacy_fallback=True,
            )
            logger.info("[JiuWenSwarmDeepAdapter] evolution approval accepted: request_id=%s", request_id)
            if skill_for_rebuild:
                self._queue_auto_rebuild_skill(skill_for_rebuild)
                self._schedule_pending_auto_rebuild(request_id)
        else:
            await reject_evolution_records(
                rail,
                request_id,
                legacy_fallback=True,
            )
            logger.info("[JiuWenSwarmDeepAdapter] evolution approval rejected: request_id=%s", request_id)

        return True

    # ------------------------------------------------------------------
    # Team Skill approval handlers
    # ------------------------------------------------------------------

    @staticmethod
    def find_team_skill_rail(request_id: str, channel_id: str | None = None):
        """Find TeamSkillEvolutionRail that owns the given pending request_id."""
        try:
            from jiuwenswarm.agents.harness.team import (
                find_team_skill_rail_across_managers,
                get_team_manager,
            )
            rail = get_team_manager(channel_id).find_team_skill_rail_for_request(request_id)
            if rail is not None:
                return rail
            return find_team_skill_rail_across_managers(request_id)
        except Exception:
            return None

    async def handle_team_skill_evolve_approval(
        self,
        request_id: str,
        answers: list,
        session_id: str | None = None,
        channel_id: str | None = None,
    ) -> bool:
        rail = self.find_team_skill_rail(request_id, channel_id)
        if rail is None:
            logger.warning("[JiuWenSwarmDeepAdapter] team skill evolve approval failed: no TeamSkillEvolutionRail")
            return False

        record_ids_by_index = record_ids_from_pending_approval(rail, request_id)
        accepted, approved_record_ids = approved_record_ids_from_answers(
            answers,
            EVOLUTION_ACCEPT_LABELS,
            record_ids_by_index,
        )

        logger.info(
            "[JiuWenSwarmDeepAdapter] team skill evolve approval: request_id=%s, answers=%s, accepted=%s",
            request_id, answers, accepted,
        )

        if accepted:
            await approve_evolution_records(rail, request_id, approved_record_ids)
            # Sync updated team skill from workspace to global team_skills dir.
            try:
                from jiuwenswarm.agents.harness.team import refresh_team_shared_skill_links_across_managers

                refresh_team_shared_skill_links_across_managers(session_id)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] team shared skill link refresh after approval failed: %s", exc)
            logger.info("[JiuWenSwarmDeepAdapter] team skill evolve accepted: request_id=%s", request_id)
        else:
            await reject_evolution_records(rail, request_id)
            logger.info("[JiuWenSwarmDeepAdapter] team skill evolve rejected: request_id=%s", request_id)

        await self._push_team_skill_evolve_resolution_status(
            request_id,
            session_id=session_id,
            channel_id=channel_id,
            accepted=accepted,
        )
        return True

    async def _handle_team_simplify_approval(
        self,
        request_id: str,
        answers: list,
        session_id: str | None = None,
        channel_id: str | None = None,
    ) -> bool:
        rail = self.find_team_skill_rail(request_id, channel_id)
        if rail is None:
            logger.warning("[JiuWenSwarmDeepAdapter] team simplify approval failed: no TeamSkillEvolutionRail")
            return False

        accepted = answers_select_option(answers, EVOLUTION_EXECUTE_LABELS)
        if accepted:
            await rail.on_approve_simplify(request_id)
            try:
                from jiuwenswarm.agents.harness.team import refresh_team_shared_skill_links_across_managers

                if session_id:
                    refresh_team_shared_skill_links_across_managers(session_id)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] team shared skill link refresh after simplify failed: %s", exc)
            logger.info("[JiuWenSwarmDeepAdapter] team simplify accepted: request_id=%s", request_id)
        else:
            await rail.on_reject_simplify(request_id)
            logger.info("[JiuWenSwarmDeepAdapter] team simplify rejected: request_id=%s", request_id)

        return True

    async def _handle_simplify_approval(
        self,
        request_id: str,
        answers: list,
        session_id: str | None,
        channel_id: str | None,
        evolution_meta: dict[str, Any],
    ) -> bool:
        rail_kind = str(evolution_meta.get("rail_kind") or "").strip().lower()
        if rail_kind == "team":
            return await self._handle_team_simplify_approval(
                request_id,
                answers,
                session_id,
                channel_id,
            )
        if rail_kind == "regular":
            return await self._handle_governance_approval(request_id, answers, "simplify")

        if self.find_team_skill_rail(request_id, channel_id) is not None:
            return await self._handle_team_simplify_approval(
                request_id,
                answers,
                session_id,
                channel_id,
            )
        return await self._handle_governance_approval(request_id, answers, "simplify")

    @staticmethod
    async def _push_team_skill_evolve_resolution_status(
        request_id: str,
        *,
        session_id: str | None,
        channel_id: str | None,
        accepted: bool,
    ) -> None:
        """Close the frontend evolution status after a team skill approval is resolved."""
        if not session_id:
            return
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

        stage = "completed" if accepted else "hidden"
        message = (
            "Team skill evolution accepted"
            if accepted
            else "Team skill evolution rejected"
        )
        try:
            await push_evolution_status(
                EvolutionPushContext(
                    transport=WebSocketGatewayPushTransport(),
                    channel_id=channel_id,
                    session_id=session_id,
                ),
                build_evolution_status_update(
                    request_id=request_id,
                    status="end",
                    stage=stage,
                    message=message,
                ),
                build_server_push_message,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] team skill evolve status push failed: request_id=%s error=%s",
                request_id,
                exc,
            )

    @staticmethod
    def _approval_chunk_from_event(event: Any) -> dict[str, Any] | None:
        parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(event)
        if not isinstance(parsed, dict) or parsed.get("event_type") != "chat.ask_user_question":
            return None
        request_id = parsed.get("request_id")
        questions = parsed.get("questions")
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        if not isinstance(questions, list) or not questions:
            return None
        return parsed

    @staticmethod
    def _format_approval_summary(
        *,
        skill_name: str,
        questions: list[Any],
        action_label: str,
    ) -> str:
        summaries = "\n".join(
            f"  {i + 1}. {q.get('question', '')[:200]}" for i, q in enumerate(questions) if isinstance(q, dict)
        )
        return f"已为 Skill '{skill_name}' {action_label} {len(questions)} 条待审批内容：\n{summaries}"

    def _approval_response_from_event_or_records(
        self,
        *,
        skill_name: str,
        event: Any,
        records: list[Any],
        action_label: str,
        no_changes_output: str,
        invalid_output: str,
    ) -> dict[str, Any]:
        parsed = self._approval_chunk_from_event(event)
        if parsed is not None:
            questions = parsed.get("questions", [])
            return {
                "output": self._format_approval_summary(
                    skill_name=skill_name,
                    questions=questions,
                    action_label=action_label,
                ),
                "result_type": "answer",
                "approval_chunks": [parsed],
            }
        if not records:
            return {"output": no_changes_output, "result_type": "answer"}
        return {"output": invalid_output, "result_type": "error"}

    def _approval_response_from_simplify_result(
        self,
        *,
        skill_name: str,
        simplify_result: Any,
    ) -> dict[str, Any]:
        return self._approval_response_from_event_or_records(
            skill_name=skill_name,
            event=getattr(simplify_result, "approval_event", None),
            records=list(getattr(simplify_result, "actions", []) or []),
            action_label="生成",
            no_changes_output=f"Skill '{skill_name}' 经验库状态良好，无需整理。",
            invalid_output=f"Skill '{skill_name}' 精简方案已生成，但审批事件为空或格式无效。",
        )

    def _approval_response_from_evolve_result(
        self,
        *,
        skill_name: str,
        evolve_result: Any,
    ) -> dict[str, Any]:
        return self._approval_response_from_event_or_records(
            skill_name=skill_name,
            event=getattr(evolve_result, "approval_event", None),
            records=list(getattr(evolve_result, "records", []) or []),
            action_label="生成",
            no_changes_output="当前对话未发现明确的演进信号（无工具执行失败、无用户纠正）。\n",
            invalid_output=f"已为 Skill '{skill_name}' 生成演进经验，但审批事件为空或格式无效。",
        )

    async def _handle_governance_approval(
        self, request_id: str, answers: list, kind: str
    ) -> bool:
        """Unified handler for simplify governance approvals."""
        rail = self._skill_evolution_rail
        if rail is None:
            logger.warning("[JiuWenSwarmDeepAdapter] governance approval failed: no SkillEvolutionRail")
            return False

        accept_labels = {"执行"} if kind == "simplify" else set()
        accepted = any(
            isinstance(ans, dict)
            and bool(accept_labels & set(ans.get("selected_options", [])))
            for ans in answers
        )

        if kind == "simplify":
            if accepted:
                await rail.on_approve_simplify(request_id)
            else:
                await rail.on_reject_simplify(request_id)

        logger.info(
            "[JiuWenSwarmDeepAdapter] governance %s %s: request_id=%s",
            kind, "accepted" if accepted else "rejected", request_id,
        )
        return True

    # ------------------------------------------------------------------
    # Evolution version management (archives / rollback / rebuild)
    # ------------------------------------------------------------------

    def _get_disk_evolution_store(self):
        return evolution_version_ctl.get_disk_evolution_store(self._resolve_skill_dirs())

    def _should_auto_merge_evolved_skill(self, skill_name: str) -> bool:
        """Return True when per-skill selfEvolution resolves to ``auto``.

        Unlisted skills use ``default_auto_save=False`` → ``suggest`` (no auto merge).
        """
        name = str(skill_name or "").strip()
        if not name:
            return False
        action = resolve_skill_evolution_action(
            name,
            default_auto_save=False,
            skills_dirs=self._resolve_skill_dirs(),
        )
        return action == "auto"

    def _queue_auto_rebuild_skill(self, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not name:
            return
        if not self._should_auto_merge_evolved_skill(name):
            return
        if name not in self._pending_auto_rebuild_skills:
            self._pending_auto_rebuild_skills.append(name)

    def _take_pending_auto_rebuild_skills(self) -> list[str]:
        pending = list(self._pending_auto_rebuild_skills)
        self._pending_auto_rebuild_skills.clear()
        return pending

    async def handle_skills_evolution_archives(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.archives — list rollback archive versions."""
        skill_name = evolution_version_ctl.safe_path_name(
            params.get("name") or params.get("skill_name"), "skill"
        )
        store = self._get_disk_evolution_store()
        if not store.skill_exists(skill_name):
            raise ValueError(f"未找到 Skill '{skill_name}'")
        subject = evolution_version_ctl.resolve_subject(store, skill_name)
        return {
            "name": skill_name,
            "versions": evolution_version_ctl.list_body_archive_versions(
                store,
                skill_name,
                subject_kind=str(subject.get("kind") or "skill"),
            ),
        }

    async def handle_skills_evolution_rollback(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.rollback — rollback skill via disk EvolutionStore."""
        skill_name = evolution_version_ctl.safe_path_name(
            params.get("name") or params.get("skill_name"), "skill"
        )
        raw_version = params.get("version")
        version = (
            str(raw_version).strip()
            if raw_version is not None and str(raw_version).strip()
            else None
        )
        skill_path = params.get("skill_path") or params.get("path")
        if skill_path:
            evolution_version_ctl.validate_rebuild_skill_path(
                str(skill_path),
                skill_name=skill_name,
            )
            skills_base = evolution_version_ctl.skills_root_from_skill_md_path(str(skill_path))
            store = evolution_version_ctl.get_disk_evolution_store(
                [skills_base] if skills_base else self._resolve_skill_dirs()
            )
        else:
            store = self._get_disk_evolution_store()

        result = await evolution_version_ctl.do_evolve_rollback(store, skill_name, version)
        if not result.get("ok"):
            raise ValueError(str(result.get("error") or "回滚失败"))
        if result.get("rolled_back"):
            payload = {
                "success": True,
                "name": result["name"],
                "version": result["version"],
                "rolled_back": True,
            }
            if result.get("warning"):
                payload["warning"] = result["warning"]
            return payload
        return {
            "success": True,
            "name": result["name"],
            "rolled_back": False,
            "versions": result.get("versions") or [],
        }

    async def handle_skills_evolution_rebuild(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.rebuild — merge evolution records into a new version."""
        return await self.generate_evolution_merge_version(params)

    async def generate_evolution_merge_version(
        self,
        params: dict | None = None,
        *,
        skill_name: str | None = None,
        stream_ctx: Any = None,
    ) -> dict[str, Any]:
        """Prepare → LLM rewrite → complete_rebuild (shared by RPC and auto merge)."""
        params = params if isinstance(params, dict) else {}
        name = skill_name or params.get("name") or params.get("skill_name")
        name = evolution_version_ctl.safe_path_name(name, "skill")
        record_ids = evolution_version_ctl.normalize_record_ids(params.get("record_ids"))
        user_intent = params.get("user_intent") or params.get("intent")
        if user_intent is not None:
            user_intent = str(user_intent).strip() or None
        min_score_raw = params.get("min_score", 0.5)
        try:
            min_score = float(min_score_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid min_score: {min_score_raw}") from exc

        skill_path = params.get("skill_path") or params.get("path")
        resolved_skill_md: str | None = None
        skills_dirs_param = params.get("skills_dirs")
        if isinstance(skills_dirs_param, (list, tuple)):
            store_dirs: list[str] | None = [
                str(item).strip() for item in skills_dirs_param if str(item).strip()
            ] or None
        elif skills_dirs_param is not None and str(skills_dirs_param).strip():
            store_dirs = [str(skills_dirs_param).strip()]
        else:
            store_dirs = None
        if skill_path:
            resolved_skill_md = evolution_version_ctl.validate_rebuild_skill_path(
                str(skill_path),
                skill_name=name,
            )
            # Use relay/control-plane skill_path root only (same as rollback).
            # Do not merge channel-local workspace skill dirs (often empty C: path).
            if store_dirs is None:
                skills_base = evolution_version_ctl.skills_root_from_skill_md_path(
                    resolved_skill_md
                )
                if skills_base:
                    store_dirs = [skills_base]

        logger.info(
            "[JiuWenSwarmDeepAdapter] skills.evolution.rebuild start: skill=%s "
            "record_ids=%s skill_path=%s",
            name,
            record_ids,
            resolved_skill_md,
        )

        ns_token, overlay_token, wk_token = self._bind_request_env_overlay()
        try:
            if self._instance is None:
                await self.ensure_instance()
            store = (
                evolution_version_ctl.get_disk_evolution_store(store_dirs)
                if store_dirs is not None
                else self._get_disk_evolution_store()
            )
            prepared = await evolution_version_ctl.prepare_rebuild_followup(
                store,
                name,
                user_intent=user_intent,
                record_ids=record_ids,
                min_score=min_score,
                language=self._resolve_runtime_language(),
                llm=self._model,
                model=self._resolve_model_name(),
                skill_md_path=resolved_skill_md,
            )
            if not prepared.get("ok"):
                raise ValueError(str(prepared.get("error") or "rebuild prepare failed"))

            rebuild_context = prepared.get("rebuild_context") or {}
            skill_md_path = rebuild_context.get("skill_md_path") or resolved_skill_md
            self._apply_rebuild_permission_trusted_dirs(skill_md_path)
            before_fp = evolution_version_ctl.skill_md_fingerprint(skill_md_path)
            prompt = str(prepared.get("followup_prompt") or "")
            rebuild_ok = await self._execute_merge_version_rewrite(
                prompt,
                stream_ctx=stream_ctx,
            )
            after_fp = evolution_version_ctl.skill_md_fingerprint(skill_md_path)
            if rebuild_ok and before_fp is not None and after_fp == before_fp:
                rebuild_ok = False
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skills.evolution.rebuild rewrite left "
                    "SKILL.md unchanged: skill=%s path=%s",
                    name,
                    skill_md_path,
                )
            if not rebuild_ok:
                raise ValueError(f"Skill '{name}' 重建改写失败（SKILL.md 未更新）。")

            finalized = await evolution_version_ctl.finalize_rebuild_followup(
                store,
                rebuild_context,
                llm=self._model,
                model=self._resolve_model_name(),
                language=self._resolve_runtime_language(),
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] skills.evolution.rebuild done: skill=%s "
                "new_version=%s cleared=%s",
                name,
                finalized.get("new_version"),
                finalized.get("cleared"),
            )
            if not finalized.get("ok"):
                raise ValueError(
                    str(finalized.get("error") or f"Skill '{name}' 版本收尾失败")
                )
            return {
                "success": True,
                "name": name,
                "skill_path": skill_md_path,
                "archive_path": rebuild_context.get("archive_path"),
                "new_version": finalized.get("new_version"),
                "cleared": finalized.get("cleared"),
            }
        finally:
            self._reset_request_env_bindings(ns_token, overlay_token, wk_token)

    def _apply_rebuild_permission_trusted_dirs(self, skill_md_path: str | None) -> None:
        """Allow write_file/edit_file on the rebuild SKILL.md even without HITL.

        Rebuild RPC often has ``session_id=None``. Path-layer write defaults to
        ask outside workspace; ASK without HITL becomes interrupt and leaves
        SKILL.md unchanged.
        """
        permission_rail = getattr(self, "_permission_rail", None)
        setter = getattr(permission_rail, "set_trusted_dirs", None)
        extra = evolution_version_ctl.extra_trusted_dirs_for_skill_md(skill_md_path)
        if permission_rail is None or not callable(setter):
            return
        existing: list[str] = []
        engine = getattr(permission_rail, "_engine", None)
        for item in getattr(engine, "trusted_dirs", None) or []:
            text = str(item).strip()
            if text:
                existing.append(text)
        try:
            setter(merge_shared_skills_trusted_dirs(existing + extra))
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] rebuild permission trusted_dirs seed failed",
                exc_info=True,
            )

    async def _execute_merge_version_rewrite(
        self,
        prompt: str,
        *,
        stream_ctx: Any = None,
    ) -> bool:
        """Run LLM rewrite for merge-version; return True when agent call succeeds."""
        _ = stream_ctx
        if self._instance is None:
            return False
        try:
            from openjiuwen.core.session.agent import create_agent_session

            # Fresh session so default_session checkpoint HITL is not treated as resume.
            session = create_agent_session(
                session_id=f"evolution-rebuild-{uuid.uuid4().hex[:12]}",
                card=getattr(self._instance, "card", None),
            )
            result = await Runner.run_agent(
                agent=self._instance,
                inputs={"query": prompt},
                session=session,
            )
            if isinstance(result, dict) and result.get("result_type") == "interrupt":
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] merge-version rewrite interrupted"
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] merge-version rewrite failed: %s", exc
            )
            return False

    def _schedule_pending_auto_rebuild(self, request_id: str | None = None) -> None:
        """Fire-and-forget version merge for skills queued after experience persist."""
        if not self._pending_auto_rebuild_skills:
            return
        rid = str(request_id or "auto-rebuild").strip() or "auto-rebuild"
        asyncio.create_task(
            self._run_auto_rebuild_skills_detached(request_id=rid),
            name=f"auto-rebuild-{rid}",
        )

    async def _skill_has_live_evolution_records(self, skill_name: str) -> bool:
        """Return True when disk evolutions.json has at least one live entry."""
        try:
            store = self._get_disk_evolution_store()
            subject = evolution_version_ctl.resolve_subject(store, skill_name)
            evo_log = await store.load_full_evolution_log(
                skill_name,
                subject_kind=str(subject.get("kind") or "skill") or None,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] could not inspect live evolutions for "
                "skill=%s: %s",
                skill_name,
                exc,
            )
            # Fail open: let generate_evolution_merge_version decide.
            return True
        entries = getattr(evo_log, "entries", None) or []
        return bool(entries)

    async def _run_auto_rebuild_skills_detached(self, *, request_id: str | None = None) -> None:
        """Background auto version merge gated by per-skill selfEvolution=auto."""
        skills = self._take_pending_auto_rebuild_skills()
        for skill_name in skills:
            if not self._should_auto_merge_evolved_skill(skill_name):
                logger.info(
                    "[JiuWenSwarmDeepAdapter] skip auto rebuild: request_id=%s "
                    "skill=%s reason=selfEvolution_not_auto",
                    request_id,
                    skill_name,
                )
                continue
            if not await self._skill_has_live_evolution_records(skill_name):
                logger.info(
                    "[JiuWenSwarmDeepAdapter] skip auto rebuild: request_id=%s "
                    "skill=%s reason=no_evolution_records",
                    request_id,
                    skill_name,
                )
                continue
            try:
                await self.generate_evolution_merge_version(skill_name=skill_name)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] auto rebuild failed: request_id=%s "
                    "skill=%s error=%s",
                    request_id,
                    skill_name,
                    exc,
                )

    @staticmethod
    def _followup_response(action: str, followup_prompt: str, skill_name: str) -> dict[str, Any]:
        return {
            "action": action,
            "followup_prompt": followup_prompt,
            "skill_name": skill_name,
            "result_type": "followup",
        }

    @staticmethod
    def _extract_followup_prompt(slash_result: dict[str, Any] | None) -> str | None:
        """Return follow-up prompt when a slash command should continue as an agent turn."""
        if not isinstance(slash_result, dict):
            return None
        if slash_result.get("result_type") != "followup":
            return None
        prompt = slash_result.get("followup_prompt")
        if not isinstance(prompt, str):
            return None
        prompt = prompt.strip()
        return prompt or None

    async def _ensure_evolution_rail_for_slash(self, mode: str) -> str | None:
        """Check evolution availability for slash commands; lazily init rail if needed.

        Returns None when the rail is (or becomes) available, or an error message string.
        """
        # 合并后演进能力在 agent 模式可用（兼容历史 agent.plan token）。
        if mode not in ("agent", "agent.plan"):
            display_mode = str(mode or "当前").strip() or "当前"
            return f"{display_mode} 模式下演进功能不可用。"
        if not get_evolution_enabled(self._config_cache):
            return "演进功能未启用。"
        await self._ensure_active_evolution_rails_registered()
        if self._skill_evolution_rail is None:
            return "演进功能初始化失败。"

        # SkillCreateRail requires skill_create config
        if get_skill_create_enabled(self._config_cache):
            if self._skill_create_rail is None:
                self._skill_create_rail = self._build_skill_create_rail(self._config_cache)
        return None


    async def _handle_slash_command(
        self,
        query: Any,
        session_id: str = "default",
        mode: str = "agent",
        channel_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Intercept slash commands before agent invocation.

        Returns result dict if handled, None to proceed normally.
        The dict may contain an ``approval_chunks`` list that the caller
        should forward to the frontend as separate stream events.
        """
        if not isinstance(query, str):
            return None

        # Goal slash text is a TUI convenience only. Web (and other channels)
        # must use structured ``command.goal`` / ``attach_goal``; a plain
        # chat message like "/goal set ..." stays ordinary user text.
        if str(channel_id or "").strip().lower() == "tui":
            goal_result = await self._handle_goal_slash_command(
                query,
                session_id,
            )
            if goal_result is not None:
                return goal_result

        stripped = query.strip()

        slash_dirs = self._resolve_skill_dirs()
        slash_result = await handle_evolution_slash_command(
            stripped,
            EvolutionSlashContext(
                mode=mode,
                session_id=session_id,
                skills_dir=slash_dirs[0] if slash_dirs else str(get_agent_skills_dir()),
                evolution_enabled=get_evolution_enabled(self._config_cache),
                language=self._resolve_runtime_language(),
            ),
        )
        if slash_result is not None:
            if str(slash_result.get("action") or "").strip() == "run_rebuild_inline":
                slash_result = await self._execute_slash_rebuild_request(
                    slash_result,
                    skills_dirs=slash_dirs or None,
                )
            return evolution_slash_result(
                evolution_slash_command_name(stripped),
                slash_result,
                warning_phrases=REGULAR_EVOLUTION_SLASH_WARNING_PHRASES,
            )

        return None

    async def _execute_slash_rebuild_request(
        self,
        slash_result: dict[str, Any],
        *,
        skills_dirs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run shared rebuild pipeline for `/evolve_rebuild` (not a followup turn)."""
        skill_name = str(slash_result.get("skill_name") or "").strip()
        user_intent = slash_result.get("user_intent")
        if user_intent is not None:
            user_intent = str(user_intent).strip() or None
        params: dict[str, Any] = {"name": skill_name}
        if user_intent is not None:
            params["user_intent"] = user_intent
        if skills_dirs:
            params["skills_dirs"] = list(skills_dirs)
        try:
            merge = await self.generate_evolution_merge_version(params)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] /evolve_rebuild failed: skill=%s error=%s",
                skill_name,
                exc,
            )
            return {
                "result_type": "error",
                "output": str(exc) or f"Skill '{skill_name}' 重建失败。",
            }
        new_version = merge.get("new_version")
        version_text = f"`{new_version}`" if new_version else "新版本"
        return {
            "result_type": "answer",
            "output": (
                f"Skill '{skill_name}' 已采纳演进经验并生成版本 {version_text}。"
            ),
            "new_version": new_version,
            "name": skill_name,
            "cleared": merge.get("cleared"),
        }

    # Goal capability adapter -------------------------------------------------

    @staticmethod
    def _goal_record_payload(record: Any | None) -> dict[str, Any] | None:
        if record is None:
            return None
        to_dict = getattr(record, "to_dict", None)
        return to_dict() if callable(to_dict) else None

    @staticmethod
    def _format_goal_control_message(action: str, goal: dict[str, Any] | None) -> str:
        if action == "get":
            return "No goal in this session." if goal is None else f"Goal: {goal.get('objective', '')}"
        if action == "pause":
            return "Goal paused." if goal is not None else "No goal in this session."
        if action == "clear":
            return "Goal cleared." if goal is None else "Goal was not cleared."
        if action == "resume":
            return "Goal resumed." if goal is not None else "No goal in this session."
        return "Goal set." if goal is not None else "Goal was not set."

    @staticmethod
    def _record_goal_set_history_if_needed(
        request: AgentRequest,
        *,
        action: str | None,
        result_type: str | None,
        goal_payload: dict[str, Any] | None,
    ) -> None:
        """Write objective as a user history turn only after a successful set."""
        if str(action or "").strip().lower() != "set":
            return
        if result_type in {"goal_error", "goal_confirm_required", None}:
            return
        if not isinstance(goal_payload, dict):
            return
        objective = str(goal_payload.get("objective") or "").strip()
        if not objective:
            return
        params = request.params if isinstance(request.params, dict) else {}
        goal_id = str(goal_payload.get("goal_id") or "").strip() or None
        append_history_record(
            session_id=request.session_id or "default",
            request_id=request.request_id,
            channel_id=request.channel_id,
            role="user",
            content=objective,
            timestamp=time.time(),
            channel_metadata=request.metadata,
            mode=params.get("mode", "unknown"),
            extra={
                "goal_id": goal_id,
                "is_goal_objective_message": True,
            },
        )

    @staticmethod
    def _goal_completed_history_exists(session_id: str, goal_id: str) -> bool:
        """Return True when this session already persisted a completion card for goal_id."""
        message_id = f"goal-completed-{goal_id}"
        try:
            for rec in load_history_records(session_id):
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("id") or "") == message_id:
                    return True
                if rec.get("is_goal_completed_message") and str(rec.get("goal_id") or "") == goal_id:
                    return True
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] goal completed history lookup failed: session_id=%s goal_id=%s",
                session_id,
                goal_id,
                exc_info=True,
            )
        return False

    @staticmethod
    def _record_goal_completed_history_if_needed(
        *,
        session_id: str,
        channel_id: str,
        channel_metadata: dict[str, Any] | None,
        mode: str | None,
        goal_payload: dict[str, Any] | None,
    ) -> None:
        """Persist a goal-completed card once when status first becomes completed."""
        if not isinstance(goal_payload, dict):
            return
        status = goal_payload.get("status")
        status_value = getattr(status, "value", status)
        if str(status_value or "").strip().lower() != "completed":
            return
        goal_id = str(goal_payload.get("goal_id") or "").strip()
        if not goal_id:
            return
        sid = (session_id or "default").strip() or "default"
        if JiuWenSwarmDeepAdapter._goal_completed_history_exists(sid, goal_id):
            return

        evidence = ""
        last_assessment = goal_payload.get("last_assessment")
        if isinstance(last_assessment, dict):
            evidence = str(last_assessment.get("evidence") or "").strip()
        # Keep the existing frontend GoalCompletedCard wire format so history
        # restore / localStorage merge keep working without a content-parser fork.
        content = "goal.completed:" + json.dumps({"evidence": evidence}, ensure_ascii=False)
        message_id = f"goal-completed-{goal_id}"
        append_history_record(
            session_id=sid,
            request_id=message_id,
            channel_id=channel_id,
            role="assistant",
            content=content,
            timestamp=time.time(),
            channel_metadata=channel_metadata,
            mode=mode,
            extra={
                "id": message_id,
                "goal_id": goal_id,
                "is_goal_completed_message": True,
                "evidence": evidence,
            },
        )

    @staticmethod
    def _interaction_goal_updated_payload(payload: Any) -> dict[str, Any]:
        """Normalize goal updates to the public Web/TUI payload shape."""
        if not isinstance(payload, dict):
            return {"event_type": GOAL_UPDATED_EVENT_TYPE, "goal": None}
        if "goal" in payload:
            return {
                "event_type": GOAL_UPDATED_EVENT_TYPE,
                "goal": payload.get("goal"),
            }
        return {
            "event_type": GOAL_UPDATED_EVENT_TYPE,
            "goal": payload or None,
        }

    def _get_goal_manager(self) -> Any:
        if self._instance is None:
            return None
        return getattr(self._instance, "goal_manager", None)

    async def dispatch_goal_control(
        self,
        *,
        action: str,
        objective: str | None = None,
        overwrite_confirmed: bool = False,
        token_budget: int | None = None,
        max_attempts: int | None = None,
        session_id: str = "default",
    ) -> dict[str, Any] | None:
        """Public Goal control entry used by the facade/session-pool adapter."""
        return await self._dispatch_goal_control(
            action=action,
            objective=objective,
            overwrite_confirmed=overwrite_confirmed,
            token_budget=token_budget,
            max_attempts=max_attempts,
            session_id=session_id,
        )

    async def _dispatch_goal_control(
        self,
        *,
        action: str,
        objective: str | None = None,
        overwrite_confirmed: bool = False,
        token_budget: int | None = None,
        max_attempts: int | None = None,
        session_id: str = "default",
        request: AgentRequest | None = None,
    ) -> dict[str, Any] | None:
        """Map JiuwenSwarm protocol fields to the independent Goal methods."""
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(
                session_id, request=request
            )
            try:
                return await session_adapter.dispatch_goal_control(
                    action=action,
                    objective=objective,
                    overwrite_confirmed=overwrite_confirmed,
                    token_budget=token_budget,
                    max_attempts=max_attempts,
                    session_id=session_id,
                )
            finally:
                await self._evict_idle_session_adapters()
        if self._instance is None:
            return None

        normalized_action = action.strip().lower()
        goal_manager = self._get_goal_manager()
        if goal_manager is None:
            return {
                "result_type": "goal_error",
                "action": normalized_action,
                "error_code": "goal_manager_not_started",
                "error": "goal manager is not started",
            }
        try:
            if normalized_action == "get":
                # Read-only status query: use the lock-free ``peek`` snapshot so a
                # long-running / stuck goal round (which holds the shared
                # interaction control lock across ``set``/``clear`` -> abort) can
                # never block a plain ``command.goal get`` up to the unary timeout.
                # ``await get()`` would serialize on that same control lock.
                peek = getattr(goal_manager, "peek", None)
                goal = peek() if callable(peek) else await goal_manager.get()
            elif normalized_action == "set":
                goal = await goal_manager.set(
                    objective or "",
                    overwrite_confirmed=overwrite_confirmed,
                    token_budget=token_budget,
                    max_attempts=max_attempts,
                )
            elif normalized_action == "pause":
                before = await goal_manager.get()
                if before is None:
                    return {
                        "result_type": "goal_error",
                        "action": normalized_action,
                        "error_code": "no_goal",
                        "error": "No goal in this session; cannot pause.",
                        "goal": None,
                    }
                before_status = before.status
                goal = await goal_manager.pause()
                goal_payload = self._goal_record_payload(goal)
                if before_status is not GoalStatus.ACTIVE:
                    status_value = getattr(before_status, "value", str(before_status))
                    return {
                        "result_type": "goal_error",
                        "action": normalized_action,
                        "error_code": "invalid_state",
                        "error": (
                            f"Goal is {status_value}; only active goals can be paused."
                        ),
                        "goal": goal_payload,
                    }
                return {
                    "result_type": "goal_control",
                    "action": normalized_action,
                    "goal": goal_payload,
                    "output": "Goal paused.",
                }
            elif normalized_action == "resume":
                before = await goal_manager.get()
                if before is None:
                    return {
                        "result_type": "goal_error",
                        "action": normalized_action,
                        "error_code": "no_goal",
                        "error": "No goal in this session; cannot resume.",
                        "goal": None,
                    }
                before_status = before.status
                if before_status is GoalStatus.ACTIVE:
                    goal_payload = self._goal_record_payload(before)
                    return {
                        "result_type": "goal_control",
                        "action": normalized_action,
                        "goal": goal_payload,
                        "output": "Goal already active.",
                    }
                if before_status not in (GoalStatus.PAUSED, GoalStatus.BLOCKED):
                    status_value = getattr(before_status, "value", str(before_status))
                    return {
                        "result_type": "goal_error",
                        "action": normalized_action,
                        "error_code": "invalid_state",
                        "error": (
                            f"Goal is {status_value}; only paused/blocked goals "
                            "can be resumed."
                        ),
                        "goal": self._goal_record_payload(before),
                    }
                goal = await goal_manager.resume()
            elif normalized_action == "clear":
                removed = await goal_manager.clear()
                if removed is None:
                    return {
                        "result_type": "goal_error",
                        "action": normalized_action,
                        "error_code": "no_goal",
                        "error": "No goal in this session; nothing to clear.",
                        "goal": None,
                        "cleared_goal": None,
                    }
                return {
                    "result_type": "goal_control",
                    "action": normalized_action,
                    "goal": None,
                    "cleared_goal": self._goal_record_payload(removed),
                    "output": "Goal cleared.",
                }
            else:
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "invalid_action",
                    "error": f"unsupported goal action: {action}",
                }
        except GoalOperationError as exc:
            if exc.code == "already_exists":
                return {
                    "result_type": "goal_confirm_required",
                    "action": normalized_action,
                    "error_code": exc.code,
                    "error": str(exc),
                    "existing_goal": self._goal_record_payload(exc.goal),
                    "requested_objective": objective,
                }
            return {
                "result_type": "goal_error",
                "action": normalized_action,
                "error_code": exc.code,
                "error": str(exc),
                "goal": self._goal_record_payload(exc.goal),
            }

        goal_payload = self._goal_record_payload(goal)
        active = goal is not None and goal.status is GoalStatus.ACTIVE
        return {
            "result_type": "goal_stream"
            if normalized_action in {"set", "resume"} and active
            else "goal_control",
            "action": normalized_action,
            "goal": goal_payload,
            "output": self._format_goal_control_message(normalized_action, goal_payload),
        }

    async def handle_goal_command_structured(
        self,
        request: AgentRequest,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Structured ``command.goal`` endpoint（业务字段取自 ``request.params``）。"""
        raw = request.params if isinstance(request.params, dict) else {}
        objective = raw.get("objective")
        sid = (
            str(session_id).strip()
            if isinstance(session_id, str) and session_id.strip()
            else (request.session_id or "default")
        )
        return await self._dispatch_goal_control(
            action=str(raw.get("action", "get")),
            objective=objective if isinstance(objective, str) else None,
            overwrite_confirmed=bool(raw.get("overwrite_confirmed", False)),
            token_budget=raw.get("token_budget"),
            max_attempts=raw.get("max_attempts"),
            session_id=sid,
            request=request,
        )

    async def _handle_goal_slash_command(
        self,
        query: str,
        session_id: str = "default",
    ) -> dict[str, Any] | None:
        """Translate only product syntax; the SDK receives capability calls."""
        text = query.strip()
        if not text.startswith("/goal"):
            return None
        args = text[5:].strip()
        if not args:
            return await self._dispatch_goal_control(action="get", session_id=session_id)
        lower = args.lower()
        if lower in {"pause", "resume", "clear"}:
            return await self._dispatch_goal_control(action=lower, session_id=session_id)
        if lower.startswith("set "):
            objective = args[4:].strip()
        elif lower == "set":
            objective = ""
        else:
            # ``get`` and ``stop`` are not user command words.  They remain
            # valid goal text in the documented ``/goal <objective>`` form.
            objective = args
        return await self._dispatch_goal_control(
            action="set", objective=objective, session_id=session_id
        )

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
            if registered_tool is not None:
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

        try:
            todos = await modify_tool.load_todos(session_id)
            if not todos:
                return None

            _done_statuses = {
                TodoStatus.COMPLETED.value,
                TodoStatus.CANCELLED.value,
            }

            ids_to_cancel = []
            for todo in todos:
                if todo.status.value not in _done_statuses:
                    ids_to_cancel.append(todo.id)

            if ids_to_cancel:
                await modify_tool._cancel_todos(ids_to_cancel, todos)
                logger.info(
                    "[JiuWenSwarmDeepAdapter] 已将 session %s 的未完成任务标记为 cancelled",
                    session_id,
                )

            # 重新加载并返回前端格式的 todo 列表
            updated_todos = await modify_tool.load_todos(session_id)
            if updated_todos and self._stream_event_rail is not None:
                return self._stream_event_rail._format_todos_for_frontend(updated_todos)
            return None
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] 标记 todo cancelled 失败: %s", exc)
            return None

    def _active_deepresearch_session(self, session_id: str) -> Any | None:
        """Return the already-bound product session only for an exact id match."""
        if not isinstance(session_id, str) or not session_id:
            return None
        instance = getattr(self, "_instance", None)
        session = getattr(instance, "_interaction_session", None)
        get_session_id = getattr(session, "get_session_id", None)
        if session is None or not callable(get_session_id):
            return None
        try:
            active_session_id = get_session_id()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(active_session_id, str) or active_session_id != session_id:
            return None
        return session

    async def _load_deepresearch_rewrite_html_target(
        self,
        session_id: str,
    ) -> RewriteHtmlTarget | None:
        """Restore the trusted HTML target from the active product session."""
        session = self._active_deepresearch_session(session_id)
        if session is None:
            return None
        try:
            return target_from_state(
                session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[DeepResearchRewriteHtmlFollowup] restore target failed "
                "session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            return None

    async def _try_deepresearch_rewrite_html_followup(
        self,
        query: object,
        session_id: str,
    ) -> RewriteHtmlFollowupResult | None:
        """Handle an explicit HTML follow-up without routing through the model."""
        if not is_html_followup_request(query):
            return None

        target = await self._load_deepresearch_rewrite_html_target(session_id)
        if target is None:
            return RewriteHtmlFollowupResult(
                status="error",
                error_code="TARGET_UNAVAILABLE",
                message=(
                    "未找到可生成 HTML 的已完成改写版本。"
                    "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
                ),
            )

        from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools

        try:
            raw_result = await rewrite_tools.deepresearch_generate_rewrite_html._func(  # pylint: disable=protected-access
                report_path=target.report_path,
                revision_id=target.revision_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[DeepResearchRewriteHtmlFollowup] HTML tool failed "
                "session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            raw_result = None
        return decode_html_tool_result(raw_result)

    async def _run_deepresearch_rewrite_html_transaction(
        self,
        query: object,
        *,
        session_id: str,
    ) -> RewriteHtmlFollowupResult | None:
        """Linearize trusted-target loading and HTML generation with Core work."""
        if self._deepresearch_rewrite_tx_uncertain:
            return None
        instance = getattr(self, "_instance", None)
        send_lock = getattr(instance, "_interaction_send_lock", None)
        control_lock = getattr(instance, "_interaction_control_lock", None)
        keep_open = getattr(instance, "_should_keep_interaction_open_locked", None)
        if send_lock is None or control_lock is None or not callable(keep_open):
            return None
        async with send_lock:
            async with control_lock:
                if keep_open():
                    return None
                return await self._try_deepresearch_rewrite_html_followup(
                    query,
                    session_id,
                )

    async def _try_deepresearch_rewrite_fast_path(
        self,
        query: object,
    ) -> RewriteFastPathResult | None:
        from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools
        from jiuwenswarm.common.thinking.adapter import adapt_thinking
        from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs

        model_call_kwargs = thaw_llm_call_kwargs(
            adapt_thinking("off", model=self._model).llm_call_kwargs
        )
        return await run_rewrite_fast_path(
            query,
            model_invoke=self._model.invoke,
            prepare_invoke=rewrite_tools.deepresearch_prepare_rewrite._func,  # pylint: disable=protected-access
            commit_invoke=rewrite_tools.deepresearch_commit_rewrite._func,  # pylint: disable=protected-access
            model_call_kwargs=model_call_kwargs,
        )

    @staticmethod
    def _valid_deepresearch_rewrite_request_id(request_id: object) -> bool:
        return (
            isinstance(request_id, str)
            and 0 < len(request_id) <= _DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH
        )

    def _decode_deepresearch_rewrite_replays(
        self,
        value: object,
    ) -> list[tuple[str, str, RewriteFastPathResult, dict[str, object]]]:
        try:
            encoded_state = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError):
            return []
        invalid_state = (
            len(encoded_state) > _DEEPRESEARCH_REWRITE_REPLAY_MAX_BYTES
            or not isinstance(value, dict)
            or set(value) != {"schema_version", "entries"}
            or value.get("schema_version")
            != _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION
        )
        if invalid_state:
            return []
        raw_entries = value.get("entries")
        if (
            not isinstance(raw_entries, list)
            or len(raw_entries) > _DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES
        ):
            return []
        decoded = []
        seen_request_ids = set()
        for entry in raw_entries:
            if not isinstance(entry, dict) or set(entry) != {
                "request_id",
                "query_sha256",
                "terminal_kind",
                "action",
                "target",
            }:
                return []
            request_id = entry.get("request_id")
            query_sha256 = entry.get("query_sha256")
            terminal_kind = entry.get("terminal_kind")
            action = entry.get("action")
            raw_target = entry.get("target")
            target = target_from_state(raw_target)
            invalid_entry = (
                not self._valid_deepresearch_rewrite_request_id(request_id)
                or not isinstance(query_sha256, str)
                or len(query_sha256) != 64
                or any(character not in "0123456789abcdef" for character in query_sha256)
                or request_id in seen_request_ids
                or terminal_kind not in _DEEPRESEARCH_REWRITE_REPLAY_TERMINAL_KINDS
                or (
                    action not in {"polish", "expand", "shorten"}
                    and not (terminal_kind == "publish_uncertain" and action is None)
                )
                or target is None
                or len(target.report_path.encode("utf-8"))
                > _DEEPRESEARCH_REWRITE_REPLAY_PATH_MAX_BYTES
            )
            if invalid_entry:
                return []
            commit_result = {
                "status": "completed",
                "report_delivered": terminal_kind == "success",
                "report_path": target.report_path,
                "revision_id": target.revision_id,
            }
            delivery_failed = terminal_kind == "report_delivery_failed"
            publish_uncertain = terminal_kind == "publish_uncertain"
            decoded.append(
                (
                    request_id,
                    query_sha256,
                    RewriteFastPathResult(
                        recognized=True,
                        status="error" if publish_uncertain else "completed",
                        action=action,
                        error_code=(
                            "PUBLISH_STATE_UNCERTAIN"
                            if publish_uncertain
                            else (
                                "REPORT_DELIVERY_FAILED" if delivery_failed else None
                            )
                        ),
                        message=(
                            _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
                            if publish_uncertain
                            else (
                                _DEEPRESEARCH_REWRITE_DELIVERY_FAILURE_MESSAGE
                                if delivery_failed
                                else _DEEPRESEARCH_REWRITE_SUCCESS_MESSAGE
                            )
                        ),
                        usage_metadata=None,
                        prepare_ms=0.0,
                        model_ms=0.0,
                        commit_ms=0.0,
                        total_ms=0.0,
                        model_calls=0,
                        model_output_adjustments=(),
                        model_output_error_reason=None,
                        commit_result=commit_result,
                    ),
                    target.to_state(),
                )
            )
            seen_request_ids.add(request_id)
        return decoded

    def _encode_deepresearch_rewrite_replay_entry(
        self,
        request_id: str,
        query_sha256: str,
        result: RewriteFastPathResult,
        target: RewriteHtmlTarget,
    ) -> dict[str, object]:
        return {
            "request_id": request_id,
            "query_sha256": query_sha256,
            "terminal_kind": self._deepresearch_rewrite_terminal_kind(result),
            "action": result.action,
            "target": target.to_state(),
        }

    @staticmethod
    def _deepresearch_rewrite_replay_conflict() -> RewriteFastPathResult:
        return RewriteFastPathResult(
            recognized=True,
            status="error",
            action=None,
            error_code="REPLAY_CONFLICT",
            message="无法安全重放改写请求，请重新提交。",
            usage_metadata=None,
            prepare_ms=0.0,
            model_ms=0.0,
            commit_ms=0.0,
            total_ms=0.0,
            model_calls=0,
            commit_result=None,
        )

    @staticmethod
    def _deepresearch_rewrite_publish_uncertain(
        target: RewriteHtmlTarget | None = None,
    ) -> RewriteFastPathResult:
        return RewriteFastPathResult(
            recognized=True,
            status="error",
            action=None,
            error_code="PUBLISH_STATE_UNCERTAIN",
            message=_DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE,
            usage_metadata=None,
            prepare_ms=0.0,
            model_ms=0.0,
            commit_ms=0.0,
            total_ms=0.0,
            model_calls=0,
            commit_result=(
                {
                    "status": "completed",
                    "report_delivered": False,
                    "report_path": target.report_path,
                    "revision_id": target.revision_id,
                }
                if target is not None
                else None
            ),
        )

    @staticmethod
    def _deepresearch_rewrite_terminal_kind(
        result: RewriteFastPathResult,
    ) -> str | None:
        commit_result = result.commit_result
        target = (
            target_from_commit_result(commit_result)
            if isinstance(commit_result, dict)
            else None
        )
        publish_uncertain = (
            result.status == "error"
            and result.action is None
            and result.error_code == "PUBLISH_STATE_UNCERTAIN"
            and result.message == _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
            and isinstance(commit_result, dict)
            and commit_result.get("status") == "completed"
            and commit_result.get("report_delivered") is False
            and target is not None
        )
        if publish_uncertain:
            return "publish_uncertain"
        invalid_completed_result = (
            result.status != "completed"
            or not isinstance(commit_result, dict)
            or commit_result.get("status") != "completed"
            or not isinstance(commit_result.get("report_delivered"), bool)
            or target is None
        )
        if invalid_completed_result:
            return None
        if commit_result["report_delivered"] is False:
            return "report_delivery_failed"
        if result.error_code is None:
            return "success"
        return None

    def _load_deepresearch_rewrite_replay(
        self,
        session_id: str,
        request_id: str,
        query_sha256: str,
    ) -> tuple[bool, RewriteFastPathResult | None]:
        session = self._active_deepresearch_session(session_id)
        if session is None:
            return False, None
        try:
            state = session.get_state(_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY)
            current_target = target_from_state(
                session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
            )
        except Exception:  # noqa: BLE001
            return True, None
        decoded_replays = self._decode_deepresearch_rewrite_replays(state)
        if not decoded_replays and state is not None:
            is_empty_state = (
                isinstance(state, dict)
                and set(state) == {"schema_version", "entries"}
                and state.get("schema_version")
                == _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION
                and state.get("entries") == []
            )
            if not is_empty_state:
                return True, None
        for saved_request_id, saved_query_sha256, result, saved_target in decoded_replays:
            if saved_request_id == request_id:
                if (
                    saved_query_sha256 != query_sha256
                    or current_target is None
                    or saved_target != current_target.to_state()
                ):
                    return True, None
                return True, result
        return False, None

    async def _run_deepresearch_rewrite_fast_path_transaction(
        self,
        query: object,
        *,
        session_id: str,
        request_id: str,
    ) -> RewriteFastPathResult | None:
        """Linearize the irreversible rewrite and its conversation checkpoint."""
        if (
            not self._valid_deepresearch_rewrite_request_id(request_id)
            or self._deepresearch_rewrite_tx_uncertain
        ):
            return None
        instance = getattr(self, "_instance", None)
        send_lock = getattr(instance, "_interaction_send_lock", None)
        control_lock = getattr(instance, "_interaction_control_lock", None)
        keep_open = getattr(instance, "_should_keep_interaction_open_locked", None)
        if send_lock is None or control_lock is None or not callable(keep_open):
            return None

        async with send_lock:
            async with control_lock:
                if keep_open():
                    return None
                if not isinstance(query, str):
                    return None
                try:
                    query_sha256 = hashlib.sha256(
                        query.encode("utf-8")
                    ).hexdigest()
                except UnicodeEncodeError:
                    return None
                replay_found, replay = self._load_deepresearch_rewrite_replay(
                    session_id,
                    request_id,
                    query_sha256,
                )
                if replay_found:
                    return replay or self._deepresearch_rewrite_replay_conflict()
                from jiuwenswarm.agents.harness.common.tools.deepresearch import (
                    rewrite_tools,
                )

                published = False
                published_target = None

                def mark_published(publish_result: dict[str, object]) -> None:
                    nonlocal published, published_target
                    published = True
                    published_target = target_from_commit_result(
                        {
                            "status": "completed",
                            "report_path": publish_result.get("report_path"),
                            "revision_id": publish_result.get("revision_id"),
                        }
                    )

                observe_rewrite_publish = getattr(
                    rewrite_tools, "_observe_rewrite_publish"
                )
                with observe_rewrite_publish(mark_published):
                    fast_path_task = asyncio.create_task(
                        self._try_deepresearch_rewrite_fast_path(query)
                    )
                consumer_cancellation: asyncio.CancelledError | None = None
                task_failure: BaseException | None = None
                result = None
                try:
                    result = await asyncio.shield(fast_path_task)
                except asyncio.CancelledError as exc:
                    current_task = asyncio.current_task()
                    inner_task_cancelled = (
                        fast_path_task.done()
                        and fast_path_task.cancelled()
                        and current_task is not None
                        and current_task.cancelling() == 0
                    )
                    if inner_task_cancelled:
                        fast_path_task.result()
                    consumer_cancellation = exc
                    published_when_cancelled = published
                    if not published_when_cancelled:
                        fast_path_task.cancel()
                    while not fast_path_task.done():
                        try:
                            await asyncio.shield(fast_path_task)
                        except asyncio.CancelledError as repeated_exc:
                            if consumer_cancellation is None:
                                consumer_cancellation = repeated_exc
                        except Exception as inner_exc:  # noqa: BLE001
                            task_failure = inner_exc
                            break
                    if task_failure is None:
                        if fast_path_task.cancelled():
                            task_failure = asyncio.CancelledError()
                        else:
                            try:
                                result = fast_path_task.result()
                            except asyncio.CancelledError as inner_exc:
                                task_failure = inner_exc
                            except Exception as inner_exc:  # noqa: BLE001
                                task_failure = inner_exc
                    if not published_when_cancelled and not published:
                        raise consumer_cancellation from None
                except Exception as exc:  # noqa: BLE001
                    if not published:
                        raise
                    task_failure = exc

                if published:
                    result_target = (
                        target_from_commit_result(result.commit_result)
                        if isinstance(result, RewriteFastPathResult)
                        else None
                    )
                    invalid_published_result = (
                        task_failure is not None
                        or
                        not isinstance(result, RewriteFastPathResult)
                        or result.status != "completed"
                        or published_target is None
                        or result_target != published_target
                    )
                    if invalid_published_result:
                        if task_failure is not None:
                            logger.error(
                                "[DeepResearchRewriteFastPath] post-publish runner "
                                "failed session_id=%s error_type=%s",
                                session_id,
                                type(task_failure).__name__,
                            )
                        result = self._deepresearch_rewrite_publish_uncertain(
                            published_target
                        )
                        if published_target is None:
                            self._deepresearch_rewrite_tx_uncertain = True
                            if consumer_cancellation is not None:
                                raise consumer_cancellation
                            return result
                elif task_failure is not None:
                    if consumer_cancellation is not None:
                        raise consumer_cancellation
                    raise task_failure
                if result is None or result.status != "completed":
                    if not (
                        isinstance(result, RewriteFastPathResult)
                        and result.error_code == "PUBLISH_STATE_UNCERTAIN"
                    ):
                        return result
                persistence_task = asyncio.create_task(
                    self._persist_deepresearch_rewrite_fast_path_turn(
                        session_id=session_id,
                        request_id=request_id,
                        query_sha256=query_sha256,
                        query=query,
                        result=result,
                    )
                )
                while True:
                    try:
                        persisted = await asyncio.shield(persistence_task)
                        break
                    except asyncio.CancelledError as exc:
                        if consumer_cancellation is None:
                            consumer_cancellation = exc
                        if persistence_task.done():
                            if persistence_task.cancelled():
                                self._deepresearch_rewrite_tx_uncertain = True
                                raise exc
                            persisted = persistence_task.result()
                            break
                if (
                    not persisted
                    and result.error_code == "PUBLISH_STATE_UNCERTAIN"
                ):
                    self._deepresearch_rewrite_tx_uncertain = True
                if consumer_cancellation is not None:
                    raise consumer_cancellation
                if persisted:
                    return result
                return replace(
                    result,
                    error_code="CONTEXT_PERSIST_FAILED",
                    message=(
                        "改写版本已生成，但无法保存到当前会话上下文。"
                        "请刷新会话后再继续操作。"
                    ),
                )

    async def _persist_deepresearch_rewrite_fast_path_turn(
        self,
        *,
        session_id: str,
        request_id: str | None = None,
        query_sha256: str | None = None,
        query: object,
        result: RewriteFastPathResult,
    ) -> bool:
        """Persist a fast rewrite as one valid tool-call conversation turn."""
        if not isinstance(query, str):
            return False

        terminal_kind = self._deepresearch_rewrite_terminal_kind(result)
        html_target = target_from_commit_result(result.commit_result)
        if terminal_kind is None or html_target is None:
            return False
        replay_request_id = (
            request_id
            if self._valid_deepresearch_rewrite_request_id(request_id)
            and (
                terminal_kind == "report_delivery_failed"
                or terminal_kind == "publish_uncertain"
                or result.error_code is None
            )
            else None
        )
        session = self._active_deepresearch_session(session_id)
        instance = getattr(self, "_instance", None)
        react_agent = getattr(instance, "react_agent", None)
        init_context = getattr(react_agent, "_init_context", None)
        context_engine = getattr(react_agent, "context_engine", None)
        if (
            session is None
            or not callable(init_context)
            or context_engine is None
        ):
            return False

        context = None
        context_messages_before = None
        owned_messages = ()
        previous_target = None
        previous_replay_state = None
        messages_added = False
        state_updated = False

        async def rollback() -> bool:
            restored = True
            if messages_added and context is not None:
                try:
                    current_messages = context.get_messages()
                    before_count = len(context_messages_before)
                    owned_tail = current_messages[before_count:]
                    if (
                        current_messages[:before_count] != context_messages_before
                        or len(owned_tail) > len(owned_messages)
                        or any(
                            actual != expected
                            for actual, expected in zip(owned_tail, owned_messages)
                        )
                    ):
                        raise RuntimeError("context ownership cannot be proven")
                    if owned_tail:
                        context.pop_messages(len(owned_tail), with_history=True)
                    if context.get_messages() != context_messages_before:
                        raise RuntimeError("context rollback cannot be proven")
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.warning(
                        "[DeepResearchRewriteFastPath] message rollback failed "
                        "session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
            if state_updated:
                try:
                    restored_state = {
                        PENDING_HTML_EXPORT_STATE_KEY: previous_target,
                    }
                    if replay_request_id is not None:
                        restored_state[_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY] = (
                            previous_replay_state
                        )
                    session.update_state(restored_state)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.warning(
                        "[DeepResearchRewriteFastPath] state rollback failed "
                        "session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
            if messages_added or state_updated:
                try:
                    restored_context_states = await context_engine.save_contexts(session)
                    if (
                        not isinstance(restored_context_states, dict)
                        or session.get_state("context") != restored_context_states
                    ):
                        raise RuntimeError(
                            "context rollback local state was incomplete"
                        )
                except asyncio.CancelledError as rollback_exc:
                    logger.warning(
                        "[DeepResearchRewriteFastPath] context rollback save "
                        "cancelled session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.warning(
                        "[DeepResearchRewriteFastPath] context rollback save failed "
                        "session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
                try:
                    await session.commit()
                except asyncio.CancelledError as rollback_exc:
                    logger.warning(
                        "[DeepResearchRewriteFastPath] checkpoint rollback commit "
                        "cancelled session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.warning(
                        "[DeepResearchRewriteFastPath] checkpoint rollback commit "
                        "failed "
                        "session_id=%s error_type=%s",
                        session_id,
                        type(rollback_exc).__name__,
                    )
                    restored = False
            if not restored:
                self._deepresearch_rewrite_tx_uncertain = True
            return restored

        try:
            previous_target = session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
            if replay_request_id is not None:
                if not isinstance(query_sha256, str):
                    return False
                previous_replay_state = session.get_state(
                    _DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY
                )
            next_state = {
                PENDING_HTML_EXPORT_STATE_KEY: html_target.to_state(),
            }
            if replay_request_id is not None:
                existing_entries = self._decode_deepresearch_rewrite_replays(
                    previous_replay_state
                )
                encoded_entries = []
                for (
                    saved_request_id,
                    saved_query_sha256,
                    saved_result,
                    saved_target,
                ) in existing_entries:
                    if saved_request_id == replay_request_id:
                        continue
                    encoded_entries.append(
                        self._encode_deepresearch_rewrite_replay_entry(
                            saved_request_id,
                            saved_query_sha256,
                            saved_result,
                            RewriteHtmlTarget(
                                report_path=saved_target["report_path"],
                                revision_id=saved_target["revision_id"],
                            ),
                        )
                    )
                encoded_entries.append(
                    self._encode_deepresearch_rewrite_replay_entry(
                        replay_request_id,
                        query_sha256,
                        result,
                        html_target,
                    )
                )
                replay_state = {
                    "schema_version": _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION,
                    "entries": encoded_entries[
                        -_DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES:
                    ],
                }
                decoded_entries = self._decode_deepresearch_rewrite_replays(
                    replay_state
                )
                expected_request_ids = [
                    entry["request_id"] for entry in replay_state["entries"]
                ]
                if [entry[0] for entry in decoded_entries] != expected_request_ids:
                    return False
                next_state[_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY] = replay_state
            context = await init_context(session)
            context_messages_before = list(context.get_messages())
            tool_call_id = f"rewrite-fast-path-{uuid.uuid4().hex}"

            from openjiuwen.core.foundation.llm import (
                AssistantMessage,
                ToolMessage,
                UserMessage,
            )
            from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

            tool_call = ToolCall(
                id=tool_call_id,
                type="function",
                name="deepresearch_commit_rewrite",
                arguments="{}",
            )
            tool_result = json.dumps(
                result.commit_result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            owned_messages = (
                UserMessage(content=query),
                AssistantMessage(content="", tool_calls=[tool_call]),
                ToolMessage(content=tool_result, tool_call_id=tool_call_id),
                AssistantMessage(content=result.message),
            )
            messages_added = True
            await context.add_messages(list(owned_messages))
            current_messages = list(context.get_messages())
            before_count = len(context_messages_before)
            if (
                current_messages[:before_count] != context_messages_before
                or tuple(current_messages[before_count:]) != owned_messages
            ):
                raise RuntimeError("context message write was incomplete")
            state_updated = True
            session.update_state(next_state)
            if any(
                session.get_state(key) != expected
                for key, expected in next_state.items()
            ):
                raise RuntimeError("session state write was incomplete")
            context_states = await context_engine.save_contexts(session)
            if (
                not isinstance(context_states, dict)
                or session.get_state("context") != context_states
            ):
                raise RuntimeError("context local state write was incomplete")
            await session.commit()
            return True
        except asyncio.CancelledError:
            await rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[DeepResearchRewriteFastPath] persist tool result failed "
                "session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            await rollback()
            return False

    def _is_stream_rewrite_fast_path_eligible(
        self,
        request: AgentRequest,
        *,
        pending_goal_op: dict[str, Any] | None,
        attach_goal_request: bool,
        goal_stream_request: bool,
    ) -> bool:
        """Limit rewrite bypasses to an idle, ordinary single-agent turn."""
        if pending_goal_op is not None or attach_goal_request or goal_stream_request:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        if "answers" in params:
            return False
        if self._should_inject_into_existing_interaction(params):
            return False
        instance = getattr(self, "_instance", None)
        if instance is None or getattr(instance, "active_round", None) is not None:
            return False
        return not self._goal_record_is_active()

    @staticmethod
    def _fast_path_chunks(
        result: RewriteFastPathResult,
        *,
        request_id: str,
        channel_id: str,
    ) -> tuple[AgentResponseChunk, ...]:
        if result.status == "completed":
            payload = {
                "event_type": "chat.final",
                "content": result.message,
                "status": "completed",
            }
            if result.error_code:
                payload["error_code"] = result.error_code
        else:
            code = result.error_code or "INTERNAL_ERROR"
            payload = {
                "event_type": "chat.error",
                "error": f"改写失败（{code}）：{result.message}",
                "status": "error",
                "error_code": code,
            }
        return (
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload=payload,
                is_complete=False,
            ),
        )

    @staticmethod
    def _rewrite_html_followup_chunks(
        result: RewriteHtmlFollowupResult,
        *,
        request_id: str,
        channel_id: str,
    ) -> tuple[AgentResponseChunk, ...]:
        return (
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={
                    "event_type": "chat.final",
                    "content": result.message,
                },
                is_complete=False,
            ),
        )

    @staticmethod
    def _normalize_deepresearch_usage(value: object) -> dict[str, float] | None:
        keys = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_tokens",
            "input_cost",
            "output_cost",
            "total_cost",
        )
        if isinstance(value, dict):
            raw = value
        else:
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                try:
                    raw = model_dump()
                except Exception:  # noqa: BLE001
                    raw = {}
            else:
                raw = {key: getattr(value, key, None) for key in keys}
        if not isinstance(raw, dict):
            return None
        normalized = {}
        for key in keys:
            item = raw.get(key)
            if isinstance(item, (int, float)) and not isinstance(raw.get(key), bool):
                normalized[key] = operator.getitem(raw, key)
        return normalized or None

    @staticmethod
    def _deepresearch_usage_summary(
        usage: dict[str, float],
    ) -> dict[str, float | str]:
        summary: dict[str, float | str] = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        input_tokens = usage.get("input_tokens", 0)
        cache_tokens = usage.get("cache_tokens", 0)
        if input_tokens and cache_tokens:
            summary["cache_tokens"] = cache_tokens
            summary["cache_hit_rate"] = f"{cache_tokens / input_tokens:.1%}"
        for cost in ("input_cost", "output_cost", "total_cost"):
            if usage.get(cost, 0) > 0:
                summary[cost] = round(usage[cost], 6)
        return summary

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
        if not self._is_session_scoped_adapter:
            # 提前绑定 LLM trace ContextVar，使 supervisor task（由
            # _get_or_create_session_adapter → start_interaction →
            # self._instance.start() 经 asyncio.create_task 创建）继承正确的
            # session_id / request_id。否则 task 上下文里恒为空，
            # LLM_IO_TRACE 行全部 session_id='' request_id=''。
            _early_trace_sid = _LLM_TRACE_SESSION_ID.set(
                request.session_id or "default"
            )
            _early_trace_rid = _LLM_TRACE_REQUEST_ID.set(
                request.request_id or ""
            )
            _early_trace_iter = _LLM_TRACE_ITERATION.set(0)
            _early_trace_model = _LLM_TRACE_MODEL_NAME.set(
                getattr(self, "_model", None)
                and getattr(self._model, "model_config", None)
                and getattr(self._model.model_config, "model_name", "")
                or ""
            )
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id, request=request
            )
            request_mcp = await session_adapter.register_request_scoped_office_claw_mcp(request)
            try:
                with bind_active_office_claw_mcp_tools(
                    request_mcp.tool_ids if request_mcp is not None else ()
                ):
                    return await session_adapter.process_message_impl(request, inputs)
            finally:
                _LLM_TRACE_SESSION_ID.reset(_early_trace_sid)
                _LLM_TRACE_REQUEST_ID.reset(_early_trace_rid)
                _LLM_TRACE_ITERATION.reset(_early_trace_iter)
                _LLM_TRACE_MODEL_NAME.reset(_early_trace_model)
                try:
                    await session_adapter.cleanup_request_scoped_office_claw_mcp(request_mcp)
                finally:
                    await self._evict_idle_session_adapters()

        if self._deepresearch_rewrite_tx_uncertain:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error_code": _DEEPRESEARCH_CHECKPOINT_UNCERTAIN,
                    "error": _DEEPRESEARCH_CHECKPOINT_UNCERTAIN_MESSAGE,
                },
                metadata=request.metadata,
            )

        if self._instance is None:
            raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")

        self._inject_extension_config_into_inputs(inputs)

        _req_model = (request.params.get("model_name") or "") if isinstance(request.params, dict) else ""
        if not self._has_valid_model_config(_req_model):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "模型未正确配置，请先配置模型信息"},
                metadata=request.metadata,
            )

        session_id = request.session_id or "default"
        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent")

        # [CRON-CTX] 非流式入口：与流式对称，跨 channel 时强制 recover cron 历史。
        if str(session_id).startswith("cron_") and self._instance is not None:
            _cron_sess = getattr(self._instance, "_interaction_session", None)
            _cron_inner = getattr(_cron_sess, "_inner", None) if _cron_sess is not None else None
            if _cron_inner is not None and not self._cron_session_has_context(_cron_inner):
                try:
                    await self._get_adapter_checkpointer().pre_agent_execute(
                        _cron_inner, None
                    )
                    logger.info(
                        "[CRON-CTX] cron session re-recover(non-stream): session_id=%s "
                        "request_id=%s",
                        session_id, request.request_id,
                    )
                except Exception as _e:  # noqa: BLE001
                    logger.warning(
                        "[CRON-CTX] cron session re-recover(non-stream) failed: "
                        "session_id=%s error=%s request_id=%s",
                        session_id, _e, request.request_id,
                    )

        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(request.request_id or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self, "_model", None) and getattr(self._model, "model_config", None)
            and getattr(self._model.model_config, "model_name", "") or ""
        )
        self._current_request_route = {
            "session_id": session_id,
            "request_id": request.request_id or "",
            "channel_id": request.channel_id or "",
            "output_dir": self._deepresearch_artifact_output_dir(
                request.params.get("project_dir")
                if isinstance(request.params, dict)
                else None
            ),
        }

        slash_result = await self._handle_slash_command(
            query,
            session_id,
            mode,
            channel_id=request.channel_id,
        )
        if slash_result is not None:
            result_type = slash_result.get("result_type")
            if result_type == "goal_stream":
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "event_type": "goal.snapshot",
                        "action": slash_result.get("action"),
                        "goal": slash_result.get("goal"),
                        "message": "Goal is ready to run through a streaming request.",
                    },
                    metadata=request.metadata,
                )
            elif result_type == "goal_control":
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "goal.snapshot",
                        "action": slash_result.get("action"),
                        "goal": slash_result.get("goal"),
                        "cleared_goal": slash_result.get("cleared_goal"),
                    },
                    metadata=request.metadata,
                )
            elif result_type == "goal_confirm_required":
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "goal.confirm_required",
                        "existing_goal": slash_result.get("existing_goal"),
                        "requested_objective": slash_result.get("requested_objective"),
                    },
                    metadata=request.metadata,
                )
            elif result_type == "goal_error":
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "event_type": ERROR_EVENT_TYPE,
                        "code": slash_result.get("error_code", "goal_error"),
                        "message": slash_result.get("error", "goal operation failed"),
                        "goal": slash_result.get("goal"),
                    },
                    metadata=request.metadata,
                )
            followup_prompt = self._extract_followup_prompt(slash_result)
            if followup_prompt is not None:
                inputs = dict(inputs)
                inputs["query"] = followup_prompt
                inputs["_invoke_turn_id"] = request.request_id
            else:
                approval_chunks = slash_result.get("approval_chunks")
                if approval_chunks:
                    payload: dict[str, Any] = {"approval_chunks": approval_chunks}
                else:
                    content = slash_result.get("output", str(slash_result))
                    payload = {
                        "content": content,
                        "source": slash_result.get("source"),
                        "slash_command": slash_result.get("slash_command"),
                        "display_level": slash_result.get("display_level"),
                    }
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
            project_dir=(request.params.get("project_dir") if isinstance(request.params, dict) else None),
            params=request.params if isinstance(request.params, dict) else None,
        )
        token_cid = None
        token_perm = None
        token_perm_sid = None
        token_perm_agent = None
        request_env_tokens = None
        session_active = False
        session_task_registered = False
        perf_context_initialized = False
        image_files_token = None
        _run_span: Any = None
        collected_content: list[str] = []
        error_text: str | None = None
        interaction_stream = None
        interaction_stream_abort = True
        first_byte_marked = False
        perf_summary_status = "ok"
        try:
            self._runtime_cron_tool_context.remember_current_binding()
            token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
            token_perm = setup_permission_context(request)
            token_perm_sid = setup_permissions_session_scope(session_id)
            token_perm_agent = self._bind_agent_permissions_base()
            try:
                self._update_permission_rail(
                    self._config_base_cache, session_id=session_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] permission rail agent-level bind failed: %s",
                    exc,
                )
            resolved_model = self._resolve_model_for_request(request)
            self._apply_model_to_react_agent(resolved_model)
            await self._maybe_apply_pending_reload()
            request_env_tokens = self._bind_request_env_overlay()
            self._mark_session_active(session_id)
            session_active = True
            self._register_session_agent_task(session_id)
            session_task_registered = True
            if self._stream_event_rail is not None:
                self._stream_event_rail.reset_abort(session_id)
            set_perf_summary_context(
                getattr(self, "_request_summary_rail", None),
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                mode=mode,
            )
            perf_context_initialized = True
        except BaseException:
            cleanup_steps: list[Callable[[], None]] = []
            if perf_context_initialized:
                cleanup_steps.append(
                    lambda: clear_perf_summary_context(
                        getattr(self, "_request_summary_rail", None),
                        session_id=session_id,
                        request_id=request.request_id,
                    )
                )
            if session_task_registered:
                cleanup_steps.append(lambda: self._unregister_session_agent_task(session_id))
            if session_active:
                cleanup_steps.append(lambda: self._unmark_session_active(session_id))
            if request_env_tokens is not None:
                cleanup_steps.append(
                    lambda: self._reset_request_env_bindings(*request_env_tokens)
                )
            if token_perm_sid is not None:
                cleanup_steps.append(
                    lambda: reset_permissions_session_scope(token_perm_sid)
                )
            if token_perm is not None:
                cleanup_steps.append(lambda: cleanup_permission_context(token_perm))
            if token_cid is not None:
                cleanup_steps.append(lambda: TOOL_PERMISSION_CHANNEL_ID.reset(token_cid))
            cleanup_steps.extend(
                (
                    lambda: self._reset_runtime_cron_context(
                        cron_context_tokens,
                        suppress_errors=True,
                    ),
                    lambda: _LLM_TRACE_MODEL_NAME.reset(token_trace_model),
                    lambda: _LLM_TRACE_ITERATION.reset(token_trace_iter),
                    lambda: _LLM_TRACE_REQUEST_ID.reset(token_trace_rid),
                    lambda: _LLM_TRACE_SESSION_ID.reset(token_trace_sid),
                )
            )
            for cleanup_step in cleanup_steps:
                try:
                    cleanup_step()
                except BaseException:
                    continue
            raise
        try:
            await self._update_runtime_config(
                self._RuntimeConfig(
                    session_id=session_id,
                    mode=mode,
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    request_metadata=request.metadata,
                    trusted_dirs=inputs.get("trusted_dirs"),
                    cwd=inputs.get("cwd"),
                    workspace=inputs.get("workspace_dir"),
                    project_dir=inputs.get("project_dir"),
                    supports_user_interaction=inputs.get(
                        "supports_user_interaction", True
                    ),
                    interactive_ask=self._extract_request_interactive_ask(request),
                    request_system_prompt=self._extract_request_system_prompt(request),
                )
            )
            html_followup_result = None
            if self._is_stream_rewrite_fast_path_eligible(
                request,
                pending_goal_op=self._structured_goal_op_from_request(request),
                attach_goal_request=self._wants_attach_goal(request.params),
                goal_stream_request=False,
            ):
                html_followup_result = (
                    await self._run_deepresearch_rewrite_html_transaction(
                        query,
                        session_id=session_id,
                    )
                )
            if html_followup_result is not None:
                if html_followup_result.status != "completed":
                    perf_summary_status = "error"
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=html_followup_result.status == "completed",
                    payload={"content": html_followup_result.message},
                    metadata=request.metadata,
                )
            inputs = dict(inputs)
            inputs = self._prepare_multimodal_image_inputs(
                request,
                inputs,
            )
            enable_read_image_multimodal = self._native_image_input_enabled(
                self._config_cache,
                resolved_model,
            )
            inputs = self._prepare_react_image_tool_prompt(
                request,
                inputs,
                enable_read_image_multimodal=enable_read_image_multimodal,
            )
            await self._sync_prompt_attachments_for_request(session_id)
            from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                set_current_multimodal_image_files,
            )

            image_files_token = set_current_multimodal_image_files(
                inputs.pop("_multimodal_image_files", []) or []
            )
            # Sync single-agent / coding-agent observability with current
            # config before running, and open a root span so OtelCallbackHandler
            # has a parent for LLM/tool spans (see streaming path for details).
            from jiuwenswarm.agents.harness.agent_observability import (
                close_agent_run_span,
                open_agent_run_span,
                sync_agent_observability,
            )
            sync_agent_observability()
            _run_span = open_agent_run_span(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                mode=mode,
            )
            attach_goal = self._wants_attach_goal(request.params)
            dispatch_mode = self._resolve_input_dispatch_mode(request.params)
            if attach_goal:
                gm = self._get_goal_manager()
                peek = getattr(gm, "peek", None) if gm is not None else None
                record = peek() if callable(peek) else None
                status = getattr(getattr(record, "status", None), "value", getattr(record, "status", None))
                if status != "active":
                    return AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={"event_type": "runtime.accepted", "request_id": request.request_id},
                        metadata=request.metadata,
                    )
                interaction_stream = await self._instance.attach_output()
            elif self._should_inject_into_existing_interaction(request.params):
                # Idle → become the reader; busy → inject into the existing stream.
                interaction_stream = await self._instance.attach_output()
                await self._instance.send_input(
                    SendInputRequest(
                        request_id=request.request_id,
                        inputs=inputs,
                        mode=dispatch_mode,
                    )
                )
            else:
                interaction_stream = await self._instance.attach_output()
                if interaction_stream is not None:
                    await self._instance.send_input(
                        SendInputRequest(
                            request_id=request.request_id,
                            inputs=inputs,
                            mode=dispatch_mode,
                        )
                    )
            if interaction_stream is None:
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"event_type": "runtime.accepted", "request_id": request.request_id},
                    metadata=request.metadata,
                )
            async for chunk in interaction_stream:
                if hasattr(chunk, "type") and hasattr(chunk, "payload"):
                    if chunk.type in ("llm_output", "answer"):
                        text = (
                            chunk.payload.get("content", "")
                            if isinstance(chunk.payload, dict)
                            else str(chunk.payload)
                        )
                        if text:
                            collected_content.append(text)
                            if not first_byte_marked:
                                first_byte_marked = True
                                mark_request_first_byte()
                    else:
                        # check for error in other typed chunks (e.g. controller_output.task_failed)
                        parsed = self._parse_stream_chunk(chunk)
                        if parsed is not None:
                            event_type = str(parsed.get("event_type") or "").strip()
                            if event_type in ("chat.error", "error"):
                                err = parsed.get("error") or parsed.get("message") or ""
                                if err:
                                    error_text = str(err)
                                    perf_summary_status = "error"
                else:
                    parsed = self._parse_stream_chunk(chunk)
                    if parsed is not None:
                        text = parsed.get("content", "")
                        if text:
                            collected_content.append(text)
                            if not first_byte_marked:
                                first_byte_marked = True
                                mark_request_first_byte()
            interaction_stream_abort = False
        except asyncio.CancelledError:
            perf_summary_status = "cancelled"
            logger.info(
                "[JiuWenSwarmDeepAdapter] Agent 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                session_id,
            )
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_cancelled",
            )
            raise
        except Exception as e:
            perf_summary_status = "error"
            logger.error("[JiuWenSwarmDeepAdapter] Agent 任务执行异常: %s", e)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_error",
            )
            raise
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            if interaction_stream is not None:
                try:
                    await interaction_stream.close(
                        abort_active_round=interaction_stream_abort,
                    )
                except BaseException as exc:
                    cleanup_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup failed: "
                        "step=interaction_stream error_type=%s",
                        type(exc).__name__,
                    )
            cleanup_steps: list[tuple[str, Callable[[], None]]] = []
            if _run_span is not None:
                cleanup_steps.append(
                    ("run_span", lambda: close_agent_run_span(_run_span, session_id=session_id))
                )
            if image_files_token is not None:
                from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                    reset_current_multimodal_image_files,
                )
                cleanup_steps.append(
                    (
                        "multimodal_images",
                        lambda: reset_current_multimodal_image_files(image_files_token),
                    )
                )
            if perf_context_initialized:
                cleanup_steps.extend(
                    (
                        (
                            "perf_finalize",
                            lambda: finalize_perf_summary_request(
                                request.request_id,
                                status=perf_summary_status,
                            ),
                        ),
                        (
                            "perf_context",
                            lambda: clear_perf_summary_context(
                                getattr(self, "_request_summary_rail", None),
                                session_id=session_id,
                                request_id=request.request_id,
                            ),
                        ),
                    )
                )
            if session_task_registered:
                cleanup_steps.append(
                    ("session_task", lambda: self._unregister_session_agent_task(session_id))
                )
            if session_active:
                cleanup_steps.append(
                    ("session_active", lambda: self._unmark_session_active(session_id))
                )
            if request_env_tokens is not None:
                cleanup_steps.append(
                    (
                        "request_env",
                        lambda: self._reset_request_env_bindings(*request_env_tokens),
                    )
                )
            if token_perm_sid is not None:
                cleanup_steps.append(
                    (
                        "permission_session",
                        lambda: reset_permissions_session_scope(token_perm_sid),
                    )
                )
            if token_perm_agent is not None:
                cleanup_steps.append(
                    (
                        "permission_agent",
                        lambda: reset_permissions_agent_base(token_perm_agent),
                    )
                )
            if token_perm is not None:
                cleanup_steps.append(
                    ("permission", lambda: cleanup_permission_context(token_perm))
                )
            if token_cid is not None:
                cleanup_steps.append(
                    ("permission_channel", lambda: TOOL_PERMISSION_CHANNEL_ID.reset(token_cid))
                )
            cleanup_steps.extend(
                (
                    ("runtime_context", lambda: self._reset_runtime_cron_context(cron_context_tokens)),
                    ("trace_model", lambda: _LLM_TRACE_MODEL_NAME.reset(token_trace_model)),
                    ("trace_iteration", lambda: _LLM_TRACE_ITERATION.reset(token_trace_iter)),
                    ("trace_request", lambda: _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)),
                    ("trace_session", lambda: _LLM_TRACE_SESSION_ID.reset(token_trace_sid)),
                )
            )
            step_error = self._run_cleanup_steps(cleanup_steps)
            if cleanup_error is None:
                cleanup_error = step_error
            try:
                await self._maybe_apply_pending_reload()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] cleanup failed: "
                    "step=pending_reload error_type=%s",
                    type(exc).__name__,
                )
            # [CRON-CTX] 非流式 cron 执行收尾补 commit：对齐流式路径的自动落盘。
            # 根因：cron 走非流式 _inner_invoke，其 commit 挂在 need_cleanup 下，
            # 而 adapter 提前绑定了 session 致 need_cleanup=False，commit 被跳过，
            # cron_* 在 checkpointer 恒为空、web 恢复读不到历史。此处显式补一次。
            if str(session_id).startswith("cron_"):
                try:
                    await self._persist_cron_checkpoint(session_id, request.request_id)
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup failed: "
                        "step=cron_checkpoint error_type=%s",
                        type(exc).__name__,
                    )
            if active_error is None and cleanup_error is not None:
                raise cleanup_error

        content = "".join(collected_content) if collected_content else ""

        if not content and error_text:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": error_text},
                metadata=request.metadata,
            )

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
        # Start of this adapter's own share of the turn; reported on the
        # "entering runner streaming" line so the pre-dispatch work is visible.
        stream_impl_started_at = time.monotonic()
        if not self._is_session_scoped_adapter:
            # 提前绑定 LLM trace ContextVar，使 supervisor task（由
            # _get_or_create_session_adapter → start_interaction →
            # self._instance.start() 经 asyncio.create_task 创建）继承正确的
            # session_id / request_id。否则 task 上下文里恒为空，
            # LLM_IO_TRACE 行全部 session_id='' request_id=''。
            _early_trace_sid = _LLM_TRACE_SESSION_ID.set(
                request.session_id or "default"
            )
            _early_trace_rid = _LLM_TRACE_REQUEST_ID.set(
                request.request_id or ""
            )
            _early_trace_iter = _LLM_TRACE_ITERATION.set(0)
            _early_trace_model = _LLM_TRACE_MODEL_NAME.set(
                getattr(self, "_model", None)
                and getattr(self._model, "model_config", None)
                and getattr(self._model.model_config, "model_name", "")
                or ""
            )
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id, request=request
            )
            request_mcp = await session_adapter.register_request_scoped_office_claw_mcp(request)
            try:
                with bind_active_office_claw_mcp_tools(
                    request_mcp.tool_ids if request_mcp is not None else ()
                ):
                    async for chunk in session_adapter.process_message_stream_impl(request, inputs):
                        yield chunk
                return
            finally:
                _LLM_TRACE_SESSION_ID.reset(_early_trace_sid)
                _LLM_TRACE_REQUEST_ID.reset(_early_trace_rid)
                _LLM_TRACE_ITERATION.reset(_early_trace_iter)
                _LLM_TRACE_MODEL_NAME.reset(_early_trace_model)
                try:
                    await session_adapter.cleanup_request_scoped_office_claw_mcp(request_mcp)
                finally:
                    await self._evict_idle_session_adapters()

        if self._deepresearch_rewrite_tx_uncertain:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error_code": _DEEPRESEARCH_CHECKPOINT_UNCERTAIN,
                    "error": _DEEPRESEARCH_CHECKPOINT_UNCERTAIN_MESSAGE,
                },
                is_complete=True,
                metadata=request.metadata or {},
            )
            return

        if self._instance is None:
            raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")

        self._inject_extension_config_into_inputs(inputs)

        _req_model = (request.params.get("model_name") or "") if isinstance(request.params, dict) else ""
        if not self._has_valid_model_config(_req_model):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.error", "error": "模型未正确配置，请先配置模型信息"},
                is_complete=True,
            )
            return

        # SkillTurbo resume: 检测 answers + resume_ctx，走 SkillTurbo resume 路径
        skill_turbo_resume_stream = await self._try_skill_turbo_resume(request, inputs)
        if skill_turbo_resume_stream is not None:
            async for chunk in skill_turbo_resume_stream:
                yield chunk
            return

        session_id = request.session_id or "default"
        rid = request.request_id
        cid = request.channel_id
        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent")

        # [CRON-CTX] 跨 channel（cron 写 / web 读）恢复：cron 与 web 是不同 channel_key，
        # 各自持有独立 JiuWenSwarm → 独立 adapter → 独立 _session_adapters 缓存。web adapter
        # 的 session 在 cron 执行前 pre_run 过、读的是旧 checkpoint；cron 落盘新 context 后
        # web adapter HIT cache 复用、不重跑 pre_run，session.global_state 无 context。
        # 此处检测到无 context 时强制重新 recover 一次，把 checkpoint 里 cron 的历史灌进
        # 当前 session。姿势同 Session.pre_run 内部：直接调
        # checkpointer.pre_agent_execute(_inner, None)，绕过 _pre_run_done 守卫。
        # recover→_restore_state→set_state 会覆盖整个 global_state（本场景即所求：从 checkpoint
        # 还原）。仅 cron_* session 触发，避免影响普通 web 会话。
        if str(session_id).startswith("cron_") and self._instance is not None:
            _cron_sess = getattr(self._instance, "_interaction_session", None)
            _cron_inner = getattr(_cron_sess, "_inner", None) if _cron_sess is not None else None
            if _cron_inner is not None and not self._cron_session_has_context(_cron_inner):
                try:
                    await self._get_adapter_checkpointer().pre_agent_execute(
                        _cron_inner, None
                    )
                    logger.info(
                        "[CRON-CTX] cron session re-recover: session_id=%s request_id=%s",
                        session_id, request.request_id,
                    )
                except Exception as _e:  # noqa: BLE001
                    logger.warning(
                        "[CRON-CTX] cron session re-recover failed: session_id=%s "
                        "error=%s request_id=%s",
                        session_id, _e, request.request_id,
                    )

        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(rid or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self, "_model", None) and getattr(self._model, "model_config", None)
            and getattr(self._model.model_config, "model_name", "") or ""
        )
        self._current_request_route = {
            "session_id": session_id,
            "request_id": rid or "",
            "channel_id": cid or "",
            "output_dir": self._deepresearch_artifact_output_dir(
                request.params.get("project_dir")
                if isinstance(request.params, dict)
                else None
            ),
        }

        # Team 模式处理
        if mode in ("team", "team.plan", "code.team"):
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import process_team_message_stream

            set_perf_summary_context(
                getattr(self, "_request_summary_rail", None),
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                mode=mode,
            )
            perf_summary_status = "ok"
            try:
                resolved_model = self._resolve_model_for_request(request)
                self._apply_model_to_react_agent(resolved_model)
                inputs = self._prepare_multimodal_image_inputs(request, inputs)
                enable_read_image_multimodal = self._native_image_input_enabled(
                    self._config_cache,
                    resolved_model,
                )
                inputs = self._prepare_react_image_tool_prompt(
                    request,
                    inputs,
                    enable_read_image_multimodal=enable_read_image_multimodal,
                )
                resolved_language = self._resolve_runtime_language()
                resolved_channel = str(cid or self._resolve_prompt_channel(session_id) or "web").strip() or "web"
                if self._runtime_prompt_rail:
                    self._runtime_prompt_rail.set_model_name(self._resolve_model_name())
                    self._runtime_prompt_rail.set_mode(mode)
                    self._runtime_prompt_rail.set_session_id(session_id)
                self._write_runtime_state(
                    mode=mode,
                    language=resolved_language,
                    channel=resolved_channel,
                    session_id=session_id,
                    project_dir=inputs.get("project_dir")
                    or inputs.get("cwd")
                    or self._project_dir
                    or self._workspace_dir,
                )

                from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

                _tenant_agent_id, _tenant_service_id, workspace_key = (
                    TenantAgentPool.extract_ids(request)
                )
                team_stream_kwargs = {
                    "config_base": self._config_base_cache,
                    "sessions_root": resolve_tenant_sessions_dir(workspace_key),
                }
                if (
                    evolution_slash_command_name(str(inputs.get("query") or ""))
                    == "evolve_rebuild"
                ):
                    team_stream_kwargs["rebuild_skill"] = (
                        self.generate_evolution_merge_version
                    )
                async for chunk in process_team_message_stream(
                    request,
                    inputs,
                    self._instance,
                    **team_stream_kwargs,
                ):
                    maybe_mark_answer_first_byte(getattr(chunk, "payload", None))
                    yield chunk
            except asyncio.CancelledError:
                perf_summary_status = "cancelled"
                raise
            except Exception:
                perf_summary_status = "error"
                raise
            finally:
                finalize_perf_summary_request(request.request_id, status=perf_summary_status)
                clear_perf_summary_context(
                    getattr(self, "_request_summary_rail", None),
                    session_id=session_id,
                    request_id=request.request_id,
                )
                _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
                _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
                _LLM_TRACE_ITERATION.reset(token_trace_iter)
                _LLM_TRACE_MODEL_NAME.reset(token_trace_model)
            return

        # Auto-Harness 模式处理
        if mode == "auto_harness":
            set_perf_summary_context(
                getattr(self, "_request_summary_rail", None),
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                mode=mode,
            )
            perf_summary_status = "ok"
            try:
                if self._auto_harness_service is None:
                    self._auto_harness_service = AutoHarnessService(
                        self._stream_event_rail,
                        agent=self._instance,
                    )

                await self._update_runtime_config(
                    self._RuntimeConfig(
                        session_id=session_id,
                        mode=mode,
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        request_metadata=request.metadata,
                        trusted_dirs=inputs.get("trusted_dirs"),
                        cwd=inputs.get("cwd"),
                        project_dir=inputs.get("project_dir"),
                        supports_user_interaction=inputs.get(
                            "supports_user_interaction", True
                        ),
                        interactive_ask=self._extract_request_interactive_ask(request),
                        request_system_prompt=self._extract_request_system_prompt(request),
                    )
                )

                activate_response = request.params.get("activate_response")
                if isinstance(activate_response, dict):
                    async for chunk in self._auto_harness_service.resume_activate(
                        session_id, rid, cid, activate_response
                    ):
                        maybe_mark_answer_first_byte(getattr(chunk, "payload", None))
                        yield chunk
                else:
                    resolved_model = self._resolve_model_for_request(request)
                    if self._auto_harness_service.is_activate_only_request(request, query):
                        async for chunk in self._auto_harness_service.run_activate_only(
                            request, session_id, rid, query, model=resolved_model
                        ):
                            maybe_mark_answer_first_byte(getattr(chunk, "payload", None))
                            yield chunk
                    elif self._auto_harness_service.is_implement_only_request(request, query):
                        async for chunk in self._auto_harness_service.run_implement_only(
                            request, session_id, rid, query, model=resolved_model
                        ):
                            maybe_mark_answer_first_byte(getattr(chunk, "payload", None))
                            yield chunk
                    else:
                        async for chunk in self._auto_harness_service.run(
                            request, session_id, rid, query=query, model=resolved_model
                        ):
                            maybe_mark_answer_first_byte(getattr(chunk, "payload", None))
                            yield chunk
            except asyncio.CancelledError:
                perf_summary_status = "cancelled"
                raise
            except Exception:
                perf_summary_status = "error"
                raise
            finally:
                finalize_perf_summary_request(request.request_id, status=perf_summary_status)
                clear_perf_summary_context(
                    getattr(self, "_request_summary_rail", None),
                    session_id=session_id,
                    request_id=request.request_id,
                )
                _LLM_TRACE_SESSION_ID.reset(token_trace_sid)
                _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)
                _LLM_TRACE_ITERATION.reset(token_trace_iter)
                _LLM_TRACE_MODEL_NAME.reset(token_trace_model)
            return

        # 拦截斜杠命令 / 结构化 command.goal
        goal_stream_request = False
        goal_snapshot: dict[str, Any] | None = None
        goal_action: str | None = None
        pending_goal_op: dict[str, Any] | None = None
        attach_goal_request = self._wants_attach_goal(request.params)
        # Structured command.goal set/resume (Web/TUI): same attach→set→read path.
        # Plain chat text "/goal ..." is NOT parsed here for Web — only TUI slash.
        pending_goal_op = self._structured_goal_op_from_request(request)
        if self._should_parse_tui_goal_slash(
            pending_goal_op=pending_goal_op,
            attach_goal_request=attach_goal_request,
            channel_id=request.channel_id,
            query=query,
        ):
            intent = self._parse_goal_slash_intent(query)
            if intent is not None and intent.get("action") in {"set", "resume"}:
                # Defer set/resume until after attach_output (attach → set).
                pending_goal_op = intent
        slash_result = None
        if pending_goal_op is None and not attach_goal_request:
            slash_result = await self._handle_slash_command(
                query,
                session_id,
                mode,
                channel_id=request.channel_id,
            )
        if slash_result is not None:
            result_type = slash_result.get("result_type")
            if result_type == "goal_stream":
                # Legacy path: control already applied; attach will ensure work.
                goal_stream_request = True
                goal_snapshot = slash_result.get("goal")
                goal_action = slash_result.get("action")
            elif result_type == "goal_control":
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={
                        "event_type": "goal.snapshot",
                        "action": slash_result.get("action"),
                        "goal": slash_result.get("goal"),
                        "cleared_goal": slash_result.get("cleared_goal"),
                    },
                    is_complete=True,
                )
                return
            elif result_type == "goal_confirm_required":
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={
                        "event_type": "goal.confirm_required",
                        "existing_goal": slash_result.get("existing_goal"),
                        "requested_objective": slash_result.get("requested_objective"),
                    },
                    is_complete=True,
                )
                return
            elif result_type == "goal_error":
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={
                        "event_type": ERROR_EVENT_TYPE,
                        "code": slash_result.get("error_code", "goal_error"),
                        "message": slash_result.get("error", "goal operation failed"),
                        "goal": slash_result.get("goal"),
                    },
                    is_complete=True,
                )
                return
            followup_prompt = self._extract_followup_prompt(slash_result)
            if followup_prompt is not None:
                inputs = dict(inputs)
                inputs["query"] = followup_prompt
                inputs["_invoke_turn_id"] = request.request_id
            else:
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
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "chat.final",
                            "content": content,
                            "source": slash_result.get("source"),
                            "slash_command": slash_result.get("slash_command"),
                            "display_level": slash_result.get("display_level"),
                        },
                        is_complete=True,
                    )
                return

        has_streamed_content = False
        had_reasoning_output = False
        visible_text_since_last_final = False
        # Survives tool_call resets of visible_text_since_last_final; used by the
        # stream-end reasoning-only fallback so pre-tool deltas are not forgotten.
        had_visible_text_ever = False
        segment_streamed_text = ""
        accumulated_text = ""
        accumulated_reasoning = ""
        had_assistant_output = False
        emitted_terminal_chat_final = False
        usage_accumulator = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            # Prompt tokens the provider served from its KV cache. Reported as a
            # hit rate below: it is the direct read on whether the cached prefix
            # is holding steady across turns, which is what keeps a 30k-token
            # prompt from being re-processed on every message.
            "cache_tokens": 0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }
        emitted_ask_user_request_ids: set[str] = set()
        accounted_deepresearch_usage_ids: set[str] = set()
        approved_plan_exit_tool_call_id = self._approved_plan_exit_resume_tool_call_id(
            request.params
        )
        saw_approved_plan_exit_result = False

        def should_skip_duplicate_ask_user(parsed: dict | None) -> bool:
            if not isinstance(parsed, dict):
                return False
            if parsed.get("event_type") != "chat.ask_user_question":
                return False
            request_id = str(parsed.get("request_id") or "").strip()
            if not request_id:
                return False
            if request_id in emitted_ask_user_request_ids:
                return True
            emitted_ask_user_request_ids.add(request_id)
            return False

        first_byte_marked = False
        first_answer_marked = False

        def _mark_first_byte_once() -> None:
            nonlocal first_byte_marked
            if first_byte_marked:
                return
            first_byte_marked = True
            mark_request_first_byte()

        def _mark_first_answer_once() -> None:
            nonlocal first_answer_marked
            if first_answer_marked:
                return
            first_answer_marked = True
            mark_request_first_answer()

        def note_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal had_assistant_output, emitted_terminal_chat_final
            nonlocal saw_approved_plan_exit_result, had_reasoning_output
            nonlocal visible_text_since_last_final, segment_streamed_text
            nonlocal has_streamed_content, had_visible_text_ever
            event_type = payload.get("event_type")
            if event_type == "chat.reasoning":
                had_reasoning_output = True
            if event_type == "chat.tool_call":
                # New tool round: forget prior-segment deltas / has_streamed so
                # post-tool answer drain can rescue empty streams, without
                # inheriting pre-tool llm_output flags that force a re-drain.
                # had_visible_text_ever is intentionally not cleared — terminal
                # fallback still needs to know the user already saw pre-tool text.
                segment_streamed_text = ""
                has_streamed_content = False
                visible_text_since_last_final = False
            if (
                event_type == "chat.delta"
                and str(payload.get("content") or "").strip()
            ):
                segment_streamed_text += str(payload.get("content") or "")
                visible_text_since_last_final = True
                had_visible_text_ever = True
            elif event_type == "chat.final":
                visible_text_since_last_final = False
                # Do not clear segment_streamed_text on empty trailer finals.
                # Drain clears final content before note; wiping segment here
                # makes a subsequent check miss already-streamed llm_output and
                # re-emit the same body (history/front show the reply twice).
                # Segment resets on tool_call / next invocation instead.
            is_plan_exit_result = (
                event_type == "chat.tool_result"
                and str(payload.get("tool_name") or "") == "exit_plan_mode"
            )
            if (
                approved_plan_exit_tool_call_id
                and is_plan_exit_result
                and str(payload.get("tool_call_id") or "")
                == approved_plan_exit_tool_call_id
            ):
                saw_approved_plan_exit_result = True
            # first_byte: first history-visible assistant event (matches history.jsonl).
            if event_type in (
                "chat.delta",
                "chat.final",
                "chat.tool_call",
                "chat.reasoning",
            ):
                _mark_first_byte_once()
            # first_answer: first answer token only.
            if event_type in ("chat.delta", "chat.final"):
                _mark_first_answer_once()
            if event_type in ("chat.delta", "chat.reasoning", "chat.final"):
                had_assistant_output = True
            if event_type == "chat.final":
                emitted_terminal_chat_final = True
            # Persist goal-completed cards at the stream yield choke point so
            # every goal.updated path (typed chunk / dict chunk) is covered once
            # without touching the pure payload parser.
            if event_type == GOAL_UPDATED_EVENT_TYPE:
                goal_obj = payload.get("goal")
                self._record_goal_completed_history_if_needed(
                    session_id=session_id,
                    channel_id=cid,
                    channel_metadata=request.metadata if isinstance(request.metadata, dict) else None,
                    mode=mode,
                    goal_payload=goal_obj if isinstance(goal_obj, dict) else None,
                )
            return payload

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
            project_dir=(request.params.get("project_dir") if isinstance(request.params, dict) else None),
            params=request.params if isinstance(request.params, dict) else None,
        )
        token_cid = None
        token_perm = None
        token_perm_sid = None
        token_perm_agent = None
        request_env_tokens = None
        session_active = False
        session_task_registered = False
        perf_context_initialized = False
        perf_usage_fallback: dict[str, int] | None = None
        initialization_complete = False
        stream_consumer_cancelled = False
        image_files_token = None
        _run_span: Any = None
        _debug_logger = None
        _debug_trace_token = None  # reset token for the ContextVar-bound logger
        interaction_stream = None
        interaction_stream_abort = True
        perf_summary_status = "ok"
        hitl_pending_stream = False
        try:
            self._runtime_cron_tool_context.remember_current_binding()
            token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
            token_perm = setup_permission_context(request)
            token_perm_sid = setup_permissions_session_scope(session_id)
            token_perm_agent = self._bind_agent_permissions_base()
            try:
                self._update_permission_rail(
                    self._config_base_cache, session_id=session_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] permission rail agent-level bind failed: %s",
                    exc,
                )
            # 按请求选择模型（企业配置已在 create_instance 合并）
            resolved_model = self._resolve_model_for_request(request)
            self._apply_model_to_react_agent(resolved_model)
            await self._maybe_apply_pending_reload()
            request_env_tokens = self._bind_request_env_overlay()
            self._mark_session_active(session_id)
            session_active = True
            self._register_session_agent_task(session_id)
            session_task_registered = True
            set_perf_summary_context(
                getattr(self, "_request_summary_rail", None),
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                mode=mode,
            )
            perf_context_initialized = True
            initialization_complete = True
            await self._update_runtime_config(
                self._RuntimeConfig(
                    session_id=session_id,
                    mode=mode,
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    request_metadata=request.metadata,
                    trusted_dirs=inputs.get("trusted_dirs"),
                    cwd=inputs.get("cwd"),
                    workspace=inputs.get("workspace_dir"),
                    project_dir=inputs.get("project_dir"),
                    supports_user_interaction=inputs.get(
                        "supports_user_interaction", True
                    ),
                    interactive_ask=self._extract_request_interactive_ask(request),
                    request_system_prompt=self._extract_request_system_prompt(request),
                )
            )
            direct_path_eligible = self._is_stream_rewrite_fast_path_eligible(
                request,
                pending_goal_op=pending_goal_op,
                attach_goal_request=attach_goal_request,
                goal_stream_request=goal_stream_request,
            )
            html_followup_result = None
            if direct_path_eligible:
                html_followup_result = (
                    await self._run_deepresearch_rewrite_html_transaction(
                        query,
                        session_id=session_id,
                    )
                )
            if html_followup_result is not None:
                if html_followup_result.status != "completed":
                    perf_summary_status = "error"
                for chunk in self._rewrite_html_followup_chunks(
                    html_followup_result,
                    request_id=rid,
                    channel_id=cid,
                ):
                    yield chunk
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=None,
                    is_complete=True,
                )
                interaction_stream_abort = False
                return

            if direct_path_eligible:
                fast_path_result = (
                    await self._run_deepresearch_rewrite_fast_path_transaction(
                        query,
                        session_id=session_id,
                        request_id=rid,
                    )
                )
                if fast_path_result is not None:
                    if (
                        fast_path_result.status != "completed"
                        or fast_path_result.error_code is not None
                    ):
                        perf_summary_status = "error"
                    for chunk in self._fast_path_chunks(
                        fast_path_result,
                        request_id=rid,
                        channel_id=cid,
                    ):
                        yield chunk
                    usage = self._normalize_deepresearch_usage(
                        fast_path_result.usage_metadata
                    )
                    if usage is not None and usage.get("total_tokens", 0) > 0:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload={
                                "event_type": "chat.usage_summary",
                                "session_id": session_id,
                                "usage": self._deepresearch_usage_summary(usage),
                                "model": self._resolve_model_name(),
                            },
                            is_complete=False,
                        )
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=None,
                        is_complete=True,
                    )
                    interaction_stream_abort = False
                    return
            if self._stream_event_rail is not None:
                self._stream_event_rail.reset_abort(session_id)
            inputs = dict(inputs)
            inputs = self._prepare_multimodal_image_inputs(
                request,
                inputs,
            )
            enable_read_image_multimodal = self._native_image_input_enabled(
                self._config_cache,
                resolved_model,
            )
            image_tool_fallback_notice = self._build_image_tool_fallback_notice(
                request,
                enable_read_image_multimodal=enable_read_image_multimodal,
                model=resolved_model,
            )
            inputs = self._prepare_react_image_tool_prompt(
                request,
                inputs,
                enable_read_image_multimodal=enable_read_image_multimodal,
            )
            if image_tool_fallback_notice is not None:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=image_tool_fallback_notice,
                    is_complete=False,
                )
            await self._sync_prompt_attachments_for_request(session_id)
            from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                set_current_multimodal_image_files,
            )

            image_files_token = set_current_multimodal_image_files(
                inputs.pop("_multimodal_image_files", []) or []
            )
            # Resolve debug-trace settings early: its otel_enabled drives the
            # OTel force-enable below. Best-effort (config read never raises).
            from jiuwenswarm.server.runtime.debug_trace.config import (
                resolve_debug_trace_settings,
            )
            _dbg_settings = resolve_debug_trace_settings(
                mode=mode, request_debug=bool(inputs.get("_request_debug"))
            )
            # Sync single-agent / coding-agent observability with current config
            # before running.
            from jiuwenswarm.agents.harness.agent_observability import (
                close_agent_run_span,
                open_agent_run_span,
                sync_agent_observability,
            )
            sync_agent_observability(force=_dbg_settings.otel_enabled)
            _run_span = open_agent_run_span(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                mode=mode,
            )
            _otel_trace_id = ""
            _otel_span_id = ""
            if _run_span is not None:
                try:
                    _span_ctx = _run_span.root_span.get_span_context()
                    _otel_trace_id = format(_span_ctx.trace_id, "032x")
                    _otel_span_id = format(_span_ctx.span_id, "016x")
                except Exception:
                    pass
            try:
                from jiuwenswarm.server.runtime.debug_trace.context import (
                    register_debug_trace_logger,
                    set_debug_trace_logger,
                )
                from jiuwenswarm.server.runtime.debug_trace.paths import debug_trace_file
                from jiuwenswarm.server.runtime.debug_trace.stream_logger import (
                    DebugTraceLogger,
                )
                if _dbg_settings.enabled and _dbg_settings.dump_enabled:
                    _debug_logger = DebugTraceLogger(
                        file_path=debug_trace_file(mode, session_id),
                        mode=mode,
                        session_id=session_id,
                        request_id=rid,
                        settings=_dbg_settings,
                    )
                    _debug_logger.start_run(
                        input_text=inputs.get("query"),
                        otel_trace_id=_otel_trace_id,
                        otel_span_id=_otel_span_id,
                    )
                    # Publish the logger so subagent dispatch sites (TaskTool in
                    # the SDK, AgentTool in jiuwenswarm) can capture subagent
                    # streams into this same dump. asyncio.create_task copies the
                    # current ContextVar, so background subagents inherit it too.
                    _debug_trace_token = set_debug_trace_logger(_debug_logger)
                    # Also register by session_id: TaskTool.invoke runs in the
                    # DeepAgent's supervisor task (created at session setup, before
                    # any /debug request), so the per-request ContextVar above
                    # can't reach it. Dispatch sites fall back to this registry
                    # via get_debug_trace_logger_for_session.
                    register_debug_trace_logger(session_id, _debug_logger)
            except Exception as _dbg_exc:
                logger.warning("[JiuWenSwarmDeepAdapter] debug trace init failed: %s", _dbg_exc)
                _debug_logger = None

            async def _yield_runtime_accepted() -> AsyncIterator[AgentResponseChunk]:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "runtime.accepted", "request_id": rid},
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=None,
                    is_complete=True,
                )

            if pending_goal_op is not None:
                interaction_stream = await self._instance.attach_output()
                control = await self._dispatch_goal_control(
                    action=str(pending_goal_op.get("action") or "get"),
                    objective=pending_goal_op.get("objective")
                    if isinstance(pending_goal_op.get("objective"), str)
                    else None,
                    overwrite_confirmed=bool(pending_goal_op.get("overwrite_confirmed", False)),
                    token_budget=pending_goal_op.get("token_budget"),
                    max_attempts=pending_goal_op.get("max_attempts"),
                    session_id=session_id,
                )
                result_type = (control or {}).get("result_type")
                if result_type == "goal_confirm_required":
                    if interaction_stream is not None:
                        await interaction_stream.close(abort_active_round=False)
                        interaction_stream = None
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={
                            "event_type": "goal.confirm_required",
                            "existing_goal": control.get("existing_goal"),
                            "requested_objective": control.get("requested_objective"),
                        },
                        is_complete=True,
                    )
                    interaction_stream_abort = False
                    return
                if result_type == "goal_error":
                    if interaction_stream is not None:
                        await interaction_stream.close(abort_active_round=False)
                        interaction_stream = None
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={
                            "event_type": ERROR_EVENT_TYPE,
                            "code": control.get("error_code", "goal_error"),
                            "message": control.get("error", "goal operation failed"),
                            "goal": control.get("goal"),
                        },
                        is_complete=True,
                    )
                    interaction_stream_abort = False
                    return
                goal_snapshot = (control or {}).get("goal")
                goal_action = (control or {}).get("action")
                if goal_snapshot is not None:
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={
                            "event_type": "goal.snapshot",
                            "action": goal_action,
                            "goal": goal_snapshot,
                        },
                        is_complete=False,
                    )
                self._record_goal_set_history_if_needed(
                    request,
                    action=goal_action if isinstance(goal_action, str) else None,
                    result_type=result_type if isinstance(result_type, str) else None,
                    goal_payload=goal_snapshot if isinstance(goal_snapshot, dict) else None,
                )
                # Only keep the lease when set/resume left an ACTIVE goal to run.
                if result_type != "goal_stream" or interaction_stream is None:
                    if interaction_stream is not None:
                        await interaction_stream.close(abort_active_round=False)
                        interaction_stream = None
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
            elif goal_stream_request or attach_goal_request:
                if goal_snapshot is not None:
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={
                            "event_type": "goal.snapshot",
                            "action": goal_action,
                            "goal": goal_snapshot,
                        },
                        is_complete=False,
                    )
                self._record_goal_set_history_if_needed(
                    request,
                    action=goal_action if isinstance(goal_action, str) else None,
                    result_type="goal_stream" if goal_stream_request else "goal_control",
                    goal_payload=goal_snapshot if isinstance(goal_snapshot, dict) else None,
                )
                if attach_goal_request:
                    gm = self._get_goal_manager()
                    peek = getattr(gm, "peek", None) if gm is not None else None
                    record = peek() if callable(peek) else None
                    status = getattr(getattr(record, "status", None), "value", getattr(record, "status", None))
                    if status != "active":
                        async for chunk in _yield_runtime_accepted():
                            yield chunk
                        interaction_stream_abort = False
                        return
                interaction_stream = await self._instance.attach_output()
                if interaction_stream is None:
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
            elif self._should_inject_into_existing_interaction(request.params):
                # Idle → become the reader; busy → inject and accept.
                # Interrupt resumes (permission/confirm/ask-user) must send_input
                # even when Goal already holds the output lease.
                interaction_stream = await self._instance.attach_output()
                # Last stop before the message is injected into the running
                # single-agent interaction (interrupt / HITL resume).
                server_logger.info(
                    "[AgentServer] message entering runner interaction inject: session_id=%s"
                    " request_id=%s channel_id=%s mode=%s query=%s",
                    session_id,
                    rid,
                    cid,
                    mode,
                    preview_text(inputs.get("query", "")),
                )
                await self._instance.send_input(
                    SendInputRequest(
                        request_id=rid,
                        inputs=inputs,
                        mode=self._resolve_input_dispatch_mode(request.params),
                    )
                )
                if interaction_stream is None:
                    # The interrupted chat.send consumer releases its output
                    # lease asynchronously.  A resume arriving in that tiny
                    # window used to receive only runtime.accepted, while the
                    # original consumer had already emitted its complete frame;
                    # no client then remained to relay the resumed final reply.
                    # Give that predecessor a bounded chance to unwind before
                    # falling back to the ACK-only path for genuinely busy
                    # sessions owned by another consumer.
                    interaction_stream = await self._reattach_interrupt_output(
                        request.session_id or "default"
                    )
                if interaction_stream is None:
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
            else:
                interaction_stream = await self._instance.attach_output()
                if interaction_stream is None:
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
                # Last stop before the message enters the single-agent runner
                # streaming path. ``prepare_ms`` covers everything this adapter
                # did with the turn before handing it over.
                server_logger.info(
                    "[AgentServer] message entering runner streaming: session_id=%s request_id=%s"
                    " channel_id=%s mode=%s prepare_ms=%.1f query=%s",
                    session_id,
                    rid,
                    cid,
                    mode,
                    (time.monotonic() - stream_impl_started_at) * 1000,
                    preview_text(inputs.get("query", "")),
                )
                await self._instance.send_input(
                    SendInputRequest(
                        request_id=rid,
                        inputs=inputs,
                        mode=self._resolve_input_dispatch_mode(request.params),
                    )
                )
            run_failure: tuple[str, str] | None = None
            emitted_chat_error = False
            # HITL 暂停标志：本轮有 ask_user 卡片发出时置位，收尾据此发
            # chat.invocation_paused 终结帧（而非普通完成帧），避免前端把"等待用户
            # 输入"误判为"任务完成"。镜像 vendor(clowder-ai) 的 _detect_hitl_pause。
            hitl_pending_stream = False
            # HITL 检测到 ask_user 后，跳过后续所有 chunk（chat.final / chat.delta /
            # chat.reasoning 等），由循环后的 chat.invocation_paused 终结帧收尾。
            # 镜像 enterprise_dev 的 suppress_stream_after_hitl 机制。
            suppress_stream_after_hitl = False
            # Start of the wait for the runner's first chunk; every branch above
            # has either handed the message over or attached to a running round.
            runner_stream_started_at = time.monotonic()
            first_chunk_seen = False
            # A plain user-chat consumer stream (not a command.goal / attach-goal
            # stream). Only these may be hijacked by a goal round before the user
            # round produces its first visible token; used to allow a bubble
            # split even when ``prev`` is still None. Pure goal streams keep the
            # strict prev=="user" rule inside ``_begin_visible_chat_content``.
            stream_is_user_originated = (
                pending_goal_op is None
                and not attach_goal_request
                and not goal_stream_request
            )
            async for chunk in interaction_stream:
                if suppress_stream_after_hitl:
                    continue
                # [DIAG] HITL resume 调试：对照 forwarder([DeepAgent][fwd]) 转发的 chunk，
                # 确认主循环实际从 interaction_stream 消费到什么类型。
                _stream_ct = getattr(chunk, "type", None) or type(chunk).__name__
                logger.debug(
                    "[JiuWenSwarmDeepAdapter][stream] consumer chunk type=%s request_id=%s",
                    _stream_ct, rid,
                )
                # First chunk handed back by the runner: records the time to
                # first token for this round.
                if not first_chunk_seen:
                    first_chunk_seen = True
                    server_logger.info(
                        "[AgentServer] runner streaming first chunk: session_id=%s request_id=%s"
                        " channel_id=%s mode=%s elapsed_ms=%.1f chunk_type=%s",
                        session_id,
                        rid,
                        cid,
                        mode,
                        (time.monotonic() - runner_stream_started_at) * 1000,
                        getattr(chunk, "type", None) or type(chunk).__name__,
                    )
                if _debug_logger is not None:
                    _debug_logger.feed(chunk)
                    # Surface run-level terminal failures (model/task_failed,
                    # error answer) on the /debug run-end status instead of a
                    # blanket "ok"; recoverable tool_result errors stay "ok".
                    if run_failure is None:
                        run_failure = self._run_failure(chunk)
                if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
                    parsed = self._parse_stream_chunk(chunk)
                    # Only stamp provenance / inject split on new visible deltas.
                    # A late user-round chat.final must keep the prior content kind.
                    if isinstance(parsed, dict) and parsed.get("event_type") == "chat.delta":
                        boundary = self._begin_visible_chat_content(stream_is_user_originated)
                        if boundary is not None:
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload(boundary),
                                is_complete=False,
                            )
                            # Boundary closes the user bubble only; goal segment follows.
                            emitted_terminal_chat_final = False
                    parsed = self._adapt_goal_intermediate_final(parsed)
                    if parsed is not None:
                        if should_skip_duplicate_ask_user(parsed):
                            continue
                        if self._is_ask_user_payload(parsed):
                            hitl_pending_stream = True
                        if accumulated_text:
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload({"event_type": "chat.delta", "content": accumulated_text}),
                                is_complete=False,
                            )
                            accumulated_text = ""
                        if accumulated_reasoning:
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload({
                                    "event_type": "chat.reasoning",
                                    "content": accumulated_reasoning,
                                }),
                                is_complete=False,
                            )
                            accumulated_reasoning = ""
                        if parsed.get("event_type") == "chat.final":
                            self._stream_content_run_kind = None
                        if parsed.get("event_type") == "chat.error":
                            emitted_chat_error = True
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(parsed),
                            is_complete=False,
                        )
                        if hitl_pending_stream:
                            suppress_stream_after_hitl = True
                            continue
                    continue

                chunk_type = chunk.type

                if chunk_type == "llm_usage":
                    logger.info(f"[JiuWenSwarmDeepAdapter] llm_usage chunk: {chunk}")
                    usage_meta = (
                        chunk.payload.get("usage_metadata", {})
                        if isinstance(chunk.payload, dict)
                        else {}
                    )
                    if isinstance(usage_meta, dict):
                        for token in (
                            "input_tokens",
                            "output_tokens",
                            "total_tokens",
                            "cache_tokens",
                        ):
                            usage_accumulator[token] += usage_meta.get(token, 0) or 0
                        for cost in ("input_cost", "output_cost", "total_cost"):
                            usage_accumulator[cost] += usage_meta.get(cost, 0.0) or 0.0
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload={
                            "event_type": "chat.usage_metadata",
                            "metadata": chunk.payload,
                            "session_id": session_id,
                        },
                        is_complete=False,
                    )
                    continue

                if chunk_type == "llm_reasoning":
                    content = (
                        (chunk.payload.get("content", "") or chunk.payload.get("output", ""))
                        if isinstance(chunk.payload, dict)
                        else str(chunk.payload)
                    )
                    reasoning_payload = self._stream_text_payload(
                        "chat.reasoning", content
                    )
                    if reasoning_payload is None:
                        continue
                    _propagate_stream_source_id(chunk.payload, reasoning_payload)
                    boundary = self._begin_visible_chat_content(stream_is_user_originated)
                    if boundary is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(boundary),
                            is_complete=False,
                        )
                        emitted_terminal_chat_final = False
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload(reasoning_payload),
                        is_complete=False,
                    )
                    continue

                if chunk_type == "llm_output":
                    content = (
                        chunk.payload.get("content", "")
                        if isinstance(chunk.payload, dict)
                        else str(chunk.payload)
                    )
                    delta_payload = self._stream_text_payload("chat.delta", content)
                    if delta_payload is None:
                        continue
                    _propagate_stream_source_id(chunk.payload, delta_payload)
                    has_streamed_content = True
                    if accumulated_reasoning:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload({
                                "event_type": "chat.reasoning",
                                "content": accumulated_reasoning,
                            }),
                            is_complete=False,
                        )
                        accumulated_reasoning = ""
                    boundary = self._begin_visible_chat_content(stream_is_user_originated)
                    if boundary is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(boundary),
                            is_complete=False,
                        )
                        emitted_terminal_chat_final = False
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload(delta_payload),
                        is_complete=False,
                    )
                    continue

                if chunk_type == "answer":
                    if accumulated_text:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload({"event_type": "chat.delta", "content": accumulated_text}),
                            is_complete=False,
                        )
                        accumulated_text = ""
                    if accumulated_reasoning:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload({
                                "event_type": "chat.reasoning",
                                "content": accumulated_reasoning,
                            }),
                            is_complete=False,
                        )
                        accumulated_reasoning = ""
                    if has_streamed_content:
                        parsed = self._parse_stream_chunk(
                            chunk,
                            _has_streamed_content=True,
                            _streamed_text=segment_streamed_text,
                        )
                        parsed = self._adapt_goal_intermediate_final(parsed)
                        if parsed is not None:
                            self._fill_reasoning_only_empty_final_if_needed(
                                parsed,
                                had_reasoning_output=had_reasoning_output,
                                visible_text_since_last_final=visible_text_since_last_final,
                            )
                            for _delta in self._drain_final_content_to_deltas(
                                parsed,
                                segment_streamed_text=segment_streamed_text,
                                chunk_payload=chunk.payload,
                            ):
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                    payload=note_chat_payload(_delta),
                                    is_complete=False,
                                )
                            if should_skip_duplicate_ask_user(parsed):
                                continue
                            if self._is_ask_user_payload(parsed):
                                hitl_pending_stream = True
                            if parsed.get("event_type") == "chat.final":
                                self._stream_content_run_kind = None
                            if parsed.get("event_type") == "chat.error":
                                emitted_chat_error = True
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload(parsed),
                                is_complete=False,
                            )
                            if hitl_pending_stream:
                                suppress_stream_after_hitl = True
                                continue
                        continue
                    parsed = self._parse_stream_chunk(
                        chunk,
                        _streamed_text=segment_streamed_text,
                    )
                    parsed = self._adapt_goal_intermediate_final(parsed)
                    if parsed is not None:
                        self._fill_reasoning_only_empty_final_if_needed(
                            parsed,
                            had_reasoning_output=had_reasoning_output,
                            visible_text_since_last_final=visible_text_since_last_final,
                        )
                        for _delta in self._drain_final_content_to_deltas(
                            parsed,
                            segment_streamed_text=segment_streamed_text,
                            chunk_payload=chunk.payload,
                        ):
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload(_delta),
                                is_complete=False,
                            )
                        if should_skip_duplicate_ask_user(parsed):
                            continue
                        if self._is_ask_user_payload(parsed):
                            hitl_pending_stream = True
                        if parsed.get("event_type") == "chat.final":
                            self._stream_content_run_kind = None
                        if parsed.get("event_type") == "chat.error":
                            emitted_chat_error = True
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(parsed),
                            is_complete=False,
                        )
                        if hitl_pending_stream:
                            suppress_stream_after_hitl = True
                            continue
                    continue

                # Outer ReAct tool_result starts a new LLM round. Inner SkillTurbo
                # tool_results must not clear the flag or nested answers leak as chat.final.
                if chunk_type == "tool_result" and _is_outer_react_tool_result(
                    chunk.payload
                ):
                    has_streamed_content = False

                if accumulated_text:
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload({"event_type": "chat.delta", "content": accumulated_text}),
                        is_complete=False,
                    )
                    accumulated_text = ""
                if accumulated_reasoning:
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload({"event_type": "chat.reasoning", "content": accumulated_reasoning}),
                        is_complete=False,
                    )
                    accumulated_reasoning = ""
                if chunk_type == "tool_result":
                    self._account_deepresearch_sdk_usage(
                        chunk.payload,
                        request_id=rid,
                        usage_accumulator=usage_accumulator,
                        accounted_usage_ids=accounted_deepresearch_usage_ids,
                    )
                parsed = self._parse_stream_chunk(
                    chunk,
                    _streamed_text=segment_streamed_text,
                )
                parsed = self._adapt_goal_intermediate_final(parsed)
                if parsed is not None:
                    self._fill_reasoning_only_empty_final_if_needed(
                        parsed,
                        had_reasoning_output=had_reasoning_output,
                        visible_text_since_last_final=visible_text_since_last_final,
                    )
                    for _delta in self._drain_final_content_to_deltas(
                        parsed,
                        segment_streamed_text=segment_streamed_text,
                        chunk_payload=getattr(chunk, "payload", None),
                    ):
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(_delta),
                            is_complete=False,
                        )
                    if should_skip_duplicate_ask_user(parsed):
                        continue
                    if self._is_ask_user_payload(parsed):
                        hitl_pending_stream = True
                    if parsed.get("event_type") == "chat.final":
                        self._stream_content_run_kind = None
                    if parsed.get("event_type") == "chat.error":
                        emitted_chat_error = True
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload(parsed),
                        is_complete=False,
                    )
                    if hitl_pending_stream:
                        suppress_stream_after_hitl = True
                        continue

            if run_failure is not None and not emitted_chat_error:
                error_type, error_message = run_failure
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload({
                        "event_type": "chat.error",
                        "error": error_message,
                        "error_type": error_type,
                    }),
                    is_complete=False,
                )
                emitted_chat_error = True

            if accumulated_text and not hitl_pending_stream:
                # Same rule as _adapt_goal_intermediate_final: demote host
                # flush only when the flushed text belonged to a goal round.
                if self._should_demote_goal_intermediate_final():
                    flush_payload: dict[str, Any] = {
                        "event_type": "chat.delta",
                        "content": accumulated_text,
                        "goal_intermediate": True,
                    }
                else:
                    flush_payload = {
                        "event_type": "chat.final",
                        "content": accumulated_text,
                    }
                    self._stream_content_run_kind = None
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload(flush_payload),
                    is_complete=False,
                )
            if accumulated_reasoning and not hitl_pending_stream:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload({"event_type": "chat.reasoning", "content": accumulated_reasoning}),
                    is_complete=False,
                )

            # pause→clear (and similar): round cancelled, iterator ends without
            # a model chat.final. Synthesize a real final so the frontend can
            # stopStreaming; do not demote.
            # HITL 暂停时跳过合成 final：由循环后的 chat.invocation_paused 终结帧
            # 收尾，避免 relayclaw sidecar 提前关闭 FrameQueue 导致 recoverable_pause 丢失。
            if not hitl_pending_stream and self._should_emit_stream_end_chat_final(
                had_assistant_output=had_assistant_output,
                emitted_terminal_chat_final=emitted_terminal_chat_final,
            ):
                self._stream_content_run_kind = None
                fallback_content = ""
                if saw_approved_plan_exit_result and not had_assistant_output:
                    fallback_content = self._plan_exit_fallback_content()
                # Reasoning-only models (e.g. glm-5.2) may emit chat.reasoning with
                # empty answer content. Non-team hosts skip empty chat.final for
                # bubble text — fill a short stable reply so the UI is not blank.
                # Use had_visible_text_ever (not visible_text_since_last_final): the
                # latter is cleared on tool_call, which would wrongly inject the
                # "no visible reply" text after pre-tool deltas + post-tool reasoning.
                fallback_content = self._apply_reasoning_only_empty_reply_fallback(
                    has_streamed_content=had_visible_text_ever,
                    had_reasoning_output=had_reasoning_output,
                    fallback_content=fallback_content,
                    reasoning_only_fallback=self._reasoning_only_empty_reply_fallback(),
                )
                if str(fallback_content or "").strip():
                    for _delta in self._iter_delta_for_unstreamed_final(
                        content=fallback_content,
                        chunk_payload=None,
                    ):
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(_delta),
                            is_complete=False,
                        )
                    # Body already on delta; keep final empty to avoid RelayClaw
                    # computeFinalTextDelta appending a duplicate copy.
                    fallback_content = ""
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload({
                        "event_type": "chat.final",
                        "content": fallback_content,
                    }),
                    is_complete=False,
                )

            if self._skill_evolution_rail is not None:
                task = asyncio.create_task(
                    self._watch_evolution_and_push(rid, cid, session_id)
                )
                task.add_done_callback(self._on_evolution_watcher_done)
                self._evolution_watcher_tasks.add(task)
            if _debug_logger is not None:
                if run_failure is not None:
                    _debug_logger.end_run(
                        status="error",
                        error_type=run_failure[0],
                        error_message=run_failure[1],
                    )
                else:
                    _debug_logger.end_run(status="ok")
            interaction_stream_abort = False
        except asyncio.CancelledError:
            perf_summary_status = "cancelled"
            stream_consumer_cancelled = initialization_complete
            logger.info(
                "[JiuWenSwarmDeepAdapter] 流式任务被取消: request_id=%s session_id=%s",
                rid,
                session_id,
            )
            # InteractionOutputStream.close() in ``finally`` owns the targeted
            # round abort. Do not issue a second adapter-level abort here:
            # it can race with a newly attached output consumer.
            # Do not yield chat.final here: CancelledError often means the
            # consumer is already tearing down, so the frame may never reach
            # the frontend; user Stop already closes UI via interrupt_result.
            if _debug_logger is not None:
                _debug_logger.end_run(status="cancelled")
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_cancelled",
            )
            raise
        except Exception as exc:
            perf_summary_status = "error"
            logger.exception("[JiuWenSwarmDeepAdapter] 流式任务异常: %s", exc)
            if _debug_logger is not None:
                _debug_logger.end_run(status="error", error=exc)
            self._revoke_main_skill_authorization(
                session_id,
                reason="task_error",
            )
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={
                    "event_type": "chat.error",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                is_complete=False,
            )
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            if perf_context_initialized:
                perf_usage_fallback = snapshot_perf_summary_usage(request.request_id)
            if interaction_stream is not None:
                try:
                    await interaction_stream.close(
                        abort_active_round=interaction_stream_abort,
                    )
                except BaseException as exc:
                    cleanup_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup failed: "
                        "step=interaction_stream error_type=%s",
                        type(exc).__name__,
                    )
            cleanup_steps: list[tuple[str, Callable[[], None]]] = []
            if _run_span is not None:
                cleanup_steps.append(
                    ("run_span", lambda: close_agent_run_span(_run_span, session_id=session_id))
                )
            if _debug_logger is not None:
                cleanup_steps.append(("debug_logger", _debug_logger.flush))
            if _debug_trace_token is not None:
                from jiuwenswarm.server.runtime.debug_trace.context import (
                    reset_debug_trace_logger,
                    unregister_debug_trace_logger,
                )
                cleanup_steps.extend(
                    (
                        (
                            "debug_trace",
                            lambda: reset_debug_trace_logger(_debug_trace_token),
                        ),
                        (
                            "debug_trace_registry",
                            lambda: unregister_debug_trace_logger(session_id),
                        ),
                    )
                )
            if image_files_token is not None:
                from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                    reset_current_multimodal_image_files,
                )
                cleanup_steps.append(
                    (
                        "multimodal_images",
                        lambda: reset_current_multimodal_image_files(image_files_token),
                    )
                )
            if perf_context_initialized:
                cleanup_steps.extend(
                    (
                        (
                            "perf_finalize",
                            lambda: finalize_perf_summary_request(
                                request.request_id,
                                status=perf_summary_status,
                            ),
                        ),
                        (
                            "perf_context",
                            lambda: clear_perf_summary_context(
                                getattr(self, "_request_summary_rail", None),
                                session_id=session_id,
                                request_id=request.request_id,
                            ),
                        ),
                    )
                )
            if session_task_registered:
                cleanup_steps.append(
                    ("session_task", lambda: self._unregister_session_agent_task(session_id))
                )
            # Always clean up rail state — process_interrupt's
            # _stop_session_interrupt_work sets abort flags but does NOT
            # call cleanup_session(), so skipping cleanup here would leak
            # _abort_requested / _pause_events entries on long-lived adapters.
            if session_active:
                cleanup_steps.append(
                    (
                        "session_active",
                        lambda: self._unmark_session_active(
                            session_id,
                            cleanup_rail=True,
                        ),
                    )
                )
            if request_env_tokens is not None:
                cleanup_steps.append(
                    (
                        "request_env",
                        lambda: self._reset_request_env_bindings(*request_env_tokens),
                    )
                )
            if token_perm_sid is not None:
                cleanup_steps.append(
                    (
                        "permission_session",
                        lambda: reset_permissions_session_scope(token_perm_sid),
                    )
                )
            if token_perm_agent is not None:
                cleanup_steps.append(
                    (
                        "permission_agent",
                        lambda: reset_permissions_agent_base(token_perm_agent),
                    )
                )
            if token_perm is not None:
                cleanup_steps.append(
                    ("permission", lambda: cleanup_permission_context(token_perm))
                )
            if token_cid is not None:
                cleanup_steps.append(
                    ("permission_channel", lambda: TOOL_PERMISSION_CHANNEL_ID.reset(token_cid))
                )
            cleanup_steps.extend(
                (
                    (
                        "runtime_context",
                        lambda: self._reset_stream_runtime_context(
                            cron_context_tokens,
                            stream_consumer_cancelled=stream_consumer_cancelled,
                        ),
                    ),
                    ("trace_model", lambda: _LLM_TRACE_MODEL_NAME.reset(token_trace_model)),
                    ("trace_iteration", lambda: _LLM_TRACE_ITERATION.reset(token_trace_iter)),
                    ("trace_request", lambda: _LLM_TRACE_REQUEST_ID.reset(token_trace_rid)),
                    ("trace_session", lambda: _LLM_TRACE_SESSION_ID.reset(token_trace_sid)),
                )
            )
            step_error = self._run_cleanup_steps(cleanup_steps)
            if cleanup_error is None:
                cleanup_error = step_error
            if initialization_complete:
                try:
                    await self._maybe_apply_pending_reload()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup failed: "
                        "step=pending_reload error_type=%s",
                        type(exc).__name__,
                    )
            # 流式 chat 收尾补 commit：把本轮 context_engine 内存 context 落盘到 checkpointer。
            # 根因：流式路径 session.commit() 挂在 need_cleanup 下，而 adapter 预绑定 session 致
            # need_cleanup=False，commit 被跳过 → 跨重启/跨 channel 下一轮 restore 出来历史为空，
            # agent 从头重新调研。对齐 _persist_cron_checkpoint 显式补一次落盘，覆盖 officeclaw
            # 等非 cron 普通会话。失败不阻断清理（仅记 cleanup_error），对齐周围姿势。
            if initialization_complete:
                try:
                    await self._persist_session_checkpoint(session_id, rid)
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] cleanup failed: "
                        "step=session_checkpoint error_type=%s",
                        type(exc).__name__,
                    )
            if active_error is None and cleanup_error is not None:
                raise cleanup_error

        summary_chunk = self._log_and_make_usage_summary_chunk(
            request_id=rid,
            channel_id=cid,
            session_id=session_id,
            usage_accumulator=usage_accumulator,
            perf_usage_fallback=perf_usage_fallback,
        )
        if summary_chunk is not None:
            yield summary_chunk

        if hitl_pending_stream:
            # HITL 暂停：以 awaiting_user_input 终结帧收尾，网关出站会把该帧转成
            # is_final=False 的 in_progress 帧，前端流保持开启等待用户作答。
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={
                    "event_type": "chat.invocation_paused",
                    "awaiting_user_input": True,
                },
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
    def _stream_text_payload(
        event_type: str,
        content: Any,
    ) -> dict[str, Any] | None:
        """Build a text event without discarding formatting-only chunks."""
        if content is None or content == "":
            return None
        return {"event_type": event_type, "content": content}

    @staticmethod
    def _is_ask_user_payload(payload: Any) -> bool:
        """HITL 暂停判定：payload 是否为 ask_user 卡片事件。"""
        return isinstance(payload, dict) and payload.get("event_type") == "chat.ask_user_question"

    @staticmethod
    def _run_failure(chunk) -> tuple[str, str] | None:
        """Return ``(error_type, message)`` if *chunk* is a run-level terminal failure.

        These are failures that end the round and are surfaced to the user as a
        ``chat.error`` by :meth:`_parse_stream_chunk` — a
        ``controller_output``/``task_failed`` (e.g. model call failed) or an
        ``answer`` whose ``result_type`` is ``"error"``. Recoverable
        ``tool_result`` errors are intentionally excluded: the agent loop may
        still retry them, so they must not flip the run status.

        Used only to give the /debug ``run end`` ``status`` triage value; the
        chunk still flows through ``_parse_stream_chunk`` unchanged.
        """
        ctype = getattr(chunk, "type", None)
        payload = getattr(chunk, "payload", None)

        def _get(key, default=None):
            if isinstance(payload, dict):
                return payload.get(key, default)
            return getattr(payload, key, default)

        if ctype == "controller_output" and payload is not None:
            inner_t = getattr(payload, "type", None)
            inner_val = getattr(inner_t, "value", inner_t) if inner_t is not None else None
            if inner_val == "task_failed":
                data = getattr(payload, "data", None) or []
                msg = next(
                    (
                        getattr(item, "text", None)
                        for item in data
                        if hasattr(item, "text")
                    ),
                    None,
                )
                return "task_failed", (msg or "task failed")
            return None

        if ctype == "answer" and _get("result_type") == "error":
            out = _get("output")
            if isinstance(out, dict):
                out = out.get("output")
            return "answer_error", (str(out) if out else "task failed")

        if isinstance(chunk, dict) and chunk.get("result_type") == "error":
            return "answer_error", str(chunk.get("output") or "task failed")

        return None

    @staticmethod
    def _parse_stream_chunk(
        chunk,
        *,
        _has_streamed_content: bool = False,
        _stage: str = "",
        _streamed_text: str = "",
        _protocol_buffer: Any = None,
    ) -> dict | None:
        """将 SDK OutputSchema 转为前端可消费的 payload dict.

        Args:
            chunk: OutputSchema 或 dict
            _has_streamed_content: 是否已通过 llm_output 流式发送过内容
            _stage: 当前阶段名称，用于 auto_harness harness.message 事件
            _streamed_text: 本段已通过 chat.delta 发出的可见正文（用于空 final / drain）
            _protocol_buffer: 调用方持有的 StreamProtocolBuffer；跨 chunk 缓冲在 call site
                feed/flush，此处仅吸收关键字以免 TypeError

        Returns:
            dict  – 含 event_type 的 payload，或 None（需跳过的帧）。
        """
        _ = _protocol_buffer
        try:
            if hasattr(chunk, "type") and hasattr(chunk, "payload"):
                chunk_type = chunk.type
                payload = chunk.payload

                if chunk_type == GOAL_UPDATED_EVENT_TYPE:
                    return JiuWenSwarmDeepAdapter._interaction_goal_updated_payload(payload)

                if chunk_type == ERROR_EVENT_TYPE:
                    err_payload = payload if isinstance(payload, dict) else {}
                    return {
                        "event_type": ERROR_EVENT_TYPE,
                        "code": err_payload.get("code", "execution_error"),
                        "message": err_payload.get("message", "execution error"),
                        "goal": err_payload.get("goal"),
                    }

                if chunk_type == "controller_output" and payload is not None:
                    inner_t = getattr(payload, "type", None)
                    inner_val = getattr(inner_t, "value", inner_t) if inner_t is not None else None
                    if inner_val == "task_completion":
                        return None
                    if inner_val == "task_failed":
                        error = next(
                            (item.text for item in payload.data if hasattr(item, "text")),
                            "任务执行失败",
                        )
                        return {"event_type": "chat.error", "error": error}

                if chunk_type == "llm_output":
                    content = (
                        payload.get("content", "") if isinstance(payload, dict) else str(payload)
                    )
                    delta_payload = JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.delta", content
                    )
                    return _propagate_stream_source_id(payload, delta_payload)

                if chunk_type == "llm_reasoning":
                    content = (
                        (payload.get("content", "") or payload.get("output", ""))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    reasoning_payload = JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.reasoning", content
                    )
                    return _propagate_stream_source_id(payload, reasoning_payload)

                if chunk_type == "content_chunk":
                    content = (
                        payload.get("content", "") if isinstance(payload, dict) else str(payload)
                    )
                    delta_payload = JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.delta", content
                    )
                    src = payload if isinstance(payload, dict) else {}
                    return _propagate_stream_source_id(src, delta_payload)

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
                            output.get("output", "") if isinstance(output, dict) else str(output)
                        )
                        is_chunked = (
                            output.get("chunked", False) if isinstance(output, dict) else False
                        )
                    else:
                        content = str(payload)
                        is_chunked = False

                    if not content or not content.strip():
                        return None

                    if _has_streamed_content and not is_chunked:
                        # 已通过 chat.delta 流完 → 仅收尾标记，避免下游重复。
                        # has_streamed_content 可能被空白/极小 delta 误置，真实答案
                        # 却只在 answer chunk 里。此时保留正文（answer 处理器会以
                        # chat.delta 补流），保证非 team 前端（只渲染 delta）能看到回复。
                        if _streamed_text and content.strip() in _streamed_text:
                            return {"event_type": "chat.final", "content": ""}
                        streamed_visible = str(_streamed_text or "").strip()
                        # Substantial stream already on the wire this segment —
                        # do not keep final body for drain (would duplicate).
                        if streamed_visible and len(streamed_visible) >= max(
                            8, len(content.strip()) // 4
                        ):
                            return {"event_type": "chat.final", "content": ""}
                        return {"event_type": "chat.final", "content": content}
                    if is_chunked:
                        return {"event_type": "chat.delta", "content": content}
                    return {"event_type": "chat.final", "content": content}

                if chunk_type == "tool_call":
                    tool_info = (
                        payload.get("tool_call", payload) if isinstance(payload, dict) else payload
                    )
                    return {"event_type": "chat.tool_call", "tool_call": tool_info}

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
                    return {
                        "event_type": "chat.tool_update",
                        **update_payload,
                    }

                if chunk_type == "tool_result":
                    if isinstance(payload, dict):
                        result_info = payload.get("tool_result", payload)
                        result_payload = {
                            "result": (
                                result_info.get("result", str(result_info))
                                if isinstance(result_info, dict)
                                else str(result_info)
                            ),
                        }
                        if isinstance(result_info, dict):
                            result_payload["tool_name"] = result_info.get(
                                "tool_name"
                            ) or result_info.get("name")
                            result_payload["tool_call_id"] = result_info.get(
                                "tool_call_id"
                            ) or result_info.get("toolCallId")
                            raw_output = result_info.get("raw_output")
                            if raw_output is None:
                                raw_output = result_info.get("rawOutput")
                            if raw_output is not None:
                                result_payload["raw_output"] = raw_output
                            for key in (
                                "status",
                                "success",
                                "is_error",
                                "error",
                                "summary",
                                "score_status",
                                "score_build",
                                "direct_display",
                                "display_format",
                                "mermaid",
                            ):
                                if key in result_info:
                                    result_payload[key] = result_info[key]
                    else:
                        result_payload = {"result": str(payload)}
                    return {
                        "event_type": "chat.tool_result",
                        **result_payload,
                    }

                if chunk_type == "error":
                    error_msg = (
                        payload.get("error", str(payload))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    return {"event_type": "chat.error", "error": error_msg}

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

                if chunk_type == "security.alert":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "security.alert",
                            **payload,
                        }
                    return None

                if chunk_type == "chat.retract":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "chat.retract",
                            **payload,
                        }
                    return None

                # send_file OBS path: OutputSchema(type="chat.file", payload={files:[url...]})
                # must not fall through to _stream_text_payload — files-only payload has
                # no content/output, so the frame would be dropped and Gateway never
                # materializes a download token.
                if chunk_type == "chat.file":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "chat.file",
                            **payload,
                        }
                    return None

                if chunk_type == "thinking":
                    return {
                        "event_type": "chat.processing_status",
                        "is_processing": True,
                        "current_task": "thinking",
                    }

                if chunk_type == "todo.updated":
                    todos = payload.get("todos", []) if isinstance(payload, dict) else []
                    return {"event_type": "todo.updated", "todos": todos}

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

                if chunk_type == "task.update":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "task.update",
                            "tasks": payload.get("tasks", []),
                            "total_tasks": payload.get("total_tasks", 0),
                            "completed_tasks": payload.get("completed_tasks", 0),
                            "in_progress_tasks": payload.get("in_progress_tasks", 0),
                            "pending_tasks": payload.get("pending_tasks", 0),
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

                if chunk_type == "context.usage":
                    if isinstance(payload, dict):
                        usage_payload = {
                            "event_type": "context.usage",
                            "rate": payload.get("rate", 0),
                            "context_max": payload.get("context_max") or 0,
                            "tokens_used": payload.get("tokens_used") or 0,
                        }
                        for key in ("role", "member_name"):
                            value = payload.get(key)
                            if value is not None:
                                usage_payload[key] = value
                        return usage_payload
                    return {"event_type": "context.usage", "rate": 0}

                if chunk_type == "context.compression_state":
                    if hasattr(payload, "model_dump"):
                        state_payload = payload.model_dump(mode="json")
                    elif isinstance(payload, dict):
                        state_payload = payload
                    else:
                        state_payload = {"summary": str(payload)}
                    return {
                        "event_type": "context.compression_state",
                        **state_payload,
                    }

                if chunk_type == "chat.ask_user_question":
                    return parse_ask_user_question_payload(payload)

                if chunk_type == "chat.symphony_status":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "chat.symphony_status",
                            **payload,
                        }
                    return None

                if chunk_type == "__interaction__":
                    if isinstance(payload, dict) and payload.get("interaction_type") == "activate_confirm":
                        return {
                            "event_type": "harness.activate_interaction",
                            "interaction_type": "activate_confirm",
                            "interaction_id": payload.get("interaction_id", ""),
                            "extension_name": payload.get("extension_name", ""),
                            "runtime_path": payload.get("runtime_path", ""),
                            "session_runtime_path": payload.get("session_runtime_path", ""),
                            "extension_runtime_path": payload.get(
                                "extension_runtime_path", payload.get("runtime_path", "")
                            ),
                            "options": payload.get("options", ["accept", "reject"]),
                        }
                    return convert_interactions_to_ask_user_question([payload])

                # Auto-harness specific: harness.message event
                if chunk_type == "message":
                    content = (
                        payload.get("content", "")
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    # Extract stage from payload if available, fallback to _stage parameter
                    stage_from_payload = (
                        payload.get("stage", "")
                        if isinstance(payload, dict)
                        else ""
                    )
                    result: dict[str, Any] = {
                        "event_type": "harness.message",
                        "content": content,
                        "stage": stage_from_payload or _stage,
                    }
                    # Pass through stages array for dynamic stage definition
                    if isinstance(payload, dict):
                        if "stages" in payload:
                            result["stages"] = payload["stages"]
                        if "pipeline" in payload:
                            result["pipeline"] = payload["pipeline"]
                        # Pass through metadata for security alerts and other custom data
                        if "metadata" in payload:
                            result["metadata"] = payload["metadata"]
                    return result

                # Auto-harness specific: harness.stage_result event
                if chunk_type == "stage_result":
                    if isinstance(payload, dict):
                        stage = payload.get("stage", _stage)
                        return {
                            "event_type": "harness.stage_result",
                            "stage": stage,
                            "status": payload.get("status", "success"),
                            "error": payload.get("error", ""),
                            "messages": payload.get("messages", []),
                            "metrics": payload.get("metrics", {}),
                            "scope": payload.get("scope", ""),
                            "parent_stage": payload.get("parent_stage", ""),
                            "extension_stage": payload.get("extension_stage", ""),
                            "extension_name": payload.get("extension_name", ""),
                            "task_id": payload.get("task_id", ""),
                        }
                    return None

                # Auto-harness specific: harness.extension_ready event
                if chunk_type == "extension_ready":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "harness.extension_ready",
                            "extension_name": payload.get("extension_name", ""),
                            "runtime_path": payload.get("runtime_path", ""),
                            "session_runtime_path": payload.get("session_runtime_path", ""),
                            "extension_runtime_path": payload.get("extension_runtime_path", ""),
                            "config_path": payload.get("config_path", ""),
                            "runtime_extensions": payload.get("runtime_extensions", []),
                            "verify_report": payload.get("verify_report", {}),
                            "components_summary": payload.get("components_summary", {}),
                        }
                    return None

                if chunk_type == "harness_session_finished":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "harness.session_finished",
                            "pipeline": payload.get("pipeline", ""),
                            "status": payload.get("status", "success"),
                            "results_count": payload.get("results_count", 0),
                            "is_terminal": bool(payload.get("is_terminal", True)),
                        }
                    return {
                        "event_type": "harness.session_finished",
                        "status": "success",
                        "is_terminal": True,
                    }

                # Auto-harness specific: activate_testing_guide summary
                if chunk_type == "activate_testing_guide":
                    if isinstance(payload, dict):
                        text = payload.get("text", "")
                        if text:
                            return {"event_type": "chat.delta", "content": text}
                    return None

                if isinstance(payload, dict):
                    if "traceId" in payload or "invokeId" in payload:
                        return None
                    content = payload.get("content") or payload.get("output")
                else:
                    content = str(payload)
                delta_payload = JiuWenSwarmDeepAdapter._stream_text_payload(
                    "chat.delta", content
                )
                src = payload if isinstance(payload, dict) else {}
                return _propagate_stream_source_id(src, delta_payload)

            if isinstance(chunk, dict):
                if "traceId" in chunk or "invokeId" in chunk:
                    return None
                # Interaction loop lifecycle events emitted by DeepAgent
                # (goal.updated snapshot etc.) forwarded to the TUI goal status bar.
                interaction_evt = chunk.get("type")
                if interaction_evt == GOAL_UPDATED_EVENT_TYPE:
                    return JiuWenSwarmDeepAdapter._interaction_goal_updated_payload(
                        chunk.get("payload")
                    )
                if interaction_evt == ERROR_EVENT_TYPE:
                    err_payload = chunk.get("payload")
                    err_payload = err_payload if isinstance(err_payload, dict) else {}
                    return {
                        "event_type": ERROR_EVENT_TYPE,
                        "code": err_payload.get("code", "execution_error"),
                        "message": err_payload.get("message", "execution error"),
                        "goal": err_payload.get("goal"),
                    }
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
                    return {"event_type": "chat.delta", "content": str(output)}
                return None

        except Exception:
            logger.debug("[_parse_stream_chunk] 解析异常", exc_info=True)

        return None

    async def _handle_memory_rail_by_config(self, mode: str):
        config = (
            self._startup_config_base
            if isinstance(self._startup_config_base, dict)
            else get_config()
        )
        if get_memory_mode(config) == "local":
            # 引擎门禁：memory.engine 未放行内置时，等同于禁用
            builtin_on = is_builtin_memory_allowed(config) and is_memory_enabled(mode, config)
            if builtin_on:
                # 开启记忆
                new_embed_fp = self._embedding_config_fingerprint(config)
                previous_embed_fp = self._memory_embedding_fingerprint
                if self._memory_rail is not None:
                    cur_memory_type = is_proactive_memory(mode, config)
                    if self._is_proactive_memory != cur_memory_type:
                        # 当前记忆类型（主动/被动）和之前注册的不一致，重新注册
                        await self._instance.unregister_rail(self._memory_rail)
                        self._memory_rail = None
                    elif self._memory_embedding_fingerprint != new_embed_fp:
                        # 记忆类型没变，但 embedding 配置（base_url/api_key/model）变了：
                        # 必须重建 rail 才能刷新 MemoryRail._embedding_config，否则换 endpoint
                        # 时 rail 仍持旧配置，新 manager 仍用旧 provider。
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] embedding config changed, rebuilding MemoryRail"
                        )
                        await self._instance.unregister_rail(self._memory_rail)
                        self._memory_rail = None
                    else:
                        # 已经注册，且记忆类型与 embedding 配置均未变，无需其他操作
                        return
                if self._memory_rail is None:
                    self._memory_rail = self._build_memory_rail(mode)
                if self._memory_rail is not None:
                    # 重建（或首次注册）后记录新指纹，作为下次比对的基线
                    self._memory_embedding_fingerprint = new_embed_fp
                    try:
                        await self._instance.register_rail(self._memory_rail)
                    except Exception as e:
                        # register_rail 失败：回滚指纹与 rail，避免留下"指纹已是新值、
                        # 但 rail 未成功注册"的不一致状态——否则下次比对会认为"没变"而
                        # 走 return，让这个孤儿 rail 既不注册也不重建，记忆静默失效。
                        self._memory_embedding_fingerprint = ""
                        self._memory_rail = None
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] register MemoryRail failed: %s", e
                        )
                    else:
                        logger.info(f"[JiuWenSwarmDeepAdapter] MemoryRail registered for {mode} mode")
                        # 重建后触发延时重索引（debounce），使新 embedding 配置对历史记忆文件生效
                        if previous_embed_fp and previous_embed_fp != new_embed_fp:
                            self._schedule_memory_reindex()
            elif not builtin_on and self._memory_rail is not None:
                await self._instance.unregister_rail(self._memory_rail)
                self._memory_rail = None
                logger.info(f"[JiuWenSwarmDeepAdapter] MemoryRail unregistered for {mode} mode")

    def _build_external_memory_rail(self):
        from jiuwenswarm.agents.harness.common.memory.external_memory_builder import (
            build_external_memory_rail,
        )

        return build_external_memory_rail(
            config=get_config(),
            workspace_dir=self._workspace_dir,
        )

    async def _handle_external_memory_rail_by_config(self):
        """Register / unregister ExternalMemoryRail based on config.

        External memory is mode-independent — configured once and active in
        the merged agent mode. `_external_memory_rail_registered` dedups
        repeated calls from `_update_agent_rails()`.
        Not part of `_get_current_agent_rails()`, so it is not torn down on
        config hot-reload (preserves prefetch cache + circuit breaker state).
        """
        from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
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
                logger.info("[JiuWenSwarmDeepAdapter] ExternalMemoryRail registered")
            except Exception as exc:
                logger.error("[JiuWenSwarmDeepAdapter] ExternalMemoryRail register failed: %s", exc)
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
                    logger.debug("[JiuWenSwarmDeepAdapter] on_session_end failed: %s", exc)
            try:
                await self._instance.unregister_rail(self._external_memory_rail)
                logger.info("[JiuWenSwarmDeepAdapter] ExternalMemoryRail unregistered")
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] ExternalMemoryRail unregister failed: %s", exc
                )
            self._external_memory_rail = None
            self._external_memory_rail_registered = False

    async def compress_context(
            self,
            session_id: str,
            session: Any = None,
            *,
            return_state: bool = False,
    ) -> dict[str, Any]:
        """主动触发上下文压缩。

        Args:
            session_id: 会话ID
            session: Session 对象（可选）

        Returns:
            包含压缩结果的字典:
            - result: "busy" | "compressed" | "noop"
            - stats: 压缩统计信息（仅当 result == "compressed" 时）
        """
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(session_id)
            try:
                return await session_adapter.compress_context(
                    session_id=session_id,
                    session=session,
                    return_state=return_state,
                )
            finally:
                await self._evict_idle_session_adapters()

        if self._instance is None or self._instance.react_agent is None:
            raise ValueError("Agent instance not available")

        context_engine = self._instance.react_agent.context_engine
        react_agent = self._instance.react_agent

        context = context_engine.get_context(session_id=session_id)
        if context is None:
            return {"result": "noop", "stats": None}

        raw_total_tokens = await self._count_full_context_tokens(
            context, react_agent, session_id
        )

        compact_result = await context_engine.compress_context(
            session=session,
            session_id=session_id,
            return_state=True,
        )
        summary: str | None = None
        state: dict[str, Any] | None = None
        if isinstance(compact_result, dict):
            result = compact_result.get("result") or compact_result.get("status")
            raw_state = compact_result.get("state")
            if isinstance(raw_state, dict):
                state = raw_state
            raw_summary = compact_result.get("compact_summary")
            if raw_summary is None:
                if isinstance(state, dict):
                    raw_summary = state.get("compact_summary")
            if isinstance(raw_summary, str) and raw_summary.strip():
                summary = raw_summary.strip()
        else:
            result = compact_result

        response: dict[str, Any] = {"result": result}
        if return_state and state:
            response["state"] = state
            compact_summary = state.get("compact_summary")
            if isinstance(compact_summary, str) and compact_summary.strip():
                response["compact_summary"] = compact_summary.strip()

        if result == "compressed":
            context = context_engine.get_context(session_id=session_id)
            if context:
                total_tokens = await self._count_full_context_tokens(
                    context, react_agent, session_id
                )

                stats = context.statistic()
                response["stats"] = {
                    "total_messages": stats.total_messages,
                    "total_tokens": total_tokens,
                    "raw_total_tokens": raw_total_tokens,
                }
                if summary:
                    response["summary"] = summary
                    response.setdefault("compact_summary", summary)

        return response

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        """获取当前上下文窗口占用统计。

        Args:
            session_id: 会话ID

        Returns:
            包含上下文使用情况统计的字典:
            - context_window_limit: 模型上下文窗口总 token 数
            - total_tokens: 当前上下文已用 token 数
            - system_prompt_tokens: 系统提示词 token 数
            - messages_tokens: 对话消息 token 数
            - tools_tokens: 工具定义 token 数
            - occupancy_rate: 占用率 (0-100)
            - message_count: 对话消息数量
            - context_occupancy: 上下文占用详情（来自 deepagent）
        """
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(session_id)
            try:
                return await session_adapter.get_context_usage(session_id=session_id)
            finally:
                await self._evict_idle_session_adapters()

        if self._instance is None:
            raise ValueError("Agent instance not available")

        context_engine = self._instance.react_agent.context_engine
        react_agent = self._instance.react_agent
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            return {
                "context_window_limit": 0, "total_tokens": 0,
                "system_prompt_tokens": 0, "messages_tokens": 0,
                "tools_tokens": 0, "occupancy_rate": 0,
                "message_count": 0, "context_occupancy": None,
            }

        # 分项估算：直接用 context engine 的 token counter
        token_counter = context.token_counter()
        from openjiuwen.core.foundation.tool import ToolInfo

        # 系统提示词
        system_prompt = self._get_agent_system_prompt()
        if system_prompt and token_counter:
            system_prompt_tokens = token_counter.count(system_prompt) or 0
        elif system_prompt:
            system_prompt_tokens = len(system_prompt) // 4
        else:
            system_prompt_tokens = 0

        # 对话消息
        context_messages = context.get_messages() or []
        if context_messages and token_counter:
            messages_tokens = token_counter.count_messages(context_messages) or 0
        elif context_messages:
            messages_tokens = sum(len(str(msg.content)) // 4 for msg in context_messages)
        else:
            messages_tokens = 0

        # 工具定义
        tools: list[ToolInfo] = []
        if hasattr(react_agent, "ability_manager") and react_agent.ability_manager is not None:
            for card in react_agent.ability_manager.list() or []:
                if hasattr(card, "to_tool_info"):
                    tools.append(card.to_tool_info())
                elif hasattr(card, "name") and hasattr(card, "description"):
                    tools.append(ToolInfo(
                        name=card.name,
                        description=card.description or "",
                        parameters=getattr(card, "input_params", {}),
                    ))
        if tools and token_counter:
            tools_tokens = token_counter.count_tools(tools) or 0
        else:
            tools_tokens = 0

        # 总量 & 窗口限制：优先用 DeepAgent 的准确值，回退到估算
        total_tokens = system_prompt_tokens + messages_tokens + tools_tokens
        context_window_limit = 0
        occupancy_rate = 0.0
        context_occupancy = None

        try:
            usage = self._instance.get_context_usage(session_id=session_id)
            context_occupancy = usage
            # DeepAgent 的 total_tokens 来自 usage_metadata，比估算更准确
            da_total = usage.get("total_tokens", 0)
            if da_total > 0:
                total_tokens = da_total
            context_window_limit = usage.get("context_window_tokens", 0)
            occupancy_rate = usage.get("usage_percent", 0)
        except Exception as exc:
            logger.debug("[JiuWenSwarmDeepAdapter] DeepAgent.get_context_usage failed: %s", exc)
            from openjiuwen.core.context_engine.context.context_utils import ContextUtils
            model_name = (
                getattr(self._model_request_config, "model_name", "") or ""
                if self._model_request_config else ""
            )
            context_window_limit = ContextUtils.resolve_context_max(model_name=model_name)
            if context_window_limit > 0:
                occupancy_rate = round(total_tokens / context_window_limit * 100, 1)

        message_count = len(context_messages)

        return {
            "context_window_limit": context_window_limit,
            "total_tokens": total_tokens,
            "system_prompt_tokens": system_prompt_tokens,
            "messages_tokens": messages_tokens,
            "tools_tokens": tools_tokens,
            "occupancy_rate": occupancy_rate,
            "message_count": message_count,
            "context_occupancy": context_occupancy,
        }

    async def generate_recap(self, session_id: str) -> dict[str, Any]:
        """生成会话快速回顾（read-only，不修改对话历史）。

        取最近30条消息 → fast model → 1-3句摘要。
        """
        if not self._is_session_scoped_adapter:
            session_adapter = self._get_cached_session_adapter(session_id)
            if session_adapter is not None:
                try:
                    return await session_adapter.generate_recap(session_id=session_id)
                finally:
                    await self._evict_idle_session_adapters()

        from jiuwenswarm.server.runtime.agent_adapter.recap_prompts import (
            RECENT_MESSAGE_WINDOW,
            build_recap_prompt,
        )

        messages = self._get_recent_messages(session_id, window=RECENT_MESSAGE_WINDOW)
        if not messages:
            return {"status": "no_turn"}

        # 透传主 agent tools schema 保 cache key（工具执行由单轮 + tool_use 丢弃禁止）
        tools = await self._get_agent_tools(session_id)

        prompt = build_recap_prompt(memory=None, language=self._resolve_prompt_language())
        summary_text = await self._call_model_for_recap(messages, prompt, tools=tools or None)
        if not summary_text:
            return {"status": "failed", "error": "Model returned empty response"}

        return {"status": "ok", "summary": summary_text.strip()}

    def _get_recent_messages(self, session_id: str, window: int = 30) -> list[Any]:
        """从当前 agent 对话上下文中提取最近N条消息。

        查找顺序：
        1. 当前 adapter 的 context_engine（session-scoped adapter 自身或已加载的 parent）
        2. 父 adapter 查找 session-scoped child adapter 的 context_engine
           （解决 /btw 等侧查询在 parent adapter 上执行时，会话上下文在 child adapter
           中而 parent 的 context_engine 未加载该 session 的问题）
        3. 回退到从磁盘读取兼容格式的 history 文件
        """
        # --- 快速路径：当前 adapter 的 context_engine 已加载 ---
        if self._instance is not None and self._instance.react_agent is not None:
            context_engine = self._instance.react_agent.context_engine
            context = context_engine.get_context(session_id=session_id)
            if context is not None:
                try:
                    all_messages = list(context.get_messages() or [])
                    if all_messages:
                        return all_messages[-window:]
                except Exception as exc:
                    logger.debug("[JiuWenSwarmDeepAdapter] _get_recent_messages from context_engine failed: %s", exc)

        # --- 中间路径：从 session-scoped child adapter 的 context_engine 查找 ---
        # 当 /btw 等侧查询在 parent adapter 上执行时，会话上下文实际在
        # session-scoped child adapter 的内存中（context_engine），而非磁盘。
        # 直接从内存读取可避免与异步写队列的"写后读"竞态。
        if not getattr(self, "_is_session_scoped_adapter", False):
            session_adapter = self._get_cached_session_adapter(session_id)
            if session_adapter is not None:
                inst = getattr(session_adapter, "_instance", None)
                if inst is not None and getattr(inst, "react_agent", None) is not None:
                    ctx_eng = inst.react_agent.context_engine
                    ctx = ctx_eng.get_context(session_id=session_id)
                    if ctx is not None:
                        try:
                            all_msgs = list(ctx.get_messages() or [])
                            if all_msgs:
                                logger.debug(
                                    "[JiuWenSwarmDeepAdapter] _get_recent_messages: "
                                    "read %d messages from session-scoped adapter context_engine "
                                    "for session %s",
                                    len(all_msgs),
                                    session_id,
                                )
                                return all_msgs[-window:]
                        except Exception as exc:
                            logger.debug(
                                "[JiuWenSwarmDeepAdapter] _get_recent_messages "
                                "from session-scoped adapter context_engine failed: %s",
                                exc,
                            )

        # --- 回退路径：从磁盘读取兼容格式 history ---
        # 典型场景：/resume 之后，context_engine 还未加载新 session 的上下文，
        # 但磁盘上已有该 session 的历史消息。
        try:
            from types import SimpleNamespace

            records = load_history_records(session_id)
            if not records:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] _get_recent_messages: no history records on disk for session %s",
                    session_id,
                )
                return []

            # 过滤出适合 recap 的消息记录
            # 只保留 user 消息和 assistant 的最终回复 / compact summary
            recapworthy = []
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                role = rec.get("role")
                content = rec.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue

                if role == "user":
                    recapworthy.append(SimpleNamespace(role="user", content=content))
                elif role == "assistant":
                    # Goal-completed cards are UI-only history facts; never feed them
                    # back into model recap context.
                    if rec.get("is_goal_completed_message"):
                        continue
                    event_type = rec.get("event_type")
                    # 只包含 assistant 的最终回复和 compact summary
                    if event_type in ("chat.final", "context.compact_summary") or not event_type:
                        recapworthy.append(SimpleNamespace(role="assistant", content=content))

            if not recapworthy:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] _get_recent_messages: no recap-worthy records on disk for session %s",
                    session_id,
                )
                return []

            logger.info(
                "[JiuWenSwarmDeepAdapter] _get_recent_messages: loaded %d records from disk for session %s "
                "(context_engine fallback)",
                len(recapworthy),
                session_id,
            )
            return recapworthy[-window:]
        except Exception as exc:
            logger.debug("[JiuWenSwarmDeepAdapter] _get_recent_messages disk fallback failed: %s", exc)
            return []

    async def _get_agent_tools(self, session_id: str) -> list[Any]:
        """取主 agent 当前 tools 列表（List[ToolInfo]），用于 btw/recap 透传给模型。

        透传 tools schema 是为了与主 agent 保持 cache key 一致（openjiuwen 的
        prompt cache 布局为 tools → system → messages，tools 段缺失会破坏前缀
        匹配）。工具执行仍被禁用：btw/recap 单轮 + tool_use 检测丢弃。

        查找顺序与 _get_recent_messages 一致：先当前 adapter 的 react_agent，
        再 session-scoped child adapter（btw 在 parent 执行时 tools 在 child）。
        返回空列表表示无工具可用，调用方应按不传 tools 处理（tools or None）。
        """
        async def _from(inst: Any) -> list[Any]:
            ra = getattr(inst, "react_agent", None)
            if ra is None:
                return []
            am = getattr(ra, "ability_manager", None)
            if am is None or not callable(getattr(am, "list_tool_info", None)):
                return []
            try:
                return list(await am.list_tool_info() or [])
            except Exception as exc:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] _get_agent_tools list_tool_info failed: %s",
                    exc,
                )
                return []

        # 1) 当前 adapter
        if self._instance is not None:
            tools = await _from(self._instance)
            if tools:
                return tools

        # 2) session-scoped child adapter（btw 等侧查询在 parent 执行时 tools 在 child）
        if not getattr(self, "_is_session_scoped_adapter", False):
            session_adapter = self._get_cached_session_adapter(session_id)
            if session_adapter is not None:
                inst = getattr(session_adapter, "_instance", None)
                if inst is not None:
                    return await _from(inst)

        return []

    def _get_agent_system_prompt(self) -> str:
        """Return the current agent's system prompt, or empty string if unavailable.

        Result is cached since the system prompt is derived from project context
        (CLAUDE.md, skills, etc.) which doesn't change within a session.
        Reusing the same bytes is critical for prompt cache prefix matching.
        """
        if self._last_system_prompt:
            return self._last_system_prompt
        if self._instance is None or self._instance.react_agent is None:
            return ""
        react_agent = self._instance.react_agent
        if hasattr(react_agent, "prompt_builder") and react_agent.prompt_builder is not None:
            self._last_system_prompt = react_agent.prompt_builder.build()
            return self._last_system_prompt
        if hasattr(react_agent, "system_prompt_builder") and react_agent.system_prompt_builder is not None:
            self._last_system_prompt = react_agent.system_prompt_builder.build()
            return self._last_system_prompt
        return ""

    async def _call_model_for_recap(
        self,
        messages: list[Any],
        prompt: str,
        system_prompt: str = "",
        enable_prompt_caching: bool = True,
        tools: list[Any] | None = None,
    ) -> str | None:
        """调用 model 生成简短回答（单轮、禁工具执行）。

        - system_prompt 非空时以 SystemMessage 形式前置
        - prompt 作为最后一条 user message 追加到对话末尾
        - tools 非空时透传给模型以保 cache key（与主 agent 一致），但单轮 +
          tool_use 检测丢弃 = 工具不被执行（对齐 claude-code canUseTool:deny）
        - 不设置 temperature（继承模型默认值，与主 agent 保持一致以复用 prompt cache）

        prompt cache 策略：
        - 保持消息原始格式（保留 structured content blocks，包括 tool_use/tool_result）
        - 最后一条 pre-prompt 消息添加 cache_control: {type: "ephemeral"} marker
        - btw prompt 不添加 cache_control（skipCacheWrite — 侧问题响应不写入 cache）
        """
        from openjiuwen.core.foundation.llm.schema.message import (
            AssistantMessage,
            SystemMessage,
            UserMessage,
        )

        if self._model is None:
            logger.error("[oneshot] no model instance available")
            return None

        recap_messages: list[Any] = []

        if system_prompt:
            recap_messages.append(SystemMessage(content=system_prompt))

        for msg in messages:
            role = getattr(msg, "role", None) or ""
            content = getattr(msg, "content", None) or ""

            # Skip truly empty messages
            if isinstance(content, str) and not content.strip():
                continue
            if isinstance(content, (list, tuple)) and len(content) == 0:
                continue
            if content is None:
                continue

            # Keep original content format (string or list of structured blocks).
            # This is critical for prompt cache prefix matching — converting to
            # plain text with str() would strip tool_use/tool_result blocks and
            # break byte-identical prefix matching with the main agent's calls.
            if role == "user":
                recap_messages.append(UserMessage(content=content))
            elif role == "assistant":
                recap_messages.append(AssistantMessage(content=content))
            else:
                recap_messages.append(UserMessage(content=content))

        # Mark the last pre-prompt message for prompt caching.
        # The btw prompt itself does NOT carry a cache_control marker
        # skipCacheWrite — the side-question
        # response doesn't create a new cache entry.
        if enable_prompt_caching and recap_messages:
            _try_add_cache_control(recap_messages[-1])

        # Append btw prompt as final user message (no cache_control → skipCacheWrite)
        recap_messages.append(UserMessage(content=prompt))

        try:
            # No temperature override — inherit model default to match main agent
            # API params (thinking config is part of the Anthropic cache key).
            result = await self._model.invoke(recap_messages, tools=tools)
            # Tool-use guard: tools schema is passed only to preserve the cache
            # key (matches the main agent). Single turn + discard any tool_use
            # the model emits → tools are never executed. Aligned with
            # claude-code's canUseTool:{behavior:'deny'} + tool_use fallback.
            tool_calls = getattr(result, "tool_calls", None)
            if tool_calls:
                names = ", ".join(getattr(tc, "name", "tool") for tc in tool_calls)
                logger.info(
                    "[btw/recap] model emitted tool_use despite no-tool constraint: %s",
                    names,
                )
                return (
                    f"(模型尝试调用工具 {names} 而非直接回答。"
                    "请重新措辞或在主对话中提问。)"
                )
            content = getattr(result, "content", None) or str(result)
            # Log cache metrics for observability
            usage = getattr(result, "usage_metadata", None)
            if usage and getattr(usage, "cache_tokens", 0) > 0:
                logger.info(
                    "[btw/recap] cache hit: cache_tokens=%s, input_tokens=%s, output_tokens=%s",
                    getattr(usage, "cache_tokens", 0),
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                )
            return content
        except Exception:
            logger.exception("[generate_recap] model call failed")
            return None

    async def generate_btw_answer(self, session_id: str, question: str) -> dict[str, Any]:
        """回答 /btw 侧问题：独立、无工具、单轮 LLM 查询。

        prompt cache 策略：
        - 共享主 agent 的 system prompt（项目上下文、skills、CLAUDE.md 等）
        - 保持消息原始格式（含 structured content blocks）以实现 byte-identical 前缀
        - 最后一条 pre-prompt 消息添加 cache_control marker（ephemeral）
        - btw prompt 不添加 cache_control（skipCacheWrite）
        - 透传主 agent tools schema 保 cache key，但单轮 + tool_use 丢弃 = 禁止执行
        - 不修改对话历史（read-only）

        Args:
            session_id: 会话ID
            question: 用户侧问题

        Returns:
            {"status": "ok", "answer": "..."} 或 {"status": "no_context"|"failed", ...}
        """
        from jiuwenswarm.server.runtime.agent_adapter.recap_prompts import (
            RECENT_MESSAGE_WINDOW,
            _build_btw_prompt,
        )

        # 1) 获取 system prompt（与主 agent 相同，已缓存）
        system_prompt = self._get_agent_system_prompt()

        # 2) 获取最近对话消息（保持原始格式，不做 str() 转换）
        messages = self._get_recent_messages(session_id, window=RECENT_MESSAGE_WINDOW)
        if not messages and not system_prompt:
            return {"status": "no_context"}

        # 2.5) 取主 agent tools（透传给模型以保 cache key；工具执行由单轮 +
        #      tool_use 检测丢弃禁止，对齐 claude-code canUseTool:deny）
        tools = await self._get_agent_tools(session_id)

        # 3) 构建 btw prompt（system prompt 通过 SystemMessage 传递，不嵌入文本）
        prompt = _build_btw_prompt(
            question=question,
            language=self._resolve_prompt_language(),
        )

        # 4) 调用模型 — system_prompt 作为 SystemMessage 前置，prompt 作为 UserMessage
        # enable_prompt_caching=True 启用 cache_control marker
        answer = await self._call_model_for_recap(
            messages, prompt, system_prompt=system_prompt, enable_prompt_caching=True,
            tools=tools or None,
        )
        if not answer:
            return {"status": "failed", "error": "Model returned empty response"}

        return {"status": "ok", "answer": answer.strip()}

    async def repair_model_response(self, prompt: str) -> str | None:
        """Run a focused repair prompt using the currently selected chat model."""
        if self._model is None:
            logger.warning("[JiuWenSwarmDeepAdapter] repair skipped: no model instance available")
            return None
        from openjiuwen.core.foundation.llm.schema.message import UserMessage

        result = await self._model.invoke(
            [UserMessage(content=prompt)],
            temperature=0,
        )
        content = getattr(result, "content", None)
        if isinstance(content, str):
            return content
        output = getattr(result, "output", None)
        if isinstance(output, str):
            return output
        return str(result) if result is not None else None

    async def compact_partial(
        self,
        session_id: str,
        turn_index: int,
        direction: str = "from",
    ) -> dict[str, Any]:
        """部分对话压缩 — /rewind summarize from here 的核心实现。

        从 history.json 读取消息，找到指定 turn 的 pivot 位置，将对应范围的消息
        发送给 LLM 生成结构化摘要（9 节：Primary Request, Technical Concepts,
        Files, Errors, Problem Solving, User Messages, Pending Tasks,
        Current Work, Optional Next Step）。

        Args:
            session_id: 会话ID
            turn_index: 基准 turn 号（1-based）
            direction: "from" (摘要 turn 及之后) 或 "up_to" (摘要 turn 之前)

        Returns:
            - status: "ok" | "no_turn" | "failed"
            - summary: 摘要文本
            - summarized_count: 被摘要的消息 record 数
        """
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            get_agent_sessions_dir,
        )
        from jiuwenswarm.server.runtime.session.session_history import _read_history
        from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import (
            NO_TOOLS_PREAMBLE,
            PARTIAL_COMPACT_PROMPT,
            PARTIAL_COMPACT_UP_TO_PROMPT,
        )

        sessions_dir = get_agent_sessions_dir()
        history_path = sessions_dir / session_id / "history.json"
        history = _read_history(history_path)
        if not history:
            return {"status": "no_turn"}

        user_positions = []
        for i, record in enumerate(history):
            if record.get("role") == "user":
                user_positions.append(i)

        total_turns = len(user_positions)
        if total_turns == 0 or turn_index > total_turns:
            return {"status": "no_turn"}

        pivot_idx = user_positions[turn_index - 1]

        if direction == "from":
            messages_to_summarize = history[pivot_idx:]
        elif direction == "up_to":
            messages_to_summarize = history[:pivot_idx]
        else:
            return {"status": "failed", "error": f"unknown direction: {direction}"}

        if not messages_to_summarize:
            return {"status": "no_turn"}

        summarized_count = len(messages_to_summarize)

        prompt = (
            NO_TOOLS_PREAMBLE + PARTIAL_COMPACT_UP_TO_PROMPT
            if direction == "up_to"
            else NO_TOOLS_PREAMBLE + PARTIAL_COMPACT_PROMPT
        )

        recap_messages = self._build_messages_for_model(messages_to_summarize)
        if not recap_messages:
            return {"status": "no_turn"}

        # Add the prompt as the final user message
        from openjiuwen.core.foundation.llm.schema.message import UserMessage
        recap_messages.append(UserMessage(content=prompt))

        try:
            result = await self._model.invoke(recap_messages, temperature=0)
            raw = getattr(result, "content", None) or str(result)
        except Exception:
            logger.exception("[compact_partial] model call failed")
            return {"status": "failed", "error": "Model call failed"}

        summary = raw if isinstance(raw, str) else str(raw)
        if not summary.strip():
            return {"status": "failed", "error": "Model returned empty response"}

        # Strip <analysis> block to get clean summary
        cleaned = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL).strip()

        return {
            "status": "ok",
            "summary": cleaned or summary.strip(),
            "summarized_count": summarized_count,
        }

    @staticmethod
    def _build_messages_for_model(records: list[dict[str, Any]]) -> list[Any]:
        from openjiuwen.core.foundation.llm.schema.message import UserMessage, AssistantMessage

        messages: list[Any] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            role = rec.get("role")
            content = rec.get("content")
            if not isinstance(content, str) or not content.strip():
                continue

            event_type = rec.get("event_type")
            # skip tool call/result records — they contain JSON blobs, not useful for summary
            if role == "user":
                # strip file-content blocks to save tokens
                cleaned = re.sub(r"<file-content[^>]*>.*?</file-content>", "", content, flags=re.DOTALL).strip()
                if cleaned:
                    messages.append(UserMessage(content=cleaned))
            elif role == "assistant":
                if rec.get("is_goal_completed_message"):
                    continue
                if event_type in ("chat.final", "context.compact_summary", "context.rewind_summary") or not event_type:
                    if event_type in ("context.compact_boundary",):
                        continue
                    messages.append(AssistantMessage(content=content))

        return messages

    async def _count_full_context_tokens(
        self,
        context: Any,
        react_agent: Any,
        session_id: str,
    ) -> int:
        """计算完整上下文的 token 数（包含 system messages + context messages + tools）。
        Args:
            context: ModelContext 对象
            react_agent: ReActAgent 对象
            session_id: 会话ID

        Returns:
            完整上下文的 token 总数
        """
        from openjiuwen.core.foundation.tool import ToolInfo

        token_counter = context.token_counter()
        total_tokens = 0

        # 1. 计算系统消息的 tokens
        system_prompt = self._get_agent_system_prompt()

        if system_prompt:
            if token_counter is not None:
                total_tokens += token_counter.count(system_prompt)
            else:
                total_tokens += len(system_prompt) // 4

        # 2. 计算对话消息的 tokens
        context_messages = context.get_messages()
        if context_messages:
            if token_counter is not None:
                total_tokens += token_counter.count_messages(context_messages)
            else:
                total_tokens += sum(len(str(msg.content)) // 4 for msg in context_messages)

        # 3. 计算工具定义的 tokens
        tools: list[ToolInfo] = []
        if hasattr(react_agent, "ability_manager") and react_agent.ability_manager is not None:
            for card in react_agent.ability_manager.list() or []:
                if hasattr(card, "to_tool_info"):
                    tools.append(card.to_tool_info())
                elif hasattr(card, "name") and hasattr(card, "description"):
                    tools.append(ToolInfo(
                        name=card.name,
                        description=card.description or "",
                        parameters=getattr(card, "input_params", {}),
                    ))

        if tools and token_counter is not None:
            total_tokens += token_counter.count_tools(tools)

        return total_tokens

    async def _watch_evolution_and_push(self, rid: str, cid: str, session_id: str) -> None:
        """Poll passive evolution events and push progress, approval, and terminal status."""
        from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

        push_context = EvolutionPushContext(
            transport=WebSocketGatewayPushTransport(),
            channel_id=cid,
            session_id=session_id,
        )

        async def _push_status(status: str, stage: str, message: str = "") -> None:
            await push_evolution_status(
                push_context,
                build_evolution_status_update(rid, status, stage, message),
                build_server_push_message,
                include_payload_request_id=False,
            )

        async def _push_approval(evt) -> None:
            await push_evolution_event(
                push_context,
                rid,
                evt,
                build_server_push_message,
            )

        async def _cleanup_evolution_rail(*, cancel: bool = False) -> None:
            if self._skill_evolution_rail is None:
                return
            try:
                rail = self._skill_evolution_rail
                if cancel:
                    cancel_fn = getattr(rail, "cancel_pending_evolution", None)
                    if cancel_fn is not None:
                        await cancel_fn()
                        return
                await rail.cleanup_background_tasks()
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] evolution cleanup failed: request_id=%s "
                    "session_id=%s error=%s",
                    rid,
                    session_id,
                    exc,
                )

        try:
            if self._skill_evolution_rail is None:
                return

            active = False
            last_event_at = time.monotonic()
            event_timeout_sec = resolve_evolution_event_timeout_sec(
                self._skill_evolution_rail,
                fallback_sec=TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
            )

            while True:
                if self._skill_evolution_rail is None:
                    return

                events = await self._skill_evolution_rail.drain_pending_approval_events(wait=False) or []
                if not events:
                    idle_for = time.monotonic() - last_event_at
                    if idle_for >= event_timeout_sec:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] evolution watcher timed out: "
                            "request_id=%s session_id=%s idle_for=%.1fs",
                            rid,
                            session_id,
                            idle_for,
                        )
                        if active:
                            message = (
                                f"Evolution analysis timed out after "
                                f"{event_timeout_sec:.0f}s without host events"
                            )
                            await _push_status("end", "hidden", message)
                        await _cleanup_evolution_rail(cancel=True)
                        return
                    await asyncio.sleep(TEAM_EVOLUTION_IDLE_SLEEP_SEC)
                    continue
                last_event_at = time.monotonic()

                # Queue skills for auto version merge only when experiences were
                # persisted this cycle (``auto_approved``). Do not treat
                # ``completed`` as persist: SDK may emit stage=completed with
                # skill_name for ``no_evolution_no_records``, and team rail emits
                # completed when a request is merely ready. Per-skill gate:
                # only selfEvolution=auto is queued inside
                # ``_queue_auto_rebuild_skill``.
                for evt in events:
                    payload = event_payload_dict(evt)
                    meta = evolution_meta_from_payload(payload) or {}
                    skill_name = str(
                        payload.get("skill_name")
                        or meta.get("skill_name")
                        or ""
                    ).strip()
                    raw_stage = str(
                        payload.get("stage") or meta.get("stage") or ""
                    ).strip().lower()
                    if skill_name and raw_stage == "auto_approved":
                        self._queue_auto_rebuild_skill(skill_name)

                visible_progress_statuses = visible_evolution_progress_from_events(events)
                just_started_with_progress = None
                if not active:
                    start_progress_statuses = visible_regular_evolution_start_progress(
                        visible_progress_statuses
                    )
                    if start_progress_statuses:
                        just_started_with_progress = start_progress_statuses[0]

                if just_started_with_progress is not None:
                    start_stage = just_started_with_progress.stage
                    start_message = just_started_with_progress.message
                    await _push_status("start", start_stage, start_message)
                    active = True

                await push_evolution_progress(
                    push_context,
                    rid,
                    events,
                    parse_stream_chunk=self._parse_stream_chunk,
                    build_push_message=build_server_push_message,
                )

                progress_statuses_to_push = visible_progress_statuses
                if just_started_with_progress is not None:
                    progress_statuses_to_push = [
                        progress_status
                        for progress_status in visible_progress_statuses
                        if progress_status is not just_started_with_progress
                    ]
                for progress_status in progress_statuses_to_push:
                    if progress_status.terminal:
                        continue
                    await _push_status("progress", progress_status.stage, progress_status.message)

                approval_events = [evt for evt in events if is_evolution_approval_event(evt)]
                if approval_events:
                    if not active:
                        await _push_status("start", "approval_required", "")
                        active = True
                    for evt in approval_events:
                        await _push_approval(evt)
                    await _push_status("end", "approval_required", "")
                    await _cleanup_evolution_rail()
                    return

                outcomes = [
                    evolution_outcome_from_event(evt)
                    for evt in events
                    if is_evolution_outcome_event(evt)
                ]
                if outcomes:
                    outcome = outcomes[-1]
                    stage = str(outcome.get("status") or "completed").strip().lower()
                    message = str(outcome.get("message") or "")
                    if stage in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES:
                        end_stage = "hidden"
                    else:
                        end_stage = stage or "completed"
                    if (
                        not active
                        and (
                            end_stage == "hidden"
                            or end_stage in TEAM_EVOLUTION_NOOP_STAGES
                        )
                    ):
                        await _cleanup_evolution_rail()
                        return
                    if not active:
                        await _push_status("start", end_stage, message)
                        active = True
                    await _push_status(
                        "end",
                        end_stage,
                        message or "Evolution analysis completed",
                    )
                    await _cleanup_evolution_rail()
                    self._schedule_pending_auto_rebuild(rid)
                    return

                terminal_progress = [
                    terminal
                    for terminal in (team_evolution_terminal_progress(evt) for evt in events)
                    if terminal is not None
                ]
                if terminal_progress:
                    terminal = terminal_progress[-1]
                    end_stage = terminal_stage(terminal) or "no_evolution_generated"
                    if end_stage in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES:
                        end_stage = "hidden"
                    if (
                        not active
                        and (
                            end_stage == "hidden"
                            or end_stage in TEAM_EVOLUTION_NOOP_STAGES
                        )
                    ):
                        await _cleanup_evolution_rail()
                        self._schedule_pending_auto_rebuild(rid)
                        return
                    if not active:
                        await _push_status(
                            "start",
                            end_stage,
                            str(terminal.get("message") or ""),
                        )
                        active = True
                    await _push_status(
                        "end",
                        end_stage,
                        str(terminal.get("message") or ""),
                    )
                    await _cleanup_evolution_rail()
                    # auto_approved maps to terminal "completed"; must schedule here
                    # (outcome events are not emitted on the rail auto-save path).
                    self._schedule_pending_auto_rebuild(rid)
                    return
        except asyncio.CancelledError:
            try:
                await _cleanup_evolution_rail(cancel=True)
            finally:
                raise
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] evolution watcher failed: %s", exc)
            try:
                await _push_status("end", "hidden", "")
            except Exception:
                pass

    def _on_evolution_watcher_done(self, task: asyncio.Task) -> None:
        """Callback when an evolution watcher task completes.

        Discards the task from the tracking set and logs any exception.
        """
        self._evolution_watcher_tasks.discard(task)
        try:
            task.result()
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] evolution watcher task exception: %s", exc)

    @staticmethod
    def _is_approval_event(evt) -> bool:
        """Check whether an OutputSchema event is an approval request."""
        evt_type = getattr(evt, "type", "")
        if evt_type == "chat.ask_user_question":
            return True
        if hasattr(evt, "payload") and isinstance(evt.payload, dict):
            return evt.payload.get("event_type") == "chat.ask_user_question"
        return False

    async def try_start_dreaming(self, busy_checker: Callable[[], bool] | None = None) -> None:
        if self._dreaming_started:
            return
        try:
            from jiuwenswarm.agents.harness.common.memory.dreaming import start_dreaming
            from jiuwenswarm.common.utils import get_agent_sessions_dir
            sessions_dir = str(get_agent_sessions_dir() or "")
            mode = getattr(self, "_dreaming_mode", "agent")
            output_name = "memory" if mode == "agent" else "coding_memory"
            base_dir = getattr(self, "_agent_workspace_dir", None) or self._workspace_dir
            output_dir = os.path.join(base_dir, output_name)
            orch = await start_dreaming(
                sessions_dir=sessions_dir,
                output_dir=output_dir,
                mode=mode,
                busy_checker=busy_checker,
            )
            self._dreaming_started = orch is not None
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] start_dreaming failed: %s", exc)

    async def try_stop_dreaming(self) -> None:
        if not self._dreaming_started:
            return
        try:
            from jiuwenswarm.agents.harness.common.memory.dreaming import stop_dreaming
            mode = getattr(self, "_dreaming_mode", "agent")
            await stop_dreaming(mode=mode)
            self._dreaming_started = False
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] stop_dreaming failed: %s", exc)


def _agent_def_to_subagent_config(
    agent_def: AgentDefinition,
    model: Any,
    workspace: str,
    model_cache: dict[str, Any] | None = None,
) -> SubAgentConfig:
    """将 AgentDefinition 转换为 SubAgentConfig，用于 SubagentRail 注册。

    Args:
        agent_def: 自定义 agent 定义（来自 .jiuwenswarm/agents/*.md）
        model: 父 agent 的 Model 实例（作为默认模型）
        workspace: 工作空间路径
        model_cache: 模型缓存字典（用于按名称查找指定模型）
    """
    from openjiuwen.harness.schema.config import SubAgentConfig

    # Resolve model: if agent_def specifies a model name, look it up in cache
    resolved_model = model
    if agent_def.model and isinstance(model_cache, dict):
        resolved_model = model_cache.get(agent_def.model, model)

    # Build tool list: merge allowed tools and disallowed_tools
    tools: list[str] = list(agent_def.tools) if agent_def.tools else ["*"]
    if agent_def.disallowed_tools and tools != ["*"]:
        tools = [t for t in tools if t not in agent_def.disallowed_tools]

    # A stable id keeps ``create_deep_agent`` on its get-or-create path for this
    # sub-agent's SysOperation: the id is derived from the card, and the default
    # ``AgentCard.id`` is a fresh uuid, so leaving it unset made every Agent-tool
    # invocation register another SysOperation plus its ~16 derived tools in the
    # process-global resource manager, never to be reclaimed. Same-named
    # definitions carry the same permissions, so sharing one instance is safe.
    card = AgentCard(
        name=agent_def.name,
        id=f"subagent_{agent_def.name}",
        description=agent_def.description,
    )

    # Build progressive tool rail for custom subagent if enabled.
    custom_rails: list[Any] | None = None
    config_base = get_config()
    react_config = config_base.get("react", {}) or {}
    if is_subagent_tool_lazy_load_enabled(react_config):
        progressive_rail = build_progressive_tool_rail_from_config(
            react_config,
            language=resolve_language(
                str(config_base.get("preferred_language", "zh"))
            ),
            profile="subagent",
            agent_id=card.id,
            agent_card_id=card.id,
        )
        if progressive_rail is not None:
            custom_rails = [progressive_rail]

    return SubAgentConfig(
        agent_card=card,
        system_prompt=agent_def.prompt,
        tools=tools,
        model=resolved_model,
        skills=agent_def.skills,
        max_iterations=agent_def.max_iterations,
        enable_task_loop=True,
        rails=custom_rails,
    )


def _load_custom_subagents(
    workspace_dir: str,
    subagents_cfg: dict | None,
    model: Any,
    workspace: str,
    logger_name: str,
    **kwargs: Any,
) -> list[Any]:
    """从 AgentConfigService 加载自定义 agent 并转换为 SubAgentConfig 列表。

    通用逻辑，同时被 JiuWenSwarmDeepAdapter 和 JiuWenSwarmCodeAdapter 使用。

    Args:
        workspace_dir: 工作空间目录路径
        subagents_cfg: 子 agent 配置字典
        model: 模型配置
        workspace: 工作空间路径
        logger_name: 日志记录器名称
        **kwargs: 额外参数，支持 model_cache 等
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    _logger = logging.getLogger(logger_name)
    agent_service = AgentConfigService(workspace_dir)
    model_cache: dict | None = kwargs.get("model_cache")
    result: list[Any] = []
    for agent_def in agent_service.list_agents():
        if agent_def.source == "builtin":
            continue
        subagent_cfg = subagents_cfg.get(agent_def.name) if isinstance(subagents_cfg, dict) else None
        # 只有显式 enabled: true 才加载
        if not (isinstance(subagent_cfg, dict) and bool(subagent_cfg.get("enabled", False))):
            continue
        custom_spec = _agent_def_to_subagent_config(agent_def, model, workspace, model_cache)
        custom_spec.factory_kwargs = {"auto_create_workspace": False}
        result.append(custom_spec)
        _logger.info("loaded custom agent '%s' from %s", agent_def.name, agent_def.source)
    return result
