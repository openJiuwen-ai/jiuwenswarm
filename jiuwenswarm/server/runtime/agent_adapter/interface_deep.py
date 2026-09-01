# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuWenSwarm Deep Adapter - 基于 openjiuwen DeepAgent 的适配器实现.

此模块实现 AgentAdapter 协议，封装 Deep SDK 的所有专属逻辑。
公共编排逻辑（session 队列、Skills 路由、heartbeat 等）由 Facade 层处理。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, List, Optional, Tuple

if TYPE_CHECKING:
    from openjiuwen.harness.schema.config import SubAgentConfig
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

import yaml
from pydantic import ValidationError
from openjiuwen.core.context_engine.schema.config import (
    CompressionRecallConfig,
    ContextEngineConfig,
)
from openjiuwen.core.context_engine.token.tokenizer_registry import TokenizerRegistry
from openjiuwen.core.context_engine.token.tokenizer_spec import TokenizerSpec
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
from openjiuwen.core.single_agent import (
    AgentCard,
    ReActAgentConfig,
    create_agent_session,
)
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
from openjiuwen.harness.image_modality_probe import get_cached_image_support
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails import (
    ModelAnomalyDetectionRail,
    SkillUseRail,
    TaskPlanningRail,
    SecurityRail,
    SubagentRail,
    SysOperationRail,
    MemoryRail,
)
from openjiuwen.harness.rails import (
    EvolutionInterruptRail,
    SkillCreateRail,
    SkillEvolutionRail,
    configure_skill_evolution_runtime,
    unconfigure_skill_evolution,
)
from openjiuwen.harness.rails.personal_context import PersonalContextRail
from openjiuwen.harness.rails.evolution import EvolutionReviewRuntime
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import ContextAssembleRail
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
from openjiuwen.harness.subagents.browser_agent import build_browser_agent_config
from openjiuwen.harness.subagents.research_agent import build_research_agent_config
from openjiuwen.harness.subagent_runtime import (
    SUBAGENT_ACTIVITY_EVENT_TYPE,
    SUBAGENT_MESSAGE_EVENT_TYPE,
    SUBAGENT_UPDATED_EVENT_TYPE,
)
from openjiuwen.harness.tools import (
    WebFetchWebpageTool,
    WebFreeSearchTool,
    WebPaidSearchTool,
    create_audio_tools,
    create_vision_tools,
)
from openjiuwen.harness.goal.schema import GoalOperationError, GoalStatus
from openjiuwen.harness.schema.interaction import (
    InteractionEventType,
    InputDispatchMode,
    SendInputRequest,
)

GOAL_UPDATED_EVENT_TYPE = InteractionEventType.GOAL_UPDATED.value
_ERROR_EVENT = getattr(InteractionEventType, "EXECUTION_ERROR", None)
if _ERROR_EVENT is None:
    _ERROR_EVENT = getattr(InteractionEventType, "RUNTIME_ERROR")
ERROR_EVENT_TYPE = _ERROR_EVENT.value

# SDK chunk types that close one interaction round's output. ``answer`` carries
# every round result (normal answer, empty answer, goal attempt boundary); HITL
# interrupt frames replace it when a round stops for a human decision.
_ROUND_TERMINAL_CHUNK_TYPES = frozenset(
    {
        "answer",
        "chat.ask_user_question",
        "__interaction__",
    }
)

# Upper bound for the per-round streamed-text memo used to de-duplicate a
# demoted goal attempt final. Long enough for a full answer, bounded so a
# many-step round cannot grow it without limit.
_ROUND_VISIBLE_TEXT_MAX_CHARS = 256 * 1024


def _strip_whitespace(text: str) -> str:
    """Whitespace-free form used to compare two renderings of the same text.

    Collapsing to a single space (the frontend's ``collapseWs``) is not enough
    here: a round's answer re-wraps the streamed tokens, and CJK text has no
    space at the point where a newline was inserted.
    """
    return re.sub(r"\s+", "", text or "")

try:
    from openjiuwen.harness.tools import is_paid_search_enabled
except ImportError:  # Compatibility with older agent-core versions.
    try:
        from openjiuwen.harness.tools.web_tools import is_paid_search_enabled
    except ImportError:

        def is_paid_search_enabled() -> bool:
            api_key_envs = (
                "BOCHA_API_KEY",
                "PERPLEXITY_API_KEY",
                "SERPER_API_KEY",
                "JINA_API_KEY",
            )
            for key in api_key_envs:
                if str(os.environ.get(key, "") or "").strip():
                    return True
            return False


from openjiuwen.harness.schema.task import TodoStatus
from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode

from jiuwenswarm.server.runtime.tokenizer_service import (
    configured_tokenizer_profiles,
    resolve_tokenizer_cache_dir,
)
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
    STATUSLINE_SETUP_AGENT_TYPE,
    build_statusline_setup_agent_config,
)
from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
    init_a2x_client,
    register_blank_agent_if_teammate,
    resolve_a2x_config,
)
from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import CronRuntimeBridge
from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail
from jiuwenswarm.agents.harness.common.auto_harness import (
    AutoHarnessService,
    validate_harness_config,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    SKILL_EVOLUTION_APPROVAL_SCHEMA,
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.tools.todo_compat import (
    CompatibleTodoModifyTool,
    install_todo_modify_compat_patch,
)
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import build_agent_identity_prompt
from jiuwenswarm.agents.harness.common.rails import (
    BrowserTaskPromptRail,
    JiuSwarmStreamEventRail,
    MultimodalImageRail,
    ResponsePromptRail,
    RuntimePromptRail,
    StructuredAskUserRail,
    SymphonyOrchestrationRail,
)
from jiuwenswarm.agents.harness.common.rails.eternal_conversation import (
    EternalConversationRail,
)
from jiuwenswarm.agents.harness.common.rails.execution_guard import (
    CircuitBreakerRail,
    CircuitBreakerConfig,
)
from jiuwenswarm.common.context_window import parse_positive_int, resolve_context_window_tokens

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
    get_memory_mode,
    is_memory_enabled,
    is_proactive_memory,
)
from jiuwenswarm.agents.harness.common.memory.external_memory_config import is_builtin_memory_allowed
from jiuwenswarm.common.model_config_validation import (
    is_placeholder_api_base,
    is_placeholder_model_entry,
    model_client_config_view,
)
from jiuwenswarm.common.kv_cache_affinity_config import (
    build_kv_cache_affinity_config,
    model_provider,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
)
from jiuwenswarm.server.runtime.session.session_metadata import build_server_push_message
from jiuwenswarm.server.runtime.session.session_history import append_history_record, load_history_records
from jiuwenswarm.server.runtime import extension_package_manager as equipment

# Goal 用户历史：忙碌插队时先挂起，等上一轮→goal 边界（或流结束）再落盘，
# 时间戳与 live「答完再入列」对齐。按 session 暂存，跨同 session 的并发 stream 共享。
_pending_goal_objective_history: dict[str, dict[str, Any]] = {}
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
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
    evolution_outcome_from_event,
    evolution_meta_from_params,
    evolution_slash_command_name,
    evolution_slash_result,
    is_evolution_approval_event,
    is_evolution_outcome_event,
    push_evolution_event,
    push_evolution_progress,
    push_evolution_status,
    record_ids_from_pending_approval,
    reject_evolution_records,
    resolve_evolution_event_timeout_sec,
    team_evolution_terminal_progress,
    terminal_stage,
    visible_evolution_progress_from_events,
    visible_regular_evolution_start_progress,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_slash import (
    EvolutionSlashContext,
    handle_evolution_slash_command,
)
from jiuwenswarm.server.utils.stream_utils import (
    normalize_context_usage_payload,
    parse_ask_user_question_payload,
)
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_audio_model_config_from_yaml,
    apply_image_gen_model_config_from_yaml,
    apply_video_gen_model_config_from_yaml,
    apply_video_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    complete_multimodal_model_configured,
    multimodal_model_enabled,
)
from jiuwenswarm.agents.harness.common.tools.video_tools import (
    generate_video,
    video_understanding,
)
from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image

from jiuwenswarm.agents.harness.common.tools import (
    SendFileToolkit,
    SkillRetrievalToolkit,
    SkillToolkit,
    build_model_discovery_settings,
    is_skill_retrieval_enabled,
    skill_sources_from_manager,
    SymphonyToolkit,
)
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    bind_active_send_file_toolkit,
    clear_active_send_file_toolkit,
)
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
    SkillRetrievalPromptRail,
)
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.agents.harness.common.tools.pdf_tools import read_pdf
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
    get_config,
    get_model_names,
    get_default_models,
    get_evolution_auto_save_enabled,
    get_progressive_tool_enabled,
    get_skill_evolution_enabled,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
    is_subagent_runtime_enabled,
    get_mcp_server_config,
    get_config_yaml_mcp_servers,
    resolve_env_vars,
)
from jiuwenswarm.common.mcp_config import (
    build_mcp_credential_resolver,
    build_mcp_server_config,
    extract_enabled_mcp_server_entries,
    preflight_mcp_server_reachable,
)
from jiuwenswarm.server.runtime.mcp.call_timeout_patch import apply_mcp_call_timeout_patch
from jiuwenswarm.common.task_loop_config import (
    resolve_task_loop_completion_timeout,
)
from jiuwenswarm.common.reasoning_config import resolve_endpoint_profile_override
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    create_local_sysop_card,
    create_sandbox_sysop_card,
)
from jiuwenswarm.server.runtime.agent_adapter.user_turn import TEAM_USER_TURN_KEY, UserTurn
from jiuwenswarm.agents.harness.common.auto_harness.service import _HARNESS_PACKAGES_FILE
from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
from jiuwenswarm.gateway.cron import CronTargetChannel
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import (
    apply_free_search_runtime_defaults,
    get_agent_skills_dir,
    get_agent_workspace_dir,
    get_checkpoint_dir,
    get_default_project_session_workspace_dir,
    get_env_file,
    get_runtime_state_path,
)
from jiuwenswarm.dotenv_early import load_dotenv_runtime
from jiuwenswarm.common.mode_matrix import (
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    deprecate_mode,
    is_code_profile_mode,
    is_team_mode,
)

load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
apply_free_search_runtime_defaults()
TodoModifyTool = CompatibleTodoModifyTool
install_todo_modify_compat_patch()

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
_CRON_TOOL_USER_ID: ContextVar[str | None] = ContextVar(
    "cron_tool_user_id",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _RuntimeCronContextTokens:
    channel: Token[str]
    session: Token[str | None]
    metadata: Token[dict[str, Any] | None]
    mode: Token[str | None]
    bound: Token[bool]
    shell: Token[str | None]
    user_id: Token[str | None]


def get_runtime_tool_session_id() -> str | None:
    """Session id bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_SESSION_ID.get()

logger = logging.getLogger(__name__)


def _diag_auth_headers(cfg: Any) -> str:
    """Diagnostic snapshot of cfg.auth_headers (value prefix + length only).

    Prints each header value's first 15 chars + total length, so logs reveal
    whether a token was injected (e.g. ``Authorization[len=42]: Bearer ghp_abc``)
    or left empty/placeholder (``Authorization[len=8]: Bearer ``). The full
    token is never logged.
    """
    headers = getattr(cfg, "auth_headers", None)
    if not isinstance(headers, dict) or not headers:
        return f"{{no auth_headers; client_type={getattr(cfg,'client_type','?')}}}"
    parts: list[str] = []
    for k, v in headers.items():
        vs = str(v)
        parts.append(f"{k}[len={len(vs)}]: {vs[:15]}")
    return "{" + ", ".join(parts) + "}"

_PERSISTENT_CHECKPOINTER_LOCK: asyncio.Lock | None = None
_PERSISTENT_CHECKPOINTER_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_PERSISTENT_CHECKPOINTER_LOCK_INIT = threading.Lock()
_PERSISTENT_CHECKPOINTER_READY = False


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

_ACP_BLOCKED_DEFAULT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "code",
    }
)
_SKILL_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "skill_index",
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


def _assemble_run_answer(deltas: list[str], final: str) -> str:
    """Join a streaming run's assistant text into its final answer.

    Used for the OTel trace-level output. The two sources overlap in one case
    and not in the other, so neither alone is right:

    * An ``answer`` chunk re-sends the **whole** reply as ``chat.final`` after
      the deltas that already carried it — concatenating would double it.
    * A round cut short (pause / clear) never reaches that chunk; the pending
      buffer is flushed as ``chat.final`` carrying only the **tail** — dropping
      the deltas would lose the head.

    Containment tells the two apart: a final already present in the joined
    deltas is the duplicate form.

    Args:
        deltas: ``chat.delta`` contents, in emission order.
        final: Content of the last ``chat.final``; empty when none was emitted.

    Returns:
        The final answer text, empty when the run produced no assistant output.
    """
    joined = "".join(deltas)
    if final and final not in joined:
        return joined + final
    return joined or final


def init_permission_engine(*_args: Any, **_kwargs: Any) -> None:
    """Legacy shim for tests/older call sites.

    The project now relies on openjiuwen's PermissionInterruptRail and does not
    require a standalone permission engine initialization step.
    """
    return None


def _mcc_looks_usable(mcc: dict) -> bool:
    """检查 model_client_config 是否包含有效的 API 凭据。

    新声明下凭据判定以 auth_mode 为准：
    - auth_mode=openai_account_oauth 或 api_mode=responses：OAuth 路径，不要求 api_key。
    - auth_mode=none 或 custom_headers(无 key)：按该认证模式判断(不要求 api_key)。
    - 否则(默认 api_key 模式，含 OpenAI / Anthropic)：要求 api_base + api_key。
    兼容旧配置：client_provider=OpenAIAccount 别名也视为 OAuth。
    """
    api_base = str(mcc.get("api_base", "") or "").strip()
    if not api_base or is_placeholder_api_base(api_base):
        return False

    auth_mode = str(mcc.get("auth_mode") or "").strip().lower()
    api_mode = str(mcc.get("api_mode") or "").strip().lower()
    # OAuth 路径：新式 auth_mode=openai_account_oauth 或旧式 provider 名 OpenAIAccount。
    provider = mcc.get("client_provider", "")
    provider = getattr(provider, "value", provider)
    if (
        auth_mode == "openai_account_oauth"
        or api_mode == "responses"
        or is_openai_account_provider(str(provider or ""))
    ):
        return True

    # none / custom_headers 无 key 的认证模式：只要 api_base 在即可。
    if auth_mode in ("none", "custom_headers"):
        # custom_headers 通常仍需自定义头，但凭据层面不算 api_key 必填。
        return True

    api_key = str(mcc.get("api_key", "") or "").strip()
    return bool(api_key)


def build_model_from_entry(mcc: dict, mco: dict) -> Model:
    """根据单个模型条目的 model_client_config / model_config_obj 构建 Model 实例。

    模块级公开函数：除本适配器外，模型缓存构建（``agent_ws_server`` /
    ``auto_harness.scheduler``）与图像模态探测预热（``image_modality_warmup``）
    都要按同一规则从 ``models.defaults`` 条目造 Model，共享这一份实现。

    Args:
        mcc: 条目的 ``model_client_config`` 段，``model_name`` 单独取出。
        mco: 条目的 ``model_config_obj`` 段。

    Returns:
        按该条目配置构建的 Model 实例。
    """
    name = mcc.get("model_name", "")
    mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
    if not mcc_fields.get("client_provider"):
        mcc_fields["client_provider"] = "OpenAI"
    # 已知自建网关（如 DashScope 风格端点）按 api_base host 补全 endpoint_profile，
    # 覆盖尚未重新保存过的存量条目；显式配置的方言优先。core 依赖该字段
    # 选择线格式（enable_thinking + thinking_budget），档位仍按模型名。
    if not mcc_fields.get("endpoint_profile"):
        _inferred_profile = resolve_endpoint_profile_override(
            mcc_fields.get("api_base") or mcc_fields.get("base_url")
        )
        if _inferred_profile:
            mcc_fields["endpoint_profile"] = _inferred_profile

    # ``context_window``（模型支持的上下文总长度）可配在任意模型条目的 mco 里
    # （defaults / agentos / video / audio / vision / image_gen 均可），经
    # ``build_reasoning_model_request_kwargs`` 摊开进入 kwargs，进 core 的
    # ``ModelRequestConfig`` 供 core 人员取值。
    # 是否在出口 pop 取决于 core 是否已把 context_window 加为 ModelRequestConfig
    # 正式字段（见 ``reasoning_injector.core_has_context_window_field``，自动适配）：
    # - core 未加字段（过渡期）：context_window 进 extra 会被
    #   ``base_model_client._build_request_params`` 经 ``model_dump`` 透传给厂商
    #   SDK 报 unexpected keyword argument -> reasoning_injector 公共出口 pop 防发厂商。
    # - core 已加字段：context_window 作正式字段，core 自行 exclude 不发厂商、
    #   ``self.model_config.context_window`` 可读 -> 不 pop，留给 core。
    # 出口 pop 不再守 _source=="agentos"：所有条目一视同仁，defaults 配了
    # context_window 同样过渡期 pop 防发厂商。agentos 条目的 mco 含内部标记
    # ``_source == "agentos"``（由 ``get_default_models`` 注入），仅用于前端
    # is_agentos 置灰只读展示，不再参与 context_window 出口判断。
    # 不再挂 ``_agentos_ctx_window`` 普通属性：旧机制是把 context_window 喂给
    # ``ContextEngineConfig.context_window_tokens``（压缩阈值）的桥接，已拆除
    # （见 ``_deep_agent_context_engine_config``）。
    request_kwargs = build_reasoning_model_request_kwargs(
        model_client_config=mcc_fields,
        model_config_obj=mco,
        model_name=name,
    )
    m_config = ModelRequestConfig(**request_kwargs)
    model = Model(model_client_config=ModelClientConfig(**mcc_fields), model_config=m_config)
    return model


def parse_int(value: Any, default: int) -> int:
    """Parse integer-like values safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any, default: bool = False) -> bool:
    """Parse persisted YAML/API boolean values without truthiness surprises."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


@dataclass(frozen=True)
class _ContextEngineModelState:
    """Model-specific inputs used while building a context configuration."""

    full_config: dict[str, Any] | None = None
    model_name: str | None = None
    model: Any = None
    config_base: dict[str, Any] | None = None


def _build_deep_agent_context_engine_config(
    react_cfg: dict[str, Any] | None,
    model_state: _ContextEngineModelState | None = None,
) -> ContextEngineConfig:
    """Build the agent-core Context Engine configuration.

    仅承接 ContextEngine 自身配置；KV cache affinity 由独立
    ``react.kv_cache_affinity_config`` 管理。

    context_window（模型支持的上下文总长度）由 ``_build_model_from_entry`` 放进
    core 的 ``ModelRequestConfig``，再由 ReActAgent 注入当前 ContextEngine 的模型级
    元数据。本函数只承接全局覆盖和显式模型映射；最终优先级为全局值 > 当前 AgentOS
    模型值 > 显式映射 > core 按模型名解析 / 兜底。
    """
    model_state = model_state or _ContextEngineModelState()
    react_cfg = react_cfg or {}
    cec = react_cfg.get("context_engine_config")
    cec = cec if isinstance(cec, dict) else {}
    cw_tokens = parse_positive_int(cec.get("context_window_tokens"))
    model_context_windows = cec.get("model_context_window_tokens")
    if isinstance(model_context_windows, dict):
        valid_model_context_windows = {}
        for configured_model_name, raw_window in model_context_windows.items():
            if not isinstance(configured_model_name, str):
                continue
            parsed_window = parse_positive_int(raw_window)
            if parsed_window is not None:
                valid_model_context_windows[configured_model_name] = parsed_window
        model_context_windows = valid_model_context_windows
        if not model_context_windows:
            model_context_windows = None
    else:
        model_context_windows = None
    recall = cec.get("compression_recall_config")
    recall = recall if isinstance(recall, dict) else {}
    tokenizer_enabled = _parse_bool(cec.get("enable_tiktoken_counter"), False)
    raw_registry = cec.get("tokenizer_registry")
    tokenizer_registry: list[TokenizerSpec] = []
    if isinstance(raw_registry, list):
        for raw_spec in raw_registry:
            try:
                tokenizer_registry.append(
                    raw_spec
                    if isinstance(raw_spec, TokenizerSpec)
                    else TokenizerSpec.model_validate(raw_spec)
                )
            except (AttributeError, TypeError, ValidationError) as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] invalid tokenizer registry entry: %s",
                    exc,
                )

    # Model profiles may carry their tokenizer declaration next to
    # model_client_config. Copy those declarations into the core registry so
    # the context engine can select the same artifact that the AgentServer
    # prewarmed, including for non-OpenAI providers.
    effective_config = (
        model_state.config_base
        if isinstance(model_state.config_base, dict)
        else model_state.full_config
    )
    if isinstance(effective_config, dict):
        try:
            known_keys: set[tuple[str, str]] = set()
            for registered_spec in tokenizer_registry:
                known_keys.add(
                    (
                        registered_spec.provider.strip().lower(),
                        registered_spec.model.strip().lower(),
                    )
                )
            for profile in configured_tokenizer_profiles(effective_config):
                if not profile.spec:
                    continue
                try:
                    spec = TokenizerSpec.model_validate(profile.spec)
                except (AttributeError, TypeError, ValidationError) as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] invalid model tokenizer spec for %s: %s",
                        profile.model,
                        exc,
                    )
                    continue
                key = (spec.provider.strip().lower(), spec.model.strip().lower())
                if key not in known_keys:
                    tokenizer_registry.append(spec)
                    known_keys.add(key)
        except (AttributeError, ImportError, KeyError, TypeError, ValueError) as exc:
            logger.debug("[JiuWenSwarmDeepAdapter] tokenizer registry enrichment skipped: %s", exc)

    raw_spec = cec.get("tokenizer_spec")
    tokenizer_spec = None
    if raw_spec is not None:
        try:
            if isinstance(raw_spec, str):
                raw_spec = {"id": raw_spec}
            tokenizer_spec = (
                raw_spec
                if isinstance(raw_spec, TokenizerSpec)
                else TokenizerSpec.model_validate(raw_spec)
            )
        except (AttributeError, TypeError, ValidationError) as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] invalid tokenizer_spec: %s", exc)

    tokenizer_cache_dir = None
    try:
        cache_config = (
            effective_config
            if isinstance(effective_config, dict)
            else {"react": react_cfg}
        )
        tokenizer_cache_dir = str(resolve_tokenizer_cache_dir(cache_config))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        tokenizer_cache_dir = cec.get("tokenizer_cache_dir") or None

    defaults = ReActAgentConfig().context_engine_config
    supported = {
        key: value
        for key, value in cec.items()
        if key in ContextEngineConfig.model_fields
    }
    # 显式设置的上下文窗口上限；非法值回退 None（由 agent-core 按模型解析）。
    supported["context_window_tokens"] = cw_tokens
    if "model_context_window_tokens" in ContextEngineConfig.model_fields:
        supported["model_context_window_tokens"] = model_context_windows
    # 压缩召回：压缩时归档原始消息，供模型按需召回。
    if "compression_recall_config" in ContextEngineConfig.model_fields:
        supported["compression_recall_config"] = CompressionRecallConfig(
            enabled=_parse_bool(recall.get("enabled"), False),
            chunk_size_tokens=parse_int(recall.get("chunk_size_tokens"), 3000),
            chunk_overlap_tokens=parse_int(recall.get("chunk_overlap_tokens"), 300),
        )
    # Preserve tolerant handling for malformed values from YAML/API payloads.
    supported["enable_reload"] = _parse_bool(
        cec.get("enable_reload"), bool(getattr(defaults, "enable_reload", False))
    )
    supported["enable_tiktoken_counter"] = tokenizer_enabled
    supported["tokenizer_spec"] = tokenizer_spec
    supported["tokenizer_registry"] = tokenizer_registry
    supported["tokenizer_cache_dir"] = tokenizer_cache_dir
    # Context creation is deliberately read-only. TokenizerService owns the
    # only download-capable warm-up path before this config is consumed.
    supported["enable_tokenizer_download"] = False
    supported["tokenizer_offline"] = True
    supported["enable_openrouter_model_context_window_tokens"] = _parse_bool(
        cec.get("enable_openrouter_model_context_window_tokens"),
        bool(getattr(defaults, "enable_openrouter_model_context_window_tokens", False)),
    )
    supported["enable_context_debug"] = _parse_bool(
        cec.get("enable_context_debug"), bool(getattr(defaults, "enable_context_debug", False))
    )
    if cec.get("context_debug_dir") not in (None, ""):
        supported["context_debug_dir"] = cec.get("context_debug_dir")

    # Prefer the selected Model object so duplicate AgentOS entries with the
    # same model name cannot borrow another entry's max_tokens value.
    selected_model_name = (
        model_state.model_name.strip()
        if isinstance(model_state.model_name, str)
        else ""
    )
    if not selected_model_name and model_state.model is not None:
        for candidate in (
            getattr(getattr(model_state.model, "model_config", None), "model_name", None),
            getattr(
                getattr(model_state.model, "model_client_config", None),
                "model_name",
                None,
            ),
        ):
            if isinstance(candidate, str) and candidate.strip():
                selected_model_name = candidate.strip()
                break
    if selected_model_name:
        # Context usage/tokenizer selection must follow the model that will
        # actually receive this request.  The old path left these fields at
        # their startup values, so switching models changed the provider call
        # but not the cached context's usage identity.
        supported["model_name"] = selected_model_name
    selected_model_provider = (
        model_provider(model_state.model)
        if model_state.model is not None
        else ""
    )
    selected_tokenizer_provider = selected_model_provider or str(
        cec.get("model_provider") or ""
    ).strip()
    if selected_model_provider:
        supported["model_provider"] = selected_model_provider

    # ``tokenizer_spec`` is an exact call-site override.  When it came from
    # the startup model and a request switches to another model, keeping it
    # would make the new model silently use the old tokenizer.  Move the old
    # spec into the registry so the selected model can resolve its own exact
    # entry (or a safe family entry) instead.
    if tokenizer_spec is not None and selected_model_name:
        explicit_match = TokenizerRegistry([tokenizer_spec]).resolve_match(
            selected_tokenizer_provider,
            selected_model_name,
        )
        if explicit_match is None:
            tokenizer_registry.append(tokenizer_spec)
            tokenizer_spec = None
            supported["tokenizer_spec"] = None
            supported["tokenizer_registry"] = tokenizer_registry
    agentos_cw: int | None = None
    if model_state.model is not None:
        agentos_cw = parse_int(
            getattr(model_state.model, "_agentos_ctx_window", None),
            None,
        )
    elif isinstance(effective_config, dict) and selected_model_name:
        agentos_raw = (effective_config.get("models") or {}).get("agentos")
        agentos_list = agentos_raw if isinstance(agentos_raw, list) else []
        for block in agentos_list:
            if not isinstance(block, dict):
                continue
            model_client_config = block.get("model_client_config") or {}
            if (
                isinstance(model_client_config, dict)
                and model_client_config.get("model_name") == selected_model_name
            ):
                model_config = block.get("model_config_obj") or {}
                agentos_cw = parse_int(model_config.get("max_tokens"), None)
                break
    if agentos_cw is not None:
        supported["context_window_tokens"] = agentos_cw

    return ContextEngineConfig.model_validate({**defaults.model_dump(), **supported})


def _deep_agent_context_engine_config(
    react_cfg: dict[str, Any] | None,
) -> ContextEngineConfig:
    """Build a context configuration from the static ReAct settings.

    Keep this small compatibility-facing helper limited to the original
    ``react_cfg`` argument.  Model-specific state is supplied through
    :func:`_deep_agent_context_engine_config_for_model` at the model assembly
    and model-switch call sites.
    """
    return _build_deep_agent_context_engine_config(react_cfg)


def _deep_agent_context_engine_config_for_model(
    react_cfg: dict[str, Any] | None,
    *,
    model_state: _ContextEngineModelState | None = None,
) -> ContextEngineConfig:
    """Build context configuration with the currently selected model state."""
    return _build_deep_agent_context_engine_config(react_cfg, model_state)


def _deep_agent_kv_cache_affinity_config(
    react_cfg: dict[str, Any] | None,
    model: Model | None = None,
) -> KVCacheAffinityConfig:
    """Build the ReActAgent KV cache affinity config from jiuwenswarm config."""
    # 取 model.model_client_config 供新式 extensions.kv_cache.mode 判别；
    # model_provider 兼容旧 AscendAffinity 别名。
    mcc = getattr(model, "model_client_config", None)
    return build_kv_cache_affinity_config(
        react_cfg,
        provider=model_provider(model),
        model_client_config=mcc,
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


async def ensure_persistent_checkpointer() -> None:
    """Ensure the process-wide default checkpointer uses sqlite persistence."""
    global _PERSISTENT_CHECKPOINTER_READY

    if _PERSISTENT_CHECKPOINTER_READY:
        return

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
            return

        try:
            PersistenceCheckpointerProvider()
            checkpoint_path = get_checkpoint_dir()
            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(
                    type="persistence",
                    conf={"db_type": "sqlite", "db_path": f"{checkpoint_path}/checkpoint"},
                ),
            )
            CheckpointerFactory.set_default_checkpointer(checkpointer)
            _PERSISTENT_CHECKPOINTER_READY = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] persistent checkpointer ready: %s",
                checkpoint_path / "checkpoint",
            )
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] fail to setup checkpoint due to: %s",
                exc,
            )
            raise RuntimeError("persistent checkpointer initialization failed") from exc
    finally:
        if acquired:
            lock.release()


_MODE_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "agent": {"cn": "智能体模式", "en": "Agent Mode"},
    # Web 的 work 单 agent 计划模式（mode=agent.plan + work_mode=work）。
    "agent.plan": {"cn": "计划模式", "en": "Plan Mode"},
    # 历史 token 归一到合并后的 agent 显示名（兼容旧会话 / 旧请求）。
    "agent.fast": {"cn": "智能体模式", "en": "Agent Mode"},
    "team": {"cn": "集群模式", "en": "Cluster Mode"},
    "team.plan": {"cn": "集群计划模式", "en": "Cluster Plan Mode"},
    "team.plan.normal": {"cn": "集群计划模式", "en": "Cluster Plan Mode"},
    "team.plan.code": {"cn": "代码集群计划模式", "en": "Code Team Plan Mode"},
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
        self._fallback_user_id: str | None = None

    def remember_current_binding(self) -> None:
        self._fallback_channel_id = _CRON_TOOL_CHANNEL_ID.get()
        self._fallback_session_id = _CRON_TOOL_SESSION_ID.get()
        metadata = _CRON_TOOL_METADATA.get()
        self._fallback_metadata = dict(metadata) if isinstance(metadata, dict) else None
        self._fallback_mode = _CRON_TOOL_MODE.get()
        self._fallback_user_id = _CRON_TOOL_USER_ID.get()

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
    def user_id(self) -> str | None:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_USER_ID.get()
        return self._fallback_user_id

    @property
    def tool_scope(self) -> str:
        return self._tool_scope


class JiuWenSwarmDeepAdapter:
    SESSION_ADAPTER_IDLE_TTL_SEC = 2 * 60 * 60
    SESSION_ADAPTER_EVICT_BATCH_SIZE = 3
    SESSION_ADAPTER_RELOAD_RETRY_INTERVAL_SEC = 30.0
    _subagent_runtime_supported: bool = True
    _subagent_progress_batches: dict[str, list[str]] = {}
    _subagent_progress_batches_lock = threading.Lock()
    _RUNTIME_STATE_WRITE_LIMIT = threading.BoundedSemaphore(2)
    # Goal stream bookkeeping (see __init__ for what each one tracks). Declared
    # on the class as well so the goal helpers stay safe to call on an instance
    # that has not run through __init__.
    _stream_content_run_kind: str | None = None
    _stream_round_kind_latch: str | None = None
    _stream_round_output_ended: bool = False
    _stream_round_visible_text: str = ""

    """Deep SDK 适配器，实现 AgentAdapter 协议.

    封装所有 Deep SDK 专属逻辑：
    - DeepAgent 实例生命周期管理
    - Deep runtime tools 注册
    - Deep stream event 解析
    - Deep evolution 绑定
    - Deep interrupt / user_answer 处理
    """

    def __init__(self) -> None:
        # Apply the MCP per-call timeout patch once per process: wraps
        # StreamableHttpClient/SseClient.call_tool & list_tools in
        # asyncio.wait_for and honors config ``timeout_s`` (--timeout_s), so a
        # killed remote MCP server fails fast instead of hanging on the MCP
        # SDK's 300s SSE read timeout. Idempotent (module-level _PATCHED guard).
        apply_mcp_call_timeout_patch()
        self._instance: DeepAgent | None = None
        self._project_dir: str | None = None
        self._workspace_dir: str = str(get_agent_workspace_dir())
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
        self._video_gen_tool_registered: bool = False
        self._model: Model | None = None
        self._model_client_config: ModelClientConfig | None = None
        self._model_request_config: ModelRequestConfig | None = None
        self._last_resolved_model: Model | None = None
        self._active_request_model: Model | None = None
        self._selected_model_context_window_tokens: int | None = None
        self._config_base_cache: dict[str, Any] | None = None
        self._config_cache: dict[str, Any] = {}
        self._filesystem_rail: SysOperationRail | None = None
        self._skill_rail: SkillUseRail | None = None
        self._stream_event_rail: JiuSwarmStreamEventRail | None = None
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
        # Run kind of the round that produced the chunks being consumed right
        # now, sampled once per round instead of per chunk (see
        # ``_track_round_output_boundary``).
        self._stream_round_kind_latch: str | None = None
        self._stream_round_output_ended: bool = False
        # Visible assistant text already streamed for the round being consumed.
        # Lets a demoted goal attempt final drop text the bubble already shows
        # (see ``_goal_intermediate_final_repeats_streamed_text``).
        self._stream_round_visible_text: str = ""
        # In-flight asyncio tasks per session (stream/non-stream agent runs).
        self._session_agent_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._task_planning_rail: TaskPlanningRail | None = None
        self._context_assemble_rail: ContextAssembleRail | None = None
        self._context_assemble_mode: str | None = None
        self._context_processor_rail: ContextProcessorRail | None = None
        self._eternal_conversation_rail: EternalConversationRail | None = None
        self._eternal_conversation_enabled: bool = False
        self._runtime_prompt_rail: RuntimePromptRail | None = None
        self._response_prompt_rail: ResponsePromptRail | None = None
        self._personal_context_rail: PersonalContextRail | None = None
        self._personal_context_rail_lock = asyncio.Lock()
        self._security_rail: SecurityRail | None = None
        self._memory_rail: MemoryRail | None = None
        self._external_memory_rail: Any = None
        self._external_memory_rail_registered: bool = False
        # 记忆 embedding 配置指纹：用于检测 embed 段变化并据此重建 MemoryRail。
        # 重建 rail 才能让 _embedding_config 刷新；否则换 endpoint 时 rail 复用旧配置。
        self._memory_embedding_fingerprint: str = ""
        # 最近一次请求使用的 mode：reload 时无 runtime_config 上下文，靠它主动刷新 memory rail。
        self._last_mode: str | None = None
        # PersonalContext is controlled by the Host runtime switch.  Keep the
        # last control-plane snapshot here so the disabled path never mounts a
        # rail merely because an Agent mode request arrived.
        self._personal_context_runtime_enabled: bool = False
        # 延时重索引任务（debounce）：连续改多次 embedding 只在最后一次后跑一次。
        self._memory_reindex_task: asyncio.Task | None = None
        # 后台预热：web 重启后对 connected MCP 建进程级连接缓存，首轮对话不重 spawn。
        self._mcp_prewarm_task: asyncio.Task | None = None
        self._model_anomaly_detection_rail: ModelAnomalyDetectionRail | None = None
        self._heartbeat_rail: HeartbeatRail | None = None
        self._heartbeat_service: Any | None = None
        self._skill_evolution_rail: SkillEvolutionRail | None = None
        self._evolution_interrupt_rail: EvolutionInterruptRail | None = None
        self._skill_create_rail: SkillCreateRail | None = None
        self._subagent_rail: SubagentRail | None = None
        self._ask_user_rail: StructuredAskUserRail | None = None
        self._permission_rail: Any = None
        self._avatar_rail: Any = None
        self._memory_forbidden_rail: Any = None
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
        self._video_gen_model_config: bool = False
        self._vision_tools: list[Any] = []
        self._audio_tools: list[Any] = []
        self._instance_overrides: dict[str, Any] = {}
        self._is_session_scoped_adapter: bool = False
        self._parent_session_id: str | None = None
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
        self._pending_session_reload_scopes: set[str] | None = None
        self._session_instance_config: dict[str, Any] | None = None
        self._session_instance_mode: str = "agent"
        self._session_instance_sub_mode: str | None = None
        self._xiaoyi_phone_tools_registered: bool = False
        self._paid_search_registered: bool = False
        self._paid_search_tool: WebPaidSearchTool | None = None
        self._symphony_tools: list[Any] = []
        self._symphony_tools_registered: bool = False
        self._symphony_orchestration_rail = None
        self._skill_retrieval_tools_registered: bool = False
        self._skill_retrieval_tools: list[Any] = []
        self._skill_retrieval_toolkit: SkillRetrievalToolkit | None = None
        self._skill_retrieval_environment: Any | None = None
        self._skill_retrieval_prompt_rail: SkillRetrievalPromptRail | None = None
        # Frozen when this adapter/session is created. Config reloads are
        # persisted for future sessions without changing the current tool or
        # prompt prefix halfway through a conversation.
        self._skill_retrieval_session_enabled: bool | None = None
        self._initial_skill_retrieval_model_name: str = ""
        self._skill_retrieval_context_model_name: str = ""
        self._skill_retrieval_context_window_tokens: int | None = None
        self._skill_retrieval_settings: Any | None = None
        self._restored_skill_retrieval_profile: dict[str, Any] | None = None
        self._skill_manager: SkillManager | None = None
        self._a2x_client: Any | None = None
        self._a2x_config: dict[str, Any] = {}
        self._a2x_blank_service_id: str = ""
        self._a2x_blank_dataset: str = ""
        self._cron_runtime = CronRuntimeBridge()
        self._runtime_cron_tool_context = _RuntimeCronToolContext(
            tool_scope=f"runtime_{id(self):x}",
        )
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
        # 全局列表下标 → cache_key。通道侧（Web）用全局 origin_index 拼请求 key，
        # 与后端 per-name 的 cache_key 序号语义分叉；此映射在 _resolve_model_by_name
        # 回退分支把通道传入的全局序号换算成真实 cache_key，避免同名条目
        # （如 agentos 第 2+ 个同名模型）被静默解析到 defaults 的同名首条目。
        self._global_index_to_cache_key: dict[int, str] = {}
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
        # MCP names this session loaded via chat.send's ``mcp`` field
        # (reconcile_session_mcp). init-registered config.yaml MCPs are NOT in
        # this set — reconcile only adds/removes its own, never touching init's.
        self._session_selected_mcp: set[str] = set()
        # Names supplied by the first chat.send while this session child is
        # still being assembled. They affect Skill scan roots only, allowing
        # the retrieval snapshot to see bundled Skills before MCP registration;
        # they deliberately do not mark an MCP as registered/selected.
        self._pending_skill_scan_mcp_names: set[str] = set()
        # MCP Skill names that participated in the immutable retrieval snapshot
        # when this session child was assembled. A warm-pool child starts with
        # an empty snapshot because session.create does not yet know chat.send's
        # MCP selection.
        self._initial_skill_scan_mcp_names: frozenset[str] = frozenset()
        # Claimed under the parent adapter's per-session lock by the first MCP
        # reconcile. Until then the child is speculative/unused and may be
        # replaced when the first chat reveals different frozen MCP inputs.
        self._session_mcp_reconcile_started: bool = False
        self._auto_harness_service: Optional[AutoHarnessService] = None
        self._dreaming_started = False
        self._dreaming_mode: str = "agent"
        self._send_file_toolkit: SendFileToolkit | None = None
        self._runtime_state_write_task: asyncio.Task[None] | None = None
        self._channel_id: str | None = None
        # (name, load_record, manifest.version)
        self._loaded_agent_template: tuple[str, Any, str] | None = None
        # name → (load_record, manifest.version)
        self._loaded_plugins: dict[str, tuple[Any, str]] = {}

    def set_heartbeat_service(self, service: Any | None) -> None:
        """Bind the one process-level Heartbeat runtime used by this rail."""
        self._heartbeat_service = service
        for adapter in self._session_adapters.values():
            adapter.set_heartbeat_service(service)

    def _build_heartbeat_rail(self) -> HeartbeatRail | None:
        if self._heartbeat_service is None:
            return None
        return HeartbeatRail(
            service=self._heartbeat_service,
            context=self._runtime_cron_tool_context,
        )

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

    @staticmethod
    def _session_adapter_key(session_id: str | None) -> str:
        sid = str(session_id or "").strip()
        return sid or "default"

    @staticmethod
    def _skill_retrieval_profile_path(session_id: str | None) -> Path:
        sid = JiuWenSwarmDeepAdapter._session_adapter_key(session_id)
        runtime_path = get_runtime_state_path(sid)
        digest = hashlib.sha256(
            sid.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        return runtime_path.with_name(
            f"{runtime_path.stem}.{digest}.skill-retrieval.json"
        )

    @staticmethod
    def _skill_retrieval_profile_checksum(profile: Mapping[str, Any]) -> str:
        unsigned = dict(profile)
        unsigned.pop("profile_checksum", None)
        payload = json.dumps(
            unsigned,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _load_skill_retrieval_session_profile(
        cls,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        path = cls._skill_retrieval_profile_path(session_id)
        sid = cls._session_adapter_key(session_id)

        def _unrecoverable(reason: str) -> dict[str, Any]:
            logger.warning(
                "Skill retrieval profile is not recoverable: session_id=%s reason=%s",
                sid,
                reason,
            )
            return {
                "schema_version": 1,
                "session_id": sid,
                "enabled": False,
                "profile_recovery_error": reason,
            }

        try:
            if not path.is_file():
                return None
            if path.stat().st_size > 16 * 1024 * 1024:
                return _unrecoverable("profile_too_large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                return _unrecoverable("invalid_profile_schema")
            checksum = str(payload.get("profile_checksum") or "")
            if (
                len(checksum) != 64
                or checksum != cls._skill_retrieval_profile_checksum(payload)
            ):
                return _unrecoverable("profile_checksum_mismatch")
            if str(payload.get("session_id") or "") != sid:
                return _unrecoverable("session_id_mismatch")
            from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
                is_valid_skill_retrieval_session_profile,
            )

            if not is_valid_skill_retrieval_session_profile(payload):
                return _unrecoverable("invalid_profile_payload")
            return payload
        except (OSError, ValueError, TypeError):
            return _unrecoverable("unreadable_profile")

    def _persist_skill_retrieval_session_profile(self) -> None:
        toolkit = self._skill_retrieval_toolkit
        if not self._is_session_scoped_adapter:
            return
        if toolkit is None:
            # A global-off session still freezes the legacy choice. Never
            # replace an already restored profile with a newly inferred one.
            if self._restored_skill_retrieval_profile is not None:
                return
            profile: dict[str, Any] = {"schema_version": 1}
        else:
            profile_builder = getattr(toolkit, "session_profile", None)
            if not callable(profile_builder):
                return
            profile = dict(profile_builder())
        profile.update(
            {
                "session_id": self._session_adapter_key(self._parent_session_id),
                "enabled": bool(self._skill_retrieval_session_enabled),
                "requested_model_name": self._initial_skill_retrieval_model_name,
                "model_name": self._skill_retrieval_context_model_name,
                "context_window_tokens": self._skill_retrieval_context_window_tokens,
                "initial_mcp_names": sorted(
                    getattr(self, "_initial_skill_scan_mcp_names", frozenset())
                ),
            }
        )
        profile["profile_checksum"] = self._skill_retrieval_profile_checksum(
            profile
        )
        path = self._skill_retrieval_profile_path(self._parent_session_id)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(profile, ensure_ascii=True, sort_keys=True))
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError:
            logger.warning(
                "Unable to persist Skill retrieval profile: session_id=%s",
                self._parent_session_id,
                exc_info=True,
            )
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _discard_skill_retrieval_session_profile(self) -> None:
        try:
            self._skill_retrieval_profile_path(self._parent_session_id).unlink(
                missing_ok=True
            )
        except OSError:
            logger.warning(
                "Unable to discard Skill retrieval profile: session_id=%s",
                self._parent_session_id,
                exc_info=True,
            )

    def skill_retrieval_status_fields(self) -> dict[str, Any]:
        """Return the child adapter fields exposed to session status callers."""
        return {
            "enabled": bool(self._skill_retrieval_session_enabled)
            and is_skill_retrieval_enabled(self._config_base_cache),
            "model_name": self._skill_retrieval_context_model_name,
        }

    def skill_retrieval_model_profile_matches(
        self,
        requested_model_name: str | None,
    ) -> bool:
        """Whether this child can retain its frozen model retrieval profile."""
        return self._skill_retrieval_model_profile_matches(requested_model_name)

    def restore_skill_retrieval_session(
        self,
        profile: dict[str, Any] | None,
        initial_mcp_names: set[str],
        requested_model_name: str | None,
    ) -> None:
        """Restore immutable Skill and MCP inputs before child construction."""
        self._pending_skill_scan_mcp_names = set(initial_mcp_names)
        self._initial_skill_scan_mcp_names = frozenset(initial_mcp_names)
        self._restored_skill_retrieval_profile = profile
        if profile is not None:
            self._skill_retrieval_context_model_name = str(
                profile.get("model_name") or ""
            )
            try:
                restored_window = int(profile.get("context_window_tokens") or 0)
            except (TypeError, ValueError):
                restored_window = 0
            self._skill_retrieval_context_window_tokens = (
                restored_window if restored_window > 0 else None
            )
        self._set_initial_skill_retrieval_model(
            profile.get("requested_model_name")
            if profile is not None
            else requested_model_name
        )

    def mark_session_mcp_reconcile_started(self) -> None:
        """Claim this child for the session's immutable first MCP snapshot."""
        self._session_mcp_reconcile_started = True

    def persist_skill_retrieval_session_profile(self) -> None:
        """Persist this child's frozen Skill retrieval profile sidecar."""
        self._persist_skill_retrieval_session_profile()

    def clear_pending_skill_scan_mcp_names(
        self,
        pending_scan_names: set[str],
    ) -> None:
        """Consume construction-time MCP scan hints before a rail refresh."""
        pending_holder = self._pending_skill_scan_mcp_names
        if isinstance(pending_holder, set):
            pending_holder.difference_update(pending_scan_names)
        else:
            self._pending_skill_scan_mcp_names = set()

    def discard_transient_skill_retrieval_session_profile(self) -> None:
        """Discard an optimistic sidecar unless it came from stored state."""
        if self._restored_skill_retrieval_profile is None:
            self._discard_skill_retrieval_session_profile()

    def _new_session_scoped_adapter(self, session_id: str) -> "JiuWenSwarmDeepAdapter":
        """Create a child adapter that owns one DeepAgent for a single session."""
        adapter = type(self)()
        adapter.mark_as_session_scoped(session_id)
        # Inherit the channel id so the child's MCP load strategy matches the
        # parent's (TUI loads the global set, web loads nothing on init).
        adapter._channel_id = getattr(self, "_channel_id", "")
        adapter.set_personal_context_runtime_enabled(
            self._personal_context_runtime_enabled
        )
        if self._skill_manager is not None:
            adapter.set_skill_manager(self._skill_manager)
        adapter.set_heartbeat_service(self._heartbeat_service)
        return adapter

    @staticmethod
    def _session_instance_extra_create_kwargs() -> dict[str, Any]:
        """Return subclass-specific arguments for deferred/session creation.

        Base implementation returns an empty dict; subclasses override to
        propagate instance-specific data.
        """
        return {}

    def mark_as_session_scoped(self, session_id: str) -> None:
        self._is_session_scoped_adapter = True
        self._parent_session_id = session_id

    # chat.send equipment: apply package load/unload at the fresh-turn boundary.
    # 团队会话（新旧 canonical：team / team.plan / code.team / team.work.* /
    # team.code.*）一律不装配 chat extension，由 is_team_mode 统一判定；
    # 此处仅保留 auto_harness 单例。
    _SKIP_EXTENSION_MODES: "frozenset[str]" = frozenset({"auto_harness"})

    @staticmethod
    def _equipment_error_response(request: AgentRequest, message: str) -> AgentResponse:
        """Build a terminal chat.error AgentResponse for equipment failures."""
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"event_type": "chat.error", "error": message},
            metadata=request.metadata,
        )

    async def _ensure_chat_extensions(self, request: AgentRequest) -> AgentResponse | None:
        """Apply agent_template / plugin equipment for the upcoming turn.
        """
        params = dict(request.params) if isinstance(request.params, dict) else {}
        mode = str(params.get("mode") or "").strip() or "agent"
        if mode in self._SKIP_EXTENSION_MODES or is_team_mode(mode):
            return None

        params.setdefault("agent_template_name", "")
        params.setdefault("plugin_names", [])

        # Rule1: extension packages must be installed
        marketplace_error = self._marketplace_equipment_gate(params)
        if marketplace_error is not None:
            return self._equipment_error_response(request, marketplace_error)

        # Rule2: extension packages must be connected(particularly, connectors are declared)
        connector_error = self._connector_equipment_gate(params)
        if connector_error is not None:
            return self._equipment_error_response(request, connector_error)

        # Rule3: can only load package at fresh turn(no running agent / goal attached)
        would_change, reason = self._equipment_would_change(params)
        if would_change:
            attach_goal = self._wants_attach_goal(params)
            if attach_goal or self._is_session_live(request.session_id):
                return self._equipment_error_response(
                    request, f"equipment change rejected at non-fresh turn: {reason}"
                )

        try:
            await self._unload_plugins_for_request(params)
            await self._unload_agent_template_for_request(params)
            await self._load_agent_template_for_request(params)
            await self._load_plugins_for_request(params)
        except (ValueError, RuntimeError) as exc:
            return self._equipment_error_response(request, str(exc))
        return None

    @staticmethod
    def _marketplace_equipment_gate(params: dict) -> str | None:
        """Hard-reject non-empty equipment that marketplace does not allow.
        """
        v = params.get("agent_template_name")
        if isinstance(v, str) and v != "":
            if not equipment.is_agent_template_installed(v):
                return f"agent_template not installed: {v}"
        v = params.get("plugin_names")
        if isinstance(v, list):
            for item in v:
                if not isinstance(item, str) or item == "":
                    continue
                if not equipment.is_plugin_allowed(item):
                    return f"plugin not installed: {item}"
        return None

    @staticmethod
    def _connector_equipment_gate(params: dict) -> str | None:
        """Hard-reject when declared connectors are not connected (read-only).
        """
        agent_id = None
        plugin_ids: list[str] = []
        v = params.get("agent_template_name")
        if isinstance(v, str) and v != "":
            agent_id = v
        v = params.get("plugin_names")
        if isinstance(v, list):
            plugin_ids = [item for item in v if isinstance(item, str) and item != ""]
        pending = equipment.unready_connectors(
            equipment.collect_connectors_for_packages(
                agent_template_id=agent_id,
                plugin_ids=plugin_ids,
            )
        )
        if not pending:
            return None
        return (
            f"connector not connected: {', '.join(pending)}; "
            "reconnect from the MCP management page"
        )

    def _equipment_would_change(self, params: dict) -> tuple[bool, str]:
        """Return whether passed equipment fields differ from the session handle."""
        v = params["agent_template_name"]
        if not isinstance(v, str):
            return True, "agent_template_name illegal type"
        # None (not loaded) and "" (cleared) are the same empty state.
        current = self._loaded_agent_template[0] if self._loaded_agent_template else ""
        if v != current:
            return True, f"agent_template_name {current!r}→{v!r}"
        v = params["plugin_names"]
        if not isinstance(v, list):
            return True, "plugin_names illegal type"
        if set(self._loaded_plugins) != set(v):
            return True, "plugin set changed"
        return False, ""

    async def _unload_agent_template_for_request(self, params: dict) -> None:
        """Unload the current expert when switching, clearing, or version drifts."""
        v = params["agent_template_name"]
        if not isinstance(v, str):
            return
        current = self._loaded_agent_template
        if current is None:
            return
        if v != "" and v == current[0]:
            desired_version = equipment.read_manifest_version(
                equipment.resolve_agent_template_dir(v)
            )
            if desired_version == current[2]:
                return
        if self._instance is not None:
            await self._instance.unload_extension(current[1])
        self._loaded_agent_template = None

    async def unload_equipment_if_loaded(self, kind: str, package_id: str) -> None:
        """Unload ``package_id`` from this adapter and live sessions.

        Not loaded is a no-op. Unload failure raises so uninstall does not
        delete the package directory.
        """
        pid = str(package_id or "").strip()
        if not pid:
            return
        try:
            if kind == "agent_templates":
                current = self._loaded_agent_template
                if current is not None and current[0] == pid:
                    if self._instance is not None:
                        await self._instance.unload_extension(current[1])
                    self._loaded_agent_template = None
            elif kind == "plugin_packages":
                entry = self._loaded_plugins.get(pid)
                if entry is not None:
                    if self._instance is not None:
                        await self._instance.unload_extension(entry[0])
                    self._loaded_plugins.pop(pid, None)
        except Exception as exc:
            raise RuntimeError(
                f"unload equipment failed: kind={kind} id={pid}: {exc}"
            ) from exc
        # Session-scoped adapters only unload themselves; root fans out.
        if getattr(self, "_is_session_scoped_adapter", False):
            return
        for adapter in list(getattr(self, "_session_adapters", {}).values()):
            if adapter is not self:
                await adapter.unload_equipment_if_loaded(kind, pid)

    async def _load_agent_template_for_request(self, params: dict) -> None:
        """Load the expert when name or manifest.version differs from the handle."""
        v = params["agent_template_name"]
        if not isinstance(v, str):
            raise ValueError(f"agent_template_name must be str, got {type(v).__name__}")
        if v == "":
            return
        pkg_dir = equipment.resolve_agent_template_dir(v)
        desired_version = equipment.read_manifest_version(pkg_dir)
        current = self._loaded_agent_template
        if (
            current is not None
            and current[0] == v
            and current[2] == desired_version
        ):
            return
        if self._instance is None:
            raise RuntimeError("DeepAgent instance not ready for equipment load")
        record = await self._instance.load_agent_template(str(pkg_dir))
        self._loaded_agent_template = (v, record, desired_version)

    async def _unload_plugins_for_request(self, params: dict) -> None:
        """Unload plugins removed from the set or whose manifest.version drifted."""
        v = params["plugin_names"]
        if not isinstance(v, list):
            return
        desired = set(v)
        to_unload: list[str] = []
        for name, (_record, loaded_version) in self._loaded_plugins.items():
            if name not in desired:
                to_unload.append(name)
                continue
            desired_version = equipment.read_manifest_version(
                equipment.resolve_plugin_dir(name)
            )
            if loaded_version != desired_version:
                to_unload.append(name)
        for name in to_unload:
            entry = self._loaded_plugins.get(name)
            if entry is None:
                continue
            if self._instance is not None:
                await self._instance.unload_extension(entry[0])
            self._loaded_plugins.pop(name, None)

    async def _load_plugins_for_request(self, params: dict) -> None:
        """Load desired plugins missing from the handle or with a new version."""
        v = params["plugin_names"]
        if not isinstance(v, list):
            raise ValueError(f"plugin_names must be list, got {type(v).__name__}")
        for item in v:
            if not isinstance(item, str):
                raise ValueError(f"plugin_names element must be str, got {type(item).__name__}")
        to_load: list[tuple[str, str, Path]] = []
        for name in v:
            pkg_dir = equipment.resolve_plugin_dir(name)
            desired_version = equipment.read_manifest_version(pkg_dir)
            entry = self._loaded_plugins.get(name)
            if entry is not None and entry[1] == desired_version:
                continue
            to_load.append((name, desired_version, pkg_dir))
        if not to_load:
            return
        if self._instance is None:
            raise RuntimeError("DeepAgent instance not ready for equipment load")
        for name, desired_version, pkg_dir in to_load:
            record = await self._instance.load_plugin(str(pkg_dir))
            self._loaded_plugins[name] = (record, desired_version)

    def _get_cached_session_adapter(self, session_id: str | None) -> "JiuWenSwarmDeepAdapter | None":
        sid = self._session_adapter_key(session_id)
        return self._session_adapters.get(sid)

    def get_skill_retrieval_status_profile(
        self,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        """Return a live session's frozen retrieval profile without creating it."""

        adapter = (
            self
            if self._is_session_scoped_adapter
            else self._get_cached_session_adapter(session_id)
        )
        if adapter is None:
            profile = self._load_skill_retrieval_session_profile(session_id)
            if profile is None:
                return None
            profile = dict(profile)
            profile.update(
                {
                    "profile_scope": "session",
                    "session_id": self._session_adapter_key(session_id),
                }
            )
            return profile
        toolkit = getattr(adapter, "_skill_retrieval_toolkit", None)
        if toolkit is None:
            profile = dict(
                getattr(adapter, "_restored_skill_retrieval_profile", None)
                or self._load_skill_retrieval_session_profile(session_id)
                or {}
            )
            if not profile:
                return None
        else:
            profile_builder = getattr(toolkit, "session_profile", None)
            if not callable(profile_builder):
                return None
            profile = dict(profile_builder())
        profile.update(
            {
                "profile_scope": "session",
                "session_id": self._session_adapter_key(session_id),
                **adapter.skill_retrieval_status_fields(),
            }
        )
        return profile

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
        if not remove_runtime_state:
            return
        try:
            get_runtime_state_path(session_id).unlink(missing_ok=True)
            self._skill_retrieval_profile_path(session_id).unlink(missing_ok=True)
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
        reload_scopes: set[str] | None = None,
    ) -> None:
        self._session_adapter_config_version += 1
        self._pending_session_reload_config_base = copy.deepcopy(config_base)
        self._pending_session_reload_env_overrides = (
            copy.deepcopy(env_overrides) if isinstance(env_overrides, dict) else None
        )
        self._pending_session_reload_scopes = (
            set(reload_scopes) if reload_scopes else None
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
                reload_scopes=self._pending_session_reload_scopes,
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
            last_used = self._session_adapter_last_used.get(sid, 0.0)
            if now - last_used < self.SESSION_ADAPTER_IDLE_TTL_SEC:
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
        retain_session = getattr(
            getattr(self, "_heartbeat_service", None),
            "should_retain_session",
            None,
        )
        if callable(retain_session) and await retain_session(sid):
            logger.debug(
                "[JiuWenSwarmDeepAdapter] keep session adapter for Heartbeat: %s",
                sid,
            )
            return False
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
            # If the preceding lock owner removed (or failed to create) the
            # child and no reconnect is queued, this waiter is responsible for
            # pruning the now-empty lock.  Without this, concurrent disconnect
            # cleanup calls leave an empty lock behind forever and
            # has_session_runtime() keeps reporting the session as retained.
            if (
                sid not in self._session_adapters
                and self._is_session_lock_idle(sid, lock)
            ):
                self._session_adapter_last_used.pop(sid, None)
                self._session_adapter_versions.pop(sid, None)
                self._session_adapter_reload_failures.pop(sid, None)
                self._session_adapter_locks.pop(sid, None)
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
                # Idle eviction releases memory, not the logical session.
                self._drop_session_adapter_cache_entry(
                    sid,
                    remove_lock=False,
                    remove_runtime_state=False,
                )
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
        model_name: str | None = None,
        pending_mcp_scan_names: set[str] | None = None,
        history_before_request_id: str | None = None,
    ) -> "JiuWenSwarmDeepAdapter":
        """Return the session-owned adapter, creating and initializing it once."""
        if self._is_session_scoped_adapter:
            self._touch_session_adapter(session_id)
            return self

        sid = self._session_adapter_key(session_id)
        requested_mcp_scan_names = (
            set(pending_mcp_scan_names) if pending_mcp_scan_names is not None else None
        )
        lock = self._session_adapter_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            existing = self._session_adapters.get(sid)
            if existing is not None:
                first_mcp_reconcile = requested_mcp_scan_names is not None and not bool(
                    getattr(
                        existing,
                        "_session_mcp_reconcile_started",
                        False,
                    )
                )
                frozen_mcp_scan_names = set(
                    getattr(
                        existing,
                        "_initial_skill_scan_mcp_names",
                        frozenset(),
                    )
                    or ()
                )
                should_replace_unused = (
                    first_mcp_reconcile
                    and bool(
                        getattr(
                            existing,
                            "_skill_retrieval_session_enabled",
                            False,
                        )
                    )
                    and (
                        frozen_mcp_scan_names != requested_mcp_scan_names
                        or not existing.skill_retrieval_model_profile_matches(
                            model_name
                        )
                    )
                )
                if should_replace_unused:
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] replacing unused session "
                        "adapter before first MCP reconcile: session_id=%s "
                        "frozen_mcp=%s requested_mcp=%s",
                        sid,
                        sorted(frozen_mcp_scan_names),
                        sorted(requested_mcp_scan_names),
                    )
                    await existing.cleanup()
                    self._drop_session_adapter_cache_entry(
                        sid,
                        remove_lock=False,
                    )
                    existing = None

            if existing is not None:
                if requested_mcp_scan_names is not None:
                    # Claim the immutable first-chat snapshot while the parent
                    # session lock is still held. Concurrent sends may reconcile
                    # live MCPs later, but cannot dispose the child underneath
                    # this first caller.
                    existing.mark_session_mcp_reconcile_started()
                await self._reload_session_adapter_if_stale(sid, existing)
                self._touch_session_adapter(sid)
                return existing

            adapter = self._new_session_scoped_adapter(sid)
            restored_profile = self._load_skill_retrieval_session_profile(sid)
            restored_mcp_names = (
                {
                    str(name).strip()
                    for name in restored_profile.get("initial_mcp_names", [])
                    if str(name).strip()
                }
                if restored_profile is not None
                else set(requested_mcp_scan_names or ())
            )
            adapter.restore_skill_retrieval_session(
                restored_profile,
                restored_mcp_names,
                model_name,
            )
            config = (
                dict(self._session_instance_config)
                if isinstance(self._session_instance_config, dict)
                else None
            )
            create_started_at = time.monotonic()
            await adapter.create_instance(
                config,
                mode=self._session_instance_mode,
                sub_mode=self._session_instance_sub_mode,
                **self._session_instance_extra_create_kwargs(),
            )
            adapter.persist_skill_retrieval_session_profile()
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
            # 服务重启 / adapter 被驱逐后重建时，context_engine 内存池为空，
            # 而 chat.send 主路径不会回灌磁盘 history.jsonl——继续历史会话时
            # 模型将拿到空上下文。这里在新建 adapter 后从磁盘恢复上下文
            # （全新会话磁盘无历史，warmup 内部会静默跳过）。
            try:
                from jiuwenswarm.agents.harness.common.session_ops_service import (
                    warmup_session_context,
                )

                await warmup_session_context(
                    deep_agent=getattr(adapter, "_instance", None),
                    session_id=sid,
                    history_before_request_id=history_before_request_id,
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] session context warmup failed: "
                    "session_id=%s error=%s",
                    sid,
                    exc,
                )
            if requested_mcp_scan_names is not None or restored_profile is not None:
                adapter.mark_session_mcp_reconcile_started()
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
            if cleanup_rail:
                circuit_breaker_rail = getattr(self, "_circuit_breaker_rail", None)
                if circuit_breaker_rail is not None:
                    try:
                        circuit_breaker_rail.cleanup_session(sid)
                    except Exception as exc:
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] circuit_breaker cleanup_session(%s) failed: %s",
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
                os.getenv("PLAYWRIGHT_RUNTIME_MCP_ENABLED")
                or os.getenv("BROWSER_RUNTIME_MCP_ENABLED")
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
    def _resolve_managed_browser_type_from_config(
        config_base: dict[str, Any] | None = None,
    ) -> str:
        """Resolve browser type: auto | chrome | msedge."""
        if config_base is None:
            config_base = get_config()
        if not isinstance(config_base, dict):
            return "auto"
        config = resolve_env_vars(config_base)
        browser_cfg = config.get("browser", {}) if isinstance(config, dict) else {}
        if not isinstance(browser_cfg, dict):
            return "auto"
        raw = browser_cfg.get("browser_type", "auto")
        if not isinstance(raw, str):
            return "auto"
        normalized = raw.strip().lower()
        if normalized in {"chrome", "google-chrome", "google_chrome"}:
            return "chrome"
        if normalized in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
            return "msedge"
        return "auto"

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

    @staticmethod
    def _sync_mcp_credentials_environment() -> bool:
        """Inject MCP tokens into ``os.environ``.

        Skill-only MCPs (ctrip-wendao, netease-mail) run their bundled skill
        script via BashTool, which inherits ``os.environ``. MCP stdio servers
        get tokens via the McpServerConfig credential_resolver, but skill
        scripts have no such hook — so their token env vars must be visible in
        the agent process's own environment. This mirrors the browser
        runtime's env-sync pattern (BashTool reads BROWSER_DRIVER the same way).

        Idempotent: writes the current set of connected MCPs' tokens every
        call. Called at agent init/reload and at connect/disconnect.

        Returns True on a real sync (the caller stops on the first live agent
        that can sync, since os.environ is process-global); False when the
        credential store/state is unreadable (the caller falls through to try
        another live agent — covers the cold-start race where the first agent
        in ``self.agents`` has no adapter yet).
        """
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                list_connected_mcps,
            )
            from jiuwenswarm.server.runtime.mcp.credential import (
                CredentialStore,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] mcp credential sync skipped: %s", exc,
            )
            return False
        store = CredentialStore()
        for rec in list_connected_mcps():
            n = str(rec.get("name", "")).strip()
            if not n:
                continue
            try:
                tokens = store.get_all(n)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] mcp '%s' tokens read failed: %s",
                    n, exc,
                )
                continue
            for key, value in tokens.items():
                k = str(key)
                if k:
                    os.environ[k] = str(value) if value is not None else ""
        return True

    @staticmethod
    def _mcp_env_keys(name: str) -> list[str]:
        """Token env keys an MCP owns: CredentialStore keys + schema's
        required field keys (covers the post-delete case where disconnect
        wants to clear env vars whose stored value is already gone)."""
        n = str(name or "").strip()
        if not n:
            return []
        keys: set[str] = set()
        try:
            from jiuwenswarm.server.runtime.mcp.credential import (
                CredentialStore,
                required_tokens_from_schema,
            )
            store_keys = set(CredentialStore().get_all(n).keys())
            keys.update(store_keys)
            keys.update(required_tokens_from_schema(n))
        except Exception:  # noqa: BLE001
            pass
        return sorted(keys)

    def _clear_mcp_credentials_environment(self, name: str) -> None:
        """Remove a disconnected MCP's token env vars from ``os.environ``.

        Companion to :meth:`_sync_mcp_credentials_environment`. Called from
        the disconnect path so a stale token doesn't linger in the agent
        environment after the user disconnects the MCP.
        """
        for k in self._mcp_env_keys(name):
            os.environ.pop(k, None)

    def _sync_browser_runtime_environment(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        """Synchronize browser launch settings before browser runtimes are built."""
        headless = self._resolve_headless_from_config(config_base)
        mcp_args_raw = (
            os.getenv("PLAYWRIGHT_MCP_ARGS") or "-y @playwright/mcp@latest"
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

        browser_type = self._resolve_managed_browser_type_from_config(config_base)
        if browser_type and browser_type != "auto":
            os.environ["BROWSER_MANAGED_TYPE"] = browser_type
        else:
            os.environ.pop("BROWSER_MANAGED_TYPE", None)

        # Hint MCP/CDP which Chromium flavor to expect (auto → leave unset / chrome default).
        path_l = chrome_path.replace("\\", "/").lower() if chrome_path else ""
        path_looks_edge = bool(
            path_l
            and (
                "msedge" in path_l
                or "/microsoft/edge/" in path_l
                or "microsoft edge" in path_l
            )
        )
        if browser_type == "msedge" or path_looks_edge:
            os.environ["PLAYWRIGHT_MCP_BROWSER"] = "msedge"
        elif browser_type == "chrome" or chrome_path:
            os.environ["PLAYWRIGHT_MCP_BROWSER"] = "chrome"
        else:
            # auto without explicit path: ManagedBrowserDriver chooses Chrome then Edge.
            os.environ.pop("PLAYWRIGHT_MCP_BROWSER", None)

        logger.info(
            "[%s] browser runtime config: headless=%s, browser_type=%s, chrome_path=%s",
            type(self).__name__,
            headless,
            browser_type or "auto",
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
        """Build configured research + browser subagents (agent 模式).

        每个 spec 都带上主 Agent 的 ``sys_operation``，让子 Agent 与父 Agent 共享同一
        个文件系统边界；留空时 ``DeepAgent.create_subagent`` 会另建一个受
        ``restrict_to_sandbox`` 约束的 LOCAL SysOperation，本地模式下把子 Agent 锁死在
        workspace 内、sandbox 模式下又让它逃出沙箱落到宿主机。
        """
        react_cfg = config if isinstance(config, dict) else {}
        subagents_cfg = react_cfg.get("subagents")

        resolved_language = self._resolve_runtime_language()
        workspace = self._workspace_dir or "./"
        sys_operation = self._sys_operation
        subagents: list[Any] = []
        should_add_general_purpose = False

        statusline_setup_cfg = (
            subagents_cfg.get(STATUSLINE_SETUP_AGENT_TYPE)
            if isinstance(subagents_cfg, dict)
            else None
        )
        if self._is_subagent_default_enabled(statusline_setup_cfg):
            statusline_setup_options = (
                statusline_setup_cfg if isinstance(statusline_setup_cfg, dict) else {}
            )
            subagents.append(
                build_statusline_setup_agent_config(
                    model,
                    workspace=workspace,
                    sys_operation=sys_operation,
                    language=resolved_language,
                    max_iterations=parse_int(
                        statusline_setup_options.get("max_iterations"),
                        DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
                    ),
                )
            )

        if isinstance(subagents_cfg, dict):
            general_agent_cfg = subagents_cfg.get("general_agent")
            if self._is_subagent_enabled(general_agent_cfg):
                should_add_general_purpose = True

            research_agent_cfg = subagents_cfg.get("research_agent")
            if self._is_subagent_enabled(research_agent_cfg):
                subagents.append(
                    build_research_agent_config(
                        model,
                        workspace=workspace,
                        sys_operation=sys_operation,
                        language=resolved_language,
                        max_iterations=parse_int(
                            research_agent_cfg.get("max_iterations"),
                            react_cfg.get("max_iterations", 15),
                        ),
                    )
                )

        browser_agent_cfg = (
            subagents_cfg.get("browser_agent") if isinstance(subagents_cfg, dict) else {}
        )

        # Swarm members and the main browser subagent read these variables when
        # their browser runtimes are built.
        self._sync_browser_runtime_environment(config_base)
        # Skill-only MCPs' bundled scripts read tokens from os.environ (BashTool
        # inherits it). Sync now so a freshly built agent process has the
        # connected MCPs' tokens available before any skill runs.
        self._sync_mcp_credentials_environment()

        browser_enabled = self._browser_runtime_enabled()
        if browser_enabled:
            if not str(os.getenv("BROWSER_DRIVER") or "").strip():
                os.environ["BROWSER_DRIVER"] = "managed"
                logger.info(
                    "[JiuWenSwarmDeepAdapter] browser subagent enabled without BROWSER_DRIVER; "
                    "defaulting to managed mode"
                )
            subagents.append(
                build_browser_agent_config(
                    model,
                    workspace=workspace,
                    sys_operation=sys_operation,
                    language=resolved_language,
                    max_iterations=parse_int(
                        (
                            browser_agent_cfg.get("max_iterations")
                            if isinstance(browser_agent_cfg, dict)
                            else None
                        ),
                        DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
                    ),
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
                    workspace_dir=self._workspace_dir,
                    subagents_cfg=subagents_cfg,
                    model=model,
                    workspace=workspace,
                    logger_name=__name__,
                    model_cache=self._model_cache,
                    sys_operation=sys_operation,
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
        """Build McpServerConfig from a config entry, resolving ${VAR} placeholders.

        state.json stores placeholders, never real tokens. Here we inject a
        credential_resolver that checks the MCP's CredentialStore (keyed by
        entry name = MCP name) + os.environ, so the spawned stdio process /
        HTTP request gets real credentials while state.json stays secret-free.

        server_id_scope="jiuwenswarm" produces a stable server_id so reload
        doesn't re-register a duplicate MCP server (process leak).
        """
        name = str(entry.get("name", "")).strip()
        resolver = build_mcp_credential_resolver(name)
        return build_mcp_server_config(entry, server_id_scope="jiuwenswarm", credential_resolver=resolver)

    @staticmethod
    def _extract_enabled_mcp_server_entries(config_base: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_enabled_mcp_server_entries(config_base)

    @staticmethod
    def _yaml_enabled_mcp_entries(config_base: dict[str, Any]) -> list[dict[str, Any]]:
        """Enabled config.yaml mcp.servers; fallback to config_base on read error."""
        try:
            servers = get_config_yaml_mcp_servers()
        except Exception:  # noqa: BLE001
            servers = []
            if isinstance(config_base, dict):
                mcp_cfg = config_base.get("mcp", {})
                if isinstance(mcp_cfg, dict):
                    servers = mcp_cfg.get("servers", []) or []
        return [s for s in servers
                if isinstance(s, dict) and bool(s.get("enabled", True))]

    @staticmethod
    def _state_enabled_mcp_entries() -> list[dict[str, Any]]:
        """Enabled state.json MCPs (TUI-created / web-connected, enabled=True).

        TUI channel (root adapter) loads config.yaml ``enabled`` ∪ these —
        its global default set. Web ignores ``enabled`` (session-level via
        chat.send's ``mcp`` field), so the root is the only caller. C/D
        (skill-only / pure-cli) are dropped by ``record_to_mcp_entry``
        (returns None — no MCP host); they surface via skills, and the root
        never scans MCP skill dirs.
        """
        from jiuwenswarm.server.runtime.mcp.state_store import (
            list_tui_enabled_mcps, record_to_mcp_entry,
        )
        out: list[dict[str, Any]] = []
        try:
            for rec in list_tui_enabled_mcps():
                name = str(rec.get("name", "") or "").strip()
                if not name:
                    continue
                entry = record_to_mcp_entry(name, rec)
                if entry is None:
                    continue  # C/D — no MCP host, surface via skills
                out.append(entry)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] read state.json enabled mcps failed: %s",
                exc,
            )
        return out

    async def _register_mcp_server(self, cfg: McpServerConfig, *, tag: str) -> bool:
        if self._instance is None:
            return False
        # stdio: command 必须可执行（npx/uvx/node 等），否则 SDK 启动子进程会
        # 抛 OSError 但被 connect() 的 except 吞掉，前端只看到笼统失败。
        # 提前 shutil.which 检查，缺失则直接报"命令不存在"，不发到 SDK。
        if str(cfg.client_type).strip().lower() == "stdio":
            import shutil
            cmd = str((cfg.params or {}).get("command", "")).strip()
            if cmd and not shutil.which(cmd):
                raise RuntimeError(
                    f"MCP '{cfg.server_name}' command '{cmd}' not found on PATH "
                    f"(install it or add to PATH)"
                )
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
                "[JiuWenSwarmDeepAdapter] MCP server register failed: "
                "%s (ok=False, result=%r, auth_headers_diag=%s)",
                cfg.server_name, result, _diag_auth_headers(cfg),
            )
            # Surface the runner's error reason so the connect handler can report
            # it to the frontend instead of a silent "connected". The runner
            # returns an openjiuwen ``Error`` object whose value is exposed via
            # the ``msg()`` / ``error()`` methods (and the ``_error`` attr), NOT
            # via ``error_message`` / ``message`` / ``reason`` attributes.
            runner_reason = ""
            for getter in ("error", "msg"):
                fn = getattr(result, getter, None)
                if callable(fn):
                    try:
                        val = fn()
                    except Exception:  # noqa: BLE001
                        val = None
                    if val:
                        runner_reason = str(val)
                        break
            if not runner_reason:
                val = getattr(result, "_error", None)
                if val:
                    runner_reason = str(val)
            raise RuntimeError(
                runner_reason
                or f"MCP server '{cfg.server_name}' register rejected (no runner reason)"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenSwarmDeepAdapter] MCP server '%s' register failed: %s",
                cfg.server_name, exc,
            )
            raise

    async def _unregister_mcp_server(self, server_id: str) -> None:
        if self._instance is None:
            return
        cfg = self._registered_mcp_servers.get(server_id)
        server_name = getattr(cfg, "server_name", "") if cfg is not None else ""
        # openjiuwen's remove_mcp_server closes the MCP client (SSE/HTTP). For
        # SSE clients the close runs a TaskGroup __aexit__ that can raise
        # "Attempted to exit cancel scope in a different task" — this propagates
        # into the caller's task and cancels it, tearing down the whole
        # mcp.disconnect handler (no reply to the frontend, state.json left
        # inconsistent). Isolate the removal in a fresh task so the cancel-scope
        # error stays contained; the bookkeeping below runs regardless of the
        # removal outcome.
        await self._safe_remove_mcp_server(server_id)
        if server_name:
            try:
                self._instance.ability_manager.remove(server_name)
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] MCP ability remove failed: %s", exc)
        self._registered_mcp_server_ids.discard(server_id)
        self._registered_mcp_servers.pop(server_id, None)

    async def _safe_remove_mcp_server(self, server_id: str) -> None:
        """Run remove_mcp_server in a fresh task so its TaskGroup's cancel-scope
        errors stay contained (SSE clients raise 'exit cancel scope in a
        different task' on close)."""
        async def _do() -> None:
            try:
                await Runner.resource_mgr.remove_mcp_server(server_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[JiuWenSwarmDeepAdapter] remove_mcp_server inner: %s", exc)
        try:
            t = asyncio.create_task(_do())
            await asyncio.wait_for(t, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[JiuWenSwarmDeepAdapter] MCP remove timed out/errored: %s", exc)

    # --- Targeted single-MCP control (no full reload) ---
    # The mcp connect/disconnect/enable/disable handlers call these instead
    # of reload_agents_config so one MCP's change doesn't resync every MCP in
    # mcp.servers (the heavy _sync_mcp_servers_for_runtime diff).

    def _iter_mcp_target_adapters(self) -> list["JiuWenSwarmDeepAdapter"]:
        """Adapters whose _registered_mcp_servers must be touched for a
        targeted MCP add/remove.

        A parent adapter owns both its own _registered_mcp_servers (the main
        agent's MCPs) and a pool of session-scoped child adapters, each with
        its OWN _registered_mcp_servers — the session child is where the
        agent run actually calls MCP tools. So a targeted register/unregister
        on the parent must propagate to every live session child; a session
        child only owns itself and must not recurse.

        getattr guards bare __new__-constructed adapters (tests) that skipped
        __init__ and so lack _is_session_scoped_adapter / _session_adapters.
        """
        if getattr(self, "_is_session_scoped_adapter", False):
            return [self]
        targets: list[JiuWenSwarmDeepAdapter] = [self]
        for _sid, child in list(getattr(self, "_session_adapters", {}).items()):
            if child is self:
                continue
            targets.append(child)
        return targets

    async def register_mcp_by_name(self, name: str, *, tag: str = "agent.mcp") -> bool:
        """Register one MCP by its name (from state.json/config.yaml merged list).

        Propagates to every live session child so a freshly connected MCP is
        visible to all sessions of this adapter, not just the parent's main run.

        Idempotent: if already registered under this name (in a given adapter),
        that adapter skips. Returns True if at least one adapter applied it.
        Raises RuntimeError when no adapter applied it, carrying the first
        adapter's error reason (e.g. the runner's "add mcp server failed"
        message) so the connect handler can report failure to the frontend
        instead of a silent "connected".

        Skill-only / pure-CLI MCPs have no server entry (no mcp.json command,
        no stdio bin) — get_mcp_server_config returns None. They expose tools
        via bundled skills, not via an MCP server, so there is nothing to
        register; return True so the connect handler's apply_mcp_change
        succeeds instead of raising "register rejected".
        """
        entry = get_mcp_server_config(name)
        if not entry:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] register_mcp_by_name: " \
                "'%s' has no MCP entry (CLI/skill-only, no-op)",
                name
            )
            return True
        applied_any = False
        first_error: str = ""
        for adapter in self._iter_mcp_target_adapters():
            if adapter._instance is None:
                continue
            already = any(
                str(getattr(cfg, "server_name", "") or "").strip() == name
                for _sid, cfg in adapter._registered_mcp_servers.items()
            )
            if already:
                applied_any = True
                continue
            cfg = adapter._build_mcp_server_config(entry)
            if cfg is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] register_mcp_by_name: '%s' invalid entry (adapter=%s)",
                    name, "session" if adapter is not self else "parent",
                )
                continue
            try:
                if await adapter._register_mcp_server(cfg, tag=tag):
                    applied_any = True
                elif not first_error:
                    first_error = (
                        f"MCP server register failed for '{name}' "
                        f"(adapter returned ok=False; see prior WARNING for the "
                        f"runner error / cancel-scope error)"
                    )
            except Exception as exc:  # noqa: BLE001
                # _register_mcp_server (via openjiuwen add_tool_server) surfaces
                # plain Exceptions only — WorkflowError for connect/register
                # failures, RuntimeError for ok=False. Collect the first failure
                # so the caller can report it; raise if none succeeded.
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] register_mcp_by_name '%s' failed on %s: %s",
                    name,
                    "session" if adapter is not self else "parent",
                    exc,
                )
                if not first_error:
                    first_error = str(exc) or repr(exc)
        if not applied_any:
            raise RuntimeError(first_error or f"MCP '{name}' register failed")
        return applied_any

    async def unregister_mcp_by_name(self, name: str) -> bool:
        """Unregister one MCP by name (no full reload).

        Propagates to every live session child — otherwise the child's MCP
        stays registered and the agent run can still call those tools after a
        disconnect. Returns True if at least one adapter removed it.
        """
        removed_any = False
        for adapter in self._iter_mcp_target_adapters():
            if adapter._instance is None:
                continue
            for server_id, cfg in list(adapter._registered_mcp_servers.items()):
                if str(getattr(cfg, "server_name", "") or "").strip() == name:
                    try:
                        await adapter._unregister_mcp_server(server_id)
                        removed_any = True
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[JiuWenSwarmDeepAdapter] unregister_mcp_by_name '%s' failed on %s: %s",
                            name,
                            "session" if adapter is not self else "parent",
                            exc,
                        )
                    break
        if not removed_any:
            logger.debug("[JiuWenSwarmDeepAdapter] unregister_mcp_by_name: '%s' not registered", name)
        return removed_any

    async def reconcile_session_mcp(
        self,
        session_id: str | None,
        needed: list[str] | None,
        *,
        model_name: str | None = None,
        history_before_request_id: str | None = None,
    ) -> None:
        """Reconcile this session's MCP set to ``needed`` (idempotent diff).

        ``needed`` is chat.send's ``mcp`` field (MCP server names). Diffs
        against the session child's self-loaded set (``_session_selected_mcp``)
        and adds/removes only the delta. ``None`` (field absent) and ``[]``
        both clear that set; init-registered config.yaml MCPs are never in it,
        so they survive. MCP-server forms (stdio/remote/hybrid) register/
        unregister; cli/skill-only forms surface via ``_skill_scan_dirs``
        (filtered by the live selection after cold-start assembly), picked up
        by the refresh_skill_rails on change. Because reconcile can be the
        first session-child creation point, it also forwards the current
        request's disk-history boundary into context warmup.
        """
        needed_set = {str(n).strip() for n in (needed or []) if isinstance(n, str) and str(n).strip()}
        child = await self._get_or_create_session_adapter(
            session_id,
            model_name=model_name,
            pending_mcp_scan_names=needed_set,
            history_before_request_id=history_before_request_id,
        )
        if child._instance is None:
            pending_holder = getattr(
                child,
                "_pending_skill_scan_mcp_names",
                None,
            )
            if isinstance(pending_holder, set):
                pending_holder.clear()
            return
        selected = child._session_selected_mcp
        pending_scan_names = set(
            getattr(child, "_pending_skill_scan_mcp_names", set())
        )
        changed = False
        failed_names: set[str] = set()
        # Add: in needed but not yet self-loaded.
        for name in needed_set - selected:
            try:
                await child.register_mcp_by_name(name)
                selected.add(name)
                changed = True
            except Exception as exc:  # noqa: BLE001
                failed_names.add(name)
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] reconcile add '%s' failed: %s",
                    name, exc,
                )
        # Remove: self-loaded but not in needed. None (absent) and [] both
        # land here as needed_set=∅ → unload all self-loaded. init's config.yaml
        # MCPs are not in `selected`, so they survive.
        for name in list(selected - needed_set):
            if not name:
                selected.discard(name)
                continue
            try:
                await child.unregister_mcp_by_name(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] reconcile remove '%s' failed: %s",
                    name, exc,
                )
            selected.discard(name)
            changed = True
        # The pending set was only a construction-time scan hint. Clear it
        # after real registration/unregistration has established the live set.
        # If registration failed, refreshing now also removes the optimistic
        # bundled Skill roots from the ordinary SkillUseRail.
        child.clear_pending_skill_scan_mcp_names(pending_scan_names)

        # A cli/skill MCP's bundled skills surface via _skill_scan_dirs (filtered
        # by _session_selected_mcp), not register_mcp_by_name (no server entry).
        # Refresh so the rail picks up the new selection.
        if changed or pending_scan_names:
            try:
                await child.refresh_skill_rails()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] refresh_skill_rails after "
                    "reconcile failed: %s", exc,
                )
        if failed_names:
            # A construction-time MCP hint may put unavailable bundled Skills in
            # the immutable appendix. Keep the live-session freeze, but do not
            # persist that optimistic snapshot after a transient registration
            # failure. A restored profile remains the authoritative state.
            child.discard_transient_skill_retrieval_session_profile()

    def _start_mcp_prewarm(self) -> None:
        """后台预热 connected MCP 的进程级连接缓存（仅 web root adapter）。

        只建 Runner.resource_mgr 缓存，不挂任何会话（不碰
        _session_selected_mcp / _registered_mcp_servers），保持 default-False 契约。
        首轮对话 reconcile 命中 existing-entry 不重 spawn。失败隔离：单个 MCP
        预热失败不阻断其余、不降级 state。
        """
        if self._mcp_prewarm_task is not None and not self._mcp_prewarm_task.done():
            return
        self._mcp_prewarm_task = asyncio.create_task(
            self._do_mcp_prewarm(),
            name=f"mcp-prewarm-{self._agent_name}",
        )

    async def _do_mcp_prewarm(self) -> None:
        """委托 ``prewarm_connected_mcps`` 建进程级缓存（启动预热任务的幂等兜底）。"""
        from jiuwenswarm.common.mcp_config import prewarm_connected_mcps
        await prewarm_connected_mcps()

    async def _register_mcp_servers_from_config(
        self, config_base: dict[str, Any], *, tag: str = "agent.main"
    ) -> None:
        """Register the TUI channel's global-default MCP set at init.

        That set is the union of config.yaml ``mcp.servers`` (legacy stock,
        TUI-managed) and state.json ``enabled=True`` records (TUI-created /
        web-connected that the user enabled for the TUI). Only called on the
        root adapter — a session-scoped child skips this (web is session-level:
        its MCPs load via reconcile_session_mcp from chat.send's ``mcp``
        field, config.yaml is tui-only). Per-MCP failure isolation: a bad
        entry logs and continues without starving the rest.
        """
        # state.json first so a name present in BOTH files resolves to the
        # state.json entry (user's latest via web connect / TUI add) —
        # config.yaml is legacy stock, state.json is the active source.
        yaml_entries = self._yaml_enabled_mcp_entries(config_base)
        state_entries = self._state_enabled_mcp_entries()
        enabled_entries = state_entries + yaml_entries
        seen_names: set[str] = set()
        for entry in enabled_entries:
            nm = str(entry.get("name", "") or "").strip()
            if nm and nm in seen_names:
                # state.json already registered this name; skip the config.yaml dup.
                continue
            if nm:
                seen_names.add(nm)
            cfg = self._build_mcp_server_config(entry)
            if cfg is None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skip invalid mcp server entry: %s",
                    entry.get("name", "<unknown>"),
                )
                continue
            server_name = str(getattr(cfg, "server_name", "") or "").strip()
            try:
                ok = await self._register_mcp_server(cfg, tag=tag)
                if not ok:
                    # _register_mcp_server returns False on preflight
                    # unreachability (HTTP host down) — a real failure, not a
                    # skip. Normalize it into the except path so it degrades
                    # state just like a raised register error.
                    raise RuntimeError(
                        f"MCP '{server_name}' preflight unreachable "
                        f"(host down / DNS failed) — skipping registration"
                    )
                # Register succeeded — promote connecting → connected. A
                # connecting MCP (left over from a connect interrupted by a
                # restart) just proved it can register, so it is now truly
                # connected. A connected MCP stays connected (idempotent flip).
                # Without this, a restart-while-connecting MCP would stay
                # connecting forever (frontend stuck on "connecting") even
                # though its server is live.
                if server_name:
                    try:
                        from jiuwenswarm.server.runtime.mcp.state_store import (
                            get_mcp_record,
                            set_mcp_state,
                        )
                        rec = get_mcp_record(server_name)
                        if rec and rec.get("state") == "connecting":
                            set_mcp_state(server_name, state="connected")
                    except Exception as prom_exc:  # noqa: BLE001
                        logger.debug(
                            "[JiuWenSwarmDeepAdapter] connecting→connected "
                            "promote for '%s' failed: %s",
                            server_name, prom_exc,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MCP '%s' register failed on "
                    "startup, marking disconnected (one-shot; won't retry on "
                    "next restart): %s",
                    server_name, exc,
                )
                if server_name:
                    try:
                        from jiuwenswarm.server.runtime.mcp.state_store import (
                            set_mcp_state,
                        )
                        set_mcp_state(server_name, state="disconnected")
                    except Exception as degr_exc:  # noqa: BLE001
                        logger.debug(
                            "[JiuWenSwarmDeepAdapter] state degrade for '%s' failed: %s",
                            server_name, degr_exc,
                        )
                # continue to next MCP — one failure must not starve the rest

    async def _sync_mcp_servers_for_runtime(
        self, config_base: dict[str, Any], *, tag: str = "agent.reload"
    ) -> None:
        if self._instance is None:
            return
        # Desired differs by channel (not adapter scope):
        # - TUI channel (root + session children): loads its global-default set
        #   (config.yaml ``enabled`` ∪ state.json ``enabled=True``) on every
        #   reload, so an enable/disable/add/remove re-syncs the live set.
        # - Web channel: loads NOTHING here — web is session-level, its MCPs
        #   come solely from reconcile_session_mcp (chat.send's ``mcp``
        #   field). A reload must not register the global set into a web
        #   session or it would break the default-False contract.
        is_web = getattr(self, "_channel_id", "") == "web"
        if is_web:
            yaml_entries = []
            state_entries: list[dict[str, Any]] = []
        else:
            yaml_entries = self._yaml_enabled_mcp_entries(config_base)
            state_entries = self._state_enabled_mcp_entries()
        # Include session-selected MCPs (reconcile-loaded) so a reload doesn't
        # drop the user's per-session selection.
        selected_entries = []
        for name in getattr(self, "_session_selected_mcp", set()):
            entry = get_mcp_server_config(name)
            if entry is not None:
                selected_entries.append(entry)
        enabled_entries = yaml_entries + state_entries + selected_entries
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
            # Isolate per-MCP failures (same as the init path above): a bad MCP
            # degrades to disconnected, the rest still register.
            try:
                await self._register_mcp_server(desired_by_name[server_name], tag=tag)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MCP '%s' register failed on "
                    "reload, marking disconnected: %s",
                    server_name, exc,
                )
                try:
                    from jiuwenswarm.server.runtime.mcp.state_store import (
                        set_mcp_state,
                    )
                    set_mcp_state(server_name, state="disconnected")
                except Exception as degr_exc:  # noqa: BLE001
                    logger.debug(
                        "[JiuWenSwarmDeepAdapter] state degrade for '%s' failed: %s",
                        server_name, degr_exc,
                    )

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
            # Same per-MCP isolation as to_add above.
            try:
                await self._register_mcp_server(desired_cfg, tag=tag)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MCP '%s' re-register failed on "
                    "reload, marking disconnected: %s",
                    server_name, exc,
                )
                try:
                    from jiuwenswarm.server.runtime.mcp.state_store import (
                        set_mcp_state,
                    )
                    set_mcp_state(server_name, state="disconnected")
                except Exception as degr_exc:  # noqa: BLE001
                    logger.debug(
                        "[JiuWenSwarmDeepAdapter] state degrade for '%s' failed: %s",
                        server_name, degr_exc,
                    )

    @staticmethod
    def _build_vision_model_config(
        config_base: dict[str, Any],
    ) -> VisionModelConfig | None:
        """Build DeepAgent vision config from service config/env mapping."""
        if not (
            multimodal_model_enabled(config_base, "vision")
            and complete_multimodal_model_configured(config_base, "vision")
        ):
            logger.info(
                "[JiuWenSwarmDeepAdapter] vision tools skipped: capability disabled or config incomplete"
            )
            return None
        apply_vision_model_config_from_yaml(config_base)
        api_key = str(os.getenv("VISION_API_KEY", "")).strip()
        base_url = str(os.getenv("VISION_BASE_URL") or os.getenv("VISION_API_BASE") or "").strip()
        model_name = str(os.getenv("VISION_MODEL") or os.getenv("VISION_MODEL_NAME") or "").strip()
        if not api_key or not base_url or not model_name:
            logger.info("[JiuWenSwarmDeepAdapter] vision tools skipped: incomplete config")
            return None
        return VisionModelConfig(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            max_retries=parse_int(os.getenv("VISION_MAX_RETRIES"), 3),
        )

    @staticmethod
    def _build_audio_model_config(
        config_base: dict[str, Any],
    ) -> AudioModelConfig | None:
        """Build DeepAgent audio config from service config/env mapping."""
        if not (
            multimodal_model_enabled(config_base, "audio")
            and complete_multimodal_model_configured(config_base, "audio")
        ):
            logger.info(
                "[JiuWenSwarmDeepAdapter] audio tools skipped: capability disabled or config incomplete"
            )
            return None
        apply_audio_model_config_from_yaml(config_base)
        api_key = str(os.getenv("AUDIO_API_KEY", "")).strip()
        base_url = str(os.getenv("AUDIO_BASE_URL") or os.getenv("AUDIO_API_BASE") or "").strip()
        if not api_key or not base_url:
            logger.info("[JiuWenSwarmDeepAdapter] audio tools skipped: incomplete config")
            return None
        transcription_model = str(
            os.getenv("AUDIO_TRANSCRIPTION_MODEL") or os.getenv("AUDIO_MODEL_NAME") or ""
        ).strip()
        question_answering_model = str(
            os.getenv("AUDIO_QUESTION_ANSWERING_MODEL") or os.getenv("AUDIO_MODEL_NAME") or ""
        ).strip()
        config_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "max_retries": parse_int(os.getenv("AUDIO_MAX_RETRIES"), 3),
            "http_timeout": parse_int(os.getenv("AUDIO_HTTP_TIMEOUT"), 20),
            "max_audio_bytes": parse_int(
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
            config_kwargs["question_answering_model"] = question_answering_model
        return AudioModelConfig(**config_kwargs)

    @staticmethod
    def _build_video_model_config(
        config_base: dict[str, Any],
    ) -> bool:
        """Build DeepAgent video config from service config/env mapping."""
        if not (
            multimodal_model_enabled(config_base, "video")
            and complete_multimodal_model_configured(config_base, "video")
        ):
            logger.info(
                "[JiuWenSwarmDeepAdapter] video tools skipped: capability disabled or config incomplete"
            )
            return False
        apply_video_model_config_from_yaml(config_base)
        video_api_key = str(os.getenv("VIDEO_API_KEY", "")).strip()
        video_api_base = str(os.getenv("VIDEO_API_BASE", "")).strip()
        video_model_name = str(os.getenv("VIDEO_MODEL_NAME", "")).strip()
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
        if not os.getenv("IMAGE_GEN_API_KEY"):
            logger.info("[JiuWenSwarmDeepAdapter] image_gen tool skipped: incomplete config")
            return False
        return True

    @staticmethod
    def _build_video_gen_model_config(
        config_base: dict[str, Any],
    ) -> bool:
        """Build video generation config; falls back to image_gen credentials."""
        apply_video_gen_model_config_from_yaml(config_base)
        if not os.getenv("VIDEO_GEN_API_KEY"):
            logger.info("[JiuWenSwarmDeepAdapter] video_gen tool skipped: incomplete config")
            return False
        return True

    def _iter_runtime_audio_tools(self, agent_id: str | None) -> list[Any]:
        """Return audio tools only while the audio capability is enabled."""
        if self._audio_model_config is None:
            return []
        return list(
            create_audio_tools(
                language=self._resolve_runtime_language(),
                audio_model_config=self._audio_model_config,
                agent_id=agent_id,
            )
        )

    def _refresh_multimodal_configs(
        self,
        config_base: dict[str, Any],
    ) -> None:
        """Refresh cached multimodal configs and live tool instances."""
        self._vision_model_config = self._build_vision_model_config(config_base)
        self._audio_model_config = self._build_audio_model_config(config_base)
        self._video_model_config = self._build_video_model_config(config_base)
        self._image_gen_model_config = self._build_image_gen_model_config(config_base)
        self._video_gen_model_config = self._build_video_gen_model_config(config_base)

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
        """Create the session-local installed-Skill reference directory tool."""
        if not self._skill_retrieval_tools_enabled_for_runtime(
            self._config_base_cache
        ):
            logger.info("[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit skipped: disabled")
            return []
        skill_retrieval_toolkit = self._get_or_create_skill_retrieval_toolkit()
        tools = skill_retrieval_toolkit.get_tools()
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillRetrievalToolkit built: tools=%s",
            [tool.card.name for tool in tools],
        )
        return tools

    def _skill_retrieval_session_scope(self) -> str:
        return self._parent_session_id or f"adapter-{id(self):x}"

    def _set_initial_skill_retrieval_model(self, model_name: str | None) -> None:
        """Set the requested model used to freeze a new session's 1% budget."""

        if self._initial_skill_retrieval_model_name:
            return
        self._initial_skill_retrieval_model_name = str(model_name or "").strip()

    def _skill_retrieval_model_profile_matches(
        self,
        requested_model_name: str | None,
    ) -> bool:
        """Whether an unused warm child already has the requested 1% profile."""

        requested = str(requested_model_name or "").strip()
        if not requested:
            return True
        selected_model = self._resolve_model_by_name(requested)
        if selected_model is None:
            return not self._skill_retrieval_context_model_name
        actual_name = str(
            getattr(getattr(selected_model, "model_config", None), "model_name", "")
            or ""
        ).strip()
        settings = build_model_discovery_settings(
            self._config_base_cache,
            model=selected_model,
        )
        return (
            actual_name == self._skill_retrieval_context_model_name
            and settings.context_window_tokens
            == self._skill_retrieval_context_window_tokens
        )

    def _freeze_skill_retrieval_context_window(
        self,
        config_base: dict[str, Any],
        default_model: Model,
    ) -> None:
        """Resolve the selected model's context window once for this session."""

        if self._skill_retrieval_context_window_tokens is not None:
            return

        selected_model = (
            self._resolve_model_by_name(self._initial_skill_retrieval_model_name)
            or default_model
        )
        model_name = str(
            getattr(getattr(selected_model, "model_config", None), "model_name", "")
            or ""
        ).strip()
        settings = build_model_discovery_settings(config_base, model=selected_model)
        self._skill_retrieval_context_model_name = model_name
        self._skill_retrieval_context_window_tokens = settings.context_window_tokens
        self._skill_retrieval_settings = settings

    def _live_skill_retrieval_disabled_skills(self) -> list[str]:
        """Return the persisted deny-list used to materialize the live SkillFS.

        Skill-management RPCs and chat sessions can be served by different
        adapter instances that share the state file but not the manager's
        in-memory ``_state`` snapshot. Reload before every SkillFS refresh so a
        toggle made through the UI is reflected by the next inventory command.
        The full disabled list is safe here because the filesystem scan itself
        intersects it with Skills that are currently present.
        """

        manager = self._skill_manager
        if manager is None:
            return []
        reload_state = getattr(manager, "reload_state", None)
        if callable(reload_state):
            try:
                reload_state()
            except Exception:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] failed to reload Skill state "
                    "before retrieval",
                    exc_info=True,
                )
        list_disabled = getattr(manager, "list_disabled_skills", None)
        if not callable(list_disabled):
            list_disabled = getattr(
                manager,
                "list_execution_disabled_skills",
                None,
            )
        if not callable(list_disabled):
            return []
        try:
            return [str(name) for name in list_disabled() if str(name).strip()]
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] failed to read disabled Skills "
                "before retrieval",
                exc_info=True,
            )
            return []

    def _get_or_create_skill_retrieval_toolkit(self) -> SkillRetrievalToolkit:
        toolkit = self._skill_retrieval_toolkit
        if toolkit is not None:
            return toolkit
        toolkit = SkillRetrievalToolkit(
            # Match SkillUseRail exactly: selected MCP-bundled Skills are part
            # of this session's directory, while unselected MCPs stay hidden.
            skill_directories=self._skill_scan_dirs,
            # Shared taxonomy generations are built only from JiuwenSwarm's
            # stable installed inventory. Session-selected MCP Skills remain
            # visible through the live provider above and appear in a stale
            # taxonomy under /newly_installed_skills.
            index_skill_directories=lambda: [str(get_agent_skills_dir())],
            disabled_skills=self._live_skill_retrieval_disabled_skills,
            source_by_name=lambda: (
                skill_sources_from_manager(self._skill_manager)
                if self._skill_manager is not None
                else {}
            ),
            session_scope=self._skill_retrieval_session_scope(),
            config_base=self._config_base_cache,
            settings=getattr(self, "_skill_retrieval_settings", None),
            auto_build_index=True,
            frozen_profile=getattr(self, "_restored_skill_retrieval_profile", None),
        )
        self._skill_retrieval_toolkit = toolkit
        self._skill_retrieval_environment = toolkit.environment
        self._persist_skill_retrieval_session_profile()
        return toolkit

    @staticmethod
    def _resolve_skill_retrieval_session_enabled(
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        return is_skill_retrieval_enabled(config_base)

    def _skill_retrieval_tools_enabled_for_runtime(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        """Return the session profile under the current global kill switch."""

        if self._skill_retrieval_session_enabled is None:
            self._skill_retrieval_session_enabled = (
                self._resolve_skill_retrieval_session_enabled(config_base)
            )
        return self._skill_retrieval_session_enabled and is_skill_retrieval_enabled(
            config_base
        )

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
            self._skill_retrieval_toolkit = None
            self._skill_retrieval_environment = None
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

    @staticmethod
    def _skill_retrieval_build_profile(
        config_base: dict[str, Any] | None,
    ) -> tuple[bool, Path]:
        """Resolve the effective build permission and root before a reload."""

        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            is_skill_retrieval_index_enabled,
            skill_retrieval_artifact_root,
        )

        allowed = is_skill_retrieval_enabled(
            config_base
        ) and is_skill_retrieval_index_enabled(config_base)
        return allowed, skill_retrieval_artifact_root(config_base)

    async def _cancel_skill_retrieval_build_after_reload(
        self,
        previous_profile: tuple[bool, Path],
        config_base: dict[str, Any],
    ) -> None:
        """Stop an old-root taxonomy build when its permission is removed."""

        previous_allowed, previous_root = previous_profile
        current_allowed, current_root = self._skill_retrieval_build_profile(config_base)
        if not previous_allowed or (
            current_allowed and previous_root == current_root
        ):
            return
        try:
            from jiuwenswarm.symphony.skill_retrieval import SkillTaxonomyRuntime

            await asyncio.to_thread(
                SkillTaxonomyRuntime(
                    records_provider=lambda: (),
                    index_root=previous_root,
                    config_base=config_base,
                ).cancel
            )
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] unable to cancel the disabled Skill "
                "taxonomy build at %s",
                previous_root,
                exc_info=True,
            )

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

        _, self._video_gen_tool_registered = self._sync_tool_group(
            current_tools=mark_stateless([generate_video]),
            registered=self._video_gen_tool_registered,
            enabled=bool(self._video_gen_model_config),
            create_fn=lambda: mark_stateless([generate_video]),
            warn_label="generate_video tool",
        )

    def _sync_paid_search_tool_for_runtime(self) -> None:
        """Sync paid-search tool registration after config reload."""
        # The owner id, not ``card.id``; see ``_sync_multimodal_tools_for_runtime``.
        agent_id = self._tool_owner_id()
        tools, self._paid_search_registered = self._sync_tool_group(
            current_tools=[self._paid_search_tool] if self._paid_search_tool else [],
            registered=self._paid_search_registered,
            enabled=is_paid_search_enabled(),
            create_fn=lambda: [
                WebPaidSearchTool(language=self._resolve_runtime_language(), agent_id=agent_id)
            ],
            warn_label="paid search tool",
        )
        self._paid_search_tool = tools[0] if tools else None
        if self._paid_search_tool is not None:
            self._prioritize_paid_search_tool_card()

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

    @staticmethod
    async def set_checkpoint() -> None:
        await ensure_persistent_checkpointer()

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

    def _register_model_cache_entry(
        self,
        entry: dict[str, Any],
        name_counter: dict[str, int],
    ) -> str | None:
        """Register one model entry into the request-selectable model cache.

        Returns the cache_key (``{model_name}#{per_name_idx}``) on success, or
        ``None`` when the entry is skipped (missing model_name or build failure).
        The caller pairs this return value with the entry's global list index
        to populate ``_global_index_to_cache_key``.
        """
        mcc = entry.get("model_client_config") or {}
        if not mcc.get("model_name"):
            return None
        model_name = mcc["model_name"]
        idx = name_counter.get(model_name, 0)
        name_counter[model_name] = idx + 1
        cache_key = f"{model_name}#{idx}"
        try:
            model = build_model_from_entry(
                mcc,
                entry.get("model_config_obj") or {},
            )
            self._model_cache[cache_key] = model
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] 跳过无效模型条目 %s: %s",
                model_name, exc,
            )
            return None
        if model_name not in self._model_name_to_keys:
            self._model_name_to_keys[model_name] = []
        self._model_name_to_keys[model_name].append(cache_key)

        # 同时用纯 model_name 作为 key 指向 is_default=true 的条目
        if entry.get("is_default") is True:
            self._model_cache[model_name] = self._model_cache[cache_key]

        alias = entry.get("alias") or ""
        if alias and alias != model_name and alias not in self._model_cache:
            self._model_cache[alias] = self._model_cache[cache_key]

        return cache_key

    def _build_model_cache_from_defaults(self, config: dict) -> None:
        """从 models.defaults 列表构建模型缓存。

        key 使用 {model_name}#{index} 格式以支持同名模型共存。
        同时记录 _model_name_to_keys 映射以便按 model_name 查找，
        以及 _global_index_to_cache_key 映射以便在 _resolve_model_by_name
        回退分支把通道侧的全局 origin_index 换算成真实 cache_key。
        """
        self._model_name_to_keys.clear()
        self._global_index_to_cache_key.clear()
        name_counter: dict[str, int] = {}

        for global_idx, entry in enumerate(get_default_models(config)):
            cache_key = self._register_model_cache_entry(entry, name_counter)
            if cache_key is not None:
                self._global_index_to_cache_key[global_idx] = cache_key

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
            self._model_cache[model_name] = build_model_from_entry(mcc, mco)
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
        self._global_index_to_cache_key.clear()
        self._inject_attribution_to_config(config)
        self._build_model_cache_from_defaults(config)
        if not self._model_cache:
            self._build_model_cache_legacy(config)

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

        # 首次启动兜底：config 默认条目仍为 .env 占位符时，改选 Zen 免费模型
        # （如 DeepSeek V4 Flash）作为默认，避免把占位模型发往厂商。
        # 仅内存态生效，不回写 config.yaml；Zen 不可达时保持原占位行为。
        if is_placeholder_model_entry(
            model_client_config_view(self._model_cache[default_name].model_client_config)
        ):
            from jiuwenswarm.server.runtime.opencode_zen import get_zen_default_free_model_entry
            zen_default = get_zen_default_free_model_entry()
            if zen_default is not None:
                zmcc = zen_default["model_client_config"]
                zname = zmcc["model_name"]
                self._model_cache[zname] = build_model_from_entry(
                    zmcc, zen_default.get("model_config_obj") or {}
                )
                default_name = zname

        self._default_model_name = default_name
        self._model = self._model_cache[default_name]
        self._model_client_config = self._model.model_client_config
        self._model_request_config = self._model.model_config
        self._selected_model_context_window_tokens = getattr(
            self._model_request_config,
            "context_window",
            None,
        )
        self._last_models_config_fingerprint = new_fp
        return self._model

    @staticmethod
    def _models_config_fingerprint(config: dict) -> str:
        """计算模型配置段的指纹，用于判断是否需要重建模型缓存。"""
        models = config.get("models", {})
        react = config.get("react") or {}
        payload = json.dumps(
            {
                "models": models,
                "react": {
                    "model_client_config": react.get("model_client_config"),
                    "model_name": react.get("model_name"),
                    "model_config_obj": react.get("model_config_obj"),
                },
            },
            sort_keys=True, default=str, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _session_stored_model_name(session_id: str | None) -> str:
        """Read the model last persisted on this session, if any."""
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            metadata = get_session_metadata(
                sid,
                cache_bust=True,
                enable_writeback=False,
            )
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] failed to load session model for %s",
                sid,
                exc_info=True,
            )
            return ""
        stored = metadata.get("model") if isinstance(metadata, dict) else None
        return stored.strip() if isinstance(stored, str) else ""

    def _resolve_model_by_name(self, requested_model_name: str = "") -> Model | None:
        """Resolve the exact model object that will be used.

        Accepts three request formats from channels (web/TUI/etc.):

        1. Pure ``model_name`` — returns the ``is_default=true`` entry registered
           under the bare name, if any.
        2. ``{model_name}#{index}`` whose ``#index`` is the channel-supplied
           global list position (e.g. Web's ``origin_index`` from models.list).
           It is converted to the real per-name cache_key via
           ``_global_index_to_cache_key``; if no entry is registered at that
           global position (out of range) or the mapped key has since been
           evicted, a warning is logged and the default model is returned
           rather than silently falling back to the first same-name entry
           (which would mis-resolve an agentos same-name entry to the defaults
           entry and send the wrong api_base/api_key).

        Note: the ``#index`` is *always* treated as a global position. An
        earlier version first tried ``requested in self._model_cache`` (whose
        keys use the per-name ``name_counter`` scheme
        ``{model_name}#{per_name_idx}``); when a channel-supplied global index
        happened to collide with a per-name index of a *different* same-name
        entry, that lookup silently returned the wrong entry. Routing every
        ``#``-bearing request through the global map removes that collision
        while keeping the pure-name path (format 1) intact.
        """
        requested = (requested_model_name or "").strip()
        if not requested:
            # ask_user_interrupt 等中断恢复请求不带 model_name，
            # 回退到 session 上次应用的模型，而非 config.yaml 默认占位模型。
            return getattr(self, "_last_resolved_model", None) or self._model
        # 含 # 的请求一律走全局 origin_index 换算（见上文 Note），避免 per-name
        # cache_key 与通道侧全局 index 碰撞时误命中另一同名条目。
        if "#" not in requested:
            # 纯 model_name 精确命中（仅 is_default=true 的条目注册了纯名 key）
            if requested in self._model_cache:
                return self._model_cache[requested]
            # 纯 model_name 查找（_model_name_to_keys 的 key 是纯名）
            keys = self._model_name_to_keys.get(requested)
            if keys:
                resolved = self._model_cache.get(keys[0])
                if resolved is not None:
                    return resolved
        # 通道侧使用全局 origin_index；将其换算成后端 per-name cache key。
        if "#" in requested:
            bare_name, _, index_part = requested.rpartition("#")
            if not bare_name:
                return self._model
            try:
                global_idx = int(index_part)
            except ValueError:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] model resolve: requested %r has "
                    "non-integer index, falling back to default model", requested,
                )
                return self._model
            cache_key = self._global_index_to_cache_key.get(global_idx)
            if cache_key is None or cache_key not in self._model_cache:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] model resolve: global index %d "
                    "for %r not in cache map (mapped_key=%s), falling back to "
                    "default model", global_idx, requested, cache_key,
                )
                return self._model
            return self._model_cache[cache_key]

        # Opencode Zen 免费模型（纯内存态，不入 config.yaml）：从进程内存缓存
        # 取完整 model_client_config / model_config_obj 构建并缓存，供本次及后续请求复用。
        # 不写回 config，开关关闭（get_zen_free_model_entries 返回空）则自然查不到。
        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )
            for zent in get_zen_free_model_entries():
                zmcc = zent.get("model_client_config") or {}
                if (zmcc.get("model_name") or "") == requested:
                    built = build_model_from_entry(
                        zmcc, zent.get("model_config_obj") or {}
                    )
                    self._model_cache[requested] = built
                    return built
        except Exception:
            logger.debug(
                "[JiuWenSwarmDeepAdapter] resolve zen free model %s failed",
                requested, exc_info=True,
            )
        # 显式请求的模型全部未命中（不在 config / model_cache / Zen 免费缓存）：
        # 打 warning 暴露配置漂移（如 cron job 引用已下线模型、两进程缓存分歧），
        # 不再静默回退默认模型——cron 无人值守场景，静默用错模型难以及时发现。
        fallback_name = str(
            getattr(getattr(self._model, "model_config", None), "model_name", "") or ""
        )
        logger.warning(
            "[JiuWenSwarmDeepAdapter] requested model %r not found in "
            "configured models or zen free-model cache; falling back to "
            "default model %r",
            requested,
            fallback_name or type(self._model).__name__,
        )
        return self._model

    def _requested_model_name(self, request: AgentRequest) -> str:
        """Resolve the model name this request should use.

        Explicit ``model_name`` wins. Otherwise, if this adapter has not yet
        applied a model, fall back to the session's persisted ``model`` so
        ``command.goal`` / interrupt resume can still hit the user's choice.
        """
        params = request.params if isinstance(request.params, dict) else {}
        requested = str(params.get("model_name") or "").strip()
        if requested:
            return requested
        if getattr(self, "_last_resolved_model", None) is not None:
            return ""
        return self._session_stored_model_name(getattr(request, "session_id", None))

    def _resolve_model_for_request(self, request: AgentRequest) -> Model:
        """根据请求中的 model_name 参数查找对应模型（支持别名），未匹配则回退默认模型。

        支持两种格式：
        - 纯 model_name：查找 is_default=true 的条目
        - {model_name}#{index}：查找指定索引的条目

        请求未显式携带 model_name 时：最近一次已应用模型 → 会话 metadata.model
        → 适配器默认模型。避免 command.goal / 中断恢复在适配器重建后掉回默认。
        """
        requested = self._requested_model_name(request)
        if not requested:
            last = getattr(self, "_last_resolved_model", None)
            if last is not None:
                return last
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
        vision_tool_available: bool,
    ) -> dict[str, Any]:
        """Expose image paths when native image input is unavailable."""
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
                (
                    "当前主模型不支持原生图片输入。请调用已配置的图片理解工具；"
                    "优先使用 image_reading(local_url=mediaPath, prompt=question)，"
                    "或使用 visual_question_answering(image_path_or_url=mediaPath, question=question)。"
                )
                if vision_tool_available
                else (
                    "当前主模型不支持原生图片输入，且没有配置可用的视觉模型工具。"
                    "请直接向用户说明当前没有图片理解能力，不要猜测图片内容。"
                )
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
        vision_tool_available: bool,
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
        if vision_tool_available:
            content = f"当前模型{model_label}不支持原生图片理解，已切换为图片理解工具处理。"
        else:
            content = (
                f"当前模型{model_label}不支持原生图片理解，"
                "且未配置可用的视觉模型工具。"
            )
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

    @staticmethod
    def _native_image_input_enabled(config: dict[str, Any], model: Any | None) -> bool:
        configured = config.get("enable_read_image_multimodal")
        if isinstance(configured, bool):
            return configured
        return get_cached_image_support(model) is True

    @staticmethod
    def _resolve_enable_read_image_multimodal(
        config: dict[str, Any],
    ) -> bool | None:
        configured = config.get("enable_read_image_multimodal")
        if isinstance(configured, bool):
            return configured
        return None

    def _apply_model_to_react_agent(
        self,
        model: Model,
        *,
        session_id: str | None = None,
    ) -> None:
        """将指定模型应用到 react_agent 实例（替换 _llm 和 _config 字段）。

        react_agent._railed_model_call 使用 self._config.model_name 作为 model= 参数，
        因此需要同时替换 _llm 和 _config 中的模型相关字段。

        会话适配器在首个 chat 请求到来前就已创建，此时它只能使用配置默认
        模型装配 DeepAgent 和其 subagent specs。若仅替换主 ReActAgent，后续
        ``create_subagent`` 会继续从旧 spec 取模型，导致 cron/chat 选中的模型
        没有传递到子智能体。这里同步更新“继承父模型”的 specs；显式配置为
        其他模型的自定义 subagent 不会被覆盖。
        """
        react_agent = getattr(self._instance, "_react_agent", None)
        if react_agent is None:
            return

        deep_config = getattr(self._instance, "_deep_config", None)
        previous_model = getattr(deep_config, "model", None)
        if deep_config is not None:
            deep_config.model = model
            inherited_subagents = 0
            for spec in list(getattr(deep_config, "subagents", None) or []):
                spec_model = getattr(spec, "model", None)
                # Built-in subagents are assembled with the parent's model.
                # A None model also means inheritance in DeepAgent.create_subagent.
                if spec_model is None or spec_model is previous_model:
                    try:
                        spec.model = model
                        inherited_subagents += 1
                    except (AttributeError, TypeError):
                        # A pre-built DeepAgent or an immutable third-party spec
                        # owns its own model and must retain that configuration.
                        continue
            if inherited_subagents:
                logger.info(
                    "[JiuWenSwarmDeepAdapter] synchronized %d inherited subagent "
                    "model(s) with active request model %s",
                    inherited_subagents,
                    getattr(getattr(model, "model_config", None), "model_name", ""),
                )
        if callable(getattr(react_agent, "set_llm", None)):
            react_agent.set_llm(model)
        config = getattr(react_agent, "_config", None)
        if config is not None:
            config.model_name = model.model_config.model_name
            config.model_client_config = model.model_client_config
            config.model_config_obj = model.model_config

            # ``ContextEngine`` keeps contexts by session/context ID.  Update
            # its global config for future contexts and rebind only the active
            # request's cached context so message history is retained while
            # tokenizer/window/usage state follows the selected model.
            try:
                context_model_state = _ContextEngineModelState(
                    full_config=self._config_base_cache,
                    model_name=model.model_config.model_name,
                    model=model,
                    config_base=self._config_base_cache,
                )
                context_config = _deep_agent_context_engine_config_for_model(
                    self._config_cache,
                    model_state=context_model_state,
                )
                config.context_engine_config = context_config
                context_engine = getattr(react_agent, "context_engine", None)
                rebind_context_model = getattr(context_engine, "rebind_context_model", None)
                rebound = 0
                if callable(rebind_context_model):
                    rebound = rebind_context_model(
                        context_config,
                        session_id=session_id,
                    )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] synchronized context model=%s provider=%s "
                    "session_id=%s rebound_contexts=%s",
                    context_config.model_name,
                    context_config.model_provider,
                    session_id or "<all>",
                    rebound,
                )
            except (
                AttributeError,
                ImportError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] failed to synchronize context model binding "
                    "for model=%s; keeping the provider model switch: %s",
                    getattr(model.model_config, "model_name", ""),
                    exc,
                    exc_info=True,
                )
        if deep_config is not None and config is not None:
            try:
                deep_config.context_engine_config = config.context_engine_config
            except (AttributeError, TypeError) as exc:
                logger.debug(
                    "[JiuWenSwarmDeepAdapter] deep config context binding skipped: %s",
                    exc,
                )
        # TaskCompletionRail 的 transcript assessor 读的是 deep_config.model，
        # 只换 react_agent 会让每轮目标评估仍打到构建时的默认模型。
        deep_config = getattr(self._instance, "deep_config", None)
        if deep_config is not None:
            deep_config.model = model
        self._selected_model_context_window_tokens = getattr(
            model.model_config,
            "context_window",
            None,
        )
        update_model_context = getattr(self._instance, "update_model_context", None)
        if callable(update_model_context):
            update_model_context(
                model_name=model.model_config.model_name,
                context_window_tokens=self._selected_model_context_window_tokens,
            )
        self._model_request_config = model.model_config
        # 记录最近一次解析并应用的模型，供 ask_user_interrupt 等不带 model_name
        # 的中断恢复请求回退使用，避免回退到 config.yaml 默认占位模型。
        self._last_resolved_model = model
        self._active_request_model = model

    @staticmethod
    def _resolve_skill_mode(
        config: dict[str, Any],
        config_base: dict[str, Any] | None = None,
        retrieval_enabled: bool | None = None,
    ) -> str:
        """Validate configured skill mode and fallback safely on invalid values."""
        if (
            is_skill_retrieval_enabled(config_base)
            if retrieval_enabled is None
            else retrieval_enabled
        ):
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
        new_policy, upload_list = build_filesystem_policy(
            runtime.get("files") or {},
            project_dir=self._resolve_project_dir_for_sandbox(),
            is_code_agent=self._is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
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

    async def _unload_active_packages(
        self, config_paths: list[str] | None = None
    ) -> None:
        """Unload every active harness package. Idempotent —
        ``unload_harness_config`` no-ops when the ledger has no record, so
        safe to run before every re-bind cycle.
        """
        if self._instance is None:
            return
        if config_paths is None:
            config_paths = self._get_active_package_config_paths()
        if not config_paths:
            return
        for config_path in config_paths:
            try:
                await self._instance.unload_harness_config(config_path)
            except Exception as exc:
                logger.error(
                    "[JiuWenSwarmDeepAdapter] Failed to unload active package %s: %s: %r",
                    config_path,
                    exc.__class__.__name__,
                    exc,
                )

    async def _load_active_packages(self) -> list[str]:
        """Restore active harness packages from harness-packages.json.

        Idempotent: unloads first so re-binding onto an agent that already
        carries those tool names does not trip openjiuwen's "already bound"
        raise (which rolls back the whole batch). Called at create_instance
        and after configure() reconciles harness tools out of config.tools.
        """
        if self._instance is None:
            return []

        config_paths = self._get_active_package_config_paths()
        if not config_paths:
            return []

        # Unload first (same path list) so re-bind does not trip "already bound".
        await self._unload_active_packages(config_paths)

        loaded: list[str] = []
        for config_path in config_paths:
            # Skip packages failing the hot-load guards.
            try:
                validate_harness_config(Path(config_path))
            except ValueError as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] Skipping active package %s: %s",
                    config_path,
                    exc,
                )
                continue
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
                logger.error(
                    "[JiuWenSwarmDeepAdapter] Failed to load active package %s: %s: %r",
                    config_path,
                    exc.__class__.__name__,
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
            # Never activate a package failing the hot-load guards.
            # load_harness_config would spawn a declared subprocess (mcps) or
            # exec an out-of-package .py (escaped path). The import-time
            # guard blocks new imports; this catches packages that slipped
            # through before it existed or were placed on disk directly.
            validate_harness_config(Path(config_path))
            return await self._instance.load_harness_config(config_path)
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] apply_package_change(%s) failed on %s: %s: %r",
                operation,
                config_path,
                exc.__class__.__name__,
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
        self, config: dict[str, Any], include_tools: bool = False
    ) -> SkillUseRail | None:
        """Build SkillUseRail.

        skills_dir seeds with the agent's main skills dir PLUS the connected
        MCPs' skill dirs (derived from state.json), so a freshly started agent
        sees connected MCP skills immediately.
        """
        try:
            skill_mode = self._resolve_skill_mode(
                config,
                self._config_base_cache,
                retrieval_enabled=(
                    self._skill_retrieval_tools_enabled_for_runtime(
                        self._config_base_cache
                    )
                ),
            )
            logger.info("[JiuWenSwarmDeepAdapter] current skill_mode: %s", skill_mode)
            skills_dirs = self._skill_scan_dirs()
            skill_rail = SkillUseRail(
                skills_dir=skills_dirs,
                skill_mode=skill_mode,
                include_tools=include_tools,
                disabled_skills=self._skill_manager.list_execution_disabled_skills(),
            )
            logger.info("[JiuWenSwarmDeepAdapter] SkillUseRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillUseRail create failed: %s", exc)
            skill_rail = None
        return skill_rail

    def _skill_scan_dirs(self) -> list[str]:
        """Skill scan roots: agent skills dir + this session's selected MCP
        skill dirs.

        Centralized so _build_skill_rail (init) and refresh_skill_rails
        (reload) compute the same roots. Only a session-scoped child scans MCP
        skill dirs, filtered to its ``_session_selected_mcp`` set (populated by
        reconcile_session_mcp from chat.send's ``mcp`` field). During a cold
        first chat, the construction-only pending set is included as well so
        bundled Skills precede retrieval's frozen snapshot without pretending
        the MCP is already registered. The root adapter never scans MCP dirs:
        cli/skill MCPs are session-level only, and the TUI channel does not
        support them, so the root's view of them must be empty (previously it
        scanned ALL connected MCP dirs, leaking cli/skill skills into rewind/
        cron/admin root runs).
        """
        roots = [str(get_agent_skills_dir())]
        if not getattr(self, "_is_session_scoped_adapter", False):
            return roots
        try:
            selected = set(getattr(self, "_session_selected_mcp", set()) or ())
            selected.update(
                getattr(self, "_pending_skill_scan_mcp_names", set()) or ()
            )
            for entry in self._skill_manager._mcp_skills_dirs():
                if str(entry.get("name", "")).strip() not in selected:
                    continue
                d = str(entry.get("dir", "") or "").strip()
                if d and d not in roots:
                    roots.append(d)
        except Exception:  # noqa: BLE001
            pass
        return roots

    def _build_skill_evolution_rail(self, config: dict[str, Any]) -> SkillEvolutionRail | None:
        """Build SkillEvolutionRail."""
        if not get_skill_evolution_enabled(config):
            return None
        from jiuwenswarm.agents.harness.observability_runtime import (
            get_trajectory_span_processor,
        )

        try:
            evolution_auto_save = get_evolution_auto_save_enabled(config)
            model_name = self._default_model_name or config.get("model_name", "gpt-4")
            skill_evolution_rail = SkillEvolutionRail(
                skills_dir=self._skill_scan_dirs(),
                llm=self._model,
                model=model_name,
                review_runtime=EvolutionReviewRuntime(),
                signal_trigger=False,
                auto_save=evolution_auto_save,
                review_trigger=True,
                disabled_skills=self._skill_manager.list_execution_disabled_skills(),
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
        evolution_auto_save = get_evolution_auto_save_enabled(
            self._config_base_cache or self._config_cache
        )
        if (
            self._skill_evolution_rail is not None
            and getattr(self._skill_evolution_rail, "_language", None) != resolved_language
        ):
            await self._unconfigure_active_evolution_rails()

        disabled_skills = (
            self._skill_manager.list_execution_disabled_skills()
            if self._skill_manager is not None
            else []
        )
        from jiuwenswarm.agents.harness.observability_runtime import (
            get_trajectory_span_processor,
        )

        await configure_skill_evolution_runtime(
            self._instance,
            skills_dir=str(get_agent_skills_dir()),
            llm=self._model,
            model=self._default_model_name
            or self._config_cache.get("model_name", "gpt-4"),
            auto_save=evolution_auto_save,
            signal_trigger=False,
            review_trigger=True,
            disabled_skills=disabled_skills,
            language=resolved_language,
            trajectory_span_processor=get_trajectory_span_processor(),
        )
        self._refresh_active_evolution_rail_refs()
        if self._skill_evolution_rail is not None:
            self._skill_evolution_rail.auto_save = evolution_auto_save

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
            # Lightweight test doubles and older host adapters may not expose
            # the canonical bulk-strip helper; explicit unregister below still
            # removes the cached rails from those instances.
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
        if not get_skill_evolution_enabled(self._config_base_cache or self._config_cache):
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
        Config: react.evolution.skill_evolution (bool) - true to register the
        rail with auto_trigger=true.
        """
        try:
            if not get_skill_evolution_enabled(config):
                logger.debug("[JiuWenSwarmDeepAdapter] SkillCreateRail disabled by config")
                return None

            from jiuwenswarm.agents.harness.observability_runtime import (
                get_trajectory_span_processor,
            )

            language = config.get("language", "cn")
            rail = SkillCreateRail(
                skills_dir=str(get_agent_skills_dir()),
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

    @staticmethod
    def _build_task_planning_rail() -> TaskPlanningRail | None:
        """Build TaskPlanningRail."""
        try:
            task_planning_rail = TaskPlanningRail(inject_prompt=False)
            logger.info("[JiuWenSwarmDeepAdapter] TaskPlanningRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] TaskPlanningRail create failed: %s", exc)
            task_planning_rail = None
        return task_planning_rail

    @staticmethod
    def _build_subagent_rail(
        config_base: dict[str, Any] | None = None,
    ) -> SubagentRail | None:
        """Build SubagentRail for subagent delegation."""
        try:
            runtime_enabled = is_subagent_runtime_enabled(config_base)
            subagent_rail = BrowserTaskPromptRail(
                enable_subagent_runtime=runtime_enabled,
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] SubagentRail create success "
                "(subagent_runtime=%s)",
                runtime_enabled,
            )
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
            config = get_config()
            embed_config = config.get("embed") if isinstance(config, dict) else None
            has_api_key = (
                embed_config.get("embed_api_key") if isinstance(embed_config, dict) else None
            )
            has_base_url = (
                embed_config.get("embed_base_url") if isinstance(embed_config, dict) else None
            )
            has_model = embed_config.get("embed_model") if isinstance(embed_config, dict) else None
            if not all([has_api_key, has_base_url, has_model]):
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MemoryRail create failed: No available embedding config"
                )
            self._is_proactive_memory = is_proactive_memory(mode, config)
            memory_rail = MemoryRail(
                embedding_config=EmbeddingConfig(
                    model_name=embed_config.get("embed_model"),
                    base_url=embed_config.get("embed_base_url"),
                    api_key=embed_config.get("embed_api_key"),
                ),
                is_proactive=self._is_proactive_memory,
            )
            logger.info("[JiuWenSwarmDeepAdapter] MemoryRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MemoryRail create failed: %s", exc)
            memory_rail = None
        return memory_rail

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
    def _build_memory_forbidden_rail() -> Any | None:
        """Build the execution-time sensitive-memory write guard."""
        try:
            from jiuwenswarm.agents.harness.common.rails.memory_forbidden_rail import (
                MemoryForbiddenRail,
            )

            rail = MemoryForbiddenRail()
            logger.info("[JiuWenSwarmDeepAdapter] MemoryForbiddenRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MemoryForbiddenRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_model_anomaly_detection_rail(
        config_base: dict[str, Any] | None = None,
    ) -> ModelAnomalyDetectionRail | None:
        try:
            config_base = config_base or get_config()
            guard_cfg = config_base.get("execution_guard", {}) if isinstance(config_base, dict) else {}
            retry_cfg = (
                guard_cfg.get("model_anomaly_detection_rail", {})
                if isinstance(guard_cfg, dict)
                else {}
            )
            if retry_cfg.get("enabled", False) is not True:
                logger.info("[JiuWenSwarmDeepAdapter] ModelAnomalyDetectionRail disabled by config")
                return None
            rail = ModelAnomalyDetectionRail(
                max_retries=retry_cfg.get("max_retries", 2),
                repeat_min_pattern_chars=retry_cfg.get("repeat_min_pattern_chars", 2),
                repeat_max_pattern_chars=retry_cfg.get("repeat_max_pattern_chars", 64),
                repeat_min_count=retry_cfg.get("repeat_min_count", 6),
                repeat_min_total_chars=retry_cfg.get("repeat_min_total_chars", 160),
                repeat_window_chars=retry_cfg.get("repeat_window_chars", 1024),
                single_char_repeat_count=retry_cfg.get("single_char_repeat_count", 100),
                tool_loop_compact=retry_cfg.get("tool_loop_compact"),
            )
            logger.info("[JiuWenSwarmDeepAdapter] ModelAnomalyDetectionRail create success")
            return rail
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ModelAnomalyDetectionRail create failed: %s",
                exc,
            )
            return None

    def _build_circuit_breaker_rail(self) -> CircuitBreakerRail | None:
        try:
            guard_cfg = (get_config() or {}).get("execution_guard") or {}
            cb_cfg = guard_cfg.get("circuit_breaker") or {}
            if cb_cfg.get("enabled", False) is not True:
                logger.info("[JiuWenSwarmDeepAdapter] CircuitBreakerRail disabled by config")
                return None
            defaults = CircuitBreakerConfig()
            config = CircuitBreakerConfig(
                warning_threshold=cb_cfg.get("warning_threshold", defaults.warning_threshold),
                critical_threshold=cb_cfg.get("critical_threshold", defaults.critical_threshold),
                global_breaker_threshold=cb_cfg.get(
                    "global_breaker_threshold", defaults.global_breaker_threshold
                ),
                unknown_tool_threshold=cb_cfg.get(
                    "unknown_tool_threshold", defaults.unknown_tool_threshold
                ),
            )
            rail = CircuitBreakerRail(config, language=self._resolve_runtime_language())
            logger.info("[JiuWenSwarmDeepAdapter] CircuitBreakerRail create success")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] CircuitBreakerRail create failed: %s", exc)
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
            )
            logger.info("[JiuWenSwarmDeepAdapter] RuntimePromptRail create success")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] RuntimePromptRail create failed: %s", exc)
            rail = None
        return rail

    @staticmethod
    def _build_eternal_conversation_rail() -> EternalConversationRail | None:
        """Mount an inert Rail; a Session request flag activates it later."""
        try:
            rail = EternalConversationRail()
            logger.info("[JiuWenSwarmDeepAdapter] EternalConversationRail created (disabled)")
            return rail
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] EternalConversationRail create failed: %s", exc)
            return None

    @staticmethod
    def shutdown_context_session_memory(context_processor_rail: ContextProcessorRail) -> bool:
        """Stop overlapping semantic session memory through a stable Adapter API."""
        session_memory_manager = getattr(context_processor_rail, "_session_memory_mgr", None)
        if session_memory_manager is None:
            return False
        session_memory_manager.shutdown()
        setattr(context_processor_rail, "_session_memory_mgr", None)
        return True

    def _build_skill_retrieval_prompt_rail(self) -> SkillRetrievalPromptRail | None:
        """Build Skill directory guidance and per-session reminders."""
        if not self._skill_retrieval_tools_enabled_for_runtime(
            self._config_base_cache
        ):
            return None
        try:
            toolkit = self._get_or_create_skill_retrieval_toolkit()
            return SkillRetrievalPromptRail(
                toolkit=toolkit,
                session_scope=self._skill_retrieval_session_scope(),
                config_base=self._config_base_cache,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] SkillRetrievalPromptRail create failed: %s", exc)
            return None

    async def refresh_skill_rails(self) -> None:
        """轻量刷新 skill 相关 rail，避免全量重建 agent 实例.

        reload_skills() 重新扫描 skills_dir，增量移除已删除 skill 的缓存；
        同步更新 disabled_skills。MCP bundled skills 位于
        <workspace>/mcp/skills/<name>/（agent 主 skills_dir 之外），reload
        前把已连接 MCP 的 skill 目录并入 scan roots，让新连接 MCP 的 skills
        对 agent 可见。
        """
        # disabled skills: use list_disabled_skills (NOT list_execution_disabled_skills)
        # — the latter filters to "registered" (installed_plugins/local_skills) and
        # excludes MCP bundled skills, so a disabled MCP's skills would stay
        # visible. list_disabled_skills returns ALL skill_configs entries with
        # enabled=False, including MCP skills. Fall back to
        # list_execution_disabled_skills for older SkillManager variants / test
        # mocks that only implement the latter.
        sm = self._skill_manager
        # reload_state first: MCP disable/enable writes skill_configs via a
        # different SkillManager instance, so this adapter's _skill_manager._state
        # is stale. Reload from disk so list_disabled_skills sees the just-toggled
        # MCP skills.
        _reload = getattr(sm, "reload_state", None)
        if _reload is not None:
            try:
                _reload()
            except Exception:  # noqa: BLE001
                pass
        _list_disabled = getattr(sm, "list_disabled_skills", None)
        if _list_disabled is None:
            _list_disabled = getattr(sm, "list_execution_disabled_skills", lambda: [])
        new_disabled = set(_list_disabled())
        # Rebuild the rail's scan roots from scratch (agent skills dir +
        # currently-connected MCP skill dirs). Previously this preserved the
        # rail's existing roots and only appended new MCP dirs, so a
        # disconnected MCP's skill dir stayed in the scan list and its skills
        # remained visible to the agent. Rebuilding via _skill_scan_dirs makes
        # the roots reflect the live connection state — disconnected MCPs drop
        # out — and stays in sync with _build_skill_rail's init-time roots.
        skills_dirs = self._skill_scan_dirs()
        # Keep hot-bound plugin/template skill roots. _skill_scan_dirs() only
        # knows workspace + session MCP; package dirs live on deep_config.skills
        # after _bind_skill. Prepend them so MCP refresh does not drop them and
        # package skills retain precedence over same-named workspace skills.
        instance = getattr(self, "_instance", None)
        bound = getattr(getattr(instance, "deep_config", None), "skills", None)
        if bound:
            seen = {str(Path(item).expanduser().resolve()) for item in skills_dirs if item}
            extra: list[str] = []
            for raw in [bound] if isinstance(bound, str) else list(bound):
                text = str(raw or "").strip()
                if not text:
                    continue
                resolved = str(Path(text).expanduser().resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                extra.append(text)
            skills_dirs = [*extra, *skills_dirs]
        if self._skill_rail is not None:
            # Update the rail's scan roots before reload so it picks up newly
            # connected (and drops disconnected) MCP skill dirs.
            try:
                self._skill_rail.skills_dir = skills_dirs
            except (AttributeError, TypeError):
                pass
            if self._skill_rail.disabled_skills != new_disabled:
                self._skill_rail.disabled_skills = new_disabled
            try:
                await self._skill_rail.reload_skills()
            except Exception as exc:
                logger.warning("[JiuWenSwarmDeepAdapter] skill rail reload failed: %s", exc)
            # Clear the session's persisted skill baseline (skill_use state).
            # SkillUseRail snapshots self.skills into the session at first use
            # (_ensure_session_baseline) and get_skills_for_session merges that
            # baseline back — so a skill disabled mid-session stays reachable
            # via skill_tool (it reads from the baseline). Dropping the
            # baseline forces the next skill_tool / prompt build to re-snapshot
            # from the now-filtered self.skills, so disabled skills disappear.
            self._clear_skill_session_baseline()
        if self._skill_evolution_rail is not None:
            try:
                self._skill_evolution_rail.skills_dir = skills_dirs
            except (AttributeError, TypeError):
                pass
            if getattr(self._skill_evolution_rail, "disabled_skills", None) != new_disabled:
                try:
                    self._skill_evolution_rail.disabled_skills = new_disabled
                except (AttributeError, TypeError):
                    pass
        # Propagate to live session child adapters — each has its own
        # SkillUseRail that caches the skill set. An MCP's bundled skills were
        # installed via skill_installer (writes skill_state.json + copies dirs),
        # but session child rails won't see them until reloaded.
        # getattr guards bare __new__-constructed adapters (tests) that skipped
        # __init__ and so lack _is_session_scoped_adapter / _session_adapters.
        if not getattr(self, "_is_session_scoped_adapter", False) and getattr(self, "_session_adapters", {}):
            for _sid, child in list(self._session_adapters.items()):
                if child is self:
                    continue
                try:
                    await child.refresh_skill_rails()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[JiuWenSwarmDeepAdapter] session skill rail reload %s failed: %s",
                        _sid, exc,
                    )

    def _clear_skill_session_baseline(self) -> None:
        """Drop this adapter's session skill baseline so disabled skills vanish.

        SkillUseRail snapshots ``self.skills`` into the session on first use
        (``_ensure_session_baseline``) and ``get_skills_for_session`` merges
        that baseline back into the result ``skill_tool`` reads. A skill
        disabled mid-session therefore stays reachable via ``skill_tool``
        because the stale baseline still lists it. Clearing the baseline
        forces the next ``get_skills_for_session`` to re-snapshot from the
        already-filtered ``self.skills`` (disabled excluded), so the
        disabled skill disappears from ``skill_tool`` output too.
        """
        try:
            instance = getattr(self, "_instance", None)
            loop_session = getattr(instance, "_loop_session", None)
            if loop_session is None:
                return
            # _SESSION_STATE_KEY = "skill_use" (openjiuwen SkillUseRail)
            loop_session.update_state({"skill_use": None})
        except Exception:  # noqa: BLE001
            pass

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

            rails_list.append(ObservabilityRail())
        except Exception as exc:
            logger.warning("%s Failed to attach ObservabilityRail: %s", log_prefix, exc)
        stage_timer.mark("observability_rail")

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

    def _build_work_agent_mode_rail(self) -> Any | None:
        """构建 work profile 的 plan rail（只读白名单 + 通用工作计划提示词）。

        与 code 侧的区别只在构造参数：work 不引用 plan_agent，
        白名单只放调研与计划文件写入，其余会产生业务副作用的工具在 plan 期间
        一律被拦截。
        """
        try:
            from jiuwenswarm.agents.harness.work.rails.work_agent_mode_rail import (
                WorkAgentModeRail,
            )

            return WorkAgentModeRail(language=self._resolve_runtime_language())
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] WorkAgentModeRail create failed: %s", exc)
            return None

    @staticmethod
    def _build_work_plan_approval_rail() -> Any | None:
        """构建 work plan 的审批 rail（``exit_plan_mode`` 即时弹窗）。"""
        try:
            from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
                PlanApprovalInterruptRail,
            )

            return PlanApprovalInterruptRail()
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] PlanApprovalInterruptRail create failed: %s", exc
            )
            return None

    def _build_agent_rails(
        self,
        config: dict[str, Any],
        config_base: dict[str, Any],
        *,
        mode: str = "agent",
    ) -> list[Any]:
        """Build DeepAgent rails consistently for cold start and hot reload."""
        rail_infos = [
            _RailBuildInfo("_runtime_prompt_rail", self._build_runtime_prompt_rail),
            _RailBuildInfo("_response_prompt_rail", self._build_response_prompt_rail),
            _RailBuildInfo(
                "_multimodal_image_rail",
                self._build_multimodal_image_rail,
                {
                    "enable_image_multimodal": self._resolve_enable_read_image_multimodal(config),
                },
            ),
            _RailBuildInfo("_stream_event_rail", self._build_stream_event_rail),
            _RailBuildInfo("_task_planning_rail", self._build_task_planning_rail),
            _RailBuildInfo("_security_rail", self._build_security_rail),
            _RailBuildInfo(
                "_model_anomaly_detection_rail",
                self._build_model_anomaly_detection_rail,
                {"config_base": config_base},
            ),
            _RailBuildInfo("_heartbeat_rail", self._build_heartbeat_rail),
            _RailBuildInfo("_circuit_breaker_rail", self._build_circuit_breaker_rail),
            _RailBuildInfo("_avatar_rail", self._build_avatar_rail),
            _RailBuildInfo("_memory_forbidden_rail", self._build_memory_forbidden_rail),
            _RailBuildInfo(
                "_subagent_rail",
                self._build_subagent_rail,
                {"config_base": config_base},
            ),
            _RailBuildInfo(
                "_permission_rail",
                build_permission_rail,
                {
                    "config": config_base,
                    "llm": self._model,
                    "model_name": config_base.get("models", {})
                    .get("default", {})
                    .get("model_client_config", {})
                    .get("model_name", "gpt-4"),
                },
            ),
            _RailBuildInfo(
                "_context_processor_rail",
                _build_context_processor_rail,
                {"config": self._config_cache},
            ),
            _RailBuildInfo(
                "_eternal_conversation_rail", self._build_eternal_conversation_rail
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
                "_symphony_orchestration_rail",
                self._build_symphony_orchestration_rail,
            ),
        )
        if isinstance(mode, str) and mode.startswith("agent"):
            rail_infos.append(_RailBuildInfo("_ask_user_rail", self._build_structured_ask_user_rail))

            # work 单 agent 常挂 plan rails，与 code 侧一致：plan 是会话运行期状态，
            # 不是另一种 agent 装配，所以不能按 sub_mode 决定挂不挂——否则开关 Plan
            # 就得换 agent 实例，内存里的对话上下文会跟着一起丢。
            #
            # 非 plan 态它们几乎不做事：``AgentModeRail.before_model_call`` 会把
            # enter/exit_plan_mode 从模型可见工具里滤掉（``WorkAgentModeRail`` 再补上
            # switch_mode），审批 rail 只拦 ``exit_plan_mode``，普通模式下不会触发。
            rail_infos.append(
                _RailBuildInfo("_work_agent_mode_rail", self._build_work_agent_mode_rail)
            )
            rail_infos.append(
                _RailBuildInfo("_work_plan_approval_rail", self._build_work_plan_approval_rail)
            )

        return self._instantiate_rails(rail_infos, config_base)

    @staticmethod
    def _resolve_enable_task_loop(
        config: dict[str, Any], config_base: dict[str, Any] | None
    ) -> bool:
        """Resolve enable_task_loop considering evolution rail requirements.

        Evolution follow-ups require task-loop mode (enable_task_loop=True) because
        they use AFTER_TASK_ITERATION events and enqueue_follow_up(). When
        skill_evolution=True, force enable_task_loop=True regardless of user config.

        Args:
            config: The react config section.
            config_base: The full config base (contains react.evolution.skill_evolution).

        Returns:
            True if task-loop should be enabled, False otherwise.
        """
        config_base = config_base or get_config()
        evolution_enabled = get_skill_evolution_enabled(config_base)
        configured_value = config.get("enable_task_loop", True)

        if evolution_enabled:
            if not configured_value:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] skill_evolution=True requires enable_task_loop=True; "
                    "overriding user config (enable_task_loop=%s -> True)",
                    configured_value,
                )
            return True
        return configured_value

    def _resolve_enable_subagent_runtime(
        self,
        config_base: dict[str, Any] | None = None,
    ) -> bool:
        """Return whether persistent subagent runtime tools should be enabled."""
        if not getattr(self, "_subagent_runtime_supported", True):
            return False
        return is_subagent_runtime_enabled(config_base or get_config())

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
        ) or None
        context_model_state = _ContextEngineModelState(
            full_config=config_base,
            model_name=getattr(getattr(model, "model_config", None), "model_name", ""),
            model=model,
            config_base=config_base,
        )
        context_engine_config = _deep_agent_context_engine_config_for_model(
            config,
            model_state=context_model_state,
        )
        return DeepAgentConfig(
            model=model,
            card=agent_card,
            tool_owner_id=self._tool_owner_id(),
            system_prompt=build_agent_identity_prompt(
                language=self._resolve_prompt_language(),
            ),
            context_engine_config=context_engine_config,
            kv_cache_affinity_config=_deep_agent_kv_cache_affinity_config(config, model),
            enable_task_loop=self._resolve_enable_task_loop(config, config_base),
            enable_subagent_runtime=self._resolve_enable_subagent_runtime(config_base),
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
            progressive_tool_enabled=get_progressive_tool_enabled(config_base),
            vision_model_config=self._vision_model_config,
            audio_model_config=self._audio_model_config,
            enable_read_image_multimodal=self._resolve_enable_read_image_multimodal(config),
            completion_timeout=resolve_task_loop_completion_timeout(config),
        )

    def _update_permission_rail(self, config_base: dict[str, Any] | None) -> None:
        """原地更新已有 PermissionRail 配置，或在首次启用时新建。"""
        permission_config = config_base.get("permissions", {}) if config_base else {}
        if self._permission_rail is not None:
            self._permission_rail.update_config(permission_config)
            logger.info("[JiuWenSwarmDeepAdapter] _permission_rail config hot-updated")
        elif permission_config.get("enabled", False):
            self._permission_rail = build_permission_rail(
                config=config_base,
                llm=self._model,
                model_name=config_base.get("models", {})
                .get("default", {})
                .get("model_client_config", {})
                .get("model_name", "gpt-4"),
            )
            if self._permission_rail is not None:
                logger.info("[JiuWenSwarmDeepAdapter] _permission_rail newly created on hot-reload")

    def _get_current_agent_rails(
        self, config: dict[str, Any], config_base: dict[str, Any] | None = None
    ) -> list[Any]:
        """Return rail instances that need to be re-initialized on hot reload.

        SkillUseRail, ContextEngineeringRail, and MemoryRail are rebuilt on config reload.
        All other rails read language dynamically from system_prompt_builder.language
        and are updated in-place where needed — they are NOT passed to configure()
        so their existing registered state is preserved without an uninit/init cycle.
        """
        # Apply in-place updates to skill_evolution_rail (no re-init needed).
        if self._skill_evolution_rail is not None:
            self._skill_evolution_rail.update_llm(self._model, self._default_model_name)
            self._skill_evolution_rail.auto_save = get_evolution_auto_save_enabled(
                config_base or self._config_base_cache or config
            )

        # Reuse existing SkillUseRail to preserve dynamically loaded skills
        # from activate_package() / load_harness_config(). When directory
        # retrieval is enabled, AUTO_LIST keeps SkillTool live while the
        # Symphony rail owns discovery and prompt disclosure.
        if self._skill_rail is None:
            self._skill_rail = self._build_skill_rail(
                config,
                include_tools=self._skill_include_tools_for_profile(),
            )
        else:
            # Update existing rail's skill_mode if changed.
            new_skill_mode = self._resolve_skill_mode(
                config,
                config_base or self._config_base_cache,
                retrieval_enabled=(
                    self._skill_retrieval_tools_enabled_for_runtime(
                        config_base or self._config_base_cache
                    )
                ),
            )
            if self._skill_rail.skill_mode != new_skill_mode:
                self._skill_rail.skill_mode = new_skill_mode
            # Update disabled_skills.
            new_disabled = set(self._skill_manager.list_execution_disabled_skills())
            if self._skill_rail.disabled_skills != new_disabled:
                self._skill_rail.disabled_skills = new_disabled

        if not self._filesystem_rail_enabled_for_profile():
            self._filesystem_rail = None

        self._update_permission_rail(config_base)

        if self._heartbeat_rail is None:
            self._heartbeat_rail = self._build_heartbeat_rail()

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
        if self._memory_forbidden_rail is not None:
            rails_list.append(self._memory_forbidden_rail)
        if self._permission_rail is not None:
            rails_list.append(self._permission_rail)
        if self._heartbeat_rail is not None:
            rails_list.append(self._heartbeat_rail)
        return rails_list

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
        if self._is_session_scoped_adapter:
            return f"{_AGENT_CARD_ID}_s_{self._session_adapter_key(self._parent_session_id)}"
        return f"{_AGENT_CARD_ID}_root"

    @staticmethod
    def _register_shared_tool(tool: Any) -> None:
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
        """
        mark_stateless([tool])
        register_tool(tool, None)

    @staticmethod
    def _register_agent_owned_tool(tool: Any, owner_id: str) -> None:
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
        """
        if getattr(tool.card, "stateless", False):
            logger.debug(
                "[JiuWenSwarmDeepAdapter] tool %s is declared shared; registering it as shared"
                " despite the agent-owned call site",
                tool.card.name,
            )
        register_tool(tool, owner_id)

    async def _get_tool_cards(self, agent_id: str):
        """Get tool cards."""
        tool_cards = []

        for wtool in [read_pdf]:
            self._register_shared_tool(wtool)
            tool_cards.append(wtool.card)

        # 付费搜索工具：有任意一个付费 key 就注册
        if is_paid_search_enabled():
            self._paid_search_tool = WebPaidSearchTool(
                language=self._resolve_runtime_language(), agent_id=agent_id
            )
            self._register_agent_owned_tool(self._paid_search_tool, agent_id)
            tool_cards.append(self._paid_search_tool.card)
            self._paid_search_registered = True

        for tool_cls in [WebFreeSearchTool, WebFetchWebpageTool]:
            tool_instance = tool_cls(agent_id=agent_id)
            self._register_agent_owned_tool(tool_instance, agent_id)
            tool_cards.append(tool_instance.card)

        self._vision_tools = []
        self._vision_tools_registered = False
        if self._vision_model_config is not None:
            try:
                for tool in create_vision_tools(
                    language=self._resolve_runtime_language(),
                    vision_model_config=self._vision_model_config,
                    agent_id=agent_id,
                ):
                    self._register_agent_owned_tool(tool, agent_id)
                    tool_cards.append(tool.card)
                    self._vision_tools.append(tool)
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
            self._audio_tools = self._iter_runtime_audio_tools(agent_id)
            for tool in self._audio_tools:
                self._register_agent_owned_tool(tool, agent_id)
                tool_cards.append(tool.card)
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
                self._register_shared_tool(video_understanding)
                tool_cards.append(video_understanding.card)
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
                self._register_shared_tool(generate_image)
                tool_cards.append(generate_image.card)
                self._image_gen_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] generate_image tool registration failed: %s",
                    exc,
                )

        # generate_video tool: video_gen config, falling back to image_gen credentials
        self._video_gen_tool_registered = False
        if self._video_gen_model_config:
            try:
                self._register_shared_tool(generate_video)
                tool_cards.append(generate_video.card)
                self._video_gen_tool_registered = True
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] generate_video tool registration failed: %s",
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
                    self._register_shared_tool(xt)
                    tool_cards.append(xt.card)
                self._xiaoyi_phone_tools_registered = True
                logger.info(
                    "[JiuWenSwarmDeepAdapter] %d xiaoyi phone tools registered", len(_xiaoyi_tools)
                )
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] xiaoyi phone tools registration failed: %s", exc
                )

        try:
            skill_toolkit = SkillToolkit(manager=self._skill_manager)
            skill_tool_names: list[str] = []
            for tool in skill_toolkit.get_tools():
                self._register_shared_tool(tool)
                tool_cards.append(tool.card)
                skill_tool_names.append(tool.card.name)
            logger.info(
                "[JiuWenSwarmDeepAdapter] SkillToolkit registered: tools=%s",
                skill_tool_names,
            )
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] skill tools registration failed: %s", exc)

        if self._skill_retrieval_tools_enabled_for_runtime(
            self._config_base_cache
        ):
            try:
                self._skill_retrieval_tools = self._create_skill_retrieval_tools()
                skill_retrieval_tool_names: list[str] = []
                for tool in self._skill_retrieval_tools:
                    # skill_index owns session-frozen scale, taxonomy revision,
                    # and visibility state, so it must never share the first
                    # session's instance through the process-wide bare id.
                    self._register_agent_owned_tool(tool, self._tool_owner_id())
                    tool_cards.append(tool.card)
                    skill_retrieval_tool_names.append(tool.card.name)
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
                self._register_shared_tool(tool)
                tool_cards.append(tool.card)
                symphony_tool_names.append(tool.card.name)
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
                self._register_shared_tool(acp_chat)
                tool_cards.append(acp_chat.card)
                logger.info("[JiuWenSwarmDeepAdapter] acp_chat tool registered")
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] acp_chat registration failed: %s", exc)

        return tool_cards

    def _build_cron_tools(self) -> list[Any]:
        """Build cron tools from the shared runtime bridge."""
        return self._cron_runtime.build_tools(
            context=self._runtime_cron_tool_context,
            agent_id=self._tool_owner_id(),
            language=self._resolve_runtime_language(),
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

    def get_live_session_instance(self, session_id: str | None) -> Any | None:
        """Return the already-running DeepAgent that owns ``session_id``.

        Chat turns run on a session-scoped child adapter, and
        ``DeepAgent.load_state`` caches its snapshot on the bound Session object.
        Callers that mutate session state outside a chat turn (plan mode sync)
        must therefore reach that instance instead of a throwaway session, or
        the running conversation keeps reading the pre-change snapshot.

        Returns:
            The live DeepAgent, or None when this session has not started one yet
            (first turn of a session). Prefer :meth:`ensure_live_session_instance`
            when the caller is about to write plan state: that starts the same
            session the chat turn will reuse, instead of a throwaway Session.
        """
        if self._is_session_scoped_adapter:
            return self._instance
        adapter = self._get_cached_session_adapter(session_id)
        if adapter is None:
            return None
        # 子适配器一定是 session 级的（``_new_session_scoped_adapter`` 建完就
        # ``mark_as_session_scoped``），所以这一跳递归只会走上面那个分支返回它自己的
        # 实例，不会再往下递归。
        return adapter.get_live_session_instance(session_id)

    async def release_subagent_runtime_for_session(
        self,
        session_id: str | None,
        *,
        reason: str = "session_deleted",
    ) -> None:
        """Cancel cached subagents and flush persistence for one parent session."""
        sid = self._session_adapter_key(session_id)
        if not sid:
            return
        self._clear_subagent_progress_batch(sid)
        from openjiuwen.harness.tools.subagent import release_subagent_control

        deep_agent = self.get_live_session_instance(sid)
        if deep_agent is None:
            deep_agent = self._instance
        if deep_agent is None:
            return
        try:
            await release_subagent_control(deep_agent, sid, reason=reason)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] release_subagent_runtime failed: session_id=%s error=%s",
                sid,
                exc,
            )

    async def ensure_live_session_instance(self, session_id: str | None) -> Any | None:
        """Start the session-scoped adapter if needed and return its DeepAgent.

        Plan-mode sync must write onto the Session ``start_interaction`` binds.
        A throwaway Session only updates the checkpointer; a concurrent first
        ``chat.send`` / ``command.goal`` can already have bound a normal-mode
        snapshot that the running turn keeps using.
        """
        adapter = await self._get_or_create_session_adapter(session_id)
        return adapter.get_live_session_instance(session_id)

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
                **self._session_instance_extra_create_kwargs(),
            )
            return self._instance

    async def create_instance(
        self, config: dict[str, Any] | None = None, *, mode: str = "agent", sub_mode: str = None
    ) -> None:
        """初始化 DeepAgent 实例.

        Args:
            config: 可选配置，支持以下字段：
                - agent_name: Agent 名称，默认 "main_agent"。
                - workspace_dir: 工作区目录，默认 "workspace/agent"。
                - 其余字段透传给 DeepAgentConfig。
            mode: 实例化模式，默认 "agent"，使用 create_deep_agent。
            sub_mode: 子模式
        """
        self._session_instance_config = dict(config or {}) if isinstance(config, dict) else None
        self._session_instance_mode = mode
        self._session_instance_sub_mode = sub_mode
        # Channel id drives the MCP load strategy (see _register_mcp_servers_
        # from_config / _sync_mcp_servers_for_runtime): the TUI channel loads
        # the global-default set (config.yaml ∪ state.json enabled) on init;
        # the web channel loads nothing on init (session-level via chat.send's
        # ``mcp`` field). Session children inherit this from the root.
        self._channel_id = str(
            (config or {}).get("channel_id") if isinstance(config, dict) else ""
            or ""
        ).strip() or getattr(self, "_channel_id", "")

        await self.set_checkpoint()
        await asyncio.sleep(0)

        # dreaming 只区分 agent 工作族 / code profile 两种形态。新三段命名 canonical
        # （agent.work.normal / agent.code.normal / team.code.normal 等）已取代旧
        # agent / code 串：先 deprecate 归一再按 profile 归一成二元 token（"agent" /
        # "code"），保证下游 sweeper 的 == "agent" 判定、output_dir 与 prompt 选择都正确。
        # 裸 "code" 经 deprecate -> agent.code.normal，也能被 is_code_profile_mode 命中；
        # team 等未知模式沿用历史 agent 兜底。
        _deprecated_mode = deprecate_mode(mode) if mode else ""
        if _deprecated_mode in (NEW_AGENT_WORK_NORMAL, NEW_AGENT_WORK_PLAN):
            self._dreaming_mode = "agent"
        elif is_code_profile_mode(_deprecated_mode):
            self._dreaming_mode = "code"
        else:
            self._dreaming_mode = "agent"
        self._instance_overrides = dict(config or {}) if isinstance(config, dict) else {}
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
        config_base = get_config()
        self._config_base_cache = config_base.copy()
        if self._skill_retrieval_session_enabled is None:
            restored_profile = self._restored_skill_retrieval_profile
            self._skill_retrieval_session_enabled = (
                bool(restored_profile.get("enabled"))
                if restored_profile is not None
                else self._resolve_skill_retrieval_session_enabled(config_base)
            )
        self._refresh_multimodal_configs(config_base)
        config = config_base.get("react", {}).copy()
        self._config_cache = config.copy()
        self._agent_name = self._instance_overrides.get(
            "agent_name", config.get("agent_name", "main_agent")
        )
        self._project_dir = self._instance_overrides.get(
            "project_dir", config.get("project_dir")
        )
        self._workspace_dir = config.get("workspace_dir", str(get_agent_workspace_dir()))
        if self._skip_own_instance_build():
            # Root adapter 只做 router/template holder，不建 DeepAgent instance。
            # web channel 在此后台预热 connected MCP 的进程级连接缓存——首轮对话
            # reconcile 在 session child 注册时命中缓存不重 spawn。不阻塞 create
            # instance（fire-and-forget），不挂任何会话（不碰 _session_selected_mcp）。
            if (
                getattr(self, "_channel_id", "") == "web"
                and not self._is_session_scoped_adapter
            ):
                self._start_mcp_prewarm()
            return

        model = self._create_model(config_base)
        if (
            self._is_session_scoped_adapter
            and self._skill_retrieval_tools_enabled_for_runtime(config_base)
        ):
            self._freeze_skill_retrieval_context_window(config_base, model)
        if self._is_session_scoped_adapter:
            await self._try_init_a2x_client(config_base)
        agent_card = AgentCard(name=self._agent_name, id=_AGENT_CARD_ID)

        tool_cards = await self._get_tool_cards(self._tool_owner_id())
        self._tool_cards = tool_cards
        await asyncio.sleep(0)

        # 权限护栏由 openjiuwen PermissionInterruptRail + ToolPermissionHost 接管；
        # 无需初始化 jiuwenswarm 内置 PermissionEngine（已弃用）。

        rails_list = self._build_agent_rails(config, config_base, mode=mode)

        sys_operation = self._create_sys_operation()
        if sys_operation is None:
            raise RuntimeError("sys_operation is not available, maybe task is not running")

        self._sys_operation = sys_operation
        configured_subagents, should_add_general_agent = self._build_configured_subagents(model, config, config_base)
        should_enable_general_agent = should_add_general_agent and (
            sub_mode == "plan" or (isinstance(mode, str) and mode.startswith("agent"))
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
            # Keep explicitly direct tools (including the enabled installed-Skill
            # directory) visible; defer and index the remaining ordinary tools.
            progressive_tool_enabled=get_progressive_tool_enabled(config_base),
            enable_task_loop=self._resolve_enable_task_loop(config, config_base),
            enable_subagent_runtime=self._resolve_enable_subagent_runtime(config_base),
            add_general_purpose_agent=should_enable_general_agent,
            max_iterations=config.get("max_iterations", 15),
            workspace=Workspace(
                root_path=self._workspace_dir or "./",
                language=self._resolve_runtime_language(),
            ),
            sys_operation=sys_operation,
            language=self._resolve_runtime_language(),
            auto_create_workspace=False
        )

        context_model_state = _ContextEngineModelState(
            full_config=config_base,
            model_name=getattr(getattr(model, "model_config", None), "model_name", ""),
            model=model,
            config_base=config_base,
        )
        context_engine_config = _deep_agent_context_engine_config_for_model(
            config,
            model_state=context_model_state,
        )
        self._instance = create_deep_agent(
            **common_kwargs,
            context_engine_config=context_engine_config,
            kv_cache_affinity_config=_deep_agent_kv_cache_affinity_config(config, model),
            vision_model_config=self._vision_model_config,
            audio_model_config=self._audio_model_config,
            enable_read_image_multimodal=self._resolve_enable_read_image_multimodal(config),
            enable_model_anomaly_detection_rail=(
                (config_base.get("execution_guard") or {}).get("model_anomaly_detection_rail") or {}
            ).get("enabled", False),
            completion_timeout=resolve_task_loop_completion_timeout(config),
        )

        initial_runtime_workspace = self._project_dir or str(
            get_default_project_session_workspace_dir()
        )
        self._seed_runtime_cwd(initial_runtime_workspace, workspace=initial_runtime_workspace)
        setattr(self._instance, "_jiuwenswarm_project_dir", initial_runtime_workspace)

        self._sync_a2x_runtime_state()
        # Cron tools belong to the agent's standing toolset, not to any one
        # request; build them here so the first turn does not pay for it either.
        self._ensure_cron_tools_registered(self._parent_session_id)
        self._registered_mcp_server_ids.clear()
        self._registered_mcp_servers.clear()
        # MCP load strategy by channel: the TUI channel loads its global-
        # default set (config.yaml ∪ state.json ``enabled=True``) on init —
        # both root and TUI session children register it. The web channel
        # loads NOTHING on init: web is session-level, its MCPs come solely
        # from chat.send's ``mcp`` field via reconcile_session_mcp (None and
        # [] both mean "no MCP this turn"). Skipping init keeps web's
        # default-False contract.
        if getattr(self, "_channel_id", "") != "web":
            await self._register_mcp_servers_from_config(config_base, tag=f"agent.{mode}")
        logger.info(
            "[JiuWenSwarmDeepAdapter] 初始化完成: agent_name=%s, mode=%s, sub_mode=%s", self._agent_name, mode, sub_mode
        )

        # 加载已激活的 packages（skills, rails, tools）
        await self._load_active_packages()
        await asyncio.sleep(0)

        # 动态加载用户自定义的 Rail 扩展
        await self.load_user_rails()

        # All host-level startup providers have now registered their tools.
        # Initialize the DeepAgent only after that point; its normal startup
        # path builds the initial BM25 snapshot after all pending rails.
        await self._instance.ensure_initialized()

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
            env_overrides: Optional environment variable delta applied after
                reloading the instance ``.env`` file; a None value removes the
                variable.

        Returns:
            The normalized config snapshot that was cached on this adapter.
        """
        clear_config_cache()
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

        # create_instance() already re-reads .env; reload must too, otherwise
        # ${MODEL_NAME} stays at process-start values and a later create_instance
        # is overwritten by the stale pending snapshot.
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)

        if env_overrides is not None:
            if not isinstance(env_overrides, dict):
                raise TypeError("env_overrides must be a dict when provided")
            for env_key, env_value in env_overrides.items():
                if env_value is None:
                    os.environ.pop(str(env_key), None)
                else:
                    os.environ[str(env_key)] = str(env_value)

        if config_base is None:
            config_base = get_config()
        elif not isinstance(config_base, dict):
            raise TypeError("config_base must be a dict when provided")
        else:
            config_base = resolve_env_vars(config_base)

        self._config_base_cache = config_base.copy()
        self._refresh_multimodal_configs(config_base)
        self._config_cache = config_base.get("react", {}).copy()
        return config_base

    async def _apply_multimodal_reload_snapshot(
        self,
        config_base: dict[str, Any] | None,
        env_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Refresh only multimodal configuration without resetting other runtimes."""
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
        if env_overrides is not None:
            if not isinstance(env_overrides, dict):
                raise TypeError("env_overrides must be a dict when provided")
            for env_key, env_value in env_overrides.items():
                if env_value is None:
                    os.environ.pop(str(env_key), None)
                else:
                    os.environ[str(env_key)] = str(env_value)

        if config_base is None:
            clear_config_cache()
            config_base = get_config()
        elif not isinstance(config_base, dict):
            raise TypeError("config_base must be a dict when provided")
        else:
            config_base = resolve_env_vars(config_base)

        self._config_base_cache = config_base.copy()
        self._refresh_multimodal_configs(config_base)
        return config_base

    async def _fan_out_reload_to_session_adapters(
        self,
        config_base: dict[str, Any],
        env_overrides: dict[str, Any] | None,
        target_sid: str | None,
        reload_scopes: set[str] | None = None,
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
            self._mark_session_adapters_stale_for_reload(
                config_base,
                env_overrides,
                reload_scopes,
            )
            return
        for session_id, adapter in self._iter_session_adapters_for_reload(target_sid):
            try:
                await adapter.reload_agent_config(
                    config_base,
                    env_overrides,
                    target_session_id=target_sid,
                    reload_scopes=reload_scopes,
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
        reload_scopes: set[str] | None = None,
    ) -> None:
        """从 config.yaml 重新加载配置，通过 DeepAgent.configure() 热更新当前实例（不新建 DeepAgent）。

        DeepAgent.configure() 现在自动处理 rail 生命周期：保留旧已注册 rails 的注销上下文，
        并在下次 _ensure_initialized() 时先卸载旧回调，再注册新的 rails。

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
            target_session_id: 可选的目标 session id；传入时仅级联热更新该 session adapter。
            reload_scopes: 可选的精确配置作用域。
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
                return
        scope_set = set(reload_scopes) if reload_scopes else set()
        if scope_set == {"multimodal"}:
            config_base = await self._apply_multimodal_reload_snapshot(
                config_base,
                env_overrides,
            )
            if self._instance is not None:
                self._sync_multimodal_tools_for_runtime()
            await self._fan_out_reload_to_session_adapters(
                config_base,
                env_overrides,
                target_sid,
                scope_set,
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] multimodal tools hot-reloaded"
            )
            return
        previous_skill_retrieval_build_profile = (
            self._skill_retrieval_build_profile(self._config_base_cache)
        )
        if self._instance is None:
            if self._is_session_scoped_adapter:
                raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")
            # Root adapter whose lazily-built DeepAgent nobody has needed yet:
            # it has nothing of its own to reconfigure, but the cached config
            # snapshot still has to move forward and the live session adapters
            # still have to be reloaded.
            config_base = await self._apply_reload_config_snapshot(config_base, env_overrides)
            await self._cancel_skill_retrieval_build_after_reload(
                previous_skill_retrieval_build_profile,
                config_base,
            )
            await self._fan_out_reload_to_session_adapters(
                config_base,
                env_overrides,
                target_sid,
                scope_set,
            )
            logger.info(
                "[JiuWenSwarmDeepAdapter] 配置已热更新（root 实例未构建，仅刷新缓存并级联 session adapter）"
            )
            return

        config_base = await self._apply_reload_config_snapshot(config_base, env_overrides)
        await self._cancel_skill_retrieval_build_after_reload(
            previous_skill_retrieval_build_profile,
            config_base,
        )
        config = self._config_cache.copy()

        model = self._create_model(config_base)
        if self._is_session_scoped_adapter:
            await self._try_init_a2x_client(config_base, reload=True)
            self._sync_a2x_runtime_state()
        self._agent_name = self._instance_overrides.get("agent_name", config.get("agent_name", "main_agent"))
        agent_card = AgentCard(name=self._agent_name, id=_AGENT_CARD_ID)
        self._sync_multimodal_tools_for_runtime()
        self._sync_paid_search_tool_for_runtime()
        self._sync_symphony_tools_for_runtime(config_base)
        self._sync_skill_retrieval_tools_for_runtime(config_base)
        await self._sync_skill_retrieval_prompt_rail_for_runtime(config_base)

        if not self._filesystem_rail_enabled_for_profile() and self._filesystem_rail is not None:
            try:
                await self._instance.unregister_rail(self._filesystem_rail)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] ACP filesystem rail unregister failed: %s", exc
                )
            self._filesystem_rail = None

        rails_list = self._get_current_agent_rails(config, config_base)

        # 加载用户自定义的 Rail 扩展
        await self.load_user_rails()

        deep_cfg = self._make_deep_agent_config(
            model=model,
            config=config,
            config_base=config_base,
            agent_card=agent_card,
            tool_cards=self._tool_cards if self._tool_cards else [],
            rails=rails_list,
        )
        omitted_fields, reload_fingerprints = self._omit_unchanged_reload_fields(deep_cfg)
        try:
            self._instance.configure(deep_cfg)
        finally:
            self._restore_omitted_reload_fields(deep_cfg, omitted_fields)
        self._commit_reload_fingerprints(reload_fingerprints)
        self._sync_active_evolution_review_agent_after_reload()

        await self._sync_mcp_servers_for_runtime(config_base, tag="agent.reload")

        # configure() drops harness-injected tools (they live in
        # deep_config.tools, not the config.yaml-driven tool_cards) as stale;
        # re-bind so MCP/model/config saves don't strip harness tools.
        await self._load_active_packages()

        await self._fan_out_reload_to_session_adapters(
            config_base,
            env_overrides,
            target_sid,
            scope_set,
        )

        # 主动刷新 memory rail（不等下次请求的 _update_rails_for_mode）：
        # 让 embedding 配置变更立即走指纹检测 + 重建 rail + 延时重索引。
        # 若从未处理过请求（_last_mode 为 None，如冷启动后首次 reload），退化为默认 agent。
        try:
            mode = self._last_mode or "agent"
            await self._handle_memory_rail_by_config(mode)
        except Exception as e:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] memory rail refresh on reload failed: %s", e
            )

        logger.info("[JiuWenSwarmDeepAdapter] 配置已热更新（configure），未重启进程")

    @staticmethod
    def _bind_runtime_cron_context(
        *,
        channel_id: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None,
        request_id: str | None,
        mode: str | None,
        project_dir: str | None = None,
        user_id: str | None = None,
    ) -> _RuntimeCronContextTokens:
        from openjiuwen.core.sys_operation.shell_process_registry import (
            set_shell_session_id,
        )

        normalized_channel = str(channel_id or "").strip() or CronTargetChannel.WEB.value
        normalized_mode = str(mode).strip() if isinstance(mode, str) and mode.strip() else None
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else None
        if normalized_metadata is None:
            normalized_metadata = {}
        if isinstance(request_id, str) and request_id.strip():
            normalized_metadata["request_id"] = request_id.strip()
        # cron 普通模式执行时 channel_id 是内部标识 "__cron__"（隔离执行会话），
        # 真实推送渠道由 scheduler 通过 metadata["targets"] 下发（= job.targets，
        # 与 cron 文本结果推送到同一批渠道）。这里归一为真实渠道，供 send_file
        # 等按渠道开关的工具注册判定，并作为文件推送的 channel_id。
        if normalized_channel == "__cron__":
            cron_targets = str(normalized_metadata.get("targets") or "").strip()
            if cron_targets:
                normalized_channel = cron_targets

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
        for key in ("project_id", "project_dir", "work_mode", "model_name"):
            value = normalized_metadata.get(key)
            if isinstance(value, str) and value.strip():
                continue
            session_value = session_metadata.get(key)
            if isinstance(session_value, str) and session_value.strip():
                normalized_metadata[key] = session_value.strip()
        if not str(normalized_metadata.get("model_name") or "").strip():
            session_model = session_metadata.get("model")
            if isinstance(session_model, str) and session_model.strip():
                normalized_metadata["model_name"] = session_model.strip()
        # 提取创建者 user_id，供 cron tool 透传给创建者标识。
        # agent 内部调用 cron_create_job 时无 web 连接 user_id 来源，优先取
        # 请求/E2A 信封携带的 user_id（AgentOS 路由键），其次靠会话 metadata 兜底。
        cron_user_id: str | None = None
        explicit_uid = str(user_id or "").strip() if user_id else ""
        if explicit_uid:
            cron_user_id = explicit_uid
        elif isinstance(session_metadata, dict):
            sid_uid = str(session_metadata.get("user_id") or "").strip()
            if sid_uid:
                cron_user_id = sid_uid
        return _RuntimeCronContextTokens(
            channel=_CRON_TOOL_CHANNEL_ID.set(normalized_channel),
            session=_CRON_TOOL_SESSION_ID.set(session_id),
            metadata=_CRON_TOOL_METADATA.set(normalized_metadata),
            mode=_CRON_TOOL_MODE.set(normalized_mode),
            bound=_CRON_TOOL_BOUND.set(True),
            shell=set_shell_session_id(session_id),
            user_id=_CRON_TOOL_USER_ID.set(cron_user_id),
        )

    @staticmethod
    def _reset_runtime_cron_context(
        tokens: _RuntimeCronContextTokens,
    ) -> None:
        from openjiuwen.core.sys_operation.shell_process_registry import (
            reset_shell_session_id,
        )

        reset_shell_session_id(tokens.shell)
        _CRON_TOOL_BOUND.reset(tokens.bound)
        _CRON_TOOL_MODE.reset(tokens.mode)
        _CRON_TOOL_METADATA.reset(tokens.metadata)
        _CRON_TOOL_SESSION_ID.reset(tokens.session)
        _CRON_TOOL_CHANNEL_ID.reset(tokens.channel)
        _CRON_TOOL_USER_ID.reset(tokens.user_id)

    async def _update_rails_for_mode(self, mode: str) -> None:
        """装配 agent 模式 rails。

        plan / fast 已合并为单一 ``agent`` 模式：统一挂载 plan 档能力
        （TaskPlanning / Subagent / 演进 rail 等），记忆固定为被动模式，
        不再按子模式分叉。历史 ``agent.plan`` / ``agent.fast`` 归一到此路径。
        """
        self._last_mode = mode
        await self._update_agent_rails()
        await self._sync_personal_context_rail(mode)

    def _personal_context_rail_enabled(self, mode: str) -> bool:
        """Return whether this request mode uses the embedded Core Rail."""

        return (
            not self._is_code_agent
            and deprecate_mode(mode) in {NEW_AGENT_WORK_NORMAL, NEW_AGENT_WORK_PLAN}
            and self._personal_context_runtime_enabled
        )

    def set_personal_context_runtime_enabled(self, enabled: bool) -> None:
        """Store the Host switch snapshot for this adapter and future sessions."""

        self._personal_context_runtime_enabled = bool(enabled)
        if not self._is_session_scoped_adapter:
            for adapter in list(getattr(self, "_session_adapters", {}).values()):
                adapter.set_personal_context_runtime_enabled(enabled)

    async def refresh_personal_context_rail(self) -> None:
        """Apply the current Host switch to this adapter and live session adapters."""

        mode = self._last_mode
        if mode is not None:
            await self._sync_personal_context_rail(mode)
        if not self._is_session_scoped_adapter:
            for adapter in list(self._session_adapters.values()):
                cancelled: asyncio.CancelledError | None = None
                try:
                    await adapter.refresh_personal_context_rail()
                except asyncio.CancelledError as exc:
                    cancelled = exc
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] optional PersonalContext session "
                        "Rail refresh failed: %s",
                        type(exc).__name__,
                    )
                if cancelled is not None:
                    raise cancelled

    async def _sync_personal_context_rail(self, mode: str) -> None:
        """Register or detach the fixed-path Core Rail for normal agent modes."""

        async with self._personal_context_rail_lock:
            enabled = self._personal_context_rail_enabled(mode)
            rail = self._personal_context_rail

            if rail is not None and not enabled:
                try:
                    await self._instance.unregister_rail(rail)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] optional PersonalContext Rail "
                        "unregister failed: %s",
                        type(exc).__name__,
                    )
                    return
                self._personal_context_rail = None
                rail = None

            if not enabled:
                return

            if rail is None:
                try:
                    rail = PersonalContextRail(
                        Path.home() / ".jiuwenswarm" / ".personal_context"
                    )
                    await self._instance.register_rail(rail)
                except Exception as exc:  # noqa: BLE001
                    if rail is not None:
                        try:
                            await self._instance.unregister_rail(rail)
                        except Exception as cleanup_exc:  # noqa: BLE001
                            logger.warning(
                                "[JiuWenSwarmDeepAdapter] optional PersonalContext Rail "
                                "registration cleanup failed: %s",
                                type(cleanup_exc).__name__,
                            )
                    self._personal_context_rail = None
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] optional PersonalContext Rail "
                        "registration failed: %s",
                        type(exc).__name__,
                    )
                    return
                self._personal_context_rail = rail
                logger.info(
                    "[JiuWenSwarmDeepAdapter] PersonalContextRail registered for %s", mode
                )

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

        # SkillEvolutionRail runtime configure creates/reuses and registers its
        # rail-owned tools/review subagent.  A disabled switch tears down the
        # entire stack, including pending watcher state.
        evolution_enabled = get_skill_evolution_enabled(self._config_base_cache or self._config_cache)
        if evolution_enabled:
            await self._ensure_active_evolution_rails_registered()
        else:
            await self._unconfigure_active_evolution_rails()
            for task in list(self._evolution_watcher_tasks):
                if not task.done():
                    task.cancel()
            self._evolution_watcher_tasks.clear()
            logger.info("[JiuWenSwarmDeepAdapter] evolution stack unregistered (skill_evolution=false)")

        # SkillCreateRail
        skill_create_enabled = evolution_enabled
        if skill_create_enabled:
            # Warn if task_loop is disabled
            deep_config = getattr(self._instance, "deep_config", None) if self._instance else None
            if deep_config is not None:
                if not deep_config.enable_task_loop:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] skill_evolution=true requires task_loop mode, "
                        "but enable_task_loop=False. SkillCreateRail may not function properly."
                    )
            if self._skill_create_rail is None:
                self._skill_create_rail = self._build_skill_create_rail(
                    self._config_base_cache or self._config_cache
                )
            if self._skill_create_rail is not None:
                await self._instance.register_rail(self._skill_create_rail)
                logger.info("[JiuWenSwarmDeepAdapter] SkillCreateRail registered for agent mode")
        else:
            # skill evolution disabled: unregister if exists
            if self._skill_create_rail is not None:
                await self._instance.unregister_rail(self._skill_create_rail)
                self._skill_create_rail = None
                logger.info("[JiuWenSwarmDeepAdapter] SkillCreateRail unregistered (skill_evolution=false)")

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
        if session_id is not None and session_id.startswith(
            ("heartbeat", "health_check", "cron")
        ):
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
        """刷新每请求相关的 cron / heartbeat / send_file 工具运行时状态。

        工具实例都只建一次：cron/heartbeat 分别由对应的 ``_ensure_*``
        方法注册，send_file 首次注册后改走 ``update_runtime_context``。
        这里每次请求只做幂等检查和运行时上下文更新。
        """
        self._ensure_cron_tools_registered(session_id)

        # send_file 工具：由 channels.<channel>.send_file_allowed 控制。工具实例只建一次，
        # 之后每次请求只用 update_runtime_context 刷新 request_id/session_id/channel 等
        # 运行时上下文（cron 同理，见上）。
        # channel_id/metadata 由调用前的 _bind_runtime_cron_context 已写入 contextvar
        config_base = get_config()
        channel = (
            str(channel_id or self._resolve_prompt_channel(session_id) or "web").strip() or "web"
        )
        # cron 执行时 channel_id 是内部标识（如 "__cron__"），真实推送渠道由
        # _bind_runtime_cron_context 归一后写入 contextvar（= job.targets，与 cron
        # 文本结果推送到同一批渠道）。send_file 的注册判定与文件推送都按真实渠道进行。
        if _CRON_TOOL_BOUND.get():
            cron_channel = str(_CRON_TOOL_CHANNEL_ID.get() or "").strip()
            if cron_channel:
                channel = cron_channel
        send_file_enabled = (
            config_base.get("channels", {}).get(channel, {}).get("send_file_allowed")
        )
        # web channel defaults to True, others default to False
        if send_file_enabled is None:
            send_file_enabled = (channel == "web")
        if send_file_enabled and request_id and session_id:
            channel_for_tool = _CRON_TOOL_CHANNEL_ID.get()
            metadata_for_tool = _CRON_TOOL_METADATA.get()
            already_registered = any(
                getattr(existing, "name", "").startswith("send_file_to_user")
                for existing in (self._instance.ability_manager.list() or [])
            )
            if not already_registered:
                self._send_file_toolkit = SendFileToolkit(
                    request_id=request_id,
                    session_id=session_id,
                    channel_id=channel_for_tool,
                    metadata=metadata_for_tool,
                    user_id=_CRON_TOOL_USER_ID.get(),
                    project_dir=self._project_dir,
                )
                for sf_tool in self._send_file_toolkit.get_tools():
                    self._register_agent_owned_tool(sf_tool, self._tool_owner_id())
                    self._instance.ability_manager.add(sf_tool.card)
            else:
                self._send_file_toolkit.update_runtime_context(
                    request_id=request_id,
                    session_id=session_id,
                    channel_id=channel_for_tool,
                    metadata=metadata_for_tool,
                    user_id=_CRON_TOOL_USER_ID.get(),
                    project_dir=self._project_dir,
                )
            bind_active_send_file_toolkit(self._send_file_toolkit)
        else:
            # Avoid leaving a prior request's session toolkit registered when
            # this request does not enable send_file (misdelivery risk).
            clear_active_send_file_toolkit(session_id=session_id)

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
            workspace or self._workspace_dir or self._project_dir or os.getcwd()
        )
        runtime_cwd = str(cwd or "").strip()
        if not runtime_cwd or not os.path.isdir(runtime_cwd):
            runtime_cwd = str(self._project_dir or "").strip()
        if not runtime_cwd or not os.path.isdir(runtime_cwd):
            runtime_cwd = workspace_root
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
        eternal_conversation_enabled: bool = False
        interaction_resume: bool = False

    @staticmethod
    def _resolve_eternal_conversation_enabled(params: Any) -> bool:
        """Resolve the V1 frontend runtime flag; absent remains disabled."""
        if not isinstance(params, dict):
            return False
        value = params.get("eternal_conversation_enabled", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}
        return bool(value)

    @staticmethod
    def _is_eternal_interaction_resume(params: Any) -> bool:
        if not isinstance(params, dict):
            return False
        return str(params.get("source") or "").strip() in {
            "permission_interrupt",
            "confirm_interrupt",
        }

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
        circuit_breaker_rail = getattr(self, "_circuit_breaker_rail", None)
        if circuit_breaker_rail is not None:
            circuit_breaker_rail.set_language(resolved_language)
        stage_timer.mark("rail_setters")

        eternal_conversation_rail = getattr(self, "_eternal_conversation_rail", None)
        if eternal_conversation_rail is not None:
            self._eternal_conversation_enabled = runtime_config.eternal_conversation_enabled
            eternal_conversation_rail.configure_runtime(
                enabled=runtime_config.eternal_conversation_enabled,
                session_id=runtime_config.session_id,
                request_id=runtime_config.request_id,
                mode=runtime_config.mode,
                channel=resolved_channel,
                project_dir=runtime_config.project_dir or self._project_dir,
                model=getattr(self, "_active_request_model", None) or self._model,
                interaction_resume=runtime_config.interaction_resume,
            )
            if (
                self._eternal_conversation_enabled
                and self._context_processor_rail is not None
                and self.shutdown_context_session_memory(self._context_processor_rail)
            ):
                logger.info(
                    "[JiuWenSwarmDeepAdapter] SessionMemoryManager disabled: "
                    "eternal conversation owns semantic memory"
                )
        stage_timer.mark("eternal_conversation")

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

        session = create_agent_session(
            session_id=session_id,
            card=getattr(self._instance, "card", None),
        )
        await session.pre_run(inputs={})
        await self._instance.start(session=session)
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

    async def stop_interaction(self) -> None:
        """Stop this adapter's DeepAgent interaction loop if it was started."""
        if self._instance is None:
            return
        await self._instance.stop()

    async def cleanup(self) -> None:
        """Release adapter-owned external runtime resources."""
        await self._cleanup_evolution_background_tasks()
        eternal_conversation_rail = getattr(self, "_eternal_conversation_rail", None)
        if eternal_conversation_rail is not None:
            try:
                await eternal_conversation_rail.close()
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] eternal conversation cleanup failed: %s", exc
                )
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
                if self._parent_session_id:
                    await self.release_subagent_runtime_for_session(
                        self._parent_session_id,
                        reason="adapter_cleanup",
                    )
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
        try:
            await self._sync_personal_context_rail("cleanup")
        except BaseException as exc:  # noqa: BLE001
            logger.warning(
                "[JiuWenSwarmDeepAdapter] PersonalContextRail cleanup failed: %s",
                exc,
            )
        self._teardown_agent_owned_tools()
        self._release_sys_operations()
        # 取消未到期的延时重索引 task，避免 adapter cleanup 后仍有孤儿 task
        # 去触发 manager.sync（此时 rail/manager 可能已失效）。
        if self._memory_reindex_task is not None and not self._memory_reindex_task.done():
            self._memory_reindex_task.cancel()
        self._memory_reindex_task = None
        # 取消 MCP 预热后台 task，避免 cleanup 后孤儿 task 触发 probe。
        if self._mcp_prewarm_task is not None and not self._mcp_prewarm_task.done():
            self._mcp_prewarm_task.cancel()
        self._mcp_prewarm_task = None
        await self._close_a2x_client()

    async def _cleanup_evolution_background_tasks(self) -> None:
        """Drain detached evolution work before adapter-owned state is released."""
        rail = getattr(self, "_skill_evolution_rail", None)
        cleanup = getattr(rail, "cleanup_background_tasks", None)
        if not callable(cleanup):
            return
        try:
            await cleanup()
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] evolution cleanup failed during "
                "adapter teardown: %s",
                exc,
            )
            raise

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

        Prefer the round latch (provenance of the chunk being forwarded), then
        ``_stream_content_run_kind`` (stamped while forwarding deltas), over the
        live ``active_round``: a user-round final can still be in the queue
        after the scheduler has already started the next goal round.
        Falling back to ``_has_active_goal_round()`` covers goal attempts that
        emit a final with no prior delta on this consumer.
        """
        if not self._goal_record_is_active():
            return False
        content_kind = self._stream_round_kind_latch or self._stream_content_run_kind
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

    def _consumed_round_run_kind(self) -> str | None:
        """Run kind of the round that produced the chunk being forwarded.

        Falls back to the live ``active_round`` only when no round has been
        latched yet on this stream (see ``_track_round_output_boundary``).
        """
        latched = self._stream_round_kind_latch
        if latched is not None:
            return latched
        return self._current_interaction_run_kind()

    def _reset_round_kind_latch(self) -> None:
        """Forget the previous consumer's round; the next chunk re-samples."""
        self._stream_round_kind_latch = None
        self._stream_round_output_ended = False
        self._stream_round_visible_text = ""

    def _note_round_visible_text(self, content: str) -> None:
        """Remember visible text already streamed for the current round."""
        if not content:
            return
        text = self._stream_round_visible_text + content
        if len(text) > _ROUND_VISIBLE_TEXT_MAX_CHARS:
            text = text[-_ROUND_VISIBLE_TEXT_MAX_CHARS:]
        self._stream_round_visible_text = text

    def _goal_intermediate_final_repeats_streamed_text(self, content: str) -> bool:
        """Whether a demoted goal final only repeats text the bubble already shows.

        A round's ``answer`` carries the whole answer, which normally arrives
        token by token first: a real ``chat.final`` *replaces* the bubble text,
        so nothing is duplicated. Demoted to ``chat.delta`` it is *appended*
        instead, printing the answer twice — and history (which mirrors the
        delta stream) would keep both copies.
        """
        answer = _strip_whitespace(content)
        if not answer:
            return True
        streamed = _strip_whitespace(self._stream_round_visible_text)
        return bool(streamed) and answer in streamed

    def _track_round_output_boundary(self, chunk: Any) -> None:
        """Latch which round owns the chunks currently being consumed.

        Output is drained from a queue, so ``active_round`` can already point at
        the next (goal) round while this round's tail is still queued. Sampling
        it per chunk mislabels that tail as goal output, which demotes the user
        round's terminal ``chat.final`` and merges the ordinary answer and the
        goal answer into a single bubble. Sample once per round instead, on the
        first chunk after the previous round's terminal chunk, and keep that
        value for the whole round.
        """
        if self._stream_round_output_ended:
            self._reset_round_kind_latch()
        if self._stream_round_kind_latch is None:
            self._stream_round_kind_latch = self._current_interaction_run_kind()
        if self._is_round_terminal_chunk(chunk):
            self._stream_round_output_ended = True

    @staticmethod
    def _is_round_terminal_chunk(chunk: Any) -> bool:
        """Whether this chunk is the last output of one interaction round.

        ``answer`` is written once per round (including empty answers and goal
        attempt boundaries); HITL interrupt frames end a round without one.
        """
        chunk_type = getattr(chunk, "type", None)
        if chunk_type is None and isinstance(chunk, dict):
            chunk_type = chunk.get("type")
        if chunk_type is None:
            return False
        return str(getattr(chunk_type, "value", chunk_type)) in _ROUND_TERMINAL_CHUNK_TYPES

    def _begin_visible_chat_content(
        self,
        stream_is_user_originated: bool = False,
        *,
        session_id: str | None = None,
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

        Provenance comes from the per-round latch, not from the live
        ``active_round``: the tail of a user round is still forwarded after the
        scheduler started the goal round, and stamping it as goal output splits
        the bubble in the wrong place and demotes the user round's own final.

        First visible goal token also flushes a deferred Goal-objective history
        row so reload order matches live (after the interrupted user turn).
        """
        kind = self._consumed_round_run_kind()
        prev = self._stream_content_run_kind
        boundary: dict[str, Any] | None = None
        switched_from_user = prev == "user" and kind == "goal"
        hijacked_before_user_token = (
            stream_is_user_originated and kind == "goal" and prev is None
        )
        if kind == "goal" and session_id:
            self._flush_pending_goal_objective_history(session_id)
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
        if self._goal_intermediate_final_repeats_streamed_text(
            str(parsed.get("content") or "")
        ):
            # Drop it: the bubble (and therefore history) already holds this
            # text. Emitting it as a delta would append a second copy, because
            # only a real chat.final replaces the bubble content.
            return None
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
                session_adapter = await self._get_or_create_session_adapter(request.session_id)
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
        # Stop the scheduler execution before asking the interaction owner to
        # cancel the round.  In particular, task_tool awaits its subagent in
        # the scheduler's exec task; cancelling only the interaction round can
        # otherwise block behind that child work and never return an
        # interrupt_result to the TUI.  This does not cancel the registered
        # session stream producer -- cancel_round still owns that lifecycle.
        self._cancel_scheduler_running_tasks()
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
        finally:
            # A scheduler task may be installed while cancel_round is
            # unwinding; repeat the targeted cancellation as a safety net.
            self._cancel_scheduler_running_tasks()
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

    async def abort_on_gateway_disconnect(
        self,
        *,
        exclude_session_ids: set[str] | None = None,
    ) -> None:
        """Abort disconnected Gateway work while preserving internal Heartbeats."""
        protected = set(exclude_session_ids or ())
        if self._is_session_scoped_adapter:
            if self._session_adapter_key(self._parent_session_id) in protected:
                return
        else:
            for session_id, adapter in list(self._session_adapters.items()):
                if session_id not in protected:
                    await adapter.abort_on_gateway_disconnect(
                        exclude_session_ids=protected
                    )

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

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        """Handle chat.user_answer request."""
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(request.session_id)
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
            session_adapter = await self._get_or_create_session_adapter(request.session_id)
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
        """Answer a HealthCheck probe without executing workspace user tasks."""
        sid = str(request.session_id or "")
        if not sid.startswith("health_check_"):
            return None

        logger.info(
            "[JiuWenSwarmDeepAdapter] health check acknowledged: "
            "request_id=%s session_id=%s",
            request.request_id,
            request.session_id,
        )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"health_check": "HEALTH_CHECK_OK"},
            metadata=request.metadata,
        )

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
            await approve_evolution_records(
                rail,
                request_id,
                approved_record_ids,
                legacy_fallback=True,
            )
            logger.info("[JiuWenSwarmDeepAdapter] evolution approval accepted: request_id=%s", request_id)
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
            pop_continuation = getattr(rail, "pop_approval_continuation", None)
            continuation = (
                pop_continuation(request_id)
                if callable(pop_continuation)
                else None
            )
            if continuation:
                from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

                delivered, reason = await get_team_manager(channel_id).interact(
                    session_id,
                    continuation,
                )
                if not delivered:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] approved evolution continuation "
                        "failed: request_id=%s reason=%s",
                        request_id,
                        reason,
                    )
            # Skills live in one physical library, so no workspace links need
            # refreshing after the approval has been persisted.
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
            # No library-side fan-out is needed: the single Skill library is re-read by
            # SkillUseRail on every model call.
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
        # 各自保留各自的旧放行集合，只追加新三段命名等价串：旧 {agent, agent.plan}
        # + 新 {agent.work.normal, agent.work.plan}。注意本闸门旧集合不含 team；
        # code 系 / team 系（含 team.work.*）均不放行。不用 deprecate_mode，因为
        # 它会把 agent.fast 归一到 agent.work.normal 而误放行。
        if mode not in {
            "agent", "agent.plan",
            "agent.work.normal", "agent.work.plan",
        }:
            display_mode = str(mode or "当前").strip() or "当前"
            return f"{display_mode} 模式下演进功能不可用。"
        if not get_skill_evolution_enabled(self._config_base_cache or self._config_cache):
            return "演进功能未启用。"
        await self._ensure_active_evolution_rails_registered()
        if self._skill_evolution_rail is None:
            return "演进功能初始化失败。"

        if self._skill_create_rail is None:
            self._skill_create_rail = self._build_skill_create_rail(
                self._config_base_cache or self._config_cache
            )
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

        slash_result = await handle_evolution_slash_command(
            stripped,
            EvolutionSlashContext(
                mode=mode,
                session_id=session_id,
                skills_dir=str(get_agent_skills_dir()),
                evolution_enabled=get_skill_evolution_enabled(
                    self._config_base_cache or self._config_cache
                ),
                language=self._resolve_runtime_language(),
            ),
        )
        if slash_result is not None:
            return evolution_slash_result(
                evolution_slash_command_name(stripped),
                slash_result,
                warning_phrases=REGULAR_EVOLUTION_SLASH_WARNING_PHRASES,
            )

        return None

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

    def _session_has_other_running_agent_tasks(self, session_id: str) -> bool:
        """同 session 是否还有「不是当前 task」的未完成 agent 流。"""
        sid = self._resolve_interrupt_session_id(session_id or "default")
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        tasks = getattr(self, "_session_agent_tasks", {}).get(sid) or set()
        if not tasks:
            return False
        return any((task is not current) and (not task.done()) for task in tasks)

    def _should_defer_goal_objective_history(self, session_id: str) -> bool:
        """忙碌插队：上一轮 user 仍在，或同 session 另有并发任务（如 chat.send）。"""
        if self._current_interaction_run_kind() == "user":
            return True
        if self._stream_content_run_kind == "user" or self._stream_round_kind_latch == "user":
            return True
        sid = self._resolve_interrupt_session_id(session_id)
        # 本 stream 已 _mark_session_active，count>1 表示还有别的请求在跑
        if self._active_session_ids.get(sid, 0) > 1:
            return True
        # chat 持有 output lease 时 goal set 仍会 mark；再看其它未完成 task
        return self._session_has_other_running_agent_tasks(sid)

    def _flush_pending_goal_objective_history(
        self, session_id: str, *, timestamp: float | None = None
    ) -> None:
        """把挂起的 Goal 用户历史落到磁盘；无挂起则 noop。"""
        sid = self._resolve_interrupt_session_id(session_id or "default")
        pending = _pending_goal_objective_history.pop(sid, None)
        if not pending:
            return
        append_history_record(
            timestamp=float(timestamp if timestamp is not None else time.time()),
            **pending,
        )

    def _record_goal_set_history_if_needed(
        self,
        request: AgentRequest,
        *,
        action: str | None,
        result_type: str | None,
        goal_payload: dict[str, Any] | None,
        defer: bool | None = None,
    ) -> None:
        """成功 set 后写入 objective 用户历史；忙碌时推迟到上一轮收尾后再写。"""
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
        sid = request.session_id or "default"
        record_kwargs: dict[str, Any] = {
            "session_id": sid,
            "request_id": request.request_id,
            "channel_id": request.channel_id,
            "role": "user",
            "content": objective,
            "channel_metadata": request.metadata,
            "mode": params.get("mode", "unknown"),
            "extra": {
                "goal_id": goal_id,
                "is_goal_objective_message": True,
            },
        }
        should_defer = (
            defer if defer is not None else self._should_defer_goal_objective_history(sid)
        )
        resolved = self._resolve_interrupt_session_id(sid)
        if should_defer:
            _pending_goal_objective_history[resolved] = record_kwargs
            return
        _pending_goal_objective_history.pop(resolved, None)
        append_history_record(timestamp=time.time(), **record_kwargs)

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
    ) -> dict[str, Any] | None:
        """Map JiuwenSwarm protocol fields to the independent Goal methods."""
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(session_id)
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
        params: dict[str, Any] | None,
        session_id: str = "default",
    ) -> dict[str, Any] | None:
        """Structured ``command.goal`` endpoint."""
        raw = params if isinstance(params, dict) else {}
        objective = raw.get("objective")
        return await self._dispatch_goal_control(
            action=str(raw.get("action", "get")),
            objective=objective if isinstance(objective, str) else None,
            overwrite_confirmed=bool(raw.get("overwrite_confirmed", False)),
            token_budget=raw.get("token_budget"),
            max_attempts=raw.get("max_attempts"),
            session_id=session_id,
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
                session = create_agent_session(
                    session_id=session_id,
                    card=getattr(self._instance, "card", None),
                )
                await modify_tool.invoke(
                    {"action": "cancel", "ids": ids_to_cancel},
                    session=session,
                )
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
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id,
                history_before_request_id=request.request_id,
            )
            try:
                return await session_adapter.process_message_impl(request, inputs)
            finally:
                await self._evict_idle_session_adapters()

        if self._instance is None:
            raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")

        _req_model = self._requested_model_name(request)
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

        equipment_error = await self._ensure_chat_extensions(request)
        if equipment_error is not None:
            return equipment_error

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
            project_dir=(request.params.get("project_dir") if isinstance(request.params, dict) else None),
            user_id=getattr(request, "user_id", None),
        )
        self._runtime_cron_tool_context.remember_current_binding()
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)
        resolved_model = self._resolve_model_for_request(request)
        self._apply_model_to_react_agent(
            resolved_model,
            session_id=request.session_id,
        )
        self._mark_session_active(session_id)
        self._register_session_agent_task(session_id)
        if self._stream_event_rail is not None:
            self._stream_event_rail.reset_abort(session_id)
        image_files_token = None
        _run_span: Any = None
        collected_content: list[str] = []
        error_text: str | None = None
        interaction_stream = None
        interaction_stream_abort = True
        # 提前 import 观测 span 工具：原 import 在 try 内 _sync_prompt_attachments
        # 之后，若该处抛异常，finally 的 close_agent_run_span 会因名字未绑定
        # 抛 UnboundLocalError，掩盖真因。提前到函数顶部规避（见 traceback 8111）。
        from jiuwenswarm.agents.harness.agent_observability import (  # noqa: E402
            close_agent_run_span,
            mark_single_agent_team,
            open_agent_run_span,
            sync_agent_observability,
        )
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
                    eternal_conversation_enabled=self._resolve_eternal_conversation_enabled(
                        request.params
                    ),
                    interaction_resume=self._is_eternal_interaction_resume(request.params),
                )
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
                vision_tool_available=(
                    getattr(self, "_vision_model_config", None) is not None
                ),
            )
            from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                set_current_multimodal_image_files,
            )

            image_files_token = set_current_multimodal_image_files(
                inputs.pop("_multimodal_image_files", []) or []
            )
            # Sync single-agent / coding-agent observability with current
            # config before running, and open a root span so OtelCallbackHandler
            # has a parent for LLM/tool spans (see streaming path for details).
            sync_agent_observability()
            mark_single_agent_team(self._instance)
            from jiuwenswarm.server.runtime.debug_trace.paths import (
                resolve_debug_trace_mode,
            )

            _run_span = open_agent_run_span(
                session_id=session_id,
                mode=resolve_debug_trace_mode(
                    mode, getattr(request, "_original_mode", None)
                ),
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
                    # openjiuwen emits terminal model failures as an ``answer``
                    # chunk whose error text is in ``payload.output`` and whose
                    # ``result_type`` is ``error``.  Do not let that compatible
                    # representation fall through as a successful empty unary
                    # response (notably for cron runs).
                    if (
                        chunk.type == "answer"
                        and isinstance(chunk.payload, dict)
                        and chunk.payload.get("result_type") == "error"
                    ):
                        output = chunk.payload.get("output") or ""
                        if isinstance(output, dict):
                            output = output.get("output") or output.get("message") or ""
                        error_text = str(output) if output else "task failed"
                        continue
                    if chunk.type in ("llm_output", "answer"):
                        text = (
                            chunk.payload.get("content", "")
                            if isinstance(chunk.payload, dict)
                            else str(chunk.payload)
                        )
                        if text:
                            collected_content.append(text)
                    else:
                        # check for error in other typed chunks (e.g. controller_output.task_failed)
                        parsed = self._parse_stream_chunk(chunk, _parent_session_id=self._parent_session_id)
                        if parsed is not None:
                            event_type = str(parsed.get("event_type") or "").strip()
                            # execution.error（DeepAgent round 级异常，如模型调用失败）
                            # 与 chat.error / error 一样视为终端失败，透传其 message。
                            if event_type in ("chat.error", "error", ERROR_EVENT_TYPE):
                                err = parsed.get("error") or parsed.get("message") or ""
                                if err:
                                    error_text = str(err)
                else:
                    parsed = self._parse_stream_chunk(chunk, _parent_session_id=self._parent_session_id)
                    if parsed is not None:
                        text = parsed.get("content", "")
                        if text:
                            collected_content.append(text)
            interaction_stream_abort = False
        except asyncio.CancelledError:
            logger.info(
                "[JiuWenSwarmDeepAdapter] Agent 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                session_id,
            )
            raise
        except Exception as e:
            logger.error("[JiuWenSwarmDeepAdapter] Agent 任务执行异常: %s", e)
            raise
        finally:
            close_agent_run_span(
                _run_span,
                session_id=session_id,
                output="".join(collected_content),
            )
            if image_files_token is not None:
                from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                    reset_current_multimodal_image_files,
                )

                reset_current_multimodal_image_files(image_files_token)
            if interaction_stream is not None:
                try:
                    await interaction_stream.close(
                        abort_active_round=interaction_stream_abort,
                    )
                except Exception:
                    logger.debug("[Goal] interaction stream close failed", exc_info=True)
            self._unregister_session_agent_task(session_id)
            TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
            cleanup_permission_context(token_perm)
            self._reset_runtime_cron_context(cron_context_tokens)
            self._unmark_session_active(session_id)

        content = "".join(collected_content) if collected_content else ""

        if error_text:
            # 模型/round 级错误：即使已流出部分内容，也按失败返回并透传错误消息，
            # 避免 cron 等调用方误判为"执行完成但未返回结果"。
            payload: dict[str, Any] = {"error": error_text}
            if content:
                payload["content"] = content
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
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
            session_adapter = await self._get_or_create_session_adapter(
                request.session_id,
                history_before_request_id=request.request_id,
            )
            try:
                async for chunk in session_adapter.process_message_stream_impl(request, inputs):
                    yield chunk
                return
            finally:
                await self._evict_idle_session_adapters()

        if self._instance is None:
            raise RuntimeError("JiuWenSwarmDeepAdapter 未初始化，请先调用 create_instance()")

        _req_model = self._requested_model_name(request)
        if not self._has_valid_model_config(_req_model):
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
        mode = request.params.get("mode", "agent")

        # Team 模式处理
        if is_team_mode(mode):
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                bind_team_heartbeat_service,
                process_team_message_stream,
                reset_team_heartbeat_service,
            )

            resolved_model = self._resolve_model_for_request(request)
            self._apply_model_to_react_agent(
                resolved_model,
                session_id=request.session_id,
            )
            # Images take the single-agent path: native input rides the
            # ``_CURRENT_MULTIMODAL_IMAGE_FILES`` ContextVar into each member's
            # MultimodalImageRail (members mount ``swarm.multimodal_image``),
            # and models without native image input fall back to the
            # image-reading tool prompt. The ContextVar is set here, before
            # team_helpers spawns the stream task, so the task inherits it.
            #
            # The pipeline runs against the user's own words rather than
            # ``inputs["query"]``: team delivers ``turn.text`` re-rendered, so a
            # hint appended to the rendered envelope would be dropped.
            team_turn = inputs.get(TEAM_USER_TURN_KEY)
            rewrite_turn_text = isinstance(team_turn, UserTurn) and isinstance(team_turn.text, str)
            rendered_query = inputs.get("query")
            if rewrite_turn_text:
                inputs["query"] = team_turn.text
            inputs = self._prepare_multimodal_image_inputs(request, inputs)
            enable_read_image_multimodal = self._native_image_input_enabled(
                self._config_cache,
                resolved_model,
            )
            image_tool_fallback_notice = self._build_image_tool_fallback_notice(
                request,
                enable_read_image_multimodal=enable_read_image_multimodal,
                model=resolved_model,
                vision_tool_available=(
                    getattr(self, "_vision_model_config", None) is not None
                ),
            )
            inputs = self._prepare_react_image_tool_prompt(
                request,
                inputs,
                enable_read_image_multimodal=enable_read_image_multimodal,
                vision_tool_available=(
                    getattr(self, "_vision_model_config", None) is not None
                ),
            )
            if rewrite_turn_text:
                inputs[TEAM_USER_TURN_KEY] = team_turn.with_text(inputs["query"])
                inputs["query"] = rendered_query
            if image_tool_fallback_notice is not None:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=image_tool_fallback_notice,
                    is_complete=False,
                )
            from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                set_current_multimodal_image_files,
            )

            image_files_token = set_current_multimodal_image_files(
                inputs.pop("_multimodal_image_files", []) or []
            )
            # Same permission bindings the single agent installs. Both are
            # ContextVars, and ``asyncio.create_task`` snapshots the context, so
            # the long-lived team stream task keeps the values this request set
            # even after the resets below run at request end.
            token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
            token_perm = setup_permission_context(request)
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

            token_heartbeat_service = bind_team_heartbeat_service(
                getattr(self, "_heartbeat_service", None)
            )
            try:
                async for chunk in process_team_message_stream(
                    request,
                    inputs,
                    self._instance,
                ):
                    yield chunk
            finally:
                from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                    reset_current_multimodal_image_files,
                )

                reset_current_multimodal_image_files(image_files_token)
                reset_team_heartbeat_service(token_heartbeat_service)
                TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
                cleanup_permission_context(token_perm)
            return

        # Auto-Harness 模式处理
        if mode == "auto_harness":
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
                    eternal_conversation_enabled=self._resolve_eternal_conversation_enabled(
                        request.params
                    ),
                    interaction_resume=self._is_eternal_interaction_resume(request.params),
                )
            )

            activate_response = request.params.get("activate_response")
            if isinstance(activate_response, dict):
                async for chunk in self._auto_harness_service.resume_activate(
                    session_id, rid, cid, activate_response
                ):
                    yield chunk
                return

            resolved_model = self._resolve_model_for_request(request)
            if self._auto_harness_service.is_activate_only_request(request, query):
                async for chunk in self._auto_harness_service.run_activate_only(
                    request, session_id, rid, query, model=resolved_model
                ):
                    yield chunk
                return
            if self._auto_harness_service.is_implement_only_request(request, query):
                async for chunk in self._auto_harness_service.run_implement_only(
                    request, session_id, rid, query, model=resolved_model
                ):
                    yield chunk
                return

            async for chunk in self._auto_harness_service.run(
                request, session_id, rid, query=query, model=resolved_model
            ):
                yield chunk
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
        # The run's final answer for the OTel trace output, kept as the streamed
        # deltas plus the terminal chat.final — see ``_assemble_run_answer``.
        run_answer_deltas: list[str] = []
        run_answer_final = ""

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

        def note_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal had_assistant_output, emitted_terminal_chat_final
            nonlocal run_answer_final
            event_type = payload.get("event_type")
            if event_type in ("chat.delta", "chat.reasoning", "chat.final"):
                had_assistant_output = True
            if event_type == "chat.delta":
                # Single choke point for forwarded text: memo it so a demoted
                # goal attempt final can skip text the bubble already shows.
                self._note_round_visible_text(str(payload.get("content") or ""))
            if event_type == "chat.final":
                emitted_terminal_chat_final = True
            # Assemble the run's final answer for the OTel trace output. This is
            # the one choke point every assistant-visible payload passes through,
            # so collecting it here needs no change at the yield sites.
            if event_type in ("chat.delta", "chat.final"):
                text = payload.get("content")
                if isinstance(text, str) and text:
                    if event_type == "chat.delta":
                        run_answer_deltas.append(text)
                    else:
                        run_answer_final = text
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

        equipment_error = await self._ensure_chat_extensions(request)
        if equipment_error is not None:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": (
                        equipment_error.payload.get("error", "equipment error")
                        if isinstance(equipment_error.payload, dict)
                        else "equipment error"
                    ),
                },
                is_complete=True,
                metadata=request.metadata if isinstance(request.metadata, dict) else {},
            )
            return

        cron_context_tokens = self._bind_runtime_cron_context(
            channel_id=request.channel_id,
            session_id=request.session_id,
            metadata=request.metadata,
            request_id=request.request_id,
            mode=mode,
            project_dir=(request.params.get("project_dir") if isinstance(request.params, dict) else None),
            user_id=getattr(request, "user_id", None),
        )
        self._runtime_cron_tool_context.remember_current_binding()
        token_cid = TOOL_PERMISSION_CHANNEL_ID.set((request.channel_id or "").strip())
        token_perm = setup_permission_context(request)
        # 按请求选择模型
        resolved_model = self._resolve_model_for_request(request)
        self._apply_model_to_react_agent(
            resolved_model,
            session_id=request.session_id,
        )
        self._mark_session_active(session_id)
        self._register_session_agent_task(session_id)
        stream_consumer_cancelled = False
        image_files_token = None
        _run_span: Any = None
        _debug_logger = None
        _debug_trace_token = None  # reset token for the ContextVar-bound logger
        interaction_stream = None
        interaction_stream_abort = True
        # 提前 import 观测 span 工具（同 7382 处理由）：原 import 在 try 内
        # _sync_prompt_attachments 之后，该处异常会让 finally 的
        # close_agent_run_span 因名字未绑定抛 UnboundLocalError，掩盖真因。
        from jiuwenswarm.agents.harness.agent_observability import (  # noqa: E402
            close_agent_run_span,
            mark_single_agent_team,
            open_agent_run_span,
            sync_agent_observability,
        )
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
                    eternal_conversation_enabled=self._resolve_eternal_conversation_enabled(
                        request.params
                    ),
                    interaction_resume=self._is_eternal_interaction_resume(request.params),
                )
            )
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
                vision_tool_available=(
                    getattr(self, "_vision_model_config", None) is not None
                ),
            )
            inputs = self._prepare_react_image_tool_prompt(
                request,
                inputs,
                enable_read_image_multimodal=enable_read_image_multimodal,
                vision_tool_available=(
                    getattr(self, "_vision_model_config", None) is not None
                ),
            )
            if image_tool_fallback_notice is not None:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=image_tool_fallback_notice,
                    is_complete=False,
                )
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
            from jiuwenswarm.server.runtime.debug_trace.paths import (
                debug_trace_file,
                resolve_debug_trace_mode,
            )
            _debug_trace_mode = resolve_debug_trace_mode(
                mode,
                getattr(request, "_original_mode", None),
            )
            _dbg_settings = resolve_debug_trace_settings(
                mode=_debug_trace_mode,
                request_debug=bool(inputs.get("_request_debug")),
            )
            # Sync single-agent / coding-agent observability with current config
            # before running.
            sync_agent_observability(force=_dbg_settings.otel_enabled)
            mark_single_agent_team(self._instance)
            _run_span = open_agent_run_span(
                session_id=session_id, mode=_debug_trace_mode
            )
            _otel_trace_id = ""
            _otel_span_id = ""
            if _run_span is not None:
                try:
                    _span_ctx = _run_span.get_span_context()
                    _otel_trace_id = format(_span_ctx.trace_id, "032x")
                    _otel_span_id = format(_span_ctx.span_id, "016x")
                except Exception:
                    pass
            try:
                from jiuwenswarm.server.runtime.debug_trace.context import (
                    register_debug_trace_logger,
                    set_debug_trace_logger,
                )
                from jiuwenswarm.server.runtime.debug_trace.stream_logger import (
                    DebugTraceLogger,
                )
                if _dbg_settings.enabled and _dbg_settings.dump_enabled:
                    _debug_logger = DebugTraceLogger(
                        file_path=debug_trace_file(_debug_trace_mode, session_id),
                        mode=_debug_trace_mode,
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
                # dispatch 前采样：之后 active_round 可能已切到 goal
                defer_goal_history = self._should_defer_goal_objective_history(session_id)
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
                    defer=defer_goal_history,
                )
                # Only keep the lease when set/resume left an ACTIVE goal to run.
                if result_type != "goal_stream":
                    # 控制类结果（未进入 goal 执行）：挂起历史立刻落盘
                    self._flush_pending_goal_objective_history(session_id)
                    if interaction_stream is not None:
                        await interaction_stream.close(abort_active_round=False)
                        interaction_stream = None
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
                if interaction_stream is None:
                    # chat.send 仍持有 output lease 时 attach_output 会返回 None；
                    # goal 正文会从那条流继续吐。若此处 flush，用户目标历史时间戳
                    # 仍停在 set 瞬间，重载顺序必错（见 web_19fd08* 会话）。
                    # 忙碌推迟时留给持有 lease 的流在 user→goal 边界再落盘。
                    if not defer_goal_history:
                        self._flush_pending_goal_objective_history(session_id)
                    async for chunk in _yield_runtime_accepted():
                        yield chunk
                    interaction_stream_abort = False
                    return
            elif goal_stream_request or attach_goal_request:
                defer_goal_history = self._should_defer_goal_objective_history(session_id)
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
                    defer=defer_goal_history,
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
            # A previous consumer may have stopped mid-round; this stream must
            # sample the run kind again on its own first chunk.
            self._reset_round_kind_latch()
            async for chunk in interaction_stream:
                self._track_round_output_boundary(chunk)
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
                # Failure detection also controls whether closing the output
                # lease aborts the active round; it must not depend on /debug.
                if run_failure is None:
                    run_failure = self._run_failure(chunk)
                if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
                    parsed = self._parse_stream_chunk(chunk, _parent_session_id=self._parent_session_id)
                    # Only stamp provenance / inject split on new visible deltas.
                    # A late user-round chat.final must keep the prior content kind.
                    if isinstance(parsed, dict) and parsed.get("event_type") == "chat.delta":
                        boundary = self._begin_visible_chat_content(
                            stream_is_user_originated, session_id=session_id
                        )
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
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(parsed),
                            is_complete=False,
                        )
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
                    boundary = self._begin_visible_chat_content(
                        stream_is_user_originated, session_id=session_id
                    )
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
                    boundary = self._begin_visible_chat_content(
                        stream_is_user_originated, session_id=session_id
                    )
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
                            _parent_session_id=self._parent_session_id,
                        )
                        parsed = self._adapt_goal_intermediate_final(parsed)
                        if parsed is not None:
                            if should_skip_duplicate_ask_user(parsed):
                                continue
                            if parsed.get("event_type") == "chat.final":
                                self._stream_content_run_kind = None
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=cid,
                                payload=note_chat_payload(parsed),
                                is_complete=False,
                            )
                        continue
                    parsed = self._parse_stream_chunk(chunk, _parent_session_id=self._parent_session_id)
                    parsed = self._adapt_goal_intermediate_final(parsed)
                    if parsed is not None:
                        if should_skip_duplicate_ask_user(parsed):
                            continue
                        if parsed.get("event_type") == "chat.final":
                            self._stream_content_run_kind = None
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=note_chat_payload(parsed),
                            is_complete=False,
                        )
                    continue

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
                parsed = self._parse_stream_chunk(chunk, _parent_session_id=self._parent_session_id)
                parsed = self._adapt_goal_intermediate_final(parsed)
                if parsed is not None:
                    if should_skip_duplicate_ask_user(parsed):
                        continue
                    if parsed.get("event_type") == "chat.final":
                        self._stream_content_run_kind = None
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=note_chat_payload(parsed),
                        is_complete=False,
                    )

            if accumulated_text:
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
            if accumulated_reasoning:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload({"event_type": "chat.reasoning", "content": accumulated_reasoning}),
                    is_complete=False,
                )

            # pause→clear (and similar): round cancelled, iterator ends without
            # a model chat.final. Synthesize a real final so the frontend can
            # stopStreaming; do not demote.
            if run_failure is None and self._should_emit_stream_end_chat_final(
                had_assistant_output=had_assistant_output,
                emitted_terminal_chat_final=emitted_terminal_chat_final,
            ):
                self._stream_content_run_kind = None
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=cid,
                    payload=note_chat_payload({
                        "event_type": "chat.final",
                        "content": "",
                    }),
                    is_complete=False,
                )

            if (
                self._skill_evolution_rail is not None
                and self._skill_evolution_rail.signal_trigger
                and not self._skill_evolution_rail.auto_save
            ):
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
            interaction_stream_abort = run_failure is not None
        except asyncio.CancelledError:
            stream_consumer_cancelled = True
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
            raise
        except Exception as exc:
            logger.exception("[JiuWenSwarmDeepAdapter] 流式任务异常: %s", exc)
            if _debug_logger is not None:
                _debug_logger.end_run(status="error", error=exc)
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
            # 兜底落盘：仅当没有其它并发流可接管时才 flush。
            # goal set 因 lease 被占而早退时，chat 流还在，不能在这里落盘。
            if not self._session_has_other_running_agent_tasks(session_id):
                self._flush_pending_goal_objective_history(session_id)
            close_agent_run_span(
                _run_span,
                session_id=session_id,
                output=_assemble_run_answer(run_answer_deltas, run_answer_final),
            )
            if _debug_logger is not None:
                _debug_logger.flush()
            if _debug_trace_token is not None:
                from jiuwenswarm.server.runtime.debug_trace.context import (
                    reset_debug_trace_logger,
                    unregister_debug_trace_logger,
                )
                reset_debug_trace_logger(_debug_trace_token)
                unregister_debug_trace_logger(session_id)
            if image_files_token is not None:
                from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
                    reset_current_multimodal_image_files,
                )

                reset_current_multimodal_image_files(image_files_token)
            if interaction_stream is not None:
                try:
                    await interaction_stream.close(
                        abort_active_round=interaction_stream_abort,
                    )
                except Exception:
                    logger.debug("[Goal] interaction stream close failed", exc_info=True)
            self._unregister_session_agent_task(session_id)
            TOOL_PERMISSION_CHANNEL_ID.reset(token_cid)
            cleanup_permission_context(token_perm)
            if not stream_consumer_cancelled:
                self._reset_runtime_cron_context(cron_context_tokens)
            # Always clean up rail state — process_interrupt's
            # _stop_session_interrupt_work sets abort flags but does NOT
            # call cleanup_session(), so skipping cleanup here would leak
            # _abort_requested / _pause_events entries on long-lived adapters.
            self._unmark_session_active(
                session_id,
                cleanup_rail=True,
            )

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

        logger.info(
            "[JiuWenSwarmDeepAdapter] llm_usage summary: request_id=%s session_id=%s usage=%s",
            rid,
            session_id,
            summary,
        )

        # 从 DeepAgent 获取上下文窗口占用率与窗口大小
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
            logger.debug("[JiuWenSwarmDeepAdapter] DeepAgent.get_context_usage in usage_summary failed", exc_info=True)

        # 回退：DeepAgent 未返回 context_window_tokens 时，用 ContextUtils 解析模型上下文窗口上限
        if context_window_tokens is None:
            try:
                model_name = (
                    getattr(self._model_request_config, "model_name", "") or ""
                    if self._model_request_config else ""
                )
                cw_fallback = resolve_context_window_tokens(
                    model_name=model_name,
                    context_engine_config=self._config_cache,
                    model_context_window_override=self._selected_model_context_window_tokens,
                )
                if cw_fallback > 0:
                    context_window_tokens = cw_fallback
            except Exception:
                logger.debug("[JiuWenSwarmDeepAdapter] ContextUtils.resolve_context_max fallback failed", exc_info=True)

        if usage_accumulator["total_tokens"] > 0:
            payload: dict[str, Any] = {
                "event_type": "chat.usage_summary",
                "session_id": session_id,
                "usage": summary,
                "model": self._resolve_model_name(),
            }
            if context_usage_percent is not None:
                payload["usage_percent"] = context_usage_percent
            if context_window_tokens is not None:
                payload["context_window_tokens"] = context_window_tokens

            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload=payload,
                is_complete=False,
            )

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
    def _run_failure(chunk) -> tuple[str, str] | None:
        """Return ``(error_type, message)`` if *chunk* is a run-level terminal failure.

        These are failures that end the round and are surfaced to the user as a
        ``chat.error`` by :meth:`_parse_stream_chunk` — a
        ``controller_output``/``task_failed`` (e.g. model call failed) or an
        ``answer`` whose ``result_type`` is ``"error"``. Recoverable
        ``tool_result`` errors are intentionally excluded: the agent loop may
        still retry them, so they must not flip the run status.

        It also keeps ``abort_active_round=True`` when the output stream is
        closed after a terminal failure. The chunk still flows through
        :meth:`_parse_stream_chunk` unchanged.
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

    @classmethod
    def _clear_subagent_progress_batch(cls, parent_session_id: str | None) -> None:
        if not parent_session_id:
            return
        with cls._subagent_progress_batches_lock:
            cls._subagent_progress_batches.pop(parent_session_id, None)

    @classmethod
    def _resolve_subagent_parallel_fields(
        cls,
        *,
        parent_session_id: str,
        subagent_id: str,
        legacy_status: str,
    ) -> tuple[int, int, bool]:
        """Assign stable 0-based index/total for legacy Web SubtaskProgress."""
        if not parent_session_id or not subagent_id:
            return 0, 1, False

        with cls._subagent_progress_batches_lock:
            order = cls._subagent_progress_batches.setdefault(parent_session_id, [])

            if legacy_status in ("completed", "error"):
                if subagent_id not in order:
                    return 0, 1, False
                index = order.index(subagent_id)
                total = max(len(order), 1)
                order.remove(subagent_id)
                if not order:
                    cls._subagent_progress_batches.pop(parent_session_id, None)
                return index, total, total > 1

            if subagent_id not in order:
                order.append(subagent_id)
            index = order.index(subagent_id)
            total = len(order)
            return index, total, total > 1

    @staticmethod
    def _resolve_subagent_legacy_status(projection: dict) -> tuple[str, str]:
        """Return (legacy_status, message) for Web SubtaskProgress compatibility."""
        status = str(projection.get("status") or "running")
        closed_reason = projection.get("closed_reason")
        message = ""
        if status == "closed":
            if closed_reason == "failed":
                legacy_status = "error"
                error = projection.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "")
            else:
                legacy_status = "completed"
        elif status == "idle":
            turn_outcome = projection.get("turn_outcome")
            if turn_outcome == "failed":
                legacy_status = "error"
                error = projection.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "")
            else:
                legacy_status = "completed"
        else:
            legacy_status = "starting"
        return legacy_status, message

    @staticmethod
    def _project_subagent_updated_for_web(projection: dict) -> dict:
        """Project runtime subagent_updated for Web without overwriting canonical status."""
        subagent_id = str(projection.get("subagent_id") or "")
        description = (
            str(projection.get("display_name") or "").strip()
            or str(projection.get("task_description") or "").strip()
            or subagent_id
        )
        legacy_status, message = JiuWenSwarmDeepAdapter._resolve_subagent_legacy_status(projection)

        parent_session_id = str(projection.get("parent_session_id") or "")
        index, total, is_parallel = JiuWenSwarmDeepAdapter._resolve_subagent_parallel_fields(
            parent_session_id=parent_session_id,
            subagent_id=subagent_id,
            legacy_status=legacy_status,
        )

        payload = {
            "event_type": "chat.subtask_update",
            **projection,
            "task_id": subagent_id,
            "description": description,
            "legacy_status": legacy_status,
            "index": index,
            "total": total,
            "is_parallel": is_parallel,
        }
        if message:
            payload["message"] = message
        return payload

    @staticmethod
    def _persist_subagent_transcript_message(projection: dict[str, Any]) -> None:
        parent_session_id = str(projection.get("parent_session_id") or "").strip()
        subagent_id = str(projection.get("subagent_id") or "").strip()
        if not parent_session_id or not subagent_id:
            return

        seq = projection.get("seq")
        request_id = f"{subagent_id}:{seq}" if seq is not None else subagent_id
        role = str(projection.get("role") or "assistant")
        event_type = str(projection.get("event_type") or "").strip() or None
        content = str(projection.get("content") or "")
        timestamp_ms = projection.get("at_ms")
        timestamp = float(timestamp_ms) / 1000 if timestamp_ms else time.time()

        extra: dict[str, Any] = {}
        reasoning_content = projection.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            extra["reasoning_content"] = reasoning_content.strip()
        try:
            phase_id = int(projection.get("phase_id") or 0)
        except (TypeError, ValueError):
            phase_id = 0
        if phase_id > 0:
            extra["phase_id"] = phase_id
        nested_extra = projection.get("extra")
        if isinstance(nested_extra, dict):
            extra.update(nested_extra)
        extra["parent_session_id"] = parent_session_id

        append_history_record(
            session_id=parent_session_id,
            subagent_id=subagent_id,
            request_id=request_id,
            channel_id="subagent",
            role=role,
            content=content,
            timestamp=timestamp,
            event_type=event_type if role == "assistant" else None,
            extra=extra or None,
            mode="subagent",
        )

    @staticmethod
    def _persist_subagent_activity(projection: dict[str, Any]) -> None:
        parent_session_id = str(projection.get("parent_session_id") or "").strip()
        subagent_id = str(projection.get("subagent_id") or "").strip()
        if not parent_session_id or not subagent_id:
            return

        task_id = str(projection.get("task_id") or "").strip()
        seq = projection.get("seq")
        if seq is not None:
            activity_key = str(seq)
        else:
            activity_key = str(
                projection.get("activity_id")
                or projection.get("activityId")
                or projection.get("tool_call_id")
                or projection.get("toolCallId")
                or hashlib.sha256(
                    json.dumps(projection, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:16]
            )
        request_id = f"{subagent_id}:activity:{task_id}:{activity_key}"
        timestamp_ms = projection.get("at_ms")
        timestamp = float(timestamp_ms) / 1000 if timestamp_ms is not None else time.time()
        summary = projection.get("summary")
        append_history_record(
            session_id=parent_session_id,
            subagent_id=subagent_id,
            request_id=request_id,
            channel_id="subagent",
            role="assistant",
            content=str(summary or ""),
            timestamp=timestamp,
            event_type="chat.subagent_activity",
            extra={"subagent_activity": dict(projection)},
            mode="subagent",
        )

    @staticmethod
    def _persist_subagent_roster_history(projection: dict, web_payload: dict) -> None:
        parent_session_id = str(projection.get("parent_session_id") or "").strip()
        subagent_id = str(projection.get("subagent_id") or "").strip()
        if not parent_session_id or not subagent_id:
            return

        updated_at_ms = projection.get("updated_at_ms") or projection.get("created_at_ms")
        timestamp = float(updated_at_ms) / 1000 if updated_at_ms else time.time()
        revision = projection.get("revision")
        request_id = (
            f"subagent-roster-{subagent_id}:{revision}"
            if revision is not None
            else f"subagent-roster-{subagent_id}"
        )
        append_history_record(
            session_id=parent_session_id,
            subagent_id=subagent_id,
            request_id=request_id,
            channel_id="subagent",
            role="assistant",
            event_type="chat.subtask_update",
            content=str(web_payload.get("description") or subagent_id),
            timestamp=timestamp,
            extra=web_payload,
            mode="subagent",
        )

    @staticmethod
    def _persist_subagent_activity(projection: dict[str, Any]) -> None:
        parent_session_id = str(projection.get("parent_session_id") or "").strip()
        subagent_id = str(projection.get("subagent_id") or "").strip()
        if not parent_session_id or not subagent_id:
            return

        task_id = str(projection.get("task_id") or "").strip() or "turn"
        seq = projection.get("seq")
        seq_part = str(seq) if seq is not None else str(projection.get("at_ms") or "0")
        request_id = f"{subagent_id}:activity:{task_id}:{seq_part}"
        timestamp_ms = projection.get("at_ms")
        timestamp = float(timestamp_ms) / 1000 if timestamp_ms else time.time()
        activity = {**projection, "parent_session_id": parent_session_id}
        append_history_record(
            session_id=parent_session_id,
            subagent_id=subagent_id,
            request_id=request_id,
            channel_id="subagent",
            role="assistant",
            content=str(projection.get("summary") or ""),
            timestamp=timestamp,
            event_type="chat.subagent_activity",
            extra={"subagent_activity": activity},
            mode="subagent",
        )

    @staticmethod
    def _parse_stream_chunk(
        chunk,
        *,
        _has_streamed_content: bool = False,
        _stage: str = "",
        _parent_session_id: str | None = None,
    ) -> dict | None:
        """将 SDK OutputSchema 转为前端可消费的 payload dict.

        Args:
            chunk: OutputSchema 或 dict
            _has_streamed_content: 是否已通过 llm_output 流式发送过内容
            _stage: 当前阶段名称，用于 auto_harness harness.message 事件

        Returns:
            dict  – 含 event_type 的 payload，或 None（需跳过的帧）。
        """
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
                    return JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.delta", content
                    )

                if chunk_type == "llm_reasoning":
                    content = (
                        (payload.get("content", "") or payload.get("output", ""))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    return JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.reasoning", content
                    )

                if chunk_type == "content_chunk":
                    content = (
                        payload.get("content", "") if isinstance(payload, dict) else str(payload)
                    )
                    return JiuWenSwarmDeepAdapter._stream_text_payload(
                        "chat.delta", content
                    )

                if chunk_type == "answer":
                    if isinstance(payload, dict):
                        if payload.get("result_type") == "error":
                            return {
                                "event_type": "chat.error",
                                "error": payload.get("output", "未知错误"),
                            }
                        output = payload.get("output", {})
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
                                "graph_status",
                                "graph_build",
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

                if chunk_type == "thinking":
                    return {
                        "event_type": "chat.processing_status",
                        "is_processing": True,
                        "current_task": "thinking",
                    }

                if chunk_type == "todo.updated":
                    todos = payload.get("todos", []) if isinstance(payload, dict) else []
                    return {"event_type": "todo.updated", "todos": todos}

                if chunk_type == SUBAGENT_UPDATED_EVENT_TYPE:
                    projection = (
                        payload.get("subagent_updated") if isinstance(payload, dict) else None
                    )
                    if not isinstance(projection, dict):
                        return None
                    web_payload = JiuWenSwarmDeepAdapter._project_subagent_updated_for_web(projection)
                    JiuWenSwarmDeepAdapter._persist_subagent_roster_history(projection, web_payload)
                    return web_payload

                if chunk_type == SUBAGENT_MESSAGE_EVENT_TYPE:
                    projection = (
                        payload.get("subagent_message") if isinstance(payload, dict) else None
                    )
                    if not isinstance(projection, dict):
                        return None
                    JiuWenSwarmDeepAdapter._persist_subagent_transcript_message(projection)
                    return None

                if chunk_type == SUBAGENT_ACTIVITY_EVENT_TYPE:
                    projection = (
                        payload.get("subagent_activity") if isinstance(payload, dict) else None
                    )
                    if not isinstance(projection, dict):
                        return None
                    persist_projection = dict(projection)
                    parent_session_id = str(
                        persist_projection.get("parent_session_id") or _parent_session_id or ""
                    ).strip()
                    if parent_session_id:
                        persist_projection["parent_session_id"] = parent_session_id
                    JiuWenSwarmDeepAdapter._persist_subagent_activity(persist_projection)
                    return {"event_type": "chat.subagent_activity", **projection}

                if chunk_type == "context.usage":
                    usage_payload = normalize_context_usage_payload(payload)
                    if usage_payload is not None:
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
                return JiuWenSwarmDeepAdapter._stream_text_payload(
                    "chat.delta", content
                )

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
                    return {
                        "event_type": "chat.error",
                        "error": chunk.get("output", "未知错误"),
                    }
                output = chunk.get("output", "")
                if output:
                    return {"event_type": "chat.delta", "content": str(output)}
                return None

        except Exception:
            logger.debug("[_parse_stream_chunk] 解析异常", exc_info=True)

        return None

    @staticmethod
    def parse_stream_chunk(
        chunk,
        *,
        has_streamed_content: bool = False,
        stage: str = "",
        parent_session_id: str | None = None,
    ) -> dict | None:
        """Public entry for OutputSchema stream chunk parsing."""
        return JiuWenSwarmDeepAdapter._parse_stream_chunk(
            chunk,
            _has_streamed_content=has_streamed_content,
            _stage=stage,
            _parent_session_id=parent_session_id,
        )

    @staticmethod
    def project_subagent_updated_for_web(projection: dict) -> dict:
        """Public entry for subagent roster projection."""
        return JiuWenSwarmDeepAdapter._project_subagent_updated_for_web(projection)

    @staticmethod
    def persist_subagent_roster_history(projection: dict, web_payload: dict) -> None:
        JiuWenSwarmDeepAdapter._persist_subagent_roster_history(projection, web_payload)

    @staticmethod
    def persist_subagent_activity(projection: dict[str, Any]) -> None:
        JiuWenSwarmDeepAdapter._persist_subagent_activity(projection)

    @staticmethod
    def persist_subagent_transcript_message(projection: dict[str, Any]) -> None:
        JiuWenSwarmDeepAdapter._persist_subagent_transcript_message(projection)

    @classmethod
    def clear_subagent_progress_batches(cls) -> None:
        with cls._subagent_progress_batches_lock:
            cls._subagent_progress_batches.clear()

    async def _handle_memory_rail_by_config(self, mode: str):
        config = get_config()
        if get_memory_mode(config) == "local":
            # 引擎门禁：memory.engine 未放行内置时，等同于禁用
            builtin_on = (
                not getattr(self, "_eternal_conversation_enabled", False)
                and is_builtin_memory_allowed(config)
                and is_memory_enabled(mode, config)
            )
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
        if is_external_memory_enabled(config) and not getattr(
            self, "_eternal_conversation_enabled", False
        ):
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
            model_name = (
                getattr(self._model_request_config, "model_name", "") or ""
                if self._model_request_config else ""
            )
            context_window_limit = resolve_context_window_tokens(
                model_name=model_name,
                context_engine_config=self._config_cache,
                model_context_window_override=self._selected_model_context_window_tokens,
            )
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

    async def generate_recap(
        self,
        session_id: str,
        current_mode: str | None = None,
    ) -> dict[str, Any]:
        """生成会话快速回顾（read-only，不修改对话历史）。

        取最近30条消息 → fast model → 1-3句摘要。

        Args:
            session_id: 会话 ID。
            current_mode: 触发 recap 时的 canonical runtime mode。
        """
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(session_id)
            try:
                return await session_adapter.generate_recap(
                    session_id=session_id,
                    current_mode=current_mode,
                )
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

        prompt = build_recap_prompt(
            memory=None,
            language=self._resolve_prompt_language(),
            current_mode=current_mode,
        )
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
        if not self._is_session_scoped_adapter:
            session_adapter = await self._get_or_create_session_adapter(session_id)
            try:
                return await session_adapter.generate_btw_answer(
                    session_id=session_id,
                    question=question,
                )
            finally:
                await self._evict_idle_session_adapters()

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

        async def _cleanup_evolution_rail() -> None:
            if self._skill_evolution_rail is None:
                return
            try:
                await self._skill_evolution_rail.cleanup_background_tasks()
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
            if (
                not self._skill_evolution_rail.signal_trigger
                or self._skill_evolution_rail.auto_save
            ):
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
                if (
                    not self._skill_evolution_rail.signal_trigger
                    or self._skill_evolution_rail.auto_save
                ):
                    if active:
                        await _push_status("end", "hidden", "")
                    await _cleanup_evolution_rail()
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
                        await _cleanup_evolution_rail()
                        return
                    await asyncio.sleep(TEAM_EVOLUTION_IDLE_SLEEP_SEC)
                    continue
                last_event_at = time.monotonic()

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
                    return
        except asyncio.CancelledError:
            try:
                await _cleanup_evolution_rail()
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
            # _dreaming_mode 在 create_instance 已归一到二元 "agent"/"code"；这里仍用
            # deprecate 归一判定，防御未来某处直接塞入完整 canonical 串（如
            # agent.work.normal）时 output_dir 不会错算成 coding_memory。
            output_name = (
                "memory"
                if deprecate_mode(mode) in (NEW_AGENT_WORK_NORMAL, NEW_AGENT_WORK_PLAN)
                else "coding_memory"
            )
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
    sys_operation: SysOperation | None = None,
) -> SubAgentConfig:
    """将 AgentDefinition 转换为 SubAgentConfig，用于 SubagentRail 注册。

    Args:
        agent_def: 自定义 agent 定义（来自 .jiuwenswarm/agents/*.md）
        model: 父 agent 的 Model 实例（作为默认模型）
        workspace: 工作空间路径
        model_cache: 模型缓存字典（用于按名称查找指定模型）
        sys_operation: 父 agent 的 SysOperation，子 agent 沿用它以保持同一文件系统
            边界。``DeepAgent.create_subagent`` 只在 ``spec.workspace`` 同时非空时
            才采纳 ``spec.sys_operation``，所以 workspace 也要一并写进 spec；否则子
            agent 会拿到一个受 ``restrict_to_sandbox`` 约束的新 LOCAL SysOperation。
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

    return SubAgentConfig(
        agent_card=card,
        system_prompt=agent_def.prompt,
        tools=tools,
        model=resolved_model,
        workspace=workspace,
        sys_operation=sys_operation,
        skills=agent_def.skills,
        max_iterations=agent_def.max_iterations,
        enable_task_loop=True,
    )


def _load_custom_subagents(
    *,
    workspace_dir: str,
    subagents_cfg: dict | None,
    model: Any,
    workspace: str,
    logger_name: str,
    model_cache: dict[str, Any] | None = None,
    sys_operation: SysOperation | None = None,
) -> list[Any]:
    """从 AgentConfigService 加载自定义 agent 并转换为 SubAgentConfig 列表。

    通用逻辑，同时被 JiuWenSwarmDeepAdapter 和 JiuWenSwarmCodeAdapter 使用。

    Args:
        workspace_dir: 工作空间目录路径
        subagents_cfg: 子 agent 配置字典
        model: 模型配置
        workspace: 工作空间路径
        logger_name: 日志记录器名称
        model_cache: 模型缓存字典（用于按名称查找指定模型）
        sys_operation: 父 agent 的 SysOperation，自定义子 agent 沿用它以保持同一文件
            系统边界
    """
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    _logger = logging.getLogger(logger_name)
    agent_service = AgentConfigService(workspace_dir)
    result: list[Any] = []
    for agent_def in agent_service.list_agents():
        if agent_def.source == "builtin":
            continue
        subagent_cfg = subagents_cfg.get(agent_def.name) if isinstance(subagents_cfg, dict) else None
        # 只有显式 enabled: true 才加载
        if not (isinstance(subagent_cfg, dict) and bool(subagent_cfg.get("enabled", False))):
            continue
        custom_spec = _agent_def_to_subagent_config(
            agent_def,
            model,
            workspace,
            model_cache,
            sys_operation,
        )
        custom_spec.factory_kwargs = {"auto_create_workspace": False}
        result.append(custom_spec)
        _logger.info("loaded custom agent '%s' from %s", agent_def.name, agent_def.source)
    return result
