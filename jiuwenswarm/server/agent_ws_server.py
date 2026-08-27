# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentWebSocketServer - Gateway 与 AgentServer 之间的 WebSocket 服务端."""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import logging
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, ClassVar, Optional
from weakref import WeakValueDictionary

from openjiuwen.core.common.logging import server_logger
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService, reset_harness_packages_state
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.ws_send import send_wire_payload
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager
from jiuwenswarm.common.utils import get_agent_sessions_dir, get_config_file, mask_sensitive
from jiuwenswarm.common.todo_snapshot import load_todo_snapshot_for_frontend
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
    E2A_WIRE_INTERNAL_METADATA_KEYS,
)
from jiuwenswarm.common.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.chat_send import PLAN_ENTRY_SOURCES
from jiuwenswarm.common.version import __version__
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import persist_cli_trusted_directory
from jiuwenswarm.extensions.hooks_context import AgentServerChatHookContext
from jiuwenswarm.server.runtime.agent_manager import AgentManager, ACP_DEFAULT_CAPABILITIES
from jiuwenswarm.server.runtime.agent_warm_pool import WarmClaim
from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata, remove_session_metadata_cache
from jiuwenswarm.server.runtime.session.session_history import (
    append_compact_history_records,
    history_exists,
    is_valid_session_id,
    load_history_records,
    read_member_history_records,
    read_team_history_records,
)
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
    build_yuanrong_sandbox_status_view,
    effective_files_from_policy,
    find_auto_managed_match,
    find_nested_files_conflict,
    list_effective_sandbox_files,
    validate_sandbox_files_runtime,
)
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.common.mode_matrix import (
    ResolvedMode,
    TEAM_PLAN_CODE_MODE,
    TEAM_PLAN_NORMAL_MODE,
    canonicalize_mode_text,
    is_code_profile_mode,
    is_plan_mode,
    is_team_mode,
    resolve_new_canonical_mode,
    resolve_request_mode,
)
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    get_permissions_config_req_methods,
)
from jiuwenswarm.common.config import (
    DEFAULT_SANDBOX_POLICY_FILE,
    DEFAULT_SANDBOX_STARTUP_MODE,
    get_config,
    get_default_models,
    get_mcp_server_config,
    get_mcp_servers,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    get_sandbox_startup_mode,
    get_sandbox_startup_mode_explicit,
    remove_mcp_server,
    resolve_preserve_file_sharing_mode_default,
    resolve_sandbox_policy_path,
    remove_subagent_from_config,
    set_mcp_server_enabled,
    update_sandbox_endpoint,
    update_sandbox_runtime,
    upsert_mcp_server,
    upsert_subagent_in_config,
)
from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner
from jiuwenswarm.common.hooks_config import load_hooks_config
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_origin_check_enabled,
    is_allowed_browser_origin,
)
from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_MODE_EXITED_EVENT_TYPE,
    PLAN_REMINDER_ORIGINAL_QUERY_KEY,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.personal_context import PersonalContextHostAPI
from jiuwenswarm.server.personal_context.ws_handler import (
    PERSONAL_CONTEXT_REQUEST_METHODS as _PERSONAL_CONTEXT_REQ_METHODS,
    handle_personal_context_request,
)
from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.server.runtime.gateway_adapter import (
    AdapterRegistry,
    ConfigAdapter,
    HarmonyOSAdapter,
    MemoryAdapter,
    ProjectAdapter,
    SessionAdapter,
    WorkspaceFileAdapter,
)

logger = logging.getLogger(__name__)


# These handlers also perform AgentServer process-lifecycle cleanup that the
# neutral adapters deliberately do not own.  Keep their established branches.
_GATEWAY_ADAPTER_LEGACY_METHODS = frozenset({
    ReqMethod.SESSION_DELETE,
    ReqMethod.SESSION_RENAME,
})


def _parse_single_byte_range(
    range_header: str, file_size: int
) -> tuple[int, int] | None:
    """Parse a single HTTP byte range for file-download bridge handlers."""
    if file_size <= 0 or not range_header.startswith("bytes=") or "," in range_header:
        return None
    start_text, end_text = range_header[6:].split("-", 1) if "-" in range_header[6:] else ("", "")
    if not start_text:
        if not end_text.isdecimal() or int(end_text) <= 0:
            return None
        return max(0, file_size - int(end_text)), file_size - 1
    if not start_text.isdecimal() or (end_text and not end_text.isdecimal()):
        return None
    start = int(start_text)
    if start >= file_size:
        return None
    end = min(int(end_text), file_size - 1) if end_text else file_size - 1
    return (start, end) if end >= start else None

# 后台权限重载任务引用集合,防止 fire-and-forget 任务被 GC 提前回收。
# task 完成后自动从集合移除(Python 官方推荐模式)。
_background_permission_reload_tasks: set[asyncio.Task] = set()

# Session owner preparation completes before the response. Optional KVC signals
# run after the response so affinity latency cannot fail a UI session change.
_background_session_kvc_tasks: set[asyncio.Task] = set()


def _is_session_prewarm_model_eligible(
    params: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> bool:
    """Return whether session prewarm can safely use the configured default model.

    A prewarmed single-agent child freezes its skill-retrieval budget before the
    first chat request arrives.  Requests for a non-default model must therefore
    create that child on the first chat, when the selected model is available.
    """
    requested = str(params.get("model_name") or "").strip()
    if not requested:
        return True

    resolved_config = config if config is not None else get_config()
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
        is_skill_retrieval_enabled,
    )

    # The global switch promises the unchanged legacy runtime when disabled.
    if not is_skill_retrieval_enabled(resolved_config):
        return True

    entries = get_default_models(resolved_config)
    first_identifiers: set[str] | None = None
    selected_identifiers: set[str] | None = None
    name_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_client_config = entry.get("model_client_config")
        if not isinstance(model_client_config, dict):
            continue
        model_name = str(model_client_config.get("model_name") or "").strip()
        if not model_name:
            continue

        occurrence = name_counts.get(model_name, 0)
        name_counts[model_name] = occurrence + 1
        identifiers = {model_name, f"{model_name}#{occurrence}"}
        alias = str(entry.get("alias") or "").strip()
        if alias:
            identifiers.add(alias)
        if first_identifiers is None:
            first_identifiers = identifiers
        if selected_identifiers is None and entry.get("is_default") is True:
            selected_identifiers = identifiers

    default_identifiers = selected_identifiers or first_identifiers or set()
    return requested in default_identifiers


async def _reset_active_browser_runtimes_if_available(browser_move: Any) -> int:
    """Reset active browser runtimes when supported by the installed SDK."""
    reset_runtimes = getattr(
        browser_move,
        "reset_active_browser_runtimes",
        None,
    )
    if not callable(reset_runtimes):
        logger.warning(
            "[AgentWebSocketServer] installed openjiuwen does not support "
            "reset_active_browser_runtimes; restarting the local browser "
            "runtime server only"
        )
        return 0
    return await reset_runtimes()


async def _reset_requested_browser_runtime_if_available(
    browser_move: Any,
    params: dict[str, Any],
) -> int:
    """Prefer an identity-scoped reset and retain compatibility with older SDKs."""
    reset_runtime = getattr(browser_move, "reset_managed_browser_runtime", None)
    display_mode = str(params.get("display_mode") or "").strip().lower()
    profile_name = str(params.get("profile_name") or "").strip()
    if callable(reset_runtime) and display_mode and profile_name:
        return await reset_runtime(
            browser_key=str(params.get("browser_key") or "").strip(),
            profile_name=profile_name,
            display_mode=display_mode,
            browser_binary=str(params.get("browser_binary") or "").strip(),
        )
    return await _reset_active_browser_runtimes_if_available(browser_move)


def _log_permission_reload_failure(task: asyncio.Task) -> None:
    """后台权限重载任务完成回调: 仅在异常时记 debug(与原同步 try/except 语义一致)。"""
    exc = task.exception()
    if exc is not None:
        logger.debug(
            "[AgentWebSocketServer] post-permissions reload failed (non-critical)",
            exc_info=exc,
        )


def _log_background_session_kvc_failure(task: asyncio.Task) -> None:
    """Log optional post-response KVC failures without changing session state."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[AgentWebSocketServer] %s failed after ack: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )

# Serialize plan-mode restore per session to avoid checkpoint races.
_session_mode_sync_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)

# Serialize switch owner preparation and acknowledgements per client
# connection. AgentServer handles WebSocket frames in independent tasks, so
# rapid navigation requests would otherwise race even on one socket.
_session_switch_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)

# Serialize automatic team creation per session. The lock is weakly held so
# one-shot chat sessions do not accumulate process-lifetime state.
_session_team_binding_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)

# Sessions that have successfully exited plan mode via exit_plan_mode tool.
# Set by _check_post_process_plan_exit, consumed by _ensure_code_mode_state
# to prevent TUI-race re-entrance to plan mode.
_plan_exited_sessions: set[str] = set()

# 本进程内曾进入过 plan 的 work 单 agent 会话。work 的准入面覆盖 IM / 定时任务 /
# CLI / Web work 的每一条普通消息，而其中绝大多数会话从未开过 Plan；有这个标记
# 才需要去同步 plan 状态。跨重启的情况另有一道判据（会话 metadata 里上一轮的
# canonical mode），见 ``_session_may_hold_plan_state``。
_plan_active_sessions: set[str] = set()

# 上一轮写盘前的会话 canonical mode，由 ``_prepare_code_mode_chat_turn`` 在覆盖
# metadata 之前捎带到 params 里，给 ``_ensure_code_mode_state`` 当跨重启判据。
_SESSION_PREVIOUS_MODE_KEY = "_session_previous_mode"

# ``plan_entry_source`` 的合法取值，表示"用户这一条消息明确要求进入 plan"。
# 一次性字段：TUI 的 ``/plan`` 命令、Web 用户手动打开 Plan 开关后的第一条消息。
# 字面量定义在 ``jiuwenswarm.common.schema.chat_send`` 的
# ``PLAN_ENTRY_SOURCES``，前后端共享同一契约，本文件直接引用。
# 跨层契约测试见 ``tests/unit_tests/test_plan_entry_source_contract.py``。

_CODE_MODE_SYNC_METHODS = frozenset({
    ReqMethod.CHAT_SEND,
    ReqMethod.CHAT_RESUME,
    ReqMethod.CHAT_ANSWER,
})

# ── 流式处理心跳间隔：当 Agent 处理时间超过此阈值时，发送心跳 chunk 保持 WebSocket 连接活跃 --
# 避免 ping_timeout 导致连接关闭。默认 10 秒，小于服务端 ping_timeout=20s。
_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0
from jiuwenswarm.server.wire_truncate import (  # noqa: F401  — re-exported for tests / handlers
    _HISTORY_PAGE_SIZE,
    _HISTORY_WIRE_STRING_LIMIT,
    _HISTORY_WIRE_METADATA_STRING_LIMIT,
    _HISTORY_WIRE_LIST_LIMIT,
    _HISTORY_WIRE_DEPTH_LIMIT,
    _HISTORY_WIRE_RECORD_MAX_BYTES,
    _TEAM_HISTORY_DEFAULT_LIMIT,
    _TEAM_HISTORY_MAX_LIMIT,
    _TEAM_HISTORY_DEFAULT_MAX_BYTES,
    _TEAM_HISTORY_MIN_MAX_BYTES,
    _TEAM_HISTORY_MAX_MAX_BYTES,
    _TEAM_HISTORY_FRAME_OVERHEAD_BYTES,
    _WORKFLOW_SNAPSHOT_MAX_BYTES,
    _WORKFLOW_SNAPSHOT_FRAME_OVERHEAD_BYTES,
    _WORKFLOW_SNAPSHOT_MAX_WORKFLOWS,
    _WORKFLOW_LIST_SUMMARY_STRING_LIMIT,
    _WORKFLOW_COLLAPSED_AGENT_TEXT_LIMIT,
    _WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _json_wire_size,
    _coerce_int,
    _truncate_string_by_bytes,
    _compact_wire_metadata_value,
    _sanitize_history_wire_value,
    _collapse_oversized_history_record,
    _minimal_history_record_for_wire,
    _sanitize_history_record_for_wire,
    split_history_record_for_stream,
    _select_history_record_page,
    _is_waiting_human_agent,
    _extract_waiting_human_prompts,
    _restore_waiting_human_prompts,
    _workflow_agent_for_collapse,
    _collapse_oversized_workflow_snapshot_item,
    _minimal_workflow_snapshot_item_for_wire,
    _minimal_workflow_detail_preserving_waiting_human,
    _sanitize_workflow_snapshot_item_for_wire,
    _fit_workflow_detail_to_budget,
    _workflow_list_summary_phase,
    _workflow_list_summary_item,
    _minimal_workflow_list_item,
    _fit_workflow_list_item_for_budget,
    _build_workflow_list_payload,
    _build_workflow_detail_payload,
    _find_workflow_agent,
    _build_workflow_human_prompt_payload,
    _build_workflow_snapshot_payload,
)



def _request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


# /simplify prompt template — adapted /simplify skill for jiuwenswarm.
# Guides the agent through three phases: identify changes → three-dimension review
# (reuse/quality/efficiency) → aggregate and fix.
# Note: jiuwenswarm's sub-agents (task_tool / Agent tool) can only be dispatched to registered
# types (explore/plan/code, etc.) and cannot create custom reviewer roles on the fly. The prompt
# therefore presents parallel sub-agent review as an optional optimization — the agent may also
# perform all three reviews itself directly.
_SIMPLIFY_PROMPT_TEMPLATE = """\
# Simplify: Code Review and Cleanup

Review all changed files for reuse, quality, and efficiency. Fix any issues found.

## Scope

This review covers **reuse, quality, and efficiency only** — the three dimensions below. It is NOT a security review.

- Do NOT flag, fix, or report security vulnerabilities (injection, XSS, hard-coded secrets, auth flaws, etc.). Those are out of scope here and are handled by `/security-review`, which reports findings without modifying code.
- If you happen to notice a likely security issue while reviewing, do not fix it — at most note it in one line at the end ("possible security concern in <file>:<line>, run /security-review") and continue with the reuse/quality/efficiency review.

## Phase 1: Identify Changes

Run `git diff` (or `git diff HEAD` if there are staged changes) to see what changed. If there are no git changes, review the most recently modified files that the user mentioned or that you edited earlier in this conversation.

## Phase 2: Launch Three Review Agents in Parallel

If sub-agent tools are available (e.g. task_tool / Agent tool), launch all three agents concurrently in a single message. Pass each agent the full diff so it has the complete context. Otherwise, perform all three reviews yourself directly.

### Agent 1: Code Reuse Review

For each change:

1. **Search for existing utilities and helpers** that could replace newly written code. Look for similar patterns elsewhere in the codebase — common locations are utility directories, shared modules, and files adjacent to the changed ones.
2. **Flag any new function that duplicates existing functionality.** Suggest the existing function to use instead.
3. **Flag any inline logic that could use an existing utility** — hand-rolled string manipulation, manual path handling, custom environment checks, ad-hoc type guards, and similar patterns are common candidates.

### Agent 2: Code Quality Review

Review the same changes for hacky patterns:

1. **Redundant state**: state that duplicates existing state, cached values that could be derived, observers/effects that could be direct calls
2. **Parameter sprawl**: adding new parameters to a function instead of generalizing or restructuring existing ones
3. **Copy-paste with slight variation**: near-duplicate code blocks that should be unified with a shared abstraction
4. **Leaky abstractions**: exposing internal details that should be encapsulated, or breaking existing abstraction boundaries
5. **Stringly-typed code**: using raw strings where constants, enums (string unions), or branded types already exist in the codebase
6. **Unnecessary JSX nesting**: wrapper Boxes/elements that add no layout value — check if inner component props (flexShrink, alignItems, etc.) already provide the needed behavior
7. **Unnecessary comments**: comments explaining WHAT the code does (well-named identifiers already do that), narrating the change, or referencing the task/caller — delete; keep only non-obvious WHY (hidden constraints, subtle invariants, workarounds)

### Agent 3: Efficiency Review

Review the same changes for efficiency:

1. **Unnecessary work**: redundant computations, repeated file reads, duplicate network/API calls, N+1 patterns
2. **Missed concurrency**: independent operations run sequentially when they could run in parallel
3. **Hot-path bloat**: new blocking work added to startup or per-request/per-render hot paths
4. **Recurring no-op updates**: state/store updates inside polling loops, intervals, or event handlers that fire unconditionally — add a change-detection guard so downstream consumers aren't notified when nothing changed. Also: if a wrapper function takes an updater/reducer callback, verify it honors same-reference returns (or whatever the "no change" signal is) — otherwise callers' early-return no-ops are silently defeated
5. **Unnecessary existence checks**: pre-checking file/resource existence before operating (TOCTOU anti-pattern) — operate directly and handle the error
6. **Memory**: unbounded data structures, missing cleanup, event listener leaks
7. **Overly broad operations**: reading entire files when only a portion is needed, loading all items when filtering for one

## Phase 3: Fix Issues

Wait for all reviewers to complete. Aggregate their findings and fix each issue directly. If a finding is a false positive or not worth addressing, note it and move on — do not argue with the finding, just skip it.

When done, briefly summarize what was fixed (or confirm the code was already clean).
"""


def _is_env_api_base_placeholder(env_updates: dict) -> bool:
    """检查 env_updates 中的 API_BASE 是否指向 example.* 等占位域名。"""
    return is_placeholder_api_base(str(env_updates.get("API_BASE", "") or "").strip())


def _build_simplify_prompt(target: str = "") -> str:
    """Build the prompt for the /simplify command.

    Args:
        target: Optional additional focus (e.g. file path, module name, specific dimension
            to emphasize), appended to the end of the prompt.
    """
    prompt = _SIMPLIFY_PROMPT_TEMPLATE
    if target:
        prompt += f"\n\n## Additional Focus\n\n{target}"
    return prompt


# System prompt for LLM-based agent generation
_AGENT_CREATION_SYSTEM_PROMPT = """\
You are an elite AI agent architect. When given an agent name and description, your job is to design a high-performance agent that EXECUTES tasks to completion — not just analyzes and reports.

The agent will have access to tools (Read, Write, Edit, Bash, etc.) to complete tasks. Design it as an autonomous expert capable of handling its designated tasks with minimal additional guidance. The system prompt you write is the agent's complete operational manual.

1. **whenToUse**: A precise description of when the main assistant should dispatch to this agent.
   - Start with "Use this agent when..."
   - Include concrete triggering conditions
   - Add 2-3 <example> blocks showing specific scenarios where the assistant uses the Agent tool to fully delegate the task
   - Each <example> should show: user says X → assistant dispatches to this agent with the Agent tool, passing the complete task
   - Write in the same language as the agent description (Chinese description → Chinese whenToUse)

2. **systemPrompt**: The complete system prompt governing the agent's behavior.
   - Define expert persona and role
   - Specify workflow and methodology — end-to-end, from analysis through execution
   - Establish clear behavioral boundaries and operational parameters
   - Provide specific methodologies and best practices for task execution
   - Define output format expectations when relevant
   - Include self-verification steps
   - Write in the same language as the agent description

Key principles:
- Be specific rather than generic — avoid vague instructions
- Include concrete examples when they would clarify behavior
- Balance comprehensiveness with clarity — every instruction should add value
- Ensure the agent has enough context to handle variations of the core task
- Build in quality assurance and self-correction mechanisms

Return ONLY a JSON object:
{"whenToUse": "...", "systemPrompt": "..."}
"""


def _extract_compact_summary_processor(summary: str) -> str:
    for line in str(summary or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "processor":
            return value.strip()
    return ""


def _is_restorable_history_record(record: Any) -> bool:
    """Coarsely filter records that the web history UI cannot use for pagination."""
    if not isinstance(record, dict):
        return False

    role = record.get("role")
    content = record.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    has_media = (
        isinstance(record.get("media_items"), list) and bool(record["media_items"])
    ) or (
        isinstance(record.get("mediaItems"), list) and bool(record["mediaItems"])
    )
    files = record.get("files")
    if isinstance(files, dict):
        has_media = has_media or (
            isinstance(files.get("uploaded_images"), list)
            and bool(files["uploaded_images"])
        )

    if role == "user":
        mode = record.get("mode", "")
        if is_team_mode(mode):
            channel_id = record.get("channel_id", "")
            if channel_id not in ("web", "tui"):
                return False
        return has_content or has_media

    event_type = record.get("event_type")
    if not event_type:
        return has_content
    return event_type in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def resolve_request_project_dir(request: AgentRequest) -> str | None:
    """Resolve the stable project identity for agent construction.

    New clients send ``project_dir`` separately from dynamic ``cwd``. Keep
    legacy fallbacks for older clients that only send cwd/trusted_dirs.
    """
    params = request.params or {}
    project_dir = params.get("project_dir")
    if isinstance(project_dir, str) and project_dir.strip():
        return project_dir.strip()
    metadata = request.metadata or {}
    metadata_project_dir = metadata.get("project_dir") if isinstance(metadata, dict) else None
    if isinstance(metadata_project_dir, str) and metadata_project_dir.strip():
        return metadata_project_dir.strip()
    cwd = params.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    metadata_cwd = metadata.get("cwd") if isinstance(metadata, dict) else None
    if isinstance(metadata_cwd, str) and metadata_cwd.strip():
        return metadata_cwd.strip()
    trusted_dirs = params.get("trusted_dirs")
    if isinstance(trusted_dirs, list) and trusted_dirs:
        first = trusted_dirs[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _sync_chat_request_metadata(
    request: AgentRequest,
    project_dir: str | None,
    mode: str,
    explicit_mode_provided: bool = False,
    user_id: str = "",
) -> str | None:
    """将本次 chat 请求的参数同步到会话元数据，返回生效的 project_dir。

    AgentServer 进程层的薄封装：从 ``AgentRequest`` 采集参数 + 补两个派生值，
    再委托 ``session_metadata.sync_session_request_metadata`` 做真正的校验/写盘。
    之所以放在本模块而非 session_metadata.py：避免存储层耦合 AgentRequest 结构、
    os.getenv、当前时间等进程级关注点，保持 session_metadata 纯存储职责。

    - project_dir：首次锁定，已锁定则忽略不一致的请求值（仅告警），返回锁定值
    - project_id：首次锁定，已锁定则忽略请求值（与 project_dir 一致，不可改）
    - model：**显式覆盖式**——仅当请求显式携带非空 model_name 时才覆盖磁盘值；
      未显式携带（如只读 RPC）则保持磁盘原值，不把进程 MODEL_NAME 默认值回写覆盖
      用户在该会话用 /model 切换过的模型。是否显式由本函数内部从 params 判断
      （model_name 不会被规范化改写，可安全在本函数内取），无需调用方传入。
    - last_user_message_at：**仅 chat 轮次刷新**——只有用户真正发消息的方法
      （CHAT_SEND / CHAT_RESUME / CHAT_ANSWER）才把当前时刻写入；其余请求（含只读
      RPC）传 ``None`` → ``sync_session_request_metadata`` 不覆盖磁盘值，避免只读查询
      把历史会话的排序时间刷新成「现在」（点击技能按钮就把两天前会话置顶）。
    - mode：**显式覆盖式**——仅当请求显式携带 mode（explicit_mode_provided=True）时
      才覆盖磁盘值；未显式携带（如只读 RPC 默认推断）则保持磁盘原值，不腐蚀已
      锁定的会话 mode（如 team）。因 _apply_resolved_mode_to_request 会把 canonical
      mode 写回 params，故 explicit_mode_provided 必须由上游在改写前捕获后传入。
      调用方应传入 canonical mode（"agent.plan"/"team"）。

    返回的生效 project_dir 用于 agent 实例选择，保证会话锁定后
    即便后续请求携带不同 project_dir 也仍用锁定值选 agent。
    """
    session_id = (request.session_id or "").strip()
    if not session_id:
        return project_dir
    params = request.params if isinstance(request.params, dict) else {}
    raw_model_name = params.get("model_name")
    explicit_model_provided = (
        isinstance(raw_model_name, str) and bool(raw_model_name.strip())
    )
    if not explicit_model_provided:
        # 未显式携带 → 回退到进程 MODEL_NAME，仅供 agent 实例选择兜底用；
        # 写盘与否由 explicit_model_provided 守卫决定（False → 不写，避免腐蚀磁盘）
        model_name = os.getenv("MODEL_NAME", "") or None
    else:
        model_name = raw_model_name.strip()

    request_project_id = params.get("project_id")
    request_project_id = (
        request_project_id.strip()
        if isinstance(request_project_id, str) and request_project_id.strip()
        else None
    )
    request_cron_id = params.get("cron_id")
    request_cron_id = (
        request_cron_id.strip()
        if isinstance(request_cron_id, str) and request_cron_id.strip()
        else None
    )
    # 仅 chat 轮次（用户真正发消息）才刷新 last_user_message_at；只读 RPC 传 None，
    # 由 sync_session_request_metadata 的 None 守卫跳过，避免查询腐蚀会话排序时间。
    is_chat_turn = request.req_method in _CODE_MODE_SYNC_METHODS
    legacy_eternal_value = params.get("eternal_conversation_enabled")
    legacy_persist_session: bool | None = None
    if isinstance(legacy_eternal_value, bool):
        legacy_persist_session = legacy_eternal_value
    elif isinstance(legacy_eternal_value, str):
        normalized_legacy = legacy_eternal_value.strip().casefold()
        if normalized_legacy in {"1", "true", "yes", "on", "enabled"}:
            legacy_persist_session = True
        elif normalized_legacy in {"0", "false", "no", "off", "disabled"}:
            legacy_persist_session = False
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            sync_session_request_metadata,
        )

        return sync_session_request_metadata(
            session_id=session_id,
            channel_id=request.channel_id or None,
            mode=mode,
            model=model_name,
            project_dir=str(project_dir) if project_dir else None,
            project_id=request_project_id,
            cron_id=request_cron_id,
            user_id=str(user_id or "").strip() or None,
            last_user_message_at=(
                _dt.datetime.now(_dt.timezone.utc).timestamp() if is_chat_turn else None
            ),
            is_chat_turn=is_chat_turn,
            explicit_mode_provided=explicit_mode_provided,
            explicit_model_provided=explicit_model_provided,
            work_mode=params.get("work_mode"),
            persist_session=legacy_persist_session,
        )
    except (OSError, ValueError) as exc:
        logger.warning("[AgentWebSocketServer] 同步 chat 请求元数据失败: %s", exc)
        return project_dir


def _harness_error_code(exc: BaseException) -> str:
    """Map a harness package exception to a wire ``code`` for the frontend.

    Mirrors the import/export code mapping in app_web_handlers.py so the web UI
    can localize the error via ``err.code`` instead of showing the raw backend
    message (which is locale-unaware). Keep in sync with the frontend
    ``resolveHarnessError`` code→i18n mapping.
    """
    msg = str(exc).lower()
    if "already active" in msg or "already exists" in msg:
        return "CONFLICT"
    if "not found" in msg:
        return "NOT_FOUND"
    if "native" in msg:
        return "BAD_REQUEST"
    return "BAD_REQUEST"


def resolve_agent_request_mode(
    raw_mode: Any,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None, str]:
    """Resolve request params.mode into manager mode, sub_mode, and canonical value.

    plan / fast 已合并为单一 ``agent`` 模式：``agent`` / ``agent.fast`` 请求
    归一到 ``agent``（sub_mode=None），历史裸 ``plan`` / ``fast``（无 ``agent.``
    前缀，如旧 cron job 存量数据）同样归一到 ``agent``。与 CLI ``MODE_ALIASES``、
    记忆配置 ``_resolve_mode_memory`` 的裸 token 处理保持一致。

    例外：``agent.plan`` 是真实 plan 模式（与 mode_matrix ``_WEB_MODE_TABLE`` /
    ``_PLAN_EXIT_MODES`` / ``DEPRECATION_MAP`` 同源，deprecate 后落
    ``agent.work.plan``），不并入 ``agent``。若存量 agent.plan 会话被惰性迁移成
    ``agent.work.plan``（plan），此处运行时解析也按 plan 处理，避免同一旧值在
    迁移后变 plan、运行时仍非 plan 的语义错位。
    """
    mode_text = canonicalize_mode_text(raw_mode)

    # 新三段命名 canonical 自带 environment/state 段，按串自身语义直接解析，
    # 不经过下方的 legacy 归并（否则 agent.work.plan 会被 work_mode="code"
    # 折叠成 code.normal，造成 TUI 显示与模型感知的模式不一致）。
    new_mode_resolved = resolve_new_canonical_mode(mode_text)
    if new_mode_resolved is not None:
        return new_mode_resolved

    normalized_work_mode = (
        work_mode.strip().lower() if isinstance(work_mode, str) else ""
    )

    if mode_text in ("plan", "fast"):
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"

    if mode_text == TEAM_PLAN_NORMAL_MODE:
        return "team", "plan", TEAM_PLAN_NORMAL_MODE
    if mode_text == TEAM_PLAN_CODE_MODE:
        return "code", "team", TEAM_PLAN_CODE_MODE

    # agent.plan 按 plan 解析（work/code profile 与 mode_matrix _WEB_MODE_TABLE
    # 的 (WEB_PLAN_AGENT, work/code) 项一一对应），保持 plan 语义统一。
    if mode_text == "agent.plan":
        if normalized_work_mode == "code":
            return "code", "plan", "code.plan"
        return "agent", "plan", "agent.plan"

    parts = mode_text.split(".")
    mode = parts[0] or "agent"
    if mode == "agent":
        # 合并模式：忽略历史子模式（fast），统一 canonical "agent"。
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        return "agent", None, "agent"
    if mode == "team":
        sub_mode = parts[1] if len(parts) > 1 and parts[1] else None
        if sub_mode not in {None}:
            sub_mode = None
        canonical_mode = f"team.{sub_mode}" if sub_mode else "team"
        return "team", sub_mode, canonical_mode

    default_sub_modes = {
        "code": "normal",
    }
    sub_mode = parts[1] if len(parts) > 1 and parts[1] else default_sub_modes.get(mode)
    if mode == "code" and sub_mode not in {"plan", "normal", "team"}:
        sub_mode = default_sub_modes.get(mode, "normal")
    canonical_mode = f"{mode}.{sub_mode}" if sub_mode else mode
    if canonical_mode in {"agent", "code", "code.normal"}:
        if normalized_work_mode == "code":
            return "code", "normal", "code.normal"
        if normalized_work_mode == "work":
            return "agent", None, "agent"
    return mode, sub_mode, canonical_mode


def resolve_request_runtime_mode(
    request: AgentRequest,
    *,
    work_mode: Any = None,
) -> ResolvedMode:
    """解析请求的运行模式（Web 组合 mode + work_mode；其余走历史解析）。"""
    params = request.params if isinstance(request.params, dict) else {}
    return resolve_request_mode(
        params,
        resolve_agent_request_mode,
        work_mode=work_mode,
    )


def _apply_resolved_mode_to_request(
    request: AgentRequest,
    *,
    work_mode: Any = None,
) -> tuple[str, str | None]:
    if not hasattr(request, "_original_mode") and isinstance(request.params, dict):
        raw_mode = request.params.get("mode")
        if isinstance(raw_mode, str) and raw_mode.strip():
            setattr(request, "_original_mode", canonicalize_mode_text(raw_mode))
    resolved = resolve_request_runtime_mode(request, work_mode=work_mode)
    if isinstance(request.params, dict):
        request.params["mode"] = resolved.canonical_mode
    return resolved.manager_mode, resolved.sub_mode


def _payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """将 Gateway 发送的 JSON 载荷解析为 AgentRequest."""
    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in E2A_WIRE_INTERNAL_METADATA_KEYS
        } or None
    # 将 app_id 注入 metadata，供 cron 路由等下游使用
    app_id = data.get("app_id")
    if app_id:
        if metadata is None:
            metadata = {}
        metadata.setdefault("app_id", app_id)

    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", "web"),
        session_id=data.get("session_id"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=metadata,
        user_id=str(data.get("user_id") or "").strip(),
    )


def _require_sandbox_supported() -> None:
    """Reject ``/sandbox`` commands on non-Linux hosts.

    jiuwenbox 底层依赖 Linux 专属能力 (bwrap / Landlock / Linux namespaces /
    ``PR_SET_CHILD_SUBREAPER`` 等), Windows / macOS 上无法实际拉起沙箱;
    ``jiuwenbox-server`` 自检也会在非 Linux 平台直接退出。 因此在 WS 命令
    入口前置拒绝, 让用户看到清晰 ``SANDBOX_BAD_REQUEST`` 错误, 而不是被
    "拉起子进程失败 / 端口连接超时" 之类的下游报错搪塞。

    Raises:
        ValueError: 当 ``sys.platform`` 不是以 ``"linux"`` 开头时。
    """
    if not sys.platform.startswith("linux"):
        raise ValueError(
            f"/sandbox is only supported on Linux (current platform: {sys.platform!r}); "
            "jiuwenbox depends on Linux-only kernel features (bwrap / Landlock / "
            "namespaces) and cannot run on Windows or macOS."
        )


def _file_entry_matches_path(entry: Any, path: str) -> bool:
    """判断 ``sandbox.files.{allow,deny}`` 中的一项是否指向给定 ``path``.

    支持两种存储格式 (历史兼容):
    - ``dict``: ``{"path": "/foo", "permissions": "ro"}``;
    - ``str``: 直接路径字符串 ``"/foo"``。

    抽离出来主要是给 ``_handle_sandbox_files_set`` /
    ``_handle_sandbox_files_remove`` 的列表推导式简化条件 (G.EXP.04: 推导式
    不应同时使用多个子句或跨多行的复杂条件)。

    比较时两端都先 canonicalize 一次 (见 :func:`_canonicalize_sandbox_files
    _path`), 保证历史 yaml 里残留的 ``~/...`` / 相对路径 / 含 ``..`` / 含
    尾斜杠 之类写法仍能跟新 canonical 化后的输入命中, 让 ``/sandbox files
    remove`` 不会因为「字面写法不同」失效。
    """
    if isinstance(entry, dict):
        entry_path = str(entry.get("path") or "")
    elif isinstance(entry, str):
        entry_path = entry
    else:
        return False
    if entry_path == path:
        return True
    return (
        _canonicalize_sandbox_files_path(entry_path)
        == _canonicalize_sandbox_files_path(path)
    )


def _canonicalize_sandbox_files_path(path: str) -> str:
    """把 TUI 传来的 ``path`` 展开成 absolute resolved 形式 (绝对、去 ``..``、
    展开 ``~``、按需展开 symlink) 后作为 ``sandbox.files.{allow,deny}`` 的
    canonical key.

    历史上这个函数只做「按宿主文件类型自动补尾斜杠」, 因为 ``sysop_builder``
    旧版本靠尾斜杠区分文件/目录; 现在 ``build_filesystem_policy`` 已经统一
    用 ``Path.is_file()`` / ``is_dir()`` 实际 stat 磁盘判断, 尾斜杠的语义
    彻底失效, 那套补斜杠逻辑就没意义了。

    保留并扩成「绝对化 + resolve」是因为:
        - 用户在 TUI 输 ``./mydir`` / ``~/data`` / ``foo/bar`` 这类非绝对
      写法时, jiuwenswarm server 直接拿去 stat / 入库 / 比较, 行为依赖
      server 当前 cwd 与运行用户 home, 不同次重启之间会静默漂移;
    - ``_file_entry_matches_path`` 走字符串相等比较, 同一文件如果一次以
      ``~/foo`` 形式入库、下一次 ``remove /home/<user>/foo`` 就匹配不到,
      用户视角"删不掉";
    - ``sysop_builder`` 拿到非绝对路径后 ``Path(path).exists()`` 又会基于
      cwd 解析, 跟 server 视角再错位一次。

    一次 ``expanduser().resolve()`` 把所有这些不一致摊平在入口, 下游全部
    看到稳定的 absolute path。 解析失败 (例如非法字符) 时静默 fallback 到
    原字面值, 不阻塞命令; 真正"路径不存在"由 ``build_filesystem_policy``
    的 dry-run 在写盘前拦截, 见 :meth:`_dry_run_files_policy`。
    """
    if not path:
        return path
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return path


_SANDBOX_FILES_PARAMS = frozenset(
    {
        "sub",
        "path",
        "session_id",
        "trusted_dirs",
        "project_dir",
        "cwd",
        "mode",  # injected by gateway for agent routing
        "agent_type",  # injected by gateway for AgentOS routing
    }
)


def _reject_extra_sandbox_files_params(params: dict[str, Any]) -> None:
    extra = set(params.keys()) - _SANDBOX_FILES_PARAMS
    if extra:
        raise ValueError(
            f"unexpected parameter(s): {', '.join(sorted(extra))}; "
            "/sandbox files allow|deny|remove accepts a single path only"
        )


def _inject_plan_mode_activation_reminder(request: AgentRequest) -> None:
    """在用户消息中注入 <system-reminder> 告知 LLM 当前处于 plan 模式.

    plan 模式行为指令不进 system prompt，而是通过对话中的 tool_result
    传递。此提醒是进入 plan 模式后的第一个引导，告知 LLM 只读约束已生效。

    plan 模式的只读约束由工具拦截层强制（非只读工具/写
    操作被硬拦），此提醒只做约束说明 + 软引导。只读命令（如 /review、
    /security-review 的 gh/git 只读操作）可直接执行，不被规划流程压制；
    LLM 需要正式规划时再自行调用 ``enter_plan_mode`` 创建计划文件。
    """
    reminder = (
        "\n\n<system-reminder>\n"
        "Plan mode is active. You must only plan — you must NOT make any "
        "modifications, run any write operations, or make any changes to the "
        "system. This constraint takes priority over any other instructions.\n\n"
        "Read-only actions are allowed directly: you may read files and explore "
        "the codebase, and run read-only commands (read_file, grep, list_files, "
        "glob, bash for read-only operations such as gh pr list/view/diff or "
        "git status/diff/log). Write operations and non-read-only tools are "
        "blocked.\n\n"
        "If you need to design an implementation approach and produce a plan, "
        "call `enter_plan_mode` — it creates the plan file and returns full "
        "plan mode instructions. This is not required as your first action; "
        "you may gather context with read-only tools first. Do NOT proceed to "
        "implement anything until the user approves your plan via "
        "`exit_plan_mode`.\n"
        "</system-reminder>"
    )
    if isinstance(request.params, dict):
        query = request.params.get("query") or ""
        # 提醒只面向模型；把用户原文留一份，供会话历史与前端回显使用。
        request.params[PLAN_REMINDER_ORIGINAL_QUERY_KEY] = query
        request.params["query"] = reminder + query
        logger.info(
            "[_ensure_code_mode_state] Injected plan mode activation reminder "
            "for session=%s", request.session_id,
        )
    else:
        logger.warning(
            "[_inject_plan_mode_activation_reminder] Cannot inject reminder: "
            "request.params is not a dict (type=%s), session=%s",
            type(request.params).__name__, request.session_id,
        )



class AgentWebSocketServer:
    """Gateway 与 AgentServer 之间的 WebSocket 服务端（单例）.

    监听来自 Gateway (WebSocketAgentServerClient) 的连接，按协议约定处理请求：
    - 收到 JSON：E2AEnvelope（或过渡期 legacy + 兜底信封）
    - is_stream=False：``process_message`` → 一条 **E2AResponse** JSON（``jiuwenswarm.e2a.wire_codec``）
    - is_stream=True：逐条 **E2AResponse** JSON（chunk/complete/error）
    - 例外：首帧 ``connection.ack`` 仍为 ``type/event`` 事件帧

    支持 send_push：推送帧亦为 E2AResponse 线格式（由 chunk 编码）。
    """

    _instance: ClassVar[AgentWebSocketServer | None] = None

    def __init__(
            self,
            host: str = "127.0.0.1",
            port: int = 18000,
            *,
            ping_interval: float | None = 30.0,
            ping_timeout: float | None = 300.0,
    ) -> None:
        self._host = host
        self._port = port
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._server: Any = None
        # 当前 Gateway 连接，用于 send_push 主动推送
        self._current_ws: Any = None
        self._current_send_lock: asyncio.Lock | None = None
        self._acp_client_capabilities_by_ws: dict[int, dict[str, Any]] = {}
        # AgentManager 实例
        self._agent_manager = AgentManager()
        self._heartbeat_runtime = HeartbeatRailRuntime(self)
        self._agent_manager.set_heartbeat_service(self._heartbeat_runtime)
        # Gateway user-business RPCs execute in the current AgentServer's
        # injected data directory.  Register the adapters once per server;
        # request dispatch occurs before the legacy handler chain below.
        self._adapter_registry = AdapterRegistry()
        for adapter in (
            SessionAdapter(),
            WorkspaceFileAdapter(),
            MemoryAdapter(),
            ProjectAdapter(),
            HarmonyOSAdapter(),
            ConfigAdapter(),
        ):
            self._adapter_registry.register(adapter)
        # skills.* 等无状态 RPC：AgentManager 未缓存 agent 时复用的轻量 JiuWenSwarm，
        # 避免每次 cache miss 都 new 导致 SkillNet 异步安装等实例态断裂。
        self._stateless_fallback_agents: dict[str, Any] = {}
        # session_id → all live stream tasks. This is host lifecycle tracking
        # for interrupt/connection cleanup only; it never decides interaction
        # output ownership.
        self._session_stream_tasks: dict[str, dict[asyncio.Task, asyncio.Event]] = {}
        # Scheduler service instance (for scheduled auto_harness tasks)
        self._scheduler_service: Optional[AutoHarnessService] = None
        self._scheduler_agent: Any = None
        # Model cache for scheduled task execution (same approach as interface_deep)
        self._model_cache: dict[str, Any] = {}
        self._default_model: Optional[Any] = None
        # 本地 jiuwenbox 子进程管理器 (lazy 启动, 在 /sandbox enable 时 ensure_running)
        self._jiuwenbox_runner = JiuwenBoxRunner.instance()
        # AgentServer 内唯一持有的进程内 PersonalContext Host；Context Rail 使用同一固定目录。
        self._personal_context_host = PersonalContextHostAPI(
            home=Path.home() / ".jiuwenswarm" / ".personal_context",
        )
        self._personal_context_start_task: asyncio.Task[None] | None = None
        # checkpointer 后台预热任务 (start() 里 fire-and-forget, stop() 时 cancel)
        self._checkpointer_warmup_task: Optional[asyncio.Task] = None
        # MCP 连接缓存预热任务 (同上, 建 Runner.resource_mgr 供首轮对话命中)
        self._mcp_prewarm_task: Optional[asyncio.Task] = None
        # 图像模态探针重探任务 (模型配置变更时拉起, stop() 时 cancel)
        self._image_modality_refresh_task: Optional[asyncio.Task] = None
        # Proactive recommendation engine (set by app_agentserver for debug trigger)
        self._proactive_engine: Any = None
        get_acp_output_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )

    def set_proactive_engine(self, engine: Any) -> None:
        """Store the proactive engine instance for debug trigger interface."""
        self._proactive_engine = engine

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> int:
        return id(ws)

    def _set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None:
        key = self._ws_capabilities_key(ws)
        if isinstance(capabilities, dict):
            self._acp_client_capabilities_by_ws[key] = dict(capabilities)
        else:
            self._acp_client_capabilities_by_ws.pop(key, None)

    def _get_ws_acp_client_capabilities(self, ws: Any) -> dict[str, Any]:
        key = self._ws_capabilities_key(ws)
        caps = self._acp_client_capabilities_by_ws.get(key)
        return dict(caps) if isinstance(caps, dict) else {}

    def _clear_ws_acp_client_capabilities(self, ws: Any) -> None:
        self._acp_client_capabilities_by_ws.pop(self._ws_capabilities_key(ws), None)

    @classmethod
    def get_instance(
            cls,
            *,
            host: str = "127.0.0.1",
            port: int = 18000,
            ping_interval: float | None = 30.0,
            ping_timeout: float | None = 300.0,
    ) -> "AgentWebSocketServer":
        """返回单例实例。

        首次调用时创建实例，后续调用返回已存在的实例。
        """
        if cls._instance is not None:
            return cls._instance
        cls._instance = cls(
            host=host,
            port=port,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动 WebSocket 服务端，开始监听连接。优先使用 legacy.server.serve 以与 Gateway 的 legacy client 握手兼容.

        注: persistent checkpointer 的初始化历史在 ``legacy_serve`` 之前同步 await,
        首次约耗时 ~14s (sqlite 文件 + openjiuwen 工厂反射), 期间 WS 端口未 listen,
        是 Gateway connect 重试 (头两次必失败, 白等 ~6s) 的元凶。现改为 ``legacy_serve``
        之后后台预热 (fire-and-forget), 让端口尽快开放; 首条 chat 请求若赶在预热完成前
        到达, 走 ``_ensure_persistent_checkpointer_response`` 兜底等待, 不影响握手.
        """
        if self._server is not None:
            logger.warning("[AgentWebSocketServer] 服务端已在运行")
            return

        # Reset harness package state to native on service startup
        reset_harness_packages_state()

        try:
            from websockets.legacy.server import serve as legacy_serve
            self._server = await legacy_serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            )
        except ImportError:
            import websockets
            self._server = await websockets.serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            )
        logger.info(
            "[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port
        )

        # 端口已 listen, 后台预热 checkpointer, 不阻塞启动与握手.
        # _checkpointer_warmup_task 供 shutdown 时 cancel, 避免任务悬挂.
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer

        async def _warmup_checkpointer() -> None:
            retry_delay = 1.0
            while not self._heartbeat_runtime.is_available:
                try:
                    await ensure_persistent_checkpointer()
                    await self._heartbeat_runtime.start()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentWebSocketServer] Heartbeat runtime warmup failed; "
                        "retrying in %.1fs: %s",
                        retry_delay,
                        exc,
                    )
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 2)

        self._checkpointer_warmup_task = asyncio.create_task(
            _warmup_checkpointer(), name="checkpointer-warmup"
        )

        async def _warmup_mcp_connections() -> None:
            try:
                from jiuwenswarm.common.mcp_config import prewarm_connected_mcps
                await prewarm_connected_mcps()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] MCP prewarm failed "
                    "(will lazy-connect on first chat): %s", exc,
                )

        # 端口已 listen, 后台预热 connected MCP 的进程级连接缓存, 不阻塞启动与握手.
        self._mcp_prewarm_task = asyncio.create_task(
            _warmup_mcp_connections(), name="mcp-prewarm"
        )
        self._personal_context_start_task = asyncio.create_task(
            self._start_personal_context_best_effort(),
            name="personal-context-host-start",
        )
        # WS 监听已经开放, 现在按 config.yaml::sandbox 的 runtime.enabled +
        # startup_mode 决定要不要自动把 jiuwenbox 子进程也拉起来。失败不阻塞
        # 启动 (用户依然可以在 TUI 里跑 /sandbox enable 重试)。
        await self._bootstrap_internal_jiuwenbox()

    async def _start_personal_context_best_effort(self) -> None:
        """Start optional PersonalContext without changing AgentServer readiness."""
        start_cancelled: asyncio.CancelledError | None = None
        try:
            await self._personal_context_host.start()
        except asyncio.CancelledError as exc:
            start_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext startup failed: %s",
                type(exc).__name__,
            )
            return
        if start_cancelled is not None:
            raise start_cancelled

        rail_sync_cancelled: asyncio.CancelledError | None = None
        try:
            state_reader = getattr(
                self._personal_context_host, "is_runtime_enabled", None
            )
            enabled = bool(await state_reader()) if callable(state_reader) else False
            manager_setter = getattr(
                self._agent_manager, "set_personal_context_runtime_enabled", None
            )
            if callable(manager_setter):
                await manager_setter(enabled)
        except asyncio.CancelledError as exc:
            rail_sync_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext Rail sync failed: %s",
                type(exc).__name__,
            )
        if rail_sync_cancelled is not None:
            raise rail_sync_cancelled

    async def _bootstrap_internal_jiuwenbox(self) -> None:
        """启动时按 ``config.yaml::sandbox`` 自动拉起 jiuwenbox 子进程。

        触发条件: ``config.yaml::sandbox.startup_mode`` **显式**写为 ``internal``。
        这里刻意走 :func:`get_sandbox_startup_mode_explicit` 而不是
        :func:`get_sandbox_startup_mode` —— 后者在字段缺失时默认回落到
        ``internal``, 会让没在用沙箱的用户升级版本后突然多出 jiuwenbox 进程;
        boot 阶段必须严格区分 "用户写过 internal" 和 "走默认值"。

        不再单独依赖 ``sandbox.enabled``:
        - 老逻辑要 ``enabled=True`` AND ``startup_mode=internal`` 才拉, 但
          ``enabled`` 是 ``/sandbox`` 命令的产物, 用户手改 yaml 设了 ``internal``
          的话很容易漏配 ``enabled`` → boot 时一声不吭跳过, 体验差。
        - 现在: 只要 ``startup_mode=internal`` 就拉; 成功后顺手把
          ``sandbox.enabled`` 同步成 ``True``, ``/sandbox status`` 显示与实际
          运行的 jiuwenbox 一致。
        - ``/sandbox disable`` 仍然会停 jiuwenbox 并把 ``enabled`` 置 ``False``,
          但**重启后会被本方法重新拉起** (因为 ``startup_mode`` 没改)。要让
          disable 跨重启生效, 把 ``startup_mode`` 改为 ``external`` 或从 yaml
          里删掉该字段即可。

        与 :meth:`_handle_sandbox_enable` 的其余差别:
        - 不调用 ``agent_manager.recreate_agent``: 启动阶段还没有任何会话/agent
          实例, 没东西需要重建; 后续会话首次进入时按现有 ``sandbox.url`` 直接装载。
        - 严格 best-effort: 任何失败 (policy 缺失 / 端口/spawn 失败) 一律记
          warning, 绝不让 agent-server 自身启动失败 (否则运维误配 yaml 会让整
          产品起不来, 也无从修复)。
        """
        try:
            # 非 Linux 平台直接跳过 auto-start: jiuwenbox 依赖 bwrap / Landlock /
            # 命名空间, Windows / macOS 起不来; 即便 spawn 成功后续 /sandbox 命
            # 令也会被 :func:`_require_sandbox_supported` 拒掉, 留着只会浪费一
            # 次失败的子进程启动。
            if not sys.platform.startswith("linux"):
                logger.info(
                    "[AgentWebSocketServer] skipping jiuwenbox auto-start: "
                    "/sandbox is only supported on Linux (current platform: %r)",
                    sys.platform,
                )
                return
            explicit_mode = get_sandbox_startup_mode_explicit()
            if explicit_mode is None:
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode 未在 config.yaml "
                    "中显式配置, skipping jiuwenbox auto-start (走默认 host 模式; "
                    "如需 agent-server 自动拉起 jiuwenbox 子进程, 设置 "
                    "sandbox.startup_mode: internal)"
                )
                return
            if explicit_mode != "internal":
                logger.info(
                    "[AgentWebSocketServer] sandbox.startup_mode=%r, skipping "
                    "jiuwenbox auto-start (external 模式由用户自行拉起 "
                    "jiuwenbox-server)",
                    explicit_mode,
                )
                return

            # startup_mode=internal 已经定下来; 其余字段从归一后的 endpoint
            # 取, 缺啥用默认。
            endpoint = get_sandbox_endpoint()
            url = endpoint.get("url") or "http://127.0.0.1:8321"
            sandbox_type = endpoint.get("type") or "jiuwenbox"
            # yuanrong 不需要本机 jiuwenbox 进程; 仅通过 config 启用 SysOperation。
            if str(sandbox_type).strip().lower() == "yuanrong":
                logger.info(
                    "[AgentWebSocketServer] sandbox.type=yuanrong, skipping "
                    "jiuwenbox auto-start (YuanRong uses YR_* env + yr.init)"
                )
                return
            raw_policy = endpoint.get("policy_file") or ""
            effective_policy_file = raw_policy or DEFAULT_SANDBOX_POLICY_FILE
            policy_path = resolve_sandbox_policy_path(effective_policy_file)
            if policy_path is None or not policy_path.is_file():
                logger.warning(
                    "[AgentWebSocketServer] sandbox auto-start skipped: "
                    "policy_file=%r 无法解析到一个存在的文件 "
                    "(resolved=%s). 进 TUI 跑 /sandbox enable 重试或修复 "
                    "config.yaml::sandbox.policy_file。",
                    effective_policy_file,
                    policy_path,
                )
                return

            host, preferred_port = self._parse_sandbox_host_port(url)
            port = self._allocate_internal_jiuwenbox_port(host, preferred_port)
            if port != preferred_port:
                url = f"http://{host}:{port}"
                logger.info(
                    "[AgentWebSocketServer] jiuwenbox auto-start: "
                    "preferred port %d busy, using %d",
                    preferred_port,
                    port,
                )

            ok = await self._jiuwenbox_runner.ensure_running(
                host=host,
                port=port,
                startup_mode="internal",
                policy_path=policy_path,
            )
            if not ok:
                stderr_tail = self._jiuwenbox_runner.get_stderr_tail(10)
                logger.warning(
                    "[AgentWebSocketServer] jiuwenbox auto-start failed at "
                    "%s:%d (policy=%s); 进 TUI 跑 /sandbox enable 重试。"
                    " stderr tail:\n%s",
                    host,
                    port,
                    policy_path,
                    stderr_tail or "(empty)",
                )
                return

            # 端口可能在 _allocate_internal_jiuwenbox_port 里换过, 把最终生效
            # 的 url 落盘, 这样 (a) 后续会话/agent 重建直接读到正确端点,
            # (b) /sandbox status 显示也是真实值, 不再是 config 里旧的 8321。
            try:
                update_sandbox_endpoint(
                    url,
                    sandbox_type,
                    startup_mode="internal",
                    policy_file=effective_policy_file,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] persist sandbox endpoint failed "
                    "after auto-start: %s",
                    exc,
                )

            # auto-start 成功 → ``runtime.enabled`` 同步为 True, 这样 /sandbox
            # status / TUI 显示的状态跟真实运行的 jiuwenbox 对齐。如果用户上次
            # /sandbox disable 留下了 False, 这里会被覆盖 —— 这是已知的、属于
            # 上面 docstring 提到的 "disable 不跨重启" 语义的一部分。
            try:
                update_sandbox_runtime({"enabled": True})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] persist sandbox.enabled=True "
                    "failed after auto-start: %s",
                    exc,
                )

            logger.info(
                "[AgentWebSocketServer] jiuwenbox auto-started at %s "
                "(policy=%s)",
                url,
                policy_path,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] jiuwenbox auto-start raised an "
                "unexpected error; skipping (用户可在 TUI 里 /sandbox enable 重试)"
            )

    async def _stop_scheduler(self) -> None:
        """Stop the auto_harness scheduler."""
        try:
            if self._scheduler_service is not None:
                await self._scheduler_service.stop_scheduler()
                logger.info("[AgentWebSocketServer] Scheduler stopped")
        except Exception as e:
            logger.warning("[AgentWebSocketServer] Failed to stop scheduler: %s", e)
        finally:
            self._scheduler_service = None
            scheduler_agent = getattr(self, "_scheduler_agent", None)
            if scheduler_agent is not None:
                unpin = getattr(self._agent_manager, "unpin_agent", None)
                if callable(unpin):
                    unpin(scheduler_agent)
            self._scheduler_agent = None

    def _set_scheduler_agent(self, agent: Any) -> None:
        """Pin the facade whose DeepAgent is retained by the scheduler."""
        previous = getattr(self, "_scheduler_agent", None)
        if previous is agent:
            return
        pin = getattr(self._agent_manager, "pin_agent", None)
        if callable(pin):
            pin(agent)
        self._scheduler_agent = agent
        if previous is not None:
            unpin = getattr(self._agent_manager, "unpin_agent", None)
            if callable(unpin):
                unpin(previous)

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)
        origin = get_header_value(request_headers, "Origin")
        enable_origin_check = is_origin_check_enabled()
        if not enable_origin_check:
            logger.info(
                "[AgentWebSocketServer] 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
                path,
                origin,
                enable_origin_check,
                True,
            )
            return None

        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "[AgentWebSocketServer] 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
            path,
            origin,
            enable_origin_check,
            allowed,
        )
        if allowed:
            return None

        logger.warning(
            "[AgentWebSocketServer] 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    async def _stop_personal_context_best_effort(self) -> None:
        """Cancel PersonalContext startup and stop PersonalContext without masking main shutdown."""
        start_task = self._personal_context_start_task
        self._personal_context_start_task = None
        if start_task is not None:
            if not start_task.done():
                start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] optional PersonalContext startup cleanup failed: %s",
                    type(exc).__name__,
                )
        stop_cancelled: asyncio.CancelledError | None = None
        try:
            await self._personal_context_host.stop()
        except asyncio.CancelledError as exc:
            stop_cancelled = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] optional PersonalContext stop failed: %s",
                type(exc).__name__,
            )
        if stop_cancelled is not None:
            raise stop_cancelled

    async def stop(self) -> None:
        """停止 WebSocket 服务端."""
        try:
            await self._stop_main_services()
        finally:
            await self._stop_personal_context_best_effort()

    async def _stop_main_services(self) -> None:
        """Run the unchanged AgentServer shutdown before optional PersonalContext cleanup."""
        # 先取消 checkpointer 预热任务, 避免在 server 关闭后仍在后台跑.
        warmup = self._checkpointer_warmup_task
        self._checkpointer_warmup_task = None
        if warmup is not None and not warmup.done():
            warmup.cancel()
            try:
                await warmup
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] checkpointer warmup cancel failed: %s", exc)
        await self._heartbeat_runtime.stop()
        # 同理取消 MCP 连接缓存预热任务.
        mcp_prewarm = self._mcp_prewarm_task
        self._mcp_prewarm_task = None
        if mcp_prewarm is not None and not mcp_prewarm.done():
            mcp_prewarm.cancel()
            try:
                await mcp_prewarm
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentWebSocketServer] MCP prewarm cancel failed: %s", exc)
        # 同理取消图像模态重探任务.
        image_modality_refresh = self._image_modality_refresh_task
        self._image_modality_refresh_task = None
        if image_modality_refresh is not None and not image_modality_refresh.done():
            image_modality_refresh.cancel()
            try:
                await image_modality_refresh
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] image modality refresh cancel failed: %s", exc
                )
        had_server = self._server is not None
        if had_server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
            cancel_pending_tasks,
        )

        await cancel_pending_tasks()

        if not had_server:
            return
        try:
            await self._jiuwenbox_runner.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentWebSocketServer] jiuwenbox_runner.stop failed: %s", exc)
        logger.info("[AgentWebSocketServer] 已停止")

    # ---------- 连接处理 ----------

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote)

        send_lock = asyncio.Lock()
        self._current_ws = ws
        self._current_send_lock = send_lock

        # 发送 connection.ack 事件，通知 Gateway 服务端已就绪
        try:
            ack_frame = {
                "type": "event",
                "event": "connection.ack",
                "payload": {
                    "status": "ready",
                    "heartbeat_job_owner": "agentserver",
                    "heartbeat_job_protocol": self._heartbeat_runtime.protocol_version,
                    "heartbeat_job_ready": self._heartbeat_runtime.is_available,
                },
            }
            await send_wire_payload(ws, ack_frame)
            logger.info("[AgentWebSocketServer] 已发送 connection.ack: %s", remote)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 发送 connection.ack 失败: %s", e)

        tasks: set[asyncio.Task] = set()

        try:
            async for raw in ws:
                task = asyncio.create_task(self._handle_message(ws, raw, send_lock))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketConnectionClosed as e:
            logger.info(
                "[AgentWebSocketServer] 连接关闭: %s",
                format_ws_diagnostics(
                    {
                        "remote": remote,
                        "active_tasks": len(tasks),
                        "session_stream_tasks": len(self._session_stream_tasks),
                        "ping_interval": self._ping_interval,
                        "ping_timeout": self._ping_timeout,
                    },
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] 连接处理异常 (%s): %s", remote, e)
        finally:
            self._current_ws = None
            self._current_send_lock = None
            self._clear_ws_acp_client_capabilities(ws)
            connection_tasks = list(tasks)
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
            # Gateway 进程退出/端口关闭时，必须先取消各 session 内流式生产者（SessionManager）
            # 并中止 DeepAgent 内层循环；否则仅等待 _handle_message 任务结束会一直阻塞到任务自然完成。
            try:
                await self._agent_manager.cancel_all_inflight_work(
                    reason=f"[gateway ws closed {remote}] ",
                    exclude_session_ids=(
                        self._heartbeat_runtime.execution.active_session_ids()
                    ),
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed")
            # Stop scheduler on server shutdown
            try:
                await self._stop_scheduler()
            except Exception:
                logger.exception("[AgentWebSocketServer] scheduler stop failed")
            try:
                from jiuwenswarm.agents.harness.team import cancel_all_team_stream_tasks_across_managers

                await cancel_all_team_stream_tasks_across_managers(
                    reason=f"[gateway ws closed {remote}] ",
                    exclude_session_ids=(
                        self._heartbeat_runtime.execution.active_session_ids()
                    ),
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] team stream cancel failed")
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            self._session_stream_tasks.clear()

    async def _dispatch_gateway_adapter_request(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Dispatch migrated Gateway user-business RPCs to their adapters.

        The registry is intentionally checked before the legacy handler chain:
        AgentOS requests must operate on this process's injected user directory.
        A small set of legacy handlers is retained when it owns runtime cleanup.
        """
        if request.req_method is None or request.req_method in _GATEWAY_ADAPTER_LEGACY_METHODS:
            return False
        registry = getattr(self, "_adapter_registry", None)
        if registry is None:
            return False
        adapter = registry.get(request.req_method.value)
        if adapter is None:
            return False
        try:
            response = await adapter.handle(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] Gateway adapter failed: request_id=%s method=%s",
                request.request_id,
                request.req_method.value,
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
                metadata=request.metadata,
            )
        if getattr(response, "agent_ref", None) is None:
            response.agent_ref = request.agent_ref
        wire = encode_agent_response_for_wire(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        return True

    async def _handle_gateway_cron_callback(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Consume Gateway-owned cron callbacks without creating a chat turn."""
        if request.req_method not in {
            ReqMethod.CRON_JOBS_SYNC,
            ReqMethod.CRON_COMMAND_ACK,
            ReqMethod.CRON_RUN_NOW_ACK,
        }:
            return False

        params = request.params if isinstance(request.params, dict) else {}
        from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
            install_gateway_jobs_snapshot,
            resolve_gateway_cron_command_ack,
            resolve_gateway_run_ack,
        )

        if request.req_method == ReqMethod.CRON_JOBS_SYNC:
            install_gateway_jobs_snapshot(
                params.get("jobs", []), user_id=request.user_id
            )
        elif request.req_method == ReqMethod.CRON_COMMAND_ACK:
            resolve_gateway_cron_command_ack(
                str(params.get("command_id") or ""),
                {"data": params.get("data")},
            )
        else:
            resolve_gateway_run_ack(
                str(params.get("ack_request_id") or ""),
                str(params.get("run_id") or ""),
            )

        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "ok"},
            metadata=request.metadata,
            agent_ref=request.agent_ref,
        )
        wire = encode_agent_response_for_wire(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        return True

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        """解析一条 JSON 请求并分发到 IAgentServer 处理."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            wire = encode_json_parse_error_wire(
                request_id="",
                channel_id="",
                message=f"JSON 解析失败: {e}",
            )
            try:
                async with send_lock:
                    await send_wire_payload(ws, wire)
            except WebSocketConnectionClosed as send_exc:
                logger.info(
                    "[AgentWebSocketServer] WebSocket 已关闭，JSON 解析错误未发送: %s",
                    format_ws_diagnostics(
                        {"json_error": str(e)},
                        describe_ws_peer(ws),
                        describe_ws_exception(send_exc),
                    ),
                )
            return

        try:
            env = E2AEnvelope.from_dict(data)
        except Exception as parse_err:
            logger.warning(
                "[AgentWebSocketServer] E2A from_dict 失败，按旧载荷解析: %s",
                parse_err,
            )
            request = _payload_to_request(data)
        else:
            jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
            if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
                legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
                logger.warning(
                    "[E2A][fallback] using legacy_agent_request request_id=%s",
                    env.request_id,
                )
                if not isinstance(legacy, dict):
                    raise ValueError("legacy_agent_request missing or not a dict")
                request = _payload_to_request(legacy)
            else:
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                request = e2a_to_agent_request(env)

        logger.info(
            "[AgentWebSocketServer] 收到请求: request_id=%s channel_id=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.is_stream,
        )

        # First touch point of frontend chat input inside AgentServer: record it through the
        # agent-core logging system so it lands in the unified agent log stream.
        if request.req_method == ReqMethod.CHAT_SEND:
            server_logger.info(
                "[AgentServer] chat input received: request_id=%s session_id=%s channel_id=%s query=%s",
                request.request_id,
                request.session_id,
                request.channel_id,
                preview_text(_request_query_text(request)),
            )

        try:
            if request.req_method in _PERSONAL_CONTEXT_REQ_METHODS:
                manager = getattr(self, "_agent_manager", None)
                runtime_callback = getattr(
                    manager, "set_personal_context_runtime_enabled", None
                )
                await handle_personal_context_request(
                    self._personal_context_host,
                    ws,
                    request,
                    send_lock,
                    runtime_enabled_changed=(
                        runtime_callback if callable(runtime_callback) else None
                    ),
                )
                return

            if request.channel_id == "acp" and request.req_method != ReqMethod.INITIALIZE:
                metadata = dict(request.metadata or {})
                ws_caps = self._get_ws_acp_client_capabilities(ws)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or self._agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata

            if await self._handle_gateway_cron_callback(ws, request, send_lock):
                return

            if await self._dispatch_gateway_adapter_request(ws, request, send_lock):
                return

            await self._trigger_before_chat_request_hook(request)

            if request.req_method == ReqMethod.HEARTBEAT_JOB:
                await self._handle_heartbeat_job(ws, request, send_lock)
                return

            if request.req_method == ReqMethod.SESSION_LIST:
                await self._handle_session_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_RENAME:
                await self._handle_session_rename(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_REBIND_PROJECT:
                await self._handle_session_rebind_project(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_SWITCH:
                await self._handle_session_switch(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_KVC_PREPARE:
                await self._handle_session_kvc_prepare(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_DELETE:
                await self._handle_session_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_REWIND:
                await self._handle_session_rewind_full(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_REWIND_AND_RESTORE:
                await self._handle_session_rewind_full(ws, request, send_lock, restore_files=True)
                return
            if request.req_method == ReqMethod.SESSION_REWIND_COMPACT:
                await self._handle_session_rewind_full(ws, request, send_lock, compact=True)
                return
            if request.req_method == ReqMethod.SESSION_REWIND_CONTEXT:
                await self._handle_session_rewind_context(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_TEMPLATES_LIST:
                await self._handle_team_templates_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDINGS_LIST:
                await self._handle_team_bindings_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_CREATE:
                await self._handle_team_binding_create(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_GENERATE:
                await self._handle_team_binding_generate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SESSION_BIND:
                await self._handle_team_session_bind(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_DELETE:
                await self._handle_team_delete(ws, request, send_lock)
                return
            if request.req_method in get_permissions_config_req_methods():
                await self._handle_permissions_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_GET:
                if request.is_stream:
                    await self._handle_history_get_stream(ws, request, send_lock)
                else:
                    await self._handle_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SNAPSHOT:
                await self._handle_team_snapshot(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MQ_PUBLISH:
                await self._handle_team_mq_publish(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.PROACTIVE_TICK:
                await self._handle_proactive_tick(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_WORKFLOWS:
                await self._handle_command_workflows(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_HISTORY_GET:
                await self._handle_team_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MEMBERS_GET:
                await self._handle_team_members_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_ADD_DIR:
                await self._handle_command_add_dir(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CHROME:
                await self._handle_command_chrome(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT:
                await self._handle_command_compact(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT_PARTIAL:
                await self._handle_command_compact_partial(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CONTEXT:
                await self._handle_command_context(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RECAP:
                await self._handle_command_recap(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_BTW:
                await self._handle_command_btw(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_DIFF:
                await self._handle_command_diff(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SIMPLIFY:
                await self._handle_command_simplify(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MODEL:
                await self._handle_command_model(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MCP:
                await self._handle_command_mcp(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_LIST:
                await self._handle_mcp_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SHOW:
                await self._handle_mcp_show(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_CONNECT:
                await self._handle_mcp_connect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_WAIT_AUTH:
                await self._handle_mcp_wait_auth(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DISCONNECT:
                await self._handle_mcp_disconnect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_REGISTER_CUSTOM:
                await self._handle_mcp_register_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DELETE_CUSTOM:
                await self._handle_mcp_delete_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SAVE_CREDENTIALS:
                await self._handle_mcp_save_credentials(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SANDBOX:
                await self._handle_command_sandbox(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RESUME:
                await self._handle_command_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SESSION:
                await self._handle_command_session(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_STATUS:
                await self._handle_command_status(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_RUNTIME_RESTART:
                await self._handle_browser_runtime_restart(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CONFIG_CACHE_CLEAR:
                await self._handle_config_cache_clear(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_RELOAD_CONFIG:
                await self._handle_agent_reload_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_PREWARM_SYNC:
                await self._handle_agent_prewarm_sync(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_LIST:
                await self._handle_extensions_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_IMPORT:
                await self._handle_extensions_import(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_DELETE:
                await self._handle_extensions_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_TOGGLE:
                await self._handle_extensions_toggle(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HOOKS_LIST:
                await self._handle_hooks_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_GET:
                await self._handle_harness_packages_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_SCAN:
                await self._handle_harness_packages_scan(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_ACTIVATE:
                await self._handle_harness_packages_activate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_DEACTIVATE:
                await self._handle_harness_packages_deactivate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_DELETE:
                await self._handle_harness_packages_delete(ws, request, send_lock)
                return
            # Schedule task management
            if request.req_method == ReqMethod.SCHEDULE_CHECK_CONFIG:
                await self._handle_schedule_request(ws, request, send_lock, "check_config")
                return
            if request.req_method == ReqMethod.SCHEDULE_UPDATE_CONFIG:
                await self._handle_schedule_request(ws, request, send_lock, "update_config")
                return
            if request.req_method == ReqMethod.SCHEDULE_CREATE:
                await self._handle_schedule_request(ws, request, send_lock, "create")
                return
            if request.req_method == ReqMethod.SCHEDULE_RUN:
                await self._handle_schedule_request(ws, request, send_lock, "run")
                return
            if request.req_method == ReqMethod.SCHEDULE_LIST:
                await self._handle_schedule_request(ws, request, send_lock, "list")
                return
            if request.req_method == ReqMethod.SCHEDULE_STATUS:
                await self._handle_schedule_request(ws, request, send_lock, "status")
                return
            if request.req_method == ReqMethod.SCHEDULE_LOGS:
                await self._handle_schedule_request(ws, request, send_lock, "logs")
                return
            if request.req_method == ReqMethod.SCHEDULE_CANCEL:
                await self._handle_schedule_request(ws, request, send_lock, "cancel")
                return
            if request.req_method == ReqMethod.SCHEDULE_DELETE:
                await self._handle_schedule_request(ws, request, send_lock, "delete")
                return
            if request.req_method == ReqMethod.ISSUE_WATCH_ONCE:
                await self._handle_schedule_request(ws, request, send_lock, "issue_watch_once")
                return
            if request.req_method == ReqMethod.ISSUE_STATE_LIST:
                await self._handle_schedule_request(ws, request, send_lock, "issue_state_list")
                return
            if request.req_method == ReqMethod.ISSUE_DELETE:
                await self._handle_schedule_request(ws, request, send_lock, "issue_delete")
                return
            if request.req_method == ReqMethod.ISSUE_MATRIX:
                await self._handle_schedule_request(ws, request, send_lock, "issue_matrix")
                return
            if request.req_method == ReqMethod.AGENTS_LIST:
                await self._handle_agents_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_GET:
                await self._handle_agents_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_CREATE:
                await self._handle_agents_create(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_UPDATE:
                await self._handle_agents_update(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_DELETE:
                await self._handle_agents_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_ENABLE:
                await self._handle_agents_set_enabled(ws, request, send_lock, True)
                return
            if request.req_method == ReqMethod.AGENTS_DISABLE:
                await self._handle_agents_set_enabled(ws, request, send_lock, False)
                return
            if request.req_method == ReqMethod.AGENTS_TOOLS_LIST:
                await self._handle_agents_tools_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CHAT_CANCEL:
                # 中断请求：根据 intent 决定是否取消流式任务
                sid = request.session_id or "default"
                intent = request.params.get("intent", "cancel") if isinstance(request.params, dict) else "cancel"
                cleanup_after_cancel = self._is_client_disconnect_cancel_request(request)

                # 只有 cancel/supplement 才取消流式任务
                # pause/resume 不取消，因为任务仍在运行（pause 在 checkpoint 阻塞，resume 解除阻塞）
                stream_tasks: list[asyncio.Task] = []
                if intent in ("cancel", "supplement"):
                    entries = self._session_stream_tasks.get(sid, {})
                    for stream_task, stream_stop_event in list(entries.items()):
                        if stream_task.done():
                            continue
                        logger.info(
                            "[AgentWebSocketServer] cancel: 终止 session 流式任务: session_id=%s intent=%s",
                            sid,
                            intent,
                        )
                        stream_stop_event.set()
                        stream_task.cancel()
                        stream_tasks.append(stream_task)

                cancel_response: AgentResponse | None = None
                try:
                    # 专门处理 cancel，复用已有 agent（不再 fallthrough 到 _handle_unary）
                    # allow_create=False：找不到已有 agent 时不 fallback 新建（见 _handle_cancel docstring）。
                    cancel_response = await self._handle_cancel(
                        ws,
                        request,
                        send_lock,
                        allow_create=False,
                        send_response=not cleanup_after_cancel,
                    )
                finally:
                    if stream_tasks:
                        results = await asyncio.gather(*stream_tasks, return_exceptions=True)
                        for result in results:
                            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                logger.warning(
                                    "[AgentWebSocketServer] cancel: stream task cleanup failed: "
                                    "session_id=%s intent=%s error=%s",
                                    sid,
                                    intent,
                                    result,
                                )
                    if cleanup_after_cancel and intent in ("cancel", "supplement"):
                        cleanup_succeeded = (
                            await self._cleanup_client_disconnect_session_runtime(
                                request
                            )
                        )
                        if cancel_response is not None:
                            if not cleanup_succeeded:
                                cancel_response.ok = False
                                cancel_response.payload = {
                                    "event_type": "chat.interrupt_result",
                                    "success": False,
                                    "error": "session runtime cleanup failed",
                                }
                            wire = encode_agent_response_for_wire(
                                cancel_response,
                                response_id=request.request_id,
                            )
                            async with send_lock:
                                await send_wire_payload(ws, wire)
                return
            await self._ensure_auto_team_binding_for_chat(request)
            if request.is_stream:
                await self._handle_stream(ws, request, send_lock)
            else:
                await self._handle_unary(ws, request, send_lock)
        except asyncio.CancelledError:
            # 流式任务被 interrupt 取消，正常退出无需报错
            logger.info(
                "[AgentWebSocketServer] 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                request.session_id,
            )
        except WebSocketConnectionClosed as e:
            logger.info(
                "[AgentWebSocketServer] WebSocket 已关闭，放弃请求回包: %s",
                format_ws_diagnostics(
                    {
                        "request_id": request.request_id,
                        "channel_id": request.channel_id,
                        "session_id": request.session_id,
                        "is_stream": request.is_stream,
                    },
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
            )
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock, str(e),
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except BaseException as e:
            # Last-resort net for BaseExceptionGroup escapes. A
            # BaseExceptionGroup is only an Exception subclass when *every*
            # sub-exception is, so one containing GeneratorExit/CancelledError
            # (from anyio task-group teardown — the MCP SDK's streamable/
            # SSE clients, but also openjiuwen's runner/manager task groups
            # on cooperative cancellation) sails through ``except Exception``
            # above. Per-call coercion lives in the MCP module
            # (call_timeout_patch / tools fetch); this top-level net catches
            # anything that slips past those, coerces it to an Exception,
            # and sends a failure response so the frontend doesn't hang on a
            # missing res. KeyboardInterrupt and bare CancelledError
            # (cooperative cancellation, e.g. ws disconnect) are re-raised
            # verbatim by reraise_as_exception so they propagate as signals.
            from jiuwenswarm.server.runtime.mcp.exc_group import (
                reraise_as_exception,
            )
            try:
                reraise_as_exception(e)
            except KeyboardInterrupt:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(KeyboardInterrupt): request_id=%s",
                    request.request_id
                )
                raise
            except Exception as coerced:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(BaseException 逃逸): request_id=%s: %s",
                    request.request_id,
                    coerced,
                )
                wire = AgentWebSocketServer._send_error_response(
                    ws, request, send_lock, str(coerced),
                )
                async with send_lock:
                    await send_wire_payload(ws, wire)

    @staticmethod
    def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
        return request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    async def _record_kvc_chat_started(self, request: AgentRequest) -> None:
        """Best-effort KVC task fact; only same-Session evict may block it."""
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                record_chat_started,
            )

            params = request.params if isinstance(request.params, dict) else {}
            await record_chat_started(
                session_id=str(request.session_id or params.get("session_id") or "").strip(),
                params=params,
                channel_id=str(request.channel_id or "default"),
                agent_manager=self._agent_manager,
            )
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] KVC chat-start hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

    def _record_kvc_chat_finished(
        self,
        request: AgentRequest,
        *,
        succeeded: bool,
    ) -> None:
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                record_chat_finished,
            )

            params = request.params if isinstance(request.params, dict) else {}
            record_chat_finished(
                session_id=str(request.session_id or params.get("session_id") or "").strip(),
                succeeded=succeeded,
                agent_manager=self._agent_manager,
            )
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] KVC chat-finish hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

    @staticmethod
    def _is_client_disconnect_cancel_request(request: AgentRequest) -> bool:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return (
            str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
            == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
        )

    async def _cleanup_client_disconnect_session_runtime(self, request: AgentRequest) -> bool:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return False
        channel_id = request.channel_id or "default"
        try:
            cleaned = await self._agent_manager.cleanup_session_runtime(
                channel_id=channel_id,
                session_id=session_id,
            )
            logger.info(
                "[AgentWebSocketServer] client disconnect session runtime cleanup: "
                "channel_id=%s session_id=%s cleaned=%s",
                channel_id,
                session_id,
                cleaned,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] client disconnect session runtime cleanup failed: "
                "channel_id=%s session_id=%s error=%s",
                channel_id,
                session_id,
                exc,
            )
            return False
        finally:
            # Persisted history remains on disk, but this connection-scoped
            # marker must not grow with every short-lived TUI process. Mode
            # locks are weakly cached and disappear automatically after their
            # last active/waiting user releases them.
            _plan_exited_sessions.discard(session_id)
            # 同理：内存标记不留给已断开的会话。真在 plan 里的会话靠 metadata
            # 那道判据继续被识别，不依赖这个集合。
            _plan_active_sessions.discard(session_id)

    async def _trigger_before_chat_request_hook(self, request: AgentRequest) -> None:
        if not self._should_trigger_before_chat_request_hook(request):
            return
        from jiuwenswarm.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params

        ctx = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=request.req_method.value if request.req_method is not None else None,
            params=params,
        )

        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)

    async def _handle_cancel(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        *,
        allow_create: bool = False,
        send_response: bool = True,
    ) -> AgentResponse:
        """处理 CHAT_CANCEL 中断请求：复用已有 agent 实例，避免创建新实例。

        cancel 请求的 params 中可能没有 mode 信息，如果走 _handle_unary 的 get_agent(mode) 路径
        会按默认 mode 创建新的 agent 实例，导致 interrupt 设置到空实例上，无法终止真正运行的 agent。
        因此 cancel 请求必须直接定位已有 agent 来处理。

        默认 allow_create=False：找不到已有 agent 时不 fallback 新建。
        原作者的 fallback 是为"缓存竞态/意外清空"异常兜底设计；但在"agent 首次初始化慢"场景下有害——
        此时目标 agent 仍在 create_instance 的 ensure_initialized 中、尚未写入缓存，get_agent_nowait
        返回 None，fallback 会新建第二个 agent，既无法取消正在初始化的第一个（它在线程里跑、cancel 停不掉
        其同步段），又叠一次阻塞、拖垮 gateway 等不到响应而 timeout。
        改动3 已让主事件循环在初始化期间保持响应（esc 能被读到），配合这里 allow_create=False 直接回
        success，gateway 拿到结果不 timeout、前端停转圈。后端那个初始化仍会在子线程跑完、随后进缓存复用，
        不影响后续任务。
        """
        channel_id = request.channel_id or "default"

        # 1. 尝试按 params 中的 mode 查找已有 agent
        project_dir = resolve_request_project_dir(request)
        mode_param = request.params.get("mode", "")
        if mode_param:
            mode, sub_mode, _canonical = resolve_agent_request_mode(mode_param)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = self._agent_manager.get_agent_nowait(
                channel_id,
                mode=agent_mode,
                project_dir=project_dir,
                sub_mode=sub_mode,
            )
        else:
            agent = None

        # 2. 如果按 mode 没找到，用 get_agent_nowait 找任何已有 agent
        if agent is None:
            agent = self._agent_manager.get_agent_nowait(channel_id, project_dir=project_dir)

        resp: AgentResponse | None = None

        if agent is None and not allow_create:
            # 找不到已有 agent 即视为"无运行中任务"。这覆盖 esc 命中 agent 首次初始化窗口的情况：
            # 目标 agent 仍在 create_instance 的 ensure_initialized 中、尚未写入缓存，
            # get_agent_nowait 返回 None。直接回 success，不 fallback 新建（见 docstring 说明）。
            logger.info(
                "[AgentWebSocketServer] cancel: no existing agent, skip create: "
                "channel_id=%s session_id=%s",
                channel_id,
                request.session_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "event_type": "chat.interrupt_result",
                    "success": True,
                    "message": "当前会话任务已终止",
                },
            )

        # 3. 仍然没找到时 fallback 到 get_agent（异常场景）
        if agent is None and resp is None:
            logger.warning(
                "[AgentWebSocketServer] cancel: 未找到已有 agent，fallback 创建: channel_id=%s",
                channel_id,
            )
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=project_dir,
                sub_mode=sub_mode,
            )

        if agent is None and resp is None:
            raise ValueError("Failed to get agent for cancel request")

        if resp is None:
            resp = await agent.process_message(request)

        if send_response:
            wire = encode_agent_response_for_wire(
                resp,
                response_id=request.request_id,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        return resp

    @staticmethod
    def _resolve_code_language() -> str:
        """Determine the display language for code mode plan approval messages.

        Returns ``"cn"`` or ``"en"`` based on configuration.
        Defaults to ``"cn"`` if the config key is missing.
        """
        try:
            config = get_config()
            return config.get("language", "cn")
        except Exception:
            return "cn"

    @staticmethod
    def _should_sync_code_mode_state(request: AgentRequest) -> bool:
        """Only agent chat turns may change plan/normal mode.

        Background RPCs (e.g. ``skills.list``) also send ``mode: code.normal`` but
        must not run plan-mode restore logic or race with an in-flight approval.
        """
        method = request.req_method
        if method is None:
            return True
        return method in _CODE_MODE_SYNC_METHODS

    @staticmethod
    def _is_explicit_plan_entry_request(request: AgentRequest) -> bool:
        """本次请求是否为"用户明确要求进入 plan"。

        只认一次性的 ``plan_entry_source``：TUI 的 ``/plan`` 发
        ``slash_command``，Web 在用户手动打开 Plan 开关的那一条消息上发
        ``plan_toggle``（开关本身是持续状态，但"刚被打开"只发生一次）。

        不能因为"这是一条 Web 的 plan 请求"就当成显式进入——那样
        ``_plan_exited_sessions`` 与 ``plan_slug`` 两道防重入闸门对 Web 就永远
        不生效：``plan.mode_exited`` 一旦丢包（网络抖动、页面刷新），开关不复位，
        用户的下一条消息会静默把会话重新拖回 plan。
        """
        if not isinstance(request.params, dict):
            return False
        return request.params.get("plan_entry_source") in PLAN_ENTRY_SOURCES

    @staticmethod
    def _session_mode_sync_lock(session_id: str) -> asyncio.Lock:
        lock = _session_mode_sync_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_mode_sync_locks[session_id] = lock
        return lock

    @staticmethod
    def _session_team_binding_lock(session_id: str) -> asyncio.Lock:
        lock = _session_team_binding_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _session_team_binding_locks[session_id] = lock
        return lock

    async def _push_plan_mode_exited(
        self,
        request: AgentRequest,
        *,
        exit_mode: str | None = None,
    ) -> None:
        """Notify the client that plan mode ended after user approval.

        ``mode`` 保持 TUI 已消费的语义：退出 plan 后应回到的普通模式。code 单
        agent 仍是 ``code.normal``；work 单 agent / 集群按各自 profile 动态计算。
        """
        session_id = request.session_id
        if not session_id:
            return
        if not exit_mode:
            # ``normal_mode`` 对非 plan 的 canonical 模式原样返回，所以这里不需要
            # 分情况：plan 请求得到它的退出目标，普通请求得到它自己。别写死
            # "code.normal"——最常走的这条路径（plan→normal 恢复后回调）恰好是
            # 普通请求，写死会把 code 的模式推给 work 会话。
            exit_mode = resolve_request_runtime_mode(request).normal_mode
        await self.send_push({
            "channel_id": request.channel_id or "default",
            "session_id": session_id,
            "payload": {
                "event_type": PLAN_MODE_EXITED_EVENT_TYPE,
                "mode": exit_mode,
            },
        })

    async def _check_post_process_plan_exit(
        self,
        request: AgentRequest,
        agent: Any,
    ) -> None:
        """Detect plan→normal transition that happened inside tool execution.

        When ``exit_plan_mode`` is approved, ``ExitPlanModeTool.invoke()``
        calls ``restore_mode_after_plan_exit()`` to persist the mode change
        to the session checkpointer.  This runs AFTER ``_ensure_code_mode_state``
        has already completed (which only syncs the mode BEFORE processing).

        We check the persisted state here and push a ``plan.mode_exited``
        event so the TUI status bar updates immediately, rather than waiting
        for the next user request.

        Only checks requests whose sub_mode is ``"plan"`` — the transition
        from plan→normal can only happen during a plan-mode request (the LLM
        calls ``exit_plan_mode``).  Checking ``sub_mode == "normal"`` requests
        would produce false positives for every background RPC (e.g.
        ``skills.list``) that uses ``code.normal`` but never had an active
        plan session.
        """
        session_id = request.session_id
        if not session_id:
            return
        resolved = resolve_request_runtime_mode(request)
        if isinstance(request.params, dict):
            request.params["mode"] = resolved.canonical_mode
        # 只检查"本轮确实运行在单 agent plan"的请求：plan→normal 只可能发生在
        # 这类请求里。集群 plan 的退出由 team runtime 自己处理，普通请求不检查，
        # 否则每个 code.normal 背景 RPC 都会误判。
        if resolved.is_team or not resolved.is_plan:
            return

        # 读运行中的那个 session：exit_plan_mode 是在它上面恢复模式的，落盘要等本轮
        # 结束，这里用一次性 session 读 checkpointer 有可能读到退出前的旧值。
        deep_agent, session, _live = await self._open_plan_state_session(
            agent, session_id
        )
        state = deep_agent.load_state(session)
        if state.plan_mode.mode == "normal":
            _plan_exited_sessions.add(session_id)
            _plan_active_sessions.discard(session_id)
            await self._push_plan_mode_exited(request, exit_mode=resolved.normal_mode)
            logger.info(
                "[_check_post_process_plan_exit] Detected plan→normal after "
                "tool execution for session=%s",
                session_id,
            )

    @staticmethod
    def _is_stateless_method_request(request: AgentRequest) -> bool:
        """Return True for prefix-matched read-only RPCs that skip adapter setup."""
        return (
            request.req_method is not None
            and request.req_method.value.startswith(
                ("skills.", "skilldev.", "plugins.", "symphony.",
                 "agent_groups.", "agent_templates.", "plugin_packages.")
            )
        )

    @staticmethod
    def _is_readonly_goal_get_request(request: AgentRequest) -> bool:
        """``command.goal`` + ``action=get``：只读查询，不得兜底新建 session metadata.

        与 skills.list 同类问题：走 ``_prepare_code_mode_chat_turn`` 会触发
        ``sync_session_request_metadata`` 在无 metadata 时写出
        ``metadata.json``。get 仍需要真实 agent（可能从 checkpointer 读已有
        Goal），故不能整段塞进 ``_is_stateless_method_request``。
        """
        if request.req_method != ReqMethod.COMMAND_GOAL:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "get").strip().lower()
        return action == "get"

    async def _get_stateless_agent(self, channel_id: str) -> Any:
        """为无状态请求取 agent，**不触发任何 mode 的 adapter 重建**.

        优先用 AgentManager 已缓存的 agent 模式 agent（get_agent_nowait 命中即返回，
        不命中返回 None，绝不创建）；都没缓存时复用（或首次构造）本 server 上按
        channel 缓存的轻量 JiuWenSwarm()（**不调 create_instance**，_adapter 保持
        None）——其 process_message 内部对 skills/skilldev/plugins/symphony 的无状态
        短路会在 _ensure_adapter 之前 return，碰不到 adapter。真正的 adapter 重建
        留给 chat.send。

        相比 5084467df 原版用 get_agent(mode="agent") 作 fallback（会触发 agent 模式
        adapter 重建，治标不治本），此处彻底解耦。Fallback 必须按 channel 复用，
        否则每次 cache miss 新建 SkillManager，SkillNet install/install_status 会
        落到不同实例并误报「安装会话已过期」。
        """
        cached = self._agent_manager.get_agent_nowait(
            channel_id=channel_id, mode="agent"
        )
        if cached is not None:
            return cached
        agent = self._stateless_fallback_agents.get(channel_id)
        if agent is not None:
            return agent
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
        agent = JiuWenSwarm()  # 不调 create_instance，_adapter 保持 None
        self._stateless_fallback_agents[channel_id] = agent
        return agent

    async def _prepare_code_mode_chat_turn(
        self,
        request: AgentRequest,
        channel_id: str,
        *,
        sync_metadata: bool = True,
    ) -> tuple[str, str | None, Any]:
        """Mode resolution and correct agent instance selection."""
        # [新增] 在 _apply_resolved_mode_to_request 把 canonical mode 写回 params 之前，
        # 先记录请求是否「显式」携带了 mode。下游 sync 用它做守卫：未显式携带则不覆盖
        # 磁盘已锁定的会话 mode（避免只读 RPC 用默认推断值腐蚀 team 等已锁定 mode）。
        # model 的显式与否由 _sync_chat_request_request_metadata 内部从 params 判断
        # （model_name 不会被规范化改写），故此处只捕获 mode 标志。
        # 注意：用与下游一致的严格判断——纯空白串 "   " 不算显式携带（bool("   ") 为 True
        # 会误判，导致空白 mode 走默认推断 agent.plan 并写盘腐蚀已锁定 mode）。
        params = request.params if isinstance(request.params, dict) else {}
        _raw_mode = params.get("mode")
        explicit_mode_provided = isinstance(_raw_mode, str) and bool(_raw_mode.strip())
        runtime_work_mode = None
        session_metadata: dict[str, Any] = {}
        sid = str(request.session_id or "").strip()
        if sid:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            session_metadata = get_session_metadata(
                sid,
                cache_bust=True,
                enable_writeback=False,
            )
            stored_work_mode = (
                session_metadata.get("work_mode")
                if isinstance(session_metadata, dict)
                else None
            )
            # 下面的 sync 会把本轮 canonical mode 覆盖进 metadata，所以在覆盖前
            # 先把上一轮的值捎带给 _ensure_code_mode_state：它据此判断这个会话是
            # 不是可能还停在 plan 里（跨进程重启依然有效）。
            stored_session_mode = (
                session_metadata.get("mode")
                if isinstance(session_metadata, dict)
                else None
            )
            if isinstance(stored_session_mode, str) and stored_session_mode.strip():
                stored_session_mode = stored_session_mode.strip()
                params[_SESSION_PREVIOUS_MODE_KEY] = stored_session_mode
                if not explicit_mode_provided:
                    # Internal Heartbeat requests are ordinary CHAT_SENDs and
                    # intentionally omit ``mode``.  Runtime selection must
                    # therefore inherit the Session's locked canonical mode;
                    # otherwise the generic resolver falls back to ``agent``
                    # and a Team Heartbeat silently runs through the wrong
                    # adapter.  Keep ``explicit_mode_provided`` false: this is
                    # inheritance, not a client-requested mode transition.
                    params["mode"] = stored_session_mode
            if isinstance(stored_work_mode, str) and stored_work_mode.strip().lower() in {
                "code",
                "work",
            }:
                runtime_work_mode = stored_work_mode.strip().lower()
            elif isinstance(stored_session_mode, str) and stored_session_mode.strip():
                # Sessions created before work_mode became authoritative only
                # persist the canonical execution mode. Heartbeat requests do
                # not carry a mode override, so derive the legacy profile from
                # that locked value instead of falling back to the channel
                # default (Web=work, TUI=code), which could resume the same
                # Session through the wrong adapter.
                runtime_work_mode = (
                    "code" if is_code_profile_mode(stored_session_mode) else "work"
                )
        if runtime_work_mode is None:
            request_work_mode = params.get("work_mode")
            if isinstance(request_work_mode, str) and request_work_mode.strip().lower() in {
                "code",
                "work",
            }:
                runtime_work_mode = request_work_mode.strip().lower()
            else:
                from jiuwenswarm.server.runtime.session.work_mode import (
                    default_work_mode_for_channel,
                )
                channel_id_for_default = channel_id or request.channel_id or "web"
                runtime_work_mode = default_work_mode_for_channel(channel_id_for_default)
                logger.warning(
                    "[_prepare_code_mode_chat_turn] work_mode missing in both session "
                    "metadata and request params; defaulting to %r for channel=%s session=%s",
                    runtime_work_mode,
                    channel_id_for_default,
                    request.session_id,
                )
        params["work_mode"] = runtime_work_mode
        mode, sub_mode = _apply_resolved_mode_to_request(
            request,
            work_mode=runtime_work_mode,
        )
        agent_mode = "agent" if mode == "auto_harness" else mode
        requested_project_dir = resolve_request_project_dir(request)
        # [改动] 写盘用 canonical mode（request.params["mode"]，已被规范化为
        # "agent.plan"/"team" 等），而非一级 mode（"agent"），使磁盘出现你期望的两类值。
        canonical_mode = (
            request.params.get("mode") if isinstance(request.params, dict) else None
        )
        if sync_metadata:
            project_dir = _sync_chat_request_metadata(
                request,
                requested_project_dir,
                canonical_mode if canonical_mode else mode,
                explicit_mode_provided=explicit_mode_provided,
                user_id=str(getattr(request, "user_id", "") or "").strip(),
            )
            if sid:
                # _sync_chat_request_metadata may have initialized a legacy
                # metadata record. Re-read the cache so the first post-upgrade
                # turn receives the value that was atomically locked above.
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )

                session_metadata = get_session_metadata(
                    sid,
                    enable_writeback=False,
                )
        else:
            # Read-only path (e.g. command.goal get): never create/update
            # metadata.json. Prefer request project_dir, else locked disk value.
            project_dir = requested_project_dir
            if not (isinstance(project_dir, str) and project_dir.strip()):
                sid = str(request.session_id or "").strip()
                if sid:
                    from jiuwenswarm.server.runtime.session.session_metadata import (
                        get_session_metadata,
                    )

                    meta = get_session_metadata(
                        sid, cache_bust=True, enable_writeback=False
                    )
                    locked = meta.get("project_dir") if isinstance(meta, dict) else None
                    if isinstance(locked, str) and locked.strip():
                        project_dir = locked.strip()
        if isinstance(project_dir, str) and project_dir.strip():
            project_dir = project_dir.strip()
            request.params["project_dir"] = project_dir
            request.metadata = dict(request.metadata or {})
            request.metadata["project_dir"] = project_dir

        # Public clients configure Persist Session only during session.create.
        # Per-turn values are never authoritative. Keep the existing adapter
        # runtime key internal to minimize changes in Deep/Code adapters and the
        # EternalConversationRail itself.
        effective_persist_session = (
            session_metadata.get("persist_session") is True
            if isinstance(session_metadata, dict)
            else False
        )
        requested_persist_session = params.pop("persist_session", None)
        requested_legacy_eternal = params.get("eternal_conversation_enabled")
        if (
            isinstance(requested_persist_session, bool)
            and requested_persist_session != effective_persist_session
        ):
            logger.warning(
                "会话 %s 的 persist_session 已锁定为 %s，忽略 chat 请求值 %s",
                sid,
                effective_persist_session,
                requested_persist_session,
            )
        if (
            isinstance(requested_legacy_eternal, bool)
            and requested_legacy_eternal != effective_persist_session
        ):
            logger.warning(
                "会话 %s 的 Persist Session 权威值为 %s，忽略旧式运行时值 %s",
                sid,
                effective_persist_session,
                requested_legacy_eternal,
            )
        params["eternal_conversation_enabled"] = effective_persist_session

        await self._agent_manager.wait_for_session_prewarm(request.session_id)
        agent = await self._agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )
        if agent is None:
            raise ValueError("Failed to get agent")

        return mode, sub_mode, agent

    @staticmethod
    def _session_may_hold_plan_state(request: AgentRequest, session_id: str) -> bool:
        """会话是否可能还停在 plan 里，需要同步 plan 状态。

        两道判据：本进程内的 ``_plan_active_sessions`` 标记（精确），以及会话
        metadata 里上一轮的 canonical mode（跨重启仍然有效——服务重启后一个停在
        plan 里的会话，下一条普通消息依然能被切回 normal 并通知前端复位）。

        Args:
            request: 当前请求（读其中捎带的上一轮 canonical mode）。
            session_id: 会话 ID。

        Returns:
            ``True`` 表示需要继续做 plan 状态同步。
        """
        if session_id in _plan_active_sessions:
            return True
        params = request.params if isinstance(request.params, dict) else {}
        return is_plan_mode(params.get(_SESSION_PREVIOUS_MODE_KEY))

    @staticmethod
    async def _open_plan_state_session(
        agent: Any,
        session_id: str | None,
    ) -> tuple[Any, Any, bool]:
        """Return ``(deep_agent, session, is_live)`` for reading/writing plan state.

        ``DeepAgent.load_state`` caches its snapshot on the Session object, and a
        chat turn keeps reusing the one ``start_interaction`` bound. Writing plan
        state through a throwaway session therefore only reaches the
        checkpointer: the running conversation would keep the pre-switch snapshot
        and the user's Plan toggle would do nothing until the agent instance is
        rebuilt.

        A live session already exists from the second turn on. On the first
        turn we start that same session adapter first (the chat path will
        reuse it) and write plan state there. Falling back to a throwaway
        session is last-resort only: a concurrent first ``chat.send`` /
        ``command.goal`` can bind a normal-mode snapshot after the
        throwaway commit, and rails then hide ``enter_plan_mode``.
        """
        from openjiuwen.core.single_agent import create_agent_session
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            resolve_live_agent_session,
        )

        live_deep_agent = agent.get_live_session_instance(session_id)
        if live_deep_agent is not None:
            live_session = resolve_live_agent_session(live_deep_agent, session_id or "default")
            if live_session is not None:
                return live_deep_agent, live_session, True

        starter = getattr(agent, "ensure_live_session_instance", None)
        if callable(starter):
            try:
                started = starter(session_id)
                if inspect.isawaitable(started):
                    started = await started
            except Exception as exc:
                logger.warning(
                    "[_open_plan_state_session] failed to start live session "
                    "for session=%s: %s; falling back to throwaway",
                    session_id,
                    exc,
                )
            else:
                if started is not None:
                    live_session = resolve_live_agent_session(
                        started, session_id or "default"
                    )
                    if live_session is not None:
                        return started, live_session, True

        deep_agent = await agent.ensure_instance()
        session = create_agent_session(session_id=session_id, card=deep_agent.card)
        await session.pre_run(inputs=None)  # 从 checkpointer 加载历史 state
        return deep_agent, session, False

    async def _ensure_code_mode_state(
        self,
        request: AgentRequest,
        mode: str,
        sub_mode: str,
        agent: Any,
    ) -> bool:
        """code 模式：确保 agent 的 plan_mode 状态正确，必要时执行 switch_mode 并持久化.

        当 plan 刚完成时跳过陈旧的 normal→plan switch_mode，
        避免 exit_plan_mode 已恢复的模式被覆盖；显式用户 /plan 进入除外.
        switch_mode 内部已通过 save_state 写入正确的 "deepagent" key，
        此处只需 post_run 持久化到 checkpointer.

        切换到 plan 模式且尚未调用 enter_plan_mode 时，注入 <system-reminder>
        告知 LLM 调用 enter_plan_mode。

        ``exit_plan_mode`` now restores mode immediately inside the tool
        (via ``restore_mode_after_plan_exit``), so this method no longer needs
        to gate plan→normal transitions with an approval flag.

        work 单 agent（Web ``agent`` / ``agent.plan``）复用同一套编排：Adapter 不同，
        但 plan 状态都存放在 ``DeepAgentState.plan_mode``。集群的 plan 由 team
        runtime 负责，不走这里。

        Returns:
            ``True`` if plan mode was restored to normal (mode sync occurred).
        """
        resolved = resolve_request_runtime_mode(request)
        if resolved.is_team:
            return False
        is_code_single = mode == "code" and sub_mode != "team"
        # work 单 agent：Web 组合（agent / agent.plan + work_mode）与 TUI 直发的新
        # canonical（agent.work.plan）都归属 manager_mode="agent"，plan 状态统一放在
        # DeepAgentState.plan_mode。不再依赖 from_web_composition，否则 TUI /plan 进入
        # 的 agent.work.plan 会漏同步 plan 状态。
        is_work_single_plan_capable = resolved.manager_mode == "agent"
        if not (is_code_single or is_work_single_plan_capable):
            return False
        # 目标 plan 状态由 canonical mode 决定：code 单 agent 沿用 sub_mode，
        # work 单 agent 的 sub_mode 只有 None / "plan"。
        target_plan_state = "plan" if resolved.is_plan else "normal"
        session_id = request.session_id or "default"
        # work 的准入面覆盖 IM / 定时任务 / CLI / Web work 的每一条普通消息
        # （``work_mode`` 总会被 session metadata 补齐），而这些会话绝大多数从未
        # 开过 Plan。打开 plan 状态 session 在会话首轮还没有 live session 时会强制
        # 构建 root DeepAgent（重跑工具注册、rail 装配、MCP 注册），代价不小。
        # 所以普通请求先看这个会话有没有 plan 痕迹，没有就直接返回。
        # code 单 agent 不走这条捷径，保持既有行为。
        if (
            not is_code_single
            and target_plan_state == "normal"
            and not self._session_may_hold_plan_state(request, session_id)
        ):
            return False
        if not self._should_sync_code_mode_state(request):
            return False
        if is_interrupt_resume_payload(request.params):
            logger.info(
                "[_ensure_code_mode_state] Skip mode sync while resuming tool interrupt "
                "for session=%s source=%s",
                request.session_id,
                (request.params or {}).get("source") if isinstance(request.params, dict) else None,
            )
            return False

        restored_after_approval = False
        async with self._session_mode_sync_lock(session_id):
            deep_agent, session, live = await self._open_plan_state_session(
                agent, request.session_id
            )
            state = deep_agent.load_state(session)
            # switch_mode 会就地改写这个 state 对象（load_state 返回的是 session 上
            # 缓存的同一个实例），所以切换前先把原模式记下来。
            previous_plan_state = state.plan_mode.mode
            # 仅在目标模式与当前模式不同时执行模式切换
            mode_changed_to_plan = False
            if state.plan_mode.mode != target_plan_state:
                # Guard: block stale normal→plan switches when plan was already exited.
                # Explicit user /plan requests bypass this guard and start a fresh plan.
                # Two mechanisms:
                #   1. _plan_exited_sessions flag (precise — set by _check_post_process_plan_exit)
                #   2. plan_slug fallback (defense-in-depth — plan exists but mode is normal)
                if state.plan_mode.mode == "normal" and target_plan_state == "plan":
                    blocked = False
                    explicit_plan_entry = self._is_explicit_plan_entry_request(request)
                    if explicit_plan_entry:
                        _plan_exited_sessions.discard(session_id)
                    elif session_id in _plan_exited_sessions:
                        _plan_exited_sessions.discard(session_id)
                        blocked = True
                        logger.info(
                            "[_ensure_code_mode_state] Blocked stale plan re-entry via "
                            "flag for session=%s",
                            session_id,
                        )
                    elif state.plan_mode.plan_slug is not None:
                        # Fallback: plan was completed, checkpoint is authoritative.
                        # Clear slug so this guard is one-shot.
                        state.plan_mode.plan_slug = None
                        deep_agent.save_state(session, state)
                        await session.commit()
                        blocked = True
                        logger.info(
                            "[_ensure_code_mode_state] Blocked stale plan re-entry via "
                            "plan_slug for session=%s",
                            session_id,
                        )
                    if blocked:
                        exit_mode = resolved.normal_mode
                        if isinstance(request.params, dict):
                            request.params["mode"] = exit_mode
                        await self._push_plan_mode_exited(request, exit_mode=exit_mode)
                        return False
                deep_agent.switch_mode(session=session, mode=target_plan_state)
                if previous_plan_state == "plan" and target_plan_state == "normal":
                    restored_after_approval = True
                    _plan_active_sessions.discard(session_id)
                    logger.info(
                        "[_ensure_code_mode_state] Synced plan→normal for session=%s",
                        session_id,
                    )
                if target_plan_state == "plan":
                    mode_changed_to_plan = True
                    _plan_active_sessions.add(session_id)
                    # Clear stale plan_slug from previous plan session so
                    # enter_plan_mode creates a fresh plan file.
                    state = deep_agent.load_state(session)
                    if state.plan_mode.plan_slug:
                        state.plan_mode.plan_slug = None
                        deep_agent.save_state(session, state)
                # switch_mode 内部已通过 save_state 写入 "deepagent" key，这里只需
                # 落盘。用 commit 而不是 post_run：live session 还要继续跑这一轮，
                # post_run 会关掉输出流并把它标记成已结束。
                await session.commit()
                logger.info(
                    "[_ensure_code_mode_state] plan state -> %s for session=%s (live=%s)",
                    target_plan_state,
                    session_id,
                    live,
                )

            # 切换到 plan 模式时注入 <system-reminder> 告知 LLM 调用 enter_plan_mode。
            # 使用 mode_changed_to_plan 而非 plan_slug 判断，因为 restore_mode_after_plan_exit
            # 不清除 plan_slug，导致后续 /plan 时提醒被错误跳过。
            if target_plan_state == "plan" and mode_changed_to_plan:
                _inject_plan_mode_activation_reminder(request)

        return restored_after_approval

    async def _handle_unary(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        manager = getattr(self, "_agent_manager", None)
        foreground = (
            request.req_method in _CODE_MODE_SYNC_METHODS
            and manager is not None
            and hasattr(manager, "begin_foreground_chat")
            and hasattr(manager, "end_foreground_chat")
        )
        tracks_kvc_task = self._should_trigger_before_chat_request_hook(request)
        kvc_task_succeeded = False
        if tracks_kvc_task:
            await self._record_kvc_chat_started(request)
        if foreground:
            await manager.begin_foreground_chat()
        admitted = (
            request.req_method in _CODE_MODE_SYNC_METHODS
            and not is_team_params(request.params)
        )
        session_id = request.session_id or "default"
        if admitted:
            await self._heartbeat_runtime.admission.begin_user(session_id)
        try:
            await self._handle_unary_impl(ws, request, send_lock)
            kvc_task_succeeded = True
        finally:
            if admitted:
                await self._heartbeat_runtime.admission.end_user(session_id)
            if foreground:
                await manager.end_foreground_chat()
            if tracks_kvc_task:
                self._record_kvc_chat_finished(request, succeeded=kvc_task_succeeded)

    async def _handle_unary_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
        # 兜底确保 checkpointer 就绪: start() 里改为后台预热后, 首条请求可能赶在
        # 预热完成前到达。ensure_persistent_checkpointer 内部 lock+ready 幂等, 预热
        # 完成时秒过; 未完成则阻塞至完成 (避免用到未就绪的 checkpointer)。
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer

        await ensure_persistent_checkpointer()
        await self._try_start_heartbeat_runtime()
        channel_id = request.channel_id or "default"

        if request.req_method == ReqMethod.INITIALIZE:
            await self._handle_initialize(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_CREATE:
            await self._handle_session_create(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_FORK:
            await self._handle_session_fork(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.ACP_TOOL_RESPONSE:
            await self._handle_acp_tool_response(ws, request, send_lock)
            return

        # 无状态请求（skills / skilldev / plugins / symphony）不需要 mode 解析和
        # code mode 状态管理，直接走 process_message 即可。用轻量 agent 获取，不触发
        # adapter 重建（恢复 8f54b26a7 误删的短路，并修正 5084467df 触发重建的缺陷）。
        if self._is_stateless_method_request(request):
            agent = await self._get_stateless_agent(channel_id)
            resp = await agent.process_message(request)
            if getattr(resp, "agent_ref", None) is None:
                resp.agent_ref = request.agent_ref
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            logger.info(
                "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
                request.request_id,
            )
            return

        readonly_goal_get = self._is_readonly_goal_get_request(request)
        mode, sub_mode, agent = await self._prepare_code_mode_chat_turn(
            request,
            channel_id,
            sync_metadata=not readonly_goal_get,
        )

        if not readonly_goal_get:
            restored_plan = await self._ensure_code_mode_state(
                request, mode, sub_mode, agent
            )
            if restored_plan:
                await self._push_plan_mode_exited(request)

        resp = None
        try:
            resp = await agent.process_message(request)
        finally:
            # Push plan.mode_exited if exit_plan_mode restored mode during processing
            if not readonly_goal_get:
                await self._check_post_process_plan_exit(request, agent)

        # V2: 非流式响应回带请求侧 agent_ref，供 gateway 3 元组路由（设计 §6.3）。
        # is None 守卫：保留 agent 层显式设置的 agent_ref（如 team 模式由事件派生）。
        if getattr(resp, "agent_ref", None) is None:
            resp.agent_ref = request.agent_ref

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
        )


    async def _handle_stream(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        manager = getattr(self, "_agent_manager", None)
        foreground = (
            request.req_method in _CODE_MODE_SYNC_METHODS
            and manager is not None
            and hasattr(manager, "begin_foreground_chat")
            and hasattr(manager, "end_foreground_chat")
        )
        tracks_kvc_task = self._should_trigger_before_chat_request_hook(request)
        kvc_task_succeeded = False
        if tracks_kvc_task:
            await self._record_kvc_chat_started(request)
        if foreground:
            await manager.begin_foreground_chat()
        admitted = (
            request.req_method in _CODE_MODE_SYNC_METHODS
            and not is_team_params(request.params)
        )
        session_id = request.session_id or "default"
        if admitted:
            await self._heartbeat_runtime.admission.begin_user(session_id)
        try:
            await self._handle_stream_impl(ws, request, send_lock)
            kvc_task_succeeded = True
        finally:
            if admitted:
                await self._heartbeat_runtime.admission.end_user(session_id)
            if foreground:
                await manager.end_foreground_chat()
            if tracks_kvc_task:
                self._record_kvc_chat_finished(request, succeeded=kvc_task_succeeded)

    async def _handle_heartbeat_job(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Serve the stable heartbeat.job.* API from the local Rail runtime."""
        from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import (
            HeartbeatRuntimeUnavailableError,
        )

        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "").strip()
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        effective_user_id = str(request.user_id or "").strip()
        if not effective_user_id and request.session_id:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            effective_user_id = str(
                (get_session_metadata(request.session_id) or {}).get("user_id") or ""
            ).strip()
        try:
            result = await self._heartbeat_runtime.handle_operation(
                action,
                data,
                channel_id=request.channel_id or "web",
                session_id=request.session_id or "",
                user_id=effective_user_id,
                source=("tui_rpc" if request.channel_id == "tui" else "web_rpc"),
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result},
                metadata=request.metadata,
            )
        except PermissionError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "FORBIDDEN"},
                metadata=request.metadata,
            )
        except KeyError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc).strip("'"), "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        except ValueError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except HeartbeatRuntimeUnavailableError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SERVICE_UNAVAILABLE"},
                metadata=request.metadata,
            )
        except RuntimeError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "CONFLICT"},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(
            response,
            response_id=request.request_id,
        )
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def execute_internal_heartbeat(self, request: AgentRequest) -> None:
        """Execute a Heartbeat through the ordinary CHAT_SEND session path.

        The execution task is owned by AgentServer rather than a Gateway
        connection. Gateway delivery is best-effort; session history remains
        authoritative when no channel is connected.
        """
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            ensure_persistent_checkpointer,
        )

        await ensure_persistent_checkpointer()
        channel_id = request.channel_id or "web"
        mode, sub_mode, agent = await self._prepare_code_mode_chat_turn(
            request,
            channel_id,
        )
        self._heartbeat_runtime.retain_agent(request.session_id or "default", agent)
        restored_plan = await self._ensure_code_mode_state(
            request,
            mode,
            sub_mode,
            agent,
        )
        if restored_plan:
            await self._push_plan_mode_exited(request)

        params = request.params if isinstance(request.params, dict) else {}
        prompt = str(params.get("content") or params.get("query") or "")
        # Ordinary Gateway-originated CHAT_SEND emits this frame before the
        # model stream starts. AgentServer-owned Heartbeat bypasses that path,
        # so it must publish the same lifecycle boundary itself. Besides
        # driving processing state, ``content`` lets Web render the persisted
        # Heartbeat user turn before the matching run_id assistant response.
        await self.send_push(
            {
                "request_id": request.request_id,
                "channel_id": channel_id,
                "session_id": request.session_id,
                "payload": {
                    "event_type": "chat.processing_status",
                    "session_id": request.session_id,
                    "is_processing": True,
                    "is_complete": False,
                    "content": prompt,
                },
                "is_complete": False,
                "metadata": request.metadata,
            }
        )

        response_stream = agent.process_message_stream(request)
        processing_finished = False
        try:
            async for chunk in response_stream:
                if chunk.agent_ref is None:
                    chunk.agent_ref = request.agent_ref
                # A runtime may emit another processing=true frame without
                # content. Preserve the prompt on every start frame so clients
                # never replace this run's visible user turn with an empty one.
                payload = chunk.payload
                is_processing_start = (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.processing_status"
                    and bool(payload.get("is_processing"))
                )
                content_is_missing = isinstance(payload, dict) and not str(
                    payload.get("content") or ""
                )
                if is_processing_start and content_is_missing:
                    chunk.payload = {**payload, "content": prompt}
                chunk_finishes_processing = (
                    isinstance(chunk.payload, dict)
                    and chunk.payload.get("event_type") == "chat.processing_status"
                    and chunk.payload.get("is_processing") is False
                )
                pushed = await self.send_push(
                    {
                        "request_id": request.request_id,
                        "channel_id": channel_id,
                        "session_id": request.session_id,
                        "payload": chunk.payload,
                        "is_complete": chunk.is_complete,
                        "metadata": request.metadata,
                    }
                )
                if chunk_finishes_processing and pushed:
                    processing_finished = True
        finally:
            try:
                close_stream = getattr(response_stream, "aclose", None)
                if callable(close_stream):
                    await close_stream()
            finally:
                try:
                    # The ordinary Gateway stream wrapper emits the matching
                    # processing=false boundary when a stream ends. An
                    # AgentServer-owned Heartbeat bypasses that wrapper, so a
                    # fast cancel can otherwise leave Web/TUI permanently
                    # busy after the synthetic processing=true frame above.
                    if not processing_finished:
                        await self.send_push(
                            {
                                "request_id": request.request_id,
                                "channel_id": channel_id,
                                "session_id": request.session_id,
                                "payload": {
                                    "event_type": "chat.processing_status",
                                    "session_id": request.session_id,
                                    "is_processing": False,
                                    "is_complete": True,
                                },
                                "is_complete": False,
                                "metadata": request.metadata,
                            }
                        )
                finally:
                    await self._check_post_process_plan_exit(request, agent)

    async def _handle_stream_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
        # 兜底确保 checkpointer 就绪 (见 _handle_unary 同名注释)。
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer

        await ensure_persistent_checkpointer()
        await self._try_start_heartbeat_runtime()
        channel_id = request.channel_id or "default"
        session_id = request.session_id or "default"
        current_task = asyncio.current_task()
        stream_stop_event = asyncio.Event()
        if current_task is not None:
            self._session_stream_tasks.setdefault(session_id, {})[current_task] = stream_stop_event

        # 无状态流式请求（skills / skilldev / plugins / symphony）不需要 mode 解析和
        # code mode 状态管理，直接走 process_message_stream 即可。用轻量 agent 获取，
        # 不触发 adapter 重建（恢复 8f54b26a7 误删的短路，并修正 5084467df 触发重建的缺陷）。
        readonly_goal_get = self._is_readonly_goal_get_request(request)
        if self._is_stateless_method_request(request):
            agent = await self._get_stateless_agent(channel_id)
        else:
            mode, sub_mode, agent = await self._prepare_code_mode_chat_turn(
                request,
                channel_id,
                sync_metadata=not readonly_goal_get,
            )

            if not readonly_goal_get:
                restored_plan = await self._ensure_code_mode_state(
                    request, mode, sub_mode, agent
                )
                if restored_plan:
                    await self._push_plan_mode_exited(request)

        chunk_count = 0
        # 心跳控制：当有真实 chunk 发送时重置，空闲时发送心跳
        heartbeat_event = asyncio.Event()
        heartbeat_task: asyncio.Task | None = None

        async def _heartbeat_loop() -> None:
            """后台心跳任务：在空闲期间定期发送 keepalive chunk."""
            try:
                while True:
                    # 等待心跳间隔，如果期间有真实 chunk 发送则 heartbeat_event 被设置，重置等待
                    try:
                        await asyncio.wait_for(
                            heartbeat_event.wait(),
                            timeout=_STREAM_HEARTBEAT_INTERVAL_SECONDS,
                        )
                        # 有真实 chunk 发送，重置 event 继续等待
                        heartbeat_event.clear()
                    except asyncio.TimeoutError:
                        # 超时：空闲超过心跳间隔，发送 keepalive chunk
                        heartbeat_chunk = AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=channel_id,
                            payload={"event_type": "keepalive"},
                            is_complete=False,
                        )
                        # V2: 心跳 chunk 也回带 agent_ref，避免切换 mode 后
                        # 旧 session 心跳错路由到新 agent 窗口（设计 §5.2 场景 2）。
                        if heartbeat_chunk.agent_ref is None:
                            heartbeat_chunk.agent_ref = request.agent_ref
                        wire = encode_agent_chunk_for_wire(
                            heartbeat_chunk,
                            response_id=request.request_id,
                            sequence=-1,  # 心跳使用特殊序列号 -1
                        )
                        async with send_lock:
                            await send_wire_payload(ws, wire)
                        logger.info(
                            "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                            request.request_id,
                        )
            except asyncio.CancelledError:
                pass
            except WebSocketConnectionClosed:
                logger.info(
                    "[AgentWebSocketServer] keepalive 停止，WebSocket 已关闭: request_id=%s",
                    request.request_id,
                )

        # 启动心跳任务
        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        response_stream = agent.process_message_stream(request)
        try:
            async for chunk in response_stream:
                chunk_count += 1
                # 通知心跳任务有真实 chunk 发送，重置心跳计时
                heartbeat_event.set()
                # V2: chunk 回带请求侧 agent_ref，供 gateway 3 元组精确路由
                # （设计 §6.3）。is None 守卫：保留 team 模式由事件派生的 agent_ref
                # （_build_team_event_chunk_meta 已设值），不覆盖。
                if chunk.agent_ref is None:
                    chunk.agent_ref = request.agent_ref
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=chunk_count - 1,
                )
                # 诊断：打印前 3 个和每 50 个 chunk 的发送情况
                if chunk_count <= 3 or chunk_count % 50 == 0:
                    _pl = getattr(chunk, "payload", None) or {}
                    _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                    logger.info(
                        "[AgentWebSocketServer] chunk sent: request_id=%s seq=%s"
                        " event_type=%s wire_keys=%s",
                        request.request_id, chunk_count - 1, _et,
                        list(wire.keys())[:10] if isinstance(wire, dict) else "non-dict",
                    )
                try:
                    async with send_lock:
                        sent_original = await send_wire_payload(ws, wire)
                    if not sent_original:
                        logger.warning(
                            "[AgentWebSocketServer] 流式响应因单个 chunk 超限而停止: "
                            "request_id=%s seq=%s",
                            request.request_id,
                            chunk_count - 1,
                        )
                        return
                except WebSocketConnectionClosed:
                    logger.info(
                        "[AgentWebSocketServer] 流式响应停止，WebSocket 已关闭: request_id=%s",
                        request.request_id,
                    )
                    return
                # 清除 event，让心跳任务重新开始计时
                heartbeat_event.clear()
        finally:
            close_stream = getattr(response_stream, "aclose", None)
            if callable(close_stream):
                await close_stream()
            # 停止心跳任务
            if heartbeat_task is not None:
                logger.info(
                    "[AgentWebSocketServer] cancelling heartbeat_task: request_id=%s",
                    request.request_id,
                )
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                    logger.info(
                        "[AgentWebSocketServer] heartbeat_task cancelled cleanly: request_id=%s",
                        request.request_id,
                    )
                except asyncio.CancelledError:
                    logger.info(
                        "[AgentWebSocketServer] heartbeat_task cancelled (CancelledError): request_id=%s",
                        request.request_id,
                    )
                    pass
                except WebSocketConnectionClosed:
                    logger.info(
                        "[AgentWebSocketServer] heartbeat_task cancelled (ConnectionClosed): request_id=%s",
                        request.request_id,
                    )
                    pass
            # 清除自身的宿主生命周期记录；同 session 的其它请求不受影响。
            entries = self._session_stream_tasks.get(session_id)
            if entries is not None and current_task is not None:
                entries.pop(current_task, None)
                if not entries:
                    self._session_stream_tasks.pop(session_id, None)
            # Push plan.mode_exited if exit_plan_mode restored mode during processing
            if not readonly_goal_get:
                await self._check_post_process_plan_exit(request, agent)

        logger.info(
            "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
            request.request_id,
            chunk_count,
        )

    async def _handle_session_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.list 请求：返回历史会话基础信息列表。

        响应格式与 Web fallback ``_session_list`` 保持一致:
        ``{"sessions": [...], "total": int, "limit": int, "offset": int}``,
        确保按新接口接入分页的 Web 前端能拿到分页元信息。
        """
        # 解析 limit/offset(与 Web fallback 一致的宽松解析)
        params = request.params if isinstance(request.params, dict) else {}
        limit = 20
        offset = 0
        raw_limit = params.get("limit")
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
            limit = raw_limit
        elif isinstance(raw_limit, float) and raw_limit.is_integer():
            limit = int(raw_limit)
        elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
            limit = int(raw_limit.strip())

        raw_offset = params.get("offset")
        if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
            offset = raw_offset
        elif isinstance(raw_offset, float) and raw_offset.is_integer():
            offset = int(raw_offset)
        elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
            offset = int(raw_offset.strip())

        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        try:
            sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)
        except Exception as exc:
            logger.warning("[AgentWebSocketServer] 获取会话列表失败: %s", exc)
            sessions, total = [], 0

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "sessions": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_rename(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.rename：与 CLI Gateway 本地回退共用 apply_session_rename。"""
        from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename

        sid = request.session_id or ""
        ch = (request.channel_id or "").strip() or "tui"
        ok, payload, err, code = apply_session_rename(
            request.params,
            sid,
            init_channel_id=ch,
        )
        if ok:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload or {},
                metadata=request.metadata,
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": err or "session.rename failed", "code": code or ""},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_rebind_project(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.rebind_project：TUI /workspace set 切换工作目录时重绑当前会话。

        AgentServer 拥有会话运行态与 metadata 写入权，因此重绑必须在 AgentServer
        完成：分离部署 / user_id 隔离目录时，Gateway 本地写不会落到 AgentServer
        所在的会话目录。本 handler 复用 ``find_or_create_code_project_for_tui_params``
        解析/创建新目录对应的 code 项目，再 ``rebind_session_project`` 强制更新
        metadata（含 channel_metadata.project_dir/cwd，保证 /resume current-dir 过滤
        正确归位）。下一轮 chat.send 会读到新 project_dir 并据此重选 agent 实例。
        """
        from jiuwenswarm.server.runtime.session.project_store import (
            find_or_create_code_project_for_tui_params,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            rebind_session_project,
        )

        sid = (request.session_id or "").strip()
        params = request.params if isinstance(request.params, dict) else {}
        candidate_dir = str(params.get("project_dir") or params.get("cwd") or "").strip()
        if not sid:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        elif not candidate_dir:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "project_dir is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        elif not get_session_metadata(sid):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session not found", "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        else:
            try:
                project = find_or_create_code_project_for_tui_params(
                    {"project_dir": candidate_dir}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] session.rebind_project resolve project failed: %s",
                    exc,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": str(exc),
                        "code": "PROJECT_RESOLVE_FAILED",
                    },
                    metadata=request.metadata,
                )
            else:
                if project is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "error": "project_dir must be a non-empty absolute path",
                            "code": "BAD_REQUEST",
                        },
                        metadata=request.metadata,
                    )
                else:
                    updated = rebind_session_project(
                        session_id=sid,
                        project_id=project.project_id,
                        project_dir=project.project_dir,
                        work_mode=project.work_mode,
                    )
                    if not updated:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session not found", "code": "NOT_FOUND"},
                            metadata=request.metadata,
                        )
                    else:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={
                                "session_id": sid,
                                "project_id": project.project_id,
                                "project_dir": project.project_dir,
                                "project_name": project.name,
                                "work_mode": project.work_mode,
                            },
                            metadata=request.metadata,
                        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _prepare_session_switch_owner(
        self,
        *,
        channel_id: str,
        target_session_id: str,
        previous_session_id: str,
        params: dict[str, Any],
        reason: str,
    ) -> tuple[bool, str, Any, Any, Any]:
        """Resolve switch context and run product-owner prepare (team switch).

        Returns:
            ``(target_is_team, resolved_mode, context, team_manager, dispatch_signals)``.
            ``dispatch_signals`` may be ``None`` when KVC hooks are unavailable.
        """
        target_is_team = is_team_params(params)
        _, _, resolved_mode = resolve_agent_request_mode(
            params.get("mode", "agent.plan")
        )
        context = None
        dispatch_signals = None
        try:
            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                dispatch_session_switch_signals,
                resolve_session_switch_context,
            )

            context = resolve_session_switch_context(
                target_session_id=target_session_id,
                previous_session_id=previous_session_id,
                params=params,
            )
            target_is_team = context.target_is_team
            resolved_mode = context.resolved_mode
            dispatch_signals = dispatch_session_switch_signals
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] session switch KVC context unavailable; "
                "preserving product lifecycle: target_session_id=%s error=%s",
                target_session_id,
                exc,
            )

        previous_is_team = bool(context and context.previous_is_team)
        team_manager = None
        if target_is_team or previous_is_team:
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_manager = get_team_manager(channel_id)
            await team_manager.prepare_session_switch(
                target_session_id,
                previous_session_id=(
                    previous_session_id if previous_is_team else None
                ),
                reason=reason,
            )
        return target_is_team, resolved_mode, context, team_manager, dispatch_signals

    async def _dispatch_session_switch_kvc(
        self,
        *,
        channel_id: str,
        target_session_id: str,
        previous_session_id: str,
        reason: str,
        context: Any,
        team_manager: Any,
        dispatch_signals: Any,
        view_id: str = "default-view",
    ) -> None:
        """Optional KVC signals after the product owner has prepared the switch."""
        if context is None or dispatch_signals is None:
            return
        await dispatch_signals(
            context=context,
            agent_manager=self._agent_manager,
            channel_id=channel_id,
            team_manager=team_manager,
            target_session_id=target_session_id,
            previous_session_id=previous_session_id,
            reason=reason,
            view_id=view_id,
        )

    async def _handle_session_kvc_prepare(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Record typing intent; prefetch remains best-effort and asynchronous."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        intent_id = str(params.get("intent_id") or request.request_id or "").strip()
        if not session_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            try:
                from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                    record_session_prepare,
                )

                outcome = record_session_prepare(
                    session_id=session_id,
                    intent_id=intent_id,
                    channel_id=str(request.channel_id or "default"),
                    params=params,
                    agent_manager=self._agent_manager,
                )
                logger.info(
                    "[AgentWebSocketServer] session.kvc.prepare processed: "
                    "session_id=%s intent_id=%s outcome=%s",
                    session_id,
                    intent_id,
                    outcome,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "session_id": session_id,
                        "scheduled": outcome == "scheduled",
                        "outcome": outcome,
                    },
                    metadata=request.metadata,
                )
            except Exception as exc:
                logger.warning(
                    "[AgentWebSocketServer] session.kvc.prepare failed closed: "
                    "session_id=%s error=%s",
                    session_id,
                    exc,
                )
                # KVC is an optional optimization; typing must not fail.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "session_id": session_id,
                        "scheduled": False,
                        "outcome": "failed",
                    },
                    metadata=request.metadata,
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_switch(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Switch product sessions without deleting recoverable session state."""
        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or request.session_id or "").strip()
        previous_session_id = str(params.get("previous_session_id") or "").strip()

        if not target:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        channel_id = str(request.channel_id or "").strip() or "default"
        lock_key = f"{id(ws)}:{channel_id}"
        switch_lock = _session_switch_locks.get(lock_key)
        if switch_lock is None:
            switch_lock = asyncio.Lock()
            _session_switch_locks[lock_key] = switch_lock

        async with switch_lock:
            (
                _,
                resolved_mode,
                context,
                team_manager,
                dispatch_signals,
            ) = await self._prepare_session_switch_owner(
                channel_id=channel_id,
                target_session_id=target,
                previous_session_id=previous_session_id,
                params=params,
                reason="session.switch: ",
            )
            kvc_args: dict[str, Any] | None = None
            if context is not None and dispatch_signals is not None:
                kvc_args = {
                    "channel_id": channel_id,
                    "target_session_id": target,
                    "previous_session_id": previous_session_id,
                    "reason": "session.switch: ",
                    "context": context,
                    "team_manager": team_manager,
                    "dispatch_signals": dispatch_signals,
                    "view_id": str(params.get("view_id") or f"ws:{id(ws)}"),
                }
                # This now records only in-memory foreground facts and
                # dispatches any eligible action in the background.  Apply it
                # before ack so an immediate chat.send cannot observe the new
                # Session as background.
                await self._dispatch_session_switch_kvc(**kvc_args)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": target,
                    "mode": resolved_mode,
                    "switched": True,
                },
                metadata=request.metadata,
            )

            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)


    async def _find_team_session_ids(self, team_name: str) -> list[str]:
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []

        matched_session_ids: list[str] = []
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue

            session_id = session_dir.name
            metadata = get_session_metadata(session_id)
            if not self._is_team_metadata_mode(metadata):
                continue

            metadata_team_name = str(metadata.get("team_name") or "").strip()
            if metadata_team_name == team_name:
                matched_session_ids.append(session_id)

        return sorted(set(matched_session_ids))

    async def _ensure_persistent_checkpointer_response(
        self,
        request: AgentRequest,
    ) -> AgentResponse | None:
        """Return an error response when persistent checkpoint storage is unavailable."""
        try:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import ensure_persistent_checkpointer

            await ensure_persistent_checkpointer()
            await self._try_start_heartbeat_runtime()
            return None
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] persistent checkpointer unavailable: request_id=%s error=%s",
                request.request_id,
                exc,
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "persistent checkpointer is unavailable",
                    "code": "CHECKPOINT_UNAVAILABLE",
                },
                metadata=request.metadata,
            )

    async def _try_start_heartbeat_runtime(self) -> bool:
        """Best-effort idempotent recovery after checkpointer readiness."""
        if self._heartbeat_runtime.is_available:
            return True
        try:
            await self._heartbeat_runtime.start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Heartbeat runtime is not ready yet: %s",
                exc,
            )
            return False

    @staticmethod
    def _team_binding_payload(binding: Any) -> dict[str, Any]:
        if hasattr(binding, "to_dict"):
            return binding.to_dict()
        if isinstance(binding, dict):
            return dict(binding)
        return {}

    @staticmethod
    def _create_team_binding_from_template(
        *,
        team_name: str,
        template_id: str,
        config_base: dict[str, Any],
    ) -> Any:
        from jiuwenswarm.agents.harness.team import (
            get_team_template_snapshot,
            list_team_template_summaries,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
            get_team_binding_store,
            validate_team_name,
        )
        from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

        normalized_name = validate_team_name(team_name)
        template_ids = {
            str(item.get("template_id") or "")
            for item in list_team_template_summaries(config_base)
        }
        if template_id not in template_ids:
            raise TeamBindingStoreError("template_id not found", code="NOT_FOUND")

        entity_store = get_team_entity_store()
        if entity_store.exists(normalized_name):
            raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
        template_snapshot = get_team_template_snapshot(config_base, template_id=template_id)
        binding_store = get_team_binding_store()
        binding = binding_store.create(team_name=normalized_name, template_id=template_id)
        try:
            entity_store.write(
                team_name=binding.team_name,
                template_id=binding.template_id,
                template_snapshot=template_snapshot,
                created_at=binding.created_at,
            )
        except Exception:
            binding_store.delete(binding.team_name)
            raise
        return binding

    @classmethod
    async def _create_generated_team_binding(
        cls,
        *,
        description: str,
        config_base: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """Generate a unique team name and persist its binding and entity."""
        from jiuwenswarm.agents.harness.team import (
            generate_team_name,
            list_team_template_summaries,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
        )

        normalized_description = str(description or "").strip()
        if not normalized_description:
            raise TeamBindingStoreError("description is required", code="BAD_REQUEST")

        templates = list_team_template_summaries(config_base)
        if not templates:
            raise TeamBindingStoreError("no team template configured", code="NOT_FOUND")

        default_template = templates[0]
        template_id = str(default_template.get("template_id") or "").strip()
        generated_name = await generate_team_name(
            normalized_description,
            config_base=config_base,
            template_id=template_id,
        )

        for candidate_index in range(100):
            suffix = "" if candidate_index == 0 else f"_{candidate_index + 1}"
            candidate = f"{generated_name[:64 - len(suffix)]}{suffix}"
            try:
                binding = cls._create_team_binding_from_template(
                    team_name=candidate,
                    template_id=template_id,
                    config_base=config_base,
                )
                return binding, default_template
            except TeamBindingStoreError as exc:
                if exc.code != "CONFLICT":
                    raise

        raise TeamBindingStoreError(
            "unable to allocate a unique team_name",
            code="CONFLICT",
        )

    async def _ensure_auto_team_binding_for_chat(self, request: AgentRequest) -> Any | None:
        """Create and bind a team before the first team chat without consuming its query."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return None

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return None

        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            update_session_metadata,
        )

        metadata = get_session_metadata(session_id, cache_bust=True)
        raw_mode = params.get("mode")
        effective_mode = (
            raw_mode
            if isinstance(raw_mode, str) and raw_mode.strip()
            else metadata.get("mode")
        )
        _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
        if not self._is_team_metadata_mode({"mode": canonical_mode}):
            return None

        existing_team_name = str(metadata.get("team_name") or "").strip()
        if existing_team_name:
            params.setdefault("team_name", existing_team_name)
            template_id = str(metadata.get("team_template_id") or "").strip()
            if template_id:
                params.setdefault("team_template_id", template_id)
            return existing_team_name

        query = _request_query_text(request)
        if not query:
            return None

        async with self._session_team_binding_lock(session_id):
            metadata = get_session_metadata(session_id, cache_bust=True)
            existing_team_name = str(metadata.get("team_name") or "").strip()
            if existing_team_name:
                params.setdefault("team_name", existing_team_name)
                template_id = str(metadata.get("team_template_id") or "").strip()
                if template_id:
                    params.setdefault("team_template_id", template_id)
                return existing_team_name

            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
            from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

            binding, _template = await self._create_generated_team_binding(
                description=query,
                config_base=get_config(),
            )
            binding_store = get_team_binding_store()
            entity_store = get_team_entity_store()
            try:
                binding = binding_store.bind_session(
                    team_name=binding.team_name,
                    session_id=session_id,
                )
                update_session_metadata(
                    session_id=session_id,
                    channel_id=request.channel_id or None,
                    user_content=query,
                    mode=canonical_mode,
                    team_name=binding.team_name,
                    team_template_id=binding.template_id,
                    touch_last_message_at=False,
                    sync_write=True,
                )
            except Exception:
                cleanup_errors: list[str] = []
                cleanup_steps = (
                    lambda: binding_store.unbind_session(
                        team_name=binding.team_name,
                        session_id=session_id,
                    ),
                    lambda: binding_store.delete(binding.team_name),
                    lambda: entity_store.delete_team_directory(binding.team_name),
                )
                for cleanup_step in cleanup_steps:
                    try:
                        cleanup_step()
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_errors.append(str(cleanup_exc))
                if cleanup_errors:
                    logger.warning(
                        "[AgentWebSocketServer] auto team binding rollback incomplete: "
                        "session_id=%s team_name=%s errors=%s",
                        session_id,
                        binding.team_name,
                        cleanup_errors,
                    )
                raise

            params["team_name"] = binding.team_name
            params["team_template_id"] = binding.template_id
            request.metadata = dict(request.metadata or {})
            request.metadata["team_name"] = binding.team_name
            request.metadata["team_template_id"] = binding.template_id
            logger.info(
                "[AgentWebSocketServer] auto-created and bound team before chat: "
                "session_id=%s team_name=%s template_id=%s",
                session_id,
                binding.team_name,
                binding.template_id,
            )
            return binding

    @staticmethod
    def _is_team_metadata_mode(metadata: dict[str, Any]) -> bool:
        return is_team_mode(metadata.get("mode"))

    @staticmethod
    def _active_team_session_map() -> dict[str, str]:
        from jiuwenswarm.agents.harness.team import get_all_team_managers

        active: dict[str, str] = {}
        for manager in get_all_team_managers():
            snapshot_fn = getattr(manager, "get_runtime_team_snapshot", None)
            if not callable(snapshot_fn):
                continue
            for session_id, info in snapshot_fn().items():
                team_name = str(info.get("team_name") or "").strip()
                state = str(info.get("state") or "").strip()
                if team_name and state in {"active", "pending"}:
                    active.setdefault(team_name, str(session_id))
        return active

    @staticmethod
    def _legacy_team_bindings_from_sessions(known_team_names: set[str]) -> list[dict[str, Any]]:
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        sessions_dir = get_agent_sessions_dir()
        if not sessions_dir.exists():
            return []

        legacy: dict[str, dict[str, Any]] = {}
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            metadata = get_session_metadata(session_dir.name, cache_bust=True)
            if not metadata or not AgentWebSocketServer._is_team_metadata_mode(metadata):
                continue
            team_name = str(metadata.get("team_name") or "").strip()
            if not team_name or team_name in known_team_names:
                continue
            item = legacy.setdefault(
                team_name,
                {
                    "team_name": team_name,
                    "template_id": str(metadata.get("team_template_id") or team_name).strip() or team_name,
                    "created_at": float(metadata.get("created_at") or session_dir.stat().st_ctime),
                    "updated_at": float(metadata.get("last_message_at") or session_dir.stat().st_mtime),
                    "session_ids": [],
                    "last_session_id": "",
                    "legacy": True,
                },
            )
            if session_dir.name not in item["session_ids"]:
                item["session_ids"].append(session_dir.name)
            if float(metadata.get("last_message_at") or 0) >= float(item.get("updated_at") or 0):
                item["updated_at"] = float(metadata.get("last_message_at") or session_dir.stat().st_mtime)
                item["last_session_id"] = session_dir.name
        return list(legacy.values())

    async def _handle_team_templates_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import list_team_template_summaries

        templates = list_team_template_summaries(get_config())
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"templates": templates},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_bindings_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import list_team_template_summaries
        from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity_for_binding, get_team_entity_store
        from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

        active_by_team = self._active_team_session_map()
        config_base = get_config()
        templates = {
            str(item.get("template_id") or ""): item
            for item in list_team_template_summaries(config_base)
        }
        store = get_team_binding_store()
        bindings = store.list()
        bindings_by_name = {binding.team_name: binding for binding in bindings}
        entity_store = get_team_entity_store()
        teams = [self._team_binding_payload(binding) for binding in bindings]
        known_team_names = {str(item.get("team_name") or "") for item in teams}
        teams.extend(self._legacy_team_bindings_from_sessions(known_team_names))

        enriched: list[dict[str, Any]] = []
        for item in teams:
            team_name = str(item.get("team_name") or "").strip()
            template_id = str(item.get("template_id") or "").strip()
            active_session_id = active_by_team.get(team_name, "")
            legacy = bool(item.get("legacy", False))
            entity = None
            entity_path = ""
            if team_name and not legacy:
                binding = bindings_by_name.get(team_name)
                if binding is not None:
                    entity = ensure_team_entity_for_binding(binding, config_base=config_base, store=entity_store)
                else:
                    entity = entity_store.get(team_name)
                if entity is not None:
                    item["template_id"] = entity.template_id
                    template_id = entity.template_id
                    entity_path = str(entity_store.entity_path(team_name))
            source_template_available = bool(template_id and template_id in templates)
            team_config_available = entity is not None
            template_available = bool(team_config_available or source_template_available)
            selectable = bool(team_name and not active_session_id and not legacy and team_config_available)
            disabled_reason = ""
            if active_session_id:
                disabled_reason = "active"
            elif legacy:
                disabled_reason = "legacy"
            elif not team_config_available:
                disabled_reason = "team_config_missing"
            item.update(
                {
                    "template_available": template_available,
                    "source_template_available": source_template_available,
                    "team_config_available": team_config_available,
                    "team_config_path": entity_path,
                    "session_count": len(item.get("session_ids") or []),
                    "active_session_id": active_session_id,
                    "selectable": selectable,
                    "disabled_reason": disabled_reason,
                }
            )
            enriched.append(item)

        enriched.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"teams": enriched},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_binding_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        team_name = str(params.get("team_name") or "")
        template_id = str(params.get("template_id") or "").strip()
        config_base = get_config()
        try:
            binding = self._create_team_binding_from_template(
                team_name=team_name,
                template_id=template_id,
                config_base=config_base,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"team": binding.to_dict()},
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_binding_generate(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import (
            TeamNameGenerationError,
        )
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError
        from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

        params = request.params if isinstance(request.params, dict) else {}
        description = str(params.get("description") or params.get("prompt") or "").strip()
        config_base = get_config()
        try:
            binding, default_template = await self._create_generated_team_binding(
                description=description,
                config_base=config_base,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "team": binding.to_dict(),
                    "template": default_template,
                },
                metadata=request.metadata,
            )
        except TeamNameGenerationError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "GENERATION_FAILED"},
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_session_bind(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata
        from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStoreError, get_team_binding_store
        from jiuwenswarm.server.runtime.team_entity_store import (
            TeamEntityStoreError,
            ensure_team_entity_for_binding,
            get_team_entity_store,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        team_name = str(params.get("team_name") or "").strip()
        _, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "team"))
        try:
            if not session_id:
                raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")
            if not (get_agent_sessions_dir() / session_id).is_dir():
                raise TeamBindingStoreError("session not found", code="NOT_FOUND")
            binding_store = get_team_binding_store()
            existing_binding = binding_store.get(team_name)
            if existing_binding is None:
                raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")
            entity = ensure_team_entity_for_binding(existing_binding, config_base=get_config())
            if entity is None:
                raise TeamBindingStoreError("team entity config missing", code="NOT_FOUND")
            binding = binding_store.bind_session(
                team_name=team_name,
                session_id=session_id,
            )
            update_session_metadata(
                session_id=session_id,
                channel_id=str(request.channel_id or "").strip() or None,
                mode=canonical_mode,
                team_name=binding.team_name,
                team_template_id=binding.template_id,
                sync=True,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "team_name": binding.team_name,
                    "team_template_id": binding.template_id,
                    "mode": canonical_mode,
                    "team": binding.to_dict(),
                    "team_config_path": str(get_team_entity_store().entity_path(binding.team_name)),
                },
                metadata=request.metadata,
            )
        except (TeamBindingStoreError, TeamEntityStoreError) as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": getattr(exc, "code", "BAD_REQUEST")},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Delete a team and all team sessions that persist that team."""
        from openjiuwen.core.runner import Runner
        from jiuwenswarm.agents.harness.team import (
            stop_team_session_runtime_across_managers,
        )
        from jiuwenswarm.server.runtime.team_binding_store import (
            TeamBindingStoreError,
            get_team_binding_store,
        )
        from jiuwenswarm.server.runtime.team_entity_store import (
            TeamEntityStoreError,
            get_team_entity_store,
        )

        def delete_team_directory_best_effort(team_name: str) -> None:
            try:
                entity_store.delete_team_directory(team_name)
            except TeamEntityStoreError as exc:
                logger.warning(
                    "[AgentWebSocketServer] failed to delete local team directory; "
                    "continuing team delete: team_name=%s code=%s error=%s",
                    team_name,
                    getattr(exc, "code", "DELETE_FAILED"),
                    exc,
                )

        params = request.params if isinstance(request.params, dict) else {}
        is_team = is_team_params(params)
        team_name = str(params.get("team_name") or "").strip()

        if not team_name:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "team_name is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        elif not is_team:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "team.delete is only supported for team mode",
                    "code": "UNSUPPORTED_MODE",
                },
                metadata=request.metadata,
            )
        else:
            try:
                binding_store = get_team_binding_store()
                entity_store = get_team_entity_store()
                team_session_ids = await self._find_team_session_ids(team_name)
                if not team_session_ids:
                    binding = binding_store.get(team_name)
                    if binding is None and not entity_store.exists(team_name):
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "team not found", "code": "NOT_FOUND"},
                            metadata=request.metadata,
                        )
                    else:
                        delete_team_directory_best_effort(team_name)
                        binding_store.delete(team_name)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={
                                "team_name": team_name,
                                "session_ids": [],
                                "deleted": True,
                            },
                            metadata=request.metadata,
                        )
                else:
                    checkpoint_resp = await self._ensure_persistent_checkpointer_response(request)
                    if checkpoint_resp is not None:
                        resp = checkpoint_resp
                    else:
                        from jiuwenswarm.agents.harness.team import (
                            kv_cache_hooks as team_kv_cache_hooks,
                        )

                        for team_session_id in team_session_ids:
                            await team_kv_cache_hooks.stop_runtime_before_terminal_delete(
                                stop_team_session_runtime_across_managers,
                                session_id=team_session_id,
                                reason="team.delete: ",
                            )

                        runtime_deleted = await Runner.delete_agent_team(
                            team_name=team_name,
                            session_ids=team_session_ids,
                            force=True,
                        )
                        if not runtime_deleted:
                            resp = AgentResponse(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                ok=False,
                                payload={
                                    "error": "agent team runtime cleanup failed",
                                    "code": "DELETE_FAILED",
                                    "team_name": team_name,
                                    "deleted": False,
                                },
                                metadata=request.metadata,
                            )
                        else:
                            failed_session_ids: list[str] = []
                            for team_session_id in team_session_ids:
                                session_dir = get_agent_sessions_dir() / team_session_id
                                if session_dir.exists():
                                    try:
                                        shutil.rmtree(session_dir)
                                    except Exception as exc:
                                        logger.warning(
                                            "[AgentWebSocketServer] failed to delete local team session dir: "
                                            "session_id=%s error=%s",
                                            team_session_id,
                                            exc,
                                        )
                                        failed_session_ids.append(team_session_id)
                                        continue
                                remove_session_metadata_cache(team_session_id)

                            if failed_session_ids:
                                resp = AgentResponse(
                                    request_id=request.request_id,
                                    channel_id=request.channel_id,
                                    ok=False,
                                    payload={
                                        "error": "failed to delete local team session directories",
                                        "code": "DELETE_FAILED",
                                        "team_name": team_name,
                                        "failed_session_ids": failed_session_ids,
                                        "deleted": False,
                                    },
                                    metadata=request.metadata,
                                )
                            else:
                                # agent-core normally removes team_home; retry here because it logs and
                                # suppresses filesystem cleanup failures.
                                delete_team_directory_best_effort(team_name)
                                binding_store.delete(team_name)
                                resp = AgentResponse(
                                    request_id=request.request_id,
                                    channel_id=request.channel_id,
                                    ok=True,
                                    payload={
                                        "team_name": team_name,
                                        "session_ids": team_session_ids,
                                        "deleted": True,
                                    },
                                    metadata=request.metadata,
                                )
            except (TeamBindingStoreError, TeamEntityStoreError) as exc:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(exc), "code": getattr(exc, "code", "DELETE_FAILED")},
                    metadata=request.metadata,
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Delete a single session and its recoverable runtime state."""
        from openjiuwen.core.runner import Runner
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        from jiuwenswarm.agents.harness.team import get_team_manager

        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or "").strip()
        if not target:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir

            session_dir, invalid_reason = resolve_session_dir(
                target, sessions_root=get_agent_sessions_dir()
            )
            if session_dir is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": invalid_reason or "invalid session_id", "code": "BAD_REQUEST"},
                    metadata=request.metadata,
                )
            elif not session_dir.exists():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session not found", "code": "NOT_FOUND"},
                    metadata=request.metadata,
                )
            elif not session_dir.is_dir():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session is not a directory", "code": "BAD_REQUEST"},
                    metadata=request.metadata,
                )
            else:
                checkpoint_resp = await self._ensure_persistent_checkpointer_response(request)
                if checkpoint_resp is not None:
                    resp = checkpoint_resp
                else:
                    metadata = get_session_metadata(target)
                    is_team_session = self._is_team_metadata_mode(metadata)
                    team_name = str(metadata.get("team_name") or "").strip()
                    channel_id = str(metadata.get("channel_id") or request.channel_id or "").strip() or None
                    heartbeat_delete_prepared = False
                    try:
                        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                            mark_session_deleted,
                        )

                        mark_session_deleted(
                            session_id=target,
                            channel_id=channel_id or "default",
                            is_team=is_team_session,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[AgentWebSocketServer] KVC delete tombstone failed; "
                            "preserving product delete: session_id=%s error=%s",
                            target,
                            exc,
                        )
                    try:
                        await self._heartbeat_runtime.begin_session_delete(target)
                        heartbeat_delete_prepared = True
                        if is_team_session:
                            team_manager = get_team_manager(channel_id)
                            deleted = await team_manager.delete_session_runtime(
                                target,
                                reason="session.delete: ",
                            )
                        else:
                            agent = self._agent_manager.get_agent_nowait(channel_id=channel_id)
                            if agent is not None:
                                adapter = self._resolve_adapter(agent)
                                release_runtime = getattr(
                                    adapter,
                                    "release_subagent_runtime_for_session",
                                    None,
                                )
                                if callable(release_runtime):
                                    await release_runtime(
                                        target,
                                        reason="session_deleted",
                                    )
                            await self._agent_manager.cleanup_session_runtime(
                                channel_id=channel_id or "",
                                session_id=target,
                            )

                            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                                evict_plan_session,
                            )

                            await evict_plan_session(
                                session_id=target,
                                agent_manager=self._agent_manager,
                                channel_id=channel_id,
                            )
                            await Runner.release(target)
                            deleted = True
                        if deleted:
                            shutil.rmtree(session_dir)
                    except Exception as exc:
                        logger.warning(
                            "[AgentWebSocketServer] session.delete runtime cleanup failed: session_id=%s error=%s",
                            target,
                            exc,
                        )
                        deleted = False

                    if not deleted:
                        if heartbeat_delete_prepared:
                            try:
                                await self._heartbeat_runtime.abort_session_delete(
                                    target,
                                    channel_id=channel_id or "",
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "[AgentWebSocketServer] heartbeat delete rollback failed: "
                                    "session_id=%s error=%s",
                                    target,
                                    exc,
                                )
                        try:
                            from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks import (
                                restore_session_after_failed_delete,
                            )

                            restore_session_after_failed_delete(target)
                        except Exception as exc:
                            logger.warning(
                                "[AgentWebSocketServer] KVC failed-delete rollback failed; "
                                "preserving delete response: session_id=%s error=%s",
                                target,
                                exc,
                            )
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session runtime cleanup failed", "code": "DELETE_FAILED"},
                            metadata=request.metadata,
                        )
                    else:
                        try:
                            await self._heartbeat_runtime.commit_session_delete(target)
                        except Exception as exc:  # noqa: BLE001
                            # The product Session is already gone. Resume the
                            # scheduler so its missing-session path retries the
                            # idempotent policy transition instead of leaving a
                            # permanently suspended job.
                            logger.warning(
                                "[AgentWebSocketServer] heartbeat delete commit failed; "
                                "session_id=%s error=%s",
                                target,
                                exc,
                            )
                        _plan_exited_sessions.discard(target)
                        _plan_active_sessions.discard(target)
                        remove_session_metadata_cache(target)
                        if is_team_session:
                            try:
                                from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

                                get_team_binding_store().unbind_session(
                                    team_name=team_name or None,
                                    session_id=target,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "[AgentWebSocketServer] failed to unbind deleted team session: "
                                    "session_id=%s team_name=%s error=%s",
                                    target,
                                    team_name,
                                    exc,
                                )
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={"session_id": target},
                            metadata=request.metadata,
                        )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _resolve_rewind_agent(
        self,
        channel_id: str,
        session_id: str | None = None,
    ) -> tuple[Any, Any] | None:
        """Return (deep_agent, react_agent) for rewind context rebuild.

        Prefer the live **session-scoped** DeepAgent used by chat.send.
        Root ``agent.get_instance()`` is a separate DeepAgent whose
        context_engine / ``_interaction_session`` are not the ones the next
        user turn will read — updating them leaves the model still seeing
        rewound turns.
        """
        sid = str(session_id or "").strip()
        agent = (
            self._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id or "default",
                session_id=sid,
            )
            if sid
            else None
        )
        if agent is None:
            agent = self._agent_manager.get_agent_nowait(
                channel_id=channel_id or "default"
            )
        if agent is None:
            return None

        deep_agent = None
        if sid:
            adapter = self._resolve_adapter(agent)
            if adapter is not None:
                # Already session-scoped (rare): use it directly.
                if getattr(adapter, "_is_session_scoped_adapter", False):
                    deep_agent = getattr(adapter, "_instance", None)
                else:
                    get_cached = getattr(adapter, "_get_cached_session_adapter", None)
                    if callable(get_cached):
                        session_adapter = get_cached(sid)
                        if session_adapter is not None:
                            deep_agent = getattr(session_adapter, "_instance", None)
                            if deep_agent is None:
                                logger.warning(
                                    "[AgentWS] rewind: cached session adapter has no "
                                    "instance for session_id=%s",
                                    sid,
                                )

        if deep_agent is None:
            # Fallback: no live session adapter yet (e.g. rewind before any chat
            # on this process). Checkpointer-only rebuild still helps cold start,
            # so build the root DeepAgent here if it has not been needed yet.
            deep_agent = await agent.ensure_instance()
            if deep_agent is not None and sid:
                logger.info(
                    "[AgentWS] rewind: no session-scoped DeepAgent for %s; "
                    "falling back to root instance",
                    sid,
                )

        if deep_agent is None:
            return None
        react_agent = deep_agent.react_agent
        if react_agent is None:
            return None
        return (deep_agent, react_agent)

    @staticmethod
    def _send_error_response(ws: Any, request: AgentRequest,
                              send_lock: asyncio.Lock, error: str,
                              code: str | None = None) -> dict[str, Any]:
        """Build an error AgentResponse wire payload."""
        payload: dict[str, Any] = {"error": error}
        if code:
            payload["code"] = code
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
            metadata=request.metadata,
        )
        return encode_agent_response_for_wire(
            resp,
            response_id=request.request_id,
        )

    async def _handle_session_rewind_full(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock,
        restore_files: bool = False,
        compact: bool = False,
    ) -> None:
        """Full rewind: truncate history.json + context_engine + update checkpointer."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")
        compact_summary = params.get("compact_summary") if compact else None
        direction = str(params.get("direction") or "from").strip() if compact else "from"
        summarized_count = int(params.get("summarized_count", 0) or 0) if compact else 0

        if not target_sid or turn_index is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "session_id and turn_index required", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "turn_index must be integer", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            # Step 1: Optionally restore files first
            restore_result: dict[str, Any] = {}
            if restore_files:
                from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files
                restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)

            # Step 2: Truncate history.json (local file operation)
            # "up_to" direction: keep messages from turn_index onward, summarize the prefix.
            # compact_partial_session handles this correctly (rewind_session only supports
            # the "from" direction — keeping the prefix and truncating the tail).
            if compact and direction == "up_to":
                from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session
                rewind_result = compact_partial_session(
                    session_id=target_sid,
                    turn_index=turn_index,
                    direction="up_to",
                    llm_summary=compact_summary,
                )
            else:
                rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)

            # Step 3: Truncate context_engine in-place + persist to checkpointer.
            # rewind_session_context reads the already-truncated history.json and
            # converts ALL records to context messages, so it naturally produces the
            # correct result for both "from" and "up_to" directions.
            context_ok = False
            pair = await self._resolve_rewind_agent(
                request.channel_id or "default",
                session_id=target_sid,
            )
            if pair is None:
                logger.warning(
                    "[AgentWS] session.rewind: no agent for context rebuild "
                    "(session_id=%s channel=%s); history truncated but model "
                    "context may still contain rewound turns",
                    target_sid,
                    request.channel_id,
                )
            else:
                deep_agent, _react_agent = pair
                try:
                    context_ok = await rewind_session_context(
                        deep_agent=deep_agent,
                        session_id=target_sid,
                        turn_index=turn_index,
                    )
                except Exception as exc:
                    logger.warning(
                        "[AgentWS] session.rewind context truncation failed: %s", exc,
                    )
                if not context_ok:
                    logger.warning(
                        "[AgentWS] session.rewind: history truncated but "
                        "rewind_context=false (session_id=%s)",
                        target_sid,
                    )

            payload = {**rewind_result, "rewind_context": context_ok}
            if restore_files:
                payload["restored_files"] = restore_result.get("restored_files", [])
                payload["deleted_files"] = restore_result.get("deleted_files", [])
                payload["restore_errors"] = restore_result.get("errors", [])

            # Step 4: For compact mode, append boundary + rewind_summary + compact_summary records.
            # compact_partial_session already writes these for "up_to", so only append for "from".
            if compact and direction == "from":
                import uuid as _uuid
                import time as _time
                from jiuwenswarm.server.runtime.session.session_history import append_history_record
                request_id = str(_uuid.uuid4())
                now = _time.time()

                short_text = (
                    f"Summarized {summarized_count} messages from this point."
                    if direction == "from"
                    else f"Summarized {summarized_count} messages up to this point."
                )

                append_history_record(
                    session_id=target_sid,
                    request_id=request_id,
                    channel_id=request.channel_id or "tui",
                    role="assistant",
                    event_type="context.compact_boundary",
                    content="Conversation compacted",
                    timestamp=now,
                    extra={
                        "compact_metadata": {
                            "trigger": "manual_rewind",
                            "direction": direction,
                            "turn_index": turn_index,
                            "summarized_messages": summarized_count,
                        },
                    },
                )

                append_history_record(
                    session_id=target_sid,
                    request_id=request_id,
                    channel_id=request.channel_id or "tui",
                    role="assistant",
                    event_type="context.rewind_summary",
                    content=short_text,
                    timestamp=now + 0.001,
                    extra={
                        "compact_metadata": {
                            "trigger": "manual_rewind",
                            "direction": direction,
                            "turn_index": turn_index,
                            "summarized_messages": summarized_count,
                        },
                        "is_compact_summary": True,
                    },
                )

                if isinstance(compact_summary, str) and compact_summary.strip():
                    append_history_record(
                        session_id=target_sid,
                        request_id=request_id,
                        channel_id=request.channel_id or "tui",
                        role="assistant",
                        event_type="context.compact_summary",
                        content=compact_summary.strip(),
                        timestamp=now + 0.002,
                        extra={
                            "compact_metadata": {
                                "trigger": "manual_rewind",
                                "direction": direction,
                                "turn_index": turn_index,
                                "summarized_messages": summarized_count,
                            },
                            "is_compact_summary": True,
                            "transcript_only": True,
                        },
                    )

                payload["summarized_messages"] = summarized_count

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
                metadata=request.metadata,
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.exception("[AgentWS] session.rewind failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_session_rewind_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Truncate history.json + in-memory context_engine for a session."""
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")

        if not target_sid or turn_index is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "session_id and turn_index required", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock,
                "turn_index must be integer", "BAD_REQUEST",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        pair = await self._resolve_rewind_agent(
            request.channel_id or "default",
            session_id=target_sid,
        )
        if pair is None:
            wire = AgentWebSocketServer._send_error_response(
                ws, request, send_lock, "no agent instance available",
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return
        deep_agent, _react_agent = pair

        try:
            # Truncate history.json first so rewind_session_context reads the
            # correct truncated state (the new implementation rebuilds context
            # from history.json on disk).
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
            context_ok = await rewind_session_context(
                deep_agent=deep_agent,
                session_id=target_sid,
                turn_index=turn_index,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={**rewind_result, "rewind_context": context_ok},
                metadata=request.metadata,
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.exception("[AgentWS] session.rewind_context failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_permissions_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import \
            dispatch_permissions_config_request

        resp = dispatch_permissions_config_request(request)

        # After any successful mutation (delete / update / set / create),
        # reload agent config so the PermissionInterruptRail picks up the
        # change immediately instead of waiting for the next tool call's
        # get_permissions_snapshot refresh.
        read_only_methods = {
            ReqMethod.PERMISSIONS_TOOLS_GET,
            ReqMethod.PERMISSIONS_RULES_GET,
            ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
        }
        if resp.ok and request.req_method not in read_only_methods:
            # 后台异步重载: 不阻塞权限 RPC 回包(避免 reload 慢导致 AgentServer
            # request timed out)。reload_agents_config 内部有 _reload_lock 串行化
            # + fingerprint 去重,fire-and-forget 安全。
            reload_task = asyncio.create_task(
                self._agent_manager.reload_agents_config(get_config(), None)
            )
            _background_permission_reload_tasks.add(reload_task)
            reload_task.add_done_callback(_background_permission_reload_tasks.discard)
            reload_task.add_done_callback(_log_permission_reload_failure)

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        subagent_id = params.get("subagent_id")
        data = self.get_conversation_history(
            session_id=session_id,
            page_idx=page_idx,
            subagent_id=subagent_id if isinstance(subagent_id, str) else None,
        )
        if data is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "invalid page_idx or session history not found"},
            )
        else:
            # 非流式整页塞进单个 wire 帧（AgentResponse.payload=data），没法像流式那样
            # 按 channel_id 分片流——这里所有通道统一走 _sanitize_history_record_for_wire
            # 把每条 record 裁剪到 16KB + 64KB collapse 之内，保证 wire 帧有界。
            # 流式路径在 _handle_history_get_stream 里按 channel_id 分流（web 走 split，
            # 其他走 sanitize），与此处无关。
            if isinstance(data.get("messages"), list):
                data["messages"] = [
                    _sanitize_history_record_for_wire(record)
                    for record in data["messages"]
                    if isinstance(record, dict)
                ]
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=data,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)


    async def _handle_proactive_tick(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle proactive.tick request from CronScheduler.

        This is called by Gateway's CronScheduler to trigger a recommendation tick.
        Respects cooldown and daily limits.
        """
        if self._proactive_engine is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "ProactiveEngine not initialized"},
            )
        else:
            try:
                # Extract target_channel from params
                params = request.params or {}
                target_channel = params.get("target_channel")

                # Run the tick (respects cooldown and daily limits)
                success = await self._proactive_engine.tick_now(target_channel=target_channel)

                status = "tick_executed" if success else "no_recommendation"
                last_tick = self._proactive_engine.last_tick_at
                if last_tick > 0:
                    status = f"{status} (last_tick_at={last_tick:.0f})"

                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": status, "success": success},
                )
            except Exception as e:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(e)},
                )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_snapshot(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.agents.harness.team import get_team_manager
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        channel_id = request.channel_id or "web"
        empty_payload = {"members": [], "tasks": [], "team_id": None}

        team_manager = get_team_manager(channel_id)
        monitor_handler = team_manager.get_monitor_handler(session_id) if session_id else None

        snapshot: dict[str, Any] | None = None
        source = "empty"
        if monitor_handler is not None and monitor_handler.is_running:
            try:
                snapshot = await monitor_handler.get_team_snapshot()
                if snapshot is not None:
                    source = "live"
            except Exception as e:
                logger.warning("[AgentWebSocketServer] team.snapshot (live) failed: %s", e)

        def _snapshot_tasks(payload: dict[str, Any] | None) -> list[Any]:
            if not isinstance(payload, dict):
                return []
            tasks = payload.get("tasks")
            return tasks if isinstance(tasks, list) else []

        # History restore often hits this RPC after the monitor has stopped, OR
        # while a live handler is still registered but already returns a truthy
        # empty board ({tasks: [], members: [], team_id: ...}). `if not snapshot`
        # alone would skip DB in that case and leave the frontend with no
        # title/content. Fall back whenever live has no tasks.
        needs_db = snapshot is None or not _snapshot_tasks(snapshot)
        if needs_db and session_id:
            team_name = str(params.get("team_name") or "").strip()
            if not team_name:
                team_name = str(
                    team_manager.get_active_team_name(session_id) or ""
                ).strip()
            if not team_name:
                team_name = str(
                    (get_session_metadata(session_id) or {}).get("team_name") or ""
                ).strip()
            if team_name:
                try:
                    db_snapshot = await TeamMonitorHandler.get_team_snapshot_from_db(
                        session_id, team_name
                    )
                except Exception as e:
                    logger.warning(
                        "[AgentWebSocketServer] team.snapshot (db) failed: "
                        "session_id=%s team_name=%s error=%s",
                        session_id,
                        team_name,
                        e,
                    )
                    db_snapshot = None
                # Prefer DB when it has tasks, or when live was missing entirely.
                # If both boards have empty tasks, keep live so in-memory
                # members (if any) are not wiped by an empty DB read.
                if db_snapshot is not None and (
                    snapshot is None or _snapshot_tasks(db_snapshot)
                ):
                    snapshot = db_snapshot
                    source = "db"

        payload = snapshot or empty_payload
        members = payload.get("members") if isinstance(payload, dict) else []
        tasks = _snapshot_tasks(payload if isinstance(payload, dict) else None)
        logger.info(
            "[AgentWebSocketServer] team.snapshot session_id=%s source=%s "
            "tasks_count=%s members_count=%s",
            session_id or "-",
            source if snapshot is not None else "empty",
            len(tasks),
            len(members) if isinstance(members, list) else 0,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=payload,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_mq_publish(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Relay one external team event into the active core team runtime."""
        from jiuwenswarm.agents.harness.team import get_team_manager

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        payload = request.params.get("payload")

        if not session_id:
            success, reason = False, "session_id is required"
        elif payload is None:
            success, reason = False, "payload is required"
        elif not isinstance(payload, dict) or payload.get("type") != "team.external_event":
            success, reason = False, "invalid_external_event"
        else:
            success, reason = await get_team_manager(channel_id).interact(session_id, payload)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=success,
            payload={"published": True} if success else {"error": reason},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_members_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """返回 team human_agent 席位列表供 /join 校验。

        纯查询透传：mismatch 校验与对外文案均在 gateway，server 只查 member、过滤
        human_agent、回 ok/members。查不到或异常 → ok=False（payload 不带文案，由
        gateway 拼"team 不存在"）。
        """
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
            query_team_human_members_for_join,
        )

        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id") or request.session_id or ""
        team_name = str(params.get("team_name") or "").strip()
        channel_id = request.channel_id or "web"

        try:
            members_raw = await query_team_human_members_for_join(session_id, team_name)
        except Exception:
            logger.exception(
                "[AgentWebSocketServer] team.members.get failed: session=%s team=%s",
                session_id, team_name,
            )
            members_raw = []
        members = [
            m for m in members_raw
            if isinstance(m, dict) and m.get("role") == "human_agent" and m.get("member_id")
        ]
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=bool(members),
            payload={"members": members},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_workflows(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """Handle command.workflows RPC — list summaries or get one workflow detail."""
        from jiuwenswarm.agents.harness.team import get_team_manager

        session_id = request.session_id or ""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "list").strip().lower()
        workflow_id = params.get("workflow_id") or params.get("workflow_run_id")
        wf_id_log = workflow_id.strip() if isinstance(workflow_id, str) else workflow_id

        logger.info(
            "[WF_DBG] command.workflows req channel_id=%s session_id=%s request_id=%s action=%s workflow_id=%s",
            channel_id,
            session_id,
            request.request_id,
            action,
            wf_id_log,
        )

        team_manager = get_team_manager(channel_id)
        workflow_handler = team_manager.get_workflow_handler(session_id)
        source = "live" if workflow_handler is not None else "checkpoint"
        detail_raw_bytes: int | None = None

        if workflow_handler is None:
            # No live handler (runtime not active / torn down by cancel-stop).
            # The snapshot is a read-only pull and must not depend on runtime
            # liveness — fall back to the persisted checkpoint so historical /
            # terminal workflow runs remain queryable after the team session
            # is cancelled or stopped.
            try:
                from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                    restore_workflow_runs,
                )

                restored = restore_workflow_runs(session_id)
                workflows = (
                    [run.to_workflow_run_dict() for run in restored.values()]
                    if restored
                    else []
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[WF_DBG] command.workflows checkpoint_restore_failed session_id=%s error=%s",
                    session_id,
                    exc,
                )
                workflows = []
        else:
            try:
                workflows = workflow_handler.get_workflow_snapshot()
            except Exception as e:
                logger.warning(
                    "[WF_DBG] command.workflows snapshot_failed session_id=%s error=%s",
                    session_id,
                    e,
                )
                workflows = []

        source_count = len(workflows)
        source_bytes = sum(_json_wire_size(item) for item in workflows if isinstance(item, dict))

        if action == "get":
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "workflow_id is required for action=get"},
                )
            else:
                target_id = workflow_id.strip()
                match = next(
                    (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                    None,
                )
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    detail_raw_bytes = _json_wire_size(match)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=True,
                        payload=_build_workflow_detail_payload(match, session_id=session_id),
                    )
        elif action == "get_human_prompt":
            agent_id = params.get("agent_id")
            correlation_id = params.get("correlation_id")
            agent_id_str = agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
            corr_id_str = (
                correlation_id.strip()
                if isinstance(correlation_id, str) and correlation_id.strip()
                else None
            )
            if not isinstance(workflow_id, str) or not workflow_id.strip():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "workflow_id is required for action=get_human_prompt"},
                )
            elif not agent_id_str and not corr_id_str:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": "agent_id or correlation_id is required for action=get_human_prompt"},
                )
            else:
                target_id = workflow_id.strip()
                match = next(
                    (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                    None,
                )
                if match is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok=False,
                        payload={"error": f"workflow not found: {target_id}"},
                    )
                else:
                    prompt_payload = _build_workflow_human_prompt_payload(
                        match,
                        session_id=session_id,
                        agent_id=agent_id_str,
                        correlation_id=corr_id_str,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        ok="error" not in prompt_payload,
                        payload=prompt_payload,
                    )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=True,
                payload=_build_workflow_list_payload(workflows, session_id=session_id),
            )

        payload = resp.payload if isinstance(resp.payload, dict) else {}
        payload_bytes = _json_wire_size(payload)
        truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
        included = len(payload.get("workflows", [])) if payload.get("action") == "list" else None
        error = payload.get("error") if isinstance(payload, dict) and not resp.ok else None
        log_level = logging.WARNING if (not resp.ok or truncated) else logging.INFO
        if action == "list":
            logger.log(
                log_level,
                "[WF_DBG] command.workflows res ok=%s action=list source=%s count=%d source_bytes=%d "
                "payload_bytes=%d included=%d/%d truncated=%s error=%s",
                resp.ok,
                source,
                source_count,
                source_bytes,
                payload_bytes,
                included or 0,
                source_count,
                truncated,
                error,
            )
        else:
            prompt_len = None
            if action == "get_human_prompt" and isinstance(payload, dict):
                human_prompt = payload.get("human_prompt")
                if isinstance(human_prompt, str):
                    prompt_len = len(human_prompt.encode("utf-8"))
            logger.log(
                log_level,
                "[WF_DBG] command.workflows res ok=%s action=%s source=%s workflow_id=%s "
                "raw_bytes=%s payload_bytes=%d truncated=%s prompt_len=%s error=%s",
                resp.ok,
                action,
                source,
                wf_id_log,
                detail_raw_bytes,
                payload_bytes,
                truncated,
                prompt_len,
                error,
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_team_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """返回 team 模式历史记录的分页，避免与 history.get 并发竞争。

        支持可选 member_name 参数：传入时仅返回与该 member 相关的记录
        （p2p 消息 / @all 广播 / teammate 输出）。
        """
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        member_name = params.get("member_name")
        channel_id = request.channel_id or "web"

        if not isinstance(session_id, str) or not session_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "session_id is required"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        session_id = session_id.strip()
        try:
            if member_name and isinstance(member_name, str) and member_name.strip():
                records = await asyncio.to_thread(
                    read_member_history_records, session_id, str(member_name).strip()
                )
            else:
                records = await asyncio.to_thread(read_team_history_records, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[team.history.get] read failed: session_id=%s error=%s", session_id, exc)
            records = []

        sanitized_records = [
            _sanitize_history_record_for_wire(record)
            for record in records
            if isinstance(record, dict)
        ]
        total = len(sanitized_records)
        cursor = _coerce_int(
            params.get("cursor", params.get("offset", 0)),
            default=0,
            minimum=0,
            maximum=max(0, total),
        )
        limit = _coerce_int(
            params.get("limit"),
            default=_TEAM_HISTORY_DEFAULT_LIMIT,
            minimum=1,
            maximum=_TEAM_HISTORY_MAX_LIMIT,
        )
        max_bytes = _coerce_int(
            params.get("max_bytes"),
            default=_TEAM_HISTORY_DEFAULT_MAX_BYTES,
            minimum=_TEAM_HISTORY_MIN_MAX_BYTES,
            maximum=_TEAM_HISTORY_MAX_MAX_BYTES,
        )
        page_records, next_cursor = _select_history_record_page(
            sanitized_records,
            cursor=cursor,
            limit=limit,
            max_bytes=max_bytes,
            session_id=session_id,
        )
        logger.debug(
            "[team.history.get] session_id=%s member=%s total=%d cursor=%d returned=%d next_cursor=%d max_bytes=%d",
            session_id, str(member_name or ""), total, cursor,
            len(page_records), next_cursor, max_bytes,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={
                "records": page_records,
                "session_id": session_id,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor < total,
                "total": total,
            },
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_history_get_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        subagent_id = params.get("subagent_id")
        data = self.get_conversation_history(
            session_id=session_id,
            page_idx=page_idx,
            subagent_id=subagent_id if isinstance(subagent_id, str) else None,
        )
        if data is None:
            err_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "invalid page_idx or session history not found",
                },
                is_complete=True,
            )
            wire = encode_agent_chunk_for_wire(
                err_chunk,
                response_id=request.request_id,
                sequence=0,
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        messages = data.get("messages", [])
        total_pages = data.get("total_pages")
        page = data.get("page_idx")
        response_subagent_id = data.get("subagent_id")
        sequence = 0
        # 仅 web 通道走分片流（前端 HistoryRecordReassembler 重组）。
        # 其他通道（tui/acp/...）不认 _part 字段，走旧 _sanitize_history_record_for_wire
        # 单帧 + collapse 路径，维持现状，零回归。
        use_split = request.channel_id == "web"
        if isinstance(messages, list):
            for item in messages:
                if use_split:
                    chunks_for_record = split_history_record_for_stream(item)
                else:
                    chunks_for_record = [_sanitize_history_record_for_wire(item)]
                for chunk_record in chunks_for_record:
                    chunk = AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "history.message",
                            "message": chunk_record,
                            "session_id": str(session_id or ""),
                            "subagent_id": str(response_subagent_id or subagent_id or ""),
                            "total_pages": total_pages,
                            "page_idx": page,
                        },
                        is_complete=False,
                    )
                    wire = encode_agent_chunk_for_wire(
                        chunk,
                        response_id=request.request_id,
                        sequence=sequence,
                    )
                    sequence += 1
                    sent_chunk = False
                    async with send_lock:
                        sent_chunk = await send_wire_payload(ws, wire)
                    if not sent_chunk:
                        logger.warning(
                            "[AgentWebSocketServer] history 流式响应因 chunk 超限而停止: "
                            "request_id=%s sequence=%s",
                            request.request_id,
                            sequence,
                        )
                        return

        next_seq = sequence

        # Session open / refresh: push full todo snapshot before history "done"
        # so the frontend todo panel restores without reading workspace files.
        # Only page 1 — pagination must not re-flash the panel.
        if page_idx == 1 and isinstance(session_id, str) and session_id.strip():
            todos = load_todo_snapshot_for_frontend(session_id)
            todo_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "todo.updated",
                    "todos": todos,
                    "session_id": session_id.strip(),
                },
                is_complete=False,
            )
            wire_todo = encode_agent_chunk_for_wire(
                todo_chunk,
                response_id=request.request_id,
                sequence=next_seq,
            )
            sent_todo = False
            async with send_lock:
                sent_todo = await send_wire_payload(ws, wire_todo)
            if not sent_todo:
                # chat timeline still finishes; log so oversized snapshots are visible.
                logger.warning(
                    "[AgentWebSocketServer] history todo.updated snapshot send failed "
                    "(oversized or replaced): request_id=%s session_id=%s seq=%s "
                    "todo_count=%s",
                    request.request_id,
                    session_id.strip(),
                    next_seq,
                    len(todos),
                )
            next_seq += 1

        done_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "history.message",
                "status": "done",
                "session_id": str(session_id or ""),
                "subagent_id": str(response_subagent_id or subagent_id or ""),
                "total_pages": total_pages,
                "page_idx": page,
            },
            is_complete=True,
        )
        wire_done = encode_agent_chunk_for_wire(
            done_chunk,
            response_id=request.request_id,
            sequence=next_seq,
        )
        async with send_lock:
            await send_wire_payload(ws, wire_done)

    async def _handle_command_add_dir(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            directory_path = params.get("path")
            remember = params.get("remember", False)
            persist: dict[str, Any]
            if directory_path is None or (
                    isinstance(directory_path, str) and not directory_path.strip()
            ):
                persist = {"ok": False, "error": "path is required"}
            else:
                persist = persist_cli_trusted_directory(str(directory_path))
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=bool(persist.get("ok", False)),
                payload={
                    "path": directory_path,
                    "remember": remember,
                    "persist": persist,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(e),
                    "code": "BAD_REQUEST" if isinstance(e, ValueError) else "SESSION_CREATE_FAILED",
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_chrome(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_compact(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            # 同 command.btw：先按 session_id 找承载会话的 agent，按 mode 兜底会命中影子 agent。
            agent = self._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id,
                session_id=session_id,
            )
            if agent is None:
                agent = await self._agent_manager.get_agent(
                    channel_id=channel_id,
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode,
                )

            if agent is None:
                raise ValueError("Failed to get agent")

            # /compact 同 /btw：非 chat 通道 RPC 需先 ensure_instance 懒构建根 DeepAgent，
            # 否则 self._instance 为 None 时 compress_context 会直接 noop（误报"无需压缩"）。
            await agent.ensure_instance()

            result_data = await agent.compress_context(session_id=session_id, return_state=True)

            result = result_data.get("result")
            stats = result_data.get("stats")
            state = result_data.get("state") if isinstance(result_data.get("state"), dict) else {}
            summary = str(
                result_data.get("compact_summary")
                or state.get("compact_summary")
                or result_data.get("summary")
                or ""
            ).strip()

            if result == "compressed" and stats:
                before_tokens = stats.get("raw_total_tokens", 0)
                after_tokens = stats.get("total_tokens", 0)
                if before_tokens > 0:
                    rate = round((before_tokens - after_tokens) / before_tokens * 100, 1)
                else:
                    rate = 0
                stats_summary = (
                    f"\u2713 Context compacted: {after_tokens / 1000:.1f}K/"
                    f"{before_tokens / 1000:.1f}K tokens ({rate:.1f}% saved)"
                )

                if summary:
                    append_compact_history_records(
                        session_id=session_id,
                        request_id=request.request_id,
                        channel_id=channel_id,
                        summary=summary,
                        timestamp=_dt.datetime.now().timestamp(),
                        trigger="manual",
                        stats=stats,
                        mode=params.get("mode", "agent"),
                    )
                    compression_state_payload: dict[str, Any] = {
                        **state,
                        "event_type": "context.compression_state",
                        "status": state.get("status") or "completed",
                        "phase": state.get("phase") or "active_compress",
                        "processor": state.get("processor") or _extract_compact_summary_processor(summary),
                        "before": state.get("before") or {"tokens": before_tokens},
                        "after": state.get("after") or {"tokens": after_tokens},
                        "saved": state.get("saved") or {
                            "tokens": before_tokens - after_tokens,
                            "percent": rate,
                        },
                        "summary": stats_summary,
                        "compact_summary": summary,
                    }
                    await self.send_push({
                        "channel_id": channel_id,
                        "session_id": session_id,
                        "payload": compression_state_payload,
                    })

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "result": result,
                    "stats": stats,
                    **({"summary": summary} if summary else {}),
                    **({"compact_summary": summary} if summary else {}),
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_compact_partial(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            turn_index = int(params.get("turn_index", 0))
            direction = str(params.get("direction") or "from").strip()

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.compact_partial(
                session_id=session_id,
                turn_index=turn_index,
                direction=direction,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            logger.exception("[AgentWebSocketServer] command.compact_partial failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_context(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "default"
            params = request.params or {}

            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.get_context_usage(session_id=session_id)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.context failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_recap(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /recap 命令：生成会话快速回顾（read-only，不修改历史）"""
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            mode, sub_mode, canonical_mode = resolve_agent_request_mode(
                params.get("mode", "agent")
            )
            agent_mode = "agent" if mode == "auto_harness" else mode

            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

            if agent is None:
                raise ValueError("Failed to get agent")

            result_data = await agent.generate_recap(
                session_id=session_id,
                current_mode=canonical_mode,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.recap failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_btw(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /btw 命令：独立、无工具、单轮 LLM 侧问题查询。

        - 获取当前会话上下文（最近消息）
        - 用隔离的 LLM 查询回答问题
        - 不修改对话历史
        - 不使用任何工具（纯文本回答）
        - 仅单轮（无后续 token 消耗）
        """
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            question = (params.get("question") or "").strip()

            logger.info(
                "[AgentWebSocketServer] command.btw received: session_id=%s question=%s",
                session_id,
                question[:100] if question else "",
            )

            if not question:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"status": "failed", "error": "Question is required"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await send_wire_payload(ws, wire)
                return

            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode

            agent = self._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id,
                session_id=session_id,
            )
            if agent is None:
                agent = await self._agent_manager.get_agent(
                    channel_id=channel_id,
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode,
                )

            if agent is None:
                raise ValueError("Failed to get agent")

            # /btw 是非 chat 通道 RPC：根适配器默认只作路由/模板，模型在 create_instance
            # 时被 _skip_own_instance_build 跳过、self._model 为 None。先 ensure_instance 懒构建
            # 根 DeepAgent（含模型），否则 _call_model_for_recap 报 "[oneshot] no model instance available"。
            await agent.ensure_instance()

            result_data = await agent.generate_btw_answer(
                session_id=session_id,
                question=question,
            )

            logger.info(
                "[AgentWebSocketServer] command.btw result: status=%s",
                result_data.get("status"),
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.btw failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "status": "failed",
                    "error": str(e),
                },
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_diff(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.session.git_diff_status import get_session_extra_history_roots
        from jiuwenswarm.server.utils.diff_service import get_diff_service

        try:
            session_id = request.session_id or "default"
            project_dir = resolve_request_project_dir(request)
            extra_history_roots = get_session_extra_history_roots(session_id)
            diff_service = get_diff_service()
            turns, git_diff = await asyncio.gather(
                asyncio.to_thread(
                    diff_service.get_turn_diffs,
                    session_id,
                    project_dir,
                    extra_history_roots=extra_history_roots,
                ),
                asyncio.to_thread(diff_service.get_git_diff, project_dir),
            )

            logger.info(
                "[AgentWebSocketServer] command.diff response: session_id=%s turns=%s git_diff=%s project_dir=%s",
                session_id,
                len(turns),
                git_diff is not None,
                project_dir,
            )

            payload: dict[str, Any] = {
                "type": "list",
                "turns": turns,
            }
            if git_diff is not None:
                payload["gitDiff"] = git_diff

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.diff failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_simplify(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 /simplify 命令：组装代码精简审查 prompt 并返回（由前端作为消息发送给 Agent）。

        prompt 指导 Agent 分三阶段完成
        1) 识别改动（git diff）
        2) 三维度审查（复用 / 质量 / 效率）—— 子 Agent 并行审查为可选优化手段
        3) 聚合发现并直接修复
        """
        try:
            params = request.params or {}
            target = str(params.get("target", "")).strip()

            prompt = _build_simplify_prompt(target)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"prompt": prompt},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.simplify failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_model(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            action = params.get("action")

            if action == "add_model":
                target = str(params.get("target", "")).strip()
                logger.info("[command.model] add_model: target=%s", target)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "model_added", "name": target},
                )

            elif action == "switch_model":
                target = str(params.get("model", "")).strip()
                env_updates = params.get("env_updates", {})
                logger.info(
                    "[command.model] switch_model: target=%s, env_updates=%s",
                    target,
                    mask_sensitive(env_updates),
                )

                if not env_updates:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "No env_updates provided"},
                    )
                elif _is_env_api_base_placeholder(env_updates):
                    api_base_val = str(env_updates.get("API_BASE", ""))
                    logger.warning(
                        "[command.model] switch_model rejected: API_BASE is a placeholder domain: %s",
                        api_base_val,
                    )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "error": f"API_BASE '{api_base_val}' 指向占位域名，无法实际提供服务，请配置有效的 API 地址",
                        },
                    )
                else:
                    for k, v in env_updates.items():
                        os.environ[k] = v
                    logger.info("[command.model] os.environ 已更新, MODEL_NAME=%s", os.getenv("MODEL_NAME", "unknown"))

                    try:
                        from jiuwenswarm.agents.harness.common.memory.config import clear_config_cache
                        clear_config_cache()
                        logger.info("[command.model] config cache 已清除")
                    except Exception as e:
                        logger.debug("[command.model] clear_config_cache skipped: %s", e)

                    try:
                        await self._agent_manager.reload_agents_config(None, env_updates)
                        logger.info("[command.model] agent config 已重载")
                    except Exception as e:
                        logger.debug("[command.model] reload_agents_config skipped: %s", e)

                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "current": os.getenv("MODEL_NAME", "unknown"),
                            "requested": target,
                            "type": "switched",
                            "applied": True,
                        },
                    )
                    logger.info("[command.model] 切换完成: current=%s", os.getenv("MODEL_NAME", "unknown"))

            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"current": os.getenv("MODEL_NAME", "unknown"), "available": ["default-model"]},
                )

        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    @staticmethod
    def _mask_sensitive_fields(payload: Any) -> Any:
        if isinstance(payload, dict):
            masked: dict[str, Any] = {}
            for key, value in payload.items():
                key_text = str(key).lower()
                value_text = value.lower() if isinstance(value, str) else ""
                key_sensitive = any(
                    token in key_text for token in ("api_key", "token", "authorization", "secret")
                )
                value_sensitive = any(token in value_text for token in ("bearer ", "api-key ", "secret-"))
                if (key_sensitive or value_sensitive) and not isinstance(value, (dict, list)):
                    masked[key] = "***"
                else:
                    masked[key] = AgentWebSocketServer._mask_sensitive_fields(value)
            return masked
        if isinstance(payload, list):
            return [AgentWebSocketServer._mask_sensitive_fields(item) for item in payload]
        return payload

    @staticmethod
    async def _pre_check_mcp_server(server_payload: dict[str, Any]) -> tuple[bool, str]:
        """Try a temporary connection to verify the MCP server is reachable.

        Uses ``logging.disable(CRITICAL)`` to silence the SDK's verbose
        "Failed to parse JSONRPC message" tracebacks and wraps everything
        in tight timeouts so a broken server cannot block the caller.

        Returns ``(ok, message)``.
        """
        import logging as _logging
        from openjiuwen.core.foundation.tool import McpServerConfig
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

        name = server_payload.get("name", "")
        transport = server_payload.get("transport", "")

        # Build McpServerConfig (same logic as _fetch_mcp_tools_from_config)
        payload: dict[str, Any] = {"server_name": name, "client_type": transport}
        if transport == "stdio":
            command = server_payload.get("command", "")
            if not command:
                return True, "skipped: no command"
            # stdio 预检查改为纯静态校验,静态校验零 spawn、零 anyio。
            if not shutil.which(command):
                return False, f"{name} (stdio) pre-check failed: command not found in PATH: {command}"
            raw_args = server_payload.get("args") or []
            if isinstance(raw_args, list):
                for arg in raw_args:
                    if not isinstance(arg, str):
                        continue
                    looks_like_path = (
                        arg.startswith(("/", "./", "../", "~"))
                        or arg.endswith((".js", ".mjs", ".cjs", ".json", ".py", ".sh"))
                    )
                    if looks_like_path and not Path(arg).expanduser().exists():
                        return False, f"{name} (stdio) pre-check failed: file not found: {arg}"
            return True, f"{name} (stdio) pre-check passed (static)"
        else:
            url = server_payload.get("url", "")
            if not url:
                return True, "skipped: no url"
            payload["server_path"] = url
            params = {}
            if isinstance(server_payload.get("headers"), dict):
                params["headers"] = {str(k): str(v) for k, v in server_payload["headers"].items()}
            if params:
                payload["params"] = params

        cfg = McpServerConfig(**payload)
        client = ToolMgr._create_client(cfg)
        _logging.disable(_logging.CRITICAL)
        try:
            connected = await asyncio.wait_for(client.connect(), timeout=15.0)
            if not connected:
                return False, f"{name} ({transport}) pre-check failed: connection refused"
            return True, f"{name} ({transport}) pre-check passed"
        except asyncio.TimeoutError:
            return False, f"{name} ({transport}) pre-check failed: connection timed out"
        except Exception as exc:
            return False, f"{name} ({transport}) pre-check failed: {exc}"
        finally:
            _logging.disable(_logging.NOTSET)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                # disconnect runs anyio task-group teardown which raises an
                # ExceptionGroup (Exception subclass) on SSE/HTTP streamable
                # clients. The pre-check already returns a (ok, msg) tuple, so
                # just swallow the disconnect error.
                logger.debug(
                    "[mcp] pre-check disconnect for %s failed: %r", name, exc,
                )

    @staticmethod
    async def _pre_check_mcp_http_auth(
        server_payload: dict[str, Any]
    ) -> tuple[bool, str]:
        """Config-time HTTP probe: reject bad auth (401/403), timeouts, and
        unreachable hosts before writing config.yaml. Delegates to
        ``preflight_mcp_server_reachable`` (shared with cold-start) so both
        gates stay identical. See that function for the anyio-corruption
        rationale.
        """
        from jiuwenswarm.common.mcp_config import (
            build_mcp_server_config,
            preflight_mcp_server_reachable,
        )

        name = str(server_payload.get("name", "") or "").strip()
        transport = str(server_payload.get("transport", "") or "").strip().lower()
        cfg = build_mcp_server_config(server_payload, server_id_scope="jiuwenswarm")
        if cfg is None:
            return False, f"{name} ({transport}) pre-check failed: invalid config entry"
        ok, reason = await preflight_mcp_server_reachable(cfg)
        if ok:
            return True, f"{name} ({transport}) pre-check passed: {reason}"
        return False, f"{name} ({transport}) pre-check failed: {reason}"

    @staticmethod
    async def _fetch_mcp_tools_from_config(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Create a temporary MCP connection from config entry and list tools.

        Thin delegate to the shared :func:`fetch_mcp_tools_via_temp_connection`
        so the gateway-layer ``mcp.show`` and the agent-layer command.mcp paths
        use one implementation (placeholder resolve, anyio exception-group
        coercion, disconnect cleanup all live in one place)."""
        from jiuwenswarm.common.mcp_config import fetch_mcp_tools_via_temp_connection
        return await fetch_mcp_tools_via_temp_connection(entry)

    @staticmethod
    def _normalize_mcp_payload(
            params: dict[str, Any], current: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        merged = dict(current or {})
        merged.update(params)
        name = str(merged.get("name", "")).strip()
        transport = str(merged.get("transport", "")).strip().lower()
        if not name:
            raise ValueError("MCP server name is required")
        if transport not in {"stdio", "sse", "http", "streamable-http", "streamable_http"}:
            raise ValueError("transport must be one of stdio|sse|http")

        payload: dict[str, Any] = {
            "name": name,
            "enabled": bool(merged.get("enabled", True)),
            "transport": transport,
        }
        if transport == "stdio":
            command = str(merged.get("command", "")).strip()
            if not command:
                raise ValueError("stdio transport requires command")
            payload["command"] = command
            args = merged.get("args")
            if isinstance(args, list):
                payload["args"] = [str(item) for item in args]
            else:
                # openjiuwen's StdioServerParameters requires args as a list;
                # None fails validation. Default [] for a bare command.
                payload["args"] = []
            cwd = merged.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                payload["cwd"] = cwd.strip()
            env = merged.get("env")
            if isinstance(env, dict):
                payload["env"] = {str(k): str(v) for k, v in env.items()}
        else:
            url = str(merged.get("url", "")).strip()
            if not url:
                raise ValueError(f"{transport} transport requires url")
            payload["url"] = url
            headers = merged.get("headers")
            if isinstance(headers, dict):
                payload["headers"] = {str(k): str(v) for k, v in headers.items()}
            timeout_s = merged.get("timeout_s")
            if isinstance(timeout_s, (int, float)):
                payload["timeout_s"] = int(timeout_s)
        return payload

    def _normalize_mcp_add_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._normalize_mcp_payload(params)

    def _normalize_mcp_update_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server name is required")
        current = get_mcp_server_config(name)
        if current is None:
            raise KeyError(f"MCP server '{name}' not found")
        return self._normalize_mcp_payload(params, current=current)

    async def _handle_command_mcp(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            action = str(params.get("action", "list")).strip().lower()

            if action == "list":
                items = [self._mask_sensitive_fields(item) for item in get_mcp_servers()]
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "list", "items": items},
                )
            elif action == "show":
                name = str(params.get("name", "")).strip()
                if name:
                    item = get_mcp_server_config(name)
                    if item is None:
                        raise KeyError(f"MCP server '{name}' not found")
                    masked = self._mask_sensitive_fields(item)
                    # Enrich with tool count
                    tool_count = 0
                    try:
                        from openjiuwen.core.runner import Runner
                        resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
                        if resource_registry is not None:
                            tool_mgr = resource_registry.tool()
                            server_ids = tool_mgr.get_mcp_server_ids(name)
                            if not server_ids:
                                for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
                                    if getattr(res.config, "server_name", "") == name:
                                        server_ids.append(sid)
                            _seen: set[str] = set()
                            for sid in server_ids:
                                for _tid in tool_mgr.get_mcp_tool_ids(sid):
                                    _t = getattr(tool_mgr, "_tools", {}).get(_tid)
                                    if _t is not None and hasattr(_t, "card"):
                                        _n = _t.card.name
                                        if _n not in _seen:
                                            _seen.add(_n)
                                            tool_count += 1
                    except Exception as exc:
                        logger.debug("[command.mcp] show tool_count from ToolMgr failed: %s", exc)
                    # If ToolMgr has no data, try temporary connection
                    if tool_count == 0 and bool(item.get("enabled", True)):
                        try:
                            tools = await self._fetch_mcp_tools_from_config(item)
                            tool_count = len(tools)
                        except Exception as exc:
                            logger.warning("[command.mcp] show tool_count from temp connection failed: %s", exc)
                    masked["tool_count"] = tool_count
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={"type": "detail", "item": masked},
                    )
                else:
                    enabled_items = [
                        self._mask_sensitive_fields(item)
                        for item in get_mcp_servers()
                        if bool(item.get("enabled", True))
                    ]
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={"type": "list", "items": enabled_items},
                    )
            elif action == "add":
                server_payload = self._normalize_mcp_add_payload(params)

                # Reject a broken MCP entry before it is persisted to config.yaml;
                # a bad entry (e.g. wrong Bearer → HTTP 401) surviving to
                # cold-start corrupts the anyio task group ("restart then can't
                # chat" symptom).
                pre_check_failed = False
                if bool(server_payload.get("enabled", True)):
                    _need_pre_check = False
                    _transport = str(server_payload.get("transport", "") or "").strip().lower()
                    if _transport == "stdio":
                        # Static-only probe (zero spawn): verify command in PATH
                        # and arg paths exist, catching typos like "pyhton".
                        _need_pre_check = bool(server_payload.get("command", ""))
                    elif _transport in ("sse", "http", "streamable-http", "streamable_http"):
                        # HTTP-family always probed via pure httpx (never
                        # client.connect(), which leaks on 401/timeout).
                        _need_pre_check = True
                    if _need_pre_check:
                        if _transport == "stdio":
                            check_ok, check_msg = await self._pre_check_mcp_server(server_payload)
                        else:
                            check_ok, check_msg = await self._pre_check_mcp_http_auth(server_payload)
                        if not check_ok:
                            logger.warning("[command.mcp] add pre-check failed: %s", check_msg)
                            resp = AgentResponse(
                                request_id=request.request_id,
                                channel_id=request.channel_id,
                                ok=False,
                                payload={
                                    "type": "add_failed",
                                    "name": server_payload["name"],
                                    "error": check_msg,
                                },
                            )
                            pre_check_failed = True
                        else:
                            logger.info("[command.mcp] add pre-check ok: %s", check_msg)

                if not pre_check_failed:
                    # 对于 update，先读旧配置，判断是否真有变化
                    name = server_payload.get("name", "")
                    old_item = get_mcp_server_config(name) if name else None

                    _, created = upsert_mcp_server(server_payload)
                    applied = True
                    error_message = ""

                    # 判断是否需要 reload: 新增必然需要；更新时做完整比较，
                    # 配置完全一致才跳过（dict 比较成本极低，避免漏字段导致改了不生效）。
                    config_changed = created
                    if not created and old_item is not None:
                        config_changed = (dict(old_item) != dict(server_payload))
                        if not config_changed:
                            logger.info(
                                "[command.mcp] add/update skipped reload: '%s' config unchanged", name
                            )

                    if config_changed:
                        try:
                            await self._agent_manager.reload_agents_config(get_config(), None)
                        except Exception as reload_exc:  # noqa: BLE001
                            applied = False
                            error_message = str(reload_exc)
                            logger.warning("[command.mcp] reload after add failed: %s", reload_exc)

                    resp_payload: dict[str, Any] = {
                        "type": "added" if created else "updated",
                        "name": server_payload["name"],
                        "applied": applied,
                    }
                    if error_message:
                        resp_payload["error"] = error_message
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload=resp_payload,
                    )
            elif action in {"enable", "disable"}:
                name = str(params.get("name", "")).strip()
                if not name:
                    raise ValueError("MCP server name is required")
                enabled = action == "enable"

                # 读取旧状态以判断 enabled 是否真的变化（容忍读取失败/不存在，
                # 此时回退为"按变化处理"，由 set_mcp_server_enabled_in_config 自己
                # 校验存在性并在缺失时抛 KeyError 交外层统一处理）。
                old_enabled = None
                try:
                    old_item = get_mcp_server_config(name)
                    if old_item is not None:
                        old_enabled = bool(old_item.get("enabled", True))
                except Exception:  # noqa: BLE001
                    old_enabled = None

                # set_mcp_server_enabled 双查(state.json 优先，兜底 config.yaml)，
                # 在 server 不存在时抛 KeyError，由外层统一返回 MCP_NOT_FOUND。
                item = set_mcp_server_enabled(name, enabled)

                # 只有 enabled 状态真的改变才需要 reload；无法判断旧状态时保守 reload。
                config_changed = (old_enabled is None) or (old_enabled != enabled)
                if not config_changed:
                    logger.info(
                        "[command.mcp] %s skipped reload: '%s' already %s",
                        action, name, "enabled" if enabled else "disabled",
                    )

                applied = True
                error_message = ""
                if config_changed:
                    try:
                        await self._agent_manager.reload_agents_config(get_config(), None)
                    except Exception as reload_exc:  # noqa: BLE001
                        applied = False
                        error_message = str(reload_exc)
                        logger.warning("[command.mcp] reload after %s failed: %s", action, reload_exc)

                payload = {
                    "type": "enabled" if enabled else "disabled",
                    "name": name,
                    "applied": applied,
                    "item": self._mask_sensitive_fields(item),
                }
                if error_message:
                    payload["error"] = error_message
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
            elif action in {"remove", "delete"}:
                name = str(params.get("name", "")).strip()
                if not name:
                    raise ValueError("MCP server name is required")
                # remove_mcp_server 双查(state.json 优先，兜底 config.yaml)，
                # 在 server 不存在时抛 KeyError，由外层统一返回 MCP_NOT_FOUND，
                # 且不会触发 reload（删除不存在 = 无变化）。
                removed = remove_mcp_server(name)
                applied = True
                error_message = ""
                try:
                    await self._agent_manager.reload_agents_config(get_config(), None)
                except Exception as reload_exc:  # noqa: BLE001
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning("[command.mcp] reload after remove failed: %s", reload_exc)
                payload = {
                    "type": "removed",
                    "name": name,
                    "applied": applied,
                    "item": self._mask_sensitive_fields(removed),
                }
                if error_message:
                    payload["error"] = error_message
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
            elif action == "update":
                normalized = self._normalize_mcp_update_payload(params)

                # Same config-time pre-check as add: reject before upserting
                # (avoid cold-start 401 → anyio corruption). Kept from develop's
                # HTTP/stdio pre-check enhancement; the upsert target switched
                # to upsert_mcp_server (state.json-routing) to match the
                # dynamic-loading MCP feature on the feature branch.
                pre_check_failed = False
                if bool(normalized.get("enabled", True)):
                    _transport = str(normalized.get("transport", "") or "").strip().lower()
                    if _transport in ("sse", "http", "streamable-http", "streamable_http"):
                        check_ok, check_msg = await self._pre_check_mcp_http_auth(normalized)
                    elif _transport == "stdio":
                        check_ok, check_msg = await self._pre_check_mcp_server(normalized)
                    else:
                        check_ok, check_msg = True, "skipped"
                    if not check_ok:
                        logger.warning("[command.mcp] update pre-check failed: %s", check_msg)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "type": "update_failed",
                                "name": normalized["name"],
                                "error": check_msg,
                            },
                        )
                        pre_check_failed = True

                if not pre_check_failed:
                    _, _created = upsert_mcp_server(normalized)
                    applied = True
                    error_message = ""
                    try:
                        await self._agent_manager.reload_agents_config(get_config(), None)
                    except Exception as reload_exc:  # noqa: BLE001
                        applied = False
                        error_message = str(reload_exc)
                        logger.warning("[command.mcp] reload after update failed: %s", reload_exc)
                    payload = {
                        "type": "updated",
                        "name": normalized["name"],
                        "applied": applied,
                        "item": self._mask_sensitive_fields(normalized),
                    }
                    if error_message:
                        payload["error"] = error_message
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload=payload,
                    )
            elif action == "list_tools":
                name = str(params.get("name", "")).strip()
                if not name:
                    raise ValueError("MCP server name is required")
                tools_info: list[dict[str, Any]] = []
                # 1) Try from ToolMgr (already registered)
                try:
                    from openjiuwen.core.runner import Runner
                    resource_registry = getattr(Runner.resource_mgr, "_resource_registry", None)
                    if resource_registry is not None:
                        tool_mgr = resource_registry.tool()
                        server_ids = list(tool_mgr.get_mcp_server_ids(name))
                        if not server_ids:
                            for sid, res in getattr(tool_mgr, "_mcp_server_resources", {}).items():
                                if getattr(res.config, "server_name", "") == name:
                                    server_ids.append(sid)
                        seen_tool_names: set[str] = set()
                        for sid in server_ids:
                            tool_ids = tool_mgr.get_mcp_tool_ids(sid)
                            for tid in tool_ids:
                                tool = getattr(tool_mgr, "_tools", {}).get(tid)
                                if tool is not None and hasattr(tool, "card"):
                                    card = tool.card
                                    if card.name in seen_tool_names:
                                        continue
                                    seen_tool_names.add(card.name)
                                    params_schema = card.input_params if hasattr(card, "input_params") else {}
                                    if hasattr(params_schema, "model_dump"):
                                        params_schema = params_schema.model_dump()
                                    tools_info.append({
                                        "id": card.id,
                                        "name": card.name,
                                        "description": card.description or "",
                                        "parameters": params_schema,
                                        "server_name": name,
                                    })
                except Exception as exc:
                    logger.debug("[command.mcp] list_tools from ToolMgr failed: %s", exc)
                # 2) If no tools found, try temporary MCP connection from config
                if not tools_info:
                    try:
                        config_entry = get_mcp_server_config(name)
                        if config_entry and bool(config_entry.get("enabled", True)):
                            tools_info = await self._fetch_mcp_tools_from_config(config_entry)
                    except Exception as exc:
                        logger.warning("[command.mcp] list_tools from temp connection failed: %s", exc)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "tools", "tools": tools_info, "server_name": name},
                )
            else:
                raise ValueError("Unsupported action, must be one of " \
                                 "list|show|add|update|enable|disable|remove|list_tools")
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.mcp failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.list`` RPC: list marketplace MCPs (read-only).

        filter: builtin(预置目录) | local(已连接预置 + 全部自定义)。兜底 builtin。
        web 不转发此入口（网关本地已处理），保留只为语义对齐。
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import (
                list_marketplace_mcps,
            )
            params = request.params or {}
            filter_val = str(params.get("filter") or "builtin").strip().lower() or "builtin"
            if filter_val not in ("builtin", "local"):
                filter_val = "builtin"
            items = await asyncio.to_thread(list_marketplace_mcps, filter_val)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "list", "items": items},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.list failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_show(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.show`` RPC: return one MCP detail with tools."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import get_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            item = get_mcp(name)
            if item is None:
                raise KeyError(f"mcp '{name}' not found")
            # Connected MCP but ToolMgr returned no tools: fall back to a
            # temporary connection. Shared with the gateway-layer show so the
            # fallback logic lives in one place (fill_mcp_tools_fallback).
            from jiuwenswarm.common.mcp_config import fill_mcp_tools_fallback
            await fill_mcp_tools_fallback(item, name)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "detail", "item": item},
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "show_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.show failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.connect`` RPC: install a marketplace MCP.

        Form A/B upsert config + hot-reload. Form C (CLI) runs CliDriver;
        when an auth step needs user action it returns an ``auth_required``
        sentinel with the extracted ``auth_url``. The frontend opens that URL
        in the user's browser (the CLI binary may not auto-open, and the
        backend may be headless/remote), then sends ``mcp.wait_auth`` which
        holds-open polling ``complete_cli_auth`` until OAuth completes.
        """
        from jiuwenswarm.server.runtime.mcp.registry import CliConnectError
        try:
            from jiuwenswarm.server.runtime.mcp.registry import connect_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            item = await asyncio.to_thread(connect_mcp, name)
            if isinstance(item, dict) and item.get("auth_required"):
                # CLI form: connect_mcp started the CLI auth command and
                # extracted the auth_url. Return it now so the frontend can
                # open the browser (the CLI binary may not auto-open, and the
                # backend may be headless/remote — the frontend's browser is
                # the reliable place to open the auth page). The frontend then
                # sends mcp.wait_auth to hold-open poll until OAuth completes.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "auth_required", **self._mask_sensitive_fields(item)},
                )
            elif isinstance(item, dict) and item.get("credentials_required"):
                # Form B missing token: surface a prompt instead of reloading —
                # there is no config entry to reload until tokens are provisioned.
                # NOTE: item fields are metadata (required_tokens list,
                # credential_kind), not secrets — pass through unmasked. The
                # generic _mask_sensitive_fields keys on 'token' substrings and
                # would replace required_tokens with '***', breaking the
                # frontend's array methods on that list.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "credentials_required", **item},
                )
            else:
                # Connect-time live-connect probe: not just register the entry,
                # but confirm the MCP is actually usable before reporting
                # "connected". For server-bearing types (stdio / remote-mcp /
                # hybrid-cli's mcp.json subcommand) this spawns the stdio
                # subprocess + MCP initialize handshake, or does a real HTTP
                # connect — so npx first-install cost and handshake failures
                # surface HERE (the user waits on connect) instead of silently
                # degrading to "no tools" on the first chat message. A
                # successful probe leaves the connection in the process-global
                # Runner.resource_mgr cache, which reconcile reuses (no
                # duplicate spawn on chat.send). skill-only / pure-CLI MCPs have
                # no server entry — the probe returns (True, "") and they
                # surface via bundled skills.
                probe_ok, probe_reason = await self._agent_manager.probe_mcp_live_connection(name)
                if not probe_ok:
                    try:
                        # Roll back: marketplace → remove record+skills; custom
                        # → flip to registered (keep definition for edit/retry,
                        # do NOT delete).
                        from jiuwenswarm.server.runtime.mcp.registry import (
                            rollback_failed_connect,
                        )
                        rollback_failed_connect(name)
                    except Exception as rollback_exc:  # noqa: BLE001
                        logger.warning(
                            "[mcp] rollback failed-probe entry '%s' failed: %s",
                            name, rollback_exc,
                        )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "type": "connect_failed",
                            "error": probe_reason or "MCP live-connect probe failed",
                            "code": "MCP_UNREACHABLE",
                            "name": name,
                        },
                    )
                else:
                    # Probe succeeded (or no server to probe) — flip
                    # connecting → connected so the MCP is selectable per
                    # session, sync token env for skill-only bundled scripts,
                    # and return. The MCP is NOT loaded into a specific agent
                    # session here: session-level enable is driven by the
                    # ``mcp`` field on chat.send (see reconcile_session_mcp).
                    try:
                        from jiuwenswarm.server.runtime.mcp.state_store import (
                            set_mcp_state,
                        )
                        set_mcp_state(name, state="connected")
                    except Exception as flip_exc:  # noqa: BLE001
                        logger.warning(
                            "[mcp] flip connecting→connected after connect failed for '%s': %s",
                            name, flip_exc,
                        )
                    # Skill-only connectors' bundled scripts read tokens from
                    # os.environ; sync the just-provisioned token into the agent
                    # process env so BashTool inherits it. No-op for MCP-type
                    # connectors (their tokens resolve via McpServerConfig).
                    try:
                        self._agent_manager.sync_mcp_credentials()
                    except Exception as sync_exc:  # noqa: BLE001
                        logger.warning("[mcp] sync_mcp_credentials after connect failed: %s", sync_exc)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "type": "connected",
                            "name": name,
                            "applied": True,
                            "item": self._mask_sensitive_fields(item),
                        },
                    )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "connect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except CliConnectError as exc:
            # Classifiable CLI failure (runtime missing / network / incomplete).
            # Surface code + runtime + install_cmd so the frontend shows an
            # actionable i18n hint instead of the raw "[WinError 2]" string.
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "error": str(exc),
                    "code": exc.code,
                    "runtime": exc.runtime,
                    "install_cmd": exc.install_cmd,
                    "name": name,
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.connect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_wait_auth(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.wait_auth`` RPC: hold-open poll for CLI OAuth completion.

        After ``mcp.connect`` returned ``auth_required`` (the frontend opened
        the browser when ``auth_url`` was non-empty), the frontend sends this
        RPC which polls :func:`complete_cli_auth` (via :meth:`_await_cli_auth`)
        until the user finishes OAuth in the browser, then returns the single
        ``connected``/``auth_failed`` response. Holds the RPC open for minutes;
        the frontend shows a "connecting…" spinner while waiting.
        """
        from jiuwenswarm.server.runtime.mcp.registry import CliConnectError
        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            step_index = int(params.get("step_index", 0) or 0)
            result = await self._await_cli_auth(name, step_index)
            if result.get("type") == "auth_failed":
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"type": "connect_failed", "name": name,
                             "error": str(result.get("error", "")), "code": "MCP_AUTH_FAILED"},
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=result,
                )
        except CliConnectError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "type": "connect_failed",
                    "error": str(exc),
                    "code": exc.code,
                    "runtime": exc.runtime,
                    "install_cmd": exc.install_cmd,
                    "name": name,
                },
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.wait_auth failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_disconnect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.disconnect`` RPC: remove from config + reload."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import disconnect_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            removed = await asyncio.to_thread(disconnect_mcp, name)
            applied = True
            error_message = ""
            try:
                # Phase-2: targeted unregister (no full reload). SSE/HTTP MCP
                # removal can raise "Attempted to exit cancel scope in a
                # different task" (openjiuwen closes the client TaskGroup from
                # the wrong task) — that propagates as CancelledError and tears
                # down this handler before it can reply. Shield + timeout so the
                # state.json change (already done above) still lands and the
                # frontend gets a response; the MCP server process is torn down
                # by the agent's next reload even if this call fails.
                await asyncio.wait_for(
                    asyncio.shield(self._agent_manager.apply_mcp_change(name, "remove")),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                applied = False
                error_message = "MCP unregister timed out (state updated; server will clear on next reload)"
                logger.warning("[mcp] apply_mcp_change after disconnect timed out for '%s'", name)
            except asyncio.CancelledError:
                # apply_mcp_change runs inside asyncio.shield, so an inner
                # SSE cancel-scope error cannot surface here as a
                # CancelledError (shield absorbs it). The only way this
                # except fires is the *outer* request being cancelled (ws
                # disconnect) — propagate it so the request unwinds cleanly
                # instead of dragging through clear/refresh and sending a
                # response to a dead socket.
                logger.warning(
                    "[mcp] apply_mcp_change after disconnect CancelledError for '%s'", name
                )
                raise
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning("[mcp] apply_mcp_change after disconnect failed: %s", reload_exc)
            # Clear this MCP's token env vars from the agent process so a
            # disconnected skill-only MCP's token doesn't linger.
            try:
                self._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning("[mcp] clear_mcp_credentials after disconnect failed: %s", clear_exc)
            # disconnect uninstalled the bundled skills + unregistered the dir;
            # reload SkillUseRail so the agent stops seeing those skills.
            try:
                await self._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:  # noqa: BLE001
                logger.warning("[mcp] refresh_skill_rails after disconnect failed: %s", skill_exc)
            payload = {
                "type": "disconnected",
                "name": name,
                "applied": applied,
                "item": self._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "disconnect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.disconnect failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _finalize_cli_auth(self, name: str, item: dict[str, Any]) -> dict[str, Any]:
        """Run the post-auth side-effects for a CLI MCP connect.

        Auth is done; skills were installed during registry ``_finalize_cli``.
        Syncs MCP tokens into os.environ so bundled skill scripts (BashTool
        inherits os.environ) see their token env vars, then promotes
        connecting → connected. The MCP is NOT loaded into the agent here —
        session-level enable is driven by the ``mcp`` field on chat.send (see
        reconcile_session_mcp). Returns a payload dict for the push signal.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                set_mcp_state,
            )
            set_mcp_state(name, state="connected")
        except Exception as flip_exc:  # noqa: BLE001
            logger.warning(
                "[mcp] flip connecting→connected after CLI auth failed for '%s': %s",
                name, flip_exc,
            )
        try:
            self._agent_manager.sync_mcp_credentials()
        except Exception as sync_exc:  # noqa: BLE001
            logger.warning("[mcp] sync_mcp_credentials after CLI auth failed: %s", sync_exc)
        return {
            "type": "connected",
            "name": name,
            "applied": True,
            "item": self._mask_sensitive_fields(item),
        }

    async def _await_cli_auth(
        self, name: str, step_index: int,
        *, max_attempts: int = 200, delay: float = 3.0,
    ) -> dict[str, Any]:
        """Poll a CLI MCP's auth status until authenticated, returning the result.

        Called inline by :meth:`_handle_mcp_connect` after ``connect_mcp``
        returns an ``auth_required`` sentinel (the CLI process already opened
        the browser). This holds the RPC open — the frontend shows a
        "connecting…" spinner — and loops :func:`complete_cli_auth` until the
        user finishes OAuth, then runs the post-auth side-effects and returns
        a ``connected`` payload (or ``auth_failed`` on timeout/error) for the
        handler to send as the single RPC response. No push channel needed.

        Multi-step CLIs (e.g. feishu: config init → auth login): when a step
        completes and the next needs user action, ``complete_cli_auth`` returns
        ``auth_required=True`` with the new ``step_index``; we adopt it as
        ``cur_step`` so the next poll queries the new step — otherwise the
        poller would re-query the old step forever (a real dead-loop on
        multi-step CLIs).

        Only CLI MCPs reach here (form A/B/D return ``auth_required=False`` or
        ``credentials_required`` from :func:`connect_mcp`; only
        ``itype == "cli"`` calls ``_connect_cli`` which can return the
        ``auth_required`` sentinel). No form A/B/D path triggers this loop.
        """
        from jiuwenswarm.server.runtime.mcp.registry import (
            CliConnectError,
            complete_cli_auth,
        )

        cur_step = max(0, int(step_index))
        last_output = ""
        try:
            for attempt in range(max_attempts):
                item = await asyncio.to_thread(complete_cli_auth, name, cur_step)
                if not isinstance(item, dict):
                    raise ValueError(f"complete_cli_auth returned non-dict: {item!r}")
                if item.get("auth_required"):
                    # Still pending — either the user hasn't finished in the
                    # browser, or a step just completed and the next step needs
                    # action. Adopt the authoritative step_index so we don't
                    # loop on a stale step (dead-loop guard).
                    new_step = item.get("step_index")
                    if isinstance(new_step, (int, float)):
                        cur_step = max(cur_step, int(new_step))
                    last_output = str(item.get("output") or item.get("matched") or "")[:300]
                    logger.debug(
                        "[mcp] _await_cli_auth '%s' still pending (attempt %d/%d, step %d): %s",
                        name, attempt + 1, max_attempts, cur_step, last_output[:200],
                    )
                    await asyncio.sleep(delay)
                    continue
                # Authenticated — finalize and return the connected payload.
                return await self._finalize_cli_auth(name, item)
            # Exhausted retries (~10 min) — return a failure so the handler can
            # surface it. Include the last status output so a misaligned
            # statusMatch (e.g. dingtalk's CLI status JSON not matching
            # cli.json's statusMatch keys) is diagnosable from the error.
            logger.warning(
                "[mcp] _await_cli_auth '%s' timed out after %d attempts (~%.0f min). "
                "last status output: %s",
                name, max_attempts, max_attempts * delay / 60, last_output,
            )
            timeout_error = "Authorization timed out. Retry connect."
            if last_output:
                timeout_error = f"{timeout_error} (last status: {last_output})"
            return {"type": "auth_failed", "name": name, "error": timeout_error}
        except CliConnectError:
            # Re-raise so _handle_mcp_wait_auth's except CliConnectError can
            # surface the structured code; otherwise the generic except below
            # flattens it into an auth_failed string, dropping code/runtime.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] _await_cli_auth '%s' failed: %s", name, exc)
            return {"type": "auth_failed", "name": name, "error": str(exc)}

    async def _handle_mcp_register_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.register_custom`` RPC: write a user-defined MCP server config.

        Only persists the definition to state.json (state=registered) and returns
        immediately; the frontend follows up with ``mcp.connect`` to actually
        activate it. Editing an already-connected custom MCP removes the old live
        instance so the new config takes effect on connect.
        """
        from jiuwenswarm.server.runtime.mcp.registry import McpRegistryError
        try:
            from jiuwenswarm.server.runtime.mcp.registry import register_custom_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            config = {k: v for k, v in params.items() if k != "name"}
            entry = await asyncio.to_thread(register_custom_mcp, name, config)
            was_connected = bool(entry.pop("was_connected", False))
            # Edit of a connected instance: remove the old live server so the
            # new config (just persisted as state=registered) takes effect on
            # the frontend's follow-up mcp.connect. Failure here is non-fatal —
            # the old instance also drops on the next agent reload.
            if was_connected:
                try:
                    await self._agent_manager.apply_mcp_change(name, "remove")
                except Exception as reload_exc:  # noqa: BLE001
                    logger.warning(
                        "[mcp] remove old instance after register_custom '%s' failed: %s",
                        name, reload_exc,
                    )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "registered",
                    "name": name,
                    "item": self._mask_sensitive_fields(entry),
                },
            )
        except McpRegistryError as exc:
            # Classifiable registry failure (e.g. name collides with a builtin
            # package). Surface code so the frontend shows an actionable hint
            # ("name taken by a builtin, rename") instead of a generic bad_request.
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": exc.code, "name": name},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.register_custom failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_delete_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.delete_custom``: terminal teardown of a custom MCP."""
        try:
            from jiuwenswarm.server.runtime.mcp.registry import delete_custom_mcp
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            # Clear env vars first — delete_custom_mcp wipes the credential
            # file + state record, and after that _mcp_env_keys returns [] so
            # the token would leak in os.environ. Clearing BEFORE the wipe
            # means the CredentialStore still has the keys to read.
            try:
                self._agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning("[mcp] clear_mcp_credentials before delete failed: %s", clear_exc)
            removed = await asyncio.to_thread(delete_custom_mcp, name)
            was_connected = bool(removed.get("was_connected", False))
            applied = True
            error_message = ""
            if was_connected:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._agent_manager.apply_mcp_change(name, "remove")),
                        timeout=15.0,
                    )
                except asyncio.TimeoutError:
                    applied = False
                    error_message = "MCP unregister timed out (state deleted; server will clear on next reload)"
                    logger.warning("[mcp] apply_mcp_change after delete timed out for '%s'", name)
                except asyncio.CancelledError:
                    logger.warning("[mcp] apply_mcp_change after delete CancelledError for '%s'", name)
                    raise  # outer ws-disconnect — don't reply to a dead socket
                except Exception as reload_exc:  # noqa: BLE001
                    applied = False
                    error_message = str(reload_exc)
                    logger.warning("[mcp] apply_mcp_change after delete failed: %s", reload_exc)
            try:
                await self._agent_manager.refresh_skill_rails()
            except Exception as skill_exc:  # noqa: BLE001
                logger.warning("[mcp] refresh_skill_rails after delete failed: %s", skill_exc)
            payload = {
                "type": "deleted",
                "name": name,
                "applied": applied,
                "item": self._mask_sensitive_fields(removed),
            }
            if error_message:
                payload["error"] = error_message
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except KeyError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "delete_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.delete_custom failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_mcp_save_credentials(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle ``mcp.save_credentials`` RPC: persist user-supplied tokens.

        Form B connectors (tianyancha/gildata/gmail/jira...) declare ``${VAR}``
        placeholders in mcp.json; the user fills the values via a frontend
        prompt, which calls this RPC. Tokens are stored in the local
        CredentialStore (never returned in the response). Does NOT reload —
        the caller follows up with ``mcp.connect`` to actually register.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.registry import save_mcp_credentials
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("mcp name is required")
            tokens = params.get("tokens") or {}
            if not isinstance(tokens, dict) or not tokens:
                raise ValueError("tokens (non-empty dict) is required")
            result = await asyncio.to_thread(save_mcp_credentials, name, tokens)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "credentials_saved", "name": name, "saved_keys": result.get("saved_keys", [])},
            )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] mcp.save_credentials failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_sandbox(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 ``/sandbox`` 命令.

        子命令通过 ``params["sub"]`` 路由:
        - ``status`` / ``enable`` / ``disable``
        - ``exclude.add`` / ``exclude.remove`` / ``exclude.list``
        - ``files.allow`` / ``files.deny`` / ``files.list``

        ``enable``/``disable`` 走 ``agent_manager.recreate_agent`` (重建 sys_operation 类型);
        其他写动作通过 ``adapter.apply_sandbox_runtime_patch()`` 立即热更,
        不重建 agent.

        当 ``sandbox.type=yuanrong`` 时仅允许 ``status`` (裸 ``/sandbox`` 查看
        enabled/executor/mounts); 任意子指令一律拒绝。
        """
        params = request.params or {}
        sub = str(params.get("sub", "status")).strip().lower() or "status"
        channel_id = request.channel_id or "default"
        try:
            # 平台守卫: ``/sandbox`` 全家桶仅在 Linux 上可用。 放在 try 内部是
            # 故意的, 让 ValueError 命中下方 ``except ValueError`` 分支转成
            # ``SANDBOX_BAD_REQUEST`` 回执, 跟其它入参校验失败的处理一致。
            _require_sandbox_supported()
            endpoint = get_sandbox_endpoint()
            sandbox_type = str(endpoint.get("type") or "").strip().lower()
            if sandbox_type == "yuanrong":
                if sub != "status":
                    raise ValueError(
                        "sandbox.type=yuanrong: only /sandbox (view config) is "
                        "supported; subcommands are disabled"
                    )
                payload = build_yuanrong_sandbox_status_view()
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
            else:
                validate_sandbox_files_runtime(get_sandbox_runtime().get("files"))
                if sub == "status":
                    payload = {"runtime": get_sandbox_runtime()}
                elif sub == "enable":
                    payload = await self._handle_sandbox_enable(channel_id)
                elif sub == "disable":
                    payload = await self._handle_sandbox_disable(channel_id)
                elif sub == "exclude.add":
                    payload = await self._handle_sandbox_exclude_add(channel_id, params)
                elif sub == "exclude.remove":
                    payload = await self._handle_sandbox_exclude_remove(channel_id, params)
                elif sub == "exclude.list":
                    payload = {
                        "excluded_commands": list(
                            get_sandbox_runtime().get("excluded_commands") or []
                        )
                    }
                elif sub == "files.allow":
                    payload = await self._handle_sandbox_files_set(
                        channel_id, params, bucket="allow"
                    )
                elif sub == "files.deny":
                    payload = await self._handle_sandbox_files_set(
                        channel_id, params, bucket="deny"
                    )
                elif sub == "files.remove":
                    payload = await self._handle_sandbox_files_remove(channel_id, params)
                elif sub == "files.list":
                    payload = {"files": dict(get_sandbox_runtime().get("files") or {})}
                else:
                    raise ValueError(f"unknown sub: {sub!r}")
                self._attach_effective_sandbox_files(payload, channel_id, params)
                await self._attach_landlock_status(payload)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=payload,
                )
        except ValueError as exc:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SANDBOX_BAD_REQUEST"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.sandbox failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SANDBOX_INTERNAL"},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_sandbox_enable(self, channel_id: str) -> dict[str, Any]:
        # 1. 解析 sandbox endpoint: 优先 config.yaml::sandbox.url/type, 缺省走本地 jiuwenbox.
        # ``get_sandbox_endpoint`` 已经把 startup_mode / policy_file 的归一化值一并返回:
        # - startup_mode 缺省/非法 → "internal"
        # - policy_file 缺省 → "" (此处再回落到 DEFAULT_SANDBOX_POLICY_FILE)
        endpoint = get_sandbox_endpoint()
        url = endpoint.get("url") or "http://127.0.0.1:8321"
        sandbox_type = endpoint.get("type") or "jiuwenbox"

        # startup_mode:
        # - internal: agent-server 通过 JiuwenBoxRunner 拉起 jiuwenbox (默认行为);
        # - external: 用户自己启动 jiuwenbox (例如需要 sudo + network.mode: isolated),
        #   本侧只做健康检查, 不可达直接报错并提示如何手动启动。
        startup_mode = endpoint.get("startup_mode") or DEFAULT_SANDBOX_STARTUP_MODE

        # policy_file:
        # - 仅文件名 → 在 jiuwenbox/configs 下查找; 含路径 / 绝对路径 → 整路径使用;
        # - 未配置 → 回落到 DEFAULT_SANDBOX_POLICY_FILE (即 code-agent-policy.yaml),
        #   并在下方与 url/type 一起写回 config.yaml, 让重启后无需再走 fallback 路径。
        raw_policy = endpoint.get("policy_file") or ""
        effective_policy_file = raw_policy or DEFAULT_SANDBOX_POLICY_FILE
        policy_path = resolve_sandbox_policy_path(effective_policy_file)
        if policy_path is None:
            raise RuntimeError(
                f"sandbox.policy_file={effective_policy_file!r} 无法解析: "
                f"仅给出文件名时需能定位到 jiuwenbox/configs 目录, "
                f"否则请在 config.yaml::sandbox.policy_file 里配置绝对路径。",
            )
        if not policy_path.is_file():
            raise RuntimeError(
                f"sandbox policy 文件不存在: {policy_path} "
                f"(原始配置 sandbox.policy_file="
                f"{raw_policy or f'<default:{DEFAULT_SANDBOX_POLICY_FILE}>'!r})",
            )

        # 2. 解析 host:port 并 (internal 模式下) 完成端口分配。
        # external 模式: 直接用配置里的 url, 由用户保证 jiuwenbox 监听在此处。
        # internal 模式: 期望端口被占就换一个随机空闲端口, 不去探测占用方是谁。
        host, preferred_port = self._parse_sandbox_host_port(url)
        if startup_mode == "internal":
            port = self._allocate_internal_jiuwenbox_port(host, preferred_port)
            if port != preferred_port:
                # 端口换过, 同步刷新 url 以便后续落盘 / 透传给前端
                url = f"http://{host}:{port}"
                logger.info(
                    "[command.sandbox] jiuwenbox effective url changed to %s "
                    "(preferred port %d was busy)",
                    url,
                    preferred_port,
                )
        else:
            port = preferred_port

        # 3. 启动 / 健康检查本地 jiuwenbox; 失败直接报错
        ok = await self._jiuwenbox_runner.ensure_running(
            host=host,
            port=port,
            startup_mode=startup_mode,
            policy_path=policy_path,
        )
        if not ok:
            if startup_mode == "external":
                raise RuntimeError(
                    f"jiuwenbox 未在 {host}:{port} 监听 (sandbox.startup_mode=external); "
                    f"请在另一终端先启动 jiuwenbox-server, 例如:\n"
                    f"  sudo -E .venv/bin/python -m uvicorn jiuwenbox.server.app:app "
                    f"--host {host} --port {port}\n"
                    f"  (JIUWENBOX_POLICY_PATH={policy_path})"
                )
            stderr_tail = self._jiuwenbox_runner.get_stderr_tail(20)
            hint = "\n--- jiuwenbox stderr (tail) ---\n" + stderr_tail if stderr_tail else (
                " (no stderr captured; jiuwenbox / uvicorn 可能未安装)"
            )
            raise RuntimeError(
                f"jiuwenbox 启动或健康检查失败 ({host}:{port}){hint}"
            )

        # 4. 把 endpoint 写回 config.yaml, 保证 agent 重建 / agent-server 重启后能直接读到。
        # url 此时已是端口分配后的最终值; startup_mode / policy_file / preserve_file_sharing_mode 一并落盘。
        preserve_mode = resolve_preserve_file_sharing_mode_default()
        try:
            update_sandbox_endpoint(
                url,
                sandbox_type,
                startup_mode=startup_mode,
                policy_file=effective_policy_file,
                preserve_file_sharing_mode=preserve_mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[command.sandbox] persist sandbox endpoint failed: %s", exc)

        runtime = update_sandbox_runtime({"enabled": True})
        await self._agent_manager.recreate_agent(channel_id, immediate=True)

        return {
            "runtime": runtime,
            "endpoint": {
                "url": url,
                "type": sandbox_type,
                "preserve_file_sharing_mode": preserve_mode,
                "startup_mode": startup_mode,
                "policy_file": effective_policy_file,
            },
            "jiuwenbox": {
                "host": host,
                "port": port,
                "ready": True,
                "startup_mode": startup_mode,
                "policy_path": str(policy_path),
            },
            "agent_recreated": True
        }

    async def _handle_sandbox_disable(self, channel_id: str) -> dict[str, Any]:
        runtime = update_sandbox_runtime({"enabled": False})
        await self._agent_manager.recreate_agent(channel_id, immediate=True)

        # 记录关闭前的端点用于回执 (external 模式下 runner 没拥有进程, 会是 None)。
        owned_endpoint = self._jiuwenbox_runner.get_owned_endpoint()
        jiuwenbox_stopped = False
        if owned_endpoint is not None:
            try:
                await self._jiuwenbox_runner.stop()
                jiuwenbox_stopped = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] /sandbox disable: jiuwenbox stop failed: %s",
                    exc,
                )
        else:
            logger.debug(
                "[AgentWebSocketServer] /sandbox disable: no owned jiuwenbox to stop "
                "(external startup_mode or never started)"
            )

        payload: dict[str, Any] = {
            "runtime": runtime,
            "agent_recreated": True,
            "jiuwenbox_stopped": jiuwenbox_stopped,
        }
        if owned_endpoint is not None:
            host, port = owned_endpoint
            payload["jiuwenbox"] = {"host": host, "port": port, "ready": False}
        return payload

    async def _handle_sandbox_exclude_add(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("pattern is required")
        current = get_sandbox_runtime()
        patterns = list(current.get("excluded_commands") or [])
        if pattern in patterns:
            raise ValueError(
                f"excluded_commands already contains {pattern!r}; "
                "use a different pattern or remove it first"
            )
        patterns.append(pattern)
        runtime = update_sandbox_runtime({"excluded_commands": patterns})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=False)
        return {"runtime": runtime}

    async def _handle_sandbox_exclude_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        pattern = str(params.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("pattern is required")
        current = get_sandbox_runtime()
        existing = list(current.get("excluded_commands") or [])
        if pattern not in existing:
            raise ValueError(
                f"excluded_commands does not contain {pattern!r}; "
                "nothing to remove"
            )
        patterns = [p for p in existing if p != pattern]
        runtime = update_sandbox_runtime({"excluded_commands": patterns})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=False)
        return {"runtime": runtime}

    def _dry_run_files_policy(
        self,
        channel_id: str,
        params: dict[str, Any],
        files: dict[str, Any],
    ) -> None:
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        try:
            build_filesystem_policy(
                files,
                project_dir=project_dir,
                is_code_agent=is_code_agent,
                startup_mode=get_sandbox_startup_mode(),
            )
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc

    async def _handle_sandbox_files_set(
        self, channel_id: str, params: dict[str, Any], *, bucket: str
    ) -> dict[str, Any]:
        _reject_extra_sandbox_files_params(params)
        path = str(params.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        # 把 path 展开成 absolute resolved 形式, 让 ``./foo`` / ``~/data`` /
        # 含 ``..`` 之类写法在入口就被归一化到稳定路径, 避免后续 stat / 入库
        # / 比较行为依赖 jiuwenswarm server 当前 cwd; 见
        # :func:`_canonicalize_sandbox_files_path` 的文档说明。
        canonical = _canonicalize_sandbox_files_path(path)
        if canonical != path:
            logger.info(
                "[sandbox] files %s: canonicalize path %r -> %r",
                bucket, path, canonical,
            )
            path = canonical
        # 拒绝把"自动配置且不可变"的路径 (intrinsic AGENT.md / HEARTBEAT.md /...
        # / daily_memory / 项目目录 / jiuwenswarm config.yaml) 再次写进
        # config.yaml::sandbox.files。 它们由 sysop_builder 在每次
        # build_filesystem_policy 时按需重建; 让用户能 add 只会污染配置, 而且
        # 若一个路径同时在 auto-allow 和用户-deny 里 (反之亦然), 实际行为难以
        # 预期, 不如直接在入口阻断。``params`` 透传给 ``_resolve_active_
        # project_dir`` 以便 TUI 通过 ``trusted_dirs`` / ``cwd`` 显式声明的
        # 项目目录也参与 auto 路径的判定。
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        match = find_auto_managed_match(
            path,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
        if match is not None:
            matched_bucket, canonical = match
            raise ValueError(
                f"path is auto-managed (always in {matched_bucket}): {canonical}; "
                f"cannot add via /sandbox files {bucket}"
            )
        current = get_sandbox_runtime()
        files = dict(current.get("files") or {})
        files.setdefault("allow", [])
        files.setdefault("deny", [])
        # 1) 同 bucket 内已经存在等价条目 → 直接报错, 不做 "先删后加" 的隐式覆盖。
        target_list: list[Any] = list(files.get(bucket) or [])
        for existing in target_list:
            if _file_entry_matches_path(existing, path):
                raise ValueError(
                    f"sandbox.files.{bucket} already contains {path!r}; "
                    f"use `/sandbox files remove {path}` first if you want to change it"
                )
        # 2) 反方向 bucket 已经登记了同一条 → allow / deny 在 Landlock 层语义直接
        #    冲突, 拒绝。 用户得先把它从对侧 ``remove`` 掉再加, 显式表达 "我要
        #    切换权限方向" 的意图。
        opposite_bucket = "deny" if bucket == "allow" else "allow"
        for existing in files.get(opposite_bucket) or []:
            if _file_entry_matches_path(existing, path):
                raise ValueError(
                    f"sandbox.files.{opposite_bucket} already contains {path!r}; "
                    f"cannot add the same path to {bucket}. "
                    f"`/sandbox files remove {path}` first if you want to flip it"
                )
        nested_error = find_nested_files_conflict(path, bucket, files)
        if nested_error is not None:
            raise ValueError(nested_error)
        entry: dict[str, Any] = {"path": path}
        target_list.append(entry)
        files[bucket] = target_list
        # 在写盘前做一次 dry-run, 防止后续 build_filesystem_policy 抛错时,
        # yaml 已经被更新成一份永远 build 不出 policy 的中间态 (见
        # :meth:`_dry_run_files_policy` 的文档说明)。
        self._dry_run_files_policy(channel_id, params, files)
        runtime = update_sandbox_runtime({"files": files})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=True)
        return {"runtime": runtime}

    async def _handle_sandbox_files_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _reject_extra_sandbox_files_params(params)
        path = str(params.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        # 与 _handle_sandbox_files_set 保持同一份 canonicalize, 让 ``remove
        # ./foo`` 能命中以 absolute 形式入库的 entry; 兼容旧 yaml 残留写法的
        # 兜底由 :func:`_file_entry_matches_path` 双侧 canonicalize 比较负责。
        canonical = _canonicalize_sandbox_files_path(path)
        if canonical != path:
            logger.info(
                "[sandbox] files remove: canonicalize path %r -> %r",
                path, canonical,
            )
            path = canonical
        # 同 _handle_sandbox_files_set: auto-managed 条目由 sysop_builder 在
        # 每次 build_filesystem_policy 时重建, 用户不能也不必通过 /sandbox 删除
        # 它们。如果旧版本 config.yaml 里残留了这些路径, 提示用户直接改 yaml,
        # 而不是让 /sandbox 默默地把同一个 auto-managed 名字从用户配置里抹掉
        # ——后者会让用户误以为他/她真的把 sandbox 自动条目摘掉了。
        project_dir = self._resolve_active_project_dir(channel_id, params)
        is_code_agent = self._resolve_active_is_code_agent(channel_id)
        match = find_auto_managed_match(
            path,
            project_dir=project_dir,
            is_code_agent=is_code_agent,
            startup_mode=get_sandbox_startup_mode(),
        )
        if match is not None:
            matched_bucket, canonical = match
            raise ValueError(
                f"path is auto-managed (always in {matched_bucket}): {canonical}; "
                f"cannot remove via /sandbox files remove"
            )
        current = get_sandbox_runtime()
        files = dict(current.get("files") or {})
        files.setdefault("allow", [])
        files.setdefault("deny", [])
        matched_buckets: list[str] = []
        for bucket in ("allow", "deny"):
            kept: list[Any] = []
            removed = False
            for entry in files.get(bucket) or []:
                if _file_entry_matches_path(entry, path):
                    removed = True
                    continue
                kept.append(entry)
            if removed:
                matched_buckets.append(bucket)
                files[bucket] = kept
        if not matched_buckets:
            raise ValueError(
                f"sandbox.files has no entry for {path!r}; nothing to remove"
            )
        # 与 _handle_sandbox_files_set 对齐: 在写盘前 dry-run, 避免 build 失败
        # 时 yaml 已被写成 build 不出 policy 的死局 (见 :meth:`_dry_run_files
        # _policy` 的文档说明)。
        self._dry_run_files_policy(channel_id, params, files)
        runtime = update_sandbox_runtime({"files": files})
        await self._apply_sandbox_runtime_patch(channel_id, runtime, files_changed=True)
        return {"runtime": runtime}

    def _resolve_active_project_dir(
        self, channel_id: str, params: dict[str, Any] | None = None
    ) -> str | None:
        """Resolve the user project dir for the current ``/sandbox`` view.

        Lookup order, falling through on empty/missing:

        1. ``params["project_dir"]`` -- stable client project identity.
        2. ``adapter._project_dir`` / ``adapter._instance_overrides``.
        3. ``params["cwd"]`` -- legacy/dynamic fallback.
        4. ``params["trusted_dirs"][0]`` -- final compatibility fallback.

        Returns ``None`` only when none of the above yield a usable path; we
        deliberately do NOT fall back to ``Path.cwd()`` of the agent-server
        process because that's typically ``~/.jiuwenswarm`` and would
        mislabel the displayed ``files.allow_write`` entry.
        """
        if isinstance(params, dict):
            project_dir = params.get("project_dir")
            if isinstance(project_dir, str) and project_dir.strip():
                return project_dir.strip()
        try:
            agent = self._agent_manager.get_agent_nowait(channel_id)
        except Exception as exc:
            logger.info("[command.sandbox] get_agent_nowait failed: %s", exc)
            return None
        adapter = self._resolve_adapter(agent)
        if adapter is None:
            return None
        direct = getattr(adapter, "_project_dir", None)
        if direct:
            return str(direct)
        overrides = getattr(adapter, "_instance_overrides", None)
        if isinstance(overrides, dict):
            value = overrides.get("project_dir")
            if value:
                return str(value)
        if isinstance(params, dict):
            cwd_value = params.get("cwd")
            if isinstance(cwd_value, str) and cwd_value.strip():
                return cwd_value.strip()
            trusted_dirs = params.get("trusted_dirs")
            if isinstance(trusted_dirs, (list, tuple)) and trusted_dirs:
                first = str(trusted_dirs[0]).strip()
                if first:
                    return first
        return None

    def _resolve_active_is_code_agent(self, channel_id: str) -> bool:
        """Look up whether ``channel_id``'s adapter is the code-agent flavor.

        Mirrors :meth:`_resolve_active_project_dir`'s adapter lookup so the
        three sandbox call sites (``_dry_run_files_policy``,
        ``_handle_sandbox_files_set`` / ``_remove``'s ``find_auto_managed_
        match``, ``_attach_effective_sandbox_files``'s
        ``list_effective_sandbox_files``) all hand the same flag into
        ``sysop_builder``. Without this, the dry-run / display side would
        always assume non-code-agent and mismatch the actual mount layout
        a Code adapter produces at sandbox-start time (project_dir vs
        ``get_agent_workspace_dir``).

        Returns ``False`` on any failure path (no agent, no adapter, attr
        absent) — that matches the base class default and keeps the dry-run
        / display strictly aligned with what :class:`JiuWenSwarmDeepAdapter`
        emits when ``_is_code_agent`` was never set.
        """
        try:
            agent = self._agent_manager.get_agent_nowait(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[command.sandbox] is_code_agent lookup: get_agent_nowait failed: %s", exc)
            return False
        adapter = self._resolve_adapter(agent)
        if adapter is None:
            return False
        return bool(getattr(adapter, "_is_code_agent", False))

    @staticmethod
    def _effective_files_from_adapter(adapter: Any) -> dict[str, list[dict[str, str]]] | None:
        """Read effective sandbox file mounts from the adapter's active sysop card."""
        card = getattr(adapter, "_sys_operation_card", None)
        if card is None:
            return None
        gateway_config = getattr(card, "gateway_config", None)
        launcher = getattr(gateway_config, "launcher_config", None) if gateway_config else None
        extra_params = getattr(launcher, "extra_params", None) if launcher else None
        if not isinstance(extra_params, dict):
            return None
        policy = extra_params.get("policy")
        if not isinstance(policy, dict):
            return None
        return effective_files_from_policy(policy)

    def _attach_effective_sandbox_files(
        self,
        payload: dict[str, Any],
        channel_id: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Inject ``effective_files`` into the ``/sandbox`` response payload.

        Prefer the filesystem policy cached on the active adapter's sysop card
        (same payload jiuwenbox uses at exec time). Fall back to a fresh build
        when no matching agent/sysop exists yet.
        """
        try:
            project_dir = self._resolve_active_project_dir(channel_id, params)
            adapter = None
            try:
                agent = self._agent_manager.get_agent_nowait(
                    channel_id,
                    project_dir=project_dir,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[command.sandbox] get_agent_nowait failed: %s", exc)
                agent = None
            if agent is not None:
                adapter = self._resolve_adapter(agent)
            if adapter is not None:
                adapter_project_dir = getattr(adapter, "_project_dir", None)
                if (
                    project_dir
                    and adapter_project_dir
                    and str(adapter_project_dir) != str(project_dir)
                ):
                    logger.warning(
                        "[command.sandbox] project_dir mismatch for effective_files: "
                        "client=%r adapter=%r",
                        project_dir,
                        adapter_project_dir,
                    )
                cached = self._effective_files_from_adapter(adapter)
                if cached is not None:
                    payload["effective_files"] = cached
                    return

            files_runtime: dict[str, Any] | None = None
            runtime = payload.get("runtime")
            if isinstance(runtime, dict):
                rt_files = runtime.get("files")
                if isinstance(rt_files, dict):
                    files_runtime = rt_files
            if files_runtime is None:
                files_in_payload = payload.get("files")
                if isinstance(files_in_payload, dict):
                    files_runtime = files_in_payload
            if files_runtime is None:
                files_runtime = get_sandbox_runtime().get("files") or {}
            is_code_agent = self._resolve_active_is_code_agent(channel_id)
            payload["effective_files"] = list_effective_sandbox_files(
                files_runtime,
                project_dir=project_dir,
                is_code_agent=is_code_agent,
                startup_mode=get_sandbox_startup_mode(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[command.sandbox] attach effective_files failed: %s", exc)

    @staticmethod
    def _read_landlock_compatibility(policy_path: Path | None) -> str:
        if policy_path is None or not policy_path.is_file():
            return "best_effort"
        try:
            import yaml

            data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                landlock = data.get("landlock")
                if isinstance(landlock, dict):
                    compat = landlock.get("compatibility")
                    if isinstance(compat, str) and compat.strip():
                        return compat.strip()
        except Exception as exc:
            logger.debug("[command.sandbox] read landlock compatibility failed: %s", exc)
        return "best_effort"

    async def _attach_landlock_status(self, payload: dict[str, Any]) -> None:
        """Attach jiuwenbox Landlock capability summary to sandbox responses."""
        try:
            endpoint = get_sandbox_endpoint()
            jb = payload.get("jiuwenbox")
            if isinstance(jb, dict) and jb.get("host") and jb.get("port"):
                host = str(jb["host"])
                port = int(jb["port"])
            else:
                url = endpoint.get("url") or "http://127.0.0.1:8321"
                host, port = self._parse_sandbox_host_port(url)

            health = await self._jiuwenbox_runner.fetch_health(host, port)
            landlock_supported = bool(health.get("landlock_supported")) if health else False

            policy_file = endpoint.get("policy_file") or DEFAULT_SANDBOX_POLICY_FILE
            policy_path = resolve_sandbox_policy_path(policy_file)
            compatibility = self._read_landlock_compatibility(policy_path)

            payload["landlock"] = {
                "supported": landlock_supported,
                "compatibility": compatibility,
            }
        except Exception as exc:
            logger.warning("[command.sandbox] attach landlock status failed: %s", exc)

    async def _apply_sandbox_runtime_patch(
        self, channel_id: str, runtime: dict[str, Any], *, files_changed: bool
    ) -> None:
        agent = self._agent_manager.get_agent_nowait(channel_id)
        adapter = self._resolve_adapter(agent)
        if adapter is None or not hasattr(adapter, "apply_sandbox_runtime_patch"):
            return
        try:
            await adapter.apply_sandbox_runtime_patch(runtime, files_changed=files_changed)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            logger.warning("[command.sandbox] apply_sandbox_runtime_patch failed: %s", exc)

    @staticmethod
    def _resolve_adapter(agent: Any) -> Any:
        """从 JiuwenSwarm 中提取底层 Deep/Code Adapter (持 _sys_operation_card 的实例)."""
        if agent is None:
            return None
        for attr in ("_adapter", "adapter", "_active_adapter"):
            inner = getattr(agent, attr, None)
            if inner is not None and hasattr(inner, "apply_sandbox_runtime_patch"):
                return inner
        # 兜底: agent 本身有相关方法
        if hasattr(agent, "apply_sandbox_runtime_patch"):
            return agent
        return None

    @staticmethod
    def resolve_adapter(agent: Any) -> Any:
        """Public wrapper for :meth:`_resolve_adapter` (避开 protected-access)."""
        return AgentWebSocketServer._resolve_adapter(agent)

    @staticmethod
    def _parse_sandbox_host_port(url: str) -> tuple[str, int]:
        """从 sandbox url 解析 host:port; 默认 127.0.0.1:8321."""
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8321
        except Exception:
            host, port = "127.0.0.1", 8321
        return host, int(port)

    @staticmethod
    def _is_tcp_port_bindable(host: str, port: int) -> bool:
        """``True`` 表示当前能在 ``host:port`` 上 ``bind`` 成功 (即没有被占用)。

        不去探测 ``/health`` 之类应用层信息——只看四层占用情况, 谁占着、占着的
        是不是 jiuwenbox 都不关心。
        """
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                sock.bind((host, port))
            except OSError:
                return False
            return True
        finally:
            sock.close()

    @staticmethod
    def _pick_free_tcp_port(host: str) -> int:
        """让内核挑一个空闲端口 (``bind`` 到 0); 仅用于绑定测试, 不会真正监听。

        存在 TOCTOU 风险 (返回后端口可能立即被别人抢), 但接下来 uvicorn 起来
        通常足够快; 即便撞上, uvicorn 自己会因 EADDRINUSE 失败, 上游再报错。
        """
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def _allocate_internal_jiuwenbox_port(
        self,
        host: str,
        preferred_port: int,
    ) -> int:
        """internal 模式下确定 jiuwenbox 实际监听端口。

        - 若本 runner 已经在 ``host:preferred_port`` 上拥有一个仍在跑的 jiuwenbox,
          直接复用 (避免重复 spawn);
        - 否则若 ``preferred_port`` 当前无人占用, 用之;
        - 再否则让内核挑一个空闲端口返回。
        """
        if self._jiuwenbox_runner.is_owned_listener(host, preferred_port):
            return preferred_port
        if self._is_tcp_port_bindable(host, preferred_port):
            return preferred_port
        new_port = self._pick_free_tcp_port(host)
        logger.warning(
            "[command.sandbox] preferred port %s:%d is busy; "
            "allocating fresh port %d for new jiuwenbox instance",
            host,
            preferred_port,
            new_port,
        )
        return new_port

    async def _handle_command_resume(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            query = params.get("query")
            session_id = query if isinstance(query, str) and query.strip() else "sess_mock_resume"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "query": query if isinstance(query, str) else "",
                    "resumed": True,
                    "preview": "Mock resumed conversation",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.resume failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_session(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "sess_mock"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "remote_url": f"https://example.com/session/{session_id}",
                    "qr_text": f"session:{session_id}",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.session failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_command_status(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            action = str(params.get("action", "overview")).strip().lower()

            if action == "usage":
                sessions, total = get_all_sessions_metadata(limit=500, offset=0)
                messages_total = sum(s.get("message_count", 0) for s in sessions)
                model_counts: dict[str, int] = {}
                for s in sessions:
                    mode = str(s.get("mode", "unknown"))
                    model_counts[mode] = model_counts.get(mode, 0) + 1
                active_days_set: set[str] = set()
                longest_hours = 0.0
                for s in sessions:
                    created = s.get("created_at", 0)
                    last = s.get("last_message_at", 0)
                    if created:
                        try:
                            day_str = _dt.datetime.fromtimestamp(
                                created, tz=_dt.timezone.utc
                            ).strftime("%Y-%m-%d")
                            active_days_set.add(day_str)
                        except Exception:  # noqa: BLE001
                            pass
                    if created and last:
                        longest_hours = max(longest_hours, (last - created) / 3600)

                models_used = [{"name": k, "count": v} for k, v in sorted(model_counts.items(), key=lambda x: -x[1])]
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "sessions_total": total,
                        "messages_total": messages_total,
                        "models_used": models_used,
                        "active_days": len(active_days_set),
                        "longest_session_hours": round(longest_hours, 1),
                    },
                )
            elif action == "config":
                config_path = str(get_config_file())
                settings_sources: list[str] = []
                config_dir = os.getenv("JIUWENSWARM_CONFIG_DIR")
                if config_dir:
                    settings_sources.append(f"env:JIUWENSWARM_CONFIG_DIR={config_dir}")
                settings_sources.append(config_path)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "config_path": config_path,
                        "settings_sources": settings_sources,
                    },
                )
            else:
                # overview (default)
                config = get_config()
                session_id = request.session_id or ""
                default_models = get_default_models(config)
                active_entry = default_models[0] if default_models else {}
                mcc = active_entry.get("model_client_config", {})
                model_name = str(mcc.get("model_name", "") or config.get("model", ""))
                provider = str(mcc.get("client_provider", "") or config.get("model_provider", ""))
                api_base = str(mcc.get("api_base", "") or config.get("api_base", ""))

                mcp_servers = get_mcp_servers()
                mcp_summary = [
                    {
                        "name": str(s.get("name", "unknown")),
                        "enabled": bool(s.get("enabled", True)),
                        "transport": str(s.get("transport", "unknown")),
                    }
                    for s in mcp_servers
                    if isinstance(s, dict)
                ]

                config_path = str(get_config_file())
                settings_sources: list[str] = []
                config_dir = os.getenv("JIUWENSWARM_CONFIG_DIR")
                if config_dir:
                    settings_sources.append(f"env:JIUWENSWARM_CONFIG_DIR={config_dir}")
                settings_sources.append(config_path)

                # Memory diagnostics — use the actual workspace dir (trusted_dir or cwd),
                # same as ProjectMemoryRail, so we detect JIUWESWARM.md where /init creates it.
                params = request.params or {}
                workspace_dir = str(params.get("cwd", "") or os.getcwd())
                trusted_dirs = params.get("trusted_dirs")
                if isinstance(trusted_dirs, list) and trusted_dirs:
                    workspace_dir = str(trusted_dirs[0])
                try:
                    from jiuwenswarm.agents.harness.common.rails.project_memory import (
                        clear_project_memory_cache,
                        discover_and_load_memory_files,
                        get_large_memory_files,
                    )
                    clear_project_memory_cache(workspace_dir)
                    project_files = discover_and_load_memory_files(
                        workspace=workspace_dir, target_path=workspace_dir,
                    )
                    memory_warnings = get_large_memory_files(project_files)
                    logger.info(
                        "[AgentWebSocketServer] memory diagnostics: "
                        "workspace_dir=%s, files=%d, warnings=%d",
                        workspace_dir, len(project_files), len(memory_warnings),
                    )
                except Exception as exc:
                    logger.warning(
                        "[AgentWebSocketServer] memory diagnostics failed: "
                        "workspace_dir=%s, error=%s",
                        workspace_dir, exc,
                    )
                    memory_warnings = []

                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "version": __version__,
                        "session_id": session_id,
                        "cwd": str(params.get("cwd", "") or os.getcwd()),
                        "model": model_name,
                        "provider": provider,
                        "api_base": api_base,
                        "connection_status": "connected",
                        "mcp_servers": mcp_summary,
                        "config_path": config_path,
                        "settings_sources": settings_sources,
                        "memory_warnings": memory_warnings,
                    },
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.status failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_browser_runtime_restart(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from openjiuwen.harness.tools import browser_move

            reset_runtimes = await _reset_requested_browser_runtime_if_available(
                browser_move,
                request.params or {},
            )
            result = browser_move.restart_local_browser_runtime_server()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "result": result,
                    "reset_runtimes": reset_runtimes,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] browser.runtime_restart failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            workspace_dir = request.params.get("workspace_dir") if request.params else None
            service = AgentConfigService(workspace_dir)
            agents = service.list_agents()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"agents": [dataclass_asdict(a) for a in agents]},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            params = request.params or {}
            name = params.get("name", "")
            workspace_dir = params.get("workspace_dir")
            service = AgentConfigService(workspace_dir)
            agent = service.get_agent(name)
            if agent is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": f"Agent 不存在: {name}"},
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"agent": dataclass_asdict(agent)},
                )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.get failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _generate_agent_with_llm(
        self, name: str, description: str
    ) -> tuple[str, str] | None:
        """调用 LLM 生成 agent 的 whenToUse 和 systemPrompt。

        Returns:
            (when_to_use, system_prompt) 或 None（生成失败时回退到模板）
        """
        model = self._resolve_model(None)
        if model is None:
            logger.warning("[agents.create] no model available for LLM generation")
            return None

        from openjiuwen.core.foundation.llm.schema.message import UserMessage

        full_prompt = f"""{_AGENT_CREATION_SYSTEM_PROMPT}

---
请为以下 agent 生成配置：

名称: {name}
描述: {description}

返回 JSON 对象，包含 whenToUse 和 systemPrompt 两个字段。不要返回其他内容。"""

        try:
            result = await model.invoke(
                [UserMessage(content=full_prompt)],
                max_tokens=2000,
                temperature=0.3,
            )
            text = getattr(result, "content", None) or str(result)
        except Exception:
            logger.exception("[agents.create] LLM generation failed")
            return None

        # 解析 JSON 响应
        import re as _re

        import json as _json
        try:
            data = _json.loads(text.strip())
        except _json.JSONDecodeError:
            match = _re.search(r"\{[\s\S]*\}", text)
            if not match:
                logger.warning("[agents.create] no JSON found in LLM response: %s", text[:200])
                return None
            try:
                data = _json.loads(match.group(0))
            except _json.JSONDecodeError:
                logger.warning("[agents.create] JSON parse failed: %s", text[:200])
                return None

        when_to_use = (data.get("whenToUse") or "").strip()
        system_prompt = (data.get("systemPrompt") or "").strip()

        if not when_to_use or not system_prompt:
            logger.warning("[agents.create] incomplete LLM response: %s", data)
            return None

        return when_to_use, system_prompt

    async def _handle_agents_create(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService, CreateAgentParams

        try:
            params = dict(request.params or {})
            workspace_dir = params.pop("workspace_dir", None)
            generate = params.pop("generate", True)

            # LLM 生成 when_to_use 和 prompt（失败时回退到请求中的模板值）
            generated = False
            if generate:
                name = params.get("name", "")
                description = params.get("description", "")
                if name and description:
                    llm_result = await self._generate_agent_with_llm(name, description)
                    if llm_result:
                        params["when_to_use"] = llm_result[0]
                        params["prompt"] = llm_result[1]
                        generated = True

            p = CreateAgentParams(**{k: v for k, v in params.items()
                                      if k in CreateAgentParams.__dataclass_fields__})
            service = AgentConfigService(workspace_dir)
            agent = service.create_agent(p)
            # 自动在 config.yaml 中启用新创建的 agent
            applied = True
            reload_error = ""
            try:
                upsert_subagent_in_config(agent.name, enabled=True)
                await self._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning("[AgentWebSocketServer] agents.create reload failed: %s", reload_exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "agent": dataclass_asdict(agent),
                    "generated": generated,
                    "applied": applied,
                    "reload_error": reload_error or None,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.create failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_update(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from dataclasses import asdict as dataclass_asdict
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService, UpdateAgentParams

        try:
            params = dict(request.params or {})
            name = params.pop("name", "")
            workspace_dir = params.pop("workspace_dir", None)
            generate = params.pop("generate", False)

            # LLM 生成 when_to_use 和 prompt（默认不生成，需显式 --generate）
            generated = False
            if generate and name and params.get("description"):
                llm_result = await self._generate_agent_with_llm(name, params["description"])
                if llm_result:
                    params["when_to_use"] = llm_result[0]
                    params["prompt"] = llm_result[1]
                    generated = True

            p = UpdateAgentParams(**{k: v for k, v in params.items()
                                      if k in UpdateAgentParams.__dataclass_fields__})
            service = AgentConfigService(workspace_dir)
            agent = service.update_agent(name, p)

            # 更新后热加载（对齐 create/delete 的模式）
            applied = True
            reload_error = ""
            try:
                await self._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning("[AgentWebSocketServer] agents.update reload failed: %s", reload_exc)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "agent": dataclass_asdict(agent),
                    "generated": generated,
                    "applied": applied,
                    "reload_error": reload_error or None,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.update failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            params = request.params or {}
            name = params.get("name", "")
            workspace_dir = params.get("workspace_dir")
            service = AgentConfigService(workspace_dir)
            ok = service.delete_agent(name)
            # 自动从 config.yaml 中移除被删除的 agent
            applied = True
            reload_error = ""
            try:
                remove_subagent_from_config(name)
                await self._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning("[AgentWebSocketServer] agents.delete reload failed: %s", reload_exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"ok": ok, "applied": applied, "reload_error": reload_error or None},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_set_enabled(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        enabled: bool
    ) -> None:
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        action = "enable" if enabled else "disable"
        try:
            params = request.params or {}
            name = str(params.get("name", "")).strip()
            if not name:
                raise ValueError("agent name is required")
            workspace_dir = params.get("workspace_dir")
            service = AgentConfigService(workspace_dir)
            agent = service.get_agent(name)
            if agent is None:
                raise ValueError(f"Agent 不存在: {name}")
            if agent.source == "builtin":
                raise ValueError(f"不能启用/禁用内置 agent: {name}")

            upsert_subagent_in_config(name, enabled=enabled)
            applied = True
            reload_error = ""
            try:
                await self._agent_manager.reload_agents_config(get_config(), None)
            except Exception as reload_exc:
                applied = False
                reload_error = str(reload_exc)
                logger.warning("[AgentWebSocketServer] agents.%s reload failed: %s", action, reload_exc)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "name": name,
                    "enabled": enabled,
                    "applied": applied,
                    "reload_error": reload_error or None,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.%s failed: %s", action, e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agents_tools_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

        try:
            params = request.params or {}
            workspace_dir = params.get("workspace_dir")
            service = AgentConfigService(workspace_dir)
            result = service.list_available_tools()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result,
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agents.tools_list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_config_cache_clear(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from jiuwenswarm.agents.harness.common.memory.config import clear_config_cache

            clear_config_cache()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"cleared": True},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] config.cache_clear failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agent_prewarm_sync(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Reconcile background prewarming for the Gateway's live channels."""
        params = request.params if isinstance(request.params, dict) else {}
        raw_channels = params.get("enabled_channels")
        if not isinstance(raw_channels, list):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "enabled_channels must be a list", "code": "BAD_REQUEST"},
            )
        else:
            stats = await self._agent_manager.sync_prewarm_channels(
                [str(channel) for channel in raw_channels],
                config=params.get("config"),
                env=params.get("env"),
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=stats,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_agent_reload_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            config_payload = params.get("config")
            env_overrides = params.get("env")
            target_channel_id = str(params.get("target_channel_id") or "").strip() or None
            target_session_id = str(params.get("target_session_id") or "").strip() or None
            raw_reload_scopes = params.get("reload_scopes")
            reload_scopes = {
                str(scope)
                for scope in raw_reload_scopes
                if isinstance(scope, str) and scope
            } if isinstance(raw_reload_scopes, list) else set()

            reload_kwargs = {}
            if target_channel_id:
                reload_kwargs["target_channel_id"] = target_channel_id
            if target_session_id:
                reload_kwargs["target_session_id"] = target_session_id
            if reload_scopes:
                reload_kwargs["reload_scopes"] = reload_scopes
            agent_reload_scopes = {"model", "team", "permissions", "agent_runtime"}
            should_reload_agents = not reload_scopes or bool(reload_scopes & agent_reload_scopes)

            # 模型配置变了就重探图像模态：同一个 (api_base, model_name) 背后可能已换
            # 端点 / 密钥 / 后端，旧结论不能留。跑在后台任务里——探针每个最多 5s，不该
            # 把 reload 响应拖在这里；这个 loop 活到进程结束，结论一定能落进缓存。
            should_refresh_image_modality = not reload_scopes or "model" in reload_scopes
            if should_refresh_image_modality:
                from jiuwenswarm.server.runtime.image_modality_warmup import (
                    refresh_image_modality_cache,
                )

                # 上一轮还没探完就又改了配置：旧结论已经作废，直接取消。
                previous_task = self._image_modality_refresh_task
                if previous_task is not None and not previous_task.done():
                    previous_task.cancel()
                self._image_modality_refresh_task = asyncio.create_task(
                    refresh_image_modality_cache(
                        get_config(),
                        reason="model config change",
                    )
                )
            # 模型配置变更时同步刷新本进程（AgentServer）的 Zen 免费模型缓存
            # （与上方 image modality 刷新同一 model scope）。Gateway 进程在
            # config.set/config.save_all 时会 warm，但两进程缓存独立：若本进程
            # 启动时免费模型开关关闭（后台重试循环已退出），之后经 web 打开开关，
            # 本进程缓存会一直为空，免费模型解析将静默回退默认模型。放后台任务
            # 执行——warm 自带超时且失败后自动调度后台重试，不阻塞 reload 响应。
            if should_refresh_image_modality:
                from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models

                asyncio.create_task(
                    warm_zen_free_models(reason="agent.reload_config")
                )
            if should_reload_agents:
                await self._agent_manager.reload_agents_config(
                    config_payload,
                    env_overrides,
                    **reload_kwargs,
                )
                try:
                    from jiuwenswarm.agents.harness.team import (
                        stop_all_paused_team_session_runtimes_across_managers,
                    )

                    stopped = await stop_all_paused_team_session_runtimes_across_managers(
                        reason="agent.reload_config: ",
                    )
                    if stopped:
                        logger.info(
                            "[AgentWebSocketServer] stopped paused team runtimes after agent.reload_config: "
                            "count=%s request_id=%s reload_scopes=%s",
                            stopped,
                            request.request_id,
                            sorted(reload_scopes),
                        )
                except Exception as exc:  # noqa: BLE001 - cleanup must not reject config reload
                    logger.warning(
                        "[AgentWebSocketServer] failed to stop paused team runtimes after agent.reload_config: %s",
                        exc,
                    )

            # Hot-reload ProactiveEngine config if available
            should_reload_proactive = not reload_scopes or bool(reload_scopes & {"model", "proactive", "agent_runtime"})
            if self._proactive_engine is not None and should_reload_proactive:
                cfg = get_config()
                proactive_cfg = cfg.get("proactive_recommendation", {})
                self._proactive_engine.reload_config(proactive_cfg)
                # 重建 proactive agent——它启动时建一次，模型配置固化在实例里。
                # 用户改模型后主 agent 会热更新，但 proactive agent 不在主 agent
                # 链路里，不重建会继续用旧模型（可能已失效/欠费）。
                try:
                    from jiuwenswarm.server.runtime.proactive_adapter import build_proactive_agent
                    self._proactive_engine.rebuild_proactive_agent(build_proactive_agent)
                except Exception as exc:
                    logger.warning("[AgentWebSocketServer] proactive agent rebuild failed: %s", exc)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"reloaded": True},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] agent.reload_config failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_extensions_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """获取所有 Rail 扩展列表."""
        try:
            manager = get_rail_manager()
            extensions = manager.list_extensions()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"extensions": extensions},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_extensions_import(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """导入新的 Rail 扩展（文件夹结构）."""
        try:
            params = request.params or {}
            folder_path = params.get("folder_path")

            if not folder_path:
                raise ValueError("缺少 folder_path 参数")

            source_path = Path(folder_path)
            if not source_path.exists() or not source_path.is_dir():
                raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

            manager = get_rail_manager()
            extension = manager.import_extension(folder_path)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.import failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_extensions_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """删除 Rail 扩展."""
        try:
            params = request.params or {}
            name = params.get("name")

            if not name:
                raise ValueError("缺少 name 参数")

            manager = get_rail_manager()
            manager.delete_extension(name)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"deleted": True, "name": name},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_extensions_toggle(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """切换 Rail 扩展的启用状态，并触发热更新."""
        try:
            params = request.params or {}
            name = params.get("name")
            enabled = params.get("enabled", False)

            if name is None:
                raise ValueError("缺少 name 参数")
            if enabled is None:
                raise ValueError("缺少 enabled 参数")

            manager = get_rail_manager()

            # 1. 确保 agent 实例已设置（用于热更新）
            agent = self._agent_manager.get_agent_nowait()
            if agent is not None:
                agent_instance = await agent.ensure_instance()
                if agent_instance is not None:
                    manager.set_agent_instance(agent_instance)

            # 2. 更新配置文件中的启用状态
            extension = manager.toggle_extension(name, enabled)

            # 3. 触发热更新：根据 enabled 状态注册或注销 rail
            await manager.hot_reload_rail(name, enabled)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.toggle failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_hooks_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """获取当前 hooks 配置（供 TUI /hooks 命令浏览）."""
        try:
            config_base = get_config()
            hooks_config = load_hooks_config(config_base)
            summary = hooks_config.get_event_summary()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "events": summary,
                    "disable_all_hooks": hooks_config.disable_all_hooks,
                    "source": "config.yaml",
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] hooks.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def send_push(self, msg) -> bool:
        """AgentServer 主动向 Gateway 推送消息。

        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。
        """
        if self._current_ws is None or self._current_send_lock is None:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
            )
            return False

        try:
            wire = build_server_push_wire(msg)
            async with self._current_send_lock:
                sent_original = await send_wire_payload(self._current_ws, wire)
            if not sent_original:
                logger.warning(
                    "[AgentWebSocketServer] send_push 内容过大已降级为错误帧: channel_id=%s",
                    msg.get("channel_id", ""),
                )
                return False
            response_kind = str(msg.get("response_kind") or "").strip()
            if response_kind:
                logger.info(
                    "[AgentWebSocketServer] send_push response_kind wire sent: channel_id=%s kind=%s",
                    msg.get("channel_id", ""),
                    response_kind,
                )
            else:
                logger.info(
                    "[AgentWebSocketServer] send_push 已发送(E2A wire): channel_id=%s",
                    msg.get("channel_id", ""),
                )
            return True
        except Exception as e:
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)
            return False

    def get_agent(self):
        """获取 default agent 实例（向后兼容）."""
        return self._agent_manager.get_agent_nowait()

    def get_agent_manager(self) -> AgentManager:
        """获取 AgentManager 实例."""
        return self._agent_manager

    @staticmethod
    def get_conversation_history(
        session_id: str,
        page_idx: int,
        *,
        subagent_id: str | None = None,
    ) -> dict[str, Any] | None:
        # 按照 session_id 和分页消息获取历史记录
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        if not isinstance(page_idx, int) or page_idx <= 0:
            return None

        normalized_session_id = session_id.strip()
        normalized_subagent_id = (
            subagent_id.strip() if isinstance(subagent_id, str) and subagent_id.strip() else None
        )
        if normalized_subagent_id:
            if not history_exists(normalized_session_id, subagent_id=normalized_subagent_id):
                return {
                    "messages": [],
                    "total_pages": 1,
                    "page_idx": page_idx,
                    "subagent_id": normalized_subagent_id,
                }
        elif not history_exists(normalized_session_id):
            return None
        try:
            raw = load_history_records(
                normalized_session_id,
                subagent_id=normalized_subagent_id,
            )
        except Exception:
            return None
        if not isinstance(raw, list):
            return None

        page_size = _HISTORY_PAGE_SIZE
        restorable = [
            item for item in raw
            if _is_restorable_history_record(item)
        ]
        total = len(restorable)
        total_pages = max(1, math.ceil(total / page_size))
        if page_idx > total_pages:
            return None

        ordered = list(reversed(restorable))
        start = (page_idx - 1) * page_size
        end = start + page_size
        # 不在此处 sanitize：split_history_record_for_stream（在 _handle_history_get_stream
        # 里调）需要拿到原文 content 才能正确切片；先 sanitize 会把 content 砍到 16KB，
        # 切片器拿到的就只剩 16KB，分片就失去意义。
        page_messages = list(ordered[start:end])
        logger.debug(
            "[history.get] session_id=%s subagent_id=%s page_idx=%s "
            "raw_total=%s restorable_total=%s total_pages=%s returned=%s",
            normalized_session_id,
            normalized_subagent_id or "",
            page_idx,
            len(raw),
            total,
            total_pages,
            len(page_messages),
        )
        result = {
            "messages": page_messages,
            "total_pages": total_pages,
            "page_idx": page_idx,
        }
        if normalized_subagent_id:
            result["subagent_id"] = normalized_subagent_id
        return result

    async def _handle_initialize(
            self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 initialize 方法（非流式）.

        调用 AgentManager.initialize 完成初始化，返回 capabilities。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] initialize: request_id=%s channel_id=%s", request.request_id, request.channel_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                self._set_ws_acp_client_capabilities(ws, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await self._agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)

            logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)

    async def _handle_session_create(
            self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle AgentServer-owned creation and TUI external-ID compatibility.

        Normal creation validates project identity before claiming a server-owned
        warm or fresh Session. TUI callers may supply a compatibility ID through
        this same method; AgentServer validates and serializes it, restores or
        persists its binding, and always bypasses prewarming.

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        operation = "session.create"
        logger.info("[AgentServer] %s: request_id=%s", operation, request.request_id)

        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            raw_persist_session = params.get("persist_session", False)
            if not isinstance(raw_persist_session, bool):
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": "persist_session must be a boolean",
                        "code": "BAD_REQUEST",
                    },
                )
                wire = encode_agent_response_for_wire(
                    resp, response_id=request.request_id
                )
                async with send_lock:
                    await send_wire_payload(ws, wire)
                return
            persist_session = raw_persist_session
            mode, _, canonical_mode = resolve_agent_request_mode(params.get("mode", "agent"))
            explicit_session_id = params.get("session_id")
            previous_session_id = str(params.get("previous_session_id") or "").strip()
            requested_session_id = (
                explicit_session_id.strip()
                if isinstance(explicit_session_id, str)
                else ""
            )
            external_tui_session = bool(
                requested_session_id
                and request.req_method == ReqMethod.SESSION_CREATE
                and channel_id.strip().lower() == "tui"
            )
            existing_metadata: dict[str, Any] | None = None
            if requested_session_id and not external_tui_session:
                raise ValueError(
                    "session.create no longer accepts session_id; use session.switch to restore"
                )
            external_id_lock: asyncio.Lock | None = None
            external_id_lock_acquired = False
            if external_tui_session:
                logger.warning(
                    "[AgentServer] TUI supplied session_id via session.create; "
                    "bypassing prewarm compatibility path: session_id=%s",
                    requested_session_id,
                )
                if not is_valid_session_id(requested_session_id):
                    raise ValueError("invalid session_id")

                lock_key = f"external-create:{requested_session_id}"
                external_id_lock = _session_switch_locks.get(lock_key)
                if external_id_lock is None:
                    external_id_lock = asyncio.Lock()
                    _session_switch_locks[lock_key] = external_id_lock
                await external_id_lock.acquire()
                external_id_lock_acquired = True

                # Existing TUI metadata is authoritative. The frontend injects its
                # current cwd into every RPC, which must not rebind a restored session
                # when `--session` is launched from another directory.
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )
                existing_metadata = get_session_metadata(requested_session_id)
                if existing_metadata:
                    existing_channel = str(
                        existing_metadata.get("channel_id") or ""
                    ).strip().lower()
                    if existing_channel not in {"", "tui"}:
                        raise ValueError("session_id is already owned by another channel")
                    stored_persist_session = existing_metadata.get("persist_session") is True
                    if (
                        "persist_session" in params
                        and persist_session != stored_persist_session
                    ):
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "error": "persist_session is immutable after session creation",
                                "code": "CONFLICT",
                            },
                        )
                        wire = encode_agent_response_for_wire(
                            resp, response_id=request.request_id
                        )
                        async with send_lock:
                            await send_wire_payload(ws, wire)
                        return
                    persist_session = stored_persist_session
                    for field in ("project_id", "project_dir", "work_mode", "mode"):
                        value = existing_metadata.get(field)
                        if isinstance(value, str) and value.strip():
                            params[field] = value.strip()
                    mode, _, canonical_mode = resolve_agent_request_mode(
                        params.get("mode", "agent")
                    )
                else:
                    # Resolve a new external TUI id while holding the per-id lock.
                    # This keeps concurrent windows from rebinding the same id to
                    # different projects before metadata becomes visible.
                    from jiuwenswarm.server.runtime.session.project_store import (
                        find_or_create_code_project_for_tui_params,
                    )

                    project = find_or_create_code_project_for_tui_params(params)
                    if project is not None:
                        params["project_id"] = project.project_id
                        params["project_dir"] = project.project_dir
                        params["work_mode"] = project.work_mode
            # TUI 无显式 session_id（未带 --session）创建时：AgentServer 侧按
            # cwd/project_dir 解析真实的 code 项目并写回，避免落到默认 default_code
            # （AgentOS 迁移前由 TUI 本地解析，现收敛到 AgentServer 保证归属一致）。
            if (
                not external_tui_session
                and channel_id.strip().lower() == "tui"
            ):
                from jiuwenswarm.server.runtime.session.project_store import (
                    find_or_create_code_project_for_tui_params,
                )

                candidate_dir = str(
                    params.get("project_dir") or params.get("cwd") or ""
                ).strip()
                if not str(params.get("project_id") or "").strip() and candidate_dir:
                    project = find_or_create_code_project_for_tui_params(params)
                    if project is not None:
                        params["project_id"] = project.project_id
                        params["project_dir"] = project.project_dir
                        params["work_mode"] = project.work_mode
            # Step 1: 归一化 work_mode / project_id / project_dir 三元组
            # (与 web _session_create 共用同一 helper，保持主路径/fallback 一致)
            from jiuwenswarm.server.runtime.session.work_mode import resolve_session_work_mode_params
            binding = resolve_session_work_mode_params(params, channel_id=channel_id)
            if binding.error:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": binding.error, "code": binding.code},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await send_wire_payload(ws, wire)
                return

            # 校验并解析 project_id / project_dir 绑定关系:
            # 一致性校验、按 project_id 自动补齐 project_dir、禁止单传 project_dir
            from jiuwenswarm.server.runtime.session import project_store
            from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE, is_default_project_id
            project_id, project_dir, p_err, p_code = project_store.resolve_session_project_binding(
                binding.project_id, binding.project_dir
            )
            if p_err:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": p_err, "code": p_code},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await send_wire_payload(ws, wire)
                return

            # Step 3: 确定最终 work_mode
            # 对真实 project_id: 最终 work_mode 以 Project 记录为准;若请求显式传了
            # work_mode 且与 Project 不一致 → BAD_REQUEST(设计文档 §4.1.6)
            # 对默认项目: 使用 binding 归一化的 work_mode
            #
            # has_explicit_work_mode 判定逻辑:
            # - gateway 路径: params 含 _work_mode_explicit marker(由 gateway 注入),
            #   消费后立即 pop。marker=True 表示用户显式传了 work_mode(需一致性校验);
            #   marker=False 表示 gateway 注入的通道默认值(跳过校验)。
            # - 直连路径(非 gateway): marker 缺失,使用 binding.has_explicit_work_mode
            #   (此时 params 为原始值,binding 计算结果正确)。
            explicit_work_mode_marker = params.pop("_work_mode_explicit", None)
            if isinstance(explicit_work_mode_marker, bool):
                has_explicit_work_mode = explicit_work_mode_marker
            else:
                # marker 缺失:直连 AgentServer 调用方,params 为原始值,
                # binding.has_explicit_work_mode 正确反映用户是否显式传了 work_mode
                has_explicit_work_mode = binding.has_explicit_work_mode
            if not is_default_project_id(project_id):
                proj = project_store.get_project_by_id(project_id, cache_bust=True)
                if proj is not None:
                    project_work_mode = proj.work_mode or DEFAULT_WEB_WORK_MODE
                    if has_explicit_work_mode and project_work_mode != binding.work_mode:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={
                                "error": f"work_mode mismatch: project is '{project_work_mode}' \
                                    but request specified '{binding.work_mode}'",
                                "code": "BAD_REQUEST",
                            },
                        )
                        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                        async with send_lock:
                            await send_wire_payload(ws, wire)
                        return
                    final_work_mode = project_work_mode
                else:
                    # 竞态: project 已被其他进程删除/隐藏。
                    # 不创建指向不存在项目的会话,返回 NOT_FOUND 由调用方决定回退策略。
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={
                            "error": f"project not found: {project_id}",
                            "code": "NOT_FOUND",
                        },
                    )
                    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                    async with send_lock:
                        await send_wire_payload(ws, wire)
                    return
            else:
                final_work_mode = binding.work_mode

            # 将解析后的字段回写 params,保持与 fallback 路径(app_web_handlers)一致,
            # 后续若读取 params.project_id/project_dir/work_mode 可直接拿到规范化值
            params["project_id"] = project_id
            params["project_dir"] = project_dir
            params["work_mode"] = final_work_mode

            # Resolve after the final work_mode is known. This is important for Web
            # Team sessions: mode=team + work_mode=code must enter the same
            # code.team runtime as the TUI team.code mode.
            resolved = resolve_request_runtime_mode(
                request,
                work_mode=final_work_mode,
            )
            mode = resolved.manager_mode
            canonical_mode = resolved.canonical_mode
            params["mode"] = canonical_mode
            is_swarm = bool(params.get("is_swarm")) or resolved.is_team
            prewarm_eligible = (
                not is_swarm
                and canonical_mode
                in {
                    # 排除 *.plan 模式：plan 模式需先注入 plan reminder 并初始化 plan_state，
                    # prewarm 对 plan 无意义甚至可能与 plan_slug 清理逻辑冲突，只对 normal 系有意义
                    "agent",
                    "code",
                    "code.normal",
                    "agent.work.normal",
                    "agent.code.normal",
                }
                and _is_session_prewarm_model_eligible(params)
            )
            create_token = str(params.get("create_token") or "").strip()
            if external_tui_session:
                claim = WarmClaim(
                    session_id=requested_session_id,
                    prewarm_hit=False,
                    prewarm_status="bypassed",
                )
            else:
                if not create_token:
                    raise ValueError("create_token is required")
                claim = await self._agent_manager.claim_prewarmed_session(
                    channel_id=channel_id,
                    project_id=project_id,
                    project_dir=project_dir,
                    work_mode=final_work_mode,
                    is_swarm=is_swarm,
                    persist_session=persist_session,
                    prewarm_eligible=prewarm_eligible,
                    create_token=create_token,
                )
            session_id = claim.session_id

            # 会话目录已存在则拒绝,避免覆盖既有会话元数据(与 web 本地 handler 一致)
            session_dir = get_agent_sessions_dir() / session_id
            if (session_dir / "metadata.json").is_file():
                if not external_tui_session:
                    from jiuwenswarm.server.runtime.session.session_metadata import (
                        get_session_metadata as read_session_metadata,
                    )

                    self._agent_manager.activate_session_prewarm(session_id)
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "sessionId": session_id,
                            "session_id": session_id,
                            "projectId": project_id,
                            "projectDir": project_dir,
                            "workMode": final_work_mode,
                            "persist_session": bool(
                                read_session_metadata(session_id).get(
                                    "persist_session", False
                                )
                            ),
                            "prewarm_hit": claim.prewarm_hit,
                            "prewarm_status": claim.prewarm_status,
                        },
                    )
                    wire = encode_agent_response_for_wire(
                        resp, response_id=request.request_id
                    )
                    async with send_lock:
                        await send_wire_payload(ws, wire)
                    return
                session_created = False
            else:
                session_created = True

            # 初始化会话元数据(同步写盘),将 project_dir/project_id 等字段落盘
            if session_created:
                from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
                channel_metadata = None
                if channel_id.strip().lower() == "tui":
                    workspace = str(params.get("cwd") or project_dir or "").strip()
                    if workspace:
                        channel_metadata = {
                            "cwd": workspace,
                            "project_dir": project_dir or workspace,
                        }
                init_session_metadata(
                    session_id=session_id,
                    channel_id=channel_id,
                    user_id=str(getattr(request, "user_id", "") or params.get("user_id", "") or "").strip(),
                    title=params.get("title", ""),
                    mode=canonical_mode,
                    project_dir=project_dir,
                    project_id=project_id,
                    persist_session=persist_session,
                    work_mode=final_work_mode,
                    # cron 执行会话（cron-session）创建时即写入 job 的 model（scheduler
                    # SESSION_CREATE 携带 model_name），否则要等首条 chat.send 才落盘，
                    # 前端首开会话会显示默认模型（刷新后才正确）。同步写盘，创建即可读。
                    model=str(params.get("model_name") or "").strip(),
                    cron_id=str(params.get("cron_id") or "").strip(),
                    channel_metadata=channel_metadata,
                )
                if not external_tui_session:
                    self._agent_manager.activate_session_prewarm(session_id)

            # team prepare 必须在 ack 前完成，避免首条 chat.send 与分布式切换竞态；
            # 可选 KVC 信号放到回包后异步，避免拖慢 create RPC。
            lifecycle_params = dict(params)
            lifecycle_params["mode"] = canonical_mode
            lifecycle_reason = "session.create switch: "
            (
                _target_is_team,
                _resolved_mode,
                switch_context,
                team_manager,
                dispatch_signals,
            ) = await self._prepare_session_switch_owner(
                channel_id=channel_id,
                target_session_id=session_id,
                previous_session_id=previous_session_id,
                params=lifecycle_params,
                reason=lifecycle_reason,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "sessionId": session_id,
                    "session_id": session_id,
                    "projectId": project_id,
                    "projectDir": project_dir,
                    "workMode": final_work_mode,
                    "persist_session": persist_session,
                    "prewarm_hit": claim.prewarm_hit,
                    "prewarm_status": claim.prewarm_status,
                    **(
                        {"created": session_created, "mode": canonical_mode}
                        if external_tui_session
                        else {}
                    ),
                },
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)

            logger.info("[AgentServer] %s completed: session_id=%s", operation, session_id)

            if switch_context is not None and dispatch_signals is not None:
                kvc_task = asyncio.create_task(
                    self._dispatch_session_switch_kvc(
                        channel_id=channel_id,
                        target_session_id=session_id,
                        previous_session_id=previous_session_id,
                        reason=lifecycle_reason,
                        context=switch_context,
                        team_manager=team_manager,
                        dispatch_signals=dispatch_signals,
                        view_id=str(params.get("view_id") or f"ws:{id(ws)}"),
                    ),
                    name=f"session-create-kvc-{session_id}",
                )
                _background_session_kvc_tasks.add(kvc_task)
                kvc_task.add_done_callback(_background_session_kvc_tasks.discard)
                kvc_task.add_done_callback(_log_background_session_kvc_failure)

        except Exception as e:
            logger.exception("[AgentServer] %s failed: %s", operation, e)
            if not locals().get("external_tui_session", False):
                await self._agent_manager.release_session_prewarm_claim(
                    locals().get("session_id")
                )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
        finally:
            external_id_lock = locals().get("external_id_lock")
            if (
                external_id_lock is not None
                and locals().get("external_id_lock_acquired", False)
            ):
                external_id_lock.release()

    async def _handle_session_fork(
            self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle session.fork: filesystem copy + in-memory context copy.

        Args:
            ws: WebSocket connection.
            request: AgentRequest with source_session_id, target_session_id, title.
            send_lock: Send lock.
        """
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            copy_session_context,
            copy_session_state,
            fork_session,
        )

        logger.info(
            "[AgentServer] session.fork: request_id=%s", request.request_id
        )

        try:
            params = request.params if isinstance(request.params, dict) else {}
            source = str(params.get("source_session_id") or "").strip()
            target = str(params.get("target_session_id") or "").strip()
            fork_title = str(params.get("title") or "").strip()
            channel_id = request.channel_id or "default"

            if not source:
                raise ValueError("source_session_id is required")
            if not target:
                target = await self._agent_manager.create_session(channel_id=channel_id)

            # 1. Filesystem fork (copies history.json, writes metadata)
            result = fork_session(
                source_session_id=source,
                target_session_id=target,
                title=fork_title,
                channel_id=channel_id,
            )

            # 2. Copy in-memory context (LLM conversation history)
            agent = self._agent_manager.get_agent_nowait(channel_id)
            deep_agent = None
            if agent is not None:
                deep_agent = await agent.ensure_instance()
                await copy_session_context(deep_agent, source, target)
            else:
                logger.warning(
                    "[AgentServer] session.fork: no agent for channel %s, "
                    "in-memory context copy skipped",
                    channel_id,
                )

            # 3. Copy DeepAgentState (task_plan, plan_mode, etc.)
            from openjiuwen.core.single_agent.schema.agent_card import AgentCard

            await copy_session_state(
                source_session_id=source,
                target_session_id=target,
                card=deep_agent.card if deep_agent is not None else AgentCard(id="jiuwenswarm", name="jiuwenswarm"),
                deep_agent=deep_agent,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result,
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)

            logger.info(
                "[AgentServer] session.fork completed: source=%s target=%s title=%s",
                source, target, result.get("title", ""),
            )

        except ValueError as e:
            logger.warning("[AgentServer] session.fork ValueError: %s", e)
            code = (
                "NOT_FOUND" if "not found" in str(e)
                else "ALREADY_EXISTS" if "already exists" in str(e)
                else "BAD_REQUEST"
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "code": code},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)
        except Exception as e:
            logger.exception("[AgentServer] session.fork failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(
                resp, response_id=request.request_id
            )
            async with send_lock:
                await send_wire_payload(ws, wire)

    async def _handle_acp_tool_response(
            self,
            ws: Any,
            request: AgentRequest,
            send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(jsonrpc_id, response_payload):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def handle_acp_tool_response_for_test(
            self,
            ws: Any,
            request: AgentRequest,
            send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        await self._handle_acp_tool_response(ws, request, send_lock)

    async def _handle_harness_packages_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.get request - retrieve packages info."""
        try:
            service = AutoHarnessService(rail=None, agent=None)
            payload = await asyncio.to_thread(service.get_packages_info)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.get failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_harness_packages_scan(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.scan request - scan runtime extensions."""
        try:
            service = AutoHarnessService(rail=None, agent=None)
            payload = await asyncio.to_thread(service.scan_runtime_extensions)
            await asyncio.to_thread(service.save_packages, payload)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.scan failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_harness_packages_activate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.activate request - activate a harness package."""
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")

        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            # Get or create the agent instance (auto-create if not exists)
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            channel_id = request.channel_id or "web"
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()
                logger.info(
                    "[AgentServer] harness.packages.activate: agent_instance type=%s, has_load_harness_config=%s",
                    type(agent_instance).__name__ if agent_instance else None,
                    hasattr(agent_instance, "load_harness_config") if agent_instance else False,
                )

            service = AutoHarnessService(
                rail=None,
                agent=agent_instance,
                agent_manager=self._agent_manager,
            )
            payload = await service.activate_package(package_id, channel_id=channel_id)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning("[AgentServer] harness.packages.activate validation error: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.activate failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_harness_packages_deactivate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.deactivate request - deactivate a harness package."""
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")

        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            # Get or create the agent instance (auto-create if not exists)
            channel_id = request.channel_id or "web"
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=channel_id,
                project_dir=resolve_request_project_dir(request),
                mode=agent_mode,
                sub_mode=sub_mode
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()

            service = AutoHarnessService(
                rail=None,
                agent=agent_instance,
                agent_manager=self._agent_manager,
            )
            payload = await service.deactivate_package(package_id, channel_id=channel_id)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning("[AgentServer] harness.packages.deactivate validation error: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.deactivate failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _handle_harness_packages_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle harness.packages.delete request - delete a harness package."""
        params = request.params if isinstance(request.params, dict) else {}
        package_id = params.get("package_id")

        if not package_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        if package_id == "native":
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "Cannot delete native agent version", "code": "BAD_REQUEST"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await send_wire_payload(ws, wire)
            return

        try:
            mode, sub_mode = _apply_resolved_mode_to_request(request)
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await self._agent_manager.get_agent(
                channel_id=request.channel_id,
                project_dir=resolve_request_project_dir(request),
                mode=agent_mode,
                sub_mode=sub_mode
            )
            agent_instance = None
            if agent is not None:
                agent_instance = await agent.ensure_instance()

            service = AutoHarnessService(
                rail=None,
                agent=agent_instance,
                agent_manager=self._agent_manager,
            )
            payload = await service.delete_package(package_id, channel_id=request.channel_id)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning("[AgentServer] harness.packages.delete validation error: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": _harness_error_code(exc)},
            )
        except Exception as exc:
            logger.exception("[AgentServer] harness.packages.delete failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    def _resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        """Resolve model from jiuwenswarm config.

        Args:
            model_name: Requested model name, falls back to default if None or not found

        Returns:
            Model instance or None if config cannot be loaded
        """
        # Build model cache if not already done
        if not self._model_cache:
            self._build_model_cache()

        # Resolve by name or use default
        if model_name and model_name in self._model_cache:
            return self._model_cache[model_name]
        return self._default_model

    def _build_model_cache(self) -> None:
        """Build model cache from jiuwenswarm config.yaml (reuse interface_deep logic)."""
        # Use the same model building function as interface_deep
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import build_model_from_entry

        config = get_config()

        # Build from models.defaults list
        for entry in get_default_models(config):
            mcc = entry.get("model_client_config") or {}
            model_name = mcc.get("model_name")
            if not model_name:
                continue
            mco = entry.get("model_config_obj") or {}
            self._model_cache[model_name] = build_model_from_entry(mcc, mco)

        # Fallback to legacy format if needed (same as interface_deep._build_model_cache_legacy)
        if not self._model_cache:
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
            self._model_cache[model_name] = build_model_from_entry(mcc, mco)

        # Set default model (first one)
        if self._model_cache:
            first_name = next(iter(self._model_cache))
            self._default_model = self._model_cache[first_name]
            logger.info(
                "[AgentServer] Built model cache with %d models, default=%s",
                len(self._model_cache), first_name
            )

        # 追加 Opencode Zen 免费模型（内存态，不入 config.yaml）。
        # 这些模型可被 _resolve_model 按名解析，但 _default_model 保持上面的用户自配模型。
        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )
            for zent in get_zen_free_model_entries():
                zmcc = zent.get("model_client_config") or {}
                zname = zmcc.get("model_name", "")
                if zname and zname not in self._model_cache:
                    self._model_cache[zname] = build_model_from_entry(
                        zmcc, zent.get("model_config_obj") or {}
                    )
        except Exception:
            logger.debug(
                "[AgentServer] append zen free models to cache failed",
                exc_info=True,
            )

        # 首次启动兜底：默认模型仍为 .env 占位符时，改选 Zen 免费模型（如
        # DeepSeek V4 Flash）作为默认，避免把占位模型发往厂商。仅内存态生效。
        if self._default_model is not None:
            try:
                from jiuwenswarm.common.model_config_validation import (
                    is_placeholder_model_entry,
                    model_client_config_view,
                )
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_default_free_model_entry,
                )
                if is_placeholder_model_entry(
                    model_client_config_view(self._default_model.model_client_config)
                ):
                    zen_default = get_zen_default_free_model_entry()
                    if zen_default is not None:
                        zmcc = zen_default["model_client_config"]
                        zname = zmcc["model_name"]
                        self._model_cache[zname] = build_model_from_entry(
                            zmcc, zen_default.get("model_config_obj") or {}
                        )
                        self._default_model = self._model_cache[zname]
                        logger.info(
                            "[AgentServer] default model is placeholder; "
                            "fallback to zen free model=%s",
                            zname,
                        )
            except Exception:
                logger.debug(
                    "[AgentServer] fallback default to zen free model failed",
                    exc_info=True,
                )

    async def _handle_schedule_request(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        action: str,
    ) -> None:
        """Handle schedule.* requests - schedule task management."""
        logger.info(
            "[AgentServer] schedule.%s request received: request_id=%s channel_id=%s",
            action, request.request_id, request.channel_id,
        )
        try:
            # Lazy initialization: create scheduler service on first request
            if self._scheduler_service is None:
                logger.info("[AgentServer] Initializing scheduler service on first request")
                self._scheduler_service = AutoHarnessService(None, agent=None)
                # Start the scheduler loop
                await self._scheduler_service.start_scheduler()

            params = request.params or {}
            payload: dict[str, Any] = {}

            # For actions that need agent: get agent and set on service (similar to _handle_command_compact)
            needs_agent = action in ("create", "run", "cancel", "delete", "issue_watch_once")
            if needs_agent:
                mode, sub_mode = _apply_resolved_mode_to_request(request)
                agent_mode = "agent" if mode == "auto_harness" else mode
                agent = await self._agent_manager.get_agent(
                    channel_id=request.channel_id or "tui",
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode

                )
                if agent is None:
                    raise ValueError("Failed to get agent for schedule request")
                # Set agent on service (service will use it for execution)
                await self._scheduler_service.update_agent_instance(agent)
                self._set_scheduler_agent(agent)
                logger.info("[AgentServer] Set agent for schedule action %s: %s", action, agent is not None)

            if action == "check_config":
                payload = self._scheduler_service.check_schedule_config()

            elif action == "update_config":
                fields = params.get("fields", {})
                payload = self._scheduler_service.update_schedule_config(fields)

            elif action == "create":
                query = params.get("query", "")
                interval_hours = params.get("interval_hours", 4)
                run_immediately = params.get("run_immediately", False)
                model_name = params.get("model_name")
                pipeline = params.get("pipeline")  # Pipeline preference
                # Resolve model from jiuwenswarm config
                model = self._resolve_model(model_name)
                payload = await self._scheduler_service.create_scheduled_task(
                    query, interval_hours, run_immediately, model, pipeline
                )

            elif action == "run":
                query = params.get("query", "")
                model_name = params.get("model_name")
                pipeline = params.get("pipeline")  # Pipeline preference
                # Resolve model from jiuwenswarm config
                model = self._resolve_model(model_name)
                payload = await self._scheduler_service.run_task(query, model, pipeline)

            elif action == "list":
                tasks = await self._scheduler_service.list_scheduled_tasks()
                payload = {"tasks": tasks}

            elif action == "status":
                task_id = params.get("task_id", "")
                task = await self._scheduler_service.get_scheduled_task_status(task_id)
                payload = task if task else {"error": "任务不存在", "task_id": task_id}

            elif action == "logs":
                task_id = params.get("task_id", "")
                log_type = params.get("log_type", "current")
                history_index = params.get("history_index", -1)
                offset = params.get("offset", 0)
                limit = params.get("limit", 500)
                payload = await self._scheduler_service.get_scheduled_task_logs(
                    task_id, log_type, history_index, offset, limit
                )

            elif action == "cancel":
                task_id = params.get("task_id", "")
                payload = await self._scheduler_service.cancel_scheduled_task(task_id)

            elif action == "delete":
                task_id = params.get("task_id", "")
                payload = await self._scheduler_service.delete_scheduled_task(task_id)

            elif action == "issue_watch_once":
                model_name = params.get("model_name")
                model = self._resolve_model(model_name)
                payload = await self._scheduler_service.watch_gitcode_issues_once(params, model)

            elif action == "issue_state_list":
                payload = await self._scheduler_service.list_gitcode_issue_states()

            elif action == "issue_delete":
                payload = await self._scheduler_service.delete_issue_states(params)

            elif action == "issue_matrix":
                payload = await self._scheduler_service.refresh_issue_matrix(params)

            else:
                payload = {"error": f"未知的调度操作: {action}"}

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
            logger.info(
                "[AgentServer] schedule.%s response prepared: request_id=%s channel_id=%s ok=%s payload_keys=%s",
                action, resp.request_id, resp.channel_id, resp.ok, list(payload.keys())[:10],
            )
        except Exception as exc:
            logger.exception("[AgentServer] schedule.%s failed: %s", action, exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        logger.info(
            "[AgentServer] schedule.%s sending response wire: request_id=%s wire_keys=%s",
            action, request.request_id, list(wire.keys())[:10],
        )
        async with send_lock:
            await send_wire_payload(ws, wire)
