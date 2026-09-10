# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenClaw Deep Adapter - 基于 openjiuwen DeepAgent 的适配器实现.

此模块实现 AgentAdapter 协议，封装 Deep SDK 的所有专属逻辑。
公共编排逻辑（session 队列、Skills 路由、heartbeat 等）由 Facade 层处理。
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass, field, replace

from pathlib import Path
from typing import Any, AsyncIterator, Callable, List, Self, Sequence, Tuple

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
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner

try:
    from openjiuwen.core.runner import set_request_id, reset_request_id
except ImportError:
    def set_request_id(rid: str) -> None:
        return None

    def reset_request_id(token: None) -> None:
        pass
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import AgentCard, ReActAgentConfig, create_agent_session
from openjiuwen.core.sys_operation import (
    SysOperation,
    SysOperationCard,
    OperationMode,
    LocalWorkConfig,
    SandboxGatewayConfig,
)
from openjiuwen.core.sys_operation.config import (
    SandboxIsolationConfig,
    PreDeployLauncherConfig,
    ContainerScope
)
from openjiuwen.harness import (
    AudioModelConfig,
    DeepAgent,
    DeepAgentConfig,
    VisionModelConfig,
)
try:
    from openjiuwen.harness.agent_ras import AgentRASConfig
except ImportError:
    # Older openjiuwen builds may lack Agent RAS; degrade passthrough below.
    AgentRASConfig = None
from openjiuwen.harness.factory import create_deep_agent
from pydantic import ValidationError
from openjiuwen.harness.subagents.code_agent import create_code_agent
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.prompts.sections.memory import build_memory_section
from openjiuwen.harness.rails import SkillUseRail, TaskPlanningRail, SecurityRail
from openjiuwen.harness.rails.subagent_rail import SubagentRail
from openjiuwen.harness.rails.lsp_rail import LspRail
from openjiuwen.harness.rails.context_engineering_rail import ContextEngineeringRail
from openjiuwen.harness.rails.filesystem_rail import FileSystemRail
from openjiuwen.harness.rails.heartbeat_rail import HeartbeatRail
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload as _SkillTurboConfirmPayload
from openjiuwen.core.runner.callback import AbortError as _SkillTurboAbortError
from openjiuwen.agent_evolving.checkpointing import EvolutionStore
from openjiuwen.agent_evolving.signal import SignalDetector
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
try:
    from openjiuwen.agent_evolving.experience.rebuild import ExperienceRebuildService
    from openjiuwen.harness.rails.evolution.commands import build_rebuild_command_prompt
except ImportError:
    ExperienceRebuildService = None
    build_rebuild_command_prompt = None
from openjiuwen.agent_evolving.trajectory import FileTrajectoryStore
from openjiuwen.harness.rails.memory_rail import MemoryRail
from openjiuwen.harness.rails.coding_memory_rail import CodingMemoryRail
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.code_agent import build_code_agent_config
from openjiuwen.harness.subagents.research_agent import build_research_agent_config
from openjiuwen.harness.tools import (
    create_audio_tools,
    create_vision_tools,
)
from openjiuwen.harness.tools.todo import TodoModifyTool
from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode
from openjiuwen.core.context_engine.processor.compressor.full_compact_processor import (
    FullCompactProcessorConfig,
)
from openjiuwen.core.context_engine.qa_artifact.schema import IrreducibleContextError
from openjiuwen.core.context_engine.qa_artifact.schema import (
    QAArtifactConfig,
    validate_qa_artifact_thresholds,
)
from openjiuwen.core.context_engine.qa_block.config import QABlockConfig

from jiuwenclaw.runtime.shell_pip_patch import set_skill_credential_provider
from jiuwenclaw.agentserver.utils import DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL
from jiuwenclaw.agentserver.deep_agent.cron_runtime import CronRuntimeBridge
from jiuwenclaw.agentserver.deep_agent.code_rails_builder import (
    _code_memory_enabled,
    build_code_mode_extra_rails,
)
from jiuwenclaw.agentserver.deep_agent.rails.project_memory_rail import (
    ProjectMemoryRail,
)
from jiuwenclaw.agentserver.deep_agent.skill_evolution_rail import JiuClawSkillEvolutionRail
from jiuwenclaw.agentserver.deep_agent.ask_user_question_registry import (
    ASK_REQUEST_PREFIX,
    AskUserQuestionRegistry,
    ask_user_question_request_scope,
)
from jiuwenclaw.agentserver.runtime_scope import RuntimeScopeKey
from jiuwenclaw.agentserver.llm_io_trace import (
    begin_llm_trace_event,
    end_llm_trace_event,
    log_chat_final,
    log_invoke_input,
    log_invoke_output,
    log_reasoning_delta,
    log_stream_input,
    log_stream_output,
)
from jiuwenclaw.perf.interface_hooks import (
    clear_perf_summary_context,
    finalize_perf_summary_request,
    mark_request_first_byte,
    set_perf_summary_context,
)
from jiuwenclaw.agentserver.deep_agent.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)
from jiuwenclaw.agentserver.deep_agent.interrupt_resume_helpers import (
    prepare_interrupt_resume_for_request,
    set_todo_resume_snapshot_pending,
)
from jiuwenclaw.agentserver.deep_agent.stale_todo_cleanup_helpers import (
    prepare_stale_todo_cleanup_for_request,
)
from jiuwenclaw.agentserver.skill_turbo.permission_bridge import (
    build_interaction_output_from_abort as _skill_turbo_build_interaction_output,
    clear_resume_ctx as _skill_turbo_clear_resume_ctx,
    extract_tool_interrupt as _skill_turbo_extract_tool_interrupt,
    load_resume_ctx as _skill_turbo_load_resume_ctx,
    set_skill_turbo_id as _skill_turbo_set_agent_id,
)
from jiuwenclaw.agentserver.deep_agent.plan_pause_helpers import (
    build_paused_plan_decision_prompt_from_session_snapshot,
    cancel_pending_todos_on_tool,
    clear_plan_pause_file,
    clear_plan_pause_on_session,
    clear_task_plan_on_state,
    merge_supplementary_into_request_params,
    snapshot_and_isolate_unfinished_todos,
    persist_checkpoint_for_session,
    post_agent_execute_for_session,
    resolve_context_engine,
    read_plan_pause_from_file,
    read_plan_pause_from_session,
    repair_task_plan_after_pause,
    write_plan_pause_to_file,
    write_plan_pause_to_session,
    _resolve_session_for_checkpoint,
    read_interrupt_artifacts_summary_from_session,
    read_interrupt_artifacts_from_file,
    write_interrupt_artifacts_to_file,
    write_interrupt_artifacts_summary_to_session,
    build_interrupt_artifacts_resume_prompt,
    clear_interrupt_artifacts_summary_from_session,
    clear_interrupt_recovery_injected,
    clear_interrupt_artifacts_file,
    is_interrupt_recovery_injected,
    mark_interrupt_recovery_injected,
    INTERRUPT_ARTIFACTS_SUMMARY_KEY
)
from jiuwenclaw.agentserver.deep_agent.prompt_builder import build_identity_prompt
from jiuwenclaw.agentserver.deep_agent.rails import (
    JiuClawContextEngineeringRail,
    JiuClawQAArtifactRail,
    JiuClawQABlockAssemblyRail,
    JiuClawQABlockFreezeRail,
    JiuClawStreamEventRail,
    ContextOverflowRecoveryRail,
    ResponsePromptRail,
    RuntimePromptRail,
    SkillComplianceRail,
    SkillCredentialInjectionRail,
    SkillProtocolPromptRail,
    TaskExecutionRail,
    coalesce_config_skill_envs,
    coalesce_skill_envs,
)
from jiuwenclaw.agentserver.deep_agent.rails.recent_tool_results_rail import (
    RecentToolResultsRail,
)
from jiuwenclaw.agentserver.deep_agent.rails.context_engineering_rail_ext import (
    normalize_identify_override,
    normalize_soul_override,
)
from jiuwenclaw.agentserver.utils import extract_uploaded_files
from jiuwenclaw.agentserver.deep_agent.rails.disabled_tools_rail import DisabledToolsRail
from jiuwenclaw.agentserver.deep_agent.rails.jiuwen_progressive_tool_rail import (
    JiuWenProgressiveToolRail,
)
from jiuwenclaw.agentserver.deep_agent.rails.jiuwen_skill_use_rail import JiuWenSkillUseRail
from jiuwenclaw.agentserver.deep_agent.tool_qualify import (
    clone_tool_for_session,
    register_qualified_tool,
    register_qualified_tools,
    remove_tool_from_resource_mgr,
    reregister_qualified_tool_in_resource_mgr,
)
from jiuwenclaw.agentserver.deep_agent.rails.permission_rail import clear_session_interrupt_state
from jiuwenclaw.agentserver.deep_agent.rails.pip_isolation_rail import PipIsolationRail
from jiuwenclaw.agentserver.deep_agent.rails.task_execution_rail import get_current_task_id
from jiuwenclaw.agentserver.deep_agent.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    setup_permission_context,
    cleanup_permission_context,
)
from jiuwenclaw.agentserver.permissions.core import init_permission_engine, get_permission_engine
from jiuwenclaw.agentserver.memory import (
    bind_memory_cache_fingerprint,
    reset_memory_cache_fingerprint,
)
from jiuwenclaw.agentserver.session_skill_dirs import (
    bind_session_registered_skill_dirs,
    reset_session_registered_skill_dirs,
)
from jiuwenclaw.agentserver.reload_result import (
    ReloadResult,
    embed_config_fingerprint,
    env_touches_memory,
    env_touches_shared_skills_dirs,
    env_touches_task_memory,
    memory_cache_fingerprint,
)
from jiuwenclaw.local_env_config import (
    bind_agent_env_ns,
    bind_task_env_overlay,
    build_effective_env_overlay,
    effective_tip,
    get_local_config,
    get_task_env_overlay,
    promote_staged_env,
    read_env,
    reset_agent_env_ns,
    reset_task_env_overlay,
    set_os_environ,
)
from jiuwenclaw.agentserver.memory.external_memory_config import (
    get_memory_engine,
    external_memory_fingerprint,
    is_external_memory_enabled,
)
from jiuwenclaw.agentserver.memory.config import (clear_config_cache, get_memory_mode, is_memory_enabled,
                                                  is_proactive_memory)
from jiuwenclaw.agentserver.memory.manager import (
    invalidate_memory_manager_cache,
    invalidate_memory_wiki_manager_cache,
)
from jiuwenclaw.agentserver.tools.task_tools import (
    clear_task_memory_service,
    task_memory_config_fingerprint,
)
from jiuwenclaw.agentserver.permissions.checker import TOOL_PERMISSION_CHANNEL_ID
from jiuwenclaw.agentserver.cron_config import should_register_cron_tools
from jiuwenclaw.agentserver.skill_manager import (
    SkillManager,
    safe_path_name,
    enabled_skills_from_environ,
    resolve_string_or_list_config,
)
from jiuwenclaw.agentserver.tools.memory_tools import (
    init_memory_manager_async,
    get_decorated_tools,
)
from jiuwenclaw.agentserver.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_image_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    clear_multimodal_env_groups,
    dedicated_multimodal_model_configured,
    multimodal_env_anchor_present,
    MULTIMODAL_ENV_GROUP_KEYS,
)
from jiuwenclaw.agentserver.tools.image_gen_tools import create_session_text_to_image_tool
from jiuwenclaw.agentserver.tools.video_tools import video_understanding
from jiuwenclaw.agentserver.tools.harness_named_web_tools import build_jiuwen_harness_named_web_tools

from jiuwenclaw.agentserver.tools import SendFileToolkit, SkillToolkit
from jiuwenclaw.agentserver.tools.ask_user_question_tool import get_ask_user_question_tool
from jiuwenclaw.agentserver.tools.acp_output_tools import get_tools as get_acp_output_tools
from jiuwenclaw.agentserver.tools.acp_output_tools import get_acp_output_manager
from jiuwenclaw.agentserver.tools.deepresearch_tools import (
    push_deepresearch_route,
    reset_deepresearch_route,
    get_deepresearch_tools,
)
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
    patch_model_config_from_env,
    resolve_env_vars,
    clear_config_cache as clear_global_config_cache,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    resolve_env_vars,
    _FALSE_VALUES,
    _TRUE_VALUES,
    _sandbox_yaml_to_env_overlay,
)
from jiuwenclaw.agentserver.deep_agent.sysop_builder import (
    create_local_sysop_card,
    create_sandbox_sysop_card,
)
from jiuwenclaw.agentserver.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
    run_rewrite_fast_path,
)
from jiuwenclaw.agentserver.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlFollowupResult,
    RewriteHtmlTarget,
    decode_html_tool_result,
    is_html_followup_request,
    target_from_commit_result,
    target_from_state,
)
from jiuwenclaw.agentserver.stream_content_sanitize import strip_inline_tool_protocol
from jiuwenclaw.agentserver.stream_utils import propagate_stream_source_id, tool_calls_payload_to_json_list
from jiuwenclaw.agentserver.extensions import get_rail_manager
from jiuwenclaw.gateway.cron.models import CronTargetChannel
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.message import ReqMethod
from jiuwenclaw.utils import (
    deep_merge_dicts,
    get_agent_evolution_trajectories_dir,
    get_agent_registered_skill_dirs,
    resolve_agent_registered_skill_dirs,
    get_agent_workspace_dir,
    get_checkpoint_dir,
    get_env_file,
    get_agent_root_dir,
    is_bootstrap_builtin_skill,
)

load_dotenv(dotenv_path=get_env_file())
from jiuwenclaw.local_env_config import ingest_bare_business_into_tip

ingest_bare_business_into_tip()

from jiuwenclaw.agentserver.deep_agent.agent_card_id import (
    DEFAULT_SESSION_ID,
    JIUWENCLAW_RESOURCE_AGENT_ID,
    is_default_session,
    resolve_agent_card_id as _resolve_agent_card_id_pure,
)

_react_config = get_config().get("react", {})
_sandbox_config = get_config().get("sandbox", {})


@asynccontextmanager
async def _noop_async_context() -> AsyncIterator[None]:
    yield


async def _empty_agent_stream() -> AsyncIterator[Any]:
    if False:
        yield None


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


def _reset_llm_trace_tokens(
    token_sid: Token,
    token_rid: Token,
    token_iter: Token,
    token_model: Token,
) -> None:
    _LLM_TRACE_SESSION_ID.reset(token_sid)
    _LLM_TRACE_REQUEST_ID.reset(token_rid)
    _LLM_TRACE_ITERATION.reset(token_iter)
    _LLM_TRACE_MODEL_NAME.reset(token_model)


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


@dataclass(slots=True)
class _AgentInitContext:
    """`_init_agent_instance_sync` 的具名入参封装（DeepAgent 实例初始化上下文）。"""

    config: dict[str, Any]
    config_base: dict[str, Any]
    mode: str
    model: Model
    agent_card: AgentCard
    tool_cards: list[Any]
    extra_skill_dir: str | None = None


@dataclass(slots=True)
class _RuntimeConfigParams:
    """`_update_runtime_config` 的具名入参封装（会话、模式与请求级上下文）。"""

    session_id: str | None
    mode: str = "agent.plan"
    request_id: str | None = None
    channel_id: str | None = None
    request_metadata: dict[str, Any] | None = None
    request_system_prompt: str | None = None
    request_identify: str | None = None
    request_soul: str | None = None
    sync_code_submode: bool = True

    @classmethod
    def _read_param_str(cls, params: Any, *keys: str) -> str | None:
        """Read a non-empty string param; whitespace-only values are treated as absent."""
        if not isinstance(params, dict):
            return None
        for key in keys:
            raw = params.get(key)
            if isinstance(raw, str):
                value = raw.strip()
                if value:
                    return value
            elif raw is not None and not isinstance(raw, (dict, list)):
                value = str(raw).strip()
                if value:
                    return value
        return None

    @classmethod
    def from_agent_request(cls, request: AgentRequest, mode: str) -> Self:
        params = request.params if isinstance(request.params, dict) else {}
        is_interrupt_resume = (
            request.req_method == ReqMethod.CHAT_RESUME
            or bool(params.get("answers"))
            or isinstance(params.get("query"), InteractiveInput)
        )
        return cls(
            session_id=request.session_id,
            mode=mode,
            request_id=request.request_id,
            channel_id=request.channel_id,
            request_metadata=request.metadata,
            request_system_prompt=cls._read_param_str(params, "system_prompt", "systemPrompt"),
            request_identify=normalize_identify_override(
                cls._read_param_str(params, "identify", "identity", "IDENTITY"),
            ),
            request_soul=normalize_soul_override(cls._read_param_str(params, "soul", "SOUL")),
            # A resume continues the session state that the interrupted tool
            # mutated.  Replaying the request's entry mode here would undo a
            # dynamic switch_mode(plan/auto) before the tool can continue.
            sync_code_submode=not is_interrupt_resume,
        )


def _parse_int(value: Any, default: int) -> int:
    """Parse integer-like values safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool_switch(value: Any, default: bool = False) -> bool:
    """Parse true/false switch config values."""
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _normalize_tool_names(value: Any, default: list[str] | None = None) -> list[str]:
    """Normalize comma-separated/list tool-name config values."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default or [])


_DEFAULT_PROGRESSIVE_EAGER_TOOLS = [
    "tools_search",
    "invoke_tool",
    "web_search",
    "fetch_webpage",
    "ask_user_question",
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "code",
    "skill_tool",
    "skill_complete",
    "todo_create",
    "todo_list",
    "todo_modify",
]

_SUBAGENT_PROGRESSIVE_EAGER_EXCLUDE_BY_KIND: dict[str, frozenset[str]] = {
    "spawn": frozenset({"fork_agent", "spawn_subagent"}),
    "fork": frozenset({"fork_agent", "spawn_subagent"}),
}

_SUBAGENT_PROGRESSIVE_EAGER_EXCLUDE_COMMON = frozenset({"load_qa_index"})

_PROGRESSIVE_META_TOOL_NAMES = frozenset({"tools_search", "invoke_tool"})


def is_subagent_tool_lazy_load_enabled(react_config: dict[str, Any] | None) -> bool:
    """True when react.tool_lazy_load and subagents are both enabled."""
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        return False
    if not _parse_bool_switch(lazy_cfg.get("enabled", False), default=False):
        return False
    sub_cfg = lazy_cfg.get("subagents") or {}
    if not isinstance(sub_cfg, dict):
        return False
    return _parse_bool_switch(sub_cfg.get("enabled", False), default=False)


def _ensure_progressive_meta_tools(eager_tools: list[str]) -> list[str]:
    if "tools_search" not in eager_tools:
        eager_tools.insert(0, "tools_search")
    if "invoke_tool" not in eager_tools:
        eager_tools.insert(1, "invoke_tool")
    return eager_tools


def build_jiuwen_progressive_tool_rail_from_react_config(
    react_config: dict[str, Any],
    *,
    language: str,
    profile: str = "main",
    agent_id: str | None = None,
    agent_card_id: str | None = None,
    subagent_kind: str | None = None,
) -> JiuWenProgressiveToolRail | None:
    """Build JiuWenProgressiveToolRail from react.tool_lazy_load config.

    Fixed eager-tools schema; deferred tools are reached via tools_search + invoke_tool.
    For spawn/fork subagents, set profile=\"subagent\" and configure react.tool_lazy_load.subagents.
    """
    config = react_config if isinstance(react_config, dict) else {}
    lazy_cfg = config.get("tool_lazy_load") or {}
    if not isinstance(lazy_cfg, dict):
        lazy_cfg = {}

    if not _parse_bool_switch(lazy_cfg.get("enabled", False), default=False):
        return None

    enable_for_models = _normalize_tool_names(lazy_cfg.get("enable_for_models", []), [])

    normalized_profile = (profile or "main").strip().lower()
    if normalized_profile == "subagent":
        sub_cfg = lazy_cfg.get("subagents") or {}
        if not isinstance(sub_cfg, dict):
            sub_cfg = {}
        if not _parse_bool_switch(sub_cfg.get("enabled", False), default=False):
            return None

        main_eager = _ensure_progressive_meta_tools(
            _normalize_tool_names(
                lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
                _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
            )
        )
        if _parse_bool_switch(sub_cfg.get("inherit_parent_eager_tools"), default=False):
            eager_tools = list(main_eager)
        else:
            eager_tools = _ensure_progressive_meta_tools(
                _normalize_tool_names(
                    sub_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
                    _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
                )
            )

        kind = (subagent_kind or "").strip().lower()
        excluded = set(_SUBAGENT_PROGRESSIVE_EAGER_EXCLUDE_COMMON)
        kind_excluded = _SUBAGENT_PROGRESSIVE_EAGER_EXCLUDE_BY_KIND.get(kind)
        if kind_excluded:
            excluded.update(kind_excluded)
        if excluded:
            eager_tools = [name for name in eager_tools if name not in excluded]
            eager_tools = _ensure_progressive_meta_tools(eager_tools)
    else:
        eager_tools = _ensure_progressive_meta_tools(
            _normalize_tool_names(
                lazy_cfg.get("eager_tools", _DEFAULT_PROGRESSIVE_EAGER_TOOLS),
                _DEFAULT_PROGRESSIVE_EAGER_TOOLS,
            )
        )

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

    return JiuWenProgressiveToolRail(
        enabled=True,
        eager_tools=eager_tools,
        language=normalized_language,
        agent_id=agent_id,
        agent_card_id=agent_card_id,
        enable_for_models=enable_for_models,
    )


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
            event_token = begin_llm_trace_event()
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
            finally:
                end_llm_trace_event(event_token)

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
            event_token = begin_llm_trace_event()
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
                    iteration=resolved_iter,
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
            finally:
                end_llm_trace_event(event_token)

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
    ("tool_result_dedup_processor_config", "ToolResultDedupProcessor"),
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


def _config_section(cfg: dict[str, Any], key: str) -> Any:
    if key in cfg:
        return cfg.get(key)
    context_engine_cfg = cfg.get("context_engine_config")
    if isinstance(context_engine_cfg, dict) and key in context_engine_cfg:
        return context_engine_cfg.get(key)
    return None


def _validate_qa_artifact_thresholds_if_enabled(
    react_cfg: dict[str, Any],
    qa_artifact_cfg: QAArtifactConfig | None,
    session_memory: SessionMemoryConfig | None,
) -> None:
    if qa_artifact_cfg is None or not qa_artifact_cfg.enabled:
        return
    context_engine_cfg = react_cfg.get("context_engine_config")
    if not isinstance(context_engine_cfg, dict):
        context_engine_cfg = react_cfg
    session_memory_trigger = (
        session_memory.trigger_tokens
        if session_memory is not None
        else SessionMemoryConfig().trigger_tokens
    )
    full_compact_raw = context_engine_cfg.get("full_compact_processor_config") or {}
    full_compact_trigger = int(
        full_compact_raw.get(
            "trigger_total_tokens",
            FullCompactProcessorConfig().trigger_total_tokens,
        )
    )
    validate_qa_artifact_thresholds(
        qa_artifact_cfg,
        session_memory_trigger_tokens=session_memory_trigger,
        full_compact_trigger_total_tokens=full_compact_trigger,
    )


def _resolve_qa_artifact_config(
    react_cfg: dict[str, Any],
    session_memory: SessionMemoryConfig | None,
) -> QAArtifactConfig | None:
    qa_block = _config_section(react_cfg, "qa_block")
    if isinstance(qa_block, dict) and qa_block.get("enabled") is False:
        return None
    raw = _config_section(react_cfg, "qa_artifact")
    if raw is False:
        return None
    if isinstance(raw, dict) and raw.get("enabled") is True and session_memory is None:
        logger.warning(
            "[JiuWenClawDeepAdapter] qa_artifact.enabled=true but session_memory is disabled "
            "(chain A); qa_artifact will not activate. Enable chain B (session_memory) or set "
            "qa_artifact.enabled: false."
        )
    if raw is None:
        if session_memory is None:
            return None
        return QAArtifactConfig()
    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return None
        cfg = QAArtifactConfig.model_validate(raw)
        _validate_qa_artifact_thresholds_if_enabled(react_cfg, cfg, session_memory)
        return cfg
    cfg = QAArtifactConfig()
    _validate_qa_artifact_thresholds_if_enabled(react_cfg, cfg, session_memory)
    return cfg


def react_config_for_subagent(react_config: dict[str, Any] | None) -> dict[str, Any]:
    """Disable QA block / qa_artifact for spawn/fork subagents (plan scope: main session only)."""

    def _set_disabled(section: dict[str, Any], key: str) -> None:
        raw = section.get(key)
        if raw is False:
            return
        if isinstance(raw, dict):
            section[key] = {**raw, "enabled": False}
        else:
            section[key] = {"enabled": False}

    base = react_config if isinstance(react_config, dict) else {}
    cfg = copy.deepcopy(base)
    _set_disabled(cfg, "qa_block")
    _set_disabled(cfg, "qa_artifact")
    context_engine_cfg = cfg.get("context_engine_config")
    if not isinstance(context_engine_cfg, dict):
        context_engine_cfg = {}
    _set_disabled(context_engine_cfg, "qa_block")
    _set_disabled(context_engine_cfg, "qa_artifact")
    cfg["context_engine_config"] = context_engine_cfg
    return cfg


def _resolve_qa_block_config(config: dict[str, Any]) -> QABlockConfig | None:
    raw = _config_section(config, "qa_block")
    if raw is False:
        return None
    if raw is None:
        return QABlockConfig()
    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return None
        return QABlockConfig.model_validate(raw)
    return QABlockConfig()


# Once-per-process flag: avoid spamming when agent_ras module is missing.
_AGENT_RAS_UNAVAILABLE_WARNED = False


def _agent_ras_kwargs_from_config(config_base: dict[str, Any] | None) -> dict[str, Any]:
    """Pass through optional YAML ``agent_ras`` overrides to create_deep_agent.

    Missing section means core defaults (Agent RAS enabled). Explicit
    ``enabled: false`` disables it. Invalid keys / types fail here with a
    clear error (same ``AgentRASConfig`` schema as agent-core) instead of
    surfacing deep inside ``create_deep_agent``.

    When ``openjiuwen.harness.agent_ras`` is unavailable, skip passthrough
    with a warning so the adapter still starts on older openjiuwen builds.
    """
    global _AGENT_RAS_UNAVAILABLE_WARNED
    base = config_base or {}
    raw = base.get("agent_ras")
    if AgentRASConfig is None:
        if raw is not None:
            logger.warning(
                "[JiuWenClawDeepAdapter] openjiuwen.harness.agent_ras "
                "unavailable; ignoring YAML agent_ras and disabling "
                "Agent RAS config passthrough"
            )
        elif not _AGENT_RAS_UNAVAILABLE_WARNED:
            logger.warning(
                "[JiuWenClawDeepAdapter] openjiuwen.harness.agent_ras "
                "unavailable; Agent RAS config passthrough disabled"
            )
            _AGENT_RAS_UNAVAILABLE_WARNED = True
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "[JiuWenClawDeepAdapter] ignoring invalid agent_ras section: "
            "expected dict, got %s",
            type(raw).__name__,
        )
        return {}
    payload = copy.deepcopy(raw)
    payload.setdefault("enabled", True)
    try:
        validated = AgentRASConfig.model_validate(payload)
    except ValidationError as exc:
        logger.error(
            "[JiuWenClawDeepAdapter] invalid agent_ras config: %s",
            exc,
        )
        raise ValueError(
            f"invalid agent_ras config: {exc}"
        ) from exc
    return {"agent_ras": validated.model_dump(mode="python")}


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
                qa_artifact_cfg = _resolve_qa_artifact_config(config, session_memory)
                for yaml_key, processor_name in _CHAIN_B_OPTIONAL_PROCESSORS:
                    proc_cfg = dict(context_engine_cfg.get(yaml_key) or {})
                    if (
                        qa_artifact_cfg is not None
                        and qa_artifact_cfg.enabled
                        and processor_name == "FullCompactProcessor"
                    ):
                        proc_cfg["qa_artifact"] = qa_artifact_cfg.model_dump(mode="json")
                    if proc_cfg:
                        user_processors.append((processor_name, proc_cfg))
                if (
                    qa_artifact_cfg is not None
                    and qa_artifact_cfg.enabled
                    and not any(name == "FullCompactProcessor" for name, _ in user_processors)
                ):
                    user_processors.append(
                        ("FullCompactProcessor", {"qa_artifact": qa_artifact_cfg.model_dump(mode="json")})
                    )
                    logger.info(
                        "[JiuWenClawDeepAdapter] qa_artifact enabled: auto-append FullCompactProcessor "
                        "as compact safety net (full_compact_processor_config in yaml is optional)",
                        extra={"user_visible": "progress"},
                    )

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
                extra={'user_visible': 'progress'}
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
                extra={'user_visible': 'progress'}
            )
        return context_rail
    except Exception as exc:
        logger.warning("[JiuWenClawDeepAdapter] ContextEngineeringRail create failed: %s", exc,
                       extra={'user_visible': 'progress'})
        return None


class _RuntimeCronToolContext:
    """Stable cron tool context proxy backed by per-task contextvars."""

    def __init__(self, tool_scope: str) -> None:
        self._tool_scope = tool_scope

    @property
    def channel_id(self) -> str:
        return _CRON_TOOL_CHANNEL_ID.get()

    @property
    def session_id(self) -> str | None:
        return _CRON_TOOL_SESSION_ID.get()

    @property
    def metadata(self) -> dict[str, Any] | None:
        return _CRON_TOOL_METADATA.get()

    @property
    def mode(self) -> str | None:
        return _CRON_TOOL_MODE.get()

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

    def __init__(
        self,
        workspace_dir: str | None = None,
        agent_id: str | None = None,
        service_id: str | None = None,
        *,
        env_agent_id: str | None = None,
        env_service_id: str | None = None,
    ) -> None:
        _apply_llm_io_trace_patch()
        self._instance: DeepAgent | None = None
        self._workspace_dir: str = workspace_dir or str(get_agent_workspace_dir())
        self._agent_name: str = "main_agent"
        self._agent_id = agent_id
        self._service_id = service_id
        self._env_agent_id = env_agent_id if env_agent_id is not None else agent_id
        self._env_service_id = (
            env_service_id if env_service_id is not None else service_id
        )
        self._vision_tools_registered: bool = False
        self._audio_tools_registered: bool = False
        self._video_tool_registered: bool = False
        self._image_gen_tool_registered: bool = False
        self._model: Model | None = None
        self._default_model: Model | None = None
        self._model_client_config: ModelClientConfig | None = None
        self._model_request_config: ModelRequestConfig | None = None
        self._config_cache: dict[str, Any] = {}
        self._latest_config_base: dict[str, Any] | None = None
        self._last_sync_env: dict[str, Any] | None = None
        self._filesystem_rail: FileSystemRail | None = None
        self._skill_rail: JiuWenSkillUseRail | None = None
        self._qualified_memory_tool_ids: list[str] = []
        self._qualified_runtime_tool_ids: list[str] = []
        self._stream_event_rail: JiuClawStreamEventRail | None = None
        self._telemetry_rail: Any | None = None
        self._request_summary_rail: Any | None = None
        self._context_overflow_recovery_rail: ContextOverflowRecoveryRail | None = None
        self._task_execution_rail: TaskExecutionRail | None = None
        self._task_planning_rail: TaskPlanningRail | None = None
        self._context_engineering_rail: ContextEngineeringRail | None = None
        self._context_engineering_rail_mode: str | None = None
        self._runtime_prompt_rail: RuntimePromptRail | None = None
        self._response_prompt_rail: ResponsePromptRail | None = None
        self._skill_protocol_prompt_rail: SkillProtocolPromptRail | None = None
        self._skill_compliance_rail: SkillComplianceRail | None = None
        self._security_rail: SecurityRail | None = None
        self._memory_rail: MemoryRail | None = None
        self._external_memory_rail: Any = None
        self._external_memory_rail_registered: bool = False
        self._external_memory_fingerprint: str | None = None
        self._lsp_rail: LspRail | None = None
        self._coding_memory_rail: CodingMemoryRail | None = None
        self._project_memory_rail: Any | None = None
        self._code_mode_rails: list[Any] = []
        self._code_mode_rails_active = False
        self._code_mode_workspace: str | None = None
        self._heartbeat_rail: HeartbeatRail | None = None
        self._skill_evolution_rail: JiuClawSkillEvolutionRail | None = None
        # Retain fire-and-forget follow-up tasks so they are not GC'd mid-run.
        self._pending_follow_ups: set[asyncio.Task] = set()
        self._subagent_rail: SubagentRail | None = None
        self._disabled_tools_rail: DisabledToolsRail | None = None
        self._checkpointer: Any | None = None
        self._progressive_tool_rail: JiuWenProgressiveToolRail | None = None
        self._qa_block_freeze_rail: JiuClawQABlockFreezeRail | None = None
        self._qa_block_assembly_rail: JiuClawQABlockAssemblyRail | None = None
        self._qa_artifact_rail: JiuClawQAArtifactRail | None = None
        self._permission_rail: Any = None
        self._skill_credential_injection_rail: SkillCredentialInjectionRail | None = None
        self._avatar_rail: Any = None
        self._tool_cards = None
        self._sys_operation = None
        self._sandbox_fingerprint: tuple[Any, ...] | None = None
        self._vision_model_config: VisionModelConfig | None = None
        self._audio_model_config: AudioModelConfig | None = None
        self._video_model_config: bool = False
        self._image_gen_enabled: bool = False
        self._image_gen_tools: list[Any] = []
        self._vision_tools: list[Any] = []
        self._audio_tools: list[Any] = []
        self._instance_overrides: dict[str, Any] = {}
        self._session_id: str | None = None
        self._fallback_card_suffix: str | None = None
        self._xiaoyi_phone_tools_registered: bool = False
        self._skill_manager: SkillManager | None = None
        self._cron_runtime = CronRuntimeBridge()
        self._runtime_cron_tool_context = _RuntimeCronToolContext(
            tool_scope=f"runtime_{id(self):x}",
        )
        self._is_proactive_memory: bool | None = None
        self._model_cache: dict[str, Model] = {}
        self._tier_model_cache: dict[str, Model] = {}
        self._default_model_name: str = ""
        self._multi_session_toolkit: MultiSessionToolkit | None = None
        self._fork_agent_executor: Any = None
        # request_id -> toolkit；session_id -> 关联的 request_id 集合（interrupt 时按会话精确取消）
        self._request_session_toolkits: dict[str, MultiSessionToolkit] = {}
        self._session_toolkit_requests: dict[str, set[str]] = {}
        # Skills that auto_save evolution just persisted; drained for auto rebuild.
        self._pending_auto_rebuild_skills: list[str] = []
        # Fire-and-forget: wait for rail evolution then auto rebuild.
        self._pending_evolution_followup_tasks: set[asyncio.Task[Any]] = set()
        self._pending_reload: tuple[
            dict[str, Any] | None, dict[str, Any] | None, bool
        ] | None = None
        self._reload_lock = asyncio.Lock()
        self._embed_fingerprint: tuple[Any, ...] | None = None
        self._memory_cache_fingerprint: str | None = None
        self._task_memory_fingerprint: Any | None = None
        self._registered_skill_dirs: list[str] = []
        self._memory_engine_snapshot: str | None = None
        self._context_engine_config_fp: str | None = None
        self._working_checker: Callable[[], bool] | None = None
        self._last_runtime_mode: str = "agent.plan"
        set_skill_credential_provider(
            lambda: (
                self._skill_credential_injection_rail.get_skill_envs()
                if self._skill_credential_injection_rail is not None
                else {}
            )
        )

    def set_working_checker(self, checker: Callable[[], bool] | None) -> None:
        """Inject callable returning whether this session has in-flight work."""
        self._working_checker = checker

    def get_agent_instance(self) -> Any:
        """Return the underlying DeepAgent instance, if initialized."""
        return self._instance

    def get_memory_cache_fingerprint(self) -> str | None:
        """Return the bound memory cache fingerprint for this adapter session."""
        return self._memory_cache_fingerprint

    def _adapter_is_working(self) -> bool:
        if self._working_checker is not None:
            try:
                return bool(self._working_checker())
            except Exception:
                return False
        return False

    @staticmethod
    def _embed_config_fingerprint(config: dict[str, Any]) -> tuple[Any, ...]:
        return embed_config_fingerprint(config)

    @staticmethod
    def _sandbox_config_fingerprint() -> tuple[Any, ...]:
        """计算 sandbox 配置指纹（enabled/url/type），用于 reload 时判断是否需要重建 _sys_operation。

        与 ``_create_sys_operation`` 读取同一对 helper（``get_sandbox_endpoint`` /
        ``get_sandbox_runtime``），确保指纹变更等价于 sysop 路由变更。
        """
        endpoint = get_sandbox_endpoint()
        runtime = get_sandbox_runtime()
        return (
            bool(runtime.get("enabled")),
            endpoint.get("url") or "",
            endpoint.get("type") or "",
        )

    @staticmethod
    def _context_engine_config_fingerprint(config: dict[str, Any]) -> str:
        react = config.get("react") if isinstance(config, dict) else {}
        if not isinstance(react, dict):
            return ""
        ce = react.get("context_engine_config")
        try:
            return json.dumps(ce, sort_keys=True, default=str)
        except Exception:
            return str(ce)

    @staticmethod
    def _env_touches_memory(env_overrides: dict[str, Any] | None) -> bool:
        return env_touches_memory(env_overrides)

    def _sync_registered_skill_dirs_snapshot(self) -> None:
        """Capture effective shared skill dirs at create/reload apply (overlay-aware)."""
        self._registered_skill_dirs = [
            str(p) for p in resolve_agent_registered_skill_dirs()
        ]

    def _apply_registered_skill_dirs_to_runtime_rails(self) -> None:
        if self._runtime_prompt_rail is not None:
            self._runtime_prompt_rail.set_registered_skill_dirs(
                list(self._registered_skill_dirs)
            )

    def _registered_skill_dirs_for_rail(self) -> list[str]:
        if not self._registered_skill_dirs:
            self._sync_registered_skill_dirs_snapshot()
        return list(self._registered_skill_dirs)

    def set_skill_manager(self, skill_manager: SkillManager) -> None:
        """Inject shared SkillManager from facade for tool reuse."""
        self._skill_manager = skill_manager

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

    def _resolve_agent_card_id(self, session_id: str | None = None) -> str:
        """Return per-session AgentCard.id to isolate outer rail callback namespaces."""
        had_suffix = self._fallback_card_suffix is not None
        card_id, suffix = _resolve_agent_card_id_pure(
            session_id,
            cached_session_id=self._session_id,
            fallback_card_suffix=self._fallback_card_suffix,
        )
        raw_session = (session_id or self._session_id or "").strip()
        if suffix is not None and not had_suffix:
            self._fallback_card_suffix = suffix
            session_label = raw_session or "(empty)"
            logger.info(
                "[JiuWenClawDeepAdapter] AgentCard.id: allocated uuid suffix for "
                "shared session_id=%r -> card_key=%s_%s",
                session_label,
                DEFAULT_SESSION_ID,
                self._fallback_card_suffix,
            )
        if is_default_session(raw_session):
            session_label = raw_session or "(empty)"
            logger.debug(
                "[JiuWenClawDeepAdapter] AgentCard.id resolved: session_id=%r "
                "agent_card_id=%s (uuid-suffix isolation)",
                session_label,
                card_id,
            )
        return card_id

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
        cfg = self._model_request_config
        if cfg is None:
            return "unknown"
        # ModelRequestConfig field is ``model_name`` (alias ``model``); do not use
        # ``.model`` — that can confuse with pydantic's ``model_config``.
        name = getattr(cfg, "model_name", None) or getattr(cfg, "model", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return "unknown"

    @staticmethod
    def _is_placeholder_model_name(name: str) -> bool:
        """True for empty / unresolved / template placeholder model ids."""
        text = str(name or "").strip()
        if not text or text == "unknown":
            return True
        # jiuwenclaw resources/.env.template leaves MODEL_NAME=your-model-name
        return text.startswith("your-") and text.endswith("-name")

    def _resolve_evolution_model_name(self, config: dict[str, Any] | None = None) -> str:
        """Prefer the live main-chat model over static config placeholders.

        SkillEvolutionRail previously used ``config['model_name']`` which often
        stays as the template value ``your-model-name`` while tip/sync env already
        drives the real chat model (e.g. glm-5.2).
        """
        cfg = config if isinstance(config, dict) else (self._config_cache or {})
        candidates = [
            self._resolve_model_name(),
            str(self._default_model_name or "").strip(),
            str((self._last_sync_env or {}).get("MODEL_NAME") or "").strip(),
            str(cfg.get("model_name") or "").strip(),
        ]
        for name in candidates:
            if name and not self._is_placeholder_model_name(name):
                return name
        logger.warning(
            "[JiuWenClawDeepAdapter] evolution model unresolved; candidates=%s",
            candidates,
        )
        return "gpt-4"

    def _make_rebuild_service(self, store: Any) -> Any:
        """Build ExperienceRebuildService with current LLM for changelog classification."""
        if ExperienceRebuildService is None:
            raise RuntimeError("ExperienceRebuildService is unavailable")
        model_name = self._resolve_evolution_model_name()
        if self._is_placeholder_model_name(model_name):
            logger.warning(
                "[JiuWenClawDeepAdapter] model name unresolved, falling back to '%s' "
                "for changelog classification",
                model_name,
            )
        return ExperienceRebuildService(
            store=store,
            llm=self._model,
            model=model_name,
            language=self._resolve_runtime_language(),
        )

    @staticmethod
    def _browser_runtime_enabled() -> bool:
        """Whether browser runtime support is enabled for DeepAgent subagent wiring.

        Always False — plan/chat keep browser subagent closed. Team mode manages
        browser runtime independently via swarm subagent config.
        """
        # close browser subagent for plan/chat (pre-team behavior)
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

    def _get_reusable_coding_memory_rail(
        self,
        config_base: dict[str, Any] | None,
    ) -> CodingMemoryRail | None:
        """Return CodingMemoryRail only when its mode/config/workspace still match."""
        rail = self._coding_memory_rail
        if (
            rail is None
            or not getattr(self, "_code_mode_rails_active", False)
            or not _code_memory_enabled(config_base or {})
        ):
            return None
        try:
            current_workspace = str(Path(self._workspace_dir or "./").resolve())
            bound_workspace = getattr(self, "_code_mode_workspace", None) or current_workspace
            coding_dir = getattr(rail, "_coding_memory_dir", None)
            if str(Path(bound_workspace).resolve()) != current_workspace:
                return None
            if coding_dir is not None:
                expected_dir = Path(current_workspace) / "coding_memory"
                if Path(coding_dir).resolve() != expected_dir:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] discard CodingMemoryRail from "
                        "different workspace"
                    )
                    return None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return rail

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
                reusable_memory_rail = self._get_reusable_coding_memory_rail(config_base)
                if reusable_memory_rail is not None:
                    # Code mode builds the main rail before subagents and
                    # reuses it to avoid duplicate indexes and directories.
                    code_agent_rails = [FileSystemRail(), reusable_memory_rail]
                elif (
                    self._code_mode_rails_active
                    and _code_memory_enabled(config_base or {})
                    and get_memory_mode(get_config()) == "local"
                ):
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
                from jiuwenclaw.agentserver.tools.web_search.content_cache import (
                    get_agent_cache_registry,
                )

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
                            cache=get_agent_cache_registry().get_cache_sync(
                                "research_agent",
                            ),
                        ),
                    )
                )

        browser_agent_cfg = subagents_cfg.get("browser_agent") if isinstance(subagents_cfg, dict) else {}
        browser_enabled = self._browser_runtime_enabled()
        if browser_enabled:
            if not str(get_local_config("BROWSER_DRIVER", "") or "").strip():
                set_os_environ("BROWSER_DRIVER", "managed")
                logger.info(
                    "[JiuWenClawDeepAdapter] browser subagent enabled without BROWSER_DRIVER; "
                    "defaulting to managed mode"
                )
            if not str(get_local_config("BROWSER_MANAGED_BINARY", "") or "").strip():
                chrome_path = self._resolve_managed_browser_binary_from_config()
                if chrome_path:
                    set_os_environ("BROWSER_MANAGED_BINARY", chrome_path)
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
        sid, aid = self._env_ns_ids()
        if not dedicated_multimodal_model_configured(
            config_base,
            "vision",
            service_id=sid,
            agent_id=aid,
        ):
            logger.info(
                "[JiuWenClawDeepAdapter] vision tools skipped: models.vision has no dedicated "
                "api_key in config.yaml"
            )
            return None
        apply_vision_model_config_from_yaml(config_base)
        api_key = str(read_env("VISION_API_KEY")).strip()
        base_url = str(
            read_env("VISION_BASE_URL")
            or read_env("VISION_API_BASE")
        ).strip()
        model_name = str(
            read_env("VISION_MODEL")
            or read_env("VISION_MODEL_NAME")
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
            max_retries=_parse_int(read_env("VISION_MAX_RETRIES", "3"), 3),
        )

    def _build_audio_model_config(
            self,
            config_base: dict[str, Any],
    ) -> AudioModelConfig | None:
        """Build DeepAgent audio config from service config/env mapping."""
        sid, aid = self._env_ns_ids()
        if not dedicated_multimodal_model_configured(
            config_base,
            "audio",
            service_id=sid,
            agent_id=aid,
        ):
            logger.info(
                "[JiuWenClawDeepAdapter] skip full audio LLM config: models.audio has no "
                "dedicated api_key in config.yaml"
            )
            return None
        apply_audio_model_config_from_yaml(config_base)
        api_key = str(read_env("AUDIO_API_KEY")).strip()
        base_url = str(
            read_env("AUDIO_BASE_URL")
            or read_env("AUDIO_API_BASE")
        ).strip()
        if not api_key or not base_url:
            logger.info(
                "[JiuWenClawDeepAdapter] audio tools skipped: incomplete config"
            )
            return None
        transcription_model = str(
            read_env("AUDIO_TRANSCRIPTION_MODEL")
            or read_env("AUDIO_MODEL_NAME")
        ).strip()
        question_answering_model = str(
            read_env("AUDIO_QUESTION_ANSWERING_MODEL")
            or read_env("AUDIO_MODEL_NAME")
        ).strip()
        config_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": _parse_int(read_env("AUDIO_MAX_RETRIES", "3"), 3),
            "http_timeout": _parse_int(read_env("AUDIO_HTTP_TIMEOUT", "20"), 20),
            "max_audio_bytes": _parse_int(
                read_env("AUDIO_MAX_AUDIO_BYTES", str(25 * 1024 * 1024)),
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
            config_kwargs[
                "question_answering_model"
            ] = question_answering_model
        return AudioModelConfig(**config_kwargs)

    def _build_video_model_config(
            self,
            config_base: dict[str, Any],
    ) -> bool:
        """Build DeepAgent video config from service config/env mapping."""
        sid, aid = self._env_ns_ids()
        if not dedicated_multimodal_model_configured(
            config_base,
            "video",
            service_id=sid,
            agent_id=aid,
        ):
            logger.info(
                "[JiuWenClawDeepAdapter] skip video_understanding: models.video has no "
                "dedicated api_key in config.yaml"
            )
            return False
        apply_video_model_config_from_yaml(config_base)
        if not read_env("VIDEO_API_KEY"):
            logger.info(
                "[JiuWenClawDeepAdapter] video tools skipped: incomplete config"
            )
            return False
        return True

    def _build_image_gen_enabled(self, config_base: dict[str, Any]) -> bool:
        """Whether text_to_image should be registered for this runtime."""
        sid, aid = self._env_ns_ids()
        if not dedicated_multimodal_model_configured(
            config_base,
            "image_gen",
            service_id=sid,
            agent_id=aid,
        ):
            logger.info(
                "[JiuWenClawDeepAdapter] skip text_to_image: models.image_gen has no "
                "dedicated api_key in config.yaml"
            )
            return False
        apply_image_gen_model_config_from_yaml(config_base)
        api_key = str(read_env("IMAGE_GEN_API_KEY")).strip()
        api_base = str(read_env("IMAGE_GEN_API_BASE")).strip()
        model_name = str(read_env("IMAGE_GEN_MODEL_NAME")).strip()
        if not api_key or not api_base or not model_name:
            logger.info(
                "[JiuWenClawDeepAdapter] text_to_image skipped: incomplete config"
            )
            return False
        return True

    def _iter_runtime_audio_tools(self, agent_id: str | None) -> list[Any]:
        """可注册的音频工具：须先在 config 中为 ``models.audio`` 配置独立 ``api_key``。

        与 vision / video 一致，无该 key 时不挂载任何音频工具（含 ``audio_metadata``）。
        已配置 key 且 ``_audio_model_config`` 完整时注册全部 harness 音频工具；否则仅保留
        ``audio_metadata``（ACRCloud，仍依赖 ``ACR_*`` 环境变量在运行时识别曲库）。
        """
        config_base = self._latest_config_base if isinstance(self._latest_config_base, dict) else get_config()
        sid, aid = self._env_ns_ids()
        if not dedicated_multimodal_model_configured(
            config_base,
            "audio",
            service_id=sid,
            agent_id=aid,
        ):
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
        sid, aid = self._env_ns_ids()
        for group in MULTIMODAL_ENV_GROUP_KEYS:
            if dedicated_multimodal_model_configured(
                config_base,
                group,
                service_id=sid,
                agent_id=aid,
            ):
                continue
            if multimodal_env_anchor_present(group):
                continue
            clear_multimodal_env_groups(
                [group],
                service_id=sid,
                agent_id=aid,
            )

        self._vision_model_config = self._build_vision_model_config(config_base)
        self._audio_model_config = self._build_audio_model_config(config_base)
        self._video_model_config = self._build_video_model_config(config_base)
        self._image_gen_enabled = self._build_image_gen_enabled(config_base)

        for tool in self._vision_tools:
            tool.vision_model_config = self._vision_model_config
        for tool in self._audio_tools:
            tool.audio_model_config = self._audio_model_config

    def _track_qualified_runtime_tool_id(self, tool_id: str) -> None:
        if tool_id and tool_id not in self._qualified_runtime_tool_ids:
            self._qualified_runtime_tool_ids.append(tool_id)

    def _untrack_qualified_runtime_tool_id(self, tool_id: str) -> None:
        try:
            self._qualified_runtime_tool_ids.remove(tool_id)
        except ValueError:
            pass

    @staticmethod
    def _tools_in_resource_mgr(tools: list[Any]) -> bool:
        if not tools:
            return False
        for tool in tools:
            tool_id = str(getattr(getattr(tool, "card", None), "id", "") or "")
            if not tool_id or Runner.resource_mgr.get_tool(tool_id) is None:
                return False
        return True

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
        """Sync tools registered before DeepAgent existed into ability_manager."""
        if self._instance is None:
            return
        agent_card_id = self._resolve_agent_card_id()
        for tools in (
            list(self._vision_tools or []),
            list(self._audio_tools or []),
            list(self._image_gen_tools or []),
        ):
            if not tools:
                continue
            if not self._tools_in_resource_mgr(tools):
                continue
            if self._tools_in_ability_manager(tools):
                continue
            self._register_runtime_tools(tools, agent_card_id)

    def _cleanup_qualified_runtime_tools(self) -> None:
        for tool_id in list(self._qualified_runtime_tool_ids):
            remove_tool_from_resource_mgr(tool_id)
        self._qualified_runtime_tool_ids.clear()

    @staticmethod
    def _cleanup_circuit_breaker_session(session_id: str | None = None) -> None:
        """No-op: CircuitBreakerRail removed; Agent RAS owns session lifecycle.

        Kept during rolling upgrade so leftover call sites / ops hooks do not
        AttributeError. Safe to delete after one release cycle.
        """
        _ = session_id
        return

    def _register_runtime_tools(
            self,
            tools: list[Any],
            agent_card_id: str,
    ) -> bool:
        """Register session-qualified tools into resource_mgr (and ability_manager when ready)."""
        if not tools:
            return False
        if self._instance is None:
            registered_any = False
            for tool in tools:
                try:
                    _, qualified_id = reregister_qualified_tool_in_resource_mgr(
                        tool,
                        agent_card_id,
                    )
                    self._track_qualified_runtime_tool_id(qualified_id)
                    self._append_tool_card(tool.card)
                    registered_any = True
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] resource_mgr tool register failed: %s",
                        exc,
                    )
            return registered_any
        try:
            register_qualified_tools(self._instance, tools, agent_card_id)
            for tool in tools:
                self._track_qualified_runtime_tool_id(str(tool.card.id))
                self._append_tool_card(tool.card)
            return True
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] runtime tool register failed: %s",
                exc,
            )
            return False

    def _sync_tool_group(
            self,
            *,
            current_tools: list[Any],
            registered: bool,
            enabled: bool,
            create_fn: Callable[[], list[Any]],
            warn_label: str,
            agent_card_id: str | None = None,
    ) -> tuple[list[Any], bool]:
        """统一处理一组工具的热更新：启用时注册，禁用时移除。

        Returns:
            (updated_tools, updated_registered)
        """
        card_id = agent_card_id or self._resolve_agent_card_id()
        if not enabled:
            if registered:
                logger.info(
                    "[JiuWenClawDeepAdapter] multimodal tool group disabled, removing %s",
                    warn_label,
                )
                self._remove_registered_tools(current_tools)
                self._prune_tool_cards({t.card.name for t in current_tools})
            return [], False
        if registered and current_tools:
            if not self._tools_in_resource_mgr(current_tools):
                registered = False
            elif (
                self._instance is not None
                and not self._tools_in_ability_manager(current_tools)
            ):
                try:
                    ok = self._register_runtime_tools(current_tools, card_id)
                    return current_tools, ok and bool(current_tools)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] %s ability sync failed: %s",
                        warn_label,
                        exc,
                    )
                    registered = False
            else:
                return current_tools, registered
        if not registered:
            try:
                new_tools = create_fn()
                ok = self._register_runtime_tools(new_tools, card_id)
                return new_tools, ok and bool(new_tools)
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] %s reload failed: %s", warn_label, exc
                )
                return [], False
        return current_tools, registered

    def _remove_registered_tools(self, tools: list[Any]) -> None:
        """Remove tool instances from ability manager and resource manager."""
        if not tools:
            return
        for tool in tools:
            tool_id = str(tool.card.id)
            remove_tool_from_resource_mgr(tool_id)
            self._untrack_qualified_runtime_tool_id(tool_id)
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
        agent_card_id = self._resolve_agent_card_id()
        audio_tools_enabled = bool(self._iter_runtime_audio_tools(agent_card_id))
        self._vision_tools, self._vision_tools_registered = self._sync_tool_group(
            current_tools=self._vision_tools,
            registered=self._vision_tools_registered,
            enabled=self._vision_model_config is not None,
            create_fn=lambda: create_vision_tools(
                language=self._resolve_runtime_language(),
                vision_model_config=self._vision_model_config,
                agent_id=agent_card_id,
            ),
            warn_label="vision tools",
            agent_card_id=agent_card_id,
        )

        self._audio_tools, self._audio_tools_registered = self._sync_tool_group(
            current_tools=self._audio_tools,
            registered=self._audio_tools_registered,
            enabled=audio_tools_enabled,
            create_fn=lambda: self._iter_runtime_audio_tools(agent_card_id),
            warn_label="audio tools",
            agent_card_id=agent_card_id,
        )

        _, self._video_tool_registered = self._sync_tool_group(
            current_tools=[video_understanding],
            registered=self._video_tool_registered,
            enabled=bool(self._video_model_config),
            create_fn=lambda: [video_understanding],
            warn_label="video tool",
            agent_card_id=agent_card_id,
        )

        self._image_gen_tools = getattr(self, "_image_gen_tools", []) or []
        self._image_gen_tools, self._image_gen_tool_registered = self._sync_tool_group(
            current_tools=self._image_gen_tools,
            registered=self._image_gen_tool_registered,
            enabled=bool(self._image_gen_enabled),
            create_fn=lambda: [create_session_text_to_image_tool(agent_card_id)],
            warn_label="text_to_image tool",
            agent_card_id=agent_card_id,
        )

    def _tenant_disk_ids(self) -> tuple[str, str]:
        """Return ``(service_id, agent_id)`` for on-disk tenant paths.

        Prefer request-side ``env_*`` ids so ``AGENT_RUNTIME`` rewrite of
        ``self._agent_id`` (e.g. ``office_default``) does not divert checkpoint
        / prompt paths away from ``agent_office``.
        """
        from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool

        service_id = TenantAgentPool.normalize_tenant_id(
            self._env_service_id if self._env_service_id is not None else self._service_id
        )
        agent_id = TenantAgentPool.normalize_tenant_id(
            self._env_agent_id if self._env_agent_id is not None else self._agent_id
        )
        return service_id, agent_id

    async def set_checkpoint(self):
        try:
            from jiuwenclaw.utils import get_multi_tenant_user_workspace_dir

            PersistenceCheckpointerProvider()
            service_id, agent_id = self._tenant_disk_ids()
            workspace = get_multi_tenant_user_workspace_dir(service_id, agent_id)
            if workspace is None:
                raise ValueError(
                    f"invalid tenant for checkpoint: service_id={service_id!r}, agent_id={agent_id!r}"
                )
            checkpoint_path = workspace / ".checkpoint"
            checkpoint_path.mkdir(parents=True, exist_ok=True)
            conf = {"db_type": "sqlite", "db_path": f"{checkpoint_path}/checkpoint"}

            db_type = os.getenv("GATEWAY_DB_TYPE", "").strip().lower()
            if db_type == "mysql":
                mysql_engine = await _build_mysql_async_engine()
                if mysql_engine is not None:
                    conf["db_client"] = mysql_engine
                    logger.info("[JiuWenClawDeepAdapter] use mysql db_client")
            elif db_type == "postgresql":
                postgresql_engine = await _build_postgresql_async_engine()
                if postgresql_engine is not None:
                    conf["db_client"] = postgresql_engine
                    logger.info("[JiuWenClawDeepAdapter] use postgresql db_client")
            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf=conf)
            )
            self._checkpointer = checkpointer
        except Exception as e:
            logger.error("[JiuWenClawDeepAdapter] fail to setup checkpoint due to: %s", e,
                         extra={'user_visible': 'critical'})

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
        name = str(mcc.get("model_name") or "").strip()

        # 如果 api_key 为空，尝试从环境变量获取或使用默认占位值
        if not mcc.get("api_key"):
            env_api_key = read_env("API_KEY").strip()
            if env_api_key:
                mcc["api_key"] = env_api_key
                logger.info(
                    "[_build_model_from_entry] 从环境变量 API_KEY 获取到 api_key: model_name=%s",
                    name,
                )
            else:
                mcc["api_key"] = "placeholder-api-key"
                logger.warning(
                    "[_build_model_from_entry] api_key 为空且环境变量未设置，使用占位值: model_name=%s",
                    name,
                )

        # 与 api_key 对称：yaml ${VAR} 在 sealed overlay 下可能解析为空
        if not str(mcc.get("client_provider") or "").strip():
            env_provider = read_env("MODEL_PROVIDER").strip()
            if env_provider:
                mcc["client_provider"] = env_provider
                logger.info(
                    "[_build_model_from_entry] 从环境变量 MODEL_PROVIDER 获取到 client_provider: %s model_name=%s",
                    env_provider,
                    name,
                )
            else:
                mcc["client_provider"] = "OpenAI"
                logger.warning(
                    "[_build_model_from_entry] client_provider 为空且 MODEL_PROVIDER 未设置，回退 OpenAI: model_name=%s",
                    name,
                )

        if not str(mcc.get("api_base") or "").strip():
            env_api_base = read_env("API_BASE").strip()
            if env_api_base:
                mcc["api_base"] = env_api_base
                logger.info(
                    "[_build_model_from_entry] 从环境变量 API_BASE 获取到 api_base: model_name=%s",
                    name,
                )
            else:
                # Tip-only: no bare os.environ fallthrough for API_BASE.
                logger.warning(
                    "[_build_model_from_entry] api_base 为空且 tip/API_BASE 未设置，"
                    "不回落 os.environ: model_name=%s",
                    name,
                )

        if not name:
            env_model_name = read_env("MODEL_NAME").strip()
            if env_model_name:
                mcc["model_name"] = env_model_name
                name = env_model_name
                logger.info(
                    "[_build_model_from_entry] 从环境变量 MODEL_NAME 获取到 model_name: %s",
                    name,
                )

        if not str(mcc.get("api_base") or "").strip():
            raise ValueError(
                f"model client config api_base is required but empty "
                f"(model_name={name!r}). Set API_BASE in sync/reload env or process environment."
            )
        if mcc.get("api_key") == "placeholder-api-key":
            logger.warning(
                "[_build_model_from_entry] api_key 仍为占位值，后续调用可能鉴权失败: model_name=%s",
                name,
            )

        m_config = ModelRequestConfig(
            model=name,
            temperature=mco.get("temperature", 0.95),
        )
        mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
        return Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config)

    def _build_model_cache_from_defaults(self, config: dict) -> None:
        """从 models.defaults 列表构建模型缓存。"""
        self._tier_model_cache = {}
        for entry in get_default_models(config):
            mcc = entry.get("model_client_config") or {}
            # 将claw_config的配置传入到model的扩展字段中, 方便注册的model实例使用
            mcc["claw_config"] = config
            if not mcc.get("model_name"):
                continue
            self._model_cache[mcc["model_name"]] = self._build_model_from_entry(
                mcc, entry.get("model_config_obj") or {},
            )
            tier_raw = entry.get("tier")
            if not tier_raw:
                continue
            tier = str(tier_raw).strip().lower()
            if tier not in ("lite", "pro") or tier in self._tier_model_cache:
                continue
            self._tier_model_cache[tier] = self._model_cache[mcc["model_name"]]

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

    def _create_model(
        self,
        config: dict,
        env_overrides: dict[str, Any] | None = None,
    ) -> Model:
        config = patch_model_config_from_env(config, env_overrides)
        self._model_cache.clear()
        self._build_model_cache_from_defaults(config)
        if not self._model_cache:
            self._build_model_cache_legacy(config)

        env_model_name = ""
        if isinstance(env_overrides, dict) and "MODEL_NAME" in env_overrides:
            raw = env_overrides.get("MODEL_NAME")
            if raw is not None:
                env_model_name = str(raw).strip()
        if env_model_name and env_model_name not in self._model_cache:
            entries = get_default_models(config)
            if entries:
                base_entry = entries[0]
            else:
                # Fallback: use models.default from patched config so that
                # env_model_name always gets a cache entry even when
                # get_default_models returns empty.
                base_entry = config.get("models", {}).get("default", {})
            mcc = dict(base_entry.get("model_client_config") or {})
            mcc["model_name"] = env_model_name
            mco = base_entry.get("model_config_obj") or {}
            self._model_cache[env_model_name] = self._build_model_from_entry(mcc, mco)

        if env_model_name and env_model_name in self._model_cache:
            self._default_model_name = env_model_name
            self._model = self._model_cache[env_model_name]
        else:
            first_name = next(iter(self._model_cache))
            self._default_model_name = first_name
            self._model = self._model_cache[first_name]
        self._model_client_config = self._model.model_client_config
        self._model_request_config = self._model.model_config
        self._merge_service_model_cache()
        self._default_model = self._model
        return self._model

    def _merge_service_model_cache(self) -> None:
        """Merge service-level shared model entries into _model_cache after rebuild.

        Reads from TenantAgentPool's service model cache and builds Model instances
        via _build_model_from_entry. Skips keys already present in _model_cache.
        """
        from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool
        pool = TenantAgentPool.peek_instance()
        if pool is None:
            return
        service_cache = pool.get_service_model_cache(self._env_service_id)
        if not service_cache:
            return
        merged = 0
        for key, entry in service_cache.items():
            if key in self._model_cache:
                continue
            if not isinstance(entry, dict):
                continue
            mcc = dict(entry.get("model_client_config") or {})
            if not str(mcc.get("model_name") or "").strip():
                continue
            mco = entry.get("model_config_obj") or {}
            try:
                self._model_cache[key] = self._build_model_from_entry(mcc, mco)
                merged += 1
            except Exception:
                logger.warning(
                    "[JiuWenClawDeepAdapter] _merge_service_model_cache: "
                    "failed to build model for key=%s",
                    key, exc_info=True,
                )
        if merged:
            logger.info(
                "[JiuWenClawDeepAdapter] _merge_service_model_cache: "
                "service=%s merged=%d total=%d",
                self._env_service_id, merged, len(self._model_cache),
            )

    def _resolve_from_shared_model_cache(self, name: str) -> Model | None:
        """Resolve a model from TenantAgentPool's service-level shared cache on demand.

        If found, builds a Model and caches it in _model_cache for subsequent requests.
        Returns None if not found.
        """
        from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool
        pool = TenantAgentPool.peek_instance()
        if pool is None:
            return None
        service_cache = pool.get_service_model_cache(self._env_service_id)
        entry = service_cache.get(name)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            return None
        mcc = dict(entry.get("model_client_config") or {})
        if not str(mcc.get("model_name") or "").strip():
            return None
        mco = entry.get("model_config_obj") or {}
        try:
            model = self._build_model_from_entry(mcc, mco)
        except Exception:
            logger.warning(
                "[JiuWenClawDeepAdapter] _resolve_from_shared_model_cache: "
                "failed to build model for key=%s",
                name, exc_info=True,
            )
            return None
        self._model_cache[name] = model
        logger.info(
            "[JiuWenClawDeepAdapter] _resolve_from_shared_model_cache: "
            "lazy built model for key=%s service=%s",
            name, self._env_service_id,
        )
        return model

    def _resolve_model_for_request(self, request: AgentRequest) -> Model:
        """根据请求中的 model_name 参数查找对应模型。

        共享模型池按 alias 建索引（无 alias 时按 model_name），chat.send 传入
        的 model_name 直接匹配 alias key。未传 model_name 则回退默认模型。
        非法 model_name（非空但未命中缓存）抛出 ValueError，不静默回退。
        """
        params = request.params if isinstance(request.params, dict) else {}
        requested = str(params.get("model_name") or "").strip()
        if not requested:
            return getattr(self, "_default_model", None) or self._model
        if requested in self._model_cache:
            logger.info(
                "[JiuWenClawDeepAdapter] model resolved by name: requested=%s",
                requested,
                extra={'user_visible': 'progress'},
            )
            return self._model_cache[requested]
        # Cache miss: try service-level shared model cache (relay-claw top-level
        # ``models`` array transparently forwarded via sync_agents_configs) first.
        resolved = self._resolve_from_shared_model_cache(requested)
        if resolved is not None:
            return resolved
        # Then fall back to rebuilding from the last sync env_overrides
        # (MODEL_NAME / API_KEY / API_BASE / default_headers). This covers the
        # scenario where sync delivered real credentials but the cache wasn't
        # populated (e.g. get_default_models returned empty on first reload);
        # without it the request would fall back to a placeholder-credential
        # default model and fail with 401/404. See commit 93b1def7.
        if self._last_sync_env and self._latest_config_base:
            sync_model_name = str(self._last_sync_env.get("MODEL_NAME") or "").strip()
            if sync_model_name == requested:
                try:
                    patched = patch_model_config_from_env(
                        self._latest_config_base, self._last_sync_env)
                    entries = get_default_models(patched)
                    base_entry = entries[0] if entries else patched.get("models", {}).get("default", {})
                    mcc = dict(base_entry.get("model_client_config") or {})
                    mcc["model_name"] = requested
                    mco = base_entry.get("model_config_obj") or {}
                    rebuilt = self._build_model_from_entry(mcc, mco)
                    self._model_cache[requested] = rebuilt
                    logger.info(
                        "[JiuWenClawDeepAdapter] model resolved by name: requested=%s -> rebuilt_from_sync_env",
                        requested,
                        extra={'user_visible': 'progress'},
                    )
                    return rebuilt
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] model rebuild from sync env failed: "
                        "requested=%s error=%s",
                        requested, exc,
                    )
        # Both service cache and sync env rebuild missed — raise so the caller
        # (plan / fast / team entry points) surfaces a chat.error with
        # code=model_not_found. This avoids silently falling back to the
        # default model (which may carry placeholder credentials and trigger
        # 401/404 on the actual API call).
        available = sorted(self._model_cache.keys())
        raise ValueError(
            f"model_name {requested!r} not found in service model cache; "
            f"available: {available}"
        )

    def _resolve_model(
        self,
        *,
        model_name: str = "",
        model_tier: str = "",
    ) -> tuple[Model, str | None]:
        """按 model_name 或 model_tier 从缓存解析 Model；未指定则回退默认模型。"""
        name = (model_name or "").strip()
        if name:
            if name in self._model_cache:
                return self._model_cache[name], None
            logger.info(
                "[JiuWenClawDeepAdapter] 模型名无效 model_name=%r，尝试选用等级模型",
                model_name,
                extra={"user_visible": "progress"},
            )
        tier = (model_tier or "").strip().lower()
        if tier:
            if tier not in ("lite", "pro"):
                logger.info(
                    "[JiuWenClawDeepAdapter] 模型等级无效 model_tier=%r，回退主 Agent 默认模型",
                    model_tier,
                    extra={"user_visible": "progress"},
                )
                return self._model, None
            model = self._tier_model_cache.get(tier)
            if model is None:
                logger.info(
                    "[JiuWenClawDeepAdapter] 模型等级未配置 model_tier=%r，回退主 Agent 默认模型",
                    tier,
                    extra={"user_visible": "progress"},
                )
                return self._model, None
            return model, None

        if name:
            logger.info(
                "[JiuWenClawDeepAdapter] 模型名无效且无可用 model_tier=%r，回退主 Agent 默认模型",
                model_tier or None,
                extra={"user_visible": "progress"},
            )

        return self._model, None

    def _resolve_model_for_subagent(
        self,
        *,
        model_name: str = "",
        model_tier: str = "",
    ) -> tuple[Model, str | None]:
        """Subagent 工具选模型；model_name 或 model_tier 未匹配时回退默认模型。"""
        return self._resolve_model(
            model_name=model_name,
            model_tier=model_tier,
        )

    def _get_task_id(self) -> str | None:
        if self._task_execution_rail is not None:
            return self._task_execution_rail.get_current_task_id()
        return get_current_task_id()

    def _outer_loop_has_remaining_tasks(self, session_id: str) -> bool:
        """Return True when DeepAgent OuterLoop still has pending task-plan items."""
        deep = self._instance
        if deep is None:
            return False
        deep_config = getattr(deep, "_deep_config", None)
        if deep_config is None or not getattr(deep_config, "enable_task_loop", False):
            return False
        loop_session = getattr(deep, "_loop_session", None)
        has_remaining_fn = getattr(deep, "_has_remaining_tasks", None)
        if loop_session is None or not callable(has_remaining_fn):
            return False
        try:
            return bool(has_remaining_fn(loop_session))
        except Exception:
            logger.debug(
                "[JiuWenClawDeepAdapter] OuterLoop remaining-task check failed session_id=%s",
                session_id,
                exc_info=True,
            )
            return False

    def _apply_model_to_react_agent(self, model: Model) -> None:
        """将指定模型应用到 react_agent 实例（替换 _llm 和 _config 字段）。

        react_agent._railed_model_call 使用 self._config.model_name 作为 model= 参数，
        因此需要同时替换 _llm 和 _config 中的模型相关字段。
        同时同步 self._model_request_config / self._model_client_config，使
        _resolve_model_name() 与 _update_tools_for_mode() 反映当前生效模型，
        否则 RuntimePromptRail 注入的"模型：xxx"及 agent.fast 多会话子 agent
        会停留在旧值。
        """
        self._model_request_config = model.model_config
        self._model_client_config = model.model_client_config
        react_agent = getattr(self._instance, '_react_agent', None)
        if react_agent is None:
            return
        if callable(getattr(react_agent, 'set_llm', None)):
            react_agent.set_llm(model)
        config = getattr(react_agent, '_config', None)
        if config is not None:
            config.model_name = model.model_config.model_name
            config.model_client_config = model.model_client_config
            config.model_config_obj = model.model_config
        # Keep skill evolution on the same live model as the main conversation.
        if self._skill_evolution_rail is not None:
            applied_name = getattr(model.model_config, "model_name", None)
            if self._is_placeholder_model_name(str(applied_name or "")):
                applied_name = self._resolve_evolution_model_name()
            self._skill_evolution_rail.update_llm(model, str(applied_name))

    def _sync_sysop_to_react_agent(self) -> None:
        """把重建后的 sys_operation 同步到同 session 已活着的 ReActAgent。

        调用时机: reload_agent_config 里 self._instance.configure(deep_cfg)
        之后, 且 self._sys_operation 已被 _maybe_recreate_sys_operation 重建.
        """
        react_agent = getattr(self._instance, '_react_agent', None)
        if react_agent is None:
            return
        new_sysop = getattr(self, '_sys_operation', None)
        if new_sysop is None:
            return
        new_id = getattr(new_sysop, 'id', None)
        if not new_id:
            return
        cur_config = getattr(react_agent, '_config', None)
        if cur_config is None:
            return
        old_id = getattr(cur_config, 'sys_operation_id', None)
        if old_id == new_id:
            return
        # model_copy() 拿一份独立副本, 避免直接改 _config 影响其他引用;
        # 调 configure() 让 ReActAgent 按 sys_operation_id 变化重建 context_engine.
        try:
            new_config = cur_config.model_copy()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenClawDeepAdapter] sync sysop to react_agent: "
                "model_copy failed: %s", exc,
            )
            return
        new_config.sys_operation_id = new_id
        try:
            react_agent.configure(new_config)
            logger.info(
                "[JiuWenClawDeepAdapter] react_agent sysop synced on reload: "
                "old_id=%s new_id=%s",
                old_id, new_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenClawDeepAdapter] sync sysop to react_agent: "
                "configure failed: %s", exc,
            )
        # BashTool/ReadFileTool/CodeTool 等在构造时把 sysop 存到 self._operation,
        # 之后永不更新. _hot_reconfigure -> _hot_reload_tools 按 card.id 匹配,
        # id 相同就跳过, ability_manager 里的工具实例仍持有旧 SANDBOX sysop.
        # 必须遍历 DeepAgent 已注册的 rails, 更新其 sys_operation 并 uninit+init
        # 重建工具实例, 否则同 session 继续 chat 时 BashTool 仍走旧沙箱 sysop.
        self._reinit_rail_tools_with_new_sysop(new_sysop)

    def _reinit_rail_tools_with_new_sysop(self, new_sysop: Any) -> None:
        """重建已注册 rail 持有的工具实例, 确保 sysop为最新.

        仅处理持有 sys_operation 且在 init() 里构造工具的 rail
        (FileSystemRail / SysOperationRail 等). 对这类 rail:
        1. set_sys_operation(new_sysop) 更新 rail.sys_operation
        2. uninit(agent) 从 ability_manager / resource_mgr 清理旧工具
        3. init(agent) 用新 sysop 重新构造并注册工具

        其余 rail (SkillUseRail / MemoryRail 等) 不在此处理, 避免误清.
        """
        deep_agent = self._instance
        if deep_agent is None:
            return
        react_agent = getattr(deep_agent, '_react_agent', None)
        if react_agent is None:
            return
        registered_rails = getattr(deep_agent, '_registered_rails', None) or []
        reinit_targets: list[Any] = []
        for rail in registered_rails:
            # 只重建明确持有 sys_operation 的 fs/shell/code 工具 rail.
            # 用类名匹配, 兼容 SysOperationRail / FileSystemRail 等.
            cls_name = type(rail).__name__
            if cls_name in ("FileSystemRail", "SysOperationRail"):
                reinit_targets.append(rail)
        if not reinit_targets:
            return
        for rail in reinit_targets:
            try:
                set_sysop = getattr(rail, 'set_sys_operation', None)
                if callable(set_sysop):
                    set_sysop(new_sysop)
                uninit = getattr(rail, 'uninit', None)
                if callable(uninit):
                    uninit(react_agent)
                init = getattr(rail, 'init', None)
                if callable(init):
                    init(react_agent)
                logger.info(
                    "[JiuWenClawDeepAdapter] rail %s re-init with new sysop id=%s",
                    type(rail).__name__, getattr(new_sysop, 'id', None),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenClawDeepAdapter] rail %s re-init failed: %s",
                    type(rail).__name__, exc,
                )

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
            logger.info("[JiuWenClawDeepAdapter] ResponsePromptRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] ResponsePromptRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            rail = None
        return rail

    @staticmethod
    def _sys_operation_isolation_key(sysop_card: SysOperationCard) -> str | None:
        try:
            sys_operation = SysOperation(sysop_card)
            return sys_operation.isolation_key_template
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenClawDeepAdapter] failed to resolve sys_operation isolation key: %s",
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
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenClawDeepAdapter] failed to get registered sys_operation: %s",
                exc,
            )
            return None

    def _resolve_project_dir_for_sandbox(self) -> str | None:
        """Best-effort lookup of the user project directory for sandbox builds."""
        overrides = getattr(self, "_instance_overrides", None)
        if isinstance(overrides, dict):
            value = overrides.get("project_dir")
            if value:
                return str(value)
        workspace = getattr(self, "_workspace_dir", None)
        if workspace:
            return str(workspace)
        return None

    def _create_sys_operation(self) -> SysOperation | None:
        """Create a sys operation with workspace as working directory."""
        try:
            from jiuwenclaw.utils import get_multi_tenant_user_workspace_dir

            endpoint = get_sandbox_endpoint()
            runtime = get_sandbox_runtime()
            service_id, agent_id = self._tenant_disk_ids()
            tenant_ws = get_multi_tenant_user_workspace_dir(service_id, agent_id)
            agent_root = (tenant_ws / "agent") if tenant_ws is not None else get_agent_root_dir()
            work_dir = self._workspace_dir or str(agent_root)
            sandbox_url = endpoint.get("url") or ""
            sandbox_type = endpoint.get("type") or ""
            sandbox_enabled = bool(runtime.get("enabled"))
            if sandbox_enabled and sandbox_url and sandbox_type:
                logger.info(
                    "[JiuWenClawDeepAdapter] sandbox mode: url=%s type=%s "
                    "startup_mode=%s idle_ttl_seconds=%s idle_check_interval=%s",
                    sandbox_url,
                    sandbox_type,
                    endpoint.get("startup_mode"),
                    runtime.get("idle_ttl_seconds"),
                    runtime.get("idle_check_interval"),
                )
                sysop_card = create_sandbox_sysop_card(
                    sandbox_url,
                    sandbox_type,
                    self._agent_id,
                    shared_dir=agent_root,
                    files_runtime=runtime.get("files"),
                    excluded_commands=runtime.get("excluded_commands"),
                    idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
                    idle_check_interval=runtime.get("idle_check_interval"),
                    project_dir=self._resolve_project_dir_for_sandbox(),
                )
            else:
                logger.info(
                    "[DIAG] _create_sys_operation local branch: enabled=%s url=%s type=%s",
                    sandbox_enabled, sandbox_url, sandbox_type,
                )
                sysop_card = create_local_sysop_card(work_dir=work_dir)
            if sysop_card is None:
                if sandbox_enabled and sandbox_url and sandbox_type:
                    _allow_fallback = bool(runtime.get("fallback_on_failure"))
                    if _allow_fallback:
                        logger.warning(
                            "[JiuWenClawDeepAdapter] sandbox sysop_card is None "
                            "(create_sandbox_sysop_card failed); fallback to LOCAL mode "
                            "(隔离失效, 在宿主机直接执行; sandbox.fallback_on_failure=true 已启用)"
                        )
                        sysop_card = create_local_sysop_card(work_dir=work_dir)
                    else:
                        logger.error(
                            "[JiuWenClawDeepAdapter] sandbox sysop_card is None "
                            "(create_sandbox_sysop_card failed); fallback to LOCAL 已禁用 "
                            "(sandbox.fallback_on_failure 未启用). 任务失败而非静默绕过隔离"
                        )
                        return None
                if sysop_card is None:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] local fallback sysop_card also None"
                    )
                    return None

            isolation_key_template = JiuWenClawDeepAdapter._sys_operation_isolation_key(sysop_card)
            registered_sys_operation = (
                JiuWenClawDeepAdapter._get_registered_sys_operation_by_isolation_key(
                    isolation_key_template
                )
            )
            if registered_sys_operation is not None:
                logger.info(
                    "[DIAG] _create_sys_operation reuse: id=%s mode=%s iso_key=%s",
                    registered_sys_operation.id,
                    getattr(registered_sys_operation, "mode", None),
                    getattr(registered_sys_operation, "isolation_key_template", None),
                )
                return registered_sys_operation

            result = Runner.resource_mgr.add_sys_operation(sysop_card)
            if result.is_err():
                registered_sys_operation = (
                    JiuWenClawDeepAdapter._get_registered_sys_operation_by_isolation_key(
                        isolation_key_template
                    )
                )
                if registered_sys_operation is not None:
                    logger.info(
                        "[JiuWenClawDeepAdapter] reuse registered sys_operation after add failure: %s",
                        registered_sys_operation.id,
                    )
                    return registered_sys_operation
                is_sandbox_card = (
                    sysop_card.mode == OperationMode.SANDBOX
                    and sandbox_enabled
                    and sandbox_url
                    and sandbox_type
                )
                if is_sandbox_card:
                    _allow_fallback = bool(runtime.get("fallback_on_failure"))
                    if not _allow_fallback:
                        logger.error(
                            "[JiuWenClawDeepAdapter] sandbox add_sys_operation failed (%s); "
                            "fallback to LOCAL 已禁用 (sandbox.fallback_on_failure 未启用). "
                            "任务失败而非静默绕过隔离", result.msg(),
                        )
                        return None
                    logger.warning(
                        "[JiuWenClawDeepAdapter] sandbox add_sys_operation failed (%s); "
                        "fallback to LOCAL mode and retry (隔离失效, sandbox.fallback_on_failure=true)",
                        result.msg(),
                    )
                    local_card = create_local_sysop_card(work_dir=work_dir)
                    local_result = Runner.resource_mgr.add_sys_operation(local_card)
                    if local_result.is_ok():
                        return Runner.resource_mgr.get_sys_operation(local_card.id)
                    logger.warning(
                        "[JiuWenClawDeepAdapter] local fallback add also failed: %s",
                        local_result.msg(),
                    )
                logger.warning("[JiuWenClawDeepAdapter] add sys_operation failed: %s", result.msg())
                return None
            _created_sysop = Runner.resource_mgr.get_sys_operation(sysop_card.id)
            logger.info(
                "[DIAG] _create_sys_operation created: id=%s mode=%s iso_key=%s",
                sysop_card.id,
                getattr(_created_sysop, "mode", None) if _created_sysop else None,
                getattr(_created_sysop, "isolation_key_template", None) if _created_sysop else None,
            )
            return _created_sysop
        except Exception as exc:  # noqa: BLE001
            # 整个 _create_sys_operation 异常 (非 add 阶段): 若原本要走沙箱, 回退 local.
            if sandbox_enabled and sandbox_url and sandbox_type:
                _allow_fallback = bool(runtime.get("fallback_on_failure"))
                if not _allow_fallback:
                    logger.error(
                        "[JiuWenClawDeepAdapter] _create_sys_operation raised (%s); "
                        "fallback to LOCAL 已禁用 (sandbox.fallback_on_failure 未启用). "
                        "任务失败而非静默绕过隔离", exc,
                    )
                    return None
                logger.warning(
                    "[JiuWenClawDeepAdapter] _create_sys_operation raised (%s); "
                    "fallback to LOCAL mode (隔离失效, sandbox.fallback_on_failure=true)", exc,
                )
                try:
                    local_card = create_local_sysop_card(work_dir=work_dir)
                    local_result = Runner.resource_mgr.add_sys_operation(local_card)
                    if local_result.is_ok():
                        return Runner.resource_mgr.get_sys_operation(local_card.id)
                except Exception as fallback_exc:  # noqa: BLE001
                    logger.warning(
                        "[JiuWenClawDeepAdapter] local fallback after exception failed: %s",
                        fallback_exc,
                    )
            logger.warning("[JiuWenClawDeepAdapter] add sys_operation failed: %s", exc)
            return None

    def _maybe_recreate_sys_operation(self) -> None:
        """reload 时若 sandbox 配置变更，重建 _sys_operation 以让新 sandbox 生效。

        按 (enabled, url, type) 指纹比对；变更时调用 ``_create_sys_operation`` 重建，
        旧实例从 ``Runner.resource_mgr`` 清理；清理失败仅记录日志，不影响 reload。
        重建失败保留旧实例避免 reload 整体崩溃。
        """
        new_fp = self._sandbox_config_fingerprint()
        old_fp = getattr(self, "_sandbox_fingerprint", None)
        logger.info("[DIAG] _maybe_recreate: new_fp=%s old_fp=%s changed=%s",
            new_fp, old_fp, new_fp != old_fp,
        )
        if new_fp == old_fp:
            _cur = getattr(self, "_sys_operation", None)
            logger.info("[DIAG] _maybe_recreate noop (fp unchanged): sysop_id=%s mode=%s iso_key=%s",
                getattr(_cur, "id", None),
                getattr(_cur, "mode", None),
                getattr(_cur, "isolation_key_template", None) if _cur else None,
            )
            return
        old_sysop = getattr(self, "_sys_operation", None)
        new_sysop = self._create_sys_operation()
        if new_sysop is None:
            logger.warning(
                "[JiuWenClawDeepAdapter] reload sandbox changed %s -> %s but "
                "_create_sys_operation returned None; keep old sys_operation",
                old_fp, new_fp,
            )
            return
        self._sys_operation = new_sysop
        self._sandbox_fingerprint = new_fp
        logger.info(
            "[JiuWenClawDeepAdapter] sandbox config changed on reload: %s -> %s, "
            "sys_operation recreated",
            old_fp, new_fp,
        )
        if old_sysop is None:
            return
        old_id = getattr(old_sysop, "id", None) or getattr(getattr(old_sysop, "card", None), "id", None)
        if not old_id:
            return
        try:
            remove_result = Runner.resource_mgr.remove_sys_operation(old_id)
            if hasattr(remove_result, "is_err") and remove_result.is_err():
                logger.warning(
                    "[JiuWenClawDeepAdapter] remove old sys_operation failed: %s",
                    remove_result.msg(),
                )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] remove old sys_operation raised: %s", exc,
            )

    def _build_filesystem_rail(self) -> FileSystemRail | None:
        """Build FileSystemRail."""
        try:
            fs_rail = FileSystemRail()
            logger.info("[JiuWenClawDeepAdapter] FileSystemRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] FileSystemRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            fs_rail = None
        return fs_rail

    def _build_skill_rail(
        self,
        config: dict[str, Any],
        *,
        include_tools: bool = False,
        include_skill_body_tools: bool = True,
        extra_skill_dir: str | None = None,
    ) -> JiuWenSkillUseRail | None:
        """Build JiuWenSkillUseRail (per-session qualified skill tools).

        Args:
            config: React config dict
            include_tools: Whether to include harness read_file/code/bash tools
            include_skill_body_tools: Whether to include skill_tool/skill_complete tools
            extra_skill_dir: Optional extra skill directory from extension hook
        """
        try:
            skill_mode = self._resolve_skill_mode(config)
            logger.info("[JiuWenClawDeepAdapter] current skill_mode: %s", skill_mode,
                        extra={'user_visible': 'progress'})
            # Must match react.context_engine_config.max_active_skill_bodies (ContextEngineConfig);
            # otherwise SkillUseRail.init overwrites the merged yaml cap with the rail default (1).
            react_cec = (config.get("react") or {}).get("context_engine_config")
            max_bodies = DEFAULT_MAX_ACTIVE_SKILL_BODIES
            if isinstance(react_cec, dict) and react_cec.get("max_active_skill_bodies") is not None:
                try:
                    max_bodies = int(react_cec["max_active_skill_bodies"])
                except (TypeError, ValueError):
                    max_bodies = DEFAULT_MAX_ACTIVE_SKILL_BODIES

            skills_dirs = self._registered_skill_dirs_for_rail()
            if extra_skill_dir:
                skills_dirs = list(skills_dirs) + [extra_skill_dir]
                logger.info(
                    "[JiuWenClawDeepAdapter] extra_skill_dir added: %s",
                    extra_skill_dir,
                    extra={"user_visible": "progress"},
                )

            skill_rail_kwargs: dict[str, Any] = dict(
                skills_dir=skills_dirs,
                skill_mode=skill_mode,
                include_tools=include_tools,
            )
            if _UPSTREAM_HAS_ACTIVE_SKILL_BODIES:
                skill_rail_kwargs["include_skill_body_tools"] = include_skill_body_tools
                skill_rail_kwargs["max_active_skill_bodies"] = max_bodies
            skill_rail_kwargs["enabled_skills"] = enabled_skills_from_environ()
            # disabled_skills: same field accepts YAML list or ${DISABLED_SKILLS:-} string
            skill_rail_kwargs["disabled_skills"] = resolve_string_or_list_config(config.get("disabled_skills"))
            if skill_rail_kwargs["disabled_skills"]:
                logger.info(
                    "[JiuWenClawDeepAdapter] disabled_skills resolved: %s",
                    skill_rail_kwargs["disabled_skills"],
                )
            skill_rail = JiuWenSkillUseRail(**skill_rail_kwargs)
            logger.info("[JiuWenClawDeepAdapter] JiuWenSkillUseRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SkillUseRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            skill_rail = None
        return skill_rail

    def _resolve_evolution_trajectory_dir(self) -> Path:
        """Resolve directory for FileTrajectoryStore from this adapter's tenant ids."""
        sid, aid = self._tenant_disk_ids()
        return get_agent_evolution_trajectories_dir(sid, aid)

    def _build_skill_evolution_rail(self, config: dict[str, Any]) -> JiuClawSkillEvolutionRail | None:
        """Build JiuClawSkillEvolutionRail with BOOTSTRAP.md builtin skill exclusion."""
        try:
            evolution_auto_save = config.get("evolution", {}).get("auto_save", True)
            trajectory_dir = self._resolve_evolution_trajectory_dir()
            registered_skill_dirs = self._registered_skill_dirs_for_rail()
            evolution_model = self._resolve_evolution_model_name(config)
            skill_evolution_rail = JiuClawSkillEvolutionRail(
                skills_dir=registered_skill_dirs,
                llm=self._model,
                model=evolution_model,
                auto_save=evolution_auto_save,
                trajectory_store=FileTrajectoryStore(trajectory_dir),
            )
            self._skill_evolution_rail = skill_evolution_rail
            logger.info(
                "[JiuWenClaw] SkillEvolutionRail create success, model=%s, "
                "trajectory_dir=%s, auto_save=%r",
                evolution_model,
                trajectory_dir,
                evolution_auto_save,
                extra={'user_visible': 'progress'},
            )
        except Exception as exc:
            logger.warning("[JiuWenClaw] SkillEvolutionRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            skill_evolution_rail = None
        return skill_evolution_rail

    @staticmethod
    def _build_qa_block_freeze_rail(config: dict[str, Any]) -> JiuClawQABlockFreezeRail | None:
        qa_block_cfg = _resolve_qa_block_config(config)
        if qa_block_cfg is None:
            return None
        try:
            rail = JiuClawQABlockFreezeRail(qa_block_cfg)
            logger.info(
                "[JiuWenClawDeepAdapter] JiuClawQABlockFreezeRail create success enabled=%s",
                qa_block_cfg.enabled,
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] JiuClawQABlockFreezeRail create failed: %s",
                exc,
            )
            return None

    @staticmethod
    def _build_qa_block_assembly_rail(config: dict[str, Any]) -> JiuClawQABlockAssemblyRail | None:
        qa_block_cfg = _resolve_qa_block_config(config)
        if qa_block_cfg is None:
            return None
        try:
            rail = JiuClawQABlockAssemblyRail(qa_block_cfg)
            logger.info(
                "[JiuWenClawDeepAdapter] JiuClawQABlockAssemblyRail create success enabled=%s",
                qa_block_cfg.enabled,
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] JiuClawQABlockAssemblyRail create failed: %s",
                exc,
            )
            return None

    @staticmethod
    def _build_qa_artifact_rail(
        react_cfg: dict[str, Any],
        session_memory: SessionMemoryConfig | None,
    ) -> JiuClawQAArtifactRail | None:
        qa_artifact_cfg = _resolve_qa_artifact_config(react_cfg, session_memory)
        if qa_artifact_cfg is None:
            return None
        try:
            rail = JiuClawQAArtifactRail(qa_artifact_cfg, session_memory)
            logger.info(
                "[JiuWenClawDeepAdapter] JiuClawQAArtifactRail create success enabled=%s",
                qa_artifact_cfg.enabled,
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] JiuClawQAArtifactRail create failed: %s",
                exc,
            )
            return None

    def _build_stream_event_rail(self) -> JiuClawStreamEventRail | None:
        """Build JiuClawStreamEventRail."""
        try:
            stream_event_rail = JiuClawStreamEventRail()
            logger.info("[JiuWenClawDeepAdapter] JiuClawStreamEventRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] JiuClawStreamEventRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            stream_event_rail = None
        return stream_event_rail

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
        """Build TaskExecutionRail."""
        try:
            task_execution_rail = TaskExecutionRail()
            logger.info("[JiuWenClawDeepAdapter] TaskExecutionRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TaskExecutionRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            task_execution_rail = None
        return task_execution_rail

    @staticmethod
    def _build_telemetry_rail() -> Any | None:
        """Build TelemetryRail for OpenTelemetry instrumentation."""
        try:
            from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail
            rail = TelemetryRail()
            logger.info("[JiuWenClawDeepAdapter] TelemetryRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TelemetryRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            rail = None
        return rail

    @staticmethod
    def _build_request_summary_rail() -> Any | None:
        """Build RequestSummaryRail for per-request performance summaries."""
        try:
            from jiuwenclaw.perf.request_summary_rail import RequestSummaryRail

            rail = RequestSummaryRail(record_only=True)
            logger.info(
                "[JiuWenClawDeepAdapter] RequestSummaryRail create success",
                extra={"user_visible": "progress"},
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] RequestSummaryRail create failed: %s",
                exc,
                extra={"user_visible": "progress"},
            )
            rail = None
        return rail

    def _build_task_planning_rail(self, config: dict[str, Any] | None = None) -> TaskPlanningRail | None:
        """Build TaskPlanningRail."""
        try:
            from openjiuwen.harness.rails.task_planning_rail import (
                resolve_task_planning_rail_kwargs,
            )
            cfg = config if config is not None else self._config_cache
            react_cfg = (cfg or {}).get("react") or {}
            rail_kwargs = resolve_task_planning_rail_kwargs(react_cfg)
            task_planning_rail = TaskPlanningRail(**rail_kwargs)
            logger.info("[JiuWenClawDeepAdapter] TaskPlanningRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] TaskPlanningRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            task_planning_rail = None
        return task_planning_rail

    @staticmethod
    def _build_subagent_rail() -> SubagentRail | None:
        """Build SubagentRail for subagent delegation."""
        try:
            subagent_rail = SubagentRail()
            logger.info("[JiuWenClawDeepAdapter] SubagentRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SubagentRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            subagent_rail = None
        return subagent_rail

    def _build_security_rail(self) -> SecurityRail | None:
        """Build SecurityPromptRail."""
        try:
            security_prompt_rail = SecurityRail()
            logger.info("[JiuWenClawDeepAdapter] SecurityPromptRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] SecurityPromptRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
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
                logger.warning("[JiuWenClawDeepAdapter] MemoryRail create failed: No available embedding config",
                               extra={'user_visible': 'progress'})
                return None
            self._is_proactive_memory = is_proactive_memory(mode, config)
            memory_rail = MemoryRail(
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("model"),
                    base_url=embed_config.get("base_url"),
                    api_key=embed_config.get("api_key")
                ),
                is_proactive=self._is_proactive_memory
            )
            logger.info("[JiuWenClawDeepAdapter] MemoryRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] MemoryRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            memory_rail = None
        return memory_rail

    @staticmethod
    def _coding_memory_matches_workspace(
        rail: CodingMemoryRail | None,
        workspace_dir: str,
    ) -> bool:
        """Check whether a CodingMemoryRail belongs to the requested workspace."""
        if rail is None:
            return False
        cached_dir = getattr(rail, "_coding_memory_dir", None)
        if cached_dir is None:
            return True
        try:
            expected_dir = Path(workspace_dir or "./").resolve() / "coding_memory"
            return Path(cached_dir).resolve() == expected_dir
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def build_coding_memory_rail(self) -> CodingMemoryRail | None:
        """Build the CodingMemoryRail through the public adapter contract."""
        return self._build_coding_memory_rail()

    def _build_coding_memory_rail(self) -> CodingMemoryRail | None:
        """构建 CodingMemoryRail.

        Returns:
            CodingMemoryRail 实例，失败返回 None
        """
        cached_rail = self._coding_memory_rail
        cache_matches_workspace = self._coding_memory_matches_workspace(
            cached_rail,
            getattr(self, "_workspace_dir", "./"),
        )
        if cached_rail is not None and cache_matches_workspace:
            logger.info("[JiuWenClawDeepAdapter] Reusing cached CodingMemoryRail")
            return cached_rail
        if cached_rail is not None:
            logger.info(
                "[JiuWenClawDeepAdapter] Building CodingMemoryRail for new workspace"
            )
        try:
            from jiuwenclaw.agentserver.memory.config import get_embed_config
            config = get_config()
            embed_config = get_embed_config()

            # 检查 embedding 配置
            has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
            has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
            has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning("[JiuWenClawDeepAdapter] CodingMemoryRail: no embedding config, skipping",
                               extra={'user_visible': 'progress'})
                return None

            # 获取语言和 workspace 目录
            language = config.get("preferred_language", "zh")
            workspace_dir = getattr(self, "_workspace_dir", "./")
            coding_memory_dir = os.path.join(workspace_dir, "coding_memory")

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
            # A stale rail can still be registered while a workspace switch is
            # being reconciled.  Keep it out of the adapter cache until reload
            # has atomically registered this new object.
            if cached_rail is None or not getattr(self, "_code_mode_rails_active", False):
                self._coding_memory_rail = coding_memory_rail
            logger.info("[JiuWenClawDeepAdapter] CodingMemoryRail create success",
                        extra={'user_visible': 'progress'})
            return coding_memory_rail

        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] CodingMemoryRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            return None

    @staticmethod
    def _build_lsp_rail() -> LspRail | None:
        """Build LspRail."""
        try:
            lsp_rail = LspRail()
            logger.info("[JiuWenClawDeepAdapter] LspRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] LspRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            lsp_rail = None
        return lsp_rail

    def _build_heartbeat_rail(self) -> HeartbeatRail | None:
        """Build HeartbeatRail."""
        try:
            heartbeat_rail = HeartbeatRail()
            logger.info("[JiuWenClawDeepAdapter] HeartbeatRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] HeartbeatRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            heartbeat_rail = None
        return heartbeat_rail

    @staticmethod
    def _build_avatar_rail() -> Any | None:
        """Build AvatarPromptRail for digital avatar mode."""
        try:
            from jiuwenclaw.agentserver.deep_agent.rails.avatar_rail import AvatarPromptRail
            rail = AvatarPromptRail()
            logger.info("[JiuWenClawDeepAdapter] AvatarPromptRail create success",
                        extra={'user_visible': 'progress'})
            return rail
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] AvatarPromptRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            return None

    @staticmethod
    def _build_skill_protocol_prompt_rail() -> SkillProtocolPromptRail | None:
        """Build SkillProtocolPromptRail:注入技能执行协议提示。"""
        try:
            rail = SkillProtocolPromptRail()
            logger.info(
                "[JiuWenClawDeepAdapter] SkillProtocolPromptRail create success "
                "(skill protocol prompt)",
                extra={'user_visible': 'progress'}
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillProtocolPromptRail create failed: %s", exc,
                extra={'user_visible': 'progress'}
            )
            return None

    def _build_skill_compliance_rail(self) -> SkillComplianceRail | None:
        """Build SkillComplianceRail：硬绑 SKILL.md usage lifecycle。"""
        try:
            skill_dir_resolver = None
            if self._skill_rail is not None:
                skill_dir_resolver = self._skill_rail.get_skills_meta
            rail = SkillComplianceRail(skill_dir_resolver=skill_dir_resolver)
            logger.info(
                "[JiuWenClawDeepAdapter] SkillComplianceRail create success "
                "(skill_dir_resolver=%s)",
                "wired" if skill_dir_resolver else "none",
                extra={'user_visible': 'progress'}
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillComplianceRail create failed: %s", exc,
                extra={'user_visible': 'progress'}
            )
            return None

    @staticmethod
    def _build_skill_credential_injection_rail(
        config: dict[str, Any],
    ) -> SkillCredentialInjectionRail | None:
        """Build SkillCredentialInjectionRail for per-skill env-var injection."""
        try:
            skill_envs = coalesce_skill_envs(config.get("skill_envs"), None)
            rail = SkillCredentialInjectionRail(skill_envs=skill_envs)
            logger.info(
                "[JiuWenClawDeepAdapter] SkillCredentialInjectionRail create success "
                "(skills=[%s])",
                ", ".join(skill_envs.keys()) if skill_envs else "",
                extra={'user_visible': 'progress'}
            )
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillCredentialInjectionRail create failed: %s", exc,
                extra={'user_visible': 'progress'}
            )
            return None

    def _build_runtime_prompt_rail(self) -> RuntimePromptRail | None:
        """Build RuntimePromptRail for per-model-call time/channel/runtime injection."""
        try:
            default_channel = (
                "acp" if self._is_acp_tool_profile(self._instance_overrides)
                else self._resolve_prompt_channel()
            )
            disk_service_id, disk_agent_id = self._tenant_disk_ids()
            rail = RuntimePromptRail(
                language=self._resolve_runtime_language(),
                channel=default_channel,
                agent_name=self._agent_name,
                model_name=self._resolve_model_name(),
                workspace_dir=self._workspace_dir,
                # Disk layout follows env/catalog ids, not AGENT_RUNTIME cache_key.
                agent_id=disk_agent_id,
                service_id=disk_service_id,
            )
            rail.set_registered_skill_dirs(self._registered_skill_dirs_for_rail())
            logger.info("[JiuWenClawDeepAdapter] RuntimePromptRail create success",
                        extra={'user_visible': 'progress'})
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] RuntimePromptRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            rail = None
        return rail

    def _build_disabled_tools_rail(self, config: dict[str, Any]) -> DisabledToolsRail | None:
        """Build DisabledToolsRail to filter out disabled tools based on config.

        ``react.disabled_tools`` accepts two formats (same field, pick one):
          - YAML list:      ``disabled_tools: ["bash", "fork_agent"]``
          - Env var string: ``disabled_tools: ${DISABLED_TOOLS:-}``
            (set DISABLED_TOOLS=bash,fork_agent in .env)

        Uses ``touch_shared_resource_mgr=False`` so disable only affects this agent's
        ``ability_manager``; shared ``Runner.resource_mgr`` stays intact for other
        in-process agents/sessions (单进程多智能体).
        """
        try:
            disabled_list = resolve_string_or_list_config(config.get("disabled_tools"))
            rail = DisabledToolsRail(
                disabled_tools=disabled_list,
                touch_shared_resource_mgr=False,
            )
            logger.info(
                "[JiuWenClawDeepAdapter] DisabledToolsRail create success, disabled_tools: %s "
                "(ability-only, shared resource_mgr untouched)",
                disabled_list,
                extra={'user_visible': 'progress'}
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] DisabledToolsRail create failed: %s", exc,
                           extra={'user_visible': 'progress'})
            rail = None
        return rail

    def _build_progressive_tool_rail(
        self,
        config: dict[str, Any],
    ) -> JiuWenProgressiveToolRail | None:
        """Build progressive tool rail from react.tool_lazy_load config."""
        rail = build_jiuwen_progressive_tool_rail_from_react_config(
            config,
            language=self._resolve_runtime_language(),
            profile="main",
            agent_id=self._agent_id,
            agent_card_id=self._resolve_agent_card_id(),
        )
        if rail is not None:
            logger.info(
                "[JiuWenClawDeepAdapter] ProgressiveToolRail enabled (fixed schema mode)"
            )
        return rail

    def _rebind_progressive_tool_rail_after_reload(self) -> None:
        """Rebind PTR to post-configure instance so deferred cache matches ability_manager."""
        rail = getattr(self, "_progressive_tool_rail", None)
        instance = self._instance
        if rail is None or instance is None:
            return
        card_id = self._resolve_agent_card_id()
        if card_id:
            rail.update_agent_card_id(card_id)
        rail.init(instance)
        rail.invalidate_deferred_tool_cache()
        logger.info(
            "[JiuWenClawDeepAdapter] PTR rebind after configure agent_id=%s agent_card_id=%s",
            self._agent_id,
            rail.agent_card_id,
        )

    def _build_fast_subagent_permission_rail(self) -> Any | None:
        """为 agent.fast 子 ReActAgent 构造独立的 PermissionInterruptRail（每实例新建）。"""
        try:
            config_base = get_config()
            model_name = (
                (config_base.get("models") or {})
                .get("default", {})
                .get("model_client_config", {})
                .get("model_name", "gpt-4")
            )
            return build_permission_rail(
                config=config_base,
                llm=self._model,
                model_name=model_name,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] fast sub-agent PermissionInterruptRail build failed: %s",
                exc,
                extra={'user_visible': 'progress'}
            )
            return None

    def _build_fast_subagent_disabled_tools_rail(self) -> DisabledToolsRail | None:
        """为 agent.fast 子 ReActAgent 构造 DisabledToolsRail；仅剥离 ability，避免并发踩共享 resource_mgr。"""
        react = self._config_cache or {}
        disabled_list = resolve_string_or_list_config(react.get("disabled_tools"))
        if not disabled_list:
            return None
        try:
            return DisabledToolsRail(
                disabled_tools=disabled_list,
                touch_shared_resource_mgr=False,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] fast sub-agent DisabledToolsRail build failed: %s",
                exc,
                extra={'user_visible': 'progress'}
            )
            return None

    @staticmethod
    def _build_pip_isolation_rail() -> PipIsolationRail | None:
        try:
            return PipIsolationRail()
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] PipIsolationRail create failed: %s", exc)
            return None

    def _build_agent_rails(self, config: dict[str, Any], config_base: dict[str, Any], *,
                           mode: str = "agent.plan",
                           extra_skill_dir: str | None = None) -> list[Any]:
        """Build DeepAgent rails consistently for cold start and hot reload.

        Args:
            config: React config dict
            config_base: Full config dict
            mode: Agent mode (agent.plan, agent.fast, code)
            extra_skill_dir: Optional extra skill directory from extension hook
        """
        mode = self._normalize_rail_mode(mode)

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
            _RailBuildInfo("_request_summary_rail", self._build_request_summary_rail),
            _RailBuildInfo("_runtime_prompt_rail", self._build_runtime_prompt_rail),
            _RailBuildInfo("_response_prompt_rail", self._build_response_prompt_rail),
            _RailBuildInfo("_task_execution_rail", self._build_task_execution_rail),
            _RailBuildInfo("_context_overflow_recovery_rail", self._build_context_overflow_recovery_rail),
            _RailBuildInfo("_stream_event_rail", self._build_stream_event_rail),
            _RailBuildInfo("_security_rail", self._build_security_rail),
            _RailBuildInfo("_heartbeat_rail", self._build_heartbeat_rail),
            _RailBuildInfo("_avatar_rail", self._build_avatar_rail),
            _RailBuildInfo("_subagent_rail", self._build_subagent_rail),
            _RailBuildInfo("_pip_isolation_rail", self._build_pip_isolation_rail),
            _RailBuildInfo("_permission_rail", build_permission_rail, {"config": config_base, "llm": self._model,
                                                                       "model_name": config_base.get("models", {}).get(
                                                                           "default", {}).get("model_client_config",
                                                                                              {}).get("model_name",
                                                                                                      "gpt-4")}),
            _RailBuildInfo("_progressive_tool_rail", self._build_progressive_tool_rail, {"config": config}),
            # DisabledToolsRail - highest priority (100), runs last to filter disabled tools
            _RailBuildInfo("_disabled_tools_rail", self._build_disabled_tools_rail, {"config": config}),
            _RailBuildInfo("_recent_tool_results_rail", self._build_recent_tool_results_rail),
        ]
        if mode != "code":
            rail_infos.insert(
                7,
                _RailBuildInfo(
                    "_task_planning_rail",
                    self._build_task_planning_rail,
                    {"config": config_base},
                ),
            )
        # ContextEngineeringRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销

        # SkillEvolutionRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销
        # 智能模式下关闭自演进，plan 模式下按配置启用

        # MemoryRail 不在冷启动时挂载，由 _update_rails_for_mode 按 mode 按需注册/注销

        # LspRail 仅在 code 模式下挂载
        code_mode_rails: list[Any] = []
        if mode == "code":
            if not _code_memory_enabled(config_base):
                logger.info(
                    "[JiuWenClawDeepAdapter] Clearing cached code memory rails: "
                    "memory disabled"
                )
                self._project_memory_rail = None
                self._coding_memory_rail = None
            elif (
                getattr(self, "_code_mode_rails_active", False)
                and self._coding_memory_rail is not None
                and not self._coding_memory_matches_workspace(
                    self._coding_memory_rail,
                    self._workspace_dir,
                )
            ):
                # A full code-agent rebuild will replace the registered rail
                # set; do not let the old object be reused by subagents.
                self._coding_memory_rail = None
            rail_infos.append(_RailBuildInfo("_lsp_rail", self._build_lsp_rail))
            code_mode_rails = build_code_mode_extra_rails(
                self,
                config_base,
                project_dir=self._instance_overrides.get("project_dir"),
                workspace_dir=self._workspace_dir,
                language=self._resolve_runtime_language(),
            )
        else:
            self._code_mode_rails = []
            self._code_mode_rails_active = False
            self._code_mode_workspace = None
            self._project_memory_rail = None
            self._coding_memory_rail = None
            self._lsp_rail = None

        # Skill 合规相关 rail 仅在 plan 模式下挂载；agent 模式不注入，避免改变既有行为。
        # team 模式由 build_member_rails 自行挂载，不在这里处理。
        if mode == "agent.plan":
            rail_infos.append(
                _RailBuildInfo("_skill_protocol_prompt_rail", self._build_skill_protocol_prompt_rail)
            )
            rail_infos.append(
                _RailBuildInfo("_skill_compliance_rail", self._build_skill_compliance_rail)
            )
            rail_infos.append(
                _RailBuildInfo(
                    "_skill_credential_injection_rail",
                    self._build_skill_credential_injection_rail,
                    {"config": config},
                )
            )
        else:
            self._skill_protocol_prompt_rail = None
            self._skill_compliance_rail = None
            self._skill_credential_injection_rail = None

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

        if mode == "code":
            self._code_mode_rails = code_mode_rails
            self._code_mode_rails_active = True
            self._code_mode_workspace = str(Path(self._workspace_dir or "./").resolve())
            self._task_planning_rail = next(
                (
                    rail
                    for rail in code_mode_rails
                    if isinstance(rail, TaskPlanningRail)
                ),
                None,
            )
            for rail in code_mode_rails:
                rail_name = type(rail).__name__
                if isinstance(rail, ProjectMemoryRail):
                    self._project_memory_rail = rail
                elif isinstance(rail, CodingMemoryRail):
                    self._coding_memory_rail = rail
                rails_list.append(rail)
                logger.info(
                    "[JiuWenClawDeepAdapter] Code-mode rail %s added",
                    rail_name,
                )
        logger.info("[JiuWenClawDeepAdapter] Total rails built: %d, rail names: %s", len(rails_list),
                    [type(r).__name__ for r in rails_list])

        if self._task_execution_rail is None:
            logger.warning("[JiuWenClawDeepAdapter] TaskExecutionRail missing after _build_agent_rails")
        else:
            logger.info("[JiuWenClawDeepAdapter] TaskExecutionRail attached to adapter")
        return rails_list

    @staticmethod
    def _build_recent_tool_results_rail() -> RecentToolResultsRail:
        return RecentToolResultsRail()

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
            enable_read_image_multimodal=DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL,
            completion_timeout=config.get("completion_timeout", 21600.0),
        )

    def _update_permission_rail(self, config_base: dict[str, Any] | None) -> None:
        """原地更新已有 PermissionRail 配置，或在首次启用时新建。"""
        permission_config = config_base.get("permissions", {}) if config_base else {}
        # Prefer self._model's model_name (already patched by _create_model via
        # patch_model_config_from_env) over raw config_base which may still hold
        # unresolved ${MODEL_NAME} or .env baseline values.
        model_name = (
            getattr(getattr(self._model, "model_config", None), "model_name", None)
            or (config_base or {}).get("models", {}).get(
                "default", {}).get("model_client_config", {}).get("model_name", "")
        )
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
    ) -> tuple[list[Any], list[Any]]:
        """Return rail replacements and rails to retire after configure.

        Only rails that require a **new object** enter ``rails_list``. Rails updated
        in-place (PermissionRail, DisabledToolsRail) must not pass the same instance,
        or openjiuwen will unload without re-registering.
        """
        # 触发钩子获取扩展目录（与 create_instance 保持一致）
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

            logger.info(
                "[JiuWenClawDeepAdapter] reload_agent_config: BEFORE_SYSTEM_PROMPT_BUILD triggered, "
                "skill_dir=%s",
                extra_skill_dir,
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] reload_agent_config hook trigger failed: %s", exc)

        rails_to_unregister: list[Any] = []

        # SkillEvolutionRail: respond to react.evolution.enabled on hot reload.
        # core's _hot_reload_rails retires registered rails whose type appears in
        # rails_list, then loads rails_list entries. Passing the SAME object → core
        # unregisters the stale twin but skips loading (unload-only). Passing a NEW
        # object → unregister stale + load new. Omitting the type → retained.
        evolution_enabled = bool(config.get("evolution", {}).get("enabled", False))
        new_evolution_rail = None
        if self._skill_evolution_rail is not None and not evolution_enabled:
            # Keep the live reference until configure and explicit unregister succeed.
            rails_to_unregister.append(self._skill_evolution_rail)
            logger.info(
                "[JiuWenClawDeepAdapter] SkillEvolutionRail staged for unregister "
                "on reload (evolution disabled)"
            )
        elif self._skill_evolution_rail is None and evolution_enabled:
            # enabled false->true: build a fresh rail and load it.
            new_evolution_rail = self._build_skill_evolution_rail(config)
        elif self._skill_evolution_rail is not None and evolution_enabled:
            # enabled unchanged (on): in-place update LLM / auto_save, rail retained.
            self._skill_evolution_rail.update_llm(
                self._model, self._resolve_evolution_model_name(config)
            )
            self._skill_evolution_rail.auto_save = config.get("evolution", {}).get("auto_save", True)

        self._skill_rail = self._build_skill_rail(
            config,
            include_tools=self._skill_include_harness_fs_tools(),
            include_skill_body_tools=self._skill_include_skill_body_tools(),
            extra_skill_dir=extra_skill_dir,
        )

        if (
            not self._filesystem_rail_enabled_for_profile()
            and self._filesystem_rail is not None
        ):
            rails_to_unregister.append(self._filesystem_rail)

        # --- SkillCredentialInjectionRail hot-update (before permission rail) ---
        skill_credential_rail_newly_created = False
        incoming_skill_envs = config.get("skill_envs", {})
        current_skill_envs = (
            self._skill_credential_injection_rail.get_skill_envs()
            if getattr(self, "_skill_credential_injection_rail", None) is not None
            else None
        )
        new_skill_envs = coalesce_skill_envs(incoming_skill_envs, current_skill_envs)
        if new_skill_envs is not incoming_skill_envs and new_skill_envs:
            logger.info(
                "[JiuWenClawDeepAdapter] keeping skill_envs skills=[%s]; "
                "empty YAML/overlay would wipe catalog credentials",
                ", ".join(new_skill_envs.keys()),
            )
            config["skill_envs"] = new_skill_envs
            if isinstance(config_base, dict):
                react_base = config_base.get("react")
                if isinstance(react_base, dict):
                    react_base["skill_envs"] = new_skill_envs
        if getattr(self, "_skill_credential_injection_rail", None) is not None:
            self._skill_credential_injection_rail.update_skill_envs(new_skill_envs)
            logger.info("[JiuWenClawDeepAdapter] skill_envs hot-updated for SkillCredentialInjectionRail")
        else:
            # First-time activation: create the rail (will be added to rails_list below)
            self._skill_credential_injection_rail = self._build_skill_credential_injection_rail(config)
            if self._skill_credential_injection_rail is not None:
                skill_credential_rail_newly_created = True

        permission_rail_newly_created = self._permission_rail is None
        self._update_permission_rail(config_base)

        # ProgressiveToolRail can be enabled or disabled through hot reload.
        old_progressive_tool_rail = self._progressive_tool_rail
        progressive_tool_rail = self._build_progressive_tool_rail(config)
        if progressive_tool_rail is not None:
            self._progressive_tool_rail = progressive_tool_rail
        elif old_progressive_tool_rail is not None:
            rails_to_unregister.append(old_progressive_tool_rail)

        # Update disabled_tools_rail config in-place (no re-init needed)
        disabled_tools_rail_newly_created = False
        if self._disabled_tools_rail is not None:
            disabled_list = resolve_string_or_list_config(config.get("disabled_tools"))
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

        ce_fingerprint = self._context_engine_config_fingerprint(
            config_base if isinstance(config_base, dict) else {"react": config}
        )
        if (
            self._context_engineering_rail is not None
            and ce_fingerprint != self._context_engine_config_fp
        ):
            mode = self._context_engineering_rail_mode or self._last_runtime_mode
            new_ce_rail = _build_context_engineering_rail(config, mode=mode)
            if new_ce_rail is not None:
                self._context_engineering_rail = new_ce_rail
                self._context_engineering_rail_mode = mode
                rails_list.append(new_ce_rail)
        self._context_engine_config_fp = ce_fingerprint

        # MemoryRail is managed by _handle_memory_rail_by_config after configure().
        # LspRail / AvatarRail / PipIsolationRail: in-place or cold-start only — do not add.
        if permission_rail_newly_created and self._permission_rail is not None:
            rails_list.append(self._permission_rail)

        if progressive_tool_rail is not None:
            rails_list.append(progressive_tool_rail)
        if disabled_tools_rail_newly_created and self._disabled_tools_rail is not None:
            rails_list.append(self._disabled_tools_rail)
        # SkillCredentialInjectionRail: only add when newly created (in-place update otherwise)
        if skill_credential_rail_newly_created and self._skill_credential_injection_rail is not None:
            rails_list.append(self._skill_credential_injection_rail)
        if new_evolution_rail is not None:
            rails_list.append(new_evolution_rail)
        logger.info(
            "[JiuWenClawDeepAdapter]  DIAGNOSTIC: rails_list 构建完成 "
            "| rails_count=%d "
            "| rails_names=[%s] "
            "| has_runtime_prompt_rail=%s",
            len(rails_list),
            ", ".join(type(r).__name__ for r in rails_list),
            any(type(r).__name__ == "RuntimePromptRail" for r in rails_list),
        )

        return rails_list, rails_to_unregister

    async def _get_tool_cards(self, agent_card_id: str, *, mode: str = "agent.plan"):
        """Get tool cards with session-qualified resource_mgr registration."""
        tool_cards = []

        from jiuwenclaw.agentserver.tools.web_search.content_cache import (
            get_agent_cache_registry,
        )

        content_cache = await get_agent_cache_registry().get_cache(agent_card_id)
        web_tools = build_jiuwen_harness_named_web_tools(
            agent_id=agent_card_id,
            language=self._resolve_runtime_language(),
            cache=content_cache,
        )
        self._register_runtime_tools(list(web_tools), agent_card_id)
        tool_cards.extend(t.card for t in web_tools)

        self._vision_tools = []
        self._vision_tools_registered = False
        if self._vision_model_config is not None:
            try:
                vision_tools = create_vision_tools(
                    language=self._resolve_runtime_language(),
                    vision_model_config=self._vision_model_config,
                    agent_id=agent_card_id,
                )
                self._vision_tools = vision_tools
                self._vision_tools_registered = self._register_runtime_tools(
                    vision_tools,
                    agent_card_id,
                )
                tool_cards.extend(t.card for t in vision_tools)
            except Exception as exc:
                self._vision_tools = []
                logger.warning(
                    "[JiuWenClawDeepAdapter] vision tools registration failed: %s",
                    exc,
                )

        self._audio_tools = []
        self._audio_tools_registered = False
        try:
            self._audio_tools = self._iter_runtime_audio_tools(agent_card_id)
            self._audio_tools_registered = self._register_runtime_tools(
                self._audio_tools,
                agent_card_id,
            )
            tool_cards.extend(t.card for t in self._audio_tools)
        except Exception as exc:
            self._audio_tools = []
            logger.warning(
                "[JiuWenClawDeepAdapter] audio tools registration failed: %s",
                exc,
            )

        self._video_tool_registered = False
        if self._video_model_config:
            try:
                Runner.resource_mgr.add_tool(video_understanding)
                tool_cards.append(video_understanding.card)
                self._video_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] video tool registration failed: %s",
                    exc,
                )

        self._image_gen_tool_registered = False
        self._image_gen_tools = []
        if self._image_gen_enabled:
            try:
                image_tool = create_session_text_to_image_tool(agent_card_id)
                self._image_gen_tools = [image_tool]
                self._image_gen_tool_registered = self._register_runtime_tools(
                    [image_tool],
                    agent_card_id,
                )
                tool_cards.append(image_tool.card)
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] text_to_image registration failed: %s",
                    exc,
                )

        # 小艺手机端工具：由 channels.xiaoyi.phone_tools_enabled 控制
        loop = asyncio.get_running_loop()
        # Preserve sealed overlay / agent env ns across the executor thread.
        _cfg_ctx = copy_context()
        config_base = await loop.run_in_executor(None, _cfg_ctx.run, get_config)
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
                    Runner.resource_mgr.add_tool(xt)
                    tool_cards.append(xt.card)
                self._xiaoyi_phone_tools_registered = True
                logger.info(
                    "[JiuWenClawDeepAdapter] %d xiaoyi phone tools registered", len(_xiaoyi_tools)
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] xiaoyi phone tools registration failed: %s", exc
                )

        try:
            skill_toolkit = SkillToolkit(manager=self._skill_manager)
            skill_tool_names: list[str] = []
            for tool in skill_toolkit.get_tools():
                if not Runner.resource_mgr.get_tool(tool.card.id):
                    Runner.resource_mgr.add_tool(tool)
                tool_cards.append(tool.card)
                skill_tool_names.append(tool.card.name)
            logger.info(
                "[JiuWenClawDeepAdapter] SkillToolkit registered: tools=%s",
                skill_tool_names,
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] skill tools registration failed: %s", exc)

        # AskUserQuestion 工具：用于 LLM 主动结构化追问并等待用户回答
        try:
            ask_tool = get_ask_user_question_tool()
            if not Runner.resource_mgr.get_tool(ask_tool.card.id):
                Runner.resource_mgr.add_tool(ask_tool)
            tool_cards.append(ask_tool.card)
            logger.info("[JiuWenClawDeepAdapter] AskUserQuestion tool registered")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] AskUserQuestion tool registration failed: %s", exc)

        # DeepResearch 执行工具
        try:
            for tool in get_deepresearch_tools():
                Runner.resource_mgr.add_tool(tool)
                tool_cards.append(tool.card)
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
        service_id, tenant_agent_id = self._tenant_disk_ids()
        return self._cron_runtime.build_tools(
            context=self._runtime_cron_tool_context,
            agent_id=JIUWENCLAW_RESOURCE_AGENT_ID,
            service_id=service_id,
            tenant_agent_id=tenant_agent_id,
        )

    async def _proc_context_compaction(self) -> None:
        """Backward-compatible no-op hook for tests and legacy call sites."""
        return None

    def _init_agent_instance_sync(self, ctx: _AgentInitContext) -> None:
        """同步执行 agent 实例初始化的重操作（在独立线程中执行，避免阻塞 event loop）。

        包含 _build_agent_rails（磁盘 I/O 读取技能文件）、_create_sys_operation、
        create_deep_agent / create_code_agent 等耗时同步调用。
        """
        self._sync_registered_skill_dirs_snapshot()
        rails_list = self._build_agent_rails(
            ctx.config, ctx.config_base, mode=ctx.mode,
            extra_skill_dir=ctx.extra_skill_dir,
        )

        sys_operation = self._create_sys_operation()
        if sys_operation is None:
            raise RuntimeError("sys_operation is not available, maybe task is not running")

        self._sys_operation = sys_operation
        self._sandbox_fingerprint = self._sandbox_config_fingerprint()
        configured_subagents = self._build_configured_subagents(ctx.model, ctx.config, ctx.config_base)
        common_kwargs = dict(
            model=ctx.model,
            card=ctx.agent_card,
            system_prompt=build_identity_prompt(
                mode="agent.fast",
                language=self._resolve_prompt_language(),
                channel=(
                    "acp" if self._is_acp_tool_profile(self._instance_overrides)
                    else self._resolve_prompt_channel()
                ),
            ),
            tools=ctx.tool_cards if ctx.tool_cards else [],
            subagents=configured_subagents,
            rails=rails_list if rails_list else [],
            enable_task_loop=ctx.config.get("enable_task_loop", True),
            max_iterations=ctx.config.get("max_iterations", 15),
            workspace=Workspace(
                root_path=self._workspace_dir or "./",
                language=self._resolve_runtime_language(),
            ),
            sys_operation=sys_operation,
            language=self._resolve_runtime_language(),
        )

        # agent_ras applies to both code and claw paths (create_code_agent
        # forwards **config_kwargs into create_deep_agent).
        common_kwargs.update(_agent_ras_kwargs_from_config(ctx.config_base))
        # ctx.mode may still be an unnormalized Code submode (code.plan /
        # code.normal); normalize it the same way _build_agent_rails() did so
        # the constructor choice matches the rails that were just built.
        ctx_mode = self._normalize_rail_mode(ctx.mode)
        if ctx_mode == "code":
            self._instance = create_code_agent(**common_kwargs)
        else:
            self._instance = create_deep_agent(
                **common_kwargs,
                context_engine_config=_deep_agent_context_engine_config(ctx.config),
                vision_model_config=self._vision_model_config,
                audio_model_config=self._audio_model_config,
                enable_read_image_multimodal=DEFAULT_ENABLE_READ_IMAGE_MULTIMODAL,
                completion_timeout=ctx.config.get("completion_timeout", 21600.0),
            )

    async def create_instance(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent.plan",
        session_id: str | None = None,
    ) -> None:
        """Initialize an agent while its request-side environment namespace is bound."""
        sid, aid = self._env_ns_ids()
        ns_token = bind_agent_env_ns(sid, aid)
        try:
            await self._create_instance_in_env_ns(
                config,
                mode=mode,
                session_id=session_id,
            )
        finally:
            reset_agent_env_ns(ns_token)

    async def _create_instance_in_env_ns(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent.plan",
        session_id: str | None = None,
    ) -> None:
        """初始化 DeepAgent 实例.

        Args:
            config: 可选配置，支持以下字段：
                - agent_name: Agent 名称，默认 "main_agent"。
                - workspace_dir: 工作区目录，默认 "workspace/agent"。
                - 其余字段透传给 DeepAgentConfig。
            mode: 实例化模式，支持 "claw"（默认，使用 create_deep_agent）和 "code"（使用 create_code_agent）。
            session_id: 会话 ID，用于生成唯一的 AgentCard.id，隔离 outer rail 回调命名空间。
        """
        if session_id is not None:
            self._session_id = session_id.strip() or None

        await self.set_checkpoint()

        self._instance_overrides = dict(config) if isinstance(config, dict) else {}
        loop = asyncio.get_running_loop()
        # Align with reload: drop stale resolved ${VAR} cache before reading under seal.
        clear_global_config_cache()
        # Preserve sealed overlay / agent env ns across the executor thread.
        _cfg_ctx = copy_context()
        config_base = await loop.run_in_executor(None, _cfg_ctx.run, get_config)
        config_base = coalesce_config_skill_envs(config_base, self._instance_overrides)
        self._latest_config_base = config_base if isinstance(config_base, dict) else None
        self._refresh_multimodal_configs(config_base)
        config = config_base.get('react', {}).copy()
        self._config_cache = config.copy()
        self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))

        # Keep constructor-injected tenant workspace by default.
        # Only override when request explicitly provides workspace_dir.
        configured_workspace = self._instance_overrides.get("workspace_dir")
        if configured_workspace is not None:
            self._workspace_dir = configured_workspace

        agent_card_id = self._resolve_agent_card_id(session_id)
        agent_card = AgentCard(name=self._agent_name, id=agent_card_id)
        tool_cards = await self._get_tool_cards(agent_card_id, mode=mode)
        self._tool_cards = tool_cards

        permissions_cfg = config_base.get("permissions", {})
        init_permission_engine(permissions_cfg)
        logger.info(
            "[JiuWenClawDeepAdapter] Permission engine initialized: enabled=%s (raw=%s)",
            get_permission_engine().enabled,
            permissions_cfg.get("enabled", True),
        )

        # 触发 BEFORE_SYSTEM_PROMPT_BUILD 钩子获取扩展目录
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

            logger.info(
                "[JiuWenClawDeepAdapter] BEFORE_SYSTEM_PROMPT_BUILD triggered: "
                "skill_dir=%s",
                extra_skill_dir,
            )
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] hook trigger failed: %s", exc)

        # First create must patch model from sealed overlay / tip (same as reload path).
        sid, aid = self._env_ns_ids()
        create_env = get_task_env_overlay()
        if create_env is None:
            create_env = effective_tip(sid, aid)
        model = self._create_model(config_base, create_env or None)

        await loop.run_in_executor(
            None,
            lambda: self._init_agent_instance_sync(_AgentInitContext(
                config=config,
                config_base=config_base,
                mode=mode,
                model=model,
                agent_card=agent_card,
                tool_cards=tool_cards,
                extra_skill_dir=extra_skill_dir,
            )),
        )
        logger.info(
            "[JiuWenClawDeepAdapter] 初始化完成: agent_name=%s agent_card_id=%s",
            self._agent_name,
            agent_card_id,
        )

        self._sync_preinstance_runtime_tools_to_ability_manager()
        self._sync_multimodal_tools_for_runtime()

        # 动态加载用户自定义的 Rail 扩展
        await self.load_user_rails()

        await self._abort_active_subagents("adapter_create_instance")

        # Initialize fork_agent tools
        await loop.run_in_executor(None, self._init_subagent_tools)
        await loop.run_in_executor(None, self._init_skill_turbo_tool)

        cfg = self._latest_config_base if isinstance(self._latest_config_base, dict) else get_config()
        sid, aid = self._tenant_disk_ids()
        self._embed_fingerprint = self._embed_config_fingerprint(cfg)
        self._memory_cache_fingerprint = memory_cache_fingerprint(cfg)
        self._task_memory_fingerprint = task_memory_config_fingerprint(cfg)
        self._memory_engine_snapshot = get_memory_engine(cfg)
        self._context_engine_config_fp = self._context_engine_config_fingerprint(cfg)
        self._external_memory_fingerprint = (
            external_memory_fingerprint(cfg, service_id=sid, agent_id=aid)
            if is_external_memory_enabled(cfg)
            else None
        )

    def _init_subagent_tools(self) -> None:
        """Initialize fork_agent and spawn_subagent tools for creating subagents."""
        try:
            from openjiuwen.core.runner import Runner as RunnerClass
            from jiuwenclaw.agentserver.tools.subagent_executor import create_fork_agent_executor
            from jiuwenclaw.agentserver.tools.subagent_tools import fork_agent, spawn_subagent

            self._fork_agent_executor = create_fork_agent_executor(
                self._instance,
                model=self._model,
                default_role_prompts=None,
                resolve_model=self._resolve_model_for_subagent,
            )
            if self._stream_event_rail is not None:
                self._stream_event_rail.set_fork_agent_executor(self._fork_agent_executor)

            # Register fork_agent tool (ignore if already exists)
            try:
                RunnerClass.resource_mgr.add_tool(fork_agent)
            except Exception as e:
                if "already exist" not in str(e):
                    logger.warning("[JiuWenClawDeepAdapter] Failed to register fork_agent tool: %s", e)
            self._instance.ability_manager.add(fork_agent.card)

            # Register spawn_subagent tool (ignore if already exists)
            try:
                RunnerClass.resource_mgr.add_tool(spawn_subagent)
            except Exception as e:
                if "already exist" not in str(e):
                    logger.warning("[JiuWenClawDeepAdapter] Failed to register spawn_subagent tool: %s", e)
            self._instance.ability_manager.add(spawn_subagent.card)

            logger.info("[JiuWenClawDeepAdapter] Fork agent and spawn_subagent tools initialized")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] Failed to initialize subagent tools: %s", exc)

    def _get_fork_agent_executor(self) -> Any | None:
        """Return this adapter's local subagent executor."""
        return getattr(self, "_fork_agent_executor", None)

    def _init_skill_turbo_tool(self) -> None:
        """Initialize skill_turbo tool for SkillTurbo integration."""
        if self._instance is None:
            return
        try:
            config_base = get_config()
            react_config = config_base.get("react", {}) if isinstance(config_base, dict) else {}
            skill_turbo_config = react_config.get("skill_turbo", {}) if isinstance(react_config, dict) else {}
            # 检查 SkillTurbo 是否启用
            enabled = skill_turbo_config.get("enabled", False) if isinstance(skill_turbo_config, dict) else False
            if not enabled:
                logger.info("[JiuWenClawDeepAdapter] SkillTurbo disabled, skipping tool registration")
                return

            from openjiuwen.core.runner import Runner as RunnerClass
            from jiuwenclaw.agentserver.skill_turbo.skill_turbo_tools import get_skill_turbo_tools

            for tool in get_skill_turbo_tools():
                try:
                    RunnerClass.resource_mgr.add_tool(tool)
                except Exception as e:
                    if "already exist" not in str(e):
                        logger.warning("[JiuWenClawDeepAdapter] Failed to register skill_turbo tool: %s", e)
                        continue
                self._instance.ability_manager.add(tool.card)

            # 注入 adapter 到 StreamEventRail
            if self._stream_event_rail is not None:
                self._stream_event_rail.set_skill_turbo_adapter(self)
                if self._checkpointer is not None:
                    self._stream_event_rail.set_checkpointer(self._checkpointer)

            logger.info("[JiuWenClawDeepAdapter] skill_turbo tool initialized")
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] Failed to initialize skill_turbo tool: %s", exc)

    async def cleanup(self) -> None:
        """Abort active subagents and release the local executor."""
        await self._abort_active_subagents("adapter_cleanup")
        self._fork_agent_executor = None
        if self._stream_event_rail is not None:
            self._stream_event_rail.set_fork_agent_executor(None)
        # Transitional no-op (was circuit_breaker_rail.cleanup_all()).
        self._cleanup_circuit_breaker_session(None)
        # Release the provider so the lambda registered in __init__ stops
        # capturing `self` and the adapter can be garbage-collected.
        set_skill_credential_provider(None)
        runtime_tools = (
            list(self._vision_tools or [])
            + list(self._audio_tools or [])
            + list(self._image_gen_tools or [])
        )
        if runtime_tools:
            self._remove_registered_tools(runtime_tools)
        self._cleanup_qualified_runtime_tools()
        # 显式释放 checkpointer 的 SQLAlchemy AsyncEngine 连接池，
        # 避免 SQLite 连接泄漏（SDK 未提供 close 方法）。
        if self._checkpointer is not None:
            kv_store = getattr(self._checkpointer, "_kv_store", None)
            if kv_store is not None:
                engine = getattr(kv_store, "engine", None)
                if engine is not None:
                    try:
                        await engine.dispose()
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenClawDeepAdapter] checkpointer engine dispose failed: %s", exc
                        )
            self._checkpointer = None

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
                            "[JiuWenClawDeepAdapter] 用户 Rail 扩展加载失败: %s, 错误: %s",
                            ext["name"],
                            e,
                        )
        except Exception as e:
            logger.error("[JiuWenClawDeepAdapter] 加载用户 Rail 扩展时发生错误: %s", e)

    def _env_ns_ids(self) -> tuple[str, str]:
        """Request-side (service_id, agent_id) for tip/ns bags."""
        sid = getattr(self, "_env_service_id", None) or getattr(self, "_service_id", None) or "default"
        aid = getattr(self, "_env_agent_id", None) or getattr(self, "_agent_id", None) or "default"
        return str(sid), str(aid)

    async def _maybe_apply_pending_reload(self) -> ReloadResult | None:
        if self._pending_reload is None or self._adapter_is_working():
            return None
        async with self._reload_lock:
            pending = self._pending_reload
            if pending is None or self._adapter_is_working():
                return None
            config_base, env_overrides, invalidate_memory = pending
            try:
                result = await self.reload_agent_config(
                    config_base,
                    env_overrides,
                    _force_apply=True,
                    _invalidate_memory_cache=invalidate_memory,
                )
            except Exception:
                if self._pending_reload is None:
                    self._pending_reload = pending
                raise
            if result.applied:
                self._pending_reload = None
                sid, aid = self._env_ns_ids()
                promote_staged_env(service_id=sid, agent_id=aid)
                try:
                    from jiuwenclaw.agentserver.tools.browser_tools import (
                        notify_browser_runtime_after_reload,
                    )

                    notify_browser_runtime_after_reload(
                        idle=True,
                        service_id=sid,
                        agent_id=aid,
                    )
                except Exception:
                    logger.exception(
                        "[JiuWenClawDeepAdapter] browser runtime notify after pending reload failed"
                    )
            return result

    async def apply_pending_reload_if_idle(self) -> ReloadResult | None:
        """Apply deferred reload when harness has verified no inflight streams.

        Unlike ``_maybe_apply_pending_reload``, does not consult ``working_checker``
        (which includes the current stream's inflight slot).
        """
        if self._pending_reload is None:
            return None
        async with self._reload_lock:
            pending = self._pending_reload
            if pending is None:
                return None
            config_base, env_overrides, invalidate_memory = pending
            try:
                result = await self.reload_agent_config(
                    config_base,
                    env_overrides,
                    _force_apply=True,
                    _invalidate_memory_cache=invalidate_memory,
                )
            except Exception:
                if self._pending_reload is None:
                    self._pending_reload = pending
                raise
            if result.applied:
                self._pending_reload = None
                sid, aid = self._env_ns_ids()
                promote_staged_env(service_id=sid, agent_id=aid)
                try:
                    from jiuwenclaw.agentserver.tools.browser_tools import (
                        notify_browser_runtime_after_reload,
                    )

                    notify_browser_runtime_after_reload(
                        idle=True,
                        service_id=sid,
                        agent_id=aid,
                    )
                except Exception:
                    logger.exception(
                        "[JiuWenClawDeepAdapter] browser runtime notify after idle pending reload failed"
                    )
            return result

    async def _on_chat_request_start(
        self,
    ) -> tuple[Token | None, Token | None, Token | None, Token | None, Any | None]:
        """Bind request-scoped ContextVars and return tokens for paired reset.

        Returns:
            ``(overlay_token, fp_token, skill_dirs_token, ns_token, browser_pin)``.
            All tokens/pins must be passed back to ``_on_chat_request_end`` on the
            same request — never stored on the shared adapter instance (team
            follow-ups use ``asyncio.create_task`` with a copied Context).
        """
        await self._maybe_apply_pending_reload()
        if not self._registered_skill_dirs:
            self._sync_registered_skill_dirs_snapshot()
        skill_dirs_token: Token | None = None
        if self._registered_skill_dirs:
            skill_dirs_token = bind_session_registered_skill_dirs(
                self._registered_skill_dirs
            )
        sid, aid = self._env_ns_ids()
        ns_token = bind_agent_env_ns(sid, aid)
        # Formula B full tip; always bind (incl. {}) — seal S1
        overlay = build_effective_env_overlay(service_id=sid, agent_id=aid)
        env_token = bind_task_env_overlay(overlay)
        fp_token: Token | None = None
        if self._memory_cache_fingerprint:
            fp_token = bind_memory_cache_fingerprint(self._memory_cache_fingerprint)
        # Pin browser runtime generation after tip/ns bind so this request keeps
        # the credential generation captured at start across mid-request reloads.
        browser_pin: Any | None = None
        try:
            from jiuwenclaw.agentserver.tools.browser_tools import (
                pin_browser_runtime_generation,
            )

            browser_pin = pin_browser_runtime_generation(
                service_id=sid,
                agent_id=aid,
            )
        except Exception:
            logger.exception(
                "[JiuWenClawDeepAdapter] pin browser runtime generation failed"
            )
        return env_token, fp_token, skill_dirs_token, ns_token, browser_pin

    async def _on_chat_request_end(
            self,
            overlay_token: Token | None,
            fp_token: Token | None = None,
            skill_dirs_token: Token | None = None,
            ns_token: Token | None = None,
            browser_pin: Any | None = None,
    ) -> None:
        if overlay_token is not None:
            reset_task_env_overlay(overlay_token)
        if ns_token is not None:
            reset_agent_env_ns(ns_token)
        if browser_pin is not None:
            try:
                from jiuwenclaw.agentserver.tools.browser_tools import (
                    reset_browser_runtime_generation,
                )

                reset_browser_runtime_generation(browser_pin)
            except Exception:
                logger.exception(
                    "[JiuWenClawDeepAdapter] reset browser runtime generation failed"
                )
        if fp_token is not None:
            reset_memory_cache_fingerprint(fp_token)
        if skill_dirs_token is not None:
            reset_session_registered_skill_dirs(skill_dirs_token)
        await self._maybe_apply_pending_reload()

    def _refresh_fork_agent_executor_model(self) -> None:
        if self._fork_agent_executor is not None and self._model is not None:
            self._fork_agent_executor.set_model(self._model)
        if self._stream_event_rail is not None:
            self._stream_event_rail.set_fork_agent_executor(self._fork_agent_executor)

    async def reload_agent_config(
            self,
            config_base: dict[str, Any] | None = None,
            env_overrides: dict[str, Any] | None = None,
            *,
            _force_apply: bool = False,
            _invalidate_memory_cache: bool | None = None,
    ) -> ReloadResult:
        """从 config.yaml 重新加载配置，通过 DeepAgent.configure() 热更新当前实例（不新建 DeepAgent）。

        When the adapter is busy, stores a pending reload and returns deferred=True.
        Apply runs when idle (task finally or next chat entry).
        """
        if self._instance is None:
            raise RuntimeError("JiuWenClawDeepAdapter 未初始化，请先调用 create_instance()")

        # When env_overrides is None/empty (e.g., _reload_after_agents_sync or
        # BEFORE_SYSTEM_PROMPT_BUILD), fall back to the last sync env so that
        # patch_model_config_from_env applies the correct model_name / api_key
        # instead of leaving config's ${MODEL_NAME} placeholder unresolved.
        if not isinstance(env_overrides, dict) or not env_overrides:
            env_overrides = self._last_sync_env if isinstance(self._last_sync_env, dict) else env_overrides

        if _invalidate_memory_cache is None:
            invalidate_memory_cache = self._env_touches_memory(env_overrides)
        else:
            invalidate_memory_cache = _invalidate_memory_cache

        if _force_apply:
            # Authoritative apply (sync / pending drain): drop stale Gateway pending.
            self._pending_reload = None
        else:
            pending = (config_base, env_overrides, invalidate_memory_cache)
            if self._adapter_is_working():
                self._pending_reload = pending
                logger.info("[JiuWenClawDeepAdapter] reload deferred (adapter working)")
                return ReloadResult(deferred=True)
            async with self._reload_lock:
                if self._adapter_is_working():
                    self._pending_reload = pending
                    return ReloadResult(deferred=True)
                self._pending_reload = None

        sid, aid = self._env_ns_ids()
        ns_token = bind_agent_env_ns(sid, aid)
        overlay = build_effective_env_overlay(
            env_overrides,
            service_id=sid,
            agent_id=aid,
        )
        overlay_token = bind_task_env_overlay(overlay)
        try:
            if env_overrides is not None and not isinstance(env_overrides, dict):
                raise TypeError("env_overrides must be a dict when provided")

            clear_config_cache()
            clear_global_config_cache()

            if config_base is None:
                config_base = get_config()
            elif not isinstance(config_base, dict):
                raise TypeError("config_base must be a dict when provided")
            else:
                # Gateway 可能只传入部分配置（如 logging + preferred_language），
                # 需要与完整的基础配置合并，否则丢失 react.disabled_tools 等关键字段。
                full_config = get_config()
                merged = deep_merge_dicts(full_config, resolve_env_vars(config_base))
                config_base = merged

            live_skill_envs = (
                self._skill_credential_injection_rail.get_skill_envs()
                if getattr(self, "_skill_credential_injection_rail", None) is not None
                else None
            )
            previous_for_skill_envs = (
                {"react": {"skill_envs": live_skill_envs}}
                if live_skill_envs
                else getattr(self, "_latest_config_base", None)
            )
            config_base = coalesce_config_skill_envs(config_base, previous_for_skill_envs)

            self._latest_config_base = config_base if isinstance(config_base, dict) else None

            # 把 config_base['sandbox'] 的 url/type/enabled 翻译为 env overlay key,
            # 让 _create_sys_operation 经 get_sandbox_endpoint/get_sandbox_runtime 读到。
            # 注意: 始终从 get_config() 读取 sandbox 配置, 而非 config_base 参数,
            # 避免 agent_manager 重放时传入旧 _latest_config_base 覆盖前台 RPC 设置的 sandbox 值。
            current_config = get_config()
            sandbox_yaml = (
                current_config.get("sandbox") or {}
                if isinstance(current_config, dict) else {}
            )
            sandbox_overlay = _sandbox_yaml_to_env_overlay(sandbox_yaml)
            if sandbox_overlay:
                reset_task_env_overlay(overlay_token)
                overlay = build_effective_env_overlay(
                    env_overrides,
                    sandbox_overlay,
                    service_id=sid,
                    agent_id=aid,
                )
                overlay_token = bind_task_env_overlay(overlay)
            new_embed_fp = self._embed_config_fingerprint(config_base)
            new_memory_fp = memory_cache_fingerprint(config_base)
            new_task_fp = task_memory_config_fingerprint(config_base)

            # Memory INDEX_CACHE is process-global, keyed by agent_id+workspace+fp.
            # Use request-side env_agent_id so init / invalidate / acquire share one key.
            need_invalidate_memory = bool(invalidate_memory_cache) or (
                self._embed_fingerprint is not None
                and new_embed_fp != self._embed_fingerprint
            ) or (
                self._memory_cache_fingerprint is not None
                and new_memory_fp != self._memory_cache_fingerprint
            )
            if need_invalidate_memory:
                _, memory_agent_id = self._env_ns_ids()
                workspace_dir = str(getattr(self, "_workspace_dir", None) or ".")
                await invalidate_memory_manager_cache(memory_agent_id, workspace_dir)
                await invalidate_memory_wiki_manager_cache(memory_agent_id, workspace_dir)
                logger.info(
                    "[JiuWenClawDeepAdapter] Scoped memory cache invalidated "
                    "agent_id=%s workspace_dir=%s memory_fp=%s",
                    memory_agent_id,
                    workspace_dir,
                    new_memory_fp,
                )

            # TaskMemoryService pool is keyed by (service_id, agent_id, fingerprint).
            # Invalidate only this tenant, and only when task-memory config/env changes.
            need_invalidate_task_memory = bool(env_touches_task_memory(env_overrides)) or (
                self._task_memory_fingerprint is not None
                and new_task_fp != self._task_memory_fingerprint
            )
            if need_invalidate_task_memory:
                task_sid, task_aid = self._env_ns_ids()
                clear_task_memory_service(service_id=task_sid, agent_id=task_aid)
                logger.info(
                    "[JiuWenClawDeepAdapter] Scoped task memory cache invalidated "
                    "service_id=%s agent_id=%s",
                    task_sid,
                    task_aid,
                )

            # 同步扩展配置到 ExtensionRegistry
            # Gateway 已解密 extension_security_configs，AgentServer 直接使用明文
            try:
                from jiuwenclaw.extensions.registry import ExtensionRegistry
                registry = ExtensionRegistry.get_instance()
                registry.update_config(config_base)
                logger.info("[JiuWenClaw] Extension config synced to Registry")
            except Exception as exc:
                logger.warning("[JiuWenClaw] ExtensionRegistry update failed: %s", exc)

            self._refresh_multimodal_configs(config_base)
            config = config_base.get('react', {}).copy()
            self._config_cache = config.copy()

            model = self._create_model(config_base, env_overrides)
            self._model = model
            if isinstance(env_overrides, dict) and env_overrides:
                self._last_sync_env = dict(env_overrides)
            self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))
            agent_card_id = self._resolve_agent_card_id()
            agent_card = AgentCard(name=self._agent_name, id=agent_card_id)
            self._sync_multimodal_tools_for_runtime()

            self._sync_registered_skill_dirs_snapshot()
            if env_touches_shared_skills_dirs(env_overrides):
                logger.info(
                    "[JiuWenClawDeepAdapter] registered skill dirs updated on reload: %s",
                    self._registered_skill_dirs,
                )

            rails_list, rails_to_unregister = (
                await self._get_current_agent_rails(config, config_base)
            )

            # 加载用户自定义的 Rail 扩展
            await self.load_user_rails()

            disabled_names = set(
                resolve_string_or_list_config(config.get("disabled_tools"))
            )
            filtered_tool_cards = [
                card for card in (self._tool_cards or [])
                if card.name not in disabled_names
            ]

            # sandbox 配置可能随 reload 变更；按指纹重建 _sys_operation 让新 url/type/enabled 生效。
            self._maybe_recreate_sys_operation()

            deep_cfg = self._make_deep_agent_config(
                model=model,
                config=config,
                agent_card=agent_card,
                tool_cards=filtered_tool_cards,
                rails=rails_list,
            )
            logger.info(
                "[JiuWenClawDeepAdapter]  DIAGNOSTIC: 准备调用 Agent.configure() "
                "| rails_count=%d "
                "| rails_names=[%s] "
                "| deep_cfg.rails 配置完成",
                len(rails_list),
                ", ".join(type(r).__name__ for r in rails_list),
            )

            self._instance.configure(deep_cfg)
            # DeepAgent.configure() 走 _hot_reconfigure, 需要手工同步, 否则同 session 继续 chat 时
            # ReActAgent 仍持有旧 sysop id (已被 remove), 表现为无权限.
            self._sync_sysop_to_react_agent()
            if self._last_runtime_mode == "code":
                await self._reload_code_mode_memory_rails(config_base)
            rail_cache_attrs = (
                "_progressive_tool_rail",
                "_skill_evolution_rail",
                "_filesystem_rail",
            )
            first_unregister_error: Exception | None = None
            for rail in rails_to_unregister:
                try:
                    await self._instance.unregister_rail(rail)
                except Exception as exc:
                    if first_unregister_error is None:
                        first_unregister_error = exc
                    logger.warning(
                        "[JiuWenClawDeepAdapter] %s unregister on reload failed: %s",
                        type(rail).__name__,
                        exc,
                    )
                    continue
                for attr_name in rail_cache_attrs:
                    if getattr(self, attr_name, None) is rail:
                        setattr(self, attr_name, None)
                        break
                logger.info(
                    "[JiuWenClawDeepAdapter] %s unregistered on reload",
                    type(rail).__name__,
                )
            # configure() rebuilds ability_manager from tool_cards; multimodal tools
            # registered before configure are dropped — re-sync after configure.
            self._sync_multimodal_tools_for_runtime()
            self._rebind_progressive_tool_rail_after_reload()
            self._apply_model_to_react_agent(self._model)
            self._refresh_fork_agent_executor_model()

            reload_mode = self._last_runtime_mode or "agent.plan"
            engine_changed = (
                self._memory_engine_snapshot is not None
                and get_memory_engine(config_base) != self._memory_engine_snapshot
            )
            if engine_changed and self._memory_rail is not None:
                try:
                    await self._instance.unregister_rail(self._memory_rail)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] MemoryRail unregister on engine change failed: %s",
                        exc,
                    )
                self._memory_rail = None

            await self._handle_memory_rail_by_config(reload_mode, config_base)
            await self._handle_external_memory_rail_by_config(config_base)
            self._apply_registered_skill_dirs_to_runtime_rails()
            self._memory_engine_snapshot = get_memory_engine(config_base)

            if need_invalidate_memory:
                logger.info(
                    "[JiuWenClawDeepAdapter] Memory cache fingerprint updated: %s",
                    new_memory_fp,
                )
            self._memory_cache_fingerprint = new_memory_fp
            self._embed_fingerprint = new_embed_fp
            self._task_memory_fingerprint = new_task_fp

            # configure() has already applied; finish runtime reconciliation before
            # reporting an incomplete rail cleanup to the caller.
            if first_unregister_error is not None:
                raise first_unregister_error

            logger.info("[JiuWenClawDeepAdapter] 配置已热更新（configure），未重启进程")
            return ReloadResult(applied=True)
        finally:
            reset_task_env_overlay(overlay_token)
            reset_agent_env_ns(ns_token)

    def _bind_runtime_cron_context(
            self,
            *,
            channel_id: str | None,
            session_id: str | None,
            metadata: dict[str, Any] | None,
            request_id: str | None,
            mode: str | None,
    ) -> tuple[Token[str], Token[str | None], Token[dict[str, Any] | None], Token[str | None], Any]:
        from jiuwenclaw.agentserver import plan_todo_context as _plan_todo

        normalized_channel = str(channel_id or "").strip() or CronTargetChannel.WEB.value
        normalized_mode = str(mode).strip() if isinstance(mode, str) and mode.strip() else None
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else None
        if normalized_metadata is None:
            normalized_metadata = {}
        if isinstance(request_id, str) and request_id.strip():
            normalized_metadata["request_id"] = request_id.strip()
        sid, aid = self._tenant_disk_ids()
        normalized_metadata.setdefault("service_id", sid)
        normalized_metadata.setdefault("agent_id", aid)
        # 设置 DeepResearch 路由上下文
        dr_token = push_deepresearch_route(
            request_id=request_id or "",
            channel_id=normalized_channel,
            session_id=session_id or "",
            service_id=str(sid),
            agent_id=str(aid),
        )
        return (
            _CRON_TOOL_CHANNEL_ID.set(normalized_channel),
            _CRON_TOOL_SESSION_ID.set(session_id),
            _CRON_TOOL_METADATA.set(normalized_metadata),
            _CRON_TOOL_MODE.set(normalized_mode),
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
        _CRON_TOOL_MODE.reset(mode_token)
        _CRON_TOOL_METADATA.reset(metadata_token)
        _CRON_TOOL_SESSION_ID.reset(session_token)
        _CRON_TOOL_CHANNEL_ID.reset(channel_token)
        # 重置 DeepResearch 路由上下文
        reset_deepresearch_route(dr_token)

    async def _register_code_mode_rails(self) -> None:
        """Register Code-only rails when an existing agent switches to code."""
        if self._instance is None or self._code_mode_rails_active:
            return

        reusable_task_planning: TaskPlanningRail | None = None
        if self._task_planning_rail is not None:
            try:
                await self._instance.unregister_rail(self._task_planning_rail)
            except Exception as exc:  # noqa: BLE001 - mode transition boundary
                logger.warning(
                    "[JiuWenClawDeepAdapter] TaskPlanningRail unregister before "
                    "Code-mode registration failed: %s",
                    exc,
                )
                reusable_task_planning = self._task_planning_rail
            else:
                self._task_planning_rail = None

        config_base = getattr(self, "_latest_config_base", None) or get_config()
        new_rails: list[Any] = []
        lsp_rail = self._build_lsp_rail()
        if lsp_rail is not None:
            new_rails.append(lsp_rail)
        new_rails.extend(
            build_code_mode_extra_rails(
                self,
                config_base,
                project_dir=self._instance_overrides.get("project_dir"),
                workspace_dir=self._workspace_dir,
                language=self._resolve_runtime_language(),
            )
        )
        if reusable_task_planning is not None:
            new_rails = [
                rail for rail in new_rails if not isinstance(rail, TaskPlanningRail)
            ]

        registered: list[Any] = (
            [reusable_task_planning] if reusable_task_planning is not None else []
        )
        for rail in new_rails:
            try:
                await self._instance.register_rail(rail)
            except Exception as exc:  # noqa: BLE001 - rail isolation boundary
                logger.warning(
                    "[JiuWenClawDeepAdapter] Code-mode rail %s register failed: %s",
                    type(rail).__name__,
                    exc,
                )
                continue
            registered.append(rail)

        self._code_mode_rails = [
            rail for rail in registered if not isinstance(rail, LspRail)
        ]
        self._lsp_rail = next(
            (rail for rail in registered if isinstance(rail, LspRail)),
            None,
        )
        self._project_memory_rail = next(
            (rail for rail in registered if isinstance(rail, ProjectMemoryRail)),
            None,
        )
        self._coding_memory_rail = next(
            (rail for rail in registered if isinstance(rail, CodingMemoryRail)),
            None,
        )
        self._task_planning_rail = next(
            (rail for rail in registered if isinstance(rail, TaskPlanningRail)),
            None,
        )
        self._code_mode_rails_active = bool(registered)
        if registered:
            self._code_mode_workspace = str(Path(self._workspace_dir or "./").resolve())
        logger.info(
            "[JiuWenClawDeepAdapter] Code-mode rails registered on mode switch: %s",
            [type(rail).__name__ for rail in registered],
        )

    async def _unregister_code_mode_rails(self) -> bool:
        """Unload Code-only rails before leaving code mode."""
        if self._instance is None:
            return True

        current = list(self._code_mode_rails)
        if self._lsp_rail is not None:
            current.append(self._lsp_rail)
        removed: list[Any] = []
        for rail in current:
            try:
                await self._instance.unregister_rail(rail)
            except Exception as exc:  # noqa: BLE001 - retain failed rail
                logger.warning(
                    "[JiuWenClawDeepAdapter] Code-mode rail %s unregister failed: %s",
                    type(rail).__name__,
                    exc,
                )
                for removed_rail in reversed(removed):
                    try:
                        await self._instance.register_rail(removed_rail)
                    except Exception as rollback_exc:  # noqa: BLE001 - preserve state
                        logger.error(
                            "[JiuWenClawDeepAdapter] Code-mode rail %s rollback "
                            "register failed: %s",
                            type(removed_rail).__name__,
                            rollback_exc,
                        )
                logger.error(
                    "[JiuWenClawDeepAdapter] Code-mode rail cleanup aborted; "
                    "retaining the previous rail state"
                )
                return False
            removed.append(rail)

        self._code_mode_rails = []
        self._lsp_rail = None
        self._project_memory_rail = None
        self._coding_memory_rail = None
        self._task_planning_rail = None
        self._code_mode_rails_active = False
        self._code_mode_workspace = None
        return True

    async def _reload_code_mode_memory_rails(
        self,
        config_base: dict[str, Any],
    ) -> None:
        """Replace Code memory rails with transactional registration semantics."""
        if self._instance is None or not self._code_mode_rails_active:
            return

        current_memory = [
            rail
            for rail in (self._project_memory_rail, self._coding_memory_rail)
            if rail is not None
        ]
        current_coding_memory = self._coding_memory_rail

        # Build the replacement without allowing the factory to overwrite the
        # reference to a rail that is still registered in the live instance.
        workspace_changed = (
            current_coding_memory is not None
            and not self._coding_memory_matches_workspace(
                current_coding_memory,
                self._workspace_dir,
            )
        )
        if workspace_changed:
            self._coding_memory_rail = None
        try:
            desired = build_code_mode_extra_rails(
                self,
                config_base,
                project_dir=self._instance_overrides.get("project_dir"),
                workspace_dir=self._workspace_dir,
                language=self._resolve_runtime_language(),
            )
        finally:
            self._coding_memory_rail = current_coding_memory

        desired_memory = [
            rail
            for rail in desired
            if isinstance(rail, (ProjectMemoryRail, CodingMemoryRail))
        ]

        to_remove = [rail for rail in current_memory if rail not in desired_memory]
        to_add = [rail for rail in desired_memory if rail not in current_memory]
        removed: list[Any] = []
        added: list[Any] = []

        async def rollback() -> None:
            for rail in reversed(added):
                try:
                    await self._instance.unregister_rail(rail)
                except Exception as exc:  # noqa: BLE001 - preserve original failure
                    logger.error(
                        "[JiuWenClawDeepAdapter] rollback unregister %s failed: %s",
                        type(rail).__name__,
                        exc,
                    )
            for rail in removed:
                try:
                    await self._instance.register_rail(rail)
                except Exception as exc:  # noqa: BLE001 - preserve original failure
                    logger.error(
                        "[JiuWenClawDeepAdapter] rollback register %s failed: %s",
                        type(rail).__name__,
                        exc,
                    )

        try:
            for rail in to_remove:
                await self._instance.unregister_rail(rail)
                removed.append(rail)
            for rail in to_add:
                await self._instance.register_rail(rail)
                added.append(rail)
        except Exception as exc:  # noqa: BLE001 - transactional rail boundary
            await rollback()
            logger.error(
                "[JiuWenClawDeepAdapter] Code memory reload aborted; "
                "old rail references retained: %s",
                exc,
            )
            return

        self._project_memory_rail = next(
            (rail for rail in desired_memory if isinstance(rail, ProjectMemoryRail)),
            None,
        )
        self._coding_memory_rail = next(
            (rail for rail in desired_memory if isinstance(rail, CodingMemoryRail)),
            None,
        )
        non_memory = [
            rail
            for rail in self._code_mode_rails
            if not isinstance(rail, (ProjectMemoryRail, CodingMemoryRail))
        ]
        self._code_mode_rails = non_memory + desired_memory
        self._code_mode_workspace = str(Path(self._workspace_dir or "./").resolve())
        logger.info(
            "[JiuWenClawDeepAdapter] Code memory rails reloaded: %s",
            [type(rail).__name__ for rail in desired_memory],
        )

    @staticmethod
    def _normalize_rail_mode(mode: str) -> str:
        """Normalize Code submodes for shared rail lifecycle handling."""
        normalized = str(mode or "").strip().lower()
        if normalized in {"code.plan", "code.normal"}:
            return "code"
        return normalized

    async def _update_rails_for_mode(
        self,
        mode: str,
        *,
        session_id: str | None = None,
        sync_code_submode: bool = True,
    ) -> None:
        """按 mode 注册或卸载 rails。"""
        requested_mode = mode
        mode = self._normalize_rail_mode(mode)
        if mode == "code":
            await self._register_code_mode_rails()
            if sync_code_submode:
                self._sync_code_submode_to_rails(
                    requested_mode,
                    session_id=session_id,
                )
        else:
            if not await self._unregister_code_mode_rails():
                error_msg = (
                    "Mode transition aborted because Code-mode rail cleanup "
                    "was incomplete"
                )
                # process_message_impl/process_message_stream_impl publish the
                # requested mode before runtime reconciliation so a pending
                # config reload sees the current request.  The transition did
                # not commit, so restore the last known rail mode before
                # failing the request and blocking all downstream mode setup.
                self._last_runtime_mode = "code"
                logger.error("[JiuWenClawDeepAdapter] %s", error_msg)
                raise RuntimeError(error_msg)
            if mode == "agent.plan":
                await self._update_plan_mode_rails()
            else:
                await self._update_agent_mode_rails()

    def _sync_code_submode_to_rails(
        self,
        mode: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Forward the raw Code submode to the active mode rail.

        Rail lifecycle uses canonical ``code`` mode, but plan-vs-normal is a
        session state consumed by ``CodeAgentModeRail``. Keep this handoff at
        the request boundary so cold start and hot switching share it.
        """
        if self._normalize_rail_mode(mode) != "code":
            return
        for rail in getattr(self, "_code_mode_rails", []):
            setter = getattr(rail, "set_requested_mode", None)
            if callable(setter):
                setter(mode, session_id=session_id)

    async def _register_mode_rail(
        self,
        attr_name: str,
        rail: Any,
        label: str,
    ) -> bool:
        """Register a mode rail before publishing its cached reference."""
        try:
            await self._instance.register_rail(rail)
        except Exception as exc:  # noqa: BLE001 - mode rail boundary
            logger.warning(
                "[JiuWenClawDeepAdapter] %s register failed: %s",
                label,
                exc,
            )
            return False
        setattr(self, attr_name, rail)
        logger.info("[JiuWenClawDeepAdapter] %s registered", label)
        return True

    async def _update_plan_mode_rails(self) -> None:
        """plan 模式：注册 plan 专属 rails，卸载 agent 专属资源。"""
        if self._task_planning_rail is None:
            task_planning_rail = self._build_task_planning_rail()
            if task_planning_rail is not None:
                await self._register_mode_rail(
                    "_task_planning_rail",
                    task_planning_rail,
                    "TaskPlanningRail for plan mode",
                )
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
                context_rail = _build_context_engineering_rail(
                    self._config_cache, mode="agent.plan")
                if context_rail is not None and await self._register_mode_rail(
                    "_context_engineering_rail",
                    context_rail,
                    "ContextEngineeringRail for plan mode",
                ):
                    self._context_engineering_rail_mode = "agent.plan"
        elif self._context_engineering_rail is not None:
            await self._instance.unregister_rail(self._context_engineering_rail)
            self._context_engineering_rail = None
            self._context_engineering_rail_mode = None
            logger.info("[JiuWenClawDeepAdapter] ContextEngineeringRail unregistered for plan mode (disabled)")
        # 恢复自演进 rail（仅配置启用时）
        if self._skill_evolution_rail is None and self._config_cache.get("evolution", {}).get("enabled", False):
            evolution_rail = self._build_skill_evolution_rail(self._config_cache)
            if evolution_rail is not None:
                await self._register_mode_rail(
                    "_skill_evolution_rail",
                    evolution_rail,
                    "SkillEvolutionRail for plan mode",
                )
        # 已使用subagent tool替代subagent rail
        if self._subagent_rail is None:
            self._subagent_rail = self._build_subagent_rail()
            if self._subagent_rail is not None:
                await self._instance.unregister_rail(self._subagent_rail)
                logger.info("[JiuWenClawDeepAdapter] SubagentRail unregistered for plan mode")
        # plan 模式下注册 skill 合规相关 rail
        if self._skill_protocol_prompt_rail is None:
            skill_protocol_rail = self._build_skill_protocol_prompt_rail()
            if skill_protocol_rail is not None:
                await self._register_mode_rail(
                    "_skill_protocol_prompt_rail",
                    skill_protocol_rail,
                    "SkillProtocolPromptRail for plan mode",
                )
        if self._skill_compliance_rail is None:
            skill_compliance_rail = self._build_skill_compliance_rail()
            if skill_compliance_rail is not None:
                await self._register_mode_rail(
                    "_skill_compliance_rail",
                    skill_compliance_rail,
                    "SkillComplianceRail for plan mode",
                )
        if self._skill_credential_injection_rail is None:
            credential_rail = self._build_skill_credential_injection_rail(
                self._config_cache
            )
            if credential_rail is not None:
                await self._register_mode_rail(
                    "_skill_credential_injection_rail",
                    credential_rail,
                    "SkillCredentialInjectionRail for plan mode",
                )
        await self._handle_qa_block_rails_for_plan()

    async def _handle_qa_block_rails_for_plan(self) -> None:
        react_cfg = self._config_cache if isinstance(self._config_cache, dict) else {}
        context_engine_cfg = react_cfg.get("context_engine_config", {})
        session_memory = _resolve_session_memory_for_context_rail(context_engine_cfg)

        if _resolve_qa_block_config(react_cfg) is None:
            if self._qa_block_freeze_rail is not None:
                await self._instance.unregister_rail(self._qa_block_freeze_rail)
                self._qa_block_freeze_rail = None
                logger.info("[JiuWenClawDeepAdapter] QABlockFreezeRail unregistered (disabled)")
            if self._qa_block_assembly_rail is not None:
                await self._instance.unregister_rail(self._qa_block_assembly_rail)
                self._qa_block_assembly_rail = None
                logger.info("[JiuWenClawDeepAdapter] QABlockAssemblyRail unregistered (disabled)")
            if self._qa_artifact_rail is not None:
                await self._instance.unregister_rail(self._qa_artifact_rail)
                self._qa_artifact_rail = None
                logger.info("[JiuWenClawDeepAdapter] QAArtifactRail unregistered (disabled)")
            self._wire_qa_artifact_to_freeze_rail()
            return
        if self._qa_block_freeze_rail is None:
            freeze_rail = self._build_qa_block_freeze_rail(react_cfg)
            if freeze_rail is not None:
                await self._register_mode_rail(
                    "_qa_block_freeze_rail",
                    freeze_rail,
                    "QABlockFreezeRail for plan mode",
                )
        if self._qa_block_assembly_rail is None:
            assembly_rail = self._build_qa_block_assembly_rail(react_cfg)
            if assembly_rail is not None:
                await self._register_mode_rail(
                    "_qa_block_assembly_rail",
                    assembly_rail,
                    "QABlockAssemblyRail for plan mode",
                )
        if self._qa_artifact_rail is None:
            artifact_rail = self._build_qa_artifact_rail(react_cfg, session_memory)
            if artifact_rail is not None:
                await self._register_mode_rail(
                    "_qa_artifact_rail",
                    artifact_rail,
                    "QAArtifactRail for plan mode",
                )
        self._wire_qa_artifact_to_freeze_rail()

    def _wire_qa_artifact_to_freeze_rail(self) -> None:
        freeze_rail = self._qa_block_freeze_rail
        if freeze_rail is None:
            return
        mgr = (
            self._qa_artifact_rail.qa_artifact_manager
            if self._qa_artifact_rail is not None
            else None
        )
        freeze_rail.attach_qa_artifact(mgr)
        assembly_rail = self._qa_block_assembly_rail
        if assembly_rail is not None:
            assembly_rail.attach_freeze_rail(freeze_rail)

    async def _handle_qa_block_freeze_rail_for_plan(self) -> None:
        await self._handle_qa_block_rails_for_plan()

    async def _update_agent_mode_rails(self) -> None:
        """agent 模式：卸载 plan 专属 rails，按需注册 agent 专属 rails。"""
        for attr, label in (
                ("_task_planning_rail", "TaskPlanningRail"),
                ("_skill_evolution_rail", "SkillEvolutionRail"),
                ("_subagent_rail", "SubagentRail"),
                ("_skill_protocol_prompt_rail", "SkillProtocolPromptRail"),
                ("_skill_compliance_rail", "SkillComplianceRail"),
                ("_skill_credential_injection_rail", "SkillCredentialInjectionRail"),
        ):
            rail = getattr(self, attr)
            if rail is not None:
                await self._instance.unregister_rail(rail)
                setattr(self, attr, None)
                logger.info("[JiuWenClawDeepAdapter] %s unregistered for agent mode", label)
        if self._qa_block_freeze_rail is not None:
            await self._instance.unregister_rail(self._qa_block_freeze_rail)
            self._qa_block_freeze_rail = None
            logger.info("[JiuWenClawDeepAdapter] QABlockFreezeRail unregistered for agent mode")
        if self._qa_block_assembly_rail is not None:
            await self._instance.unregister_rail(self._qa_block_assembly_rail)
            self._qa_block_assembly_rail = None
            logger.info("[JiuWenClawDeepAdapter] QABlockAssemblyRail unregistered for agent mode")
        if self._qa_artifact_rail is not None:
            await self._instance.unregister_rail(self._qa_artifact_rail)
            self._qa_artifact_rail = None
            logger.info("[JiuWenClawDeepAdapter] QAArtifactRail unregistered for agent mode")
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
                channel_id=_CRON_TOOL_CHANNEL_ID.get(),
                request_id=request_id,
                sub_agent_config=sub_agent_config,
                sub_agent_permission_rail_factory=self._build_fast_subagent_permission_rail,
                sub_agent_disabled_tools_rail_factory=self._build_fast_subagent_disabled_tools_rail,
            )
            if request_id:
                self._track_session_toolkit(request_id, session_id, self._multi_session_toolkit)
            for ms_tool in self._multi_session_toolkit.get_tools():
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
                logger.error(
                    f"[JiuWenClawDeepAdapter] 定时工具注册失败: request_id={request_id} error={str(exc)}",
                    extra={'user_visible': 'critical'}
                )
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

        send_file_channel_allowed = send_file_enabled or channel == "officeclaw"
        has_send_file_request_context = bool(request_id and session_id)
        if send_file_channel_allowed and has_send_file_request_context:
            # 先卸载上一次请求遗留的 send_file 工具
            for existing in list(self._instance.ability_manager.list() or []):
                if getattr(existing, "name", "").startswith("send_file_to_user"):
                    self._instance.ability_manager.remove(existing.name)
            send_file_toolkit = SendFileToolkit(
                request_id=request_id,
                session_id=session_id,
                channel_id=_CRON_TOOL_CHANNEL_ID.get(),
                metadata=_CRON_TOOL_METADATA.get(),
            )
            for sf_tool in send_file_toolkit.get_tools():
                Runner.resource_mgr.add_tool(sf_tool)
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

        md = dict(params.request_metadata or {})
        # 注入 request_id / channel_id，供 skill_turbo 等工具从 metadata 直接读取
        md["request_id"] = params.request_id or ""
        md["channel_id"] = params.channel_id or ""
        # 写入 ContextVar，供 skill_turbo 工具安全读取（避免实例属性并发覆盖）
        from jiuwenclaw.agentserver.skill_turbo.skill_turbo_tools import (
            set_current_request_metadata,
        )
        set_current_request_metadata(md)
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

        set_effective_request_workspace_dir(resolved_workspace_dir)

        # Sync the tool CWD layer to the client-provided workspace dir so that
        # relative file paths in tool calls resolve against the correct base.
        try:
            set_cwd(resolved_workspace_dir)
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] set_cwd(%s) failed: %s", resolved_workspace_dir, exc)

        # 设置 output_dir ContextVar（新增）
        # Agent 可在 effective_project_dir 工作但将输出文件保存至 output_dir
        from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
            set_effective_request_output_dir,
            get_effective_request_output_dir,
        )

        output_dir = md.get("output_dir")
        if isinstance(output_dir, str) and output_dir.strip():
            set_effective_request_output_dir(output_dir.strip())
            logger.info(
                "[JiuWenClawDeepAdapter] output_dir 设置完成: output_dir=%s "
                "(effective_project_dir=%s)",
                output_dir.strip(),
                resolved_workspace_dir
            )

            #  方案A：动态更新write_file工具描述，注入output_dir路径
            # 让Agent在工具选择阶段就能看到推荐的保存位置
            # 正确模式：ability_manager用tool_name，resource_mgr用tool_card.id
            try:
                # Step 1: 从 ability_manager 获取 ToolCard（使用工具名称）
                tool_card = self._instance.ability_manager.get("write_file")
                if not tool_card:
                    logger.warning("[JiuWenClawDeepAdapter] write_file 工具卡片未在 ability_manager 中注册")
                else:
                    # Step 2: 使用 tool_card.id 获取 Tool 对象
                    write_file_tool = Runner.resource_mgr.get_tool(tool_card.id)
                    if write_file_tool and hasattr(write_file_tool, 'card'):
                        # 更新工具描述，在参数说明中注入output_dir推荐路径
                        original_description = write_file_tool.card.description or ""
                        output_dir_hint = (
                            f"\n\n推荐保存路径：{output_dir.strip()}\n\n"
                            f"参数 file_path 推荐使用：{output_dir.strip()}/filename.ext"
                        )

                        # 创建新的描述（保留原描述 + 添加output_dir提示）
                        enhanced_description = original_description + output_dir_hint

                        # 更新工具卡片描述
                        write_file_tool.card.description = enhanced_description

                        logger.info(
                            "[JiuWenClawDeepAdapter] ✅ write_file工具描述已更新，注入output_dir路径: %s (tool_id=%s)",
                            output_dir.strip(),
                            tool_card.id
                        )
                    else:
                        logger.warning(
                            "[JiuWenClawDeepAdapter] write_file Tool对象未找到 (tool_id=%s)",
                            tool_card.id
                        )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] write_file工具描述更新失败: %s",
                    exc,
                    exc_info=True  # 打印完整堆栈
                )
        else:
            set_effective_request_output_dir(None)

        #  诊断日志：观察 RuntimePromptRail 实例状态
        logger.info(
            "[JiuWenClawDeepAdapter]  DIAGNOSTIC: RuntimePromptRail 状态检查 "
            "| request_id=%s "
            "| _runtime_prompt_rail is %s "
            "| output_dir ContextVar=%s "
            "| workspace_dir=%s",
            params.request_id,
            "None (未创建)" if self._runtime_prompt_rail is None else "已创建",
            get_effective_request_output_dir(),
            resolved_workspace_dir,
        )

        if self._runtime_prompt_rail:
            self._runtime_prompt_rail.set_language(resolved_language)
        if self._runtime_prompt_rail:
            resolved_channel = (
                str(params.channel_id or self._resolve_prompt_channel(params.session_id) or "web").strip() or "web"
            )
            self._runtime_prompt_rail.set_channel(resolved_channel)
            self._runtime_prompt_rail.set_request_system_prompt(params.request_system_prompt)
            self._runtime_prompt_rail.set_workspace_dir(resolved_workspace_dir)

        await self._update_rails_for_mode(
            params.mode,
            session_id=params.session_id,
            sync_code_submode=params.sync_code_submode,
        )

        if self._context_engineering_rail is not None:
            if hasattr(self._context_engineering_rail, "set_request_identify"):
                self._context_engineering_rail.set_request_identify(params.request_identify)
            if hasattr(self._context_engineering_rail, "set_request_soul"):
                self._context_engineering_rail.set_request_soul(params.request_soul)
        logger.info(
            "[JiuWenClawDeepAdapter] request soul/identify overrides: "
            "request_id=%s identify_len=%s soul_len=%s context_rail=%s context_enabled=%s",
            params.request_id,
            len((params.request_identify or "")),
            len((params.request_soul or "")),
            type(self._context_engineering_rail).__name__ if self._context_engineering_rail else None,
            bool(self._config_cache.get("context_engine_config", {}).get("enabled", False)),
        )
        await self._update_tools_for_mode(params.mode, params.session_id, params.request_id)
        await self._update_session_tools(params.session_id, params.request_id, channel_id=params.channel_id)
        self._refresh_acp_runtime_tools(
            params.session_id,
            params.request_id,
            params.channel_id,
            params.request_metadata,
        )
        self._update_prompt_for_mode(params.mode, resolved_language)

        # 处理记忆工具的注册/移除：
        # 0. engine=none 时全局移除所有记忆工具（优先级最高）
        # 1. 群聊数字分身模式（group_digital_avatar=True + avatar_mode=True）：移除写入工具，但保留读取工具
        # 2. 记忆完全禁用（enable_memory=False + group_digital_avatar=True + avatar_mode=True）：移除所有记忆工具（读取和写入）
        _all_memory_tools = ("write_memory", "edit_memory", "read_memory", "memory_search",
                             "memory_get", "memory_index")
        _cfg = self._latest_config_base if isinstance(self._latest_config_base, dict) else get_config()
        if not is_builtin_memory_allowed(_cfg):
            for tool_name in _all_memory_tools:
                try:
                    self._instance.ability_manager.remove(tool_name)
                    logger.info("[JiuWenClawDeepAdapter] engine=none，移除记忆工具 %s", tool_name)
                except Exception:
                    pass
        else:
            perm_ctx = TOOL_PERMISSION_CONTEXT.get()
            is_group_digital_avatar = False
            should_disable_memory = False
            if perm_ctx is not None:
                is_group_digital_avatar = (
                    perm_ctx.group_digital_avatar
                    and perm_ctx.avatar_mode
                )

                should_disable_memory = (
                    not perm_ctx.enable_memory
                    and perm_ctx.group_digital_avatar
                    and perm_ctx.avatar_mode
                )

            # 场景2：记忆完全禁用 - 移除所有记忆工具
            if should_disable_memory:
                _all_memory_tools = ("write_memory", "edit_memory", "read_memory", "memory_search",
                                     "memory_get", "memory_index")
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
                _cfg2 = self._latest_config_base if isinstance(self._latest_config_base, dict) else get_config()
                if is_builtin_memory_allowed(_cfg2):
                    try:
                        _mem_mode = get_memory_mode(_cfg2)
                        if _mem_mode == "wiki":
                            for tool in get_decorated_tools():
                                name = getattr(getattr(tool, "card", None), "name", "")
                                if name in ("memory_index", "memory_search"):
                                    Runner.resource_mgr.add_tool(tool)
                                    self._instance.ability_manager.add(tool.card)
                    except ImportError:
                        logger.warning(
                            "[JiuWenClawDeepAdapter] 恢复记忆工具失败，memory_tools 不可用"
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

    async def _freeze_qa_block_before_abort(
        self,
        session_id: str,
        *,
        reason: str,
        persist_checkpoint: bool = False,
    ) -> None:
        """Freeze in-progress QA before interrupt abort (plan mode only)."""
        if not session_id or self._instance is None:
            return
        if self._task_planning_rail is None:
            return

        # Bind request-scoped env overlay so freeze_persist (esp. async background
        # task via asyncio.create_task) inherits default_headers for LLM summarization
        # auth. process_interrupt does not call _on_chat_request_start, so the overlay
        # would be unbound here without this bind — read_default_headers() returns
        # None → AsyncOpenAI default_headers=None → 401 APIG.0303.
        #
        # Failures here must NEVER propagate: process_interrupt still needs to run
        # abort()/cancel toolkits. Prefer skipping freeze over hanging the agent.
        sid, aid = self._env_ns_ids()
        ns_token = None
        overlay_token = None
        try:
            try:
                ns_token = bind_agent_env_ns(sid, aid)
                overlay = build_effective_env_overlay(service_id=sid, agent_id=aid)
                overlay_token = bind_task_env_overlay(overlay)
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] qa_block %s env overlay bind failed "
                    "session_id=%s: %s (skipping freeze to allow interrupt abort)",
                    reason,
                    session_id,
                    exc,
                    exc_info=True,
                )
                return

            freeze_session = None
            freeze_owned = False
            if self._qa_block_freeze_rail is not None:
                try:
                    freeze_session, freeze_owned = await _resolve_session_for_checkpoint(
                        self._instance,
                        session_id,
                        card=self._instance.card,
                    )
                    if freeze_owned:
                        await freeze_session.pre_run(inputs=None)
                    await self._qa_block_freeze_rail.freeze_current_qa_sync(
                        session_id,
                        agent=self._instance,
                        session=freeze_session,
                        status="interrupted",
                        persist_mode="sync" if freeze_owned else "async",
                    )
                    context_engine = resolve_context_engine(self._instance)
                    if context_engine is not None and freeze_session is not None:
                        actual_session = getattr(freeze_session, "_parent", freeze_session) or freeze_session
                        await context_engine.save_contexts(actual_session)
                        await post_agent_execute_for_session(freeze_session, self._checkpointer)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] qa_block %s freeze failed session_id=%s: %s",
                        reason,
                        session_id,
                        exc,
                        exc_info=True,
                    )
                finally:
                    if freeze_owned and freeze_session is not None:
                        await freeze_session.post_run()

            if persist_checkpoint:
                # freeze_session stays None when freeze_rail is absent or freeze failed;
                # persist_checkpoint_for_session accepts session=None in that case.
                try:
                    await persist_checkpoint_for_session(
                        self._instance,
                        session_id,
                        card=self._instance.card,
                        session=freeze_session if freeze_session is not None and not freeze_owned else None,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] qa_block %s checkpoint persist failed "
                        "session_id=%s: %s (continuing interrupt abort)",
                        reason,
                        session_id,
                        exc,
                        exc_info=True,
                    )
        finally:
            if overlay_token is not None:
                reset_task_env_overlay(overlay_token)
            if ns_token is not None:
                reset_agent_env_ns(ns_token)

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """处理 interrupt 请求.

        根据 intent 分流：
        - pause: 暂停循环（不取消任务）
        - resume: 恢复已暂停的循环
        - cancel: 取消所有运行中的任务
        - supplement: 取消当前任务但保留 todo

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
            # supplement: 停止当前执行，但保留 todo（新任务会根据 todo 待办继续执行）
            session_id = str(request.session_id or "").strip()
            if session_id:
                try:
                    await self._freeze_qa_block_before_abort(
                        session_id,
                        reason="supplement",
                        persist_checkpoint=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] interrupt(supplement): qa freeze failed "
                        "session_id=%s: %s (continuing abort)",
                        session_id,
                        exc,
                        exc_info=True,
                    )
            # 1. 通过 rail abort 在 checkpoint 抛 CancelledError，打断当前内层执行
            if self._stream_event_rail is not None:
                self._stream_event_rail.abort()
            # 2. 终止 DeepAgent 外层 task loop
            if self._instance is not None:
                await self._instance.abort()
            # 3. 取消当前会话关联的 MultiSessionToolkit 子任务（按 request 跟踪，避免误停其它会话）
            await self._cancel_session_toolkits(request.session_id, "interrupt(supplement): ")
            # 4. 终止 fork_agent / spawn_subagent 派生出的活跃子 Agent
            await self._abort_active_subagents(f"interrupt({intent}) request_id={request.request_id}")
            AskUserQuestionRegistry.get_instance().cancel_for_session(
                RuntimeScopeKey.from_adapter(self, session_id=str(request.session_id or ""))
            )
            await self._clear_session_persisted_interrupt_state(
                request.session_id,
                reason="interrupt(supplement)",
                clear_todo_resume_snapshot_pending=True,
            )
            # 5. 不清理 todo — 保留给新任务继续
            logger.info(
                "[JiuWenClawDeepAdapter] interrupt(supplement): 已停止执行 request_id=%s",
                request.request_id,
            )
            message = "任务已切换"

        else:
            # cancel（默认）：停止所有执行
            session_id = str(request.session_id or "").strip()
            is_plan_mode = self._task_planning_rail is not None

            # plan 模式：abort 前 freeze + 落盘，保留 cancel 前 tool/assistant 上下文
            if is_plan_mode and session_id and self._instance is not None:
                try:
                    await self._freeze_qa_block_before_abort(
                        session_id,
                        reason="cancel",
                        persist_checkpoint=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] interrupt(cancel): qa freeze failed "
                        "session_id=%s: %s (continuing abort)",
                        session_id,
                        exc,
                        exc_info=True,
                    )

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
            # 4. 终止 fork_agent / spawn_subagent 派生出的活跃子 Agent
            await self._abort_active_subagents(f"interrupt({intent}) request_id={request.request_id}")
            AskUserQuestionRegistry.get_instance().cancel_for_session(
                RuntimeScopeKey.from_adapter(self, session_id=str(request.session_id or ""))
            )
            await self._clear_session_persisted_interrupt_state(
                request.session_id,
                reason="interrupt(cancel)",
                clear_todo_resume_snapshot_pending=True,
            )
            # 5. plan：pause todo + repair task_plan + 持久化 plan_paused；其它模式标 cancelled
            updated_todos = None
            if session_id:
                try:
                    if is_plan_mode:
                        updated_todos = await self._finalize_plan_pause_after_cancel(session_id)
                    else:
                        updated_todos = await self._cancel_pending_todos(session_id)
                except Exception as exc:
                    logger.warning("[JiuWenClawDeepAdapter] 处理 cancel 后 todo 失败: %s", exc)

                # 6. 收集中断前已完成的工作产物摘要（兜底：当 plan_pause / interrupt_resume 都未生效时）
                try:
                    await self._persist_interrupt_artifacts_summary(session_id)
                except Exception as exc:
                    logger.warning("[JiuWenClawDeepAdapter] persist interrupt artifacts summary failed: %s", exc)

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
        if intent not in ("pause", "resume", "supplement") and updated_todos is not None:
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

    async def _abort_active_subagents(self, reason: str) -> int:
        """Propagate interrupt cancellation to active fork/spawn subagents."""
        executor = self._get_fork_agent_executor()
        if executor is None:
            return 0

        abort_method = getattr(executor, "abort_active_subagents", None)
        if not callable(abort_method):
            logger.warning(
                "[JiuWenClawDeepAdapter] fork agent executor has no abort_active_subagents method"
            )
            return 0

        try:
            aborted_count = await abort_method(reason=reason)
            logger.info(
                "[JiuWenClawDeepAdapter] %s: aborted active subagents count=%d",
                reason,
                aborted_count,
            )
            return aborted_count
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] %s: abort active subagents failed error=%s",
                reason,
                exc,
            )
            return 0

    async def _clear_session_persisted_interrupt_state(
        self,
        session_id: str | None,
        *,
        reason: str,
        clear_todo_resume_snapshot_pending: bool = False,
    ) -> None:
        if not session_id:
            return
        if self._instance is None:
            return

        try:
            session = create_agent_session(session_id=session_id, card=self._instance.card)
            await session.pre_run(inputs=None)
            clear_session_interrupt_state(session)
            clear_interrupt_recovery_injected(session)
            if clear_todo_resume_snapshot_pending:
                set_todo_resume_snapshot_pending(session, pending=False)
            await post_agent_execute_for_session(session, self._checkpointer)
            # 同时清理 SkillTurbo 自己的 resume 上下文，避免下次 plain chat 时
            # 误命中"resume 路径"。
            try:
                await _skill_turbo_clear_resume_ctx(session)
            except Exception:
                logger.debug(
                    "[JiuWenClawDeepAdapter] clear skill_turbo resume ctx failed",
                    exc_info=True,
                )
            await session.post_run()
            logger.info(
                "[JiuWenClawDeepAdapter] %s: cleared persisted interrupt state session_id=%s",
                reason,
                session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] %s: clear persisted interrupt state failed session_id=%s error=%s",
                reason,
                session_id,
                exc,
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
            AskUserQuestionRegistry.get_instance().cancel_for_session(
                RuntimeScopeKey.from_adapter(self, session_id=str(sid or ""))
            )
        await self._abort_active_subagents("gateway_disconnect")

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
        if self._is_filled_model_credential(read_env("API_KEY")):
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
        except Exception:
            pass

        return False

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        """Handle chat.user_answer request with explicit source-based routing."""
        params = request.params if isinstance(request.params, dict) else {}
        request_id = params.get("request_id", "")
        answers = params.get("answers", [])
        source = params.get("source", "")
        interaction_status = params.get("status", "answered")
        resolved = False
        if source == "skill_evolve":
            resolved = await self._handle_evolution_approval(request_id, answers)
        elif source == "skill_create":
            resolved = await self._handle_skill_create_approval(
                request_id, answers, session_id=request.session_id or "",
            )
        elif source == "ask_tool":
            resolved = AskUserQuestionRegistry.get_instance().resolve(
                RuntimeScopeKey.from_adapter(self, session_id=str(request.session_id or "")),
                request_id,
                answers,
                status=interaction_status,
            )
        else:
            # Backward compatibility: keep request_id-prefix routing for old channels/frontends.
            if request_id.startswith("skill_evolve_"):
                resolved = await self._handle_evolution_approval(request_id, answers)
            elif request_id.startswith("skill_create_"):
                resolved = await self._handle_skill_create_approval(
                    request_id, answers, session_id=request.session_id or "",
                )
            elif isinstance(request_id, str) and request_id.startswith(ASK_REQUEST_PREFIX):
                resolved = AskUserQuestionRegistry.get_instance().resolve(
                    RuntimeScopeKey.from_adapter(self, session_id=str(request.session_id or "")),
                    request_id,
                    answers,
                    status=interaction_status,
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

    async def _handle_skill_create_approval(
        self, request_id: str, answers: list, *, session_id: str = ""
    ) -> bool:
        """Handle approval for new Skill creation proposals.

        Uses the optimizer path: calls rail.on_approve_new_skill() for accepted
        proposals which now returns a skill_creator_prompt string (instead of
        creating the skill directly).  The host dispatches this prompt via
        ``_dispatch_skill_creator_follow_up`` to launch a new invoke that calls
        the **skill-creator** skill.
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
            prompt = await rail.on_approve_new_skill(request_id)
            if prompt:
                await self._dispatch_skill_creator_follow_up(
                    request_id, prompt, session_id=session_id,
                )
                logger.info(
                    "[JiuWenClaw] skill create follow-up dispatched: request_id=%s session=%s",
                    request_id,
                    session_id,
                )
            else:
                logger.warning(
                    "[JiuWenClaw] skill create approved but no prompt returned: request_id=%s",
                    request_id,
                )
                return False
        else:
            await rail.on_reject_new_skill(request_id)
            logger.info("[JiuWenClaw] skill create rejected: request_id=%s", request_id)

        return True

    async def _iter_skill_creator_follow_up_stream(
        self,
        *,
        base_inputs: dict[str, Any],
        prompt: str,
        skill_create_request_id: str,
        stream_request_id: str,
        channel_id: str,
        session_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """Run skill-creator follow-up in the same stream/request scope as the main invoke."""
        followup_inputs = dict(base_inputs)
        followup_inputs["query"] = prompt
        followup_inputs["conversation_id"] = session_id
        followup_inputs["_invoke_turn_id"] = stream_request_id
        logger.info(
            "[JiuWenClaw] running in-stream skill_creator follow-up: "
            "skill_create_request_id=%s stream_request_id=%s session=%s prompt_chars=%d",
            skill_create_request_id,
            stream_request_id,
            session_id,
            len(prompt),
        )
        try:
            async for chunk in Runner.run_agent_streaming(self._instance, followup_inputs):
                parsed = self._parse_stream_chunk_with_source(chunk)
                if parsed is None:
                    continue
                yield AgentResponseChunk(
                    request_id=stream_request_id,
                    channel_id=channel_id,
                    payload=parsed,
                    is_complete=False,
                )
        except Exception as exc:
            logger.error(
                "[JiuWenClaw] in-stream skill_creator follow-up failed: %s", exc,
                exc_info=True,
            )
            yield AgentResponseChunk(
                request_id=stream_request_id,
                channel_id=channel_id,
                payload={"event_type": "chat.error", "error": str(exc)},
                is_complete=False,
            )

    async def _dispatch_skill_creator_follow_up(
        self, request_id: str, prompt: str, *, session_id: str = ""
    ) -> None:
        """方案一：发起新 invoke 调用 skill-creator 技能落盘。

        Launches a new invoke (async fire-and-forget) that uses the
        **skill-creator** skill to create the new skill in ``skills_dir``.
        The new invoke reuses the current session so the skill-creator can
        see the conversation context.

        Args:
            request_id: 原 skill_create_<uuid> request_id（用于关联日志/审计）
            prompt: Rail 构造的 skill_creator_prompt（含 skills_dir / reason / suggested_name）
        """
        resolved_session_id = (session_id or self._current_session_id() or "").strip()
        logger.info(
            "[JiuWenClaw] dispatching skill_creator follow-up: request_id=%s session=%s prompt_chars=%d",
            request_id,
            resolved_session_id,
            len(prompt),
        )

        async def _run_follow_up() -> None:
            try:
                await Runner.run_agent(
                    agent=self._instance,
                    inputs={
                        "query": prompt,
                        "conversation_id": resolved_session_id,
                    },
                    session=resolved_session_id or None,
                )
            except Exception as exc:
                logger.error(
                    "[JiuWenClaw] skill_creator follow-up run failed: %s", exc
                )

        try:
            task = asyncio.create_task(_run_follow_up())
            self._pending_follow_ups.add(task)
            task.add_done_callback(self._pending_follow_ups.discard)
        except Exception as exc:
            logger.error(
                "[JiuWenClaw] skill_creator follow-up dispatch failed: %s", exc
            )

    def _current_session_id(self) -> str:
        """Return the current session id for follow-up invokes.

        Falls back to an empty string if no session is available.
        """
        # Try to get from the most recent request context
        try:
            # _instance.card may carry session info
            if hasattr(self._instance, "card") and self._instance.card is not None:
                return getattr(self._instance.card, "session_id", "")
        except Exception as exc:
            logger.debug(
                "[JiuWenClaw] failed to resolve current session_id: %s",
                exc,
                exc_info=True,
            )
        return ""

    # ------------------------------------------------------------------
    # /evolve, /evolve_list, /evolve_simplify & /solidify command handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_evolution_eligible_skill_names(skill_names: list[str]) -> list[str]:
        return [name for name in skill_names if not is_bootstrap_builtin_skill(name)]

    @staticmethod
    def _guard_bootstrap_skill(skill_name: str) -> dict[str, Any] | None:
        """Return error response if *skill_name* is a built-in BOOTSTRAP skill."""
        if not is_bootstrap_builtin_skill(skill_name):
            return None
        return {
            "output": (
                f"Skill '{skill_name}' 为内置官方技能，"
                f"不参与 Skill 自演进。"
            ),
            "result_type": "error",
        }

    async def _handle_evolve_command(self, query: str, session_id: str) -> dict[str, Any]:
        """/evolve [list | <skill_name>] handler.

        When ``evolution.auto_save`` is enabled, generates experience records and
        persists them directly (evolutions.json + solidify to SKILL.md) without
        frontend approval. When disabled, returns a configuration hint and does
        not generate or persist.
        """
        rail = self._skill_evolution_rail
        assert rail is not None
        store = rail.store

        skill_names = self._filter_evolution_eligible_skill_names(store.list_skill_names())

        parts = query.split(maxsplit=1)
        skill_arg = parts[1].strip() if len(parts) > 1 else ""

        # --- /evolve list (or bare /evolve) ---
        if not skill_arg or skill_arg == "list":
            if not skill_names:
                return {
                    "output": "当前 skills_base_dir 下未找到可参与自演进的 Skill 目录。",
                    "result_type": "answer",
                }
            summary = await store.list_pending_summary(skill_names)
            return {
                "output": f"**Skills 演进记录：**\n\n{summary}",
                "result_type": "answer",
            }

        # --- /evolve <skill_name> ---
        skill_name = skill_arg
        err = self._guard_bootstrap_skill(skill_name)
        if err:
            return err
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

        if not rail.auto_save:
            return {
                "output": (
                    "evolution.auto_save 未开启，无法执行 /evolve 落盘。\n"
                    "请在配置中将 evolution.auto_save 设为 true 后重试。"
                ),
                "result_type": "answer",
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
        detected = await detector.detect(parsed_messages)

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

        # 3) Generate experience records and persist directly (auto_save path)
        try:
            records = await rail._generate_experience_for_skill(  # pylint: disable=protected-access
                skill_name, attributed, parsed_messages
            )
        except Exception as exc:
            logger.warning("[JiuWenClaw] evolve generate failed (skill=%s): %s", skill_name, exc)
            return {
                "output": f"演进经验生成失败：{exc}",
                "result_type": "error",
            }

        if not records:
            return {
                "output": "当前对话的演进信号未产生演进记录（已有相同演进经验或演进信号无关）\n",
                "result_type": "answer",
            }

        try:
            for record in records:
                await store.append_record(skill_name, record)
            solidified = await store.solidify(skill_name)
        except Exception as exc:
            logger.warning("[JiuWenClaw] evolve persist failed (skill=%s): %s", skill_name, exc)
            return {
                "output": (
                    f"演进经验已生成但落盘失败：{exc}\n"
                    f"可使用 `/solidify {skill_name}` 重试固化。"
                ),
                "result_type": "error",
            }

        summaries = "\n".join(
            f"  {i + 1}. **[{record.change.section}]** {record.change.content[:200]}"
            for i, record in enumerate(records)
        )
        solidify_note = (
            f"已将 {solidified} 条 BODY 经验固化到 SKILL.md。"
            if solidified
            else "（无 BODY 经验需固化到 SKILL.md）"
        )
        return {
            "output": (
                f"已记录 {len(records)} 条演进经验到 Skill '{skill_name}'：\n"
                f"{summaries}\n\n"
                f"{solidify_note}"
            ),
            "result_type": "answer",
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

        qa_history_messages: list[Any] = []
        try:
            session_ref_getter = getattr(context, "get_session_ref", None)
            session_ref = (
                session_ref_getter()
                if callable(session_ref_getter)
                else getattr(context, "_session_ref", None)
            )
            context_id_getter = getattr(context, "context_id", None)
            context_id = context_id_getter() if callable(context_id_getter) else "default_context_id"
            history = context_engine.get_history_qa_buffer(session_id, context_id)
            qa_ids: list[str] = []
            try:
                from openjiuwen.core.context_engine.qa_block.registry import load_registry
                if session_ref is not None:
                    registry = load_registry(session_ref)
                    sorted_blocks = sorted(
                        registry.blocks.values(),
                        key=lambda item: item.qa_index,
                        reverse=True,
                    )
                    qa_ids = [entry.qa_id for entry in sorted_blocks]
            except Exception as registry_exc:
                logger.debug("[JiuWenClaw] load QA block registry for evolve failed: %s", registry_exc)
            if not qa_ids and hasattr(history, "recent_qa_ids"):
                qa_ids = list(reversed(history.recent_qa_ids()))
            fallback_qa_ids = qa_ids[:10]
            for qa_id in reversed(fallback_qa_ids):
                cached = history.get(qa_id)
                if cached:
                    qa_history_messages.extend(list(cached))
            if qa_history_messages:
                raw_messages = qa_history_messages + list(raw_messages)
        except Exception as fallback_exc:
            logger.debug("[JiuWenClaw] collect QA block fallback for evolve failed: %s", fallback_exc)

        parsed_messages = JiuClawSkillEvolutionRail.parse_messages(raw_messages)
        if qa_history_messages:
            parsed_messages = JiuClawSkillEvolutionRail.dedup_messages(parsed_messages)
        return parsed_messages

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

        err = self._guard_bootstrap_skill(skill_name)
        if err:
            return err

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
        err = self._guard_bootstrap_skill(skill_name)
        if err:
            return err

        if not store.skill_exists(skill_name):
            available = "、".join(self._filter_evolution_eligible_skill_names(store.list_skill_names())) or "（无可用 Skill）"
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
        err = self._guard_bootstrap_skill(skill_name)
        if err:
            return err

        if not store.skill_exists(skill_name):
            available = "、".join(self._filter_evolution_eligible_skill_names(store.list_skill_names())) or "（无可用 Skill）"
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

    @staticmethod
    def _is_body_archive_name(archive_name: str) -> bool:
        # Match EvolutionStore.is_body_archive_filename: SKILL.vMAJOR.MINOR.PATCH.md
        return (
            archive_name.startswith("SKILL.v")
            and archive_name.endswith(".md")
            and archive_name != "SKILL.md"
        )

    def _get_disk_evolution_store(self) -> EvolutionStore:
        """Build a disk-only EvolutionStore (no LLM / EvolutionRail)."""
        return EvolutionStore(self._registered_skill_dirs_for_rail())

    def _list_body_archive_versions(
        self, skill_name: str, store: EvolutionStore | None = None,
    ) -> list[str]:
        """List body archive filenames for *skill_name* (newest-first by name)."""
        evolution_store = store if store is not None else self._get_disk_evolution_store()
        archives = evolution_store.list_archives(skill_name)
        return [a for a in archives if self._is_body_archive_name(a)]

    async def _rollback_skill_via_store(
        self,
        store: EvolutionStore,
        skill_name: str,
        version: str | None = None,
    ) -> tuple[bool, bool]:
        """Rollback skill via EvolutionStore only (no EvolutionRail / LLM).

        ``version`` may be a body archive filename (``SKILL.v1.0.0.md``) or a bare
        SemVer string (``1.0.0``). When omitted, the newest body archive by mtime
        is used.

        After restoring the skill body, always clears live ``evolutions.json``
        (empty evolution log), retaining the restored SKILL.md frontmatter
        version or defaulting to ``v1.0.0``. The pre-rollback snapshot archives
        an empty paired ``evolutions.v*.json`` (all evolution archives are empty
        by design). The consumed target body archive version is removed from
        ``archive/``.

        Returns ``(success, evo_cleared)``. ``evo_cleared`` is meaningful only
        when ``success`` is True; False means body was rolled back but the live
        evolution log could not be cleared.
        """
        archive = store.get_skill_archive_dir(skill_name)
        if archive is None:
            logger.warning("[JiuWenClaw] no archive dir for %s", skill_name)
            return False, True

        if version:
            body_name = store.normalize_body_archive_name(version)
            if body_name is None:
                logger.warning(
                    "[JiuWenClaw] invalid archive version for %s: %s", skill_name, version,
                )
                return False, True
            body_archive = store.get_skill_archive_file(skill_name, body_name)
            if body_archive is None:
                logger.warning(
                    "[JiuWenClaw] invalid archive version for %s: %s", skill_name, version,
                )
                return False, True
        else:
            body_files = sorted(
                [
                    f for f in archive.iterdir()
                    if f.is_file() and store.is_body_archive_filename(f.name)
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not body_files:
                logger.warning("[JiuWenClaw] no archived body for %s", skill_name)
                return False, True
            body_archive = body_files[0]

        old_body = await store.read_archive_text(skill_name, body_archive.name)
        if not old_body:
            logger.warning(
                "[JiuWenClaw] archived body is empty for %s: %s",
                skill_name,
                body_archive.name,
            )
            return False, True

        await store.archive_current_state(skill_name)
        await store.write_skill_content(skill_name, old_body)

        # Retain version from the restored SKILL.md frontmatter; default to
        # v1.0.0 when absent so we never keep the pre-rollback live log version.
        retain_version = "v1.0.0"
        extract_version = getattr(store, "_extract_version_from_skill_md", None)
        if callable(extract_version):
            parsed_version = extract_version(old_body)
            if parsed_version:
                retain_version = parsed_version

        # Body write is the primary commit. Clearing the live evolution log is
        # best-effort: failure must not report overall rollback failure after
        # body already changed.
        evo_cleared = True
        try:
            cleared = await store.clear_evolutions(
                skill_name, retain_version=retain_version,
            )
            if cleared is False:
                evo_cleared = False
                logger.warning(
                    "[JiuWenClaw] body rolled back but failed to clear evolution log for %s",
                    skill_name,
                )
        except Exception as exc:
            evo_cleared = False
            logger.warning(
                "[JiuWenClaw] body rolled back but failed to clear evolution log for %s: %s",
                skill_name,
                exc,
            )

        deleted = await store.delete_archive_version(skill_name, body_archive.name)
        if not deleted:
            logger.warning(
                "[JiuWenClaw] rollback succeeded but failed to remove target archive for %s: %s",
                skill_name,
                body_archive.name,
            )

        if evo_cleared:
            logger.info(
                "[JiuWenClaw] disk rollback completed for %s -> %s",
                skill_name,
                body_archive.name,
            )
        else:
            logger.warning(
                "[JiuWenClaw] disk rollback completed for %s -> %s "
                "(skill body restored; evolution log was not cleared)",
                skill_name,
                body_archive.name,
            )
        return True, evo_cleared

    async def _do_evolve_rollback(
        self, skill_name: str, version: str | None,
        store=None,
        skill_path: str | None = None,
    ) -> dict[str, Any]:
        """Shared rollback used by slash command and skills.evolution.rollback RPC.

        Disk-only: uses EvolutionStore, does not require EvolutionRail / LLM.

        Returns a structured dict:
        - ``{ok, rolled_back: False, name, versions}`` when *version* is omitted (list only)
        - ``{ok, rolled_back: True, name, version[, warning][, skill_path]}`` on successful rollback
        - ``{ok: False, error}`` on failure
        """
        if store is None:
            store = self._get_disk_evolution_store()

        guard = self._guard_bootstrap_skill(skill_name)
        if guard:
            return {"ok": False, "error": guard["output"]}

        if not store.skill_exists(skill_name):
            available = (
                "、".join(self._filter_evolution_eligible_skill_names(store.list_skill_names()))
                or "（无可用 Skill）"
            )
            return {"ok": False, "error": f"未找到 Skill '{skill_name}'。当前可用：{available}"}

        body_versions = self._list_body_archive_versions(skill_name, store=store)
        if not body_versions:
            return {"ok": False, "error": f"Skill '{skill_name}' 没有归档版本可回滚。"}

        if not version:
            return {
                "ok": True,
                "rolled_back": False,
                "name": skill_name,
                "versions": body_versions,
            }

        resolved = version
        if resolved == "latest":
            resolved = body_versions[0]
        else:
            normalize = getattr(store, "normalize_body_archive_name", None)
            if callable(normalize):
                normalized = normalize(resolved)
                if normalized:
                    resolved = normalized

        if resolved not in body_versions:
            hint = "、".join(f"`{v}`" for v in body_versions[:5])
            return {"ok": False, "error": f"版本 `{version}` 不存在。可用版本：{hint}"}

        try:
            success, evo_ok = await asyncio.wait_for(
                self._rollback_skill_via_store(store, skill_name, resolved),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[JiuWenClaw] evolve_rollback timed out for skill=%s version=%s "
                "(filesystem may be partially updated)",
                skill_name,
                resolved,
            )
            return {
                "ok": False,
                "error": (
                    "回滚操作超时，文件系统可能处于部分完成状态"
                    "（body / evolution log / 归档可能不一致）。"
                    f"请检查 Skill '{skill_name}' 的 archive 目录后再重试。"
                ),
            }
        except Exception as exc:
            logger.warning("[JiuWenClaw] evolve_rollback failed: %s", exc)
            return {"ok": False, "error": f"回滚失败：{exc}"}

        if success:
            result: dict[str, Any] = {
                "ok": True,
                "rolled_back": True,
                "name": skill_name,
                "version": resolved,
            }
            if skill_path:
                result["skill_path"] = skill_path
            if not evo_ok:
                result["warning"] = (
                    "Skill body 已回滚，但 evolution log 清空失败，"
                    "请检查该 Skill 的 evolutions.json。"
                )
            return result
        return {"ok": False, "error": f"Skill '{skill_name}' 回滚失败，请检查归档版本是否有效。"}

    async def handle_skills_evolution_archives(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.archives — list rollback archive versions (disk-only)."""
        try:
            skill_name = safe_path_name(params.get("name"), "skill")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        guard = self._guard_bootstrap_skill(skill_name)
        if guard:
            raise ValueError(guard["output"])

        store = self._get_disk_evolution_store()
        if not store.skill_exists(skill_name):
            raise ValueError(f"未找到 Skill '{skill_name}'")

        return {
            "name": skill_name,
            "versions": self._list_body_archive_versions(skill_name, store=store),
        }

    async def handle_skills_evolution_rollback(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.rollback — rollback skill via disk EvolutionStore."""
        try:
            skill_name = safe_path_name(params.get("name"), "skill")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        raw_version = params.get("version")
        version = str(raw_version).strip() if raw_version is not None and str(raw_version).strip() else None

        raw_path = params.get("skill_path") or params.get("path")
        skill_path = str(raw_path).strip() if raw_path is not None and str(raw_path).strip() else None

        store = None
        if skill_path:
            skill_path = self._validate_rebuild_skill_path(skill_path)
            resolved_path = Path(skill_path).expanduser().resolve()
            if resolved_path.name != "SKILL.md":
                raise ValueError(f"skill_path 必须指向 SKILL.md，当前为：{skill_path}")
            skill_dir = resolved_path.parent
            if skill_dir.name != skill_name:
                raise ValueError(f"skill_path 目录名必须与 name 一致：name={skill_name}，目录名={skill_dir.name}")
            skills_base = skill_dir.parent
            store = EvolutionStore([str(skills_base)])

        result = await self._do_evolve_rollback(skill_name, version, store=store, skill_path=skill_path)
        if not result.get("ok"):
            raise ValueError(str(result.get("error") or "回滚失败"))

        if result.get("rolled_back"):
            payload = {
                "success": True,
                "name": result["name"],
                "version": result["version"],
                "rolled_back": True,
            }
            if result.get("skill_path"):
                payload["skill_path"] = result["skill_path"]
            if result.get("warning"):
                payload["warning"] = result["warning"]
            return payload
        return {
            "success": True,
            "name": result["name"],
            "rolled_back": False,
            "versions": result.get("versions") or [],
        }

    async def _handle_evolve_rollback_command(self, query: str) -> dict[str, Any]:
        """/evolve_rollback <skill_name> [version] — Rollback skill to archived version."""
        store = self._get_disk_evolution_store()

        parts = query.split(maxsplit=2)
        skill_name = parts[1] if len(parts) > 1 else ""
        version = parts[2].strip() if len(parts) > 2 else None

        if not skill_name:
            archives_hint = ""
            for name in self._filter_evolution_eligible_skill_names(store.list_skill_names()):
                body_versions = self._list_body_archive_versions(name, store=store)
                if body_versions:
                    archives_hint += f"\n  - **{name}**: {len(body_versions)} 个版本"
            return {
                "output": (
                    "请指定 Skill 名称：`/evolve_rollback <skill_name> [version]`"
                    + (f"\n\n可回滚的 Skill：{archives_hint}" if archives_hint else "")
                ),
                "result_type": "error",
            }

        result = await self._do_evolve_rollback(skill_name, version)
        if not result.get("ok"):
            return {"output": str(result.get("error") or "回滚失败"), "result_type": "error"}

        if not result.get("rolled_back"):
            body_versions = result.get("versions") or []
            lines = [f"**Skill '{skill_name}' 可用归档版本（最新在前）：**\n"]
            for i, v in enumerate(body_versions):
                ts = v.replace("SKILL.v", "").replace(".md", "")
                if len(ts) >= 15 and v.startswith("SKILL.v"):
                    display_ts = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]} UTC"
                else:
                    display_ts = ts
                marker = " ← 最近" if i == 0 else ""
                lines.append(f"  {i+1}. `{v}` ({display_ts}){marker}")
            lines.append(f"\n用法：`/evolve_rollback {skill_name} SKILL.v<时间戳>.md`")
            lines.append(f"快捷回滚到最近版本：`/evolve_rollback {skill_name} latest`")
            return {"output": "\n".join(lines), "result_type": "answer"}

        resolved = result.get("version") or version
        output = (
            f"Skill '{skill_name}' 已成功回滚到 `{resolved}`。\n\n"
            f"（当前状态已自动归档，可再次回滚恢复。）"
        )
        if result.get("warning"):
            output += f"\n\n⚠️ {result['warning']}"
        return {
            "output": output,
            "result_type": "answer",
        }

    @dataclass
    class MergeVersionStreamContext:
        """Optional streaming context for generate_evolution_merge_version."""

        base_inputs: dict[str, Any]
        stream_request_id: str
        channel_id: str
        session_id: str
        chunks: list[Any] = field(default_factory=list)

    async def generate_evolution_merge_version(
        self,
        *,
        skill_name: str,
        skill_path: str | None = None,
        record_ids: Sequence[str] | None = None,
        user_intent: str | None = None,
        min_score: float = 0.5,
        stream_ctx: MergeVersionStreamContext | None = None,
    ) -> dict[str, Any]:
        """Archive current skill, fuse live evolutions into a new version, clear log.

        Side effects on success:
          - archive previous SKILL.md + evolutions.json
          - rewrite SKILL.md with fused experiences (via agent)
          - bump SemVer / append changelog
          - clear live evolutions.json
        """
        # Control-plane rebuild / auto-rebuild do not go through chat request start,
        # so bind tip env (default_headers → Authorization) for LLM rewrite + changelog.
        sid, aid = self._env_ns_ids()
        ns_token = bind_agent_env_ns(sid, aid)
        overlay = build_effective_env_overlay(service_id=sid, agent_id=aid)
        env_token = bind_task_env_overlay(overlay)
        try:
            return await self._generate_evolution_merge_version_impl(
                skill_name=skill_name,
                skill_path=skill_path,
                record_ids=record_ids,
                user_intent=user_intent,
                min_score=min_score,
                stream_ctx=stream_ctx,
            )
        finally:
            reset_task_env_overlay(env_token)
            reset_agent_env_ns(ns_token)

    async def _generate_evolution_merge_version_impl(
        self,
        *,
        skill_name: str,
        skill_path: str | None = None,
        record_ids: Sequence[str] | None = None,
        user_intent: str | None = None,
        min_score: float = 0.5,
        stream_ctx: MergeVersionStreamContext | None = None,
    ) -> dict[str, Any]:
        """Inner merge-version flow (caller must bind env overlay for LLM auth)."""
        prepared = await self._prepare_rebuild_followup(
            skill_name,
            skill_path=skill_path,
            record_ids=record_ids,
            user_intent=user_intent,
            min_score=min_score,
        )
        if prepared.get("result_type") != "followup":
            return {
                "ok": False,
                "skill_name": (skill_name or "").strip(),
                "error": str(prepared.get("output") or "未生成可执行的重建指令"),
            }

        prompt = str(prepared.get("followup_prompt") or "").strip()
        resolved_name = str(prepared.get("skill_name") or skill_name).strip()
        rebuild_context = prepared.get("rebuild_context")
        resolved_path = None
        if isinstance(rebuild_context, dict):
            raw_path = rebuild_context.get("skill_md_path") or skill_path
            if isinstance(raw_path, str) and raw_path.strip():
                resolved_path = raw_path.strip()

        if not prompt:
            return {
                "ok": False,
                "skill_name": resolved_name,
                "skill_path": resolved_path,
                "error": "重建 prompt 为空",
            }

        rewrite_ok = await self._execute_merge_version_rewrite(
            prompt=prompt,
            skill_name=resolved_name,
            skill_md_path=resolved_path,
            stream_ctx=stream_ctx,
        )
        if not rewrite_ok:
            return {
                "ok": False,
                "skill_name": resolved_name,
                "skill_path": resolved_path,
                "archive_path": (
                    rebuild_context.get("archive_path")
                    if isinstance(rebuild_context, dict)
                    else None
                ),
                "error": (
                    f"Skill '{resolved_name}' 融合重写 SKILL.md 失败。"
                    "请到安全管理关闭 code、read_file、write_file、edit_file、bash、skill_complete "
                    "的安全审批后重试。"
                ),
            }

        finalized = await self._finalize_rebuild_followup(prepared)
        result: dict[str, Any] = {
            "ok": bool(finalized.get("cleared")),
            "skill_name": resolved_name,
            "skill_path": resolved_path,
            "archive_path": (
                rebuild_context.get("archive_path")
                if isinstance(rebuild_context, dict)
                else None
            ),
            "new_version": finalized.get("new_version"),
            "cleared": bool(finalized.get("cleared")),
        }
        if not result["ok"]:
            result["error"] = str(
                finalized.get("error")
                or f"Skill '{resolved_name}' 版本 bump / 清空 evolutions 失败"
            )
            logger.warning(
                "[JiuWenClawDeepAdapter] merge version partial failure: skill=%s "
                "SKILL.md rewritten but finalize failed, evolution log may need manual cleanup",
                resolved_name,
                extra={"user_visible": "progress"},
            )
        return result

    @staticmethod
    def _validate_rebuild_skill_path(skill_path: str) -> str:
        """Normalize RPC skill_path to an absolute resolved path."""
        return str(Path(skill_path).expanduser().resolve())

    async def handle_skills_evolution_rebuild(self, params: dict) -> dict[str, Any]:
        """RPC: skills.evolution.rebuild — generate a merged evolution version."""
        try:
            skill_name = safe_path_name(params.get("name") or params.get("skill_name"), "skill")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        guard = self._guard_bootstrap_skill(skill_name)
        if guard:
            raise ValueError(guard["output"])

        raw_path = params.get("skill_path") or params.get("path")
        skill_path = str(raw_path).strip() if raw_path is not None and str(raw_path).strip() else None
        if skill_path:
            skill_path = self._validate_rebuild_skill_path(skill_path)

        raw_ids = params.get("record_ids")
        record_ids: list[str] | None = None
        if isinstance(raw_ids, (list, tuple)):
            record_ids = [str(item).strip() for item in raw_ids if str(item).strip()] or None
        elif isinstance(raw_ids, str) and raw_ids.strip():
            record_ids = [part.strip() for part in raw_ids.split(",") if part.strip()] or None

        raw_intent = params.get("user_intent") or params.get("intent")
        user_intent = (
            str(raw_intent).strip()
            if raw_intent is not None and str(raw_intent).strip()
            else None
        )

        min_score = 0.5
        if params.get("min_score") is not None:
            try:
                min_score = float(params.get("min_score"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid min_score: {params.get('min_score')}") from exc

        logger.info(
            "[JiuWenClawDeepAdapter] skills.evolution.rebuild start: skill=%s "
            "record_ids=%s skill_path=%s min_score=%s",
            skill_name,
            len(record_ids) if record_ids else 0,
            skill_path,
            min_score,
            extra={"user_visible": "progress"},
        )
        result = await self.generate_evolution_merge_version(
            skill_name=skill_name,
            skill_path=skill_path,
            record_ids=record_ids,
            user_intent=user_intent,
            min_score=min_score,
        )
        if not result.get("ok"):
            raise ValueError(str(result.get("error") or "生成合并版本失败"))
        payload = {
            "success": True,
            "name": result.get("skill_name") or skill_name,
            "skill_path": result.get("skill_path"),
            "archive_path": result.get("archive_path"),
            "new_version": result.get("new_version"),
            "cleared": bool(result.get("cleared")),
        }
        logger.info(
            "[JiuWenClawDeepAdapter] skills.evolution.rebuild done: skill=%s "
            "version=%s cleared=%s archive=%s",
            payload["name"],
            payload["new_version"],
            payload["cleared"],
            payload["archive_path"],
            extra={"user_visible": "progress"},
        )
        return payload

    async def _prepare_rebuild_followup(
        self,
        skill_name: str,
        *,
        skill_path: str | None = None,
        record_ids: Sequence[str] | None = None,
        user_intent: str | None = None,
        min_score: float = 0.5,
    ) -> dict[str, Any]:
        """Prepare rebuild: archive current state and build agent rewrite prompt.

        Disk-only for store I/O (aligned with rollback); does not require
        SkillEvolutionRail. Merge rewrite still needs DeepAgent separately.
        """
        store = self._get_disk_evolution_store()
        skill_name = (skill_name or "").strip()
        if not skill_name:
            return {
                "output": "未指定 Skill 名称，无法自动重建版本。",
                "result_type": "error",
            }

        if not store.skill_exists(skill_name):
            available = "、".join(store.list_skill_names()) or "（无可用 Skill）"
            return {
                "output": f"未找到 Skill '{skill_name}'。当前可用：{available}",
                "result_type": "error",
            }

        if ExperienceRebuildService is None or build_rebuild_command_prompt is None:
            return {
                "output": "演进重建功能不可用：当前 openjiuwen 版本缺少 rebuild API。",
                "result_type": "error",
            }

        subject = {"kind": "skill", "name": skill_name}
        resolver = getattr(store, "resolve_subject_payload", None)
        if callable(resolver):
            try:
                payload = resolver(skill_name)
            except Exception:
                logger.warning("[JiuWenClaw] could not resolve rebuild subject for skill=%s", skill_name)
            else:
                if isinstance(payload, dict):
                    kind = str(payload.get("kind") or "").strip()
                    name = str(payload.get("name") or skill_name).strip() or skill_name
                    if kind:
                        subject = {"kind": kind, "name": name}

        rebuild_service = self._make_rebuild_service(store)
        rebuild_context = await rebuild_service.prepare_rebuild_context(
            subject,
            user_intent=user_intent,
            min_score=min_score,
            record_ids=list(record_ids) if record_ids is not None else None,
        )
        if rebuild_context is None:
            return {
                "output": f"Skill '{skill_name}' 未生成可执行的重建指令。",
                "result_type": "error",
            }

        if skill_path and str(skill_path).strip():
            rebuild_context["skill_md_path"] = str(skill_path).strip()

        prompt = build_rebuild_command_prompt(
            subject=subject,
            user_intent=user_intent,
            rebuild_context=rebuild_context,
        )
        return {
            "action": "run_rebuild_followup",
            "followup_prompt": prompt,
            "skill_name": skill_name,
            "subject": subject,
            "rebuild_context": rebuild_context,
            "result_type": "followup",
        }

    @staticmethod
    def _skill_md_fingerprint(skill_md_path: str | None) -> str | None:
        """Return sha256 of SKILL.md body, or None when path missing/unreadable."""
        if not skill_md_path or not str(skill_md_path).strip():
            return None
        try:
            data = Path(skill_md_path).expanduser().read_bytes()
        except OSError:
            return None
        return hashlib.sha256(data).hexdigest()

    async def _execute_merge_version_rewrite(
        self,
        *,
        prompt: str,
        skill_name: str,
        skill_md_path: str | None = None,
        stream_ctx: MergeVersionStreamContext | None,
    ) -> bool:
        """Run agent rewrite of SKILL.md; return True on success."""
        if stream_ctx is not None:
            rebuild_ok = True
            before_fp = self._skill_md_fingerprint(skill_md_path)
            try:
                async for follow_chunk in self._iter_skill_creator_follow_up_stream(
                    base_inputs=stream_ctx.base_inputs,
                    prompt=prompt,
                    skill_create_request_id=f"auto_rebuild_{skill_name}",
                    stream_request_id=stream_ctx.stream_request_id,
                    channel_id=stream_ctx.channel_id,
                    session_id=stream_ctx.session_id,
                ):
                    stream_ctx.chunks.append(follow_chunk)
                    payload = follow_chunk.payload if isinstance(follow_chunk.payload, dict) else {}
                    if payload.get("event_type") == "chat.error":
                        rebuild_ok = False
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] merge-version rewrite failed: skill=%s error=%s",
                    skill_name,
                    exc,
                    exc_info=True,
                    extra={"user_visible": "progress"},
                )
                return False
            after_fp = self._skill_md_fingerprint(skill_md_path)
            if rebuild_ok and before_fp is not None and after_fp == before_fp:
                logger.warning(
                    "[JiuWenClawDeepAdapter] merge-version rewrite failed: skill=%s "
                    "error=SKILL.md unchanged after stream rewrite",
                    skill_name,
                    extra={"user_visible": "progress"},
                )
                rebuild_ok = False
            if rebuild_ok:
                logger.info(
                    "[JiuWenClawDeepAdapter] merge-version LLM rewrite ok: skill=%s mode=%s",
                    skill_name,
                    "stream",
                    extra={"user_visible": "progress"},
                )
            else:
                logger.warning(
                    "[JiuWenClawDeepAdapter] merge-version rewrite failed: skill=%s error=%s",
                    skill_name,
                    "chat.error in stream or SKILL.md unchanged",
                    extra={"user_visible": "progress"},
                )
            return rebuild_ok

        if self._instance is None:
            logger.warning(
                "[JiuWenClawDeepAdapter] merge-version rewrite skipped: no agent instance skill=%s",
                skill_name,
            )
            return False
        before_fp = self._skill_md_fingerprint(skill_md_path)
        try:
            await Runner.run_agent(
                agent=self._instance,
                inputs={"query": prompt},
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] merge-version non-stream rewrite failed: skill=%s error=%s",
                skill_name,
                exc,
                exc_info=True,
            )
            return False

        after_fp = self._skill_md_fingerprint(skill_md_path)
        if before_fp is not None and after_fp == before_fp:
            logger.warning(
                "[JiuWenClawDeepAdapter] merge-version non-stream rewrite failed: "
                "skill=%s error=SKILL.md unchanged (LLM may have failed silently)",
                skill_name,
                extra={"user_visible": "progress"},
            )
            return False

        logger.info(
            "[JiuWenClawDeepAdapter] merge-version LLM rewrite ok: skill=%s mode=%s",
            skill_name,
            "non_stream",
            extra={"user_visible": "progress"},
        )
        return True

    async def _finalize_rebuild_followup(
        self, rebuild_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Bump version, write changelog, clear evolution log after successful rewrite."""
        if not isinstance(rebuild_result, dict):
            return {"cleared": False, "error": "invalid rebuild result"}
        if rebuild_result.get("action") != "run_rebuild_followup":
            return {"cleared": False, "error": "invalid rebuild action"}
        rebuild_context = rebuild_result.get("rebuild_context")
        if not isinstance(rebuild_context, dict):
            return {"cleared": False, "error": "missing rebuild context"}
        if ExperienceRebuildService is None:
            return {"cleared": False, "error": "ExperienceRebuildService unavailable"}
        store = self._get_disk_evolution_store()
        rebuild_service = self._make_rebuild_service(store)
        cleared = await rebuild_service.complete_rebuild(rebuild_context)
        new_version = await self._read_skill_version_after_rebuild(
            store, str(rebuild_context.get("skill_name") or ""),
        )
        logger.info(
            "[JiuWenClawDeepAdapter] rebuild followup finalized for skill=%s cleared=%s version=%s",
            rebuild_context.get("skill_name"),
            cleared,
            new_version,
        )
        return {"cleared": bool(cleared), "new_version": new_version}

    @staticmethod
    async def _read_skill_version_after_rebuild(store: Any, skill_name: str) -> str | None:
        """Best-effort read of skill SemVer after complete_rebuild."""
        if not skill_name:
            return None
        load_log = getattr(store, "load_evolution_log", None)
        if not callable(load_log):
            return None
        try:
            evo_log = await load_log(skill_name)
        except Exception:
            return None
        version = getattr(evo_log, "version", None)
        if version is None and isinstance(evo_log, dict):
            version = evo_log.get("version")
        if version is None:
            return None
        text = str(version).strip()
        return text or None

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

        if stripped.startswith("/evolve_rollback"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_evolve_rollback_command(stripped)

        if stripped.startswith("/evolve_rebuild"):
            return {
                "output": (
                    "`/evolve_rebuild` 已移除。\n"
                    "在 `.office-claw/capabilities.json` 中将 Skill 的 `selfEvolution` 设为 "
                    "`auto` 后，在线演进落盘经验时会自动融合为新版本。"
                ),
                "result_type": "answer",
            }

        if stripped.startswith("/evolve"):
            err = self._ensure_evolution_rail_for_slash(mode)
            if err:
                return {"output": err, "result_type": "error"}
            return await self._handle_evolve_command(stripped, session_id)

        return None

    def _get_todo_modify_tool(self, session_id: str) -> TodoModifyTool | None:
        """Return a session-scoped TodoModifyTool, or None if DeepAgent is unavailable."""
        if self._instance is None:
            return None

        modify_tool: TodoModifyTool | None = None
        ability_manager = getattr(self._instance, "ability_manager", None)
        if ability_manager is not None:
            try:
                tool_card = ability_manager.get("todo_modify")
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

        return modify_tool

    async def _finalize_plan_pause_after_cancel(self, session_id: str) -> list[dict] | None:
        """agent.plan cancel: snapshot todos, isolate unfinished from file, persist plan_paused."""
        if self._instance is None:
            return None

        modify_tool = self._get_todo_modify_tool(session_id)
        session = create_agent_session(session_id=session_id, card=self._instance.card)
        await session.pre_run(inputs=None)
        try:
            snapshot: dict[str, Any] | None = None
            if modify_tool is not None:
                snapshot = await snapshot_and_isolate_unfinished_todos(modify_tool, session_id)

            state = self._instance.load_state(session)
            repair_task_plan_after_pause(state)
            self._instance.save_state(session, state)
            write_plan_pause_to_session(session, paused=True, snapshot=snapshot)
            await post_agent_execute_for_session(session, self._checkpointer)

            # Disk backup: aborted-stream checkpoint may overwrite CP without plan_paused.
            try:
                write_plan_pause_to_file(Path(self._workspace_dir), session_id, snapshot)
            except Exception as file_exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] write plan pause file failed session=%s: %s",
                    session_id,
                    file_exc,
                )

            logger.info(
                "[JiuWenClawDeepAdapter] plan pause persisted session=%s",
                session_id,
            )

            if modify_tool is None:
                return None
            file_path = modify_tool.file_path_for_session(session_id)
            updated_todos = await modify_tool.load_todos(file_path)
            if updated_todos and self._stream_event_rail is not None:
                return self._stream_event_rail.format_todos_for_frontend(updated_todos)
            return None
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] finalize plan pause after cancel failed: %s",
                exc,
            )
            return None
        finally:
            await session.post_run()

    async def _persist_interrupt_artifacts_summary(self, session_id: str) -> None:
        """Cancel 时：将 session state 中实时记录的工具产物日志格式化为摘要字符串，作为兜底恢复信息。

        task_execution_rail 在每次工具调用后已将产物信息追加到 session state 的
        INTERRUPT_ARTIFACTS_SUMMARY_KEY（list 格式）。cancel 时将此 list 转换为
        可读的文本摘要并覆盖回 session state（string 格式），同时写一份到磁盘文件
        <workspace>/context/<session_id>/recovery/recovery.json，供进程重启后兜底。

        当 plan_pause 或 interrupt_resume 都无法正常工作时，下一轮新请求仍能
        通过此摘要识别哪些工作已经完成，避免盲目重复执行已完成步骤。
        """
        if not session_id or self._instance is None:
            return

        session = create_agent_session(session_id=session_id, card=self._instance.card)
        await session.pre_run(inputs=None)
        try:
            artifacts_log = session.get_state(INTERRUPT_ARTIFACTS_SUMMARY_KEY)
            if not isinstance(artifacts_log, list) or not artifacts_log:
                return

            summary_lines: list[str] = []
            for entry in artifacts_log:
                if not isinstance(entry, dict):
                    continue
                tool = str(entry.get("tool", ""))
                fp = str(entry.get("file_path", ""))
                emitted = entry.get("artifacts_emitted", False)

                if fp and emitted:
                    summary_lines.append(f"- {tool} {fp} (产物已生成)")
                elif fp:
                    summary_lines.append(f"- {tool} {fp} (已执行)")
                elif tool:
                    summary_lines.append(f"- {tool} (已执行)")

            if not summary_lines:
                return

            summary = "\n".join(summary_lines)

            # 写入 session state（内存，同一 session 内可用）
            write_interrupt_artifacts_summary_to_session(session, summary)
            await post_agent_execute_for_session(session, self._checkpointer)

            # 同时写入磁盘文件（进程重启后兜底）
            try:
                workspace_dir = Path(self._workspace_dir)
                write_interrupt_artifacts_to_file(workspace_dir, session_id, summary)
            except Exception as file_exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] write interrupt artifacts to file failed session=%s: %s",
                    session_id, file_exc,
                )

            logger.info(
                "[JiuWenClawDeepAdapter] interrupt artifacts summary persisted session=%s items=%d",
                session_id,
                len(summary_lines),
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] persist interrupt artifacts summary failed: %s",
                exc,
            )
        finally:
            await session.post_run()

    async def prepare_interrupt_artifacts_for_request(self, request: AgentRequest) -> None:
        """兜底：plan_pause 和 interrupt_resume 都没触发时，注入中断产物摘要到 supplementary_info。

        确保即使 todo.json 不存在、plan_paused 未写入，LLM 仍能获得结构化的"已完成工作"信息，
        避免从中断后的压缩对话历史中猜测任务进度而重复执行已完成步骤。

        读取优先级：session state（内存） > disk file（进程重启兜底）。
        """
        if self._instance is None:
            return

        session_id = str(request.session_id or "").strip()
        if not session_id:
            return

        params = request.params if isinstance(getattr(request, "params", None), dict) else None
        if params is None:
            return

        session = create_agent_session(session_id=session_id, card=self._instance.card)
        await session.pre_run(inputs=None)
        # SkillTurbo 节点产物存在独立的 __skill_turbo checkpointer key 下，需用单独的 session 读写
        skill_turbo_session = create_agent_session(session_id=session_id, card=self._instance.card)
        _skill_turbo_set_agent_id(skill_turbo_session, self._instance.card)
        await skill_turbo_session.pre_run(inputs=None)
        try:
            # 哨兵：plan_pause 或 interrupt_resume 已经注入，不需要兜底
            if is_interrupt_recovery_injected(session):
                return

            # 先读 session state（内存，最快路径）
            summary = read_interrupt_artifacts_summary_from_session(session)

            # 读不到时，尝试从磁盘文件读（进程重启兜底）
            if not summary:
                try:
                    workspace_dir = Path(self._workspace_dir)
                    summary = read_interrupt_artifacts_from_file(workspace_dir, session_id)
                except Exception as file_exc:
                    logger.debug(
                        "[JiuWenClawDeepAdapter] read interrupt artifacts from file failed session=%s: %s",
                        session_id, file_exc,
                    )

            # 读取 SkillTurbo 节点产物（中断的 skill_turbo 工具内部产物）
            skill_turbo_summary = await self._read_skill_turbo_node_artifacts_summary(skill_turbo_session)
            if skill_turbo_summary:
                if summary:
                    summary = f"{summary}\n{skill_turbo_summary}"
                else:
                    summary = skill_turbo_summary

            if not summary:
                return

            language = self._resolve_runtime_language()
            prompt = build_interrupt_artifacts_resume_prompt(language, summary=summary)
            merge_supplementary_into_request_params(params, prompt)

            # 一次性使用：注入后即清除（session state + disk file）
            clear_interrupt_artifacts_summary_from_session(session)
            # 同时清除 SkillTurbo 节点产物记录
            from jiuwenclaw.agentserver.skill_turbo.node_artifact_store import (
                clear_node_artifacts,
            )
            await clear_node_artifacts(skill_turbo_session)
            try:
                workspace_dir = Path(self._workspace_dir)
                clear_interrupt_artifacts_file(workspace_dir, session_id)
            except Exception as file_exc:
                logger.debug(
                    "[JiuWenClawDeepAdapter] clear interrupt artifacts file failed session=%s: %s",
                    session_id, file_exc,
                )
            mark_interrupt_recovery_injected(session)
            await post_agent_execute_for_session(session, self._checkpointer)

            logger.info(
                "[JiuWenClawDeepAdapter] interrupt artifacts summary injected session=%s",
                session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] prepare_interrupt_artifacts_for_request failed session_id=%s: %s",
                session_id,
                exc,
            )
        finally:
            await session.post_run()
            await skill_turbo_session.post_run()

    @staticmethod
    async def _read_skill_turbo_node_artifacts_summary(session: Any) -> str | None:
        """读取 SkillTurbo 节点产物记录，格式化为可读摘要文本。

        SkillTurbo executor 在中断时将节点产物持久化到 session state 的
        ``__skill_turbo_node_artifacts__`` key。此方法通过 ``load_node_artifacts``
        读取并格式化为摘要（调用方需已 ``pre_run``）。
        """
        from jiuwenclaw.agentserver.skill_turbo.node_artifact_store import (
            load_node_artifacts,
        )
        state = await load_node_artifacts(session)
        if not state:
            return None
        nodes = state.get("nodes") or {}
        skill = state.get("skill", "unknown")
        summaries = JiuWenClawDeepAdapter._build_skill_turbo_artifacts_summary(nodes)
        if not summaries:
            return None
        lines = [f"[SkillAccelerationExec ({skill}) 已完成节点产物]"] + summaries
        logger.info(
            "[JiuWenClawDeepAdapter] SkillTurbo node artifacts found session=%s nodes=%d",
            getattr(session, "session_id", "?"),
            len(nodes),
        )
        return "\n".join(lines)

    @staticmethod
    def _build_skill_turbo_artifacts_summary(nodes: dict[str, Any]) -> list[str]:
        """将节点产物 nodes 构建为可读摘要列表。

        格式: ``- {plan_name}: {info 摘要} | 文件: {路径列表}``
        """
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

    async def prepare_plan_pause_for_request(self, request: AgentRequest) -> None:
        """On next agent.plan message after cancel: clear task_plan, inject decision prompt, clear flag."""
        if self._instance is None:
            return

        session_id = str(request.session_id or "").strip()
        if not session_id:
            return

        params = request.params if isinstance(getattr(request, "params", None), dict) else None
        if params is None:
            return

        mode = str(params.get("mode", "agent.plan") or "agent.plan").strip()
        if mode != "agent.plan":
            return

        session = create_agent_session(session_id=session_id, card=self._instance.card)
        await session.pre_run(inputs=None)
        try:
            # 哨兵：已有其他恢复机制注入，跳过
            if is_interrupt_recovery_injected(session):
                return

            paused, snapshot = read_plan_pause_from_session(session)
            # CP may have been overwritten by the aborted stream; fall back to disk.
            if not paused:
                try:
                    paused, snapshot = read_plan_pause_from_file(
                        Path(self._workspace_dir), session_id
                    )
                except Exception as file_exc:
                    logger.debug(
                        "[JiuWenClawDeepAdapter] read plan pause file failed session=%s: %s",
                        session_id,
                        file_exc,
                    )
            if not paused:
                return

            state = self._instance.load_state(session)
            if clear_task_plan_on_state(state):
                self._instance.save_state(session, state)

            has_new_file = _has_uploaded_file(params)
            decision = build_paused_plan_decision_prompt_from_session_snapshot(
                self._resolve_runtime_language(),
                snapshot,
                has_new_file=has_new_file,
            )
            merge_supplementary_into_request_params(params, decision)
            clear_plan_pause_on_session(session)
            try:
                clear_plan_pause_file(Path(self._workspace_dir), session_id)
            except Exception as file_exc:
                logger.debug(
                    "[JiuWenClawDeepAdapter] clear plan pause file failed session=%s: %s",
                    session_id,
                    file_exc,
                )
            mark_interrupt_recovery_injected(session)
            await post_agent_execute_for_session(session, self._checkpointer)

            logger.info(
                "[JiuWenClawDeepAdapter] plan pause decision prompt injected session=%s",
                session_id,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] prepare_plan_pause_for_request failed session_id=%s: %s",
                session_id,
                exc,
            )
        finally:
            await session.post_run()

    async def prepare_interrupt_resume_for_request(self, request: AgentRequest) -> None:
        """On agent.plan continue/resume: inject todo resume guidance when active todos exist."""
        await prepare_interrupt_resume_for_request(self, request)

    async def prepare_stale_todo_cleanup_for_new_request(self, request: AgentRequest) -> bool:
        """Cancel orphaned active todos before a fresh non-resume user turn."""
        if self._instance is None:
            return False
        return await prepare_stale_todo_cleanup_for_request(
            request,
            agent_card=self._instance.card,
            get_todo_modify_tool=self._get_todo_modify_tool,
        )

    async def _cancel_pending_todos(self, session_id: str) -> list[dict] | None:
        """将未完成的 todo 项标记为 cancelled.

        Returns:
            更新后的 todo 列表（前端格式），用于附加到 interrupt_result 事件通知前端刷新。
            如果没有 todo 或操作失败，返回 None。
        """
        modify_tool = self._get_todo_modify_tool(session_id)
        if modify_tool is None:
            return None

        file_path = modify_tool.file_path_for_session(session_id)
        try:
            if await cancel_pending_todos_on_tool(modify_tool, session_id):
                logger.info(
                    "[JiuWenClawDeepAdapter] 已将 session %s 的未完成任务标记为 cancelled",
                    session_id,
                )

            updated_todos = await modify_tool.load_todos(file_path)
            if updated_todos and self._stream_event_rail is not None:
                return self._stream_event_rail.format_todos_for_frontend(updated_todos)
            return None
        except Exception as exc:
            logger.warning("[JiuWenClawDeepAdapter] 标记 todo cancelled 失败: %s", exc)
            return None

    # ──────────── SkillTurbo 集成 ────────────

    def build_skill_turbo_config(self) -> dict[str, Any]:
        """构建 SkillTurbo 配置."""
        tool_cards = []
        if self._instance is not None:
            ability_manager = getattr(self._instance, "ability_manager", None)
            if ability_manager is not None:
                tool_cards = ability_manager.list()

        # 创建 fallback handler，复用 DeepAgent 的工具/rail/模型/权限配置
        fallback_handler = self._create_skill_turbo_fallback_handler()

        return {
            "skill_codes_dir": "jiuwenclaw.agentserver.skill_turbo.skill_codes",
            "tool_cards": tool_cards,
            "model_client": self._model,
            "fallback_handler": fallback_handler,
            # 传递 agent card，executor 创建 session 时需要它来初始化 checkpointer，
            # 否则 session.pre_run/post_run 会因 card.id 为 None 崩溃，导致 resume_ctx 无法持久化。
            "card": self._instance.card if self._instance is not None else None,
            # LLM 并发上限，同一 SkillTurboExecutor 内最多并行 LLM 调用数
            "llm_concurrency_limit": 20,
            # 多模态能力透传：复用 DeepAdapter 已算好的可用性判断（config.yaml 各 model
            # 是否配独立 api_key + 环境变量是否齐全），与主 agent 行为保持一致。
            # 注意 _video_model_config 在 DeepAdapter 侧是 bool，skill_turbo 侧字段名为 video_model_enabled。
            "vision_model_config": self._vision_model_config,
            "audio_model_config": self._audio_model_config,
            "video_model_enabled": bool(self._video_model_config),
            "image_gen_enabled": bool(self._image_gen_enabled),
            # 显式传入主 agent 的 sys_operation，确保 SkillTurbo 使用主 agent 的不受限 sysop
            # （restrict_to_sandbox=False），而非走 Runner.resource_mgr.get_sys_operation()
            # 无参数路径取到残留 subagent 的受限 sysop（restrict_to_sandbox=True）。
            "sys_operation": self._sys_operation,
        }

    def _create_skill_turbo_fallback_handler(self) -> Any:
        """创建 SkillTurbo 节点级 fallback handler。

        基于 DeepAgent subagent，复用主 agent 的工具、rail、模型、权限配置。
        每次请求创建独立的 subagent session，避免污染主会话。
        """
        from jiuwenclaw.agentserver.skill_turbo.fallback_handler import DeepAgentFallbackHandler

        return DeepAgentFallbackHandler(
            adapter=self,
            request_id="",
            channel_id="",
            session_id="",
        )

    async def _try_skill_turbo_resume(
        self,
        request: AgentRequest,
        inputs: dict[str, Any],
    ) -> AsyncIterator[AgentResponseChunk] | None:
        """检测 resume 请求并走 SkillTurbo resume 路径。

        仅处理 answers 非空 + resume_ctx 存在的请求。
        其他请求（无 answers 或无 resume_ctx）返回 None，由 DeepAgent 主流程处理。
        """
        params = request.params if isinstance(getattr(request, "params", None), dict) else {}
        answers: list = params.get("answers") or []

        if not answers:
            return None

        if self._instance is None:
            return None

        session = create_agent_session(
            session_id=request.session_id or "default",
            card=self._instance.card,
        )
        _skill_turbo_set_agent_id(session, self._instance.card)
        resume_ctx = await _skill_turbo_load_resume_ctx(session)
        if resume_ctx is None:
            logger.warning(
                "[JiuWenClawDeepAdapter] SkillTurbo resume requested but resume_ctx is None; "
                "falling back to DeepAgent. session_id=%s",
                request.session_id,
            )
            # pre_run 已在 _skill_turbo_load_resume_ctx 中执行，需配对 post_run 释放资源
            try:
                await session.post_run()
            except Exception:
                pass
            return None

        logger.info(
            "[JiuWenClawDeepAdapter] SkillTurbo resume detected: tcid=%s",
            resume_ctx.get("pending_tool_call_id"),
        )
        # 清除 harness 的 ToolInterruptionState：SkillTurbo 有自己的 resume 机制
        # （resume_ctx + resume_stream），不走 harness 的 handle_resume（重执 tool call）。
        # 若不清除，下次 invoke 会检测到并尝试 harness resume，导致重复执行。
        try:
            from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
            session.update_state({INTERRUPTION_KEY: None})
        except Exception as exc:
            logger.debug(
                "[JiuWenClawDeepAdapter] clear ToolInterruptionState failed: %s", exc
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
        from jiuwenclaw.agentserver.skill_turbo.agent import SkillTurbo, SkillTurboNotHandled

        async def _resume_impl() -> AsyncIterator[AgentResponseChunk]:
            token_trace_sid = _LLM_TRACE_SESSION_ID.set(request.session_id or "default")
            token_trace_rid = _LLM_TRACE_REQUEST_ID.set(request.request_id or "")
            token_trace_iter = _LLM_TRACE_ITERATION.set(0)
            token_trace_model = _LLM_TRACE_MODEL_NAME.set(
                getattr(self._model, "model_config", None) and getattr(self._model.model_config, "model_name", "") or ""
            )
            skill_turbo = SkillTurbo(self.build_skill_turbo_config())

            # 将前端 answers 转为 ConfirmPayload（rail 期望格式）
            user_input = self._skill_turbo_answers_to_confirm_payload(answers, resume_ctx)

            params = request.params or {}
            raw_interactive = params.get(
                "interactive_ask", params.get("interactiveAsk")
            )
            interactive_ask = bool(raw_interactive) if raw_interactive is not None else False

            try:
                await _skill_turbo_clear_resume_ctx(session)
                try:
                    await session.post_run()
                except Exception:
                    pass
                async with ask_user_question_request_scope(
                    interactive_ask=interactive_ask,
                    session_id=request.session_id or "default",
                    stream_request_id=request.request_id or "",
                    channel_id=request.channel_id or "",
                    scope=RuntimeScopeKey.from_adapter(
                        self, session_id=request.session_id or "default"
                    ),
                ):
                    async for chunk in skill_turbo.resume_stream(
                        plan_code=resume_ctx["plan_code"],
                        inputs=resume_ctx.get("inputs", inputs),
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        pending_tool_call_id=resume_ctx["pending_tool_call_id"],
                        user_input=user_input,
                    ):
                        yield chunk
            except _SkillTurboAbortError as e:
                # 二次中断：理论上不应发生（rail 已收到答案），但防御性处理
                async for hitl_chunk in self._emit_skill_turbo_hitl_chunks(
                    request, e
                ):
                    yield hitl_chunk
                return
            except SkillTurboNotHandled:
                logger.info("[JiuWenClawDeepAdapter] SkillTurbo resume fallback to DeepAgent")
                return
            finally:
                _reset_llm_trace_tokens(token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model)

        return _resume_impl()

    @staticmethod
    def _skill_turbo_answers_to_confirm_payload(
        answers: list,
        resume_ctx: dict[str, Any],
    ) -> _SkillTurboConfirmPayload:
        """将前端 answers（用户对 ask_user_question 的答复）转为 ConfirmPayload。

        前端 answers 结构：
            [{"question": "...", "answer": "本次允许" / "总是允许" / "拒绝", ...}]

        映射：
            - "拒绝" → approved=False
            - "总是允许" → approved=True, auto_confirm=True, persist_allow=True
            - "本次允许" → approved=True
        """
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
            return _SkillTurboConfirmPayload(approved=True, auto_confirm=True, persist_allow=True)
        return _SkillTurboConfirmPayload(approved=True)

    @staticmethod
    async def _emit_skill_turbo_hitl_chunks(
        request: AgentRequest,
        abort_exc: _SkillTurboAbortError,
    ) -> AsyncIterator[AgentResponseChunk]:
        """AbortError → HITL 三件套 chunk（tool_call pending + ask_user_question + invocation_paused）。

        此方法主要用于 resume 路径的二次中断防御性处理。首次中断时 executor 已主动
        向 parent_session 写 __interaction__ 流事件，不经过此方法。
        """
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

        # (1) chat.tool_call — pending_approval
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

        # (1.5) chat.tool_update — 让前端建立工具调用卡片上下文
        # DeepAgent 正常 HITL 路径中，前端会先收到 chat.tool_update(status=in_progress)，
        # 再收到 chat.ask_user_question。前端依赖 tool_update 来渲染工具调用卡片，
        # 然后在卡片上叠加审批按钮。SkillTurbo 不走 LLM tool_calls.delta 流程，
        # 所以需要补一个 tool_update 让前端能正确渲染。
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

        # (2) chat.ask_user_question — 复用 DeepAgent 格式
        # 构造逻辑与 executor 侧共用 build_interaction_output_from_abort，保持一致。
        interaction_output = _skill_turbo_build_interaction_output(abort_exc)
        if interaction_output is None:
            logger.warning(
                "[JiuWenClawDeepAdapter] HITL build_interaction_output returned None; tool=%s tcid=%s",
                tc_data.get("name"),
                tc_data.get("id"),
            )
        else:
            ask_payload = convert_interactions_to_ask_user_question([
                interaction_output
            ])
            logger.info(
                "[JiuWenClawDeepAdapter] HITL ask_payload type=%s value=%s",
                type(ask_payload).__name__,
                ask_payload,
            )
            if ask_payload:
                # 使用原始 request_id（rid），而非 tool_call_id。
                # relay-claw 的 permissionBridge 用 payload.request_id 作为 jiuwenRequestId，
                # resume 时把它放进 params.request_id 发回 jiuwenclaw。
                # jiuwenclaw 的 resume 路径靠 session_id 找 resume_ctx，不依赖 request_id 匹配，
                # 但保持 request_id 一致可避免 relay-claw 侧的队列路由错乱。
                if isinstance(ask_payload, dict):
                    ask_payload["request_id"] = rid
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=ask_payload,
                    is_complete=False,
                )
            else:
                logger.warning(
                    "[JiuWenClawDeepAdapter] HITL ask_payload is None; tool=%s tcid=%s",
                    tc_data.get("name"),
                    tc_data.get("id"),
                )

        # (3) chat.invocation_paused — 标记等待用户输入
        # is_complete=True + awaiting_user_input=True → E2A 网关转为 is_final=False，
        # relay-claw 的 consumeFrames 循环不会 break，流保持开启。
        # 用户授权后，relay-claw permissionBridge.submitAnswer 将 resume 帧转发回原始队列，
        # consumeFrames 循环继续消费 → 前端看到续跑内容。
        # 不要在此之后追加 is_final=True 终止帧，否则 consumeFrames 提前 break，resume 帧丢失。
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=cid,
            payload={
                "event_type": "chat.invocation_paused",
                "awaiting_user_input": True,
            },
            is_complete=True,
        )

    async def _load_deepresearch_rewrite_html_target(
            self,
            session_id: str,
    ) -> RewriteHtmlTarget | None:
        """Restore the trusted HTML target from this adapter's checkpointer."""
        instance = getattr(self, "_instance", None)
        checkpointer = getattr(self, "_checkpointer", None)
        if instance is None or checkpointer is None or not session_id:
            return None

        try:
            session = create_agent_session(
                session_id=session_id,
                card=instance.card,
            )
            # Deliberately bypass Session.pre_run(), which uses the global
            # default checkpointer instead of this tenant adapter's store.
            await checkpointer.pre_agent_execute(
                session._inner,  # pylint: disable=protected-access
                None,
            )
            return target_from_state(
                session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error(
                "[DeepResearchRewriteHtmlFollowup] restore target failed "
                "session_id=%s",
                session_id,
                exc_info=True,
            )
            return None

    async def _try_deepresearch_rewrite_html_followup(
            self,
            query: object,
            session_id: str,
    ) -> RewriteHtmlFollowupResult | None:
        """Handle an explicit HTML follow-up without routing through the LLM."""
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

        from jiuwenclaw.agentserver.tools.deepresearch import rewrite_tools

        try:
            html_tool = rewrite_tools.deepresearch_generate_rewrite_html
            raw_result = await html_tool._func(  # pylint: disable=protected-access
                report_path=target.report_path,
                revision_id=target.revision_id,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error(
                "[DeepResearchRewriteHtmlFollowup] HTML tool failed "
                "session_id=%s",
                session_id,
                exc_info=True,
            )
            raw_result = None
        return decode_html_tool_result(raw_result)

    def _build_deepresearch_rewrite_model(self) -> Model:
        request_env = get_task_env_overlay()
        if request_env is None:
            raise RuntimeError(
                "DeepResearch rewrite requires a bound request environment"
            )

        config_base = patch_model_config_from_env(get_config(), request_env)
        entries = get_default_models(config_base)
        if not entries:
            raise ValueError(
                "DeepResearch rewrite model configuration is unavailable"
            )

        entry = entries[0]
        model_client_config = dict(entry.get("model_client_config") or {})
        model_client_config["claw_config"] = config_base
        return self._build_model_from_entry(
            model_client_config,
            entry.get("model_config_obj") or {},
        )

    async def _try_deepresearch_rewrite_fast_path(
            self,
            query: object,
    ) -> RewriteFastPathResult | None:
        from jiuwenclaw.agentserver.tools.deepresearch import rewrite_tools

        rewrite_model: Model | None = None

        async def _invoke_request_model(*args, **kwargs):
            nonlocal rewrite_model
            if rewrite_model is None:
                rewrite_model = self._build_deepresearch_rewrite_model()
            return await rewrite_model.invoke(*args, **kwargs)

        return await run_rewrite_fast_path(
            query,
            model_invoke=_invoke_request_model,
            prepare_invoke=rewrite_tools.deepresearch_prepare_rewrite._func,  # pylint: disable=protected-access
            commit_invoke=rewrite_tools.deepresearch_commit_rewrite._func,  # pylint: disable=protected-access
        )

    async def _persist_deepresearch_rewrite_fast_path_turn(
            self,
            *,
            session_id: str,
            query: object,
            result: RewriteFastPathResult,
    ) -> bool:
        """Persist a fast rewrite as a valid tool-call conversation turn."""
        if (
            self._instance is None
            or not session_id
            or not isinstance(query, str)
        ):
            return False
        html_target = target_from_commit_result(result.commit_result)
        if html_target is None:
            return False

        context_engine = resolve_context_engine(self._instance)
        react_agent = (
            getattr(self._instance, "react_agent", None)
            or getattr(self._instance, "_react_agent", None)
        )
        init_context = getattr(react_agent, "_init_context", None)
        if context_engine is None or not callable(init_context):
            return False

        session = None
        owned = False
        try:
            session, owned = await _resolve_session_for_checkpoint(
                self._instance,
                session_id,
                card=self._instance.card,
            )
            if owned:
                await session.pre_run(inputs=None)
            actual_session = getattr(session, "_parent", session) or session
            context = await init_context(actual_session)

            tool_call_id = f"rewrite-fast-path-{uuid.uuid4().hex}"
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
            session.update_state({
                PENDING_HTML_EXPORT_STATE_KEY: html_target.to_state(),
            })
            await context.add_messages([
                UserMessage(content=query),
                AssistantMessage(content="", tool_calls=[tool_call]),
                ToolMessage(content=tool_result, tool_call_id=tool_call_id),
                AssistantMessage(content=result.message),
            ])
            await context_engine.save_contexts(actual_session)
            await post_agent_execute_for_session(session, self._checkpointer)
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error(
                "[DeepResearchRewriteFastPath] persist tool result failed "
                "session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
                exc_info=True,
            )
            return False
        finally:
            if owned and session is not None:
                try:
                    await session.post_run()
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.error(
                        "[DeepResearchRewriteFastPath] session cleanup failed "
                        "session_id=%s",
                        session_id,
                        exc_info=True,
                    )

    @staticmethod
    def _fast_path_chunks(
            result: RewriteFastPathResult,
            *,
            request_id: str,
            channel_id: str,
    ) -> tuple[AgentResponseChunk, ...]:
        if result.status == "completed":
            payload = {"event_type": "chat.final", "content": result.message}
        else:
            code = result.error_code or "INTERNAL_ERROR"
            content = f"改写失败（{code}）：{result.message}"
            payload = {"event_type": "chat.error", "error": content}
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

        # ── SkillTurbo V2：已工具化，不再由适配器主动尝试 ──

        session_id = request.session_id or "default"
        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent.plan")
        runtime_mode = self._normalize_rail_mode(mode)
        self._last_runtime_mode = runtime_mode
        (
            chat_env_token,
            chat_fp_token,
            chat_skill_dirs_token,
            chat_ns_token,
            chat_browser_pin,
        ) = await self._on_chat_request_start()

        if self._plain_chat_should_clear_stale_interrupt(request):
            await self._clear_session_persisted_interrupt_state(
                session_id,
                reason="plain_user_message_before_agent_run",
            )

        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(request.request_id or "")
        token_request_id = set_request_id(request.request_id or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self._model, "model_config", None) and getattr(self._model.model_config, "model_name", "") or ""
        )

        slash_result = await self._handle_slash_command(query, session_id, mode)
        if slash_result is not None:
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
                    payload = {"content": content}
                reset_request_id(token_request_id)
                _reset_llm_trace_tokens(token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model)
                await self._on_chat_request_end(
                    chat_env_token,
                    chat_fp_token,
                    chat_skill_dirs_token,
                    chat_ns_token,
                    chat_browser_pin,
                )
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
            mode=mode
        )
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)

        # Set telemetry context for OpenTelemetry span creation
        if self._telemetry_rail is not None:
            self._telemetry_rail.set_telemetry_context(
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                metadata=request.metadata,
            )
        disk_service_id, disk_agent_id = self._tenant_disk_ids()
        set_perf_summary_context(
            self._request_summary_rail,
            channel_id=request.channel_id or "",
            session_id=request.session_id or "",
            request_id=request.request_id or "",
            mode=mode,
            service_id=disk_service_id,
            agent_id=disk_agent_id,
        )

        perf_summary_status = "ok"
        response_ok = True
        try:
            await self._update_runtime_config(
                _RuntimeConfigParams.from_agent_request(request, mode)
            )

            resolved_model = self._resolve_model_for_request(request)
            self._apply_model_to_react_agent(resolved_model)
            if self._runtime_prompt_rail:
                self._runtime_prompt_rail.set_model_name(self._resolve_model_name())

            html_followup_result = (
                await self._try_deepresearch_rewrite_html_followup(
                    query,
                    session_id,
                )
            )
            if html_followup_result is not None:
                result = html_followup_result.message
                response_ok = html_followup_result.status == "completed"
                if not response_ok:
                    perf_summary_status = "error"
                logger.info(
                    "[DeepResearchRewriteHtmlFollowup] request_id=%s "
                    "session_id=%s status=%s error_code=%s",
                    request.request_id,
                    session_id,
                    html_followup_result.status,
                    html_followup_result.error_code,
                    extra={"user_visible": "critical"},
                )
            else:
                result = await Runner.run_agent(
                    agent=self._instance,
                    inputs=inputs,
                )
        except asyncio.CancelledError:
            perf_summary_status = "cancelled"
            logger.info("[JiuWenClawDeepAdapter] Agent 任务被取消: request_id=%s session_id=%s", request.request_id,
                        session_id)
            raise
        except ValueError as exc:
            perf_summary_status = "error"
            logger.warning(
                "[JiuWenClawDeepAdapter] model resolution failed: request_id=%s error=%s",
                request.request_id,
                exc,
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "event_type": "chat.error",
                    "error": str(exc),
                    "code": "model_not_found",
                },
                metadata=request.metadata,
            )
        except IrreducibleContextError as exc:
            perf_summary_status = "error"
            logger.error(
                "[JiuWenClawDeepAdapter] 上下文不可再压缩: request_id=%s session_id=%s",
                request.request_id,
                session_id,
                extra={"user_visible": "critical"},
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"content": exc.user_message()},
                metadata=request.metadata,
            )
        except Exception as e:
            perf_summary_status = "error"
            logger.error("[JiuWenClawDeepAdapter] Agent 任务执行异常: %s", e, extra={'user_visible': 'critical'})
            raise
        finally:
            TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
            cleanup_permission_context(token_perm)
            self._reset_runtime_cron_context(cron_context_tokens)
            reset_request_id(token_request_id)
            _reset_llm_trace_tokens(token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model)
            if request.request_id:
                self._untrack_session_toolkit(request.request_id)
            self._cleanup_circuit_breaker_session(session_id)
            await self._on_chat_request_end(
                chat_env_token,
                chat_fp_token,
                chat_skill_dirs_token,
                chat_ns_token,
                chat_browser_pin,
            )
            finalize_perf_summary_request(request.request_id, status=perf_summary_status)
            clear_perf_summary_context()

        content = result if isinstance(result, (str, dict)) else str(result)

        # Finalize is owned by generate_evolution_merge_version (RPC / auto-rebuild).
        # /evolve_rebuild no longer yields run_rebuild_followup slash results.

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=response_ok,
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
        usage_accumulator = self._new_usage_accumulator()

        # ── SkillTurbo V2：resume 请求仍由适配器层面路由 ──
        skill_turbo_resume_stream = await self._try_skill_turbo_resume(request, inputs)
        if skill_turbo_resume_stream is not None:
            async for chunk in skill_turbo_resume_stream:
                yield chunk
            return

        # ── SkillTurbo V2：正常请求已工具化（skill_turbo），由 LLM 在 ReAct 循环中选择调用 ──

        query = request.params.get("query", "")
        mode = request.params.get("mode", "agent.plan")
        runtime_mode = self._normalize_rail_mode(mode)
        self._last_runtime_mode = runtime_mode
        (
            chat_env_token,
            chat_fp_token,
            chat_skill_dirs_token,
            chat_ns_token,
            chat_browser_pin,
        ) = await self._on_chat_request_start()
        raw_interactive = request.params.get("interactive_ask", request.params.get("interactiveAsk"))
        interactive_ask = bool(raw_interactive) if raw_interactive is not None else False
        token_trace_sid = _LLM_TRACE_SESSION_ID.set(session_id)
        token_trace_rid = _LLM_TRACE_REQUEST_ID.set(rid or "")
        token_request_id = set_request_id(rid or "")
        token_trace_iter = _LLM_TRACE_ITERATION.set(0)
        token_trace_model = _LLM_TRACE_MODEL_NAME.set(
            getattr(self._model, "model_config", None) and getattr(self._model.model_config, "model_name", "") or ""
        )

        # Team mode entry (canonicalize agent.team -> team).
        mode_norm = str(mode or "").strip().lower()
        if mode_norm in {"team", "agent.team", "team.plan", "code.team"}:
            if mode_norm == "agent.team" and isinstance(request.params, dict):
                request.params["mode"] = "team"
                mode = "team"
            from jiuwenclaw.agentserver.deep_agent.team_helpers import process_team_message_stream

            # team 模式下，只在用户/relay-claw 显式传了 model_name 时才注入
            # _resolved_model_config（触发 team_helpers 的模型切换路径）。
            # 没传 model_name 时让 team 走自己的配置默认（leader/teammate 的
            # glm-5.2，由 TeamConfigLoader 加载），不注入 sidecar 基线
            # （self._model，agentteam breed 的 defaultModel=glm-5）覆盖 team 默认。
            _team_params = request.params if isinstance(request.params, dict) else {}
            _team_requested_model = str(_team_params.get("model_name") or "").strip()

            try:
                resolved_model = self._resolve_model_for_request(request)
            except ValueError as exc:
                reset_request_id(token_request_id)
                _reset_llm_trace_tokens(
                    token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model
                )
                await self._on_chat_request_end(
                    chat_env_token,
                    chat_fp_token,
                    chat_skill_dirs_token,
                    chat_ns_token,
                    chat_browser_pin,
                )
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "chat.error",
                        "error": str(exc),
                        "code": "model_not_found",
                    },
                    is_complete=True,
                )
                return
            # 仅在显式传了 model_name 时才同步 adapter 级字段并注入
            # _resolved_model_config，触发 team_helpers 的模型切换；未传
            # model_name 时让 team 走 TeamConfigLoader 的默认（glm-5.2），
            # 不用 sidecar 基线（self._model，agentteam breed 的
            # defaultModel=glm-5）覆盖 _model_request_config /
            # _model_client_config，否则 _resolve_model_name() /
            # _update_tools_for_mode() 等适配器级查询会报 glm-5 而非 team
            # 实际用的模型。
            if _team_requested_model and isinstance(request.params, dict):
                self._model_request_config = resolved_model.model_config
                self._model_client_config = resolved_model.model_client_config
                request.params["_resolved_model_config"] = {
                    "model_client_config": resolved_model.model_client_config.model_dump(mode="json"),
                    "model_request_config": (
                        resolved_model.model_config.model_dump(mode="json")
                        if resolved_model.model_config else None
                    ),
                }
            if self._runtime_prompt_rail:
                if _team_requested_model:
                    self._runtime_prompt_rail.set_model_name(resolved_model.model_config.model_name)
                # else: keep the rail's existing model_name (avoid sidecar
                # baseline overwrite) — team's actual model is owned by
                # TeamHarness, not the adapter.
                self._runtime_prompt_rail.set_mode(mode)
                self._runtime_prompt_rail.set_session_id(session_id)

            team_scope = RuntimeScopeKey.from_adapter(self, session_id=session_id)
            try:
                async for chunk in process_team_message_stream(
                    request,
                    inputs,
                    self._instance,
                    runtime_scope=team_scope,
                ):
                    yield chunk
            finally:
                reset_request_id(token_request_id)
                _reset_llm_trace_tokens(
                    token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model
                )
                await self._on_chat_request_end(
                    chat_env_token,
                    chat_fp_token,
                    chat_skill_dirs_token,
                    chat_ns_token,
                    chat_browser_pin,
                )
            return

        # 拦截斜杠命令
        slash_result = await self._handle_slash_command(query, session_id, mode)
        if slash_result is not None:
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
                reset_request_id(token_request_id)
                _reset_llm_trace_tokens(token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model)
                await self._on_chat_request_end(
                    chat_env_token,
                    chat_fp_token,
                    chat_skill_dirs_token,
                    chat_ns_token,
                    chat_browser_pin,
                )
                return

        if self._plain_chat_should_clear_stale_interrupt(request):
            await self._clear_session_persisted_interrupt_state(
                session_id,
                reason="plain_user_message_before_agent_run",
            )

        has_streamed_content = False
        accumulated_text = ""
        accumulated_reasoning = ""
        first_byte_marked = False
        evolution_status_started = False
        evolution_status_ended = False
        last_logged_iteration = -1  # 用于记录上次记录进度的迭代次数
        hitl_pending_stream = False
        suppress_stream_after_hitl = False

        def _mark_first_byte_once() -> None:
            nonlocal first_byte_marked
            if first_byte_marked:
                return
            first_byte_marked = True
            mark_request_first_byte()

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
        )

        # Set telemetry context for OpenTelemetry span creation
        if self._telemetry_rail is not None:
            self._telemetry_rail.set_telemetry_context(
                channel_id=request.channel_id or "",
                session_id=request.session_id or "",
                request_id=request.request_id or "",
                metadata=request.metadata,
            )
        disk_service_id, disk_agent_id = self._tenant_disk_ids()
        set_perf_summary_context(
            self._request_summary_rail,
            channel_id=request.channel_id or "",
            session_id=request.session_id or "",
            request_id=request.request_id or "",
            mode=mode,
            service_id=disk_service_id,
            agent_id=disk_agent_id,
        )
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)
        perf_summary_status = "ok"
        try:
            await self._update_runtime_config(
                _RuntimeConfigParams.from_agent_request(request, mode)
            )

            try:
                resolved_model = self._resolve_model_for_request(request)
                self._apply_model_to_react_agent(resolved_model)
                if self._runtime_prompt_rail:
                    self._runtime_prompt_rail.set_model_name(self._resolve_model_name())
            except ValueError as exc:
                reset_request_id(token_request_id)
                _reset_llm_trace_tokens(
                    token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model
                )
                TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
                cleanup_permission_context(token_perm)
                self._reset_runtime_cron_context(cron_context_tokens)
                await self._on_chat_request_end(
                    chat_env_token,
                    chat_fp_token,
                    chat_skill_dirs_token,
                    chat_ns_token,
                    chat_browser_pin,
                )
                finalize_perf_summary_request(request.request_id, status="error")
                clear_perf_summary_context()
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "chat.error",
                        "error": str(exc),
                        "code": "model_not_found",
                    },
                    is_complete=True,
                )
                return

            html_followup_result = (
                await self._try_deepresearch_rewrite_html_followup(
                    query,
                    session_id,
                )
            )
            fast_path_result = None
            fast_path_handled = html_followup_result is not None
            if html_followup_result is not None:
                has_streamed_content = True
                _mark_first_byte_once()
                if html_followup_result.status != "completed":
                    perf_summary_status = "error"
                logger.info(
                    "[DeepResearchRewriteHtmlFollowup] request_id=%s "
                    "session_id=%s status=%s error_code=%s",
                    rid,
                    session_id,
                    html_followup_result.status,
                    html_followup_result.error_code,
                    extra={"user_visible": "critical"},
                )
                for html_chunk in self._rewrite_html_followup_chunks(
                    html_followup_result,
                    request_id=rid,
                    channel_id=cid,
                ):
                    yield html_chunk
            else:
                fast_path_result = await self._try_deepresearch_rewrite_fast_path(
                    query
                )
                fast_path_handled = fast_path_result is not None

            if fast_path_result is not None:
                has_streamed_content = True
                _mark_first_byte_once()
                fast_path_usage = self._normalize_usage_metadata(
                    fast_path_result.usage_metadata
                )
                if fast_path_usage is not None:
                    self._accumulate_usage_metadata(
                        usage_accumulator,
                        fast_path_usage,
                    )
                if fast_path_result.status == "completed":
                    persisted = await self._persist_deepresearch_rewrite_fast_path_turn(
                        session_id=session_id,
                        query=query,
                        result=fast_path_result,
                    )
                    if not persisted:
                        fast_path_result = replace(
                            fast_path_result,
                            error_code="CONTEXT_PERSIST_FAILED",
                            message=(
                                "改写版本已创建，但未能保存后续生成 HTML "
                                "所需的版本上下文。"
                            ),
                        )
                if (
                    fast_path_result.status != "completed"
                    or fast_path_result.error_code is not None
                ):
                    perf_summary_status = "error"
                logger.info(
                    "[DeepResearchRewriteFastPath] request_id=%s session_id=%s "
                    "action=%s status=%s error_code=%s prepare_ms=%.3f "
                    "model_ms=%.3f commit_ms=%.3f total_ms=%.3f model_calls=%d "
                    "output_adjustments=%s model_output_error_reason=%s",
                    rid,
                    session_id,
                    fast_path_result.action,
                    fast_path_result.status,
                    fast_path_result.error_code,
                    fast_path_result.prepare_ms,
                    fast_path_result.model_ms,
                    fast_path_result.commit_ms,
                    fast_path_result.total_ms,
                    fast_path_result.model_calls,
                    ",".join(fast_path_result.model_output_adjustments) or "none",
                    fast_path_result.model_output_error_reason,
                    extra={'user_visible': 'critical'},
                )
                for fast_path_chunk in self._fast_path_chunks(
                    fast_path_result,
                    request_id=rid,
                    channel_id=cid,
                ):
                    yield fast_path_chunk

            if self._stream_event_rail is not None:
                self._stream_event_rail.reset_abort()
            request_scope = (
                _noop_async_context()
                if fast_path_handled
                else ask_user_question_request_scope(
                    interactive_ask=interactive_ask,
                    session_id=session_id,
                    stream_request_id=rid or "",
                    channel_id=cid or "",
                    scope=RuntimeScopeKey.from_adapter(self, session_id=session_id),
                )
            )
            agent_stream = (
                _empty_agent_stream()
                if fast_path_handled
                else Runner.run_agent_streaming(self._instance, inputs)
            )
            async with request_scope:
                if not fast_path_handled:
                    logger.info(
                        f"[JiuWenClawDeepAdapter] Agent执行开始: request_id={request.request_id} mode={mode}",
                        extra={'user_visible': 'critical'}
                    )
                async for chunk in agent_stream:
                    if not first_byte_marked:
                        if hasattr(chunk, "type") and chunk.type in {
                            "llm_output",
                            "llm_reasoning",
                            "answer",
                        }:
                            _mark_first_byte_once()
                        elif not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
                            parsed_probe = self._parse_stream_chunk_with_source(chunk)
                            if isinstance(parsed_probe, dict) and parsed_probe.get("event_type") in {
                                "chat.delta",
                                "chat.reasoning",
                                "chat.tool_call",
                                "chat.tool_result",
                                "chat.final",
                            }:
                                _mark_first_byte_once()
                    chunk_iteration = _extract_iteration_from_chunk(chunk)
                    if chunk_iteration is not None:
                        _LLM_TRACE_ITERATION.set(chunk_iteration)
                        # 每次迭代或每5次迭代记录一次进度（避免日志过多）
                        if chunk_iteration > last_logged_iteration and chunk_iteration % 5 == 0:
                            logger.info(
                                f"[JiuWenClawDeepAdapter] LLM迭代进度: iteration={chunk_iteration}",
                                extra={'user_visible': 'progress'}
                            )
                            last_logged_iteration = chunk_iteration
                    if suppress_stream_after_hitl:
                        continue
                    if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
                        parsed = self._parse_stream_chunk_with_source(chunk)
                        hitl_pending_stream = self._detect_hitl_pause(
                            parsed,
                            hitl_pending_stream=hitl_pending_stream,
                        )
                        should_pause_for_user_input = hitl_pending_stream
                        if parsed is not None:
                            # 工具执行日志标记
                            event_type = parsed.get("event_type")
                            if event_type == "chat.tool_call":
                                tool_info = parsed.get("tool_call", {})
                                tool_name = tool_info.get("name") if isinstance(tool_info, dict) else str(tool_info)
                                logger.info(f"[JiuWenClawDeepAdapter] 开始执行工具: {tool_name}",
                                            extra={'user_visible': 'critical'})
                            elif event_type == "chat.tool_result":
                                tool_name = parsed.get("tool_name", "unknown")
                                logger.info(f"[JiuWenClawDeepAdapter] 工具执行完成: {tool_name}",
                                            extra={'user_visible': 'critical'})
                            elif event_type == "chat.error":
                                logger.error(f"[JiuWenClawDeepAdapter] 工具执行失败: {parsed.get('error', 'unknown')}",
                                             extra={'user_visible': 'critical'})
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
                            if should_pause_for_user_input:
                                suppress_stream_after_hitl = True
                                continue
                        continue

                    chunk_type = chunk.type

                    if chunk_type == "llm_usage":
                        logger.info(f"[JiuWenClawDeepAdapter] llm_usage chunk: {chunk}")
                        usage_meta = self._extract_usage_metadata_from_payload(chunk.payload)
                        if usage_meta is not None:
                            self._accumulate_usage_metadata(usage_accumulator, usage_meta)
                        usage_payload = propagate_stream_source_id(chunk, {
                            "event_type": "chat.usage_metadata",
                            "metadata": chunk.payload,
                            "session_id": session_id,
                        })
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=usage_payload,
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
                        propagate_stream_source_id(chunk, delta_payload)
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
                        propagate_stream_source_id(chunk, delta_payload)
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
                            logger.info(
                                f"[JiuWenClawDeepAdapter] Evolution操作开始: request_id={rid}",
                                extra={'user_visible': 'progress'}
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
                            parsed = self._parse_stream_chunk_with_source(chunk, _has_streamed_content=True)
                            hitl_pending_stream = self._detect_hitl_pause(
                                parsed,
                                hitl_pending_stream=hitl_pending_stream,
                            )
                            should_pause_for_user_input = hitl_pending_stream
                            if parsed is not None:
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                    payload=parsed,
                                    is_complete=False,
                                )
                                if should_pause_for_user_input:
                                    suppress_stream_after_hitl = True
                                    continue
                            continue
                        parsed = self._parse_stream_chunk_with_source(chunk)
                        hitl_pending_stream = self._detect_hitl_pause(
                            parsed,
                            hitl_pending_stream=hitl_pending_stream,
                        )
                        should_pause_for_user_input = hitl_pending_stream
                        if parsed is not None:
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=parsed,
                                is_complete=False,
                            )
                            if should_pause_for_user_input:
                                suppress_stream_after_hitl = True
                                continue
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
                    parsed = self._parse_stream_chunk_with_source(chunk)
                    hitl_pending_stream = self._detect_hitl_pause(
                        parsed,
                        hitl_pending_stream=hitl_pending_stream,
                    )
                    should_pause_for_user_input = hitl_pending_stream
                    if parsed is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=parsed,
                            is_complete=False,
                        )
                        if should_pause_for_user_input:
                            suppress_stream_after_hitl = True
                            continue

            # Evolution runs as SkillEvolutionRail background task from after_invoke.
            # Do not await it on this stream — schedule a detached follow-up for
            # summary stash + auto rebuild so the user turn can complete immediately.
            if not accumulated_text and not hitl_pending_stream and not has_streamed_content:
                accumulated_text = "处理完成，但未生成回复内容。请尝试重新发送消息。"
                logger.warning(
                    "[JiuWenClawDeepAdapter] ReAct loop ended with no visible content — using fallback: request_id=%s",
                    rid,
                )

            if accumulated_text and not hitl_pending_stream:
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
            if accumulated_reasoning and not hitl_pending_stream:
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

            # Drain only approval events already ready (usually empty while evolution
            # is still running). Late summary / rebuild happen in a background task.
            if not fast_path_handled and self._skill_evolution_rail is not None:
                for evt in self._skill_evolution_rail.drain_pending_approval_events():
                    payload = evt.payload or {}
                    action = payload.get("action")

                    if action == "skill_creator_follow_up":
                        meta = payload.get("skill_create_meta", {})
                        if meta.get("auto_save") is True:
                            # auto_save=True：在同一 stream/request scope 内续跑 skill-creator
                            prompt = payload.get("skill_creator_prompt", "")
                            request_id = payload.get("request_id", "")
                            if prompt:
                                async for follow_chunk in self._iter_skill_creator_follow_up_stream(
                                    base_inputs=inputs,
                                    prompt=prompt,
                                    skill_create_request_id=request_id,
                                    stream_request_id=rid,
                                    channel_id=cid,
                                    session_id=session_id,
                                ):
                                    yield follow_chunk
                            else:
                                logger.warning(
                                    "[JiuWenClawDeepAdapter] skill_creator_follow_up dropped: "
                                    "empty prompt (request_id=%s stream_request_id=%s)",
                                    request_id,
                                    rid,
                                )
                            continue

                    parsed = self._parse_stream_chunk_with_source(evt)
                    hitl_pending_stream = self._detect_hitl_pause(
                        parsed,
                        hitl_pending_stream=hitl_pending_stream,
                    )
                    if parsed is not None:
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=parsed,
                            is_complete=False,
                        )

                # Always schedule followup to drain evolution summary and run
                # detached auto rebuild (independent of HITL pause on this turn).
                self._schedule_background_evolution_followup(
                    request_id=rid,
                    session_id=session_id,
                )

                logger.info(
                    f"[JiuWenClawDeepAdapter] Agent执行成功: request_id={request.request_id}",
                    extra={'user_visible': 'critical'}
                )

            # Finalize is owned by generate_evolution_merge_version (RPC / auto-rebuild).
            # /evolve_rebuild no longer yields run_rebuild_followup slash results.

            if evolution_status_started and not evolution_status_ended:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.evolution_status", "status": "end"},
                    is_complete=False,
                )
                logger.info(
                    f"[JiuWenClawDeepAdapter] Evolution操作完成: request_id={rid}",
                    extra={'user_visible': 'progress'}
                )
                evolution_status_ended = True
        except asyncio.CancelledError:
            perf_summary_status = "cancelled"
            logger.info("[JiuWenClawDeepAdapter] 流式任务被取消: request_id=%s session_id=%s", rid, session_id)
            raise
        except IrreducibleContextError as exc:
            perf_summary_status = "error"
            logger.error(
                "[JiuWenClawDeepAdapter] 上下文不可再压缩: request_id=%s session_id=%s",
                request.request_id,
                session_id,
                extra={"user_visible": "critical"},
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
                payload={"event_type": "chat.error", "error": exc.user_message()},
                is_complete=False,
            )
        except Exception as exc:
            perf_summary_status = "error"
            logger.error(
                f"[JiuWenClawDeepAdapter] Agent执行失败: request_id={request.request_id} error={str(exc)}",
                extra={'user_visible': 'critical'}
            )
            logger.exception("[JiuWenClawDeepAdapter] 流式任务异常: %s", exc)
            if evolution_status_started and not evolution_status_ended:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload={"event_type": "chat.evolution_status", "status": "end"},
                    is_complete=False,
                )
                logger.info(
                    f"[JiuWenClawDeepAdapter] Evolution操作完成: request_id={rid}",
                    extra={'user_visible': 'progress'}
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
            self._reset_runtime_cron_context(cron_context_tokens)
            reset_request_id(token_request_id)
            _reset_llm_trace_tokens(token_trace_sid, token_trace_rid, token_trace_iter, token_trace_model)
            if rid:
                self._untrack_session_toolkit(rid)
            self._cleanup_circuit_breaker_session(session_id)
            await self._on_chat_request_end(
                chat_env_token,
                chat_fp_token,
                chat_skill_dirs_token,
                chat_ns_token,
                chat_browser_pin,
            )
            finalize_perf_summary_request(request.request_id, status=perf_summary_status)
            clear_perf_summary_context()

        summary = self._build_usage_summary(usage_accumulator)

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

    def _schedule_background_evolution_followup(
        self,
        *,
        request_id: str,
        session_id: str,
    ) -> None:
        """Fire-and-forget: wait for rail evolution, then auto rebuild.

        Must not be awaited from the chat stream — keeps the user turn non-blocking.
        Auto rebuild runs even when the chat turn paused for HITL, because rebuild
        is detached and does not write to the closed stream.
        """
        rail = self._skill_evolution_rail
        if rail is None:
            return
        if not bool(getattr(rail, "has_pending_evolution", False)):
            # Evolution may have already finished between after_invoke and here;
            # still try to drain summary / rebuild once in the background.
            logger.info(
                "[JiuWenClawDeepAdapter] schedule evolution followup with no pending task yet: "
                "request_id=%s",
                request_id,
                extra={"user_visible": "progress"},
            )

        task = asyncio.create_task(
            self._background_evolution_followup(
                request_id=request_id,
                session_id=session_id,
            ),
            name=f"evolution_followup_{request_id}",
        )
        self._pending_evolution_followup_tasks.add(task)

        def _on_done(done: asyncio.Task[Any]) -> None:
            self._pending_evolution_followup_tasks.discard(done)
            if done.cancelled():
                logger.info(
                    "[JiuWenClawDeepAdapter] evolution followup cancelled: request_id=%s",
                    request_id,
                    extra={"user_visible": "progress"},
                )
                return
            exc = done.exception()
            if exc is not None:
                logger.warning(
                    "[JiuWenClawDeepAdapter] evolution followup failed: request_id=%s error=%s",
                    request_id,
                    exc,
                    exc_info=exc,
                    extra={"user_visible": "progress"},
                )
            else:
                logger.info(
                    "[JiuWenClawDeepAdapter] evolution followup completed: request_id=%s",
                    request_id,
                    extra={"user_visible": "progress"},
                )

        task.add_done_callback(_on_done)
        logger.info(
            "[JiuWenClawDeepAdapter] scheduled background evolution followup: "
            "request_id=%s pending_followups=%d",
            request_id,
            len(self._pending_evolution_followup_tasks),
            extra={"user_visible": "progress"},
        )

    async def _background_evolution_followup(
        self,
        *,
        request_id: str,
        session_id: str,
        timeout: float | None = 300.0,
    ) -> None:
        """Detached worker: join rail evolution, drain summary, run auto rebuild."""
        await self._await_pending_skill_evolution(request_id, timeout=timeout)
        self._drain_evolution_run_summary(request_id)

        # Late approval events (e.g. skill_creator_follow_up) after evolution —
        # cannot yield on the closed stream; log for ops visibility.
        rail = self._skill_evolution_rail
        if rail is not None:
            late_events = rail.drain_pending_approval_events()
            if late_events:
                logger.info(
                    "[JiuWenClawDeepAdapter] dropping %d late evolution approval event(s) "
                    "after stream closed: request_id=%s types=%s",
                    len(late_events),
                    request_id,
                    [getattr(evt, "type", None) for evt in late_events],
                    extra={"user_visible": "progress"},
                )

        await self._run_auto_rebuild_skills_detached(request_id=request_id)

    async def _await_pending_skill_evolution(
        self,
        request_id: str,
        *,
        timeout: float | None = 300.0,
    ) -> None:
        """Wait for SkillEvolutionRail background evolution (background followup only)."""
        rail = self._skill_evolution_rail
        if rail is None:
            return
        wait = getattr(rail, "wait_for_pending_evolution", None)
        if not callable(wait):
            logger.info(
                "[JiuWenClawDeepAdapter] skip evolution wait: request_id=%s "
                "reason=wait_for_pending_evolution_missing",
                request_id,
                extra={"user_visible": "progress"},
            )
            return
        # Brief poll: after_invoke may schedule the task a tick after chat.final.
        if not bool(getattr(rail, "has_pending_evolution", False)):
            await asyncio.sleep(0)
        if not bool(getattr(rail, "has_pending_evolution", False)):
            logger.info(
                "[JiuWenClawDeepAdapter] skip evolution wait: request_id=%s reason=no_pending_task",
                request_id,
                extra={"user_visible": "progress"},
            )
            return
        logger.info(
            "[JiuWenClawDeepAdapter] background followup waiting for skill evolution: "
            "request_id=%s timeout=%s",
            request_id,
            timeout,
            extra={"user_visible": "progress"},
        )
        try:
            await wait(timeout=timeout)
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] evolution wait failed: request_id=%s error=%s",
                request_id,
                exc,
                exc_info=True,
                extra={"user_visible": "progress"},
            )
            return
        still_pending = bool(getattr(rail, "has_pending_evolution", False))
        logger.info(
            "[JiuWenClawDeepAdapter] background followup evolution wait done: "
            "request_id=%s still_pending=%s",
            request_id,
            still_pending,
            extra={"user_visible": "progress"},
        )

    def _should_auto_merge_evolved_skill(self, skill_name: str) -> bool:
        """Return True when per-skill selfEvolution resolves to ``auto``.

        Unlisted skills use ``default_auto_save=False`` → ``suggest`` (no auto merge).
        """
        name = (skill_name or "").strip()
        if not name:
            return False
        action = resolve_skill_evolution_action(
            name,
            default_auto_save=False,
            skills_dirs=self._registered_skill_dirs_for_rail(),
        )
        return action == "auto"

    def _drain_evolution_run_summary(self, request_id: str) -> None:
        """Drain rail run summary and fill ``_pending_auto_rebuild_skills``.

        Intended for the detached evolution followup after rail evolution finishes.
        Only skills whose ``resolve_skill_evolution_action`` is ``auto`` are queued
        for version merge; ``off`` / ``suggest`` are skipped.
        """
        self._pending_auto_rebuild_skills = []
        if self._skill_evolution_rail is None:
            logger.info(
                "[JiuWenClawDeepAdapter] evolution summary skipped: request_id=%s reason=skill_evolution_rail_none",
                request_id,
                extra={"user_visible": "progress"},
            )
            return
        try:
            evolution_summary = self._skill_evolution_rail.take_run_summary()
            if isinstance(evolution_summary, dict):
                names: list[str] = []
                for item in self._iter_evolution_summary_items(evolution_summary.get("skills")):
                    if not isinstance(item, dict):
                        continue
                    skill_name = str(item.get("skill_name") or "").strip()
                    if (
                        skill_name
                        and skill_name not in names
                        and self._should_auto_merge_evolved_skill(skill_name)
                    ):
                        names.append(skill_name)
                self._pending_auto_rebuild_skills = names
        except Exception as exc:
            logger.warning(
                "[JiuWenClawDeepAdapter] evolution summary drain failed: request_id=%s error=%s",
                request_id,
                exc,
                exc_info=True,
                extra={"user_visible": "progress"},
            )
            return
        logger.info(
            "[JiuWenClawDeepAdapter] evolution summary drain: request_id=%s has_summary=%s "
            "auto_rebuild_skills=%s",
            request_id,
            bool(evolution_summary),
            self._pending_auto_rebuild_skills,
            extra={"user_visible": "progress"},
        )

    def _take_pending_auto_rebuild_skills(self) -> list[str]:
        """Return and clear skill names queued for auto rebuild after online evolution."""
        skills = list(self._pending_auto_rebuild_skills)
        self._pending_auto_rebuild_skills = []
        return skills

    async def _run_auto_rebuild_skills_detached(
        self,
        *,
        request_id: str,
    ) -> None:
        """Rebuild skills off the chat stream (no chunk yields)."""
        if self._skill_evolution_rail is None:
            self._pending_auto_rebuild_skills = []
            return

        skill_names = self._take_pending_auto_rebuild_skills()
        if not skill_names:
            return

        for skill_name in skill_names:
            if not self._should_auto_merge_evolved_skill(skill_name):
                logger.info(
                    "[JiuWenClawDeepAdapter] background auto rebuild skipped: "
                    "request_id=%s skill=%s reason=selfEvolution_not_auto",
                    request_id,
                    skill_name,
                    extra={"user_visible": "progress"},
                )
                continue
            logger.info(
                "[JiuWenClawDeepAdapter] background auto rebuild start: "
                "request_id=%s skill=%s",
                request_id,
                skill_name,
                extra={"user_visible": "progress"},
            )
            try:
                result = await self.generate_evolution_merge_version(
                    skill_name=skill_name,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] background auto rebuild failed: "
                    "request_id=%s skill=%s error=%s",
                    request_id,
                    skill_name,
                    exc,
                    exc_info=True,
                    extra={"user_visible": "progress"},
                )
                continue

            if result.get("ok"):
                logger.info(
                    "[JiuWenClawDeepAdapter] background auto rebuild ok: "
                    "request_id=%s skill=%s new_version=%s",
                    request_id,
                    skill_name,
                    result.get("new_version"),
                    extra={"user_visible": "progress"},
                )
            else:
                logger.info(
                    "[JiuWenClawDeepAdapter] background auto rebuild skipped/failed: "
                    "request_id=%s skill=%s reason=%s",
                    request_id,
                    skill_name,
                    result.get("error") or "unknown",
                    extra={"user_visible": "progress"},
                )

    async def _iter_auto_rebuild_followups(
        self,
        *,
        base_inputs: dict[str, Any],
        stream_request_id: str,
        channel_id: str,
        session_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        """Serially rebuild skills with stream progress (tests / legacy callers).

        Online chat path uses ``_run_auto_rebuild_skills_detached`` instead so the
        user turn is not blocked.
        """
        if self._skill_evolution_rail is None:
            self._pending_auto_rebuild_skills = []
            return

        skill_names = self._take_pending_auto_rebuild_skills()
        if not skill_names:
            return

        for skill_name in skill_names:
            if not self._should_auto_merge_evolved_skill(skill_name):
                logger.info(
                    "[JiuWenClawDeepAdapter] skip auto rebuild: skill=%s reason=selfEvolution_not_auto",
                    skill_name,
                    extra={"user_visible": "progress"},
                )
                continue
            yield AgentResponseChunk(
                request_id=stream_request_id,
                channel_id=channel_id,
                payload={
                    "event_type": "chat.delta",
                    "content": f"\n\n> 正在将 Skill `{skill_name}` 的演进经验融合为新版本…",
                },
                is_complete=False,
            )

            stream_ctx = self.MergeVersionStreamContext(
                base_inputs=base_inputs,
                stream_request_id=stream_request_id,
                channel_id=channel_id,
                session_id=session_id,
            )
            try:
                result = await self.generate_evolution_merge_version(
                    skill_name=skill_name,
                    stream_ctx=stream_ctx,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenClawDeepAdapter] auto rebuild failed: skill=%s error=%s",
                    skill_name,
                    exc,
                    exc_info=True,
                    extra={"user_visible": "progress"},
                )
                yield AgentResponseChunk(
                    request_id=stream_request_id,
                    channel_id=channel_id,
                    payload={
                        "event_type": "chat.delta",
                        "content": f"\n\n> Skill `{skill_name}` 自动版本重建失败：{exc}",
                    },
                    is_complete=False,
                )
                continue

            for follow_chunk in stream_ctx.chunks:
                yield follow_chunk

            if result.get("ok"):
                yield AgentResponseChunk(
                    request_id=stream_request_id,
                    channel_id=channel_id,
                    payload={
                        "event_type": "chat.delta",
                        "content": f"\n\n> Skill `{skill_name}` 已自动生成新版本。",
                    },
                    is_complete=False,
                )
            else:
                err = str(result.get("error") or "未自动重建版本")
                logger.info(
                    "[JiuWenClawDeepAdapter] auto rebuild skipped/failed: skill=%s reason=%s",
                    skill_name,
                    err,
                    extra={"user_visible": "progress"},
                )
                yield AgentResponseChunk(
                    request_id=stream_request_id,
                    channel_id=channel_id,
                    payload={
                        "event_type": "chat.delta",
                        "content": f"\n\n> Skill `{skill_name}` 未自动重建版本：{err}",
                    },
                    is_complete=False,
                )

    @staticmethod
    def _iter_evolution_summary_items(raw: Any) -> list[Any]:
        if isinstance(raw, (list, tuple)):
            return list(raw)
        return []

    @staticmethod
    def _is_ask_user_payload(payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("event_type") == "chat.ask_user_question"

    @staticmethod
    def _new_usage_accumulator() -> dict[str, float]:
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
    def _normalize_usage_metadata(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        for method_name in ("model_dump", "dict"):
            serializer = getattr(value, method_name, None)
            if not callable(serializer):
                continue
            try:
                payload = serializer()
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _extract_usage_metadata_from_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        raw_meta = payload.get("metadata", payload)
        if not isinstance(raw_meta, dict):
            return None
        usage_meta = raw_meta.get("usage_metadata", raw_meta)
        return usage_meta if isinstance(usage_meta, dict) else None

    @staticmethod
    def _accumulate_usage_metadata(
        usage_accumulator: dict[str, float],
        usage_meta: dict[str, Any],
    ) -> None:
        for token in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
            usage_accumulator[token] += usage_meta.get(token, 0) or 0
        for cost in ("input_cost", "output_cost", "total_cost"):
            usage_accumulator[cost] += usage_meta.get(cost, 0.0) or 0.0

    @staticmethod
    def _build_usage_summary(usage_accumulator: dict[str, float]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "input_tokens": usage_accumulator["input_tokens"],
            "output_tokens": usage_accumulator["output_tokens"],
            "total_tokens": usage_accumulator["total_tokens"],
        }
        if usage_accumulator["cache_tokens"] > 0:
            summary["cache_tokens"] = usage_accumulator["cache_tokens"]
        if usage_accumulator["input_cost"] > 0:
            summary["input_cost"] = round(usage_accumulator["input_cost"], 6)
        if usage_accumulator["output_cost"] > 0:
            summary["output_cost"] = round(usage_accumulator["output_cost"], 6)
        if usage_accumulator["total_cost"] > 0:
            summary["total_cost"] = round(usage_accumulator["total_cost"], 6)
        return summary

    def _should_pause_for_user_input(self, payload: Any) -> bool:
        return self._is_ask_user_payload(payload)

    def _detect_hitl_pause(
        self,
        payload: Any,
        *,
        hitl_pending_stream: bool,
    ) -> bool:
        if not self._should_pause_for_user_input(payload):
            return hitl_pending_stream
        return True

    def _parse_stream_chunk_with_source(self, chunk, *, _has_streamed_content: bool = False) -> dict | None:
        parsed = self._parse_stream_chunk(chunk, _has_streamed_content=_has_streamed_content)
        return propagate_stream_source_id(chunk, parsed)

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
                        trace_session_id = _LLM_TRACE_SESSION_ID.get() or ""
                        if self._outer_loop_has_remaining_tasks(trace_session_id):
                            return None
                        log_chat_final(
                            session_id=trace_session_id,
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
                    if isinstance(payload, dict):
                        return {
                            "event_type": "chat.ask_user_question",
                            **(payload if isinstance(payload, dict) else {}),
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

                if chunk_type == "__interaction__":
                    return convert_interactions_to_ask_user_question([payload])

                if chunk_type == "artifact.generated":
                    if isinstance(payload, dict):
                        return {
                            "event_type": "artifact.generated",
                            **payload,
                        }
                    return None

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

    def _cleanup_builtin_memory_artifacts(self) -> None:
        """卸载 builtin memory 在 ability_manager / Runner.resource_mgr / system_prompt_builder
        中残留的工具与 prompt section。

        当 ``MEMORY_ENGINE=none`` 或 ``modes.*.memory.enabled=false`` 时，仅 unregister
        ``MemoryRail`` 不足以让 memory_search / memory_index 在当前会话失效——这些工具是
        ``_init_builtin_memory_manager`` 在以前某次配置中通过 ``ability_manager.add`` 与
        ``Runner.resource_mgr.add_tool`` 注入的，必须显式移除。memory prompt section 同理。
        """
        instance = self._instance
        if instance is None:
            return

        ability_manager = getattr(instance, "ability_manager", None)
        if ability_manager is not None:
            for tool_name in ("memory_search", "memory_index"):
                try:
                    ability_manager.remove(tool_name)
                except Exception as exc:
                    logger.debug(
                        "[JiuWenClawDeepAdapter] ability_manager.remove(%s) skipped: %s",
                        tool_name, exc,
                    )

        prompt_builder = getattr(instance, "system_prompt_builder", None)
        if prompt_builder is not None:
            try:
                prompt_builder.remove_section("memory")
            except Exception as exc:
                logger.debug(
                    "[JiuWenClawDeepAdapter] system_prompt_builder.remove_section('memory') skipped: %s",
                    exc,
                )

        # 同步清理 Runner.resource_mgr 中对应的工具实例，避免后续 ability_manager.add
        # 再次基于 resource_mgr 的旧实例自动注册
        for qualified_id in list(self._qualified_memory_tool_ids):
            remove_tool_from_resource_mgr(qualified_id)
        self._qualified_memory_tool_ids = []

        try:
            for tool in get_decorated_tools():
                tool_card = getattr(tool, "card", None)
                if tool_card is None:
                    continue
                if getattr(tool_card, "name", "") in ("memory_search", "memory_index"):
                    try:
                        if Runner.resource_mgr.get_tool(tool_card.id) is not None:
                            Runner.resource_mgr.remove_tool(tool_card.id)
                    except Exception as exc:
                        logger.debug(
                            "[JiuWenClawDeepAdapter] Runner.resource_mgr.remove_tool(%s) skipped: %s",
                            tool_card.id, exc,
                        )
        except Exception as exc:
            logger.debug(
                "[JiuWenClawDeepAdapter] _cleanup_builtin_memory_artifacts: "
                "iterate decorated tools failed: %s", exc,
            )

    async def _handle_memory_rail_by_config(self, mode: str, config_base: dict[str, Any] | None = None):
        config = config_base if isinstance(config_base, dict) else get_config()
        memory_mode = get_memory_mode(config)

        if memory_mode == "wiki":
            if not (is_builtin_memory_allowed(config) and is_memory_enabled(mode, config)):
                # 引擎为 none 或当前模式下记忆关闭 —— 清理 wiki 模式可能注入的工具与 prompt section
                self._cleanup_builtin_memory_artifacts()
                logger.info(
                    "[JiuWenClawDeepAdapter] Wiki memory disabled for %s mode "
                    "(engine_allowed=%s, mode_enabled=%s)，已清理记忆工具与 prompt section",
                    mode,
                    is_builtin_memory_allowed(config),
                    is_memory_enabled(mode, config),
                )
                return
            await self._init_builtin_memory_manager(mode, config)
            logger.info("[JiuWenClawDeepAdapter] Wiki memory initialized for %s mode", mode)
            return

        if memory_mode == "local":
            builtin_on = is_builtin_memory_allowed(config) and is_memory_enabled(mode, config)
            if builtin_on:
                embed_fp = self._embed_config_fingerprint(config)
                if self._memory_rail is not None:
                    cur_memory_type = is_proactive_memory(mode, config)
                    embed_changed = (
                        self._embed_fingerprint is not None
                        and embed_fp != self._embed_fingerprint
                    )
                    if self._is_proactive_memory != cur_memory_type or embed_changed:
                        await self._instance.unregister_rail(self._memory_rail)
                        self._memory_rail = None
                    else:
                        return
                if self._memory_rail is None:
                    self._memory_rail = self._build_memory_rail(mode)
                if self._memory_rail is not None:
                    await self._instance.register_rail(self._memory_rail)
                    self._embed_fingerprint = embed_fp
                    logger.info(f"[JiuWenClawDeepAdapter] MemoryRail registered for {mode} mode")
                else:
                    logger.warning(
                        "[JiuWenClawDeepAdapter] MemoryRail build failed for %s mode, "
                        "check embed config (embed_api_key/embed_base_url/embed_model). "
                        "Falling back to FTS-only MemoryIndexManager",
                        mode,
                    )
                    await self._init_builtin_memory_manager(mode, config)
                    logger.info(
                        "[JiuWenClawDeepAdapter] MemoryIndexManager (FTS-only) initialized for %s mode",
                        mode,
                    )
            else:
                if not is_builtin_memory_allowed(config):
                    logger.warning(
                        "[JiuWenClawDeepAdapter] memory.mode=local but memory.engine=%r "
                        "(need 'builtin' or 'both'). "
                        "Set memory.engine=builtin in config.yaml or MEMORY_ENGINE=builtin env var.",
                        (config or {}).get("memory", {}).get("engine", "builtin")
                        if isinstance(config, dict) else "unknown",
                    )
                if not is_memory_enabled(mode, config):
                    logger.warning(
                        "[JiuWenClawDeepAdapter] memory.mode=local but modes.agent.%s.memory.enabled is false. "
                        "Set modes.agent.%s.memory.enabled=true in config.yaml.",
                        mode, mode,
                    )
                if self._memory_rail is not None:
                    await self._instance.unregister_rail(self._memory_rail)
                    self._memory_rail = None
                    logger.info(f"[JiuWenClawDeepAdapter] MemoryRail unregistered for {mode} mode")
                # 即使 MemoryRail 已不存在，FTS-only 降级路径或之前的 wiki 模式也可能已注入
                # memory_search / memory_index 与 memory prompt section —— 一并清理避免会话内残留
                self._cleanup_builtin_memory_artifacts()

    async def _init_builtin_memory_manager(self, mode: str,
                                           config: dict) -> None:
        """初始化 MemoryIndexManager/WikiManager + 注册工具 + 注入 memory prompt section。"""
        _, memory_agent_id = self._env_ns_ids()
        await init_memory_manager_async(
            workspace_dir=str(self._workspace_dir),
            agent_id=memory_agent_id,
            memory_mode=mode,
            embed_fingerprint=self._memory_cache_fingerprint,
        )

        if self._instance.system_prompt_builder is not None:
            resolved_language = self._instance.system_prompt_builder.language or "cn"
            is_proactive = is_proactive_memory(mode, config)
            memory_section = build_memory_section(
                language=resolved_language,
                read_only=False,
                is_proactive=is_proactive,
            )
            if memory_section is not None:
                self._instance.system_prompt_builder.remove_section("memory")
                self._instance.system_prompt_builder.add_section(memory_section)

        agent_card_id = self._resolve_agent_card_id()
        self._qualified_memory_tool_ids = []
        for tool in get_decorated_tools():
            tool_card = getattr(tool, "card", None)
            if tool_card is None:
                continue
            if getattr(tool_card, "name", "") in ("memory_index", "memory_search"):
                session_tool = clone_tool_for_session(tool, agent_card_id)
                register_qualified_tool(self._instance, session_tool, agent_card_id)
                self._qualified_memory_tool_ids.append(session_tool.card.id)

    def _build_external_memory_rail(self, config: dict[str, Any] | None = None):
        from jiuwenclaw.agentserver.memory.external_memory_builder import (
            build_external_memory_rail,
        )
        cfg = config if isinstance(config, dict) else self._latest_config_base
        if not isinstance(cfg, dict):
            cfg = get_config()
        sid, aid = self._tenant_disk_ids()
        return build_external_memory_rail(
            config=cfg,
            workspace_dir=self._workspace_dir,
            service_id=sid,
            agent_id=aid,
        )

    async def _unregister_external_memory_rail(self) -> None:
        """Unregister ExternalMemoryRail and clear adapter-held references."""
        if self._external_memory_rail is None or not self._external_memory_rail_registered:
            self._external_memory_rail = None
            self._external_memory_rail_registered = False
            return
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

    async def _handle_external_memory_rail_by_config(
        self,
        config: dict[str, Any] | None = None,
    ):
        """Register / unregister / rebuild ExternalMemoryRail based on config.

        External memory is mode-independent — configured once and active for
        both plan and fast modes. Not part of ``_get_current_agent_rails()``;
        uses ``register_rail`` / ``unregister_rail`` directly.

        When the external-memory fingerprint is unchanged, the existing rail
        is kept (prefetch cache and sync circuit breaker state preserved).
        When the fingerprint changes, the rail is unregistered and rebuilt.

        Prefers the reload/sync ``config`` (or ``_latest_config_base``) over a
        bare ``get_config()`` so per-tenant tip + config_base drive store paths.
        """
        cfg = config if isinstance(config, dict) else self._latest_config_base
        if not isinstance(cfg, dict):
            cfg = get_config()
        sid, aid = self._tenant_disk_ids()
        new_fp = external_memory_fingerprint(cfg, service_id=sid, agent_id=aid)

        if is_external_memory_enabled(cfg):
            if (
                self._external_memory_rail_registered
                and self._external_memory_fingerprint == new_fp
            ):
                return

            if self._external_memory_rail_registered:
                await self._unregister_external_memory_rail()

            self._external_memory_rail = self._build_external_memory_rail(cfg)
            if self._external_memory_rail is None:
                self._external_memory_fingerprint = None
                return
            try:
                await self._instance.register_rail(self._external_memory_rail)
                self._external_memory_rail_registered = True
                self._external_memory_fingerprint = new_fp
                logger.info(
                    "[JiuWenClawDeepAdapter] ExternalMemoryRail registered (fp=%s sid=%s aid=%s)",
                    new_fp[:8],
                    sid,
                    aid,
                )
            except Exception as exc:
                logger.error(
                    "[JiuWenClawDeepAdapter] ExternalMemoryRail register failed: %s", exc
                )
                self._external_memory_rail = None
                self._external_memory_fingerprint = None
        elif self._external_memory_rail_registered:
            await self._unregister_external_memory_rail()
            self._external_memory_fingerprint = None

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

        Harness 主对话流式请求在 JiuWenClaw.process_message_stream 入口额外维护
        _inflight_stream_count，并通过 working_checker 注入本方法的上层判断。

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

        # 检查 Team 模式下的流式任务和 monitors（懒加载，避免 Code 模式强依赖 team）
        try:
            from jiuwenclaw.agentserver.team import get_team_manager

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


def _has_uploaded_file(params: dict) -> bool:
    """检查 request.params.files 中是否有新上传的文件"""
    uploaded = extract_uploaded_files(params)
    if uploaded is None:
        return False
    return len(uploaded) > 0
