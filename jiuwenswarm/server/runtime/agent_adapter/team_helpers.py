# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team agent streaming helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.paths import (
    get_agent_teams_home,
    independent_member_workspace,
    team_home,
)
from openjiuwen.agent_teams.runtime import RunActionKind
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.monitor import TeamStreamLogger
from openjiuwen.core.runner import Runner
from openjiuwen.core.common.logging import server_logger
from openjiuwen.harness import DeepAgent

from jiuwenswarm.agents.harness.team import TeamManager, get_team_manager
from jiuwenswarm.agents.harness.team.team_manager import TEAM_EVENT_QUEUE_MAXSIZE
from jiuwenswarm.common.log_preview import DEFAULT_PREVIEW_MAX_CHARS, preview_text
from jiuwenswarm.common.cron_team_completion import (
    _cron_solo_harness_end_pending,
    _drain_cron_delegation_grace_events,
    apply_cron_team_round_event,
    cron_team_round_should_end,
    new_cron_team_round_state,
)
from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler
from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_metadata,
    increment_session_round_count,
    update_session_metadata,
)
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from jiuwenswarm.common.invocation_context import (
    TRACE_CONTEXT_METADATA_KEY,
    TRACE_HEADER_EXPORTER_METADATA_KEY,
    get_current_invocation_context,
    trace_context_to_dict,
)
from jiuwenswarm.server.invocation_context_builder import build_invocation_context
from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk, is_retry_notice_payload
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter.team_stall_watchdog import schedule_team_stall_watchdog
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    EvolutionProgressStatus,
    EvolutionPushContext,
    TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
    TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES,
    TEAM_EVOLUTION_HIDDEN_STAGE,
    TEAM_EVOLUTION_IDLE_SLEEP_SEC,
    TEAM_EVOLUTION_SLASH_WARNING_PHRASES,
    TEAM_EVOLUTION_START_MESSAGE,
    TEAM_EVOLUTION_START_STAGE,
    broadcast_evolution_progress,
    build_evolution_status_update,
    event_type,
    evolution_outcome_from_event,
    evolution_progress_status_from_event,
    evolution_slash_command_name,
    evolution_slash_result,
    extract_evolution_request_id,
    group_evolution_approvals,
    is_evolution_outcome_event,
    make_team_evolution_cycle_request_id,
    progress_for_request,
    push_evolution_event,
    push_evolution_status,
    resolve_evolution_event_timeout_sec,
    team_evolution_end_update,
    terminal_progress_from_events,
    terminal_stage,
    visible_evolution_progress_from_events,
)
from jiuwenswarm.server.runtime.agent_adapter.evolution_slash import (
    EvolutionSlashContext,
    handle_evolution_slash_command,
)

logger = logging.getLogger(__name__)

# Waiter + cron-team-completion state lives on the singleton TeamManager
# (TeamManager._pending_waiters / TeamManager._cron_team_completion), indexed
# by session_id. TeamManager is process-wide and shared across channels, so a
# bridged follow-up (e.g. /join from feishu while the web stream is alive)
# finds the originating channel's waiter regardless of arrival channel. There
# is no module-level global waiter registry — reach it via
# get_team_manager(channel_id) or the team_manager handle passed in.
_WORKFLOW_RUNS_STATE_KEY = "workflow_runs"

_TEAM_CREATE_KINDS = {
    RunActionKind.CREATE.value,
    RunActionKind.NEW_TEAM_IN_SESSION.value,
}
_HIDE_DM_PREFIX = "/hide_dm"
_STREAM_TRACE_ENV_KEY = "JIUWENSWARM_TEAM_STREAM_TRACE"
# When set to "true", non-leader teammate frames are filtered out in team
# streaming so the frontend only receives leader output.
_HIDE_TEAMMATE_ENV_KEY = "JIUWENSWARM_TEAM_HIDE_TEAMMATE"
# /debug 剥离原语与 Agent/Code 共享（debug_trace.directives），消除两份实现。
# 别名保持 _DEBUG_PREFIX / _strip_directive 不变，_extract_query_directives 零改动。
from jiuwenswarm.server.runtime.debug_trace.directives import (
    DEBUG_PREFIX as _DEBUG_PREFIX,
    strip_slash_directive as _strip_directive,
)
_FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC = 10.0
_FOLLOWUP_INTERACT_POLL_INTERVAL_SEC = 0.05


def _new_team_event_queue() -> asyncio.Queue:
    return asyncio.Queue(maxsize=TEAM_EVENT_QUEUE_MAXSIZE)


def _safe_team_path_segment(value: str, fallback: str = "_") -> str:
    """Sanitize a value into one path segment for team workspace paths."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized[:96] or fallback


def _team_hide_teammate_enabled() -> bool:
    """Return whether non-leader teammate frames should be filtered out in team mode."""
    return os.environ.get(_HIDE_TEAMMATE_ENV_KEY, "").strip().lower() == "true"


def _is_retry_notice(parsed: dict[str, Any]) -> bool:
    """重试通知（rail 的"模型调用异常，将在 X 秒后进行第 N 次重试"）是过程不是结果：
    广播照发（前端扫光条需要），但不落盘——否则一次重试一条留痕，刷新后警告计数失真。
    判定口径在 stream_utils.is_retry_notice_payload（标记优先、文案兜底），
    与 interface.py 通用落盘器共用同一实现。"""
    return is_retry_notice_payload(parsed)


_INTERACT_REASON_ERROR_MAP: dict[str, str] = {
    "not_active": "Team is initializing, please try again later",
    "session_mismatch": "Session state mismatch, please refresh and retry",
    # 与 _FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC（10s）对应：拆除中间态窗口的
    # 诚实时长提示（重试循环最长等 10s，超时后用户可安全重试）
    "gate_closed": "Team is shutting down, please try again later (in about 10 seconds)",
    "unknown_human_agent": "Member not found, please check the name",
    "human_agent_not_enabled": "Human agent is not yet available, please try again later",
    "no_team_backend": "Team backend not ready, please try again later",
    "agent_unavailable": "Target member not available, please check the member name",
}

# ── fan_out 规则（表驱动）──────────────────────────────────────
# 所有 team 事件均显式产出 fan_out，godview 始终在 fan_out 中（不靠 Gateway 兜底）。
# 规则按两个独立维度组织，分别用一张表：
#   1. _INNER_TYPE_FANOUT —— 按 event.event.type（team.message 内层子类型）
#   2. _ROLE_FANOUT       —— 按 event.role（外层角色）
# _build_logical_targets 依次查这两张表，命中即返回；都未命中走 godview 兜底。
#
# intent 语义（三态，见 session_sharing.LogicalTarget / _build_routing_target）：
#   - godview  : 投递给 GodView 订阅者，不带 @
#   - mention  : 投递给被点名成员 + 带 @（飞书 <at>）；monitor 转发的 P2P 消息用此
#   - private  : 投递给被点名成员但不带 @；纯 teammate LLM 输出用此，不打扰人类用户
# 区分 mention/private 的关键：前者 @ 用户，后者不 @，由 _build_routing_target 按 intent 决定。


def _tgt_godview() -> dict:
    return {"intent": "godview"}


def _tgt_mention(
    member_names, *, mention_all: bool = False, speaker: str | None = None
) -> dict:
    """mention intent：投递给被点名成员并带 @（飞书 <at>）。"""
    tgt: dict = {
        "intent": "mention",
        "member_names": list(member_names),
        "speaker": speaker,
    }
    if mention_all:
        tgt["mention_all"] = True
    return tgt


def _tgt_private(member_names, *, speaker: str | None = None) -> dict:
    """private intent：投递给被点名成员但不带 @。"""
    return {
        "intent": "private",
        "member_names": list(member_names),
        "speaker": speaker,
    }


def _p2p_fanout(inner: dict) -> list[dict]:
    """P2P 消息 fan_out：godview + 收件人(mention, 带 @) + 发送方(private, 不带 @)。

    - 收件人用 mention：被 @ 提醒，飞书渲染 <at>。
    - 发送方用 private：自己发的消息不该 @ 自己（private intent 在
      ``_build_routing_target`` 里不注入 mention_member_ids），零打扰，
      仅用于发送方在自己的 /join 窗口看到自己发出的 P2P 卡片。
    - from_member 缺失时不追加 private([None])，避免把 None 当 member_name
      查 Registry 留下调试噪音（见 dispatch_to_session 的 lookup_member）。
    - from/to 落同一物理容器（飞书群、同一 ws）时，dispatch_to_session 的
      sent_containers 跨 intent 去重，先到的 intent 标记容器已发，后到跳过，
      至少显示一次，不会双发。
    """
    targets = [
        _tgt_godview(),
        _tgt_mention([inner["to_member"]], speaker=inner.get("from_member")),
    ]
    fm = inner.get("from_member")
    if fm:
        targets.append(_tgt_private([fm], speaker=fm))
    return targets


# 维度 1：按 event.event.type 分发（team.message 内层子类型）
_INNER_TYPE_FANOUT: dict[str, Any] = {
    # monitor 转发的 P2P 消息 → godview + 收件人(mention,带@) + 发送方(private,不带@)
    "team.message.p2p": _p2p_fanout,
    # 广播 → godview + mention_all
    "team.message.broadcast": lambda inner: [
        _tgt_godview(),
        _tgt_mention([], mention_all=True, speaker=inner.get("from_member")),
    ],
}

# 维度 2：按 event.role 分发（外层角色）
# teammate LLM 输出 → godview + private(该 teammate 席位，不带 @)。
# HumanAgent 需要看到自己扮演的 agent 的输出才能进行自对话；纯 agent 输出不打扰人类。
_ROLE_FANOUT: dict[str, Any] = {
    "teammate": lambda ev: [
        _tgt_godview(),
        _tgt_private([ev["member_name"]], speaker=ev["member_name"]),
    ],
}

_GODVIEW_TARGET = [_tgt_godview()]


def _build_logical_targets(event: dict) -> list[dict]:
    """所有 team 事件 → fan_out 规则（表驱动，依次查两维后兜底 godview）。

    查询顺序（命中即返回）：
      1. event.event.type ∈ _INNER_TYPE_FANOUT  —— team.message.p2p/broadcast
      2. event.role ∈ _ROLE_FANOUT              —— teammate 输出 → private
      3. 兜底 → [godview]                        —— leader 输出、team.member、team.task 等

    p2p 用 mention（带 @），teammate 输出用 private（不带 @），broadcast 用 mention_all。
    """
    # 维度 1：team.message 内层子类型
    if event.get("event_type") == "team.message":
        inner = event.get("event", {}) or {}
        fn = _INNER_TYPE_FANOUT.get(inner.get("type", ""))
        if fn:
            return fn(inner)

    # 维度 2：外层角色
    role = str(event.get("role", "")).strip().lower()
    fn = _ROLE_FANOUT.get(role)
    if fn:
        member_name = str(event.get("member_name", "")).strip()
        if member_name:
            return fn(event)

    # 兜底：其余所有 team 消息都带 godview
    return _GODVIEW_TARGET


def _is_followup_delivery_boundary_reason(reason: str | None) -> bool:
    """Return whether follow-up delivery likely hit a runtime boundary."""
    normalized = str(reason or "")
    if normalized in {"agent_unavailable", "gate_closed", "not_active"}:
        return True
    return normalized.startswith("deliver_to_leader_failed:")


@dataclass(slots=True)
class _FollowupInteractBoundaryResult:
    """Result of delivering a follow-up across a runtime boundary."""

    success: bool
    reason: str | None
    first_request_ready: bool


async def _interact_with_request_metadata(
    team_manager: Any,
    session_id: str,
    query: Any,
    request_metadata: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    interact = team_manager.interact
    try:
        parameters = inspect.signature(interact).parameters.values()
        accepts_metadata = any(
            parameter.name == "request_metadata"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_metadata = True
    if accepts_metadata:
        return await interact(
            session_id,
            query,
            request_metadata=request_metadata,
        )
    return await interact(session_id, query)


def build_team_request_metadata(request: Any) -> dict[str, Any]:
    """Build canonical Team metadata, including the current TraceContext."""
    if request is None:
        raise TypeError("request must be an AgentRequest")

    metadata = dict(getattr(request, "metadata", None) or {})
    metadata.pop(TRACE_CONTEXT_METADATA_KEY, None)
    metadata.pop(TRACE_HEADER_EXPORTER_METADATA_KEY, None)
    invocation = get_current_invocation_context()
    if invocation is None:
        try:
            invocation = build_invocation_context(request)
        except TypeError:
            invocation = None
    trace = invocation.trace if invocation is not None else None
    if trace is not None:
        metadata[TRACE_CONTEXT_METADATA_KEY] = trace_context_to_dict(trace)
    exporter_name = (
        invocation.metadata.get(TRACE_HEADER_EXPORTER_METADATA_KEY)
        if invocation is not None and isinstance(invocation.metadata, dict)
        else None
    )
    if isinstance(exporter_name, str) and exporter_name.strip():
        metadata[TRACE_HEADER_EXPORTER_METADATA_KEY] = exporter_name.strip()
    params = getattr(request, "params", None)
    if isinstance(params, dict):
        metadata.setdefault("mode", params.get("mode"))
        metadata.setdefault(
            "supports_user_interaction",
            params.get("supports_user_interaction") is not False,
        )
    return metadata


async def _deliver_followup_interact_across_boundary(
    team_manager: Any,
    session_id: str,
    query: Any,
    *,
    request_metadata: dict[str, Any] | None = None,
    initial_reason: str | None = None,
    timeout_sec: float = _FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC,
    poll_interval_sec: float = _FOLLOWUP_INTERACT_POLL_INTERVAL_SEC,
) -> _FollowupInteractBoundaryResult:
    """Deliver a follow-up until interact succeeds or the session becomes first-run ready."""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    sleep_sec = max(0.01, poll_interval_sec)
    last_reason = initial_reason
    while time.monotonic() < deadline:
        if not await _team_session_has_runtime(team_manager, session_id):
            return _FollowupInteractBoundaryResult(success=False, reason=last_reason, first_request_ready=True)
        await asyncio.sleep(sleep_sec)
        if not await _team_session_has_runtime(team_manager, session_id):
            return _FollowupInteractBoundaryResult(success=False, reason=last_reason, first_request_ready=True)
        success, reason = await _interact_with_request_metadata(
            team_manager,
            session_id,
            query,
            request_metadata,
        )
        if success:
            return _FollowupInteractBoundaryResult(success=True, reason=None, first_request_ready=False)
        last_reason = reason
        if not _is_followup_delivery_boundary_reason(reason):
            return _FollowupInteractBoundaryResult(success=False, reason=reason, first_request_ready=False)
    first_request_ready = not await _team_session_has_runtime(team_manager, session_id)
    return _FollowupInteractBoundaryResult(
        success=False,
        reason=last_reason,
        first_request_ready=first_request_ready,
    )


async def _deliver_followup_with_request_metadata(
    team_manager: Any,
    session_id: str,
    query: Any,
    *,
    request_metadata: dict[str, Any] | None,
    initial_reason: str | None,
) -> _FollowupInteractBoundaryResult:
    delivery = _deliver_followup_interact_across_boundary
    parameters = inspect.signature(delivery).parameters.values()
    accepts_metadata = any(
        parameter.name == "request_metadata"
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    kwargs: dict[str, Any] = {"initial_reason": initial_reason}
    if accepts_metadata:
        kwargs["request_metadata"] = request_metadata
    return await delivery(team_manager, session_id, query, **kwargs)


def _build_team_event_chunk_meta(event: Any) -> tuple[dict | None, dict]:
    """从 team event 统一推导 (agent_ref, metadata)，供所有 team 事件产出路径调用。

    - agent_ref: 成员身份标识。前端 team.member.spawned 用 agent_ref.id 拼接
      /join team_<name>_session_<sid>，取不到会 fallback 'unknown'。
    - metadata: fan_out_targets 路由元数据，由 _build_logical_targets 产出。

    按设计（§10/§14.2.3/§13.3）agent_ref 是 server 层统一注入，不在 monitor 层加。
    非 team 事件（chat.error / processing_status / completion 等控制信号）返回
    (None, {})，不注入。
    """
    if not isinstance(event, dict):
        return None, {}
    ev_type = event.get("event_type", "")
    role = event.get("role", "")
    if role == "teammate":
        agent_ref: dict | None = {"mode": "team", "id": event.get("member_name", "teammate")}
    elif ev_type in ("team.member", "team.task"):
        # team_id 嵌套在 event.event 内层
        inner = event.get("event", {}) or {}
        agent_ref = {"mode": "team", "id": inner.get("team_id", "team")}
    elif ev_type == "team.message":
        inner = event.get("event", {}) or {}
        agent_ref = {"mode": "team", "id": inner.get("from_member", "team")}
    else:
        agent_ref = None
    fan_out = _build_logical_targets(event)
    metadata = {"fan_out_targets": fan_out} if fan_out else {}
    return agent_ref, metadata


def _extract_query_directives(query: str) -> tuple[str, bool, bool]:
    """Strip all leading slash directives from the first team query.

    Returns (cleaned_query, hide_dm, debug).
    """
    query, hide_dm = _strip_directive(query, _HIDE_DM_PREFIX)
    query, debug = _strip_directive(query, _DEBUG_PREFIX)
    return query, hide_dm, debug


@dataclass(slots=True)
class _FirstTeamRequestPreparation:
    """Result of first-request preprocessing."""

    recovered_runtime: bool
    query: Any
    hide_dm: bool
    debug: bool
    error_chunks: list[AgentResponseChunk] | None = None


async def _prepare_first_team_request(
    *,
    team_manager: Any,
    session_id: str,
    channel_id: str | None,
    request_id: str,
    query: Any,
) -> _FirstTeamRequestPreparation:
    """Apply first-request preprocessing shared by cold starts and fallback starts."""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    hide_dm = False
    debug = False

    if isinstance(query, InteractiveInput):
        wait_for_resumable = getattr(team_manager, "wait_for_resumable_runtime", None)
        restored = False
        if callable(wait_for_resumable):
            try:
                restored = bool(await wait_for_resumable(session_id))
            except Exception as exc:
                logger.warning(
                    "[TeamHelpers] waiting for resumable runtime failed: "
                    "channel_id=%s session_id=%s error=%s",
                    _resolve_channel_id(channel_id),
                    session_id,
                    exc,
                )
        if restored or await _team_session_has_runtime(team_manager, session_id):
            logger.info(
                "[TeamHelpers] interactive input recovered paused team runtime: "
                "channel_id=%s session_id=%s",
                _resolve_channel_id(channel_id),
                session_id,
            )
            return _FirstTeamRequestPreparation(
                recovered_runtime=True,
                query=query,
                hide_dm=hide_dm,
                debug=debug,
            )

        logger.warning(
            "[TeamHelpers] interactive input ignored because no active team runtime exists: "
            "channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
        error_chunks = [
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "Team runtime is not active, please restart the task",
                },
                is_complete=False,
            ),
            _team_processing_done_chunk(request_id, channel_id, session_id),
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload=None,
                is_complete=True,
            ),
        ]
        return _FirstTeamRequestPreparation(
            recovered_runtime=False,
            query=query,
            hide_dm=hide_dm,
            debug=debug,
            error_chunks=error_chunks,
        )

    query, hide_dm, debug = _extract_query_directives(str(query or ""))
    if hide_dm or debug:
        logger.info(
            "[TeamHelpers] query directives captured for first team request: "
            "channel_id=%s session_id=%s hide_dm=%s debug=%s",
            _resolve_channel_id(channel_id),
            session_id,
            hide_dm,
            debug,
        )
    return _FirstTeamRequestPreparation(
        recovered_runtime=False,
        query=query,
        hide_dm=hide_dm,
        debug=debug,
    )


def sync_team_identity_metadata(
    *,
    channel_id: str | None,
    session_id: str,
    mode: str,
    ready_team_name: str,
    activation_kind: str | None,
) -> None:
    """Persist team identity when a team runtime becomes ready."""
    metadata = get_session_metadata(session_id)
    existing_team_name = str(metadata.get("team_name") or "").strip()
    normalized_kind = str(activation_kind or "").strip()

    if existing_team_name and existing_team_name != ready_team_name:
        logger.warning(
            "[TeamHelpers] team session identity mismatch, keep existing metadata: "
            "session_id=%s existing_team_name=%s new_team_name=%s activation_kind=%s",
            session_id,
            existing_team_name,
            ready_team_name,
            normalized_kind,
        )
        return

    update_session_metadata(
        session_id=session_id,
        channel_id=_resolve_channel_id(channel_id),
        mode=mode,
        team_name=ready_team_name,
    )


def persist_workflow_runs(runs: dict[str, WorkflowRunState], session_id: str) -> None:
    """Persist WorkflowRunState dict to session metadata (file-based store)."""
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata, _enqueue_write
    runs_data = {run_id: run_state.model_dump() for run_id, run_state in runs.items()}
    metadata = _read_metadata(session_id, cache_bust=True)
    metadata[_WORKFLOW_RUNS_STATE_KEY] = runs_data
    _enqueue_write(session_id, metadata)


def restore_workflow_runs(session_id: str) -> dict[str, WorkflowRunState] | None:
    """Restore WorkflowRunState dict from session metadata."""
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata
    metadata = _read_metadata(session_id, cache_bust=True)
    runs_data = metadata.get(_WORKFLOW_RUNS_STATE_KEY)
    if not runs_data:
        return None
    return {
        run_id: WorkflowRunState.model_validate(run_data)
        for run_id, run_data in runs_data.items()
    }


def _resolve_channel_id(channel_id: str | None) -> str:
    return str(channel_id or "default").strip() or "default"


def _resolve_request_language(request: Any) -> str:
    metadata = getattr(request, "metadata", None)
    params = getattr(request, "params", None)
    sources = []
    if isinstance(metadata, dict):
        sources.append(metadata)
    if isinstance(params, dict):
        sources.append(params)

    for source in sources:
        for key in ("language", "preferred_language", "preferred_response_language"):
            value = source.get(key)
            if value:
                return str(value).strip().lower() or "zh"
    return "zh"


def _safe_query_preview(query: Any, limit: int = DEFAULT_PREVIEW_MAX_CHARS) -> str:
    return preview_text(query, limit)


# Parsed event types that carry text produced by a model, as opposed to the
# framework control events (team.runtime_ready, tool.use, ...) that also travel
# on the same stream.
_MODEL_OUTPUT_EVENT_TYPES = frozenset({"chat.delta", "chat.final", "chat.reasoning"})


def _normalize_team_query(query: Any, *, channel_id: str | None, language: str) -> Any:
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    a2ui_prompt = build_user_prompt_if_a2ui_event(
        query,
        channel=_resolve_channel_id(channel_id),
        language=language,
    )
    if a2ui_prompt is not None:
        return a2ui_prompt
    return query


async def _team_session_has_runtime(team_manager: TeamManager, session_id: str) -> bool:
    # Keep ordinary team first-request detection scoped to claw-local
    # live markers only. Resumable Runner-pool entries are reserved for
    # InteractiveInput recovery and must not make a fresh text request
    # look like a follow-up after the previous round has ended.
    return (
        team_manager.is_runtime_active(session_id)
        or team_manager.is_runtime_pending(session_id)
        or bool(team_manager.has_stream_task(session_id))
    )


async def query_team_human_members_for_join(
    session_id: str, team_name: str,
) -> list[dict[str, Any]]:
    """直查 team.db 取该 team 的全部成员（未 role 过滤，交调用方过滤）。

    纯查询：session_id↔team_name 一致性校验与对外文案均由 gateway 拼，
    本函数只查不判。team_name 空、DB miss、DB 异常一律返回空 list。
    session_id 仅用于日志排查，不参与查询。
    """
    if not team_name:
        return []
    try:
        members = await TeamMonitorHandler.get_member_list_from_db(team_name)
    except Exception as exc:
        logger.warning(
            "[TeamHelpers] query_team_human_members_for_join db query failed: "
            "session=%s team=%s error=%s", session_id, team_name, exc,
        )
        return []
    return members or []


async def ensure_monitor_handlers_for_active_runtime(
    channel_id: str | None,
    session_id: str,
    team_name: str,
    hide_dm: bool = False,
    enable_swarmflow: bool = False,
) -> None:
    """Attach TeamMonitorHandler and optionally WorkflowMonitorHandler for the active runtime.

    Both handlers obtain their own TeamMonitor from Runner (independent listeners on
    team_agent). WorkflowMonitorHandler is only created when enable_swarmflow is True.
    """
    tm = get_team_manager(channel_id)

    # --- TeamMonitorHandler ---
    existing_monitor = tm.get_monitor(session_id)
    if existing_monitor is None or not existing_monitor.is_running:
        # create_monitor inside Runner.get_agent_team_monitor freezes the
        # current contextvar session_id into the TeamMonitor (self._session_id).
        # runtime_ready fires before the leader's bind_session, so the
        # contextvar is empty here; bind the explicit session_id so the
        # monitor does not hash an empty session id and target non-existent
        # per-session tables (team_task_<hash> / team_message_<hash>).
        token = set_session_id(session_id)
        try:
            monitor = await Runner.get_agent_team_monitor(
                team_name=team_name,
                session_id=session_id,
                hide_dm=hide_dm,
            )
        finally:
            reset_session_id(token)
        if monitor is None:
            logger.warning(
                "[TeamHelpers] active team monitor unavailable: channel_id=%s session_id=%s team_name=%s",
                _resolve_channel_id(channel_id),
                session_id,
                team_name,
            )
        else:
            monitor_handler = TeamMonitorHandler(monitor, session_id)
            try:
                await monitor_handler.start()
                tm.register_monitor(session_id, monitor_handler)
                logger.info(
                    "[TeamHelpers] Monitor started: channel_id=%s session_id=%s team_name=%s",
                    _resolve_channel_id(channel_id),
                    session_id,
                    team_name,
                )
                if monitor_handler.is_running:
                    consumer_task = asyncio.create_task(
                        _consume_monitor_events(channel_id, session_id, monitor_handler)
                    )
                    monitor_handler.set_consumer_task(consumer_task)
            except Exception as exc:
                logger.warning("[TeamHelpers] Monitor start failed: %s", exc)

    # --- WorkflowMonitorHandler (only when swarmflow is enabled) ---
    if not enable_swarmflow:
        return

    existing_wf = tm.get_workflow_handler(session_id)
    if existing_wf is not None and existing_wf.is_running:
        return

    # Build initial_runs: merge in-memory runs from a stopped handler with
    # disk-restored runs. In-memory data is more up-to-date (may contain
    # events not yet persisted), so it takes priority over disk data.
    initial_runs: dict[str, WorkflowRunState] | None = None
    if existing_wf is not None:
        # Stopped handler still holds _runs in memory — prefer these
        initial_runs = existing_wf.get_run_states()
        # Merge disk-restored runs for any IDs not present in memory
        restored_from_disk = restore_workflow_runs(session_id)
        if restored_from_disk:
            for run_id, run_state in restored_from_disk.items():
                if run_id not in initial_runs:
                    initial_runs[run_id] = run_state
        # Clean up the stale handler reference
        tm.pop_workflow_handler(session_id)
    else:
        # No in-memory handler — restore from disk only
        initial_runs = restore_workflow_runs(session_id)

    # Bind the explicit session_id so create_monitor freezes the real id
    # instead of an empty contextvar (same rationale as the TeamMonitor
    # path above).
    wf_token = set_session_id(session_id)
    try:
        wf_monitor = await Runner.get_agent_team_monitor(
            team_name=team_name,
            session_id=session_id,
        )
    finally:
        reset_session_id(wf_token)
    if wf_monitor is None:
        logger.warning(
            "[TeamHelpers] workflow monitor unavailable: channel_id=%s session_id=%s team_name=%s",
            _resolve_channel_id(channel_id),
            session_id,
            team_name,
        )
        return

    wf_handler = WorkflowMonitorHandler(
        monitor=wf_monitor,
        session_id=session_id,
        channel_id=channel_id,
        initial_runs=initial_runs,
    )
    try:
        await wf_handler.start()
        tm.register_workflow_handler(session_id, wf_handler)
        logger.info(
            "[TeamHelpers] WorkflowMonitorHandler started: channel_id=%s session_id=%s team_name=%s",
            _resolve_channel_id(channel_id),
            session_id,
            team_name,
        )
        if wf_handler.is_running:
            consumer_task = asyncio.create_task(
                _consume_workflow_events(channel_id, session_id, wf_handler),
                name=f"workflow_events_{_resolve_channel_id(channel_id)}_{session_id}",
            )
            wf_handler.set_consumer_task(consumer_task)
    except Exception as exc:
        logger.warning("[TeamHelpers] WorkflowMonitorHandler start failed: %s", exc)


def _is_cron_request_id(request_id: str) -> bool:
    return str(request_id or "").startswith("cron-")


async def _wait_for_cron_team_round_events(
    *,
    request_queue: asyncio.Queue,
    round_state: dict[str, Any],
    request_id: str,
    channel_id: str | None,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield team events until cron round completion signals align across modes."""
    while True:
        try:
            event = await asyncio.wait_for(request_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            if cron_team_round_should_end(round_state):
                break
            # Fallback: if the underlying team stream task has ended, no more
            # events will arrive.  Break so the agent stream can finalise and
            # the cron scheduler stops receiving keepalive chunks (avoids the
            # 20-minute timeout when completion events were never produced).
            try:
                tm = get_team_manager(channel_id)
                if not tm.has_stream_task(session_id):
                    logger.info(
                        "[TeamHelpers] cron team round ending: stream task gone "
                        "channel_id=%s session_id=%s request_id=%s "
                        "workflow_completed=%s leader_final_seen=%s "
                        "team_round_completed=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        request_id,
                        round_state.get("workflow_completed"),
                        round_state.get("leader_final_seen"),
                        round_state.get("team_round_completed"),
                    )
                    break
            except Exception as exc:
                logger.warning(
                    "[TeamHelpers] cron team stream-task check failed: "
                    "channel_id=%s session_id=%s request_id=%s error=%s",
                    _resolve_channel_id(channel_id),
                    session_id,
                    request_id,
                    exc,
                )
            continue
        if not isinstance(event, dict):
            continue
        evt_type = str(event.get("event_type") or "").strip()
        yield event
        if evt_type == "team.error":
            break
        apply_cron_team_round_event(round_state, event)
        if cron_team_round_should_end(round_state):
            if _cron_solo_harness_end_pending(round_state):
                for grace_event in await _drain_cron_delegation_grace_events(
                    request_queue=request_queue,
                    round_state=round_state,
                ):
                    yield grace_event
                if not cron_team_round_should_end(round_state):
                    continue
            logger.info(
                "[TeamHelpers] cron team round complete: channel_id=%s request_id=%s "
                "workflow_completed=%s leader_final_seen=%s team_round_completed=%s "
                "open_tasks=%s active_members=%s",
                _resolve_channel_id(channel_id),
                request_id,
                round_state.get("workflow_completed"),
                round_state.get("leader_final_seen"),
                round_state.get("team_round_completed"),
                len(round_state.get("open_team_tasks") or {}),
                len(round_state.get("active_team_members") or {}),
            )
            break


_CRON_DELEGATION_GRACE_SECONDS = 2.0


async def _finish_cron_team_stream_after_delegation_grace(
    channel_id: str | None,
    session_id: str,
    round_id: Any,
) -> None:
    """Wait briefly after a solo harness final before ending the cron team stream."""
    await asyncio.sleep(_CRON_DELEGATION_GRACE_SECONDS)
    resolved_channel_id = _resolve_channel_id(channel_id)
    completion = get_team_manager(channel_id).get_cron_completion(session_id)
    if completion is None:
        return
    if completion.get("tasks_ever_created"):
        completion["finish_scheduled"] = False
        return
    if not cron_team_round_should_end(completion):
        completion["finish_scheduled"] = False
        return
    await _finish_cron_team_stream_after_round(channel_id, session_id, round_id)


async def _finish_cron_team_stream_after_round(
    channel_id: str | None,
    session_id: str,
    round_id: Any,
) -> None:
    """Cancel the background team stream once cron SwarmFlow + leader report are done."""
    resolved_channel_id = _resolve_channel_id(channel_id)
    team_manager = get_team_manager(channel_id)
    try:
        stream_task = team_manager.pop_stream_task(session_id)
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
        await _broadcast_event(
            channel_id,
            session_id,
            {
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "rid": round_id,
                "is_processing": False,
                "is_complete": True,
            },
        )
        logger.info(
            "[TeamHelpers] cron team stream finished early: channel_id=%s session_id=%s",
            resolved_channel_id,
            session_id,
        )
    except Exception as exc:
        logger.warning(
            "[TeamHelpers] cron team stream finish failed: channel_id=%s session_id=%s error=%s",
            resolved_channel_id,
            session_id,
            exc,
        )
    finally:
        team_manager.pop_cron_completion(session_id)


def _try_finish_cron_team_stream(
    channel_id: str | None,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """End persistent team streams for cron once workflow completes and leader reports."""
    team_manager = get_team_manager(channel_id)
    waiters = team_manager.get_waiters(session_id)
    if not any(_is_cron_request_id(request_id) for request_id, _ in waiters):
        return

    completion = team_manager.setdefault_cron_completion(
        session_id,
        {
            **new_cron_team_round_state(),
            "round_id": None,
            "finish_scheduled": False,
        },
    )
    apply_cron_team_round_event(completion, event)
    if isinstance(event, dict) and str(event.get("event_type") or "").strip() == "chat.final":
        completion["round_id"] = event.get("rid")

    if cron_team_round_should_end(completion) and not completion.get("finish_scheduled"):
        completion["finish_scheduled"] = True
        round_id = completion.get("round_id")
        if _cron_solo_harness_end_pending(completion):
            asyncio.create_task(
                _finish_cron_team_stream_after_delegation_grace(
                    channel_id,
                    session_id,
                    round_id,
                ),
                name=f"cron-team-grace-{session_id}",
            )
            return
        asyncio.create_task(
            _finish_cron_team_stream_after_round(
                channel_id,
                session_id,
                round_id,
            ),
            name=f"cron-team-finish-{session_id}",
        )


_TEAM_BUILDING_EVENT_TYPES = frozenset({
    "team.member", "team.task", "workflow.updated",
})


async def _broadcast_event(
    channel_id: str | None, session_id: str, event: dict[str, Any]
) -> None:
    """Broadcast an event to all request queues waiting on the same session."""
    tm = get_team_manager(channel_id)
    if event and event.get("event_type") == 'team.error':
        event.update({"event_type": "chat.error"})
    result = tm.broadcast_event(session_id, event)
    if inspect.isawaitable(result):
        await result
    # Track team-building events so chat.final can be gated correctly.
    if (not tm.has_seen_team_events(session_id)) and event.get("event_type") in _TEAM_BUILDING_EVENT_TYPES:
        tm.mark_seen_team_events(session_id)
    _try_finish_cron_team_stream(channel_id, session_id, event)


def _approval_chunk_from_event(evt: Any) -> dict[str, Any] | None:
    parsed = parse_stream_chunk(evt)
    if not isinstance(parsed, dict) or parsed.get("event_type") != "chat.ask_user_question":
        return None
    request_id = parsed.get("request_id")
    questions = parsed.get("questions")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    if not isinstance(questions, list) or not questions:
        return None
    return parsed


async def _broadcast_team_state_snapshot(
    channel_id: str | None,
    session_id: str,
) -> None:
    """Broadcast a snapshot of all member and task states.

    Called before ``team.completed`` so the frontend receives the final
    state (e.g. members transitioning from "busy" to "ready") even when
    the monitor events arrive after the has_stream_task loop exits.

    Each snapshot event is also persisted via ``_persist_team_history_event``,
    mirroring the behaviour of ``_consume_monitor_events``.
    """
    try:
        team_manager = get_team_manager(channel_id)
        monitor_handler = team_manager.get_monitor_handler(session_id)
        if monitor_handler is None:
            return
        snapshot = await monitor_handler.get_team_snapshot()
        if snapshot is None:
            return
        team_id = snapshot.get("team_id", "")

        # Broadcast member status snapshot
        for m in snapshot.get("members", []):
            event = {
                "event_type": "team.member",
                "session_id": session_id,
                "event": {
                    "type": "team.member.status_changed",
                    "team_id": team_id,
                    "member_id": m["member_id"],
                    "new_status": m["status"],
                },
            }
            _persist_team_history_event(channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)

        # Broadcast task status snapshot
        for t in snapshot.get("tasks", []):
            event = {
                "event_type": "team.task",
                "session_id": session_id,
                "event": {
                    "type": "team.task.status_snapshot",
                    "team_id": team_id,
                    "task_id": t["task_id"],
                    "status": t["status"],
                    "assignee": t.get("assignee"),
                    "title": t.get("title"),
                    "content": t.get("content"),
                    "title_truncated": t.get("title_truncated"),
                    "title_original_size": t.get("title_original_size"),
                    "content_truncated": t.get("content_truncated"),
                    "content_original_size": t.get("content_original_size"),
                },
            }
            _persist_team_history_event(channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)
    except Exception:
        logger.debug(
            "[TeamHelpers] failed to broadcast team state snapshot: session_id=%s",
            session_id,
        )


def _approval_result_from_event_or_items(
    *,
    skill_name: str,
    event: Any,
    items: list[Any],
    no_changes_output: str,
    invalid_output: str,
) -> dict[str, Any]:
    approval_chunk = _approval_chunk_from_event(event)
    if approval_chunk is not None:
        questions = approval_chunk.get("questions", [])
        return {
            "output": f"Skill '{skill_name}' 演进请求已生成，请在审批弹框中确认。",
            "result_type": "answer",
            "approval_chunks": [approval_chunk],
            "question_count": len(questions),
        }
    if not items:
        return {
            "output": no_changes_output,
            "result_type": "answer",
        }
    return {"output": invalid_output, "result_type": "error"}


def _is_leader_output(chunk: Any) -> bool:
    """Return whether a team OutputSchema chunk should be shown to claw users."""
    chunk_type = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)
    # team.runtime_ready and team.completed are leader-level control events
    # that carry no per-member content but must be forwarded to the frontend.
    if chunk_type == "message" and isinstance(payload, dict):
        event_type_str = payload.get("event_type")
        if event_type_str in ("team.runtime_ready", "team.completed"):
            return True
    if chunk_type == "team.runtime_ready":
        return True

    role = getattr(chunk, "role", None)
    if role is None:
        return True
    if role == TeamRole.LEADER:
        return True

    role_value = getattr(role, "value", role)
    return str(role_value).strip().lower() == TeamRole.LEADER.value


def _is_teammate_output(chunk: Any) -> bool:
    """Return whether a team OutputSchema chunk is from a non-leader member."""
    role = getattr(chunk, "role", None)
    if role is None:
        return False
    if role == TeamRole.LEADER:
        return False
    role_value = getattr(role, "value", role)
    return str(role_value).strip().lower() != TeamRole.LEADER.value


def _leader_task_completion_text(chunk: Any) -> str:
    """提取 leader 轮末 task_completion 的答案全文。

    leader 每个执行回合以 ``controller_output``/``task_completion`` 收尾（payload.data
    为 JsonDataFrame 列表，data.output 即该轮答案）；``parse_stream_chunk`` 对这类
    chunk 返回 None（静默丢弃）。纯文本/无工作流回合因此没有任何收尾信号
    （无 chat.final、team.completed 只在 workflow 完成时发），流空转到超时——
    本 helper 供消费循环补出 chat.final。
    """
    if getattr(chunk, "type", None) != "controller_output":
        return ""
    payload = getattr(chunk, "payload", None)
    inner_t = getattr(payload, "type", None) if payload is not None else None
    if inner_t is None and isinstance(payload, dict):
        inner_t = payload.get("type")
    inner_val = getattr(inner_t, "value", inner_t)
    if str(inner_val or "").strip().lower() != "task_completion":
        return ""
    data = getattr(payload, "data", None)
    if data is None and isinstance(payload, dict):
        data = payload.get("data", [])
    if not isinstance(data, (list, tuple)):
        return ""
    for item in data:
        item_data = getattr(item, "data", None)
        if isinstance(item_data, dict):
            output = item_data.get("output")
            if isinstance(output, str) and output.strip():
                return output
    return ""


def _enrich_teammate_event(parsed: dict[str, Any], chunk: Any) -> dict[str, Any]:
    """Enrich a parsed teammate event with role and source_member for frontend display."""
    parsed["role"] = TeamRole.TEAMMATE.value
    # TeamOutputSchema uses source_member (not member_name) for the member identifier
    source_member = getattr(chunk, "source_member", None)
    if source_member:
        parsed["member_name"] = str(source_member)
    return parsed


_TEAM_TOOL_RESULT_TEXT_LIMIT = 512


def _truncate_team_tool_result_event(parsed: dict[str, Any]) -> dict[str, Any]:
    """Trim large team tool result fields before forwarding them to clients."""
    if parsed.get("event_type") != "chat.tool_result":
        return parsed

    next_event = dict(parsed)
    truncated = False
    original_size = 0
    for key in ("result", "raw_output"):
        value = next_event.get(key)
        if not isinstance(value, str):
            continue
        original_size += len(value)
        if len(value) <= _TEAM_TOOL_RESULT_TEXT_LIMIT:
            continue
        next_event[key] = value[:_TEAM_TOOL_RESULT_TEXT_LIMIT]
        truncated = True

    if truncated:
        next_event["truncated"] = True
        next_event["original_size"] = original_size
    return next_event


def _is_duplicate_ask_user_question(
    parsed: dict[str, Any],
    emitted_request_ids: set[str],
) -> bool:
    if parsed.get("event_type") != "chat.ask_user_question":
        return False
    request_id = str(parsed.get("request_id") or "").strip()
    if not request_id:
        return False
    if request_id in emitted_request_ids:
        return True
    emitted_request_ids.add(request_id)
    return False


def _team_processing_done_chunk(
    request_id: str,
    channel_id: str | None,
    session_id: str,
) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id=channel_id,
        payload={
            "event_type": "chat.processing_status",
            "session_id": session_id,
            "is_processing": False,
            "is_complete": True,
        },
        is_complete=False,
    )


def _group_team_evolution_approvals(
    session_id: str,
    events: list[Any],
) -> tuple[dict[str, list[Any]], list[str]]:
    def _warn_missing_request_id(warn_session_id: str) -> None:
        logger.warning(
            "[TeamHelpers] team evolution approval missing request_id: session_id=%s",
            warn_session_id,
        )

    return group_evolution_approvals(
        session_id,
        events,
        warn_missing_request_id=_warn_missing_request_id,
    )


def ensure_team_evolution_watcher(
    channel_id: str | None,
    session_id: str,
    *,
    source: str = "unknown",
) -> None:
    """Launch the per-session team evolution monitor once the team session is ready."""
    tm = get_team_manager(channel_id)
    watcher = tm.get_team_evolution_watcher(session_id)
    if watcher is not None and not watcher.done():
        logger.info(
            "[TeamHelpers] evolution monitor already running: channel_id=%s session_id=%s source=%s",
            channel_id,
            session_id,
            source,
        )
        return

    rail = tm.get_team_skill_rail(session_id)
    if rail is None:
        mark_deferred = getattr(tm, "mark_team_evolution_watcher_deferred", None)
        if callable(mark_deferred):
            mark_deferred(session_id)
        logger.warning(
            "[TeamHelpers] no TeamSkillEvolutionRail found, evolution watcher launch deferred: session_id=%s source=%s",
            session_id,
            source,
        )
        return
    if not rail.signal_trigger and not rail.review_trigger:
        logger.info(
            "[TeamHelpers] evolution monitor skipped because team evolution is disabled: "
            "channel_id=%s session_id=%s source=%s",
            channel_id,
            session_id,
            source,
        )
        return

    logger.info(
        "[TeamHelpers] launching evolution monitor: channel_id=%s session_id=%s source=%s",
        channel_id,
        session_id,
        source,
    )
    task = asyncio.create_task(
        _watch_team_evolution_and_push(channel_id, session_id, rail)
    )
    setattr(task, "_team_channel_id", channel_id)
    setattr(task, "_team_session_id", session_id)
    task.add_done_callback(_on_team_watcher_done)
    tm.register_team_evolution_watcher(session_id, task)



async def _handle_team_slash_command(
    channel_id: str | None,
    session_id: str,
    query: str,
    *,
    defer_missing_rail: bool = False,
    skills_dir: str | list[str] | None = None,
    language: str = "cn",
) -> dict[str, Any] | None:
    """Handle team-only slash commands before entering the team stream."""
    stripped = str(query or "").strip()
    if not (
        stripped.startswith("/evolve_list")
        or stripped.startswith("/evolve_rebuild")
        or stripped.startswith("/evolve_rollback")
        or stripped.startswith("/evolve_simplify")
        or stripped == "/evolve"
        or stripped.startswith("/evolve ")
    ):
        return None

    if stripped == "/evolve":
        return {
            "output": "请补充 Skill 名称：`/evolve <skill_name> [user_query]`",
            "result_type": "error",
        }

    resolved_skills_dir = skills_dir or _resolve_team_slash_skills_dir(session_id)
    if resolved_skills_dir is None:
        if defer_missing_rail:
            return None
        return {
            "output": "团队技能演进不可用：未找到团队 Skill 目录。",
            "result_type": "error",
        }

    return await handle_evolution_slash_command(
        stripped,
            EvolutionSlashContext(
                mode="team",
                session_id=session_id,
                skills_dir=resolved_skills_dir,
                evolution_enabled=True,
                language=language,
        ),
    )


def _resolve_team_slash_skills_dir(session_id: str) -> str | None:
    metadata = get_session_metadata(session_id)
    team_name = str(metadata.get("team_name") or "").strip()
    if not team_name:
        return None
    return str(team_home(team_name) / "team-workspace" / "skills")


def _team_spec_skills_dir(team_spec: Any) -> str:
    workspace = getattr(team_spec, "workspace", None)
    root_path = str(getattr(workspace, "root_path", "") or "").strip()
    if root_path:
        return str(Path(root_path) / "skills")
    team_name = str(getattr(team_spec, "team_name", "") or "").strip()
    return str(team_home(team_name) / "team-workspace" / "skills")


def _team_spec_monitor_roots(team_spec: Any, session_id: str | None = None) -> list[str]:
    """Return team/member workspace roots where file-op history may be written."""
    roots: list[str] = []

    def add_root(value: Any) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        try:
            root = str(Path(raw).expanduser().resolve())
        except Exception:
            root = raw
        if root not in roots:
            roots.append(root)

    workspace = getattr(team_spec, "workspace", None)
    root_path = str(getattr(workspace, "root_path", "") or "").strip()
    team_name = str(getattr(team_spec, "team_name", "") or "").strip()
    home = team_home(team_name)
    add_root(root_path or str(home / "team-workspace"))
    add_root(home / "workspaces")
    if session_id and team_name:
        # 与读取侧 get_session_extra_history_roots / team_session_worktrees_dir 对齐:
        # 对 session_id 做 sanitize,避免含特殊字符时持久化"幽灵路径"
        # (raw sid 未经 sanitize,与实际 worktree 目录不一致)。
        # 此处用已 import 的 team_home(可被测试 patch)而非 team_session_worktrees_dir
        # (后者内部调用 openjiuwen 自身的 team_home,无法被 monkeypatch)。
        add_root(home / "sessions" / _safe_team_path_segment(session_id) / "worktrees")

    agents = getattr(team_spec, "agents", None)
    if isinstance(agents, dict):
        for member_name, member_spec in agents.items():
            member_workspace = getattr(member_spec, "workspace", None)
            add_root(getattr(member_workspace, "root_path", None))
            add_root(home / "workspaces" / f"{member_name}_workspace")
            # 兜底: member 可能使用 independent_member_workspace(位于 team_home 之外,
            # get_openjiuwen_home()/{member}_workspace),仅靠上面的 home/workspaces
            # 无法覆盖,需显式补上,否则该 member 的 file_ops 不会被收集。
            try:
                add_root(str(independent_member_workspace(str(member_name))))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[TeamHelpers] failed to resolve independent member workspace: "
                    "member=%s error=%s",
                    member_name,
                    exc,
                )

    return roots


def _persist_team_file_monitor_roots(session_id: str, team_spec: Any) -> None:
    roots = _team_spec_monitor_roots(team_spec, session_id=session_id)
    if not roots:
        return
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            _enqueue_write,
            _read_metadata,
        )

        metadata = _read_metadata(session_id, cache_bust=True)
        if not metadata:
            for _ in range(3):
                time.sleep(0.05)
                metadata = _read_metadata(session_id, cache_bust=True)
                if metadata:
                    break
            if not metadata:
                # metadata.json 尚未初始化: 此时无法持久化 team_file_monitor_roots。
                # 读取侧 get_session_extra_history_roots 会基于 team_name 兜底推断标准
                # 布局路径,功能不丢失,但记录 warning 便于排查 metadata 初始化时序问题。
                logger.warning(
                    "[TeamHelpers] cannot persist team_file_monitor_roots: "
                    "metadata not initialized, session=%s",
                    session_id,
                )
                return
        existing = metadata.get("team_file_monitor_roots")
        # 直接替换而非合并: team_spec 是当前 team 组成的权威来源,
        # 合并旧 root 会导致已移除成员的 workspace 路径累积无法清理。
        if roots == existing:
            return
        metadata["team_file_monitor_roots"] = roots
        _enqueue_write(
            session_id,
            metadata,
            preserve_pin_fields=True,
            sync_write=True,
        )
    except Exception as exc:  # noqa: BLE001
        # 写盘失败会影响 last_turn 文件追踪(读取侧只能靠 team_name 兜底推断,
        # 无法覆盖 independent_member_workspace 等非标准布局),升级为 warning
        # 以便在日志中及时发现。
        logger.warning(
            "[TeamHelpers] failed to persist team file monitor roots: session=%s error=%s",
            session_id,
            exc,
        )


async def _start_team_stream_round(
    *,
    channel_id: str | None,
    session_id: str,
    request_id: str,
    team_manager: Any,
    team_name: str,
    team_spec: Any,
    query: str,
    hide_dm: bool = False,
    debug: bool = False,
    source: str = "first",
) -> asyncio.Queue:
    """Start a team stream round and register its waiter queue."""
    # Sync team observability with current config before streaming.
    # Runner.run_agent_team_streaming auto-attaches handlers when
    # is_initialized() is True; this call ensures init/shutdown
    # matches the latest config toggle.
    from jiuwenswarm.agents.harness.team.team_manager import sync_team_observability

    sync_team_observability()
    await team_manager.prepare_runtime_activation(session_id, team_name)
    request_queue = _new_team_event_queue()
    team_manager.add_waiter(session_id, request_id, request_queue)
    logger.info(
        "[TeamHelpers] %s team request: channel_id=%s session_id=%s",
        source,
        _resolve_channel_id(channel_id),
        session_id,
    )

    stream_envs: dict[str, Any] = {}
    if hide_dm:
        stream_envs["hide_dm"] = True
    if debug:
        stream_envs[_STREAM_TRACE_ENV_KEY] = "1"
    round_id = increment_session_round_count(session_id)
    stream_task = asyncio.create_task(
        _consume_stream_with_query(
            channel_id,
            session_id,
            team_spec,
            query,
            round_id=round_id,
            envs=stream_envs or None,
        )
    )
    team_manager.register_stream_task(session_id, stream_task)
    return request_queue


async def process_team_message_stream(
    request: Any,
    inputs: dict[str, Any],
    deep_agent: DeepAgent,
) -> AsyncIterator[AgentResponseChunk]:
    """Process a team-mode streaming request."""
    session_id = request.session_id or "default"
    rid = request.request_id
    channel_id = request.channel_id

    team_manager = get_team_manager(channel_id)
    language = _resolve_request_language(request)
    query = _normalize_team_query(
        inputs.get("query", ""),
        channel_id=channel_id,
        language=language,
    )
    query_text = query if isinstance(query, str) else ""
    try:
        from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
            wait_for_pending_shutdown_cleanup_for_session,
        )

        await wait_for_pending_shutdown_cleanup_for_session(session_id)
    except Exception as exc:
        logger.warning(
            "[TeamHelpers] waiting for pending shutdown cleanup failed: session_id=%s error=%s",
            session_id,
            exc,
        )
    # is_first_request 判断：
    # 1. stream task 存在 → False
    # 2. 已有同 session 的 waiter → False
    # 3. session 已初始化过 team runtime → False
    # 4. 否则 → True（首次请求，需要创建 team spec + stream）
    has_active_waiters = team_manager.has_waiters(session_id)
    is_first_request = (
        not team_manager.has_stream_task(session_id)
        and not has_active_waiters
        and not team_manager.is_session_initialized(session_id)
    )
    request_queue: asyncio.Queue | None = None

    hide_dm = False
    debug = False
    if is_first_request:
        preparation = await _prepare_first_team_request(
            team_manager=team_manager,
            session_id=session_id,
            channel_id=channel_id,
            request_id=rid,
            query=query,
        )
        if preparation.error_chunks is not None:
            for chunk in preparation.error_chunks:
                yield chunk
            return
        if preparation.recovered_runtime:
            is_first_request = False
        else:
            query = preparation.query
            query_text = query if isinstance(query, str) else ""
            hide_dm = preparation.hide_dm
            debug = preparation.debug

    try:
        request_metadata = dict(request.metadata or {})
        # V2: 若请求携带 member_name（由 Gateway resolve_member_by_user 反查注入），
        # 在前拼接 $sender，让 OpenJiuwen 识别发言人身份。
        # 规则：
        #   - 消息中有 @mention → $member_name @target body（保留显式 @）
        #   - 消息中无 @ → $member_name body（不自动拼接 @team_leader，
        #     HumanAgent 可直接与自己扮演的 agent 对话，如 $reviewer-1 看一下当前有哪些任务）
        member_name = str(request_metadata.get("member_name") or "").strip()
        if member_name and query_text and not query_text.startswith("$"):
            query = f"${member_name} {query_text}"
            query_text = query if isinstance(query, str) else str(query)
            logger.info(
                "[TeamHelpers] prefixed query with member identity: member=%s session=%s query_preview=%s",
                member_name,
                session_id,
                _safe_query_preview(query),
            )
        request_metadata = build_team_request_metadata(request)
        resolved_mode = str(request_metadata.get("mode") or "").strip()
        # Page-selected model name (from chat page model selector). Used as a
        # fallback for team members whose ``modes.team.agents.*.model`` is not
        # explicitly configured, so cluster mode honors the page model when no
        # per-agent model is set in config.yaml.
        params_obj = getattr(request, "params", None)
        requested_model_name = (
            str(params_obj.get("model_name") or "").strip()
            if isinstance(params_obj, dict)
            else ""
        ) or None
        # Provider-based assembly: build members from the shared config source,
        # no pre-built parent DeepAgent required.
        team_spec = await team_manager.get_swarm_enriched_team_spec(
            session_id=session_id,
            mode=resolved_mode,
            project_dir=request_metadata.get("project_dir"),
            request_id=rid,
            channel_id=channel_id,
            request_metadata=request_metadata,
            requested_model_name=requested_model_name,
        )
        _persist_team_file_monitor_roots(session_id, team_spec)
    except Exception as exc:
        logger.exception("[TeamHelpers] TeamAgent create failed: %s", exc)
        # 终帧即错误帧（is_complete=True 的 chat.error）：gateway_normalize 把
        # is_complete 且 event_type=chat.error 的 chunk 转换为 failed 终帧。
        # 原先错误帧非终态、随后补 payload=None 的空终帧 → e2a.complete succeeded，
        # 前端把建立失败的回合记成"已完成"
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=channel_id,
            payload={"event_type": "chat.error", "error": str(exc)},
            is_complete=True,
        )
        return

    team_name = team_spec.team_name
    team_skills_dir = _team_spec_skills_dir(team_spec)
    ensure_ready = getattr(team_manager, "ensure_team_shared_skills_ready_for_session", None)
    shared_skills_ready_prepared = False
    if is_first_request and callable(ensure_ready):
        ensure_ready(session_id, team_spec)
        shared_skills_ready_prepared = True

    slash_result = await _handle_team_slash_command(
        channel_id,
        session_id,
        query_text,
        skills_dir=team_skills_dir,
    )
    if slash_result is not None:
        approval_chunks = slash_result.get("approval_chunks")
        if isinstance(approval_chunks, list) and approval_chunks:
            for chunk in approval_chunks:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload=chunk,
                    is_complete=False,
                )
            yield _team_processing_done_chunk(rid, channel_id, session_id)
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=channel_id,
                payload={"event_type": "chat.done"},
                is_complete=True,
            )
            return

        prompt = str(slash_result.get("followup_prompt", "") or "").strip()
        if prompt:
            query = prompt
        else:
            slash_result = evolution_slash_result(
                evolution_slash_command_name(query_text),
                slash_result,
                warning_phrases=TEAM_EVOLUTION_SLASH_WARNING_PHRASES,
            )
            result_type = str(slash_result.get("result_type", "answer")).strip().lower()
            content = str(slash_result.get("output", ""))
            slash_meta = {
                "source": slash_result.get("source"),
                "slash_command": slash_result.get("slash_command"),
                "display_level": slash_result.get("display_level"),
            }
            payload = (
                {"event_type": "chat.error", "error": content, **slash_meta}
                if result_type == "error"
                else {"event_type": "chat.final", "content": content, **slash_meta}
            )
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=channel_id,
                payload=payload,
                is_complete=False,
            )
            yield _team_processing_done_chunk(rid, channel_id, session_id)
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=channel_id,
                payload=None,
                is_complete=True,
            )
            return

    try:
        first_request_source = "first"
        if not is_first_request:
            logger.info(
                "[TeamHelpers] follow-up team request: channel_id=%s session_id=%s",
                _resolve_channel_id(channel_id),
                session_id,
            )
            # V2: follow-up 不创建 waiter —— 一个 session 只保留一个 waiter（原始 stream 的）。
            # follow-up 的唯一目的是 interact() 把 query 发给 team，
            # 后续的 team events 由原始 waiter 的 while 循环产出，
            # 通过 Gateway 的 fan_out 路由机制分发到各 channel。
            # 之前 follow-up 也创建 waiter 导致 _broadcast_event 广播到两个 queue，
            # 同一事件被 yield 两次 → Gateway dispatch 两次 → 重复消息。
            if query:
                success, reason = await _interact_with_request_metadata(
                    team_manager,
                    session_id,
                    query,
                    request_metadata,
                )
                if not success:
                    logger.warning(
                        "[TeamHelpers] interact failed: channel_id=%s session_id=%s reason=%s query=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        reason,
                        _safe_query_preview(query),
                    )
                    first_request_ready = False
                    if _is_followup_delivery_boundary_reason(reason):
                        boundary_result = await _deliver_followup_with_request_metadata(
                            team_manager,
                            session_id,
                            query,
                            request_metadata=request_metadata,
                            initial_reason=reason,
                        )
                        success = boundary_result.success
                        reason = boundary_result.reason
                        first_request_ready = boundary_result.first_request_ready
                        # 「运行时残留 + gate 已关」自愈：拆除卡死/中断留下的残留条目
                        # （pool entry 与流标记还在、interact_gate 已关）会让 follow-up
                        # 永远撞 gate_closed、永不自愈（用户只能重启进程）——主动拆除
                        # 残留运行时，让本请求落入冷重建（recover_team 唤醒）而非报错。
                        if (
                            not success
                            and not first_request_ready
                            and (reason or "") == "gate_closed"
                            and await _team_session_has_runtime(team_manager, session_id)
                        ):
                            logger.warning(
                                "[TeamHelpers] stale runtime with closed gate detected, "
                                "terminating for cold rebuild: channel_id=%s session_id=%s",
                                _resolve_channel_id(channel_id),
                                session_id,
                            )
                            try:
                                await team_manager.terminate_session_runtime(
                                    session_id, reason="gate_closed stale-entry recovery"
                                )
                            except Exception:
                                logger.warning(
                                    "[TeamHelpers] stale-entry terminate failed: session_id=%s",
                                    session_id,
                                    exc_info=True,
                                )
                            first_request_ready = not await _team_session_has_runtime(
                                team_manager, session_id
                            )
                    if not success and first_request_ready:
                        preparation = await _prepare_first_team_request(
                            team_manager=team_manager,
                            session_id=session_id,
                            channel_id=channel_id,
                            request_id=rid,
                            query=query,
                        )
                        if preparation.error_chunks is not None:
                            for chunk in preparation.error_chunks:
                                yield chunk
                            return
                        is_first_request = not preparation.recovered_runtime
                        if is_first_request:
                            first_request_source = "follow-up fallback"
                            query = preparation.query
                            hide_dm = preparation.hide_dm
                            debug = preparation.debug
                            logger.info(
                                "[TeamHelpers] follow-up interact reclassified by first-request condition: "
                                "channel_id=%s session_id=%s reason=%s",
                                _resolve_channel_id(channel_id),
                                session_id,
                                reason,
                            )
                    elif not success and _is_followup_delivery_boundary_reason(reason):
                        reason = reason or "gate_closed"
                    if not success and not is_first_request:
                        final_reason = reason or ""
                        # gate_closed 曾在此静默结束流（视为 shutdown race）——但消息实际
                        # 未投递到任何成员（上方投递重试已耗尽），静默 = 用户消息被吞：
                        # 前端收到空终帧当成功空回合，整轮卡片（含身份行/头像）消失。
                        # 与其他失败原因同规：先广播错误再收尾，前端留痕。
                        error_msg = _INTERACT_REASON_ERROR_MAP.get(
                            final_reason,
                            "Failed to send message, please try again later",
                        )
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload={
                                "event_type": "chat.error",
                                "error": error_msg,
                            },
                            is_complete=False,
                        )
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload=None,
                            is_complete=True,
                        )
                        return

            if not is_first_request:
                if _is_cron_request_id(rid):
                    request_queue = _new_team_event_queue()
                    team_manager.add_waiter(session_id, rid, request_queue)
                    logger.info(
                        "[TeamHelpers] cron follow-up team request waits for round: "
                        "channel_id=%s session_id=%s request_id=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        rid,
                    )
                    round_state = new_cron_team_round_state()
                    try:
                        async for event in _wait_for_cron_team_round_events(
                            request_queue=request_queue,
                            round_state=round_state,
                            request_id=rid,
                            channel_id=channel_id,
                            session_id=session_id,
                        ):
                            _cron_agent_ref, _cron_meta = _build_team_event_chunk_meta(event)
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=channel_id,
                                payload=event,
                                agent_ref=_cron_agent_ref,
                                metadata=_cron_meta,
                                is_complete=False,
                            )
                    finally:
                        team_manager.remove_waiter(session_id, rid)
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=channel_id,
                        payload=None,
                        is_complete=True,
                    )
                    return

                logger.info(
                    "[TeamHelpers] follow-up request submitted without waiter: "
                    "channel_id=%s session_id=%s request_id=%s",
                    _resolve_channel_id(channel_id),
                    session_id,
                    rid,
                )
                # NOTE: do NOT emit is_processing=False here.
                # A follow-up request only enqueues the query into the running
                # team stream; the actual LLM work still happens inside
                # _consume_stream_with_query. The real "round complete" signal
                # will be broadcast by that background stream once team.completed
                # arrives, and forwarded to the frontend via the long-lived
                # waiter that was registered by the first request.
                # The deferred placeholder below tells the Gateway not to
                # auto-emit is_processing=False when this short stream ends,
                # which prevents the frontend from flashing
                # "finished -> wait -> running again" before the LLM replies.
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload={
                        "event_type": "chat.processing_status_deferred",
                        "session_id": session_id,
                    },
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload=None,
                    is_complete=True,
                )
                return

        if is_first_request:
            if callable(ensure_ready) and not shared_skills_ready_prepared:
                ensure_ready(session_id, team_spec)
                shared_skills_ready_prepared = True
            request_queue = await _start_team_stream_round(
                channel_id=channel_id,
                session_id=session_id,
                request_id=rid,
                team_manager=team_manager,
                team_name=team_name,
                team_spec=team_spec,
                query=query,
                hide_dm=hide_dm,
                debug=debug,
                source=first_request_source,
            )

        try:
            if _is_cron_request_id(rid) and request_queue is not None:
                cron_round_state = new_cron_team_round_state()
                async for event in _wait_for_cron_team_round_events(
                    request_queue=request_queue,
                    round_state=cron_round_state,
                    request_id=rid,
                    channel_id=channel_id,
                    session_id=session_id,
                ):
                    _cron_agent_ref, _cron_meta = _build_team_event_chunk_meta(event)
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=channel_id,
                        payload=event,
                        agent_ref=_cron_agent_ref,
                        metadata=_cron_meta,
                        is_complete=False,
                    )
            else:
                # while 循环：仅 first-request 使用，依赖 stream_task 生命周期。
                # follow-up 已在上方 return，不再进入此循环。
                while team_manager.has_stream_task(session_id):
                    if request_queue is None:
                        break
                    try:
                        event = await asyncio.wait_for(request_queue.get(), timeout=0.1)
                        # ── 统一推导 (agent_ref, fan_out_targets) ──
                        _agent_ref, _metadata = _build_team_event_chunk_meta(event)
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload=event,
                            agent_ref=_agent_ref,
                            metadata=_metadata,
                            is_complete=False,
                        )
                        if isinstance(event, dict) and event.get("event_type") == "team.error":
                            break
                    except asyncio.TimeoutError:
                        if not team_manager.has_stream_task(session_id):
                            break
                        continue
                # Drain any events that were enqueued by
                # _consume_stream_with_query but not yet read when the
                # has_stream_task loop exited.  This can happen when
                # _consume_stream_with_query's finally block calls
                # pop_stream_task (making has_stream_task return False)
                # in the same async frame that it broadcast
                # chat.processing_status / team.completed into
                # request_queue.  Without this drain, those events would
                # be lost and the frontend would never receive
                # is_complete=True.
                if request_queue is not None:
                    drained = 0
                    while True:
                        try:
                            event = request_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        drained += 1
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload=event,
                            is_complete=False,
                        )
                        if isinstance(event, dict):
                            if event.get("event_type") == "team.error":
                                break
                    if drained:
                        logger.info(
                            "[TeamHelpers] drained remaining events after has_stream_task loop: "
                            "channel_id=%s session_id=%s request_id=%s drained=%s",
                            _resolve_channel_id(channel_id),
                            session_id,
                            rid,
                            drained,
                        )
        except asyncio.CancelledError:
            logger.info(
                "[TeamHelpers] event stream cancelled: channel_id=%s session_id=%s request_id=%s",
                _resolve_channel_id(channel_id),
                session_id,
                rid,
            )
            raise
        except Exception as exc:
            logger.exception(
                "[TeamHelpers] event stream failed: channel_id=%s session_id=%s error=%s",
                _resolve_channel_id(channel_id),
                session_id,
                exc,
            )
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=channel_id,
                payload={"event_type": "chat.error", "error": str(exc)},
                is_complete=False,
            )

        yield AgentResponseChunk(
            request_id=rid,
            channel_id=channel_id,
            payload=None,
            is_complete=True,
        )
        # 当前 stream 已结束，清除初始化标记，
        # 下次请求需重新创建 stream task（gate 在 stream 结束时已关闭）。
        team_manager.clear_session_initialized(session_id)
        team_manager.reset_seen_team_events(session_id)
        team_manager.reset_workflow_completed(session_id)
        logger.info(
            "[TeamHelpers] stream ended, cleared round markers: "
            "channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id), session_id,
        )
    finally:
        if request_queue is not None:
            team_manager.remove_waiter(session_id, rid)
            if _is_cron_request_id(rid):
                team_manager.pop_cron_completion(session_id)
            if not team_manager.has_waiters(session_id):
                logger.info(
                    "[TeamHelpers] cleared waiter set: session_id=%s",
                    session_id,
                )


# leader 模型错误后判定回合死亡的静默窗口：窗口内主流零产出（chunk 计数不涨）
# 且期间无回合收尾信号，才认定本轮死亡。需大于模型重试回退周期（agent-core
# StreamController 的重试间隔为数秒级），30s 覆盖典型回退且不至于让用户等太久。
_LEADER_ROUND_DEATH_PROBE_SEC = 30.0

# 主理人死亡但成员仍在工作时不补终态：成员回报会经 mailbox 唤醒主理人续跑，
# 收尾由恢复后的正常路径负责，探针按窗口续探。上限兜底成员永忙（卡死）
# 导致轮次永不收尾——超时后仍补终态解锁前端输入。
_LEADER_ROUND_DEATH_PROBE_MAX_EXTENSIONS = 20
# 续探判定"成员仍在工作"的执行中状态（MemberStatus.busy 之外的旁证）
_MEMBER_IN_FLIGHT_EXEC_STATUSES = frozenset({"starting", "running", "completing"})


async def _count_in_flight_members(channel_id: str | None, session_id: str) -> int:
    """统计仍有在途工作的成员数（status=busy 或执行状态进行中）。

    快照不可用按 0 处理——退化方向是维持旧行为（补终态），不会更糟。
    （get_team_snapshot 的 members 已剔除 leader，见 team_monitor_handler。）
    """
    try:
        tm = get_team_manager(channel_id)
        monitor_handler = tm.get_monitor_handler(session_id)
        if monitor_handler is None:
            return 0
        snapshot = await monitor_handler.get_team_snapshot()
        if not snapshot:
            return 0
        count = 0
        for m in snapshot.get("members", []):
            if (
                m.get("status") == "busy"
                or m.get("execution_status") in _MEMBER_IN_FLIGHT_EXEC_STATUSES
            ):
                count += 1
        return count
    except Exception:
        logger.debug(
            "[TeamHelpers] count in-flight members failed: session_id=%s",
            session_id,
            exc_info=True,
        )
        return 0


def _schedule_leader_round_death_probe(
        channel_id: str | None,
        session_id: str,
        round_id: Any,
        *,
        error_text: str,
        liveness: Any,
        completion_signals: Any,
        previous: asyncio.Task | None,
        on_fired: Any = None,
) -> asyncio.Task:
    """（重新）调度 leader 轮死亡探针，返回新任务供调用方持有/取消。

    团队模式下 chat.error 是非终态警告（流继续），但 leader 重试耗尽转 IDLE 时
    回合实际死亡且无任何终态帧——探针在这个窗口补发 processing_status 终态。
    三个保险：流已终结则不补（正常收尾路径负责）；窗口内有产出则不补（已恢复）；
    窗口内已有回合收尾信号则不补（防误杀已完成回合）。
    第四个判定：探针醒来时仍有成员在途工作（busy/执行中）则不补——成员回报
    会经 mailbox 唤醒主理人续跑，此时补失败终态会让前端解锁输入、把用户追问
    steer 进仍在运行的团队（语义错位），且唤醒轮内容会被前端锁进已死 run
    （error 终态不被迟到 delta 点亮）。改为广播一条等待警告（chat.error 在前端
    映射为非终态横幅）并按窗口续探，直到成员全部落定或达到续探上限。

    liveness / completion_signals: 零参数可调用，分别返回主流已消费 chunk 数与
    已广播的回合收尾信号数（每次错误以最新值为基线）。
    on_fired: 探针实际补发终态后的零参数回调（清除本轮错误史标记，
    防长寿命流的后续轮次误带前一轮的陈旧错误）。
    """
    if previous is not None and not previous.done():
        previous.cancel()

    async def _probe() -> None:
        baseline_chunks = liveness()
        baseline_signals = completion_signals()
        extensions = 0
        waiting_warned = False
        while True:
            try:
                await asyncio.sleep(_LEADER_ROUND_DEATH_PROBE_SEC)
            except asyncio.CancelledError:
                return
            try:
                tm = get_team_manager(channel_id)
                if not tm.has_stream_task(session_id):
                    return
                if liveness() != baseline_chunks or completion_signals() != baseline_signals:
                    return
                # 仍有成员在途工作：回合不算死，广播等待警告（仅首次）并续探
                in_flight = await _count_in_flight_members(channel_id, session_id)
                if in_flight > 0 and extensions < _LEADER_ROUND_DEATH_PROBE_MAX_EXTENSIONS:
                    extensions += 1
                    if not waiting_warned:
                        waiting_warned = True
                        await _broadcast_event(
                            channel_id,
                            session_id,
                            {
                                "event_type": "chat.error",
                                "session_id": session_id,
                                "rid": round_id,
                                "error": (
                                    "主理人模型调用中断，团队仍在运行"
                                    f"（{in_flight} 名成员工作中），成员回报后将自动继续"
                                ),
                            },
                        )
                    logger.info(
                        "[TeamHelpers] leader round dead but members in flight, "
                        "probe extended: channel_id=%s session_id=%s round_id=%s "
                        "in_flight=%s extension=%s/%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        round_id,
                        in_flight,
                        extensions,
                        _LEADER_ROUND_DEATH_PROBE_MAX_EXTENSIONS,
                    )
                    continue
                logger.warning(
                    "[TeamHelpers] leader round presumed dead after model error: "
                    "channel_id=%s session_id=%s round_id=%s idle=%.0fs",
                    _resolve_channel_id(channel_id),
                    session_id,
                    round_id,
                    _LEADER_ROUND_DEATH_PROBE_SEC,
                )
                await _broadcast_event(
                    channel_id,
                    session_id,
                    {
                        "event_type": "chat.processing_status",
                        "session_id": session_id,
                        "rid": round_id,
                        "is_processing": False,
                        "is_complete": True,
                        "error": error_text.strip() or "Leader round did not recover after model error",
                    },
                )
                if callable(on_fired):
                    on_fired()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "[TeamHelpers] leader round death probe failed: session_id=%s",
                    session_id,
                    exc_info=True,
                )
                return

    return asyncio.create_task(_probe(), name=f"leader-death-probe-{session_id}")


async def _consume_stream_with_query(
    channel_id: str | None,
    session_id: str,
    team_spec: Any,
    initial_query: str,
    *,
    round_id: int,
    envs: dict[str, Any] | None = None,
) -> None:
    """Consume the team stream in the background and broadcast parsed events."""
    _envs = envs or {}
    hide_dm: bool = bool(_envs.get("hide_dm", False))
    received_chunks = 0
    # 探针活性计数只数 leader 帧：成员帧（含成员的死讯 chat.error）
    # 与 leader 错误同流到达会污染 received_chunks 基线，导致"leader 耗尽 + 成员
    # 接连死亡"时探针误判"窗口内有活动"自我放弃，回合零终态帧、前端误标已完成。
    # leader 的任何帧（delta/final/tool_call/reasoning 等）都算活性，长工具静默
    # 不误杀（工具执行期本就无帧，与原语义一致）。
    leader_activity_chunks = 0
    first_model_output_at: float | None = None
    emitted_ask_user_request_ids: set[str] = set()
    # 本轮 leader 累计正文（合成 chat.final 的兜底内容；leader final 落定即重置，
    # 防跨轮污染——流是跨用户轮次长寿命的，不重置会把前轮文本带进下一轮气泡）
    leader_round_text = ""
    # leader 发言/思考的泡序号（多气泡分卡边界：同一用户轮内 leader 多次发言各成一卡；
    # reasoning chunk 带当轮 seq，前端按 seq 把思考归到对应发言卡）
    leader_bubble_seq = 0
    # 当前「发言 → task_completion」周期内是否已广播过 leader chat.final：
    # leader 的答案会以 answer chunk（原生 final）与 controller_output/task_completion
    # （同文全文）两种形态各到一次——合成兜底前查此标记，已发过就跳过，
    # 否则同文双发（多气泡时间线下两个 final 各开一卡 → 重复展示、历史双写）；
    # 每次 task_completion 判定后复位，不影响后续无原生 final 的纯文本回合
    leader_final_emitted = False
    # 本周期经 task_completion 合成的 chat.final 文本：部分模型（GLM 实证）的
    # answer 原生帧晚于 task_completion 到达（顺序与上面守卫假设相反），
    # chat.final 分支据此文丢弃同文迟到重复帧——否则同文双发开两卡、历史同 id 双写
    leader_synth_text = ""
    # 纯文本回合的即时收尾标记：仅当本轮无任何团队活动（无团队事件/无工具调用/
    # 无成员产出）且 leader 以 task_completion 收尾时置位——下一轮循环开头 break
    # （运行时暂停，下次发送 resume_from_pause 热恢复）。leader 先答、成员后报的
    # 回合不断流（成员确认实测晚于主理人总结到达）。
    finish_after_final = False
    saw_tool_call = False
    saw_teammate_output = False
    # leader 模型错误后的回合死亡探针任务（chat.error 非终态化的配套：
    # leader 重试耗尽转 IDLE 时回合实际死亡但无任何终态帧——探针在错误后
    # 零产出的情况下补发 processing_status 终态，防前端 run 永远 running）
    death_probe_task: asyncio.Task | None = None
    # 停摆看门狗：死亡探针只管"leader 错误后零产出"；停摆是另一族——
    # leader 正常收尾但任务已下发、零成员在途（重试断片未派发/成员未启动），
    # 回合无人推进且 team.completed 永不成立。回合开始广播后启动，随流回收
    stall_watchdog_task: asyncio.Task | None = None
    # 本流已广播的回合收尾信号数（processing_status is_complete=true 等）：
    # 探针据此判断"错误之后回合已正常收尾"，避免迟到误杀已完成回合
    completion_signals = 0
    # 本轮未恢复的 leader 模型错误史：team.completed 的收尾转换
    # 对错误无感知（零成员/零任务时 is_team_completed 空虚成立，leader 重试
    # 耗尽后立刻触发），不带 error 的 processing_status 会让前端把失败回合
    # 记成"已完成"（还反过来抑制死亡探针）。leader chat.error 时记录；
    # leader 产出实质正文（delta/final 非空）视为恢复并清除；收尾帧转换时
    # 仍挂着则携带 error 字段，让回合以失败收场。
    round_unrecovered_error: str | None = None

    def _clear_round_unrecovered_error() -> None:
        nonlocal round_unrecovered_error
        round_unrecovered_error = None
    # Reset the team-events flag at the start of a new round so chat.final
    # can correctly determine whether the team is active.
    tm_ = get_team_manager(channel_id)
    tm_.reset_seen_team_events(session_id)
    tm_.reset_workflow_completed(session_id)
    lg: TeamStreamLogger | None = None
    stream_cancelled = False
    try:
        logger.info(
            "[TeamHelpers] stream started: channel_id=%s session_id=%s round_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
            round_id,
        )
        # Broadcast a round-start signal so the frontend can mark the
        # current conversation turn as "processing" before any chunks
        # arrive.  Pairs with ``chat.processing_status(is_complete=True)`` on completion.
        await _broadcast_event(
            channel_id,
            session_id,
            {
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "rid": round_id,
                "is_processing": True,
                "is_complete": False,
            },
        )
        stall_watchdog_task = schedule_team_stall_watchdog(
            channel_id,
            session_id,
            round_id,
            # 活性口径用全量流帧（received_chunks，含成员/团队事件）——停摆语义是
            # "无人干事"，与死亡探针的"leader 是否活着"（只数 leader 帧）不同
            liveness=lambda: received_chunks,
            completion_signals=lambda: completion_signals,
            # 广播函数注入（本模块的广播有 team.error 改名/建团事件标记/cron 收尾
            # 副作用）；看门狗模块不反向 import 本文件，防循环依赖
            broadcast=_broadcast_event,
        )
        stream_trace_enabled = bool(
            _envs.get(_STREAM_TRACE_ENV_KEY) or os.environ.get(_STREAM_TRACE_ENV_KEY)
        )
        if stream_trace_enabled:
            traces_dir = get_agent_teams_home() / "traces"
            traces_dir.mkdir(parents=True, exist_ok=True)
            lg = TeamStreamLogger(file_path=str(traces_dir / f"dump-team-{session_id}.txt"))
        # Last stop before the message enters the team runner streaming path.
        server_logger.info(
            "[AgentServer] team message entering runner streaming: channel_id=%s session_id=%s"
            " round_id=%s query=%s",
            _resolve_channel_id(channel_id),
            session_id,
            round_id,
            _safe_query_preview(initial_query),
        )
        runner_entered_at = time.monotonic()
        async for chunk in Runner.run_agent_team_streaming(
            agent_team=team_spec,
            inputs={"query": initial_query},
            session=session_id,
            envs=envs,
            stream_logger=lg,
        ):
            # 纯文本/已完成工作流回合即时收尾：上轮 chat.final 已发 + 回合完成
            # 信号已广播，剩余 trailing chunk（usage 等）不再需要
            if finish_after_final:
                break
            received_chunks += 1
            # First event of any kind from the runner — usually a framework
            # control event (team.runtime_ready and friends), not model output.
            # It marks how long team startup took before the stream came alive.
            if received_chunks == 1:
                server_logger.info(
                    "[AgentServer] team runner streaming first event: channel_id=%s session_id=%s"
                    " round_id=%s elapsed_ms=%.1f role=%s type=%s",
                    _resolve_channel_id(channel_id),
                    session_id,
                    round_id,
                    (time.monotonic() - runner_entered_at) * 1000,
                    getattr(chunk, "role", None),
                    getattr(chunk, "type", None),
                )
            # 诊断：每 30 个 chunk 或首个 chunk 时打印进度
            if received_chunks == 1 or received_chunks % 30 == 0:
                _role = getattr(chunk, "role", None)
                logger.info(
                    "[TeamHelpers] stream progress: channel_id=%s session_id=%s"
                    " received=%s role=%s type=%s",
                    _resolve_channel_id(channel_id), session_id,
                    received_chunks, _role, getattr(chunk, "type", None),
                )
            is_leader = _is_leader_output(chunk)
            is_teammate = _is_teammate_output(chunk)
            if not is_leader and not is_teammate:
                if received_chunks <= 3:
                    logger.info(
                        "[TeamHelpers] stream chunk filtered (non-leader/non-teammate):"
                        " session_id=%s role=%s type=%s",
                        session_id, getattr(chunk, "role", None), getattr(chunk, "type", None),
                    )
                continue
            # Optional: filter out all non-leader frames so the frontend only
            # sees leader output. Leader-level control events
            # (team.runtime_ready / team.completed) are kept because
            # _is_leader_output returns True.
            if _team_hide_teammate_enabled() and not is_leader:
                continue
            # agent-core 自愈重试的提示帧（llm_output + payload.retrying）：
            # 不进文本业务流（防 "[Retry N/10]" 文本污染合成 final 与成员子聊天框），
            # 但也不整条丢弃——改广播为独立的 chat.retry_status 事件（带结构化
            # attempt/max_attempts），前端回合头显示轻量重试状态（
            # 重试风暴期间前端零交代，用户只见"正在生成"）。
            _raw_payload = getattr(chunk, "payload", None)
            if (
                getattr(chunk, "type", None) == "llm_output"
                and isinstance(_raw_payload, dict)
                and _raw_payload.get("retrying")
            ):
                await _broadcast_event(
                    channel_id,
                    session_id,
                    {
                        "event_type": "chat.retry_status",
                        "session_id": session_id,
                        "rid": round_id,
                        "attempt": _raw_payload.get("attempt"),
                        "max_attempts": _raw_payload.get("max_attempts"),
                        "error": str(_raw_payload.get("content") or "").strip(),
                    },
                )
                continue
            parsed = parse_stream_chunk(chunk)
            # leader 轮末 task_completion（携带该轮答案全文）被 parse 丢弃，
            # 纯文本/无工作流回合因此没有 chat.final、没有回合完成信号，流空转到
            # 超时，前后端回合边界错位（后续消息被 follow-up 并入 旧流，两问答案揉进一个气泡）。
            # 这里把它合成 chat.final（本轮 leader
            # 累计正文快照），走下方统一的 chat.final 分支（广播 + 收尾判定）。
            final_from_completion = False
            if parsed is None and is_leader:
                completion_text = _leader_task_completion_text(chunk)
                if completion_text:
                    if leader_final_emitted:
                        # 本周期已广播过原生 chat.final：task_completion 携带的是
                        # 同一段答案全文，再合成即同文双发——跳过并复位周期标记
                        leader_final_emitted = False
                    else:
                        parsed = {
                            "event_type": "chat.final",
                            # 本轮全文优先（task_completion 携带）；delta 累积 buffer 仅兜底
                            "content": completion_text or leader_round_text,
                            "role": TeamRole.LEADER.value,
                        }
                        final_from_completion = True
                        # 记录合成文本：迟到的同文原生 final 在下方 chat.final 分支丢弃
                        leader_synth_text = str(parsed["content"] or "")
                        # 合成即重置：buffer 已完成本轮使命
                        leader_round_text = ""
            if parsed is not None:
                # Time to first token: the first frame actually produced by a
                # model (reasoning counts — on a thinking model it comes first).
                if first_model_output_at is None and parsed.get("event_type") in _MODEL_OUTPUT_EVENT_TYPES:
                    first_model_output_at = time.monotonic()
                    server_logger.info(
                        "[AgentServer] team runner first model output: channel_id=%s session_id=%s"
                        " round_id=%s elapsed_ms=%.1f received=%s event_type=%s role=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        round_id,
                        (first_model_output_at - runner_entered_at) * 1000,
                        received_chunks,
                        parsed.get("event_type"),
                        parsed.get("role") or getattr(chunk, "role", None),
                    )
                if not is_leader and parsed.get("event_type") == "chat.reasoning":
                    continue
                if _is_duplicate_ask_user_question(parsed, emitted_ask_user_request_ids):
                    continue
                # Skip non-leader __interaction__ (permission ASK) — approval
                # is routed internally via the leader; only leader
                # interactions are forwarded to the frontend.
                if not is_leader and parsed.get("event_type") == "chat.ask_user_question":
                    continue
                parsed["rid"] = round_id
                if is_teammate:
                    saw_teammate_output = True
                    parsed = _enrich_teammate_event(parsed, chunk)
                    # 成员文本输出归因落盘（member_name + content），
                    # 供刷新后前端重放成员子聊天框
                    _persist_member_final_output(channel_id, session_id, parsed)
                    # 成员工具事件归因落盘（成员视图工具活动跨刷新恢复）
                    _persist_member_tool_event(channel_id, session_id, parsed)
                elif is_leader:
                    # 探针活性计数：leader 任何帧都算活性
                    leader_activity_chunks += 1
                    # 标记 role=leader，使 _build_logical_targets() 走 godview 兜底
                    # （leader 不在 _ROLE_FANOUT 中，落到 [godview]）。
                    parsed["role"] = TeamRole.LEADER.value
                    # 累计本轮 leader 正文（task_completion 合成 chat.final 的兜底内容）
                    if parsed.get("event_type") == "chat.delta":
                        leader_round_text += str(parsed.get("content") or "")
                    # leader 产出实质正文 = 此前的模型错误已恢复，
                    # 清除未恢复错误史（注意 chat.error 后补的空 chat.final 无正文，
                    # 不会误清）
                    if (
                        parsed.get("event_type") in ("chat.delta", "chat.final")
                        and str(parsed.get("content") or "").strip()
                    ):
                        round_unrecovered_error = None
                    # 多气泡：leader 的 reasoning 打当轮泡序号——
                    # 思考先于发言到达，前端按 seq 把思考归到"紧接着的发言卡"
                    if parsed.get("event_type") == "chat.reasoning":
                        parsed["bubble_seq"] = leader_bubble_seq
                if parsed.get("event_type") == "chat.tool_call":
                    saw_tool_call = True
                parsed = _truncate_team_tool_result_event(parsed)
                if parsed.get("event_type") == "team.runtime_ready":
                    ready_team_name = str(parsed.get("team_name") or team_spec.team_name)
                    activation_kind = str(parsed.get("activation_kind") or "").strip()
                    sync_team_identity_metadata(
                        channel_id=channel_id,
                        session_id=session_id,
                        mode="team",
                        ready_team_name=ready_team_name,
                        activation_kind=activation_kind,
                    )
                    tm = get_team_manager(channel_id)
                    tm.commit_runtime_ready(session_id, ready_team_name)
                    await tm.attach_distributed_hooks_for_runner_runtime(
                        team_name=ready_team_name,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    await ensure_monitor_handlers_for_active_runtime(
                        channel_id,
                        session_id,
                        ready_team_name,
                        hide_dm=hide_dm,
                        enable_swarmflow=bool(getattr(team_spec, "enable_swarmflow", False)),
                    )
                    # 人类成员：注册 team→user 通知回调（消息抵达人类席位 →
                    # team.human_prompt 帧，前端成员视图显示待回答问题）
                    await _register_human_inbound_hooks(
                        channel_id, session_id, ready_team_name
                    )
                    ensure_team_evolution_watcher(
                        channel_id,
                        session_id,
                        source="runtime_ready",
                    )
                elif parsed.get("event_type") == "team.member":
                    # 成员 spawn（含运行时 /join 进来的人类席位）：刷新人类入站回调注册
                    inner_member_event = parsed.get("event")
                    if (
                        isinstance(inner_member_event, dict)
                        and inner_member_event.get("type") == "team.member.spawned"
                    ):
                        await _register_human_inbound_hooks(
                            channel_id, session_id, team_spec.team_name
                        )
                elif parsed.get("event_type") == "team.interact.failed":
                    reason = str(parsed.get("reason") or "").strip()
                    error_msg = _INTERACT_REASON_ERROR_MAP.get(
                        reason,
                        "Failed to send message, please try again later",
                    )
                    logger.warning(
                        "[TeamHelpers] initial team interact failed: "
                        "channel_id=%s session_id=%s reason=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        reason,
                    )
                    await _broadcast_event(
                        channel_id,
                        session_id,
                        {
                            "event_type": "chat.error",
                            "error": error_msg,
                            "reason": reason,
                            "session_id": session_id,
                            "rid": round_id,
                        },
                    )
                    await _broadcast_event(
                        channel_id,
                        session_id,
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": round_id,
                            "is_processing": False,
                            "is_complete": True,
                            # 投递失败已广播 chat.error，收尾帧必须
                            # 带 error，否则前端把失败回合记成"已完成"
                            "error": error_msg,
                        },
                    )
                    round_unrecovered_error = None
                    completion_signals += 1
                    continue
                elif parsed.get("event_type") == "team.completed":
                    # Team completed this round — broadcast a single
                    # round-complete signal that also carries team stats.
                    completion_event = {
                        "event_type": "chat.processing_status",
                        "session_id": session_id,
                        "rid": round_id,
                        "is_processing": False,
                        "is_complete": True,
                        "member_count": parsed.get("member_count"),
                        "task_count": parsed.get("task_count"),
                    }
                    # 收尾帧携带本轮错误史——leader 重试耗尽后
                    # （零成员/零任务时 team.completed 空虚成立）不把失败回合
                    # 标成正常完成；leader 恢复产出后该标记已清除，不受影响
                    if round_unrecovered_error:
                        completion_event["error"] = round_unrecovered_error
                    round_unrecovered_error = None
                    await _broadcast_event(channel_id, session_id, completion_event)
                    completion_signals += 1
                    continue
                elif parsed.get("event_type") == "chat.error":
                    await _broadcast_event(channel_id, session_id, parsed)
                    # 团队轮内故障落盘。chat.error 在团队模式是非终态警告
                    # （此前只广播不落盘，重启后"本轮曾出错"留痕蒸发）；extra.warning
                    # 供前端与终态错误（整轮判失败）区分；成员错误附 member_name 归因。
                    # 重试通知（"模型调用异常…第 N 次重试"）是过程不是结果：广播照发
                    # （前端扫光条），但不落盘——否则一次重试一条留痕，警告聚合计数失真。
                    # 结构化标记优先（retry_notice=True），文案匹配仅兜底旧框架
                    if not _is_retry_notice(parsed):
                        append_history_record(
                            session_id=session_id,
                            request_id=f"{round_id}-error-{int(time.time() * 1000)}",
                            channel_id=_resolve_channel_id(channel_id),
                            role="assistant",
                            event_type="chat.error",
                            content=str(parsed.get("error") or ""),
                            timestamp=time.time(),
                            extra={
                                "warning": True,
                                **(
                                    {"member_name": str(parsed.get("member_name"))}
                                    if parsed.get("member_name")
                                    else {}
                                ),
                            },
                            mode="team",
                        )
                    if is_leader and not _is_retry_notice(parsed):
                        # 注意：重试通知不进本分支——通知是 rail 过程性广播（马上自动重试），
                        # 不是"未恢复错误史"：挂探针会在重试回退窗口（0.5-2s 后还有一次
                        # 模型调用静默期）误补失败终态；round_unrecovered_error 同理，
                        # 通知后恢复产出的清除逻辑（chat.delta/final）才是它的语义边界。
                        # 记录本轮未恢复的 leader 模型错误史，
                        # 供 team.completed 等收尾帧携带（根治"重试耗尽却记已完成"）
                        round_unrecovered_error = str(parsed.get("error") or "")
                        await _broadcast_event(
                            channel_id,
                            session_id,
                            {
                                "event_type": "chat.final",
                                "content": "",
                                "session_id": session_id,
                                "rid": round_id,
                            },
                        )
                        # chat.error 在团队模式是非终态警告（前端只挂横幅不收尾），
                        # 但 leader 重试耗尽转 IDLE 时回合实际死亡且再无终态帧——
                        # 挂死亡探针：窗口内零产出且无收尾信号则补发终态
                        death_probe_task = _schedule_leader_round_death_probe(
                            channel_id,
                            session_id,
                            round_id,
                            error_text=str(parsed.get("error") or ""),
                            liveness=lambda: leader_activity_chunks,
                            completion_signals=lambda: completion_signals,
                            previous=death_probe_task,
                            on_fired=_clear_round_unrecovered_error,
                        )
                    continue
                # chat.final: if team events (team.member / team.task /
                # workflow.updated) have already been broadcast (tracked
                # via TeamManager.seen_team_events), the team is still
                # running — suppress chat.final so the frontend does not
                # prematurely set isProcessing=false.  Exception: once the
                # workflow has completed (workflow_completed=True), chat.final
                # is no longer suppressed and serves as the normal
                # end-of-round signal.  In non-swarmflow mode,
                # workflow_completed stays False so the original behavior
                # is preserved.
                if parsed.get("event_type") == "chat.final":
                    tm_ = get_team_manager(channel_id)
                    should_finish_round = (
                        (not tm_.has_seen_team_events(session_id))
                        or tm_.is_workflow_completed(session_id)
                    )
                    # 多气泡：leader 每次发言各打一卡序号——
                    # 客户端同 seq 覆盖（重试去重）、新 seq 封段开新卡；
                    # 随载荷进历史落盘 extra，刷新后按段恢复
                    if is_leader:
                        # 迟到的原生 final 与本周期合成帧同文（answer chunk 晚于
                        # task_completion 到达）→ 丢弃：不广播、不占泡序号、不落盘，
                        # 防同文双发开两卡 + 历史同 id 双写。比对后即消费合成文本，
                        # 避免误伤后续周期同文的新发言；不同文（真重新生成）正常放行。
                        if (
                            not final_from_completion
                            and leader_synth_text
                            and str(parsed.get("content") or "") == leader_synth_text
                        ):
                            leader_synth_text = ""
                            continue
                        # 标记本周期已发 final（含合成），轮末 task_completion
                        # 据此跳过重复合成
                        leader_final_emitted = True
                        if not final_from_completion:
                            # 原生 final 落定：旧合成文本失效（防跨周期误伤同文新发言）
                            leader_synth_text = ""
                        parsed["bubble_seq"] = leader_bubble_seq
                        leader_bubble_seq += 1
                        # 原生 final 同样意味着本轮落定：重置累积 buffer
                        leader_round_text = ""
                    # Deliver the final content before announcing that the
                    # round is complete. Clients may stop consuming the stream
                    # as soon as processing_status(False) arrives.
                    await _broadcast_event(channel_id, session_id, parsed)
                    if should_finish_round:
                        await _broadcast_event(
                            channel_id,
                            session_id,
                            {
                                "event_type": "chat.processing_status",
                                "session_id": session_id,
                                "rid": round_id,
                                "is_processing": False,
                                "is_complete": True,
                            },
                        )
                        completion_signals += 1
                        # 即时收尾只限「纯文本回合」：本轮无团队事件、无工具调用、
                        # 无成员产出，且收尾信号来自 task_completion（leader 任务真实
                        # 完成）——运行时暂停，下次发送 resume_from_pause 热恢复，不再
                        # 空转等 idle 超时。其余回合不断流：leader 先答、
                        # 成员后报的回合里成员收尾消息晚于主理人总结到达。
                        if (
                            final_from_completion
                            and not tm_.has_seen_team_events(session_id)
                            and not saw_tool_call
                            and not saw_teammate_output
                        ):
                            finish_after_final = True
                    continue
                await _broadcast_event(channel_id, session_id, parsed)

        # If stream ended without any chunks, broadcast an error event
        if received_chunks == 0:
            logger.warning(
                "[TeamHelpers] stream ended with no output: channel_id=%s session_id=%s",
                _resolve_channel_id(channel_id),
                session_id,
            )
            await _broadcast_event(
                channel_id,
                session_id,
                {
                    "event_type": "chat.error",
                    "error": "Team stream ended with no output (possible pool/DB inconsistency or internal error)",
                    "session_id": session_id,
                },
            )
        else:
            logger.info(
                "[TeamHelpers] stream ended: channel_id=%s session_id=%s chunks=%s",
                _resolve_channel_id(channel_id),
                session_id,
                received_chunks,
            )
    except asyncio.CancelledError:
        stream_cancelled = True
        logger.info(
            "[TeamHelpers] stream cancelled: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
        raise
    except Exception as exc:
        logger.error(
            "[TeamHelpers] stream failed: channel_id=%s session_id=%s error=%s",
            _resolve_channel_id(channel_id),
            session_id,
            exc,
            exc_info=True,
        )
        try:
            await _broadcast_event(
                channel_id,
                session_id,
                {
                    "event_type": "chat.error",
                    "error": str(exc),
                    "session_id": session_id,
                },
            )
        except asyncio.CancelledError:
            stream_cancelled = True
            raise
    finally:
        # 回合死亡探针/停摆看门狗随流回收（流结束=回合已有定论，不再需要补终态）
        if death_probe_task is not None and not death_probe_task.done():
            death_probe_task.cancel()
        if stall_watchdog_task is not None and not stall_watchdog_task.done():
            stall_watchdog_task.cancel()
        # Flush & close the stream trace logger if one was opened.
        if lg is not None:
            try:
                lg.flush()
            except Exception as e:
                logger.warning(f"TeamStreamLogger flush failed, error is {e}")
        try:
            if not stream_cancelled:
                # Broadcast team.completed so cron round watchers (both the
                # agent adapter's _wait_for_cron_team_round_events and the cron
                # scheduler's own round_state) can finalise when the stream
                # ends normally without a terminal event.  A cancelled stream
                # must not re-enter bounded waiter backpressure during cleanup.
                await _broadcast_team_state_snapshot(channel_id, session_id)
                try:
                    await _broadcast_event(
                        channel_id,
                        session_id,
                        {
                            "event_type": "team.completed",
                            "session_id": session_id,
                        },
                    )
                except Exception:
                    logger.debug(
                        "[TeamHelpers] failed to broadcast team.completed on stream end: "
                        "session_id=%s",
                        session_id,
                    )
        finally:
            # Registry release must run even if cancellation arrives while a
            # normal stream is delivering its final snapshot.
            team_manager = get_team_manager(channel_id)
            team_manager.clear_pending_runtime(session_id)
            clear_active_runtime = getattr(team_manager, "clear_active_runtime", None)
            if callable(clear_active_runtime):
                clear_active_runtime(session_id)
            team_manager.pop_stream_task(session_id)


async def _consume_monitor_events(
    channel_id: str | None,
    session_id: str,
    monitor_handler: TeamMonitorHandler,
) -> None:
    """Consume monitor events in the background and broadcast them."""
    try:
        logger.info(
            "[TeamHelpers] monitor event loop started: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
        async for event in monitor_handler.events():
            _persist_team_history_event(channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)

        logger.info(
            "[TeamHelpers] monitor event loop ended: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
    except asyncio.CancelledError:
        logger.info(
            "[TeamHelpers] monitor event loop cancelled: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
        raise
    except Exception as exc:
        logger.error(
            "[TeamHelpers] monitor event loop failed: channel_id=%s session_id=%s error=%s",
            _resolve_channel_id(channel_id),
            session_id,
            exc,
        )


# --- swarmflow workflow.updated → web team.member / team.task conversion ---
#
# TUI 前端能原生渲染 ``workflow.updated``（workflow 面板），但 web 前端只订阅
# ``team.member`` / ``team.task``。当 web 端触发 swarmflow 时，把每个 worker 的状态
# 转成 teammate 事件、把每个 phase 转成 task 事件，从而复用现有前端渲染。
#
# member_id / task_id 均以 run_id 前缀做命名空间，避免与真实 teammate/task 冲突。

# swarmflow phase status -> (web team.task event type, authoritative TeamTaskStatus).
# The status is resolved here (server-side) so the web frontend consumes it
# directly, consistent with TeamMonitorHandler's convergence. The event ``type``
# only drives the activity-log label; ``status`` alone decides the board column.
_WF_PHASE_STATUS_TO_TASK: dict[str, tuple[str, str]] = {
    "planned": ("team.task.created", "pending"),
    "running": ("team.task.claimed", "in_progress"),
    "completed": ("team.task.completed", "completed"),
    "failed": ("team.task.cancelled", "cancelled"),
    "stopped": ("team.task.cancelled", "cancelled"),
}


def _team_event_envelope(
    category: str, session_id: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Wrap an inner team event dict in the standard broadcast envelope."""
    return {"event_type": category, "session_id": session_id, "event": event}


def _workflow_updated_to_team_events(
    event: dict[str, Any],
    session_id: str,
    seen_phase: dict[str, str],
    seen_agent: dict[str, str],
    spawned_members: set[str],
) -> list[dict[str, Any]]:
    """Convert one ``workflow.updated`` event into web ``team.member`` / ``team.task`` events.

    Each swarmflow phase becomes a ``team.task`` and each worker (agent) becomes a
    ``team.member``. Only status *changes* produce events — the ``workflow.updated``
    delta repeatedly re-includes a running phase (once per agent that starts inside
    it), so ``seen_phase`` / ``seen_agent`` dedup by last-observed status.
    """
    if event.get("event_type") != "workflow.updated":
        return []

    wf = event.get("workflow") or {}
    run_id = str(wf.get("id") or "")
    team_id = str(wf.get("name") or run_id or "swarmflow")
    if not run_id:
        return []

    out: list[dict[str, Any]] = []

    for phase in wf.get("phases", []) or []:
        phase_id = phase.get("id")
        status = phase.get("status")
        if not phase_id or not status:
            continue
        task_id = f"{run_id}:{phase_id}"
        if seen_phase.get(task_id) != status:
            seen_phase[task_id] = status
            mapping = _WF_PHASE_STATUS_TO_TASK.get(status)
            if mapping is not None:
                task_type, task_status = mapping
                out.append(
                    _team_event_envelope(
                        "team.task",
                        session_id,
                        {
                            "type": task_type,
                            "team_id": team_id,
                            "task_id": task_id,
                            "title": phase.get("name") or phase_id,
                            "status": task_status,
                        },
                    )
                )

        for agent in phase.get("agents", []) or []:
            agent_id = agent.get("id")
            agent_status = agent.get("status")
            if not agent_id or not agent_status:
                continue
            member_id = f"{run_id}:{agent_id}"

            # First sighting of a worker → spawn it before any status change, even
            # when we first see it already terminal (missed the running delta).
            if member_id not in spawned_members:
                spawned_members.add(member_id)
                seen_agent[member_id] = "running"
                out.append(
                    _team_event_envelope(
                        "team.member",
                        session_id,
                        {
                            "type": "team.member.spawned",
                            "team_id": team_id,
                            "member_id": member_id,
                            "name": agent.get("name") or agent_id,
                            "status": "busy",
                        },
                    )
                )

            if seen_agent.get(member_id) != agent_status:
                old_status = seen_agent.get(member_id, "busy")
                seen_agent[member_id] = agent_status
                if agent_status != "running":
                    out.append(
                        _team_event_envelope(
                            "team.member",
                            session_id,
                            {
                                "type": "team.member.status_changed",
                                "team_id": team_id,
                                "member_id": member_id,
                                "old_status": old_status,
                                "new_status": agent_status,
                            },
                        )
                    )

    return out


async def _consume_workflow_events(
    channel_id: str | None,
    session_id: str,
    workflow_handler: WorkflowMonitorHandler,
) -> None:
    """Consume workflow events in the background and broadcast them.

    TUI keeps the native ``workflow.updated`` stream. Every other channel (web)
    gets the events translated into ``team.member`` / ``team.task`` so the
    existing web frontend can render swarmflow workers/phases.
    """
    is_tui = _resolve_channel_id(channel_id) == "tui"
    seen_phase: dict[str, str] = {}
    seen_agent: dict[str, str] = {}
    spawned_members: set[str] = set()
    try:
        logger.info(
            "[TeamHelpers] workflow event loop started: channel_id=%s session_id=%s is_tui=%s",
            _resolve_channel_id(channel_id),
            session_id,
            is_tui,
        )
        async for event in workflow_handler.events():
            # WF_DBG: 维测日志 — 广播前打印事件关键字段
            wf = event.get("workflow", {})
            logger.info(
                "[WF_DBG _consume_workflow_events] broadcast: "
                "channel_id=%s session_id=%s event_type=%s "
                "workflow_id=%s workflow_name=%s status=%s "
                "phases_count=%d agent_count=%d completed_agent_count=%d",
                _resolve_channel_id(channel_id),
                session_id,
                event.get("event_type", ""),
                wf.get("id", ""),
                wf.get("name", ""),
                wf.get("status", ""),
                len(wf.get("phases", [])),
                wf.get("agent_count", 0),
                wf.get("completed_agent_count", 0),
            )
            if is_tui:
                await _broadcast_event(channel_id, session_id, event)
                # Check terminal status for TUI path too
                wf_status = (wf.get("status") or "").strip()
                if wf_status in ("completed", "failed", "stopped"):
                    logger.info(
                        "[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s",
                        _resolve_channel_id(channel_id), session_id, wf_status,
                    )
                    get_team_manager(channel_id).mark_workflow_completed(session_id)
                continue
            for team_ev in _workflow_updated_to_team_events(
                event, session_id, seen_phase, seen_agent, spawned_members
            ):
                _persist_team_history_event(channel_id, session_id, team_ev)
                await _broadcast_event(channel_id, session_id, team_ev)
            # When the workflow reaches a terminal status, mark
            # workflow_completed and broadcast chat.processing_status
            # so the frontend transitions out of the processing state.
            wf_status = (wf.get("status") or "").strip()
            if wf_status in ("completed", "failed", "stopped"):
                logger.info(
                    "[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s",
                    _resolve_channel_id(channel_id), session_id, wf_status,
                )
                get_team_manager(channel_id).mark_workflow_completed(session_id)
        logger.info(
            "[TeamHelpers] workflow event loop ended: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
    except asyncio.CancelledError:
        logger.debug(
            "[TeamHelpers] workflow event loop cancelled: channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id),
            session_id,
        )
        raise
    except Exception as exc:
        logger.error(
            "[TeamHelpers] workflow event loop failed: channel_id=%s session_id=%s error=%s",
            _resolve_channel_id(channel_id),
            session_id,
            exc,
        )


def _persist_team_history_event(
    channel_id: str | None,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """Persist team monitor events required by team.history.get panel restore."""
    evt_type = event.get("event_type")
    if evt_type not in {"team.member", "team.task", "team.message"}:
        return

    payload = event.get("event")
    if not isinstance(payload, dict):
        return

    request_key = ""
    if evt_type == "team.member":
        member_event_type = str(payload.get("type") or "").strip()
        if member_event_type not in {
            "team.member.spawned",
            "team.member.restarted",
            "team.member.status_changed",
            "team.member.shutdown",
        }:
            return
        member_id = str(payload.get("member_id") or "").strip()
        if not member_id:
            return
        if (
            member_event_type == "team.member.status_changed"
            and not str(payload.get("new_status") or "").strip()
        ):
            return
        request_key = f"{member_id}-{member_event_type.rsplit('.', 1)[-1]}"
    elif evt_type == "team.message":
        # 团队动态（成员间通信）：只收 p2p/broadcast，正文截断防历史膨胀
        message_event_type = str(payload.get("type") or "").strip()
        if message_event_type not in {"team.message.p2p", "team.message.broadcast"}:
            return
        from_member = str(payload.get("from_member") or payload.get("member_name") or "").strip()
        if not from_member:
            return
        # 内容截断（落盘字段级，不改事件本体）
        content = payload.get("content")
        if isinstance(content, str) and len(content) > 512:
            payload = {**payload, "content": content[:512] + "…"}
        request_key = f"{from_member}-{message_event_type.rsplit('.', 1)[-1]}-{int(time.time() * 1000)}"
    else:
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            return
        request_key = task_id

    timestamp = time.time()
    append_history_record(
        session_id=session_id,
        request_id=f"{evt_type}-{request_key}-{int(timestamp * 1000)}",
        channel_id=_resolve_channel_id(channel_id),
        role="assistant",
        content="",
        timestamp=timestamp,
        event_type=evt_type,
        extra={
            "session_id": session_id,
            "event": dict(payload),
        },
        mode="team",
    )


async def _register_human_inbound_hooks(
        channel_id: str | None,
        session_id: str,
        team_name: str,
) -> None:
    """为团队的全部人类成员注册 team→user 通知回调：消息抵达人类席位即广播
    team.human_prompt 帧（前端成员视图据此显示"待你回复"的问题）。

    幂等可重入（register 覆盖同名成员的旧回调）；human_agent 在运行时加入
    （/join）时由调用方再次调用本函数刷新注册。成员非人类/无运行时静默跳过。
    """
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.agents.harness.team.team_manager import (
        _runner_team_runtime_manager,
    )

    if not team_name:
        return
    try:
        members = await query_team_human_members_for_join(session_id, team_name)
    except Exception:
        return
    human_names = [
        str(m.get("member_id") or "").strip()
        for m in members
        if isinstance(m, dict) and m.get("role") == "human_agent"
        and str(m.get("member_id") or "").strip()
    ]
    if not human_names:
        return
    runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
    for member_name in human_names:

        async def _on_inbound(event: Any, _member: str = member_name) -> None:
            await _broadcast_event(channel_id, session_id, {
                "event_type": "team.human_prompt",
                "session_id": session_id,
                "event": {
                    "type": "team.human_prompt",
                    "member_name": _member,
                    "prompt": str(getattr(event, "body", "") or ""),
                    "sender": str(getattr(event, "sender", "") or ""),
                    "message_id": str(getattr(event, "message_id", "") or ""),
                },
            })

        try:
            await runtime_mgr.register_human_agent_inbound(
                team_name=team_name,
                session_id=session_id,
                member_name=member_name,
                callback=_on_inbound,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[TeamHelpers] register human inbound failed: team=%s member=%s error=%s",
                team_name, member_name, exc,
            )


def _persist_member_final_output(
        channel_id: str | None,
        session_id: str,
        parsed: dict[str, Any],
) -> None:
    """成员子聊天框内容落盘：teammate chat.final 按 member_name 归因写入 session 历史。

    供前端刷新后从 chat.history 重放 memberOutputs（成员子聊天框跨刷新恢复）。
    chat.final 是该成员本轮全文快照，内容即正文。
    """
    if parsed.get("event_type") != "chat.final":
        return
    member_name = str(parsed.get("member_name") or "").strip()
    content = str(parsed.get("content") or "").strip()
    if not member_name or not content:
        return
    from jiuwenswarm.server.runtime.expert.expert_service import (
        current_expert_identity_extra,
    )

    timestamp = time.time()
    append_history_record(
        session_id=session_id,
        request_id=f"member-final-{member_name}-{int(timestamp * 1000)}",
        channel_id=_resolve_channel_id(channel_id),
        role="assistant",
        content=content,
        timestamp=timestamp,
        event_type="chat.final",
        # member_name 之外同时记会话当时绑定的专家身份（换团后历史归属仍准）
        extra={"member_name": member_name, **current_expert_identity_extra(session_id)},
        mode="team",
    )


# 与前端 tool-presentation.FILE_PATH_KEYS 对齐的路径字段名（用于成员工具记录
# 的产物/参考路径旁路提取，）
_TOOL_PATH_KEYS = (
    "file_path", "filePath", "filepath", "path", "target_file", "targetPath",
    "filename", "fileName", "file", "outputPath", "output_path", "save_path",
    "saved_path", "pdf_path", "image_path", "dest_path", "new_path",
)
# repr/文本形态（result 相位）里的 "key": "value" / 'key': 'value' 提取
_TOOL_PATH_RE = re.compile(
    r"[\"'](" + "|".join(_TOOL_PATH_KEYS) + r")[\"']\s*:\s*[\"']([^\"'\n]{1,512})[\"']"
)


def _looks_like_tool_path(value: str) -> bool:
    """路径形态粗判：含分隔符或扩展名；过滤普通文本值。"""
    if not value or len(value) > 512 or "\n" in value:
        return False
    if "/" in value or "\\" in value:
        return True
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", value))


def _extract_tool_record_paths(args_raw: Any, result_text: str) -> list[str]:
    """从工具入参（dict/JSON 字符串）与结果文本里提取文件路径，有序去重、封顶 8 条。

    call 相位入参在截断前是完整的（dict 或 JSON 字符串）；result 相位结果是
    repr/文本形态，用正则提取。供概览面板产物/参考提取的旁路（content 截断后
    JSON 残缺时的结构化兜底）。
    """
    paths: list[str] = []

    def _push(value: Any) -> None:
        if not isinstance(value, str):
            return
        v = value.strip()
        if _looks_like_tool_path(v) and v not in paths and len(paths) < 8:
            paths.append(v)

    args = args_raw
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = None
    if isinstance(args, dict):
        for key in _TOOL_PATH_KEYS:
            _push(args.get(key))
        for grouped in (args.get("files"), args.get("paths")):
            if isinstance(grouped, list):
                for item in grouped:
                    _push(item)
    if result_text:
        for match in _TOOL_PATH_RE.finditer(result_text):
            _push(match.group(2))
    return paths


def _persist_member_tool_event(
        channel_id: str | None,
        session_id: str,
        parsed: dict[str, Any],
) -> None:
    """成员工具事件归因落盘：teammate chat.tool_call/tool_result 按 member_name 写入
    session 历史，供成员视图的工具活动跨刷新恢复。工具结果内容截断防膨胀。"""
    event_type = parsed.get("event_type")
    if event_type not in ("chat.tool_call", "chat.tool_result"):
        return
    member_name = str(parsed.get("member_name") or "").strip()
    if not member_name:
        return
    tool_payload = parsed.get("tool_call") or parsed.get("tool_result") or {}
    if not isinstance(tool_payload, dict):
        tool_payload = {}
    # 字段两层取：chat.tool_result 的 tool_name/tool_call_id 在帧顶层，
    # chat.tool_call 的在 tool_call 子载荷里（parse_stream_chunk 两种形态）
    tool_name = str(
        parsed.get("tool_name")
        or tool_payload.get("tool_name") or tool_payload.get("name") or ""
    ).strip()
    if not tool_name:
        return
    tool_call_id = str(
        parsed.get("tool_call_id")
        or tool_payload.get("tool_call_id") or tool_payload.get("tool_id")
        or tool_payload.get("toolCallId") or ""
    ).strip()
    # content：result 相位存结果；call 相位存入参（成员视图展开详情重放用）
    if event_type == "chat.tool_call":
        args_raw = tool_payload.get("arguments") or tool_payload.get("args")
        result_text = (
            args_raw if isinstance(args_raw, str)
            else json.dumps(args_raw, ensure_ascii=False) if args_raw is not None
            else ""
        )
    else:
        args_raw = None
        result_text = str(tool_payload.get("result") or parsed.get("result") or "")
    # 路径字段在截断前单独提取入 extra（不截断）：write_file 等工具入参的
    # content 全文必超 512 被截断，残缺 JSON 让前端概览面板的产物/参考
    # 提取失路；tool_paths 作为结构化旁路兜底
    tool_paths = _extract_tool_record_paths(
        args_raw if event_type == "chat.tool_call" else None,
        result_text if event_type == "chat.tool_result" else "",
    )
    if len(result_text) > 512:
        result_text = result_text[:512] + "…"
    timestamp = time.time()
    append_history_record(
        session_id=session_id,
        request_id=f"member-tool-{member_name}-{tool_call_id or 'x'}-{int(timestamp * 1000)}",
        channel_id=_resolve_channel_id(channel_id),
        role="assistant",
        content=result_text,
        timestamp=timestamp,
        event_type=event_type,
        extra={
            "member_name": member_name,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            **({"tool_paths": tool_paths} if tool_paths else {}),
        },
        mode="team",
    )


def _on_team_watcher_done(task: asyncio.Task) -> None:
    """Callback when a team evolution monitor task completes."""
    channel_id = getattr(task, "_team_channel_id", None)
    session_id = getattr(task, "_team_session_id", None)
    if isinstance(session_id, str):
        get_team_manager(channel_id).pop_team_evolution_watcher(session_id)

    if task.cancelled():
        return

    exc = task.exception()
    if exc is not None:
        logger.warning("[TeamHelpers] evolution monitor task exception: %s", exc)


async def _watch_team_evolution_and_push(
    channel_id: str | None,
    session_id: str,
    rail: Any,
) -> None:
    """Monitor TeamSkillEvolutionRail and push stable status/approval events for every evolution cycle."""
    from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport

    push_context = EvolutionPushContext(
        transport=WebSocketGatewayPushTransport(),
        channel_id=channel_id,
        session_id=session_id,
    )
    seen_request_ids: set[str] = set()
    closed_request_ids: set[str] = set()
    fallback_cycle_index = 0
    active_cycle_request_id: str | None = None

    async def _cleanup_evolution_rail() -> None:
        cleanup = getattr(rail, "cleanup_background_tasks", None)
        if cleanup is None:
            return
        try:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning(
                "[TeamHelpers] evolution cleanup failed: session_id=%s error=%s",
                session_id,
                exc,
            )

    async def _push_cycle_start(
        request_id: str,
        progress_statuses: list[EvolutionProgressStatus],
    ) -> bool:
        if request_id in closed_request_ids:
            return False
        request_progress = progress_for_request(progress_statuses, request_id)
        first_progress = request_progress[0] if request_progress else None
        await push_evolution_status(
            push_context,
            build_evolution_status_update(
                request_id=request_id,
                status="start",
                stage=first_progress.stage if first_progress else TEAM_EVOLUTION_START_STAGE,
                message=(
                    first_progress.message
                    if first_progress
                    else TEAM_EVOLUTION_START_MESSAGE
                ),
            ),
            build_server_push_message,
        )
        return True

    try:
        last_event_at = time.monotonic()
        event_timeout_sec = resolve_evolution_event_timeout_sec(
            rail,
            fallback_sec=TEAM_EVOLUTION_EVENT_TIMEOUT_SEC,
        )
        while True:
            if not rail.signal_trigger and not rail.review_trigger:
                if active_cycle_request_id is not None:
                    await push_evolution_status(
                        push_context,
                        build_evolution_status_update(
                            request_id=active_cycle_request_id,
                            status="end",
                            stage=TEAM_EVOLUTION_HIDDEN_STAGE,
                            message="",
                        ),
                        build_server_push_message,
                    )
                await _cleanup_evolution_rail()
                return

            events = await rail.drain_pending_approval_events(wait=False) or []
            if not events:
                if active_cycle_request_id is not None:
                    idle_for = time.monotonic() - last_event_at
                    if idle_for >= event_timeout_sec:
                        logger.warning(
                            "[TeamHelpers] evolution monitor timed out: session_id=%s "
                            "request_id=%s idle_for=%.1fs",
                            session_id,
                            active_cycle_request_id,
                            idle_for,
                        )
                        await push_evolution_status(
                            push_context,
                            build_evolution_status_update(
                                request_id=active_cycle_request_id,
                                status="end",
                                stage=TEAM_EVOLUTION_HIDDEN_STAGE,
                                message=(
                                    "Team skill evolution analysis timed out after "
                                    f"{event_timeout_sec:.0f}s without host events"
                                ),
                            ),
                            build_server_push_message,
                        )
                        await _cleanup_evolution_rail()
                        return
                await asyncio.sleep(TEAM_EVOLUTION_IDLE_SLEEP_SEC)
                continue
            last_event_at = time.monotonic()

            await broadcast_evolution_progress(
                channel_id,
                session_id,
                events,
                parse_stream_chunk=parse_stream_chunk,
                broadcast_event=_broadcast_event,
            )

            grouped_approvals, _ = _group_team_evolution_approvals(session_id, events)
            outcomes = [
                evolution_outcome_from_event(evt)
                for evt in events
                if is_evolution_outcome_event(evt)
            ]
            terminal_progress = terminal_progress_from_events(events)
            visible_progress_statuses = visible_evolution_progress_from_events(events)
            just_started = False

            if active_cycle_request_id is None:
                first_request_id = next(iter(grouped_approvals), None)
                if first_request_id is None:
                    for progress_status in visible_progress_statuses:
                        if progress_status.request_id:
                            first_request_id = progress_status.request_id
                            break
                if first_request_id is None:
                    for evt in events:
                        if evolution_progress_status_from_event(evt) is not None:
                            continue
                        request_id = extract_evolution_request_id(evt)
                        if request_id:
                            first_request_id = request_id
                            break
                if first_request_id is None:
                    for terminal_request_id, terminal in terminal_progress:
                        if (
                            terminal_request_id
                            or terminal_stage(terminal)
                            not in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES
                        ):
                            first_request_id = terminal_request_id
                            break
                if first_request_id is None:
                    if any(
                        progress_status.request_id is None
                        for progress_status in visible_progress_statuses
                    ):
                        fallback_cycle_index += 1
                        first_request_id = make_team_evolution_cycle_request_id(
                            session_id,
                            fallback_cycle_index,
                        )
                    elif any(
                        terminal_request_id is None
                        and terminal_stage(terminal)
                        not in TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES
                        for terminal_request_id, terminal in terminal_progress
                    ):
                        fallback_cycle_index += 1
                        first_request_id = make_team_evolution_cycle_request_id(
                            session_id,
                            fallback_cycle_index,
                        )
                    else:
                        continue
                if await _push_cycle_start(first_request_id, visible_progress_statuses):
                    active_cycle_request_id = first_request_id
                    just_started = True

            if active_cycle_request_id is None:
                continue

            active_progress_statuses = progress_for_request(
                visible_progress_statuses,
                active_cycle_request_id,
            )
            progress_statuses_to_push = (
                active_progress_statuses[1:]
                if just_started
                else active_progress_statuses
            )
            for progress_status in progress_statuses_to_push:
                if progress_status.terminal:
                    continue
                await push_evolution_status(
                    push_context,
                    build_evolution_status_update(
                        request_id=active_cycle_request_id,
                        status="progress",
                        stage=progress_status.stage,
                        message=progress_status.message,
                    ),
                    build_server_push_message,
                )

            for request_id, approval_events in grouped_approvals.items():
                if request_id in closed_request_ids:
                    continue
                if active_cycle_request_id != request_id:
                    if not await _push_cycle_start(request_id, visible_progress_statuses):
                        continue
                    active_cycle_request_id = request_id
                if request_id in seen_request_ids:
                    logger.debug(
                        "[TeamHelpers] skip duplicated team evolution approval batch: session_id=%s request_id=%s",
                        session_id,
                        request_id,
                    )
                    continue
                seen_request_ids.add(request_id)
                for evt in approval_events:
                    try:
                        await push_evolution_event(
                            push_context,
                            request_id,
                            evt,
                            build_server_push_message,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[TeamHelpers] push approval failed for request_id=%s event_type=%s error=%s",
                            request_id,
                            event_type(evt) or "unknown",
                            exc,
                        )
                await push_evolution_status(
                    push_context,
                    build_evolution_status_update(
                        request_id=request_id,
                        status="end",
                        stage="approval_required",
                        message="Team skill evolution proposal is awaiting approval",
                    ),
                    build_server_push_message,
                )
                closed_request_ids.add(request_id)
                active_cycle_request_id = None

            terminal = None
            if outcomes:
                outcome = outcomes[-1]
                terminal = {
                    "status": str(outcome.get("status") or "completed"),
                    "stage": str(outcome.get("status") or "completed"),
                    "message": str(outcome.get("message") or ""),
                }
            elif terminal_progress:
                for terminal_request_id, candidate_terminal in terminal_progress:
                    if active_cycle_request_id is None:
                        continue
                    if (
                        terminal_request_id is not None
                        and terminal_request_id != active_cycle_request_id
                    ):
                        continue
                    terminal = candidate_terminal

            if terminal is not None and active_cycle_request_id is not None:
                await push_evolution_status(
                    push_context,
                    team_evolution_end_update(active_cycle_request_id, terminal),
                    build_server_push_message,
                )
                closed_request_ids.add(active_cycle_request_id)
                active_cycle_request_id = None
    except Exception as exc:
        logger.warning("[TeamHelpers] evolution monitor failed: %s", exc)
        try:
            if active_cycle_request_id is None:
                return
            await push_evolution_status(
                push_context,
                build_evolution_status_update(
                    request_id=active_cycle_request_id,
                    status="end",
                    stage=TEAM_EVOLUTION_HIDDEN_STAGE,
                    message=f"团队技能演进分析失败: {exc}",
                ),
                build_server_push_message,
            )
        except Exception as push_exc:
            logger.warning("[TeamHelpers] push status notification failed: %s", push_exc)
