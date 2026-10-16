# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team agent streaming helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, aclosing
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.paths import (
    get_agent_teams_home,
    independent_member_workspace,
    team_home,
)
from openjiuwen.agent_teams.runtime import RunActionKind
from openjiuwen.agent_teams.runtime.background_task_controller import BackgroundTaskController
from openjiuwen.agent_teams.schema.status import MemberStatus
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.monitor import TeamMonitor, TeamStreamLogger
from openjiuwen.core.runner import Runner
from openjiuwen.core.common.logging import server_logger, team_logger
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.harness import DeepAgent

from jiuwenswarm.server.runtime.session.history_io import (
    run_history_io, run_stream_parser, stream_chunk_writes_history,
)

from jiuwenswarm.agents.harness.team import TeamManager, get_team_manager
from jiuwenswarm.agents.harness.team.team_manager import TEAM_EVENT_QUEUE_MAXSIZE
from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingRoute,
    session_messaging_route_context,
    with_session_messaging_route,
)
from jiuwenswarm.common.log_preview import DEFAULT_PREVIEW_MAX_CHARS, preview_text
from jiuwenswarm.common.mode_matrix import NEW_TEAM_DESIGN
from jiuwenswarm.common.utils import get_agent_skills_dir, mask_sensitive
from jiuwenswarm.common.config import get_config, get_skill_evolution_enabled
from jiuwenswarm.common.chat_final import FINAL_MODE_PATCH_SEGMENT
from jiuwenswarm.common.session_message import with_cross_session_history_metadata
from jiuwenswarm.common.cron_team_completion import (
    _cron_solo_harness_end_pending,
    _drain_cron_delegation_grace_events,
    apply_cron_team_round_event,
    cron_team_round_should_end,
    new_cron_team_round_state,
)
from jiuwenswarm.agents.harness.team.handlers.base_monitor_handler import (
    BaseMonitorHandler,
    monitor_team_agent,
)
from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler
from jiuwenswarm.agents.harness.team.handlers.workflow_state import WorkflowRunState
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_metadata,
    increment_session_round_count,
    update_session_metadata,
)
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    resolve_final_record_timestamp,
)
from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk
from jiuwenswarm.server.runtime.agent_adapter.user_turn import TEAM_USER_TURN_KEY, UserTurn
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.runtime.session_input import SessionInputMode, resolve_session_input_mode
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
_SESSION_BUDGET_STATE_KEY = "session_budget"
_SESSION_SWARMFLOW_CONFIG_KEY = "session_swarmflow_config"
_SESSION_TEAM_DISPATCH_MODE_KEY = "session_team_dispatch_mode"
_TEAM_DISPATCH_MODES = frozenset({"autonomous", "scheduled"})

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
_TEAM_HEARTBEAT_SERVICE: ContextVar[Any | None] = ContextVar(
    "team_heartbeat_service",
    default=None,
)


def bind_team_heartbeat_service(service: Any | None) -> Token[Any | None]:
    """Bind AgentServer's request-scoped Heartbeat service for Team admission."""
    return _TEAM_HEARTBEAT_SERVICE.set(service)


def reset_team_heartbeat_service(token: Token[Any | None]) -> None:
    """Restore the previous request-scoped Heartbeat service binding."""
    _TEAM_HEARTBEAT_SERVICE.reset(token)


def _new_team_event_queue() -> asyncio.Queue:
    return asyncio.Queue(maxsize=TEAM_EVENT_QUEUE_MAXSIZE)


def get_background_task_controller(session_id: str) -> BackgroundTaskController:
    """Return the session's BackgroundTaskController (owned by TeamManager)."""
    return get_team_manager(None).get_background_task_controller(session_id)


def classify_swarmflow_control_miss(
    channel_id: str, session_id: str, run_id: str,
) -> dict | None:
    """Classify a swarmflow control miss into the run's authoritative state.

    The controller registry only answers "no live handle" — it cannot tell a
    naturally-terminal run (user clicking a stale running/paused card) from
    one lost to a process restart. Recover the authoritative ``status`` from
    the workflow handler's in-memory states, then the session-metadata disk
    snapshot, and return ``{"error", "status"}`` for the caller's ok=False
    payload. Returns ``None`` when no state is found anywhere (true not-found).
    """
    run_state = None
    try:
        wf_handler = get_team_manager(channel_id).get_workflow_handler(session_id)
        if wf_handler is not None:
            run_state = wf_handler.get_run_states().get(run_id)
        if run_state is None:
            restored = restore_workflow_runs(session_id)
            if restored:
                run_state = restored.get(run_id)
    except Exception as exc:
        # best-effort enrichment：分类失败不得阻断控制 RPC，按 not found 返回
        team_logger.warning(
            "[swarmflow] control miss classification failed "
            "session_id=%s run_id=%s: %s",
            session_id, run_id, exc,
        )
        return None
    if run_state is None:
        return None
    error = (
        f"workflow already {run_state.status}"
        if run_state.is_terminal
        else f"workflow is {run_state.status} but no live control handle"
    )
    return {"error": error, "status": run_state.status}


def _safe_team_path_segment(value: str, fallback: str = "_") -> str:
    """Sanitize a value into one path segment for team workspace paths."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized[:96] or fallback


def _resolve_agent_group_selection(
    *,
    session_id: str,
    params: dict[str, Any] | None,
    is_first_request: bool,
) -> tuple[str | None, bool]:
    """Resolve the immutable AgentGroup selection for one Team session.

    Returns ``(name, needs_persist)``.  A missing request field inherits the
    session binding.  Empty/non-string values and attempts to switch an active
    Team are rejected before any package is loaded.
    """
    metadata = get_session_metadata(session_id, cache_bust=True)
    stored = str(metadata.get("agent_group_name") or "").strip()
    has_request_value = isinstance(params, dict) and "agent_group_name" in params
    if not has_request_value:
        return (stored or None), False

    raw_name = params.get("agent_group_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("agent_group_name must be a non-empty string")
    requested = raw_name.strip()
    if stored and requested != stored:
        raise ValueError(
            f"agent_group_name cannot be changed for this Team session: "
            f"{stored!r} -> {requested!r}"
        )
    if not stored and not is_first_request:
        raise ValueError(
            "agent_group_name can only be selected when the Team is first built"
        )
    return requested, not stored


def _resolve_team_skill_selection(
    params: dict[str, Any] | None,
    *,
    agent_group_name: str | None = None,
) -> list[str] | None:
    """Normalize the optional Team-wide Skill selection from ``chat.send``.

    ``None`` means the request omitted ``skills`` and must not change the
    Team's persisted visibility. For a plain Team, an explicit empty list
    clears the Team-level selection (the visibility layer then inherits the
    library), while a non-empty list replaces it. AgentGroup Teams reject
    non-empty selections and treat an empty list as a no-op so their packaged
    Skills and existing visibility remain untouched.

    Args:
        params: Raw ``chat.send`` params.
        agent_group_name: Effective AgentGroup, including the session binding
            when the current request omits ``agent_group_name``.

    Returns:
        ``None`` when omitted or empty for an AgentGroup Team, otherwise a
        de-duplicated list preserving the frontend's order.

    Raises:
        ValueError: ``skills`` is invalid, or an AgentGroup Team requests an
            additional Skill selection.
    """
    if not isinstance(params, dict) or "skills" not in params:
        return None
    raw_skills = params.get("skills")
    if not isinstance(raw_skills, list):
        raise ValueError("skills must be a list of non-empty strings")
    if agent_group_name:
        if raw_skills:
            raise ValueError(
                "skills cannot be selected when an agent_group_name is selected or bound"
            )
        return None

    # Keep Team chat.send validation aligned with the visibility RPC instead
    # of accepting a path-like name that could never identify a library Skill.
    from jiuwenswarm.server.runtime.skill.skill_manager import _validate_skill_name

    selected: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_skills:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("skills must be a list of non-empty strings")
        name = _validate_skill_name(raw_name.strip())
        if name not in seen:
            selected.append(name)
            seen.add(name)
    return selected


def _validate_installed_team_skills(skill_names: list[str]) -> None:
    """Require every selected Team Skill to be installed and executable."""
    if not skill_names:
        return

    skills_root = get_agent_skills_dir().expanduser().resolve()
    missing: list[str] = []
    for name in skill_names:
        try:
            skill_dir = (skills_root / name).resolve(strict=True)
            skill_dir.relative_to(skills_root)
        except (OSError, ValueError):
            missing.append(name)
            continue
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            missing.append(name)
    if missing:
        joined = ", ".join(repr(name) for name in missing)
        raise ValueError(f"team skill not installed: {joined}")

    from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

    disabled = sorted(set(skill_names).intersection(load_execution_disabled_skills()))
    if disabled:
        joined = ", ".join(repr(name) for name in disabled)
        raise ValueError(f"team skill is disabled: {joined}")


async def _apply_team_skill_selection(
    *,
    team_manager: Any,
    session_id: str,
    team_spec: Any,
    skill_names: list[str] | None,
) -> None:
    """Persist an explicit Skill selection at Team scope and refresh live rails.

    Team and member Skill rails already compose the Team visibility document
    into their effective view. Writing that one authoritative document makes
    the selection available to the leader and every teammate without copying
    Skill packages or changing ``agent-core``.
    """
    if skill_names is None:
        return
    _validate_installed_team_skills(skill_names)

    build_context = getattr(team_spec, "build_context", None)
    visibility_path = str(
        getattr(build_context, "team_skill_visibility_path", "") or ""
    ).strip()
    team_name = str(getattr(team_spec, "team_name", "") or "").strip()
    if not visibility_path or not team_name:
        raise ValueError("Team Skill visibility path is unavailable")

    from openjiuwen.agent_teams.skill.file_lock import FileLockTimeout
    from openjiuwen.agent_teams.skill.visibility import SCOPE_TEAM, set_skill_visibility

    try:
        await asyncio.to_thread(
            set_skill_visibility,
            visibility_path,
            scope=SCOPE_TEAM,
            entity_id=team_name,
            allow=skill_names,
            # A frontend selection is an explicit Team-level enable decision.
            # Member- or process-level deny rules still retain their precedence.
            deny=[],
        )
    except FileLockTimeout as exc:
        raise ValueError("Team Skill configuration is busy, please try again") from exc
    except OSError as exc:
        raise ValueError(f"failed to persist Team Skill selection: {exc}") from exc

    reload_views = getattr(team_manager, "reload_team_skill_views", None)
    if callable(reload_views):
        try:
            await reload_views(session_id)
        except Exception as exc:  # noqa: BLE001
            # Providers re-read visibility metadata on the next call; the eager
            # reload is only a latency optimization for already-running rails.
            logger.warning(
                "[TeamHelpers] eager Team Skill reload failed: session_id=%s error=%s",
                session_id,
                exc,
            )
    logger.info(
        "[TeamHelpers] applied Team Skill selection: session_id=%s team=%s skills=%s",
        session_id,
        team_name,
        skill_names,
    )


def _team_hide_teammate_enabled() -> bool:
    """Return whether non-leader teammate frames should be filtered out in team mode."""
    return os.environ.get(_HIDE_TEAMMATE_ENV_KEY, "").strip().lower() == "true"

_INTERACT_REASON_ERROR_MAP: dict[str, str] = {
    "not_active": "Team is initializing, please try again later",
    "session_mismatch": "Session state mismatch, please refresh and retry",
    "gate_closed": "Team is shutting down, please try again later",
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


def _team_input_run_context(request: Any) -> dict[str, Any]:
    """Build the invocation-scoped context consumed by Team leader rails."""
    inherited = session_messaging_route_context(request) or {}
    try:
        hop_count = max(0, int(inherited.get("hop_count") or 0))
    except (TypeError, ValueError):
        hop_count = 0
    route = SessionMessagingRoute(
        session_id=str(getattr(request, "session_id", "") or "").strip(),
        request_id=str(getattr(request, "request_id", "") or "").strip(),
        user_id=str(getattr(request, "user_id", "") or "").strip(),
        chain_id=str(inherited.get("chain_id") or "").strip(),
        parent_message_id=str(inherited.get("message_id") or "").strip(),
        hop_count=hop_count,
    )
    routed_inputs = with_session_messaging_route({}, route)
    run = routed_inputs.get("run")
    if not isinstance(run, dict):
        return {}
    context = run.get("context")
    return dict(context) if isinstance(context, dict) else {}


async def _interact_with_team_context(
    team_manager: Any,
    session_id: str,
    query: Any,
    *,
    run_context: object | None,
) -> tuple[bool, str | None]:
    """Interact while preserving compatibility with context-free callers."""
    if run_context is None:
        return await team_manager.interact(session_id, query)
    return await team_manager.interact(
        session_id,
        query,
        run_context=run_context,
    )


def _add_team_waiter(
    team_manager: Any,
    session_id: str,
    request_id: str,
    request_queue: asyncio.Queue,
    *,
    exclusive: bool = False,
    generation: int | None,
    attach_only: bool = False,
) -> None:
    """Register a waiter with generation data when Runtime supplied it."""
    team_manager.add_waiter(
        session_id,
        request_id,
        request_queue,
        exclusive=exclusive,
        generation=generation,
        attach_only=attach_only,
    )


def _remove_team_waiter(
    team_manager: Any,
    session_id: str,
    request_id: str,
    *,
    generation: int | None,
) -> None:
    """Remove a waiter without widening the legacy manager contract."""
    team_manager.remove_waiter(
        session_id,
        request_id,
        generation=generation,
    )


def _has_team_stream_task(
    team_manager: Any,
    session_id: str,
    *,
    generation: int | None,
) -> bool:
    """Check stream ownership using generation data when available."""
    return bool(team_manager.has_stream_task(session_id, generation=generation))


async def _release_team_round(
    team_manager: Any,
    session_id: str,
    request_id: str,
    *,
    generation: int | None,
) -> None:
    """Release one round with optional generation fencing."""
    await team_manager.release_round(
        session_id,
        request_id,
        generation=generation,
    )


def _begin_team_manager_round(
    team_manager: Any,
    session_id: str,
    request_id: str,
    *,
    release_admission: Callable[[], Awaitable[None]] | None,
    defer_terminal_release: bool = False,
    terminal_armed: bool,
    generation: int | None,
) -> None:
    """Begin round ownership with optional generation fencing."""
    team_manager.begin_round(
        session_id,
        request_id,
        release_admission=release_admission,
        defer_terminal_release=defer_terminal_release,
        terminal_armed=terminal_armed,
        generation=generation,
    )


def _register_team_stream_task(
    team_manager: Any,
    session_id: str,
    stream_task: asyncio.Task,
    *,
    generation: int | None,
) -> None:
    """Register producer ownership with optional generation fencing."""
    team_manager.register_stream_task(
        session_id,
        stream_task,
        generation=generation,
    )


def _pop_team_stream_task(
    team_manager: Any,
    session_id: str,
    *,
    generation: int | None,
) -> None:
    """Pop producer ownership with optional generation fencing."""
    team_manager.pop_stream_task(session_id, generation=generation)


async def _deliver_followup_interact_across_boundary(
    team_manager: Any,
    session_id: str,
    query: Any,
    *,
    initial_reason: str | None = None,
    timeout_sec: float = _FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC,
    poll_interval_sec: float = _FOLLOWUP_INTERACT_POLL_INTERVAL_SEC,
    run_context: object | None = None,
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
        success, reason = await _interact_with_team_context(
            team_manager,
            session_id,
            query,
            run_context=run_context,
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

    if isinstance(query, dict) and query.get("type") == "group_chat":
        return _FirstTeamRequestPreparation(
            recovered_runtime=False, query=query, hide_dm=False, debug=False, error_chunks=None,
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
    ready_team_name: str,
    activation_kind: str | None,
    team_leader_identity: dict[str, Any] | None = None,
) -> None:
    """Persist team identity when a team runtime becomes ready."""
    metadata = (
        get_session_metadata(session_id, cache_bust=True)
        if team_leader_identity is not None
        else get_session_metadata(session_id)
    )
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

    # 只持久化 team 身份（team_name），不碰 metadata.mode：这里历史上写死
    # mode="team" 传给 update_session_metadata，会把 chat 轮次刚落盘的
    # team.work.plan / team.work.normal 盖回光杆 "team"，制造 session.plan_status
    # 等按 metadata.mode 判定 plan 的读取方读到误报 false 的空窗。会话的真实
    # mode 由 sync_session_request_metadata / append_history_record 按每轮请求维护。
    metadata_kwargs: dict[str, Any] = {
        "session_id": session_id,
        "channel_id": _resolve_channel_id(channel_id),
        "team_name": ready_team_name,
    }
    if team_leader_identity is not None:
        metadata_kwargs.update(
            team_leader_identity=team_leader_identity,
            cache_bust=True,
            sync_write=True,
        )
    update_session_metadata(**metadata_kwargs)


def _attach_team_leader_identity(
    parsed: dict[str, Any],
    identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Add the optional snapshot only to the existing runtime-ready event."""
    if identity is not None and parsed.get("event_type") == "team.runtime_ready":
        parsed["team_leader_identity"] = dict(identity)
    return parsed


def persist_workflow_runs(
    runs: dict[str, WorkflowRunState],
    session_id: str,
    session_budget: dict | None = None,
) -> None:
    """Persist WorkflowRunState dict (and optionally the session budget) to session metadata.

    ``session_budget`` MUST ride on the same read-modify-write as the runs:
    persisting them via two separate calls makes each one ``cache_bust``-read
    the disk before the other's async-queued write is flushed, so the second
    write replaces the whole file carrying the FIRST one's stale runs — a
    lost update that freezes ``workflow_runs`` on the checkpoint (observed
    as a stopped workflow restoring as still-running).
    """
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata, _enqueue_write
    runs_data = {run_id: run_state.model_dump() for run_id, run_state in runs.items()}
    metadata = _read_metadata(session_id, cache_bust=True)
    if not metadata.get("session_id"):
        # Do not blindly write back after a failed read (e.g. a concurrent
        # write window): the write replaces the whole file and would erase
        # session_id/title. Skip this persist; the next delta retries.
        logger.warning(
            "[TeamHelpers] skipping workflow_runs persist: failed to read "
            "session metadata (session_id=%s)",
            session_id,
        )
        return
    metadata[_WORKFLOW_RUNS_STATE_KEY] = runs_data
    fields = {_WORKFLOW_RUNS_STATE_KEY}
    if session_budget is not None:
        metadata[_SESSION_BUDGET_STATE_KEY] = session_budget
        fields.add(_SESSION_BUDGET_STATE_KEY)
    # Synchronous, field-merged: the end-of-turn metadata sync reads disk and
    # replaces the whole file, so a checkpoint still sitting in the async queue
    # is rolled back — a completed run then restores as "running 19/19".
    _enqueue_write(session_id, metadata, sync_write=True, merge_fields=frozenset(fields))


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


def _normalize_recovered_runs(
    runs: dict[str, WorkflowRunState] | None, session_id: str,
) -> dict[str, WorkflowRunState] | None:
    """Normalize disk-restored runs after cold start.

    A crash leaves runs ``running`` in the snapshot although no events will
    ever arrive; park every non-terminal run to ``paused`` and mark it
    ``recovered`` — the controller registries are empty after a restart, so
    the tree-view buttons are greyed and only the leader (launch plane via
    advisory) can resume it.
    """
    if not runs:
        return runs
    changed = False
    for run in runs.values():
        if not run.is_terminal:
            if run.status != "paused":
                run.status = "paused"
                changed = True
            if not run.recovered:
                run.recovered = True
                changed = True
    if changed:
        persist_workflow_runs(runs, session_id)
    return runs


def persist_session_budget(session_id: str, snapshot: dict) -> None:
    """Persist the session-wide (leader-shared) budget snapshot to session metadata.

    The session budget is team-scoped and shared across every run, so it lives in
    metadata alongside ``workflow_runs`` (NOT inside any single run's journal).
    ``snapshot`` is ``{total, spent, remaining, scope, exhausted}``. No-op when the
    metadata read fails (mirrors ``persist_workflow_runs``).
    """
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata, _enqueue_write
    metadata = _read_metadata(session_id, cache_bust=True)
    if not metadata.get("session_id"):
        logger.warning(
            "[TeamHelpers] skipping session_budget persist: failed to read "
            "session metadata (session_id=%s)",
            session_id,
        )
        return
    metadata[_SESSION_BUDGET_STATE_KEY] = snapshot
    _enqueue_write(
        session_id, metadata, sync_write=True, merge_fields=frozenset({_SESSION_BUDGET_STATE_KEY}),
    )


def restore_session_budget(session_id: str) -> dict | None:
    """Restore the session-wide budget snapshot from session metadata, or None."""
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata
    metadata = _read_metadata(session_id, cache_bust=True)
    snapshot = metadata.get(_SESSION_BUDGET_STATE_KEY)
    return snapshot if isinstance(snapshot, dict) else None


def persist_session_swarmflow_config(session_id: str, config: dict) -> None:
    """Persist session-level swarmflow config to session metadata.

    ``config`` is ``{enable_swarmflow: bool, swarmflow_budget: int | None}``.
    Mirrors persist_session_budget (read-modify-write, no-op on read failure).
    """
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata, _enqueue_write
    metadata = _read_metadata(session_id, cache_bust=True)
    if not metadata.get("session_id"):
        logger.warning(
            "[TeamHelpers] skipping session_swarmflow_config persist: "
            "failed to read session metadata (session_id=%s)",
            session_id,
        )
        return
    metadata[_SESSION_SWARMFLOW_CONFIG_KEY] = config
    # Synchronous, single-field merge: the agent_group binding that follows on a
    # first build reads disk (cache_bust) and replaces the whole file, so a config
    # still sitting in the async queue is lost and the toggle vanishes on the next
    # session switch. Merging also keeps us from rolling back fields written by
    # async writers we could not see on disk yet.
    _enqueue_write(
        session_id,
        metadata,
        sync_write=True,
        merge_fields=frozenset({_SESSION_SWARMFLOW_CONFIG_KEY}),
    )


def restore_session_swarmflow_config(session_id: str) -> dict | None:
    """Restore session-level swarmflow config from session metadata, or None."""
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata
    metadata = _read_metadata(session_id, cache_bust=True)
    config = metadata.get(_SESSION_SWARMFLOW_CONFIG_KEY)
    return config if isinstance(config, dict) else None


def _coerce_budget(value) -> int | None:
    """Coerce a raw budget value to a positive int, else None."""
    if value is None:
        return None
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n if n > 0 else None


def _resolve_session_worktree_flag(
    params: dict | None,
    persisted: dict | None,
) -> bool | None:
    """Resolve the session worktree flag per-key: params > persisted > unset.

    Unlike ``enable_swarmflow`` (whole-dict per source), the worktree flag
    falls through independently so a client that omits it (older frontend,
    TUI, cron) keeps the session's persisted value; ``None`` means "not
    specified" and the team config's ``worktree`` section stands.
    """
    if params is not None and params.get("swarmflow_worktree") is not None:
        return bool(params.get("swarmflow_worktree"))
    if persisted is not None and persisted.get("swarmflow_worktree") is not None:
        return bool(persisted.get("swarmflow_worktree"))
    return None


def _resolve_session_swarmflow_config(
    params: dict | None,
    config_base: dict,
    *,
    session_id: str,
) -> dict:
    """Resolve session-level swarmflow config by priority:

    request params > metadata persisted > config.yaml.

    ``swarmflow_worktree`` resolves per-key (params > persisted, else None =
    follow the team config's ``worktree`` section).
    """
    team_cfg = ((config_base.get("modes") or {}).get("team") or {}).get("jiuwen_team") or {}
    config_enabled = bool(team_cfg.get("enable_swarmflow", False))
    config_budget_raw = team_cfg.get("swarmflow_budget")
    config_budget = (
        int(config_budget_raw)
        if isinstance(config_budget_raw, (int, float)) and config_budget_raw > 0
        else None
    )

    if params and params.get("enable_swarmflow") is not None:
        # Fast path: current clients send the worktree key alongside
        # enable_swarmflow, so the metadata read is skipped unless the key is
        # absent (older client) and a sticky value may exist.
        persisted = (
            restore_session_swarmflow_config(session_id)
            if params.get("swarmflow_worktree") is None
            else None
        )
        result = {
            "enable_swarmflow": bool(params.get("enable_swarmflow")),
            "swarmflow_budget": _coerce_budget(params.get("swarmflow_budget")),
            "swarmflow_worktree": _resolve_session_worktree_flag(params, persisted),
        }
        logger.info(
            "[TeamHelpers] swarmflow config (source=request params, session=%s): %s",
            session_id, result,
        )
        return result

    persisted = restore_session_swarmflow_config(session_id)
    if persisted is not None:
        result = {
            "enable_swarmflow": bool(persisted.get("enable_swarmflow", False)),
            "swarmflow_budget": _coerce_budget(persisted.get("swarmflow_budget")),
            "swarmflow_worktree": _resolve_session_worktree_flag(params, persisted),
        }
        logger.info(
            "[TeamHelpers] swarmflow config (source=metadata, session=%s): %s",
            session_id, result,
        )
        return result

    result = {
        "enable_swarmflow": config_enabled,
        "swarmflow_budget": config_budget,
        "swarmflow_worktree": _resolve_session_worktree_flag(params, None),
    }
    logger.info(
        "[TeamHelpers] swarmflow config (source=config.yaml, session=%s): %s",
        session_id, result,
    )
    return result


def _coerce_team_dispatch_mode(value: Any) -> str | None:
    """Return ``value`` as a known dispatch mode, else None."""
    mode = str(value or "").strip().lower()
    return mode if mode in _TEAM_DISPATCH_MODES else None


def persist_session_team_dispatch_mode(session_id: str, dispatch_mode: str) -> None:
    """Persist the session-level team dispatch mode to session metadata.

    Mirrors persist_session_swarmflow_config (read-modify-write, no-op on read
    failure) so a refreshed page restores the mode the team was built with.
    """
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata, _enqueue_write
    metadata = _read_metadata(session_id, cache_bust=True)
    if not metadata.get("session_id"):
        logger.warning(
            "[TeamHelpers] skipping session_team_dispatch_mode persist: "
            "failed to read session metadata (session_id=%s)",
            session_id,
        )
        return
    metadata[_SESSION_TEAM_DISPATCH_MODE_KEY] = dispatch_mode
    _enqueue_write(session_id, metadata)


def _resolve_session_team_dispatch_mode(
    params: dict | None,
    *,
    session_id: str,
) -> str | None:
    """Resolve the session-level team dispatch mode: params > persisted > unset.

    ``None`` means "not specified" and the team config's ``dispatch_mode``
    stands, so clients that never send the key (TUI, cron) keep the global
    behavior.
    """
    if params is not None:
        requested = _coerce_team_dispatch_mode(params.get("team_dispatch_mode"))
        if requested is not None:
            return requested
    from jiuwenswarm.server.runtime.session.session_metadata import _read_metadata
    metadata = _read_metadata(session_id, cache_bust=True)
    return _coerce_team_dispatch_mode(metadata.get(_SESSION_TEAM_DISPATCH_MODE_KEY))


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

# 前端按 team 分流对话视图时，需要知道这些帧属于哪个 team。见 _tag_team_output_origin。
_TEAM_ATTRIBUTED_EVENT_TYPES = frozenset({
    "chat.delta",
    "chat.final",
    "chat.reasoning",
    "chat.tool_call",
    "chat.tool_result",
    "chat.usage",
    "chat.error",
})


def _resolve_user_turn(
    inputs: dict[str, Any],
    *,
    channel_id: str | None,
    language: str,
) -> UserTurn:
    """Return the turn built by the adapter, or reconstruct a minimal one.

    ``_build_inputs`` attaches the turn for every ``chat.send``; the fallback
    only covers callers that assemble ``inputs`` themselves, and keeps them on
    the same renderer rather than silently delivering a bare string.

    Args:
        inputs: Runner inputs prepared by the adapter.
        channel_id: Channel the request arrived on.
        language: Resolved runtime language.

    Returns:
        The ``UserTurn`` for this request.
    """
    turn = inputs.get(TEAM_USER_TURN_KEY)
    if isinstance(turn, UserTurn):
        return turn
    return UserTurn(
        text=inputs.get("query", ""),
        channel=_resolve_channel_id(channel_id),
        language=language,
        files={},
    )


# 历史渲染过滤锚点：advisory 文本进历史时按此标记剔除（渲染层/摘要层各自过滤）。
_ADVISORY_MARK = ("[swarmflow-advisory]", "[/swarmflow-advisory]")


def _should_inject_swarmflow_advisory(
    swarmflow_config: dict[str, Any], query: Any, runs: dict[str, WorkflowRunState],
) -> bool:
    """Whether the leader's turn gets the paused-run advisory prefix.

    Only swarmflow-enabled sessions, only a plain-text query (A2UI /
    InteractiveInput keep their own payload), only when there is something to
    list, and only when the text is addressed to the team — a member-addressed
    message is delivered by the message system, never seen by the leader.
    """
    return bool(
        swarmflow_config.get("enable_swarmflow")
        and isinstance(query, str)
        and runs
        and not _is_member_addressed(query)
    )


def _advisory_runs(team_manager: Any, session_id: str) -> dict[str, WorkflowRunState]:
    """Runs the advisory should list: live handler states, else the snapshot.

    A team pause pops the workflow handler with the runtime, so the next
    chat.send has no handler to ask — but the persisted snapshot still holds
    the paused runs and is the only ledger left until runtime_ready rebuilds
    the handler. Without this fallback the leader gets a bare query and guesses.
    """
    wf_handler = team_manager.get_workflow_handler(session_id)
    if wf_handler is not None:
        return wf_handler.get_run_states()
    return restore_workflow_runs(session_id) or {}


def _inject_swarmflow_context(
    turn: UserTurn,
    runs: dict[str, WorkflowRunState],
    *,
    cold_start: bool,
    controller: Any,
) -> UserTurn:
    """Prepend a resume-advisory prefix to the leader's user turn.

    ``cold_start`` lists every non-terminal run; otherwise only paused runs.
    ``controller`` decides each run's resume plane: a run it still holds a
    pause ticket for resumes in-process (``action="resume"``); any other goes
    through the launch plane (``resume_id + script_path``). Returns ``turn``
    unchanged when there is nothing to inject or the text is not a plain
    string (A2UI / InteractiveInput keep their own payload).
    """
    eligible = [
        r for r in runs.values()
        if ((not r.is_terminal) if cold_start else (r.status == "paused"))
    ]
    if not eligible or not isinstance(turn.text, str):
        return turn
    lines = [
        _ADVISORY_MARK[0],
        f"当前有 {len(eligible)} 个 swarmflow 工作流处于已暂停状态：",
    ]
    for r in eligible:
        script = r.script_path or r.script or ""
        lines.append(f"- run_id: {r.id}  脚本: {script}")
        # Two resume planes, keyed on the ticket — not on the request shape:
        # a team pause pops the handler so the next chat.send looks like a
        # cold start, yet the controller still holds the ticket in-process and
        # ``action="resume"`` relaunches the cached prefix. Only a run with no
        # ticket (process restart / session switch) needs the launch plane
        # (``resume_id + script_path``, args recovered from the journal).
        # Handing the leader the wrong one makes it bypass the ticket and start
        # a brand-new run instead of resuming.
        if controller.is_paused(r.id):
            lines.append(f'  恢复调用: swarmflow(resume_id="{r.id}", action="resume")')
        elif r.script_path:
            lines.append(
                f'  恢复调用: swarmflow(resume_id="{r.id}", script_path="{r.script_path}")'
            )
        # stop works on both planes: the tool announces stopped itself when the
        # controller no longer holds the ticket (cold start), no journal seal.
        lines.append(f'  停止调用: swarmflow(resume_id="{r.id}", action="stop")')
    # The tree-view buttons are greyed while the team sleeps,
    # so the leader is the only control path: act on an explicit request,
    # otherwise ask — coarse first, per-run only on demand, so N runs never
    # overflow ask_user's 2-4 options per question.
    lines.extend([
        "处理规则：",
        "- 若用户本条消息已明确要求恢复或停止某个（或全部）工作流，直接执行对应调用，不要询问。",
        "- 否则先调用 ask_user 单题：「已暂停的工作流如何处理？」选项「全部恢复 / 全部停止 / "
        "逐个选择 / 暂不处理」。选「逐个选择」再按每个工作流各出一题（恢复 / 停止 / 暂不处理）。"
        "「暂不处理」保持暂停，不要自行恢复。",
    ])
    lines.append(_ADVISORY_MARK[1])
    return turn.with_text("\n".join(lines) + "\n\n" + turn.text)


def _request_trusted_dirs(request: Any) -> list[str]:
    """Return the trusted directories declared by this request.

    Members mount them on their runtime-prompt and permission rails, which is
    how a single agent learns the same list (see ``_apply_runtime_config_stages``).

    Args:
        request: The incoming ``AgentRequest``.

    Returns:
        Non-empty trusted directory paths, or an empty list.
    """
    params = getattr(request, "params", None)
    if not isinstance(params, dict):
        return []
    raw = params.get("trusted_dirs")
    if not isinstance(raw, list):
        return []
    dirs: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            dirs.append(entry.strip())
    return dirs


def _is_member_addressed(text: str) -> bool:
    """Whether ``text`` is addressed to specific members rather than the team.

    Mirrors openjiuwen's own split (``TeamAgent._initial_leader_route_payloads``):
    anything that parses to a non-god-view payload — ``@member``, ``$sender``,
    ``@all`` — is delivered by the team's message system instead of becoming
    the leader's user input.

    Args:
        text: User text, possibly starting with routing tokens.

    Returns:
        True when the team message system owns delivery.
    """
    from openjiuwen.agent_teams.interaction.payload import GodViewMessage
    from openjiuwen.agent_teams.interaction.router import parse_interact_str

    payloads = parse_interact_str(text)
    return bool(payloads) and any(not isinstance(p, GodViewMessage) for p in payloads)


def _group_chat_input(request: Any, text: Any) -> dict | None:
    """Convert explicit public input; mention objects carry their core member identity."""
    params = getattr(request, "params", None) or {}
    raw = params.get("query")
    if not isinstance(raw, dict) or raw.get("type") != "group_chat":
        if params.get("type") != "group_chat":
            return None
        raw = params
    mentions = raw.get("mentions", [])
    if not isinstance(mentions, list):
        raise ValueError("Group mentions must be an array")
    targets = []
    for mention in mentions:
        if isinstance(mention, dict):
            # An expert team is represented by its leader in the outer team.
            mention = mention.get("leader_member_name") or mention.get("member_name")
        if not isinstance(mention, str) or not mention.strip():
            raise ValueError("Each group mention requires a core member name")
        targets.append(mention)
    payload = {
        "type": "group_chat", "body": raw.get("body", text),
        "client_message_id": raw.get("client_message_id") or request.request_id,
        "mentions": list(dict.fromkeys(targets)), "attachments": raw.get("attachments", []),
    }
    if not isinstance(payload["body"], str):
        raise ValueError("Group body must be a string")
    identity = payload["client_message_id"]
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("Group chat requires client_message_id")
    attachments = payload["attachments"]
    if not isinstance(attachments, list):
        raise ValueError("Group attachments must be an array")
    if any(not isinstance(item, dict) for item in attachments):
        raise ValueError("Group attachments must contain JSON objects")
    return payload


def _deliverable(turn: UserTurn, text: Any) -> Any:
    """Render ``text`` into the payload delivered to the team runtime.

    Only messages that become the leader's *user input* get the envelope, which
    is exactly the case a single agent handles — that is what the two modes must
    agree on. Member-addressed messages travel a different channel: the team
    message system wraps them in its own ``<team-inbound from=... type=...>``
    envelope carrying sender, message id, time and reply hint. Rendering the
    user-input envelope inside that one would nest two conflicting headers (the
    inner ``source: web`` / ``type: user input`` contradicting the outer
    ``from``), so those pass through untouched. Attachment paths still reach
    them: the composer inlines the 【上传文档】 block into the message text.

    Args:
        turn: The turn carrying this request's context (files, skills, sender).
        text: User text as it stands after directive / slash / ``$member``
            rewriting.

    Returns:
        The rendered envelope for team-wide input, or ``text`` unchanged when
        the team message system owns delivery.
    """
    if isinstance(text, dict) and text.get("type") == "group_chat":
        return text
    if not isinstance(text, str):
        return turn.with_text(text).render()
    if _is_member_addressed(text):
        return text
    return turn.with_text(text).render()


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


async def _get_active_team_monitor(
    team_name: str,
    session_id: str,
    *,
    hide_dm: bool = False,
) -> TeamMonitor | None:
    """Resolve a fresh (unstarted) TeamMonitor for the pool's current leader."""
    # create_monitor inside Runner.get_agent_team_monitor freezes the
    # current contextvar session_id into the TeamMonitor (self._session_id).
    # runtime_ready fires before the leader's bind_session, so the
    # contextvar is empty here; bind the explicit session_id so the
    # monitor does not hash an empty session id and target non-existent
    # per-session tables (team_task_<hash> / team_message_<hash>).
    token = set_session_id(session_id)
    try:
        return await Runner.get_agent_team_monitor(
            team_name=team_name,
            session_id=session_id,
            hide_dm=hide_dm,
        )
    finally:
        reset_session_id(token)


def _monitor_handler_is_stale(handler: Any, current_monitor: TeamMonitor | None) -> bool:
    """Return True when ``handler`` listens on a leader the pool no longer holds.

    Core may rebuild a paused runtime inside ``activate`` (``stop_team`` +
    cold recovery, e.g. after the model selection changes) without going
    through swarm teardown.  The old monitor then stays attached to the
    discarded leader: it never receives another event nor an end sentinel, so
    ``is_running`` alone cannot detect it.
    """
    if current_monitor is None or not isinstance(handler, BaseMonitorHandler):
        return False
    bound_agent = handler.bound_team_agent
    current_agent = monitor_team_agent(current_monitor)
    return bound_agent is not None and current_agent is not None and bound_agent is not current_agent


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
    A running handler bound to a leader the runtime pool has since replaced is
    stopped and rebuilt on the current leader.
    """
    tm = get_team_manager(channel_id)

    # --- TeamMonitorHandler ---
    existing_monitor = tm.get_monitor(session_id)
    monitor = await _get_active_team_monitor(team_name, session_id, hide_dm=hide_dm)
    keep_existing = (
        existing_monitor is not None
        and existing_monitor.is_running
        and not _monitor_handler_is_stale(existing_monitor, monitor)
    )
    if existing_monitor is not None and not keep_existing:
        if existing_monitor.is_running:
            logger.info(
                "[TeamHelpers] team leader replaced; rebinding monitor: "
                "channel_id=%s session_id=%s team_name=%s",
                _resolve_channel_id(channel_id),
                session_id,
                team_name,
            )
        # Release the old listener (and its consumer task) before replacing it.
        await existing_monitor.stop()
    if not keep_existing:
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
    wf_monitor = await _get_active_team_monitor(team_name, session_id)
    if existing_wf is not None and existing_wf.is_running:
        if not _monitor_handler_is_stale(existing_wf, wf_monitor):
            return
        logger.info(
            "[TeamHelpers] team leader replaced; rebinding workflow monitor: "
            "channel_id=%s session_id=%s team_name=%s",
            _resolve_channel_id(channel_id),
            session_id,
            team_name,
        )
    if existing_wf is not None:
        # Release the old listener; its in-memory runs are merged below.
        await existing_wf.stop()

    # Build initial_runs: merge in-memory runs from a stopped handler with
    # disk-restored runs. In-memory data is more up-to-date (may contain
    # events not yet persisted), so it takes priority over disk data.
    initial_runs: dict[str, WorkflowRunState] | None = None
    if existing_wf is not None:
        # Stopped handler still holds _runs in memory — prefer these
        initial_runs = existing_wf.get_run_states()
        # Merge disk-restored runs for any IDs not present in memory
        restored_from_disk = _normalize_recovered_runs(
            restore_workflow_runs(session_id), session_id,
        )
        if restored_from_disk:
            for run_id, run_state in restored_from_disk.items():
                if run_id not in initial_runs:
                    initial_runs[run_id] = run_state
        # Clean up the stale handler reference
        tm.pop_workflow_handler(session_id)
    else:
        # No in-memory handler — restore from disk only
        initial_runs = _normalize_recovered_runs(
            restore_workflow_runs(session_id), session_id,
        )

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


def _is_heartbeat_request(request: Any) -> bool:
    metadata = getattr(request, "metadata", None)
    automation = metadata.get("automation") if isinstance(metadata, dict) else None
    if not isinstance(automation, dict):
        params = getattr(request, "params", None)
        automation = params.get("automation") if isinstance(params, dict) else None
    return isinstance(automation, dict) and automation.get("kind") == "heartbeat"


async def _wait_for_bounded_team_round_events(
    *,
    request_queue: asyncio.Queue,
    round_state: dict[str, Any],
    request_id: str,
    channel_id: str | None,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield events until one Cron or Heartbeat Team round is complete."""
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
        if (
            evt_type == "chat.processing_status"
            and event.get("is_processing") is False
            and event.get("is_complete") is True
        ):
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


async def _broadcast_event(
    channel_id: str | None, session_id: str, event: dict[str, Any]
) -> bool:
    """Broadcast an event and report whether any waiter received it.

    Returns:
        True when at least one waiter queue received the event.  The answer
        comes from ``TeamManager.broadcast_event`` itself, so the delivery
        decision is atomic with the broadcast.
    """
    tm = get_team_manager(channel_id)
    if event and event.get("event_type") == 'team.error':
        event.update({"event_type": "chat.error"})
    result = tm.broadcast_event(session_id, event, channel_id=channel_id)
    if inspect.isawaitable(result):
        result = await result
    _try_finish_cron_team_stream(channel_id, session_id, event)
    return bool(result)


def _approval_chunk_from_event(evt: Any) -> dict[str, Any] | None:
    if stream_chunk_writes_history(evt):
        return None
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
            await run_history_io(_persist_team_history_event, channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)

        # Replay mirrored workflow members from the checkpointed run states so
        # a full page refresh restores them grouped by ``workflow_run_id`` —
        # including members of already-finished runs, whose live events will
        # never come again. Mirrored members have no DB row, so the DB snapshot
        # cannot cover them; DB members stay authoritative, so a run member
        # sharing a DB member_id is skipped.
        get_wf_handler = getattr(team_manager, "get_workflow_handler", None)
        if get_wf_handler is not None:
            wf_handler = get_wf_handler(session_id)
            if wf_handler is not None:
                db_member_ids = {m.get("member_id", "") for m in snapshot.get("members", [])}
                # Iterative loops reuse an agent id across phases of one run;
                # like the live path's session-wide spawned_members, each
                # mirrored member is emitted exactly once per replay.
                replayed_member_ids: set[str] = set()
                for run in getattr(wf_handler, "get_run_states", lambda: {})().values():
                    if not run.id:
                        continue
                    for phase in run.phases:
                        for agent in phase.agents:
                            member_id = f"{run.id}:{agent.id}"
                            if member_id in db_member_ids or member_id in replayed_member_ids:
                                continue
                            replayed_member_ids.add(member_id)
                            event = _team_event_envelope(
                                "team.member",
                                session_id,
                                _wf_spawned_member_event(
                                    run.id,
                                    team_id,
                                    agent.model_dump(exclude_none=True),
                                    status=_wf_agent_status_to_member_status(agent.status),
                                ),
                            )
                            await run_history_io(
                                _persist_team_history_event, channel_id, session_id, event
                            )
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
            await run_history_io(_persist_team_history_event, channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)
    except Exception:
        logger.debug(
            "[TeamHelpers] failed to broadcast team state snapshot: session_id=%s",
            session_id,
        )


# Leader tools that add rows to the team roster. They only persist the member
# (status ``unstarted``); the framework publishes ``member_spawned`` when a
# member is actually started, which normally happens much later, when a message
# addressed to it wakes it up. Clients would have no roster until then.
_ROSTER_MUTATING_TOOLS = frozenset({
    "build_team",
    "spawn_teammate",
    "spawn_human_agent",
    "spawn_bridge_agent",
    "spawn_external_cli",
})


async def _read_team_roster(
    channel_id: str | None,
    session_id: str,
    team_name: str,
) -> list[dict[str, Any]]:
    """Read the team's members, leader excluded.

    Prefers the live monitor, which already drops the leader. Falls back to the
    database when the monitor is not up yet — the roster tools write their rows
    before ``team.runtime_ready`` on a freshly built team, so the fallback is
    the normal path for the very first ``build_team``, not an edge case.

    Args:
        channel_id: Channel the session belongs to.
        session_id: Session whose team is read.
        team_name: Team to read from the database in the fallback path.

    Returns:
        Member dicts shaped like ``TeamMonitorHandler.get_member_list``; empty
        when neither source can answer.
    """
    monitor_handler = get_team_manager(channel_id).get_monitor_handler(session_id)
    if monitor_handler is not None:
        members = await monitor_handler.get_member_list()
        if members:
            return members
    if not team_name:
        return []
    return await TeamMonitorHandler.get_member_list_from_db(
        team_name,
        exclude_leader=True,
    ) or []


async def _announce_team_roster(
    channel_id: str | None,
    session_id: str,
    team_name: str,
    announced_members: set[str],
) -> None:
    """Tell clients about roster members they have not been told about yet.

    A member exists from the moment the leader creates it, but nothing on the
    event bus says so until it is started. That leaves the frontend without a
    member list, so the user cannot ``@`` anyone — and ``@`` is exactly what
    starts a member (the runtime auto-starts the addressed member before
    delivering to it). This closes that loop by announcing created-but-unstarted
    members, carrying their real status so clients render them as such.

    Args:
        channel_id: Channel the session belongs to.
        session_id: Session whose roster is announced.
        team_name: Team name used when falling back to the database.
        announced_members: Member names already announced on this stream;
            updated in place so each member is announced exactly once.
    """
    try:
        members = await _read_team_roster(channel_id, session_id, team_name)
        fresh: list[dict[str, Any]] = []
        for member in members:
            candidate_id = str(member.get("member_id") or "").strip()
            member_status = str(member.get("status") or "").strip().lower()
            if member_status == MemberStatus.SHUTDOWN.value:
                continue
            if not candidate_id or candidate_id in announced_members:
                continue
            fresh.append(member)
        if not fresh:
            return
        for member in fresh:
            member_id = str(member["member_id"]).strip()
            announced_members.add(member_id)
            role = str(member.get("role") or "")
            event = {
                "event_type": "team.member",
                "session_id": session_id,
                "event": {
                    "type": "team.member.registered",
                    "team_id": team_name,
                    "member_id": member_id,
                    "name": member.get("name"),
                    "status": member.get("status"),
                    "execution_status": member.get("execution_status"),
                    # Same convention as the monitor's spawned event: clients
                    # read ``mode`` to tell a human avatar from an AI member.
                    "mode": "human" if role == TeamRole.HUMAN_AGENT.value else role,
                    "role": role,
                    # An external CLI member's role is a plain teammate; only
                    # this says which CLI backs it (claude / codex).
                    "cli_agent": member.get("cli_agent"),
                },
            }
            await run_history_io(_persist_team_history_event, channel_id, session_id, event)
            await _broadcast_event(channel_id, session_id, event)
        logger.info(
            "[TeamHelpers] announced team roster: channel_id=%s session_id=%s members=%s",
            _resolve_channel_id(channel_id),
            session_id,
            [m["member_id"] for m in fresh],
        )
    except Exception as exc:
        logger.warning(
            "[TeamHelpers] failed to announce team roster: session_id=%s error=%s",
            session_id,
            exc,
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
    # team.runtime_ready / team.completed / team.idle are leader-level control
    # events that carry no per-member content but must be forwarded to the
    # frontend.
    if chunk_type == "message" and isinstance(payload, dict):
        event_type_str = payload.get("event_type")
        if event_type_str in ("team.runtime_ready", "team.completed", "team.idle"):
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


def _enrich_teammate_event(parsed: dict[str, Any], chunk: Any) -> dict[str, Any]:
    """Enrich a parsed teammate event with role and source_member for frontend display."""
    parsed["role"] = TeamRole.TEAMMATE.value
    # TeamOutputSchema uses source_member (not member_name) for the member identifier
    source_member = getattr(chunk, "source_member", None)
    if source_member:
        parsed["member_name"] = str(source_member)
    return parsed


_TEAM_TOOL_RESULT_TEXT_LIMIT = 512


def _tag_team_output_origin(parsed: dict[str, Any], team_name: str) -> dict[str, Any]:
    """给模型产出的帧打上 team_name/team_id，供前端分流到各自的对话视图。

    一个 session 可以同时挂多个 team（TeamRuntimePool 以 team_name 为键、按
    current_session_id 归属），而 chat.delta / chat.reasoning 这些帧本身不带任何
    team 标识——前端只有一个 session_id 可用于归属，于是多个 team 的流会混进同一份
    messages 里，切换 team 时看到的是同一段对话。

    只标注模型产出与工具事件：team.member / team.task / team.completed 这类控制帧
    的归属由 payload 内层的 team_id 表达，重复注入外层 team_name 反而会让既有的
    fan_out / agent_ref 推导（_build_team_event_chunk_meta 读 event.event 内层）
    与外层字段不一致。
    """
    if not team_name:
        return parsed
    if parsed.get("event_type") not in _TEAM_ATTRIBUTED_EVENT_TYPES:
        return parsed
    parsed["team_name"] = team_name
    parsed["team_id"] = team_name
    return parsed


def _truncate_team_tool_result_event(parsed: dict[str, Any]) -> dict[str, Any]:
    """Trim large team tool result fields before forwarding them to clients."""
    if parsed.get("event_type") != "chat.tool_result":
        return parsed

    next_event = dict(parsed)
    truncated = False
    original_size = 0
    for key in ("result", "rendered_result", "raw_output"):
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
    if not _team_evolution_host_events_enabled(rail):
        logger.info(
            "[TeamHelpers] evolution monitor skipped because no signal approval is pending: "
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


def _team_evolution_host_events_enabled(rail: Any) -> bool:
    """Whether the mounted team Rail can emit host-visible approval events."""
    signal_approval_enabled = bool(
        getattr(rail, "signal_trigger", False)
        and not getattr(rail, "auto_save", False)
    )
    return signal_approval_enabled or bool(
        getattr(rail, "review_feedback_evolution_enabled", False)
    )



async def _handle_team_slash_command(
    channel_id: str | None,
    session_id: str,
    query: str,
    *,
    defer_missing_rail: bool = False,
    skills_dir: str | list[str] | None = None,
    language: str = "cn",
    evolution_enabled: bool | None = None,
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

    if evolution_enabled is None:
        evolution_enabled = _resolve_cached_team_evolution_enabled(
            get_team_manager(channel_id),
            session_id,
        )
    if not evolution_enabled:
        return {
            "output": "演进功能未启用。",
            "result_type": "error",
        }

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
    """Resolve the Skill library a team slash command evolves against.

    Teams own no Skill directory of their own — every agent reads the single
    physical library and is narrowed by visibility metadata — so the team only
    has to still be bound to the session.

    Args:
        session_id: Session running the slash command.

    Returns:
        The Skill library path, or ``None`` when the session has no team.
    """
    metadata = get_session_metadata(session_id)
    team_name = str(metadata.get("team_name") or "").strip()
    if not team_name:
        return None
    return str(get_agent_skills_dir())


def _resolve_cached_team_evolution_enabled(
    team_manager: Any,
    session_id: str,
    team_spec: Any | None = None,
) -> bool:
    """Resolve the evolution switch from in-memory team/runtime state only."""
    get_enabled = getattr(team_manager, "get_team_evolution_enabled", None)
    if callable(get_enabled):
        cached = get_enabled(session_id)
        if cached is not None:
            return bool(cached)

    candidates = [team_spec]
    get_context = getattr(team_manager, "get_team_rail_context", None)
    if callable(get_context):
        candidates.append(get_context(session_id))
    for candidate in candidates:
        if candidate is None:
            continue
        for owner in (
            candidate,
            getattr(candidate, "workspace", None),
            getattr(candidate, "team_workspace", None),
            getattr(candidate, "build_context", None),
            getattr(candidate, "context", None),
        ):
            config = getattr(owner, "config", None)
            if isinstance(config, dict):
                return get_skill_evolution_enabled(config)

    get_team_rail = getattr(team_manager, "get_team_skill_rail", None)
    return callable(get_team_rail) and get_team_rail(session_id) is not None


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


async def _consume_attach_only_stream(
    *,
    request: Any,
    team_manager: TeamManager,
    startup: AsyncExitStack,
    generation: int | None,
) -> AsyncIterator[AgentResponseChunk]:
    """Observe a running Team stream without submitting any input.

    Mirrors the persistent waiter contract: the subscription stays attached
    across Team rounds (including idle gaps and cold-started follow-ups)
    until the response stream is closed by the caller — e.g. the gateway
    tearing the stream down on disconnect. It never creates a producer or a
    Runner stream; the waiter is registered under the caller's startup lock
    so it cannot race a stream task being registered or torn down, and the
    lock is released right after registration, never held while streaming.
    """
    session_id = request.session_id or "default"
    rid = str(request.request_id or "")
    channel_id = request.channel_id
    request_queue = _new_team_event_queue()
    _add_team_waiter(
        team_manager,
        session_id,
        rid,
        request_queue,
        generation=generation,
        attach_only=True,
    )
    if not _has_team_stream_task(team_manager, session_id, generation=generation):
        # The Team stream is already idle (e.g. attaching after a finished
        # round): no round transition will ever park this execution, so align
        # it to WAITING_FOR_INPUT now — otherwise the RUNNING execution keeps
        # the Session busy (undeletable) until the connection drops.
        team_manager.report_round_state(session_id, generation, running=False)
    try:
        await startup.aclose()
        while True:
            try:
                event = await asyncio.wait_for(request_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
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
    finally:
        _remove_team_waiter(
            team_manager,
            session_id,
            rid,
            generation=generation,
        )


async def _start_team_stream_round(
    *,
    channel_id: str | None,
    session_id: str,
    request_id: str,
    team_manager: Any,
    team_name: str,
    team_spec: Any,
    query: Any,
    hide_dm: bool = False,
    debug: bool = False,
    source: str = "first",
    exclusive_waiter: bool = False,
    request_queue: asyncio.Queue | None = None,
    team_leader_identity: dict[str, Any] | None = None,
    generation: int | None = None,
    attach_waiter: bool = True,
    run_context: object | None = None,
    cross_session_marker: dict[str, Any] | None = None,
) -> asyncio.Queue | None:
    """Start a team stream round and register its waiter queue."""
    # Sync team observability with current config before streaming.
    # Runner.run_agent_team_streaming auto-attaches handlers when
    # is_initialized() is True; this call ensures init/shutdown
    # matches the latest config toggle.
    from jiuwenswarm.agents.harness.team.team_manager import sync_team_observability

    sync_team_observability()
    await team_manager.prepare_runtime_activation(session_id, team_name)
    if request_queue is None and attach_waiter:
        # Re-check right before attaching. The waiter-kind decision was made
        # before spec assembly, and a concurrent first request may have
        # registered its interactive waiter inside that window; attaching a
        # second one would duplicate every event to the frontend and double
        # the admission ownership. Exclusive (Heartbeat) waiters keep their
        # takeover semantics and are not blocked by an existing waiter.
        already_attached = team_manager.has_interactive_waiter(session_id)
        if exclusive_waiter or not already_attached:
            request_queue = _new_team_event_queue()
            _add_team_waiter(
                team_manager,
                session_id,
                request_id,
                request_queue,
                exclusive=exclusive_waiter,
                generation=generation,
            )
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
    stream_kwargs: dict[str, Any] = {
        "round_id": round_id,
        "envs": stream_envs or None,
    }
    if team_leader_identity is not None:
        stream_kwargs["team_leader_identity"] = team_leader_identity
    # The round identity lets the producer's unattended history writer
    # attribute no-waiter frames to the request that started this stream.
    stream_kwargs["round_request_id"] = request_id
    if cross_session_marker is not None:
        stream_kwargs["cross_session_marker"] = cross_session_marker
    stream = _consume_stream_with_query(
        channel_id,
        session_id,
        team_spec,
        query,
        generation=generation,
        run_context=run_context,
        **stream_kwargs,
    )
    stream_task = asyncio.create_task(stream)
    _register_team_stream_task(
        team_manager,
        session_id,
        stream_task,
        generation=generation,
    )
    return request_queue


async def process_team_message_stream(
    request: Any,
    inputs: dict[str, Any],
    deep_agent: DeepAgent,
) -> AsyncIterator[AgentResponseChunk]:
    """Hold the session startup lock until registration, never while streaming.

    ``team.design`` sessions bind their execution context here (graph,
    StoreNodeSpawner, worker base spec — see ``DesignTeamBridge.bind_round``)
    so the leader's design tools mount; everything else about the round is
    the standard team pipeline.
    """
    design_bindings = await _bind_design_round_if_needed(request)
    try:
        async with aclosing(_process_team_message_stream_locked(
            request, inputs, deep_agent
        )) as stream:
            async for chunk in stream:
                if design_bindings is not None:
                    _push_design_tool_activity(request, chunk)
                yield chunk
    finally:
        if design_bindings is not None:
            design_bindings.cleanup()
            design_bindings.push_stage("stage", "done")


async def _bind_design_round_if_needed(request: Any) -> Any:
    """Bind the design execution context when this is a team.design turn."""
    if str(request.params.get("mode") or "").strip() != NEW_TEAM_DESIGN:
        return None
    from jiuwenswarm.runtime.session.model import TEAM_ATTACH_ONLY_PARAM

    # An attach-only observer submits no input: binding would hold the
    # node_run round guard for the whole subscription lifetime.
    if request.params.get(TEAM_ATTACH_ONLY_PARAM) is True:
        return None
    from jiuwenswarm.server.runtime.design.team_bridge import DesignTeamBridge

    try:
        bindings = await DesignTeamBridge().bind_round(
            request=request, session_id=str(request.session_id or "")
        )
    except Exception:  # best-effort: binding must not kill the chat round
        logger.exception(
            "[TeamHelpers] design round bind failed: session_id=%s",
            request.session_id,
        )
        return None
    if bindings is not None:
        bindings.push_stage("thinking", "设计主编 · 处理请求（LLM）")
    return bindings


def _push_design_tool_activity(request: Any, chunk: Any) -> None:
    """Map the round's tool events onto the canvas leader-activity peek."""
    payload = chunk.payload
    if payload is None:
        return
    payload_event = str(payload.get("event_type") or "")
    if payload_event not in ("chat.tool_call", "chat.tool_result"):
        return
    import asyncio as _asyncio

    from jiuwenswarm.server.runtime.design.team_bridge import (
        activity_text,
        push_leader_activity,
    )

    channel_id = str(request.channel_id or "web")
    session_id = str(request.session_id or "")
    kind = "tool_call" if payload_event == "chat.tool_call" else "tool_result"
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        push_leader_activity(
            channel_id,
            session_id,
            kind,
            activity_text(payload),
            str(payload.get("tool_name") or ""),
        )
    )


async def _process_team_message_stream_locked(
    request: Any,
    inputs: dict[str, Any],
    deep_agent: DeepAgent,
) -> AsyncIterator[AgentResponseChunk]:
    """The original process_team_message_stream body (startup lock owner)."""
    team_manager = get_team_manager(request.channel_id)
    session_id = request.session_id or "default"
    request_id = str(request.request_id or "")
    from jiuwenswarm.runtime.session.model import (
        TEAM_EXECUTION_CONTEXT_METADATA_KEY,
        TeamExecutionContext,
    )

    request_metadata = request.metadata if isinstance(request.metadata, dict) else {}
    execution_context = request_metadata.get(TEAM_EXECUTION_CONTEXT_METADATA_KEY)
    generation = (
        execution_context.session.generation
        if isinstance(execution_context, TeamExecutionContext)
        else None
    )
    sanitized_metadata = dict(request_metadata)
    sanitized_metadata.pop(TEAM_EXECUTION_CONTEXT_METADATA_KEY, None)
    request = replace(request, metadata=sanitized_metadata)
    # 归档闸门：Team 回合从进入适配器起即算「运行中」，直到本轮 round 终止
    # （round 之前的 spec 组装 / 运行时激活等准备阶段也要覆盖）。
    begin_request = getattr(team_manager, "begin_request", None)
    end_request = getattr(team_manager, "end_request", None)
    if callable(begin_request):
        begin_request(session_id, request_id, generation=generation)
    try:
        startup_lock = team_manager.get_startup_lock(session_id)
        async with AsyncExitStack() as startup:
            await startup.enter_async_context(startup_lock)
            async with aclosing(_process_team_message_stream(
                request,
                inputs,
                deep_agent,
                team_manager=team_manager,
                startup=startup,
                execution_context=execution_context,
            )) as stream:
                async for chunk in stream:
                    # Early replies (validation errors, slash commands) also end
                    # startup ownership before handing control to the caller.
                    await startup.aclose()
                    yield chunk
    finally:
        # 兜底：提前返回（校验失败/斜杠命令）也要解除运行中标记。
        if callable(end_request):
            end_request(session_id, request_id, generation=generation)


async def _process_team_message_stream(
    request: Any,
    inputs: dict[str, Any],
    deep_agent: DeepAgent,
    *,
    team_manager: TeamManager,
    startup: AsyncExitStack,
    execution_context: object,
) -> AsyncIterator[AgentResponseChunk]:
    """Process a team-mode streaming request."""
    heartbeat_service = _TEAM_HEARTBEAT_SERVICE.get()
    session_id = request.session_id or "default"
    rid = request.request_id
    channel_id = request.channel_id
    from jiuwenswarm.runtime.session.model import (
        TEAM_ATTACH_ONLY_PARAM,
        TeamExecutionContext,
        TeamWaiterKind,
    )

    generation = (
        execution_context.session.generation
        if isinstance(execution_context, TeamExecutionContext)
        else None
    )
    waiter_kind = (
        execution_context.details.waiter_kind
        if isinstance(execution_context, TeamExecutionContext)
        else TeamWaiterKind.NONE
    )
    has_execution_context = isinstance(execution_context, TeamExecutionContext)
    run_context = _team_input_run_context(request) if has_execution_context else None

    # Attach-only subscription: entering a running Team Session observes the
    # live stream without submitting input. It must short-circuit before the
    # spec assembly and admission work below — none of that applies to a pure
    # observer, and skipping it keeps mailbox-driven rounds observable the
    # moment the user switches into the Session.
    request_params = getattr(request, "params", None)
    if (
        isinstance(request_params, dict)
        and request_params.get(TEAM_ATTACH_ONLY_PARAM) is True
    ):
        async for chunk in _consume_attach_only_stream(
            request=request,
            team_manager=team_manager,
            startup=startup,
            generation=generation,
        ):
            yield chunk
        return

    language = _resolve_request_language(request)
    # ``query`` stays the user's own words for the whole function — directive
    # stripping, ``$member`` routing and slash commands all parse it. Every
    # delivery into the team runtime goes through ``_deliverable`` instead, so
    # the leader receives exactly the envelope a single agent would.
    turn = _resolve_user_turn(inputs, channel_id=channel_id, language=language)
    query = turn.text
    try:
        group_input = _group_chat_input(request, query)
    except ValueError as exc:
        yield AgentResponseChunk(
            request_id=rid, channel_id=channel_id,
            payload={"event_type": "chat.error", "error": str(exc)}, is_complete=True,
        )
        return
    if group_input is not None:
        query = group_input
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
    # The startup lock covers this check through waiter + stream registration,
    # including the awaits in spec assembly and MCP preflight below.
    # Attach-only subscriptions receive broadcasts but never imply the Team
    # stream is alive: a waiter from them must not flip a fresh input into a
    # follow-up, which would deliver into a stopped runtime and stall on
    # ``gate_closed``.
    has_active_waiters = team_manager.has_input_waiters(session_id)
    is_first_request = (
        not await _team_session_has_runtime(team_manager, session_id)
        and not has_active_waiters
        and not team_manager.is_session_initialized(session_id)
    )
    if not is_first_request:
        # Live follow-ups must remain concurrent; they only submit an input.
        await startup.aclose()
    request_queue: asyncio.Queue | None = None
    is_heartbeat_request = (
        waiter_kind is TeamWaiterKind.EXCLUSIVE
        if has_execution_context
        else _is_heartbeat_request(request)
    )
    is_cron_request = (
        waiter_kind is TeamWaiterKind.REQUEST
        if has_execution_context
        else _is_cron_request_id(rid)
    )
    is_bounded_round = is_heartbeat_request or is_cron_request
    admission = getattr(heartbeat_service, "admission", None)
    # A cross-Session turn is executed by the mailbox worker, which already
    # holds this turn's ``session_message_run_id`` admission. Taking the
    # interactive team slot on top of it would wait for that marker to clear —
    # i.e. wait for this very turn to finish (it is released only after the
    # stream ends), a self-deadlock. ``_begin_team_round`` routes such turns
    # through the mailbox-scoped admission instead.
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

    request_params = getattr(request, "params", None)
    cross_session_marker = (
        request_params.get(SESSION_MESSAGE_INTERNAL_KEY)
        if isinstance(request_params, dict)
        else None
    )
    is_session_message_turn = isinstance(cross_session_marker, dict)
    is_session_message_steer = (
        is_session_message_turn
        and resolve_session_input_mode(request_params) is SessionInputMode.STEER
    )
    user_admitted = False
    cron_user_admitted = False
    round_submitted = False
    event_stream_cancelled = False

    async def _complete_user_submission(*, accepted: bool) -> None:
        nonlocal user_admitted
        if user_admitted:
            await admission.complete_team_user_submission(
                session_id,
                accepted=accepted,
            )
            user_admitted = False

    async def _release_cron_admission() -> None:
        nonlocal cron_user_admitted
        if cron_user_admitted:
            await admission.end_user(session_id)
            cron_user_admitted = False

    async def _release_interactive_activity() -> None:
        await admission.end_team_user(session_id)

    def _ensure_interactive_round(*, terminal_armed: bool) -> None:
        if team_manager.is_round_active(session_id):
            return
        _begin_team_manager_round(
            team_manager,
            session_id,
            rid,
            release_admission=_release_interactive_activity,
            terminal_armed=terminal_armed,
            generation=generation,
        )

    async def _begin_team_round() -> None:
        nonlocal cron_user_admitted, user_admitted
        if group_input is not None and not group_input["mentions"]:
            return
        if not is_session_message_steer:
            # Record the round identity for the unattended history writer: a
            # persistent Runner stream survives across rounds, so events that
            # no waiter receives must be attributed to the round that now
            # owns the output.  Steering inputs deliberately keep the active
            # round's identity (their output belongs to the original round).
            _ACTIVE_ROUND_HISTORY_CONTEXT[session_id] = _RoundHistoryContext(
                request_id=rid,
                cross_session_marker=(
                    cross_session_marker if isinstance(cross_session_marker, dict) else None
                ),
            )
        if not is_bounded_round:
            if is_session_message_turn and not is_session_message_steer:
                # A task-lane mailbox worker holds this turn's session-message
                # admission. Claiming the interactive team slot on top of it
                # would wait for this very turn to end, so the mailbox-scoped
                # admission performs only the accepting check. Steering uses
                # the interactive lane because it deliberately has no task
                # marker and must reach an already active Team round.
                if admission is not None:
                    await admission.begin_team_session_message(session_id)
            elif admission is not None:
                begin_team_user = getattr(
                    admission,
                    "begin_team_user",
                    admission.begin_user,
                )
                await begin_team_user(session_id)
                user_admitted = True
            # Interactive Team input keeps its original direct delivery path.
            # Heartbeat admission tracks only the active-iteration fact; it
            # must not use TeamManager round ownership as a steer admission gate.
            # Register before delivery: a fast resume (e.g. plan skip) may
            # emit its final before interact() acknowledges the submission.
            if admission is not None:
                _ensure_interactive_round(terminal_armed=is_first_request)
            return
        if is_cron_request and admission is not None:
            await admission.begin_bounded_team_user(session_id)
            cron_user_admitted = True
        try:
            _begin_team_manager_round(
                team_manager,
                session_id,
                rid,
                release_admission=(
                    _release_cron_admission if cron_user_admitted else None
                ),
                # Bounded callers keep ownership through their completion
                # observer/grace window.  This prevents late events from the
                # previous Cron round entering a new Heartbeat waiter.
                defer_terminal_release=is_bounded_round,
                # A fresh stream emits its own processing-start frame.  A
                # follow-up on a persistent stream must instead wait for the
                # first new runtime event so duplicate terminal frames from
                # the previous generation cannot release this round.
                terminal_armed=is_first_request,
                generation=generation,
            )
        except BaseException:
            await _complete_user_submission(accepted=False)
            await _release_cron_admission()
            raise

    async def _finish_round_submission(*, accepted: bool) -> None:
        nonlocal round_submitted
        round_submitted = accepted
        if is_heartbeat_request:
            if not accepted:
                await _release_team_round(
                    team_manager,
                    session_id,
                    rid,
                    generation=generation,
                )
            return
        if is_cron_request:
            if not accepted:
                await _release_team_round(
                    team_manager,
                    session_id,
                    rid,
                    generation=generation,
                )
            return
        if admission is None:
            return
        if not accepted:
            await _complete_user_submission(accepted=False)
            await _release_team_round(
                team_manager,
                session_id,
                rid,
                generation=generation,
            )
            return
        # Concurrent steers share the active round. If it already ended during
        # delivery, a late acknowledgement must not restore its busy marker.
        await _complete_user_submission(
            accepted=team_manager.is_round_active(session_id),
        )

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
            await startup.aclose()
        else:
            query = preparation.query
            query_text = query if isinstance(query, str) else ""
            hide_dm = preparation.hide_dm
            debug = preparation.debug

    try:
        request_metadata = dict(request.metadata or {})
        if group_input is not None:
            from jiuwenswarm.agents.swarm.group_chat import resolve_group_members

            roster = resolve_group_members(
                session_id, (getattr(request, "params", None) or {}).get("group_members"),
                is_first_request=is_first_request,
            )
            if roster is not None:
                request_metadata["group_members"] = roster
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
        if isinstance(getattr(request, "params", None), dict):
            request_metadata.setdefault("mode", request.params.get("mode"))
            request_metadata.setdefault(
                "supports_user_interaction",
                request.params.get("supports_user_interaction") is not False,
            )
        resolved_mode = str(request_metadata.get("mode") or "").strip()
        # Page-selected model name (from chat page model selector). Used as a
        # fallback for team members whose ``modes.team.agents.*.model`` is not
        # explicitly configured, so cluster mode honors the page model when no
        # per-agent model is set in config.yaml.
        params_obj = getattr(request, "params", None)
        agent_group_name, persist_agent_group = _resolve_agent_group_selection(
            session_id=session_id,
            params=params_obj if isinstance(params_obj, dict) else None,
            is_first_request=is_first_request,
        )
        team_leader_identity: dict[str, Any] | None = None
        if agent_group_name:
            from jiuwenswarm.server.runtime.extension_package_manager import (
                normalize_agent_group_leader_identity,
                resolve_agent_group_leader_identity,
            )

            stored_identity = normalize_agent_group_leader_identity(
                get_session_metadata(session_id, cache_bust=True).get(
                    "team_leader_identity"
                )
            )
            if stored_identity is not None:
                team_leader_identity = stored_identity
            elif persist_agent_group or is_first_request:
                try:
                    team_leader_identity = resolve_agent_group_leader_identity(
                        agent_group_name
                    )
                except Exception as identity_exc:  # noqa: BLE001 — identity is optional
                    logger.warning(
                        "[TeamHelpers] unable to resolve AgentGroup leader identity: "
                        "session_id=%s agent_group_name=%s error=%s",
                        session_id,
                        agent_group_name,
                        identity_exc,
                    )
        # Validate against the effective session binding before assembling a
        # Team or writing visibility, including follow-ups and cold recovery.
        team_skill_names = _resolve_team_skill_selection(
            params_obj if isinstance(params_obj, dict) else None,
            agent_group_name=agent_group_name,
        )
        if agent_group_name:
            request_metadata["agent_group_name"] = agent_group_name
        requested_model_name = (
            str(params_obj.get("model_name") or "").strip()
            if isinstance(params_obj, dict)
            else ""
        ) or None
        # 选的是登录送的免费模型时，按 Gateway 随请求带下来的凭据造条目（api_key 是占位值，
        # 真 token 发请求时才换上）。不传的话集群按名字查不到它，会静默用配置里的第一个模型。
        from jiuwenswarm.common.auth.login_credentials import build_login_model_entry

        login_model_entry = build_login_model_entry(
            params_obj if isinstance(params_obj, dict) else None,
            requested_model_name or "",
        )
        # Remember whether the caller explicitly selected a model.  Only an
        # explicit choice updates the long-lived Team binding; inherited
        # session metadata remains a compatibility fallback and is not
        # implicitly promoted to Team scope.
        explicit_model_selection = (
            isinstance(params_obj, dict) and "model_selection" in params_obj
        )
        model_selection = params_obj.get("model_selection") if isinstance(params_obj, dict) else None
        # Request params are client-controlled, so they carry only the stable
        # business reference; the server resolves and compiles it (a
        # caller-supplied compiled payload with runtime credentials is never
        # accepted).
        if model_selection is not None:
            from jiuwenswarm.common.model_selection import ModelSelection as SwarmModelSelection
            model_selection = SwarmModelSelection.model_validate(model_selection).model_dump()
            if get_session_metadata(session_id, cache_bust=True).get("cron_id"):
                from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver

                selected = SwarmModelSelection.model_validate(model_selection)
                model_selection = ModelSelectionResolver().restore(selected).model_dump()
        if model_selection is None:
            current_metadata = get_session_metadata(session_id, cache_bust=True)
            stored_selection = None
            team_name_for_selection = str(current_metadata.get("team_name") or "").strip()
            if team_name_for_selection:
                try:
                    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
                    stored_selection = get_team_binding_store().get_team_selection(
                        team_name_for_selection
                    )
                except Exception:
                    stored_selection = None
            if not isinstance(stored_selection, dict):
                from jiuwenswarm.server.runtime.session.model_selection_store import (
                    get_session_model_selection,
                )

                try:
                    stored = get_session_model_selection(session_id)
                except ValueError:
                    stored = None
                stored_selection = (
                    stored.model_dump(exclude_none=True) if stored is not None else None
                )
            if isinstance(stored_selection, dict):
                from jiuwenswarm.common.model_selection import ModelSelection as SwarmModelSelection
                from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver

                selected = SwarmModelSelection.model_validate(stored_selection)
                model_selection = ModelSelectionResolver().restore(selected).model_dump()
        runtime_model_selection = model_selection
        # Provider-based assembly: build members from the shared config source,
        # no pre-built parent DeepAgent required.
        # 会话级 swarmflow 配置：请求 params > metadata > config.yaml
        swarmflow_config = _resolve_session_swarmflow_config(
            params_obj if isinstance(params_obj, dict) else None,
            get_config(),
            session_id=session_id,
        )
        team_dispatch_mode = _resolve_session_team_dispatch_mode(
            params_obj if isinstance(params_obj, dict) else None,
            session_id=session_id,
        )
        team_spec = await team_manager.get_swarm_enriched_team_spec(
            session_id=session_id,
            mode=resolved_mode,
            project_dir=(
                inputs.get("project_dir") or request_metadata.get("project_dir")
            ),
            trusted_dirs=_request_trusted_dirs(request),
            request_id=rid,
            user_id=str(getattr(request, "user_id", "") or "").strip() or None,
            channel_id=channel_id,
            request_metadata=request_metadata,
            requested_model_name=requested_model_name,
            login_model_entry=login_model_entry,
            agent_group_name=agent_group_name,
            swarmflow_config=swarmflow_config,
            dispatch_mode=team_dispatch_mode,
            model_selection=runtime_model_selection,
        )
        if group_input is not None and request_metadata.get("group_members") is not None:
            update_session_metadata(session_id=session_id,
                group_members=request_metadata["group_members"],
                touch_last_message_at=False, sync_write=True)
        if isinstance(model_selection, dict) and model_selection.get("type") and model_selection.get("id"):
            from jiuwenswarm.common.model_selection import ModelSelection as SwarmModelSelection
            from jiuwenswarm.server.runtime.session.model_selection_store import (
                get_session_model_selection,
                set_session_model_selection,
            )

            selection_obj = SwarmModelSelection.model_validate(model_selection)
            normalized = selection_obj.model_dump(exclude_none=True)
            try:
                existing = get_session_model_selection(session_id)
            except ValueError:
                existing = None
            if existing is None or existing.model_dump(exclude_none=True) != normalized:
                set_session_model_selection(session_id, selection_obj)
            if explicit_model_selection:
                team_name = str(get_session_metadata(session_id, cache_bust=True).get("team_name") or "").strip()
                if team_name:
                    from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
                    binding_store = get_team_binding_store()
                    # Legacy session-only Teams may not have a binding yet;
                    # retain their session persistence without making model
                    # selection fail solely because the optional Team store
                    # is absent.
                    if binding_store.get(team_name) is not None:
                        binding_store.set_team_selection(
                            team_name=team_name,
                            selection=model_selection,
                        )
        # 请求携带了会话级配置时持久化（刷新恢复用）
        if (
            isinstance(params_obj, dict)
            and (
                params_obj.get("enable_swarmflow") is not None
                or params_obj.get("swarmflow_worktree") is not None
            )
        ):
            persist_session_swarmflow_config(session_id, swarmflow_config)
        if (
            isinstance(params_obj, dict)
            and _coerce_team_dispatch_mode(params_obj.get("team_dispatch_mode")) is not None
        ):
            persist_session_team_dispatch_mode(session_id, team_spec.dispatch_mode)
        if team_skill_names is not None:
            await _apply_team_skill_selection(
                team_manager=team_manager,
                session_id=session_id,
                team_spec=team_spec,
                skill_names=team_skill_names,
            )
        if persist_agent_group and agent_group_name:
            metadata_kwargs = {
                "session_id": session_id,
                "agent_group_name": agent_group_name,
                "touch_last_message_at": False,
                "cache_bust": True,
                "sync_write": True,
            }
            if team_leader_identity is not None:
                metadata_kwargs["team_leader_identity"] = team_leader_identity
            update_session_metadata(**metadata_kwargs)
        _persist_team_file_monitor_roots(session_id, team_spec)
        # Drop unreachable MCPs before the team runtime starts so one bad MCP
        # can't cancel the whole stream via openjiuwen's fail-fast raise.
        # Log only; the dialog proceeds without the dropped MCP.
        try:
            from jiuwenswarm.agents.swarm.assembly import preflight_team_mcps
            dropped_mcps = await preflight_team_mcps(team_spec)
            if dropped_mcps:
                logger.warning(
                    "[TeamHelpers] dropped unreachable team MCPs (dialog "
                    "continues): names=%s session_id=%s",
                    dropped_mcps, session_id,
                )
        except Exception as preflight_exc:  # noqa: BLE001 — never block the stream
            logger.warning(
                "[TeamHelpers] team MCP preflight failed (continuing): "
                "session_id=%s error=%s",
                session_id, preflight_exc,
            )
    except Exception as exc:
        logger.exception("[TeamHelpers] TeamAgent create failed: %s", exc)
        # Resolver/compiler failures can originate in provider configuration;
        # never put credentials or authorization headers into a user-visible
        # Team error payload.
        error_payload: dict[str, Any] = {"event_type": "chat.error", "error": mask_sensitive(str(exc))}
        error_code = getattr(exc, "code", None)
        if error_code:
            error_payload["code"] = str(error_code)
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            def _safe_detail(value: Any) -> Any:
                if isinstance(value, dict):
                    return {
                        key: _safe_detail(item)
                        for key, item in value.items()
                        if str(key).lower() not in {"api_key", "apikey", "secret", "token", "password", "authorization"}
                    }
                if isinstance(value, list):
                    return [_safe_detail(item) for item in value]
                if isinstance(value, str):
                    return mask_sensitive(value)
                return value

            error_payload.update(_safe_detail(details))
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=channel_id,
            payload=error_payload,
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=channel_id,
            payload={
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "is_processing": False,
                "is_complete": True,
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

    team_name = team_spec.team_name
    # Breaking change: this used to be ``<team_workspace_root>/skills``. Those
    # per-workspace Skill directories no longer exist — Skills live in exactly
    # one physical library and per-agent visibility is metadata — so a
    # workspace-local path would leave team slash commands with no Skill at all.
    team_skills_dir = str(get_agent_skills_dir())
    # No seeding here: the team's skills-visibility.json has a single writer,
    # ``TeamWorkspaceManager.initialize``, and a missing document simply means
    # "the team imposes no restriction".

    slash_result = await _handle_team_slash_command(
        channel_id,
        session_id,
        query_text,
        skills_dir=team_skills_dir,
        evolution_enabled=_resolve_cached_team_evolution_enabled(
            team_manager,
            session_id,
            team_spec,
        ),
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
            # Interactive follow-ups normally keep using the original
            # persistent waiter.  A headless Heartbeat can be the request that
            # first creates the persistent Team stream, however, and removes
            # its exclusive waiter when the bounded run ends.  In that case
            # the next real user request becomes the new persistent consumer.
            has_interactive_waiter = getattr(
                team_manager,
                "has_interactive_waiter",
                None,
            )
            needs_interactive_waiter = (
                waiter_kind is TeamWaiterKind.PERSISTENT
                if has_execution_context
                else (
                    not is_bounded_round
                    and callable(has_interactive_waiter)
                    and not has_interactive_waiter(session_id)
                )
            )
            if is_bounded_round or needs_interactive_waiter:
                request_queue = _new_team_event_queue()
                _add_team_waiter(
                    team_manager,
                    session_id,
                    rid,
                    request_queue,
                    exclusive=is_heartbeat_request,
                    generation=generation,
                )
            if query:
                # Follow-up rounds carry their own attachments and context, so
                # they are rendered exactly like the first one.
                # 暂停后恢复裁决：仅把 paused run 清单前缀注入路由到 leader 的
                # team-wide 纯文本；member/A2UI/InteractiveInput 保持原路径。
                # args 由 agent-core 从 journal 自动恢复，提示只含 resume_id+script_path。
                runs = _advisory_runs(team_manager, session_id)
                if _should_inject_swarmflow_advisory(swarmflow_config, query, runs):
                    turn = _inject_swarmflow_context(
                        turn.with_text(query), runs, cold_start=False,
                        controller=get_background_task_controller(session_id),
                    )
                    query = turn.text if isinstance(turn.text, str) else query
                followup_payload = _deliverable(turn, query)
                await _begin_team_round()
                success, reason = await _interact_with_team_context(
                    team_manager,
                    session_id,
                    followup_payload,
                    run_context=run_context,
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
                        if run_context is None:
                            boundary_result = await _deliver_followup_interact_across_boundary(
                                team_manager,
                                session_id,
                                followup_payload,
                                initial_reason=reason,
                            )
                        else:
                            boundary_result = await _deliver_followup_interact_across_boundary(
                                team_manager,
                                session_id,
                                followup_payload,
                                initial_reason=reason,
                                run_context=run_context,
                            )
                        success = boundary_result.success
                        reason = boundary_result.reason
                        first_request_ready = boundary_result.first_request_ready
                    while not success and first_request_ready:
                        # Another follow-up may already be rebuilding the same
                        # stopped runtime. Recheck after acquiring ownership.
                        await startup.enter_async_context(
                            team_manager.get_startup_lock(session_id)
                        )
                        if not await _team_session_has_runtime(team_manager, session_id):
                            break
                        await startup.aclose()
                        if run_context is None:
                            boundary_result = await _deliver_followup_interact_across_boundary(
                                team_manager,
                                session_id,
                                followup_payload,
                                initial_reason=reason,
                            )
                        else:
                            boundary_result = await _deliver_followup_interact_across_boundary(
                                team_manager,
                                session_id,
                                followup_payload,
                                initial_reason=reason,
                                run_context=run_context,
                            )
                        success = boundary_result.success
                        reason = boundary_result.reason
                        first_request_ready = boundary_result.first_request_ready
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
                        if not is_first_request:
                            await startup.aclose()
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
                        await _finish_round_submission(accepted=False)
                        final_reason = reason or ""
                        # gate_closed 是 shutdown race（leader stream 正在收尾），静默结束流
                        if final_reason == "gate_closed":
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=channel_id,
                                payload=None,
                                is_complete=True,
                            )
                            return
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

                    if not success and is_first_request:
                        # The failed follow-up is about to be retried as a new
                        # stream. Release its handoff before admitting the
                        # replacement round.
                        await _finish_round_submission(accepted=False)

                if success:
                    await _finish_round_submission(accepted=True)

            if not is_first_request:
                await startup.aclose()
                if group_input is not None and success:
                    yield AgentResponseChunk(
                        request_id=rid, channel_id=channel_id,
                        payload={"event_type": "team.group_message.accepted",
                                 "client_message_id": group_input["client_message_id"]},
                        is_complete=False,
                    )
                    if not group_input["mentions"]:
                        await _release_team_round(
                            team_manager,
                            session_id,
                            rid,
                            generation=generation,
                        )
                        yield _team_processing_done_chunk(rid, channel_id, session_id)
                        yield AgentResponseChunk(
                            request_id=rid, channel_id=channel_id, payload=None, is_complete=True,
                        )
                        return
                if is_bounded_round and request_queue is not None:
                    logger.info(
                        "[TeamHelpers] automated follow-up team request waits for round: "
                        "channel_id=%s session_id=%s request_id=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        rid,
                    )
                    round_state = new_cron_team_round_state()
                    try:
                        async for event in _wait_for_bounded_team_round_events(
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
                        _remove_team_waiter(
                            team_manager,
                            session_id,
                            rid,
                            generation=generation,
                        )
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=channel_id,
                        payload=None,
                        is_complete=True,
                    )
                    return

                if request_queue is not None:
                    logger.info(
                        "[TeamHelpers] follow-up request attached persistent waiter: "
                        "channel_id=%s session_id=%s request_id=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        rid,
                    )
                else:
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
                if request_queue is None:
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
            # Keep an attached follow-up waiter throughout fallback startup.
            # Detaching it before prepare_runtime_activation() leaves a window
            # where another follow-up installs a second persistent consumer.
            await _begin_team_round()
            # 冷启动恢复裁决：可恢复 run 清单以文本前缀注入 leader 上下文——非终态
            # 都可能被恢复，故比 follow-up 更宽；同样只动路由到 leader 的纯文本。
            runs = _advisory_runs(team_manager, session_id)
            if _should_inject_swarmflow_advisory(swarmflow_config, query, runs):
                turn = _inject_swarmflow_context(
                    turn.with_text(query), runs, cold_start=True,
                    controller=get_background_task_controller(session_id),
                )
                query = turn.text if isinstance(turn.text, str) else query
            try:
                request_queue = await _start_team_stream_round(
                    channel_id=channel_id,
                    session_id=session_id,
                    request_id=rid,
                    team_manager=team_manager,
                    team_name=team_name,
                    team_spec=team_spec,
                    query=_deliverable(turn, query),
                    hide_dm=hide_dm,
                    debug=debug,
                    source=first_request_source,
                    exclusive_waiter=is_heartbeat_request,
                    request_queue=request_queue,
                    team_leader_identity=team_leader_identity,
                    generation=generation,
                    attach_waiter=(
                        waiter_kind is not TeamWaiterKind.NONE
                        if has_execution_context
                        else True
                    ),
                    run_context=run_context,
                    cross_session_marker=(
                        cross_session_marker if isinstance(cross_session_marker, dict) else None
                    ),
                )
            except BaseException:
                _remove_team_waiter(
                    team_manager,
                    session_id,
                    rid,
                    generation=generation,
                )
                team_manager.clear_pending_runtime(session_id)
                await _finish_round_submission(accepted=False)
                raise
            await startup.aclose()
            await _finish_round_submission(accepted=True)

            if request_queue is None:
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload={
                        "event_type": "team.input.accepted",
                        "session_id": session_id,
                    },
                    is_complete=False,
                )
                # The round is owned by the background team stream (e.g. an
                # attach waiter consumes its output). The deferred marker tells
                # the Gateway not to auto-emit is_processing=False when this
                # submit-only stream ends — that emit would flash the frontend
                # as paused until the next round event arrives.
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

        try:
            if is_bounded_round and request_queue is not None:
                cron_round_state = new_cron_team_round_state()
                async for event in _wait_for_bounded_team_round_events(
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
                while _has_team_stream_task(
                    team_manager,
                    session_id,
                    generation=generation,
                ):
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
                        if group_input is not None and not group_input["mentions"]:
                            if isinstance(event, dict) and event.get("event_type") == "team.group_message.accepted":
                                identity = event.get("client_message_id") or event.get("message", {}).get(
                                    "client_message_id",
                                )
                                if identity == group_input["client_message_id"]:
                                    yield _team_processing_done_chunk(rid, channel_id, session_id)
                                    yield AgentResponseChunk(
                                        request_id=rid, channel_id=channel_id, payload=None, is_complete=True,
                                    )
                                    return
                        if isinstance(event, dict) and event.get("event_type") == "team.error":
                            break
                    except asyncio.TimeoutError:
                        if not _has_team_stream_task(
                            team_manager,
                            session_id,
                            generation=generation,
                        ):
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
            event_stream_cancelled = True
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
        logger.info(
            "[TeamHelpers] stream ended, cleared round markers: "
            "channel_id=%s session_id=%s",
            _resolve_channel_id(channel_id), session_id,
        )
    except asyncio.CancelledError:
        event_stream_cancelled = True
        raise
    finally:
        if request_queue is not None:
            _remove_team_waiter(
                team_manager,
                session_id,
                rid,
                generation=generation,
            )
            if _is_cron_request_id(rid):
                team_manager.pop_cron_completion(session_id)
            if not team_manager.has_waiters(session_id):
                logger.info(
                    "[TeamHelpers] cleared waiter set: session_id=%s",
                    session_id,
                )
        cancelled_submitted_heartbeat = (
            event_stream_cancelled
            and is_heartbeat_request
            and round_submitted
        )
        owns_cancelled_heartbeat_round = (
            cancelled_submitted_heartbeat
            and team_manager.is_round_owner(session_id, rid)
        )
        if owns_cancelled_heartbeat_round:
            # Cancellation must stop the already submitted Runner round before
            # HeartbeatExecution releases its admission/run record.  Otherwise
            # the persistent Team stream would continue as ghost automation.
            abort_task = asyncio.create_task(
                team_manager.abort_round(session_id, rid)
            )
            await asyncio.shield(abort_task)
        elif is_bounded_round or not round_submitted:
            await _release_team_round(
                team_manager,
                session_id,
                rid,
                generation=generation,
            )
        if (
            user_admitted
            and not round_submitted
            and not team_manager.is_round_owner(session_id, rid)
        ):
            await _complete_user_submission(accepted=False)
        registered_context = _ACTIVE_ROUND_HISTORY_CONTEXT.get(session_id)
        if (
            registered_context is not None
            and registered_context.request_id == rid
            and registered_context.cross_session_marker
            == (cross_session_marker if isinstance(cross_session_marker, dict) else None)
        ):
            # Only drop the entry this request installed: a newer round may
            # have already replaced it on the persistent stream.
            _ACTIVE_ROUND_HISTORY_CONTEXT.pop(session_id, None)


def _run_lamp_active(run: WorkflowRunState) -> bool:
    """Whether ``run`` still produces output, judged at agent level.

    Run-level ``not is_terminal`` over-holds the lamp: a paused run is not
    terminal yet produces nothing, so the frontend square would spin forever.
    Only an agent actually running or awaiting a human reply keeps the lamp on.
    """
    if run.is_terminal or run.status == "paused":
        return False
    return any(
        a.status in ("running", "waiting_for_human")
        for phase in run.phases
        for a in phase.agents
    )


async def _finalize_org_bound_team_after_idle(
    *,
    session_id: str,
    team_name: str,
) -> None:
    """Pause org-bound teams after a chat stream ends so organization wakes can run.

    Org background turns (unclaimed revision/expired, parent review, ...) only
    resume leaders in ``RuntimeState.PAUSED``. Call this from stream ``finally``
    (and ``team.completed``), never from ``team.idle`` while the stream is open.
    """
    name = str(team_name or "").strip()
    sid = str(session_id or "").strip()
    if not name or not sid:
        return
    try:
        from openjiuwen.agent_teams.runtime.pool import RuntimeState
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        from jiuwenswarm.agents.harness.team.team_manager import (
            _runner_team_runtime_manager,
        )

        runtime = _runner_team_runtime_manager(GLOBAL_RUNNER)
        entry = await runtime.pool.get(name)
        if entry is None or getattr(entry, "current_session_id", None) != sid:
            return
        if getattr(entry, "state", None) is RuntimeState.PAUSED:
            return
        backend = getattr(getattr(entry, "agent", None), "team_backend", None)
        if getattr(backend, "org_task_manager", None) is None:
            return
        await runtime.finalize(team_name=name, session_id=sid)
    except Exception:
        logger.exception(
            "[TeamHelpers] finalize org-bound team after idle failed "
            "session_id=%s team_name=%s",
            sid,
            name,
        )


async def _consume_stream_with_query(
    channel_id: str | None,
    session_id: str,
    team_spec: Any,
    initial_query: Any,
    *,
    round_id: int,
    envs: dict[str, Any] | None = None,
    team_leader_identity: dict[str, Any] | None = None,
    generation: int | None = None,
    run_context: object | None = None,
    round_request_id: str | None = None,
    cross_session_marker: dict[str, Any] | None = None,
) -> None:
    """Consume the team stream in the background and broadcast parsed events."""
    _envs = envs or {}
    hide_dm: bool = bool(_envs.get("hide_dm", False))
    received_chunks = 0
    first_model_output_at: float | None = None
    emitted_ask_user_request_ids: set[str] = set()
    terminal_broadcasted = False
    # Members already announced to clients on this stream, so a roster refresh
    # only emits what is new. See _announce_team_roster.
    announced_members: set[str] = set()
    roster_team_name = str(getattr(team_spec, "team_name", "") or "")
    lg: TeamStreamLogger | None = None
    stream_cancelled = False
    # History writer for frames that no waiter receives: the durable pipeline
    # on the waiter side only sees delivered events, so the producer persists
    # the rest itself.  Starts as the round that cold-started this stream and
    # rotates when a newer round takes over the output.
    writer: _UnattendedTeamRoundHistoryWriter | None = _UnattendedTeamRoundHistoryWriter(
        channel_id=channel_id,
        session_id=session_id,
        request_id=str(round_request_id or ""),
        cross_session_marker=(
            cross_session_marker if isinstance(cross_session_marker, dict) else None
        ),
    )

    async def _deliver(event: dict[str, Any]) -> None:
        """Broadcast one event and persist it when no waiter received it."""
        nonlocal writer
        delivered = await _broadcast_event(channel_id, session_id, event)
        if writer is None:
            return
        try:
            if delivered:
                await writer.handover(event)
                return
            context = _ACTIVE_ROUND_HISTORY_CONTEXT.get(session_id)
            if (
                context is not None
                and writer.request_id != context.request_id
            ):
                # A newer round now owns the output on this persistent stream:
                # flush the previous writer's buffers and rotate the identity so
                # the records stay attributed to the round that produced them.
                await writer.flush_pending_final()
                await writer.flush_pending_reasoning()
                writer = _UnattendedTeamRoundHistoryWriter(
                    channel_id=channel_id,
                    session_id=session_id,
                    request_id=context.request_id,
                    cross_session_marker=context.cross_session_marker,
                )
            await writer.observe(event)
        except Exception:
            # History persistence must never break event delivery; the
            # durable pipeline isolates its write failures the same way.
            logger.warning(
                "[TeamHelpers] unattended history persist failed: session_id=%s",
                session_id,
                exc_info=True,
            )

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
        await _deliver(
            {
                "event_type": "chat.processing_status",
                "session_id": session_id,
                "rid": round_id,
                "is_processing": True,
                "is_complete": False,
            },
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
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import (
            get_kv_cache_runtime,
        )

        team_session = create_agent_team_session(
            session_id=session_id,
            kv_cache_runtime=get_kv_cache_runtime(),
        )
        if run_context is None:
            runner_stream = Runner.run_agent_team_streaming(
                agent_team=team_spec,
                inputs={"query": initial_query},
                session=team_session,
                envs=envs,
                stream_logger=lg,
                background_task_controller=get_background_task_controller(session_id),
            )
        else:
            runner_stream = Runner.run_agent_team_streaming(
                agent_team=team_spec,
                inputs={"query": initial_query},
                session=team_session,
                envs=envs,
                stream_logger=lg,
                background_task_controller=get_background_task_controller(session_id),
                run_context=run_context,
            )
        async for chunk in runner_stream:
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
            parsed = await run_stream_parser(parse_stream_chunk, chunk)
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
                # Preserve foreground Team reasoning visibility. Expert streams
                # use their own organization relay and conversation runtime.
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
                # 标注来源 team，让前端能把这一帧分流到对应 team 的对话视图。
                # 放在 role 标记之前：_enrich_teammate_event 会覆盖 member_name，
                # 但不动 team_name，顺序上只是先钉住归属再补身份。
                parsed = _tag_team_output_origin(parsed, roster_team_name)
                if is_teammate:
                    parsed = _enrich_teammate_event(parsed, chunk)
                elif is_leader:
                    # 标记 role=leader，使 _build_logical_targets() 走 godview 兜底
                    # （leader 不在 _ROLE_FANOUT 中，落到 [godview]）。
                    parsed["role"] = TeamRole.LEADER.value
                parsed = _truncate_team_tool_result_event(parsed)
                parsed = _attach_team_leader_identity(parsed, team_leader_identity)
                if parsed.get("event_type") == "team.runtime_ready":
                    ready_team_name = str(parsed.get("team_name") or team_spec.team_name)
                    activation_kind = str(parsed.get("activation_kind") or "").strip()
                    sync_team_identity_metadata(
                        channel_id=channel_id,
                        session_id=session_id,
                        ready_team_name=ready_team_name,
                        activation_kind=activation_kind,
                        team_leader_identity=team_leader_identity,
                    )
                    tm = get_team_manager(channel_id)
                    tm.commit_runtime_ready(session_id, ready_team_name)
                    from jiuwenswarm.server.runtime.group_service import (
                        read_group, resolve_group_backend, sync_group_roster,
                    )

                    group = read_group(session_id)
                    if group is not None:
                        backend = await resolve_group_backend(group)
                        if backend is not None:
                            await sync_group_roster(group, backend)
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
                    ensure_team_evolution_watcher(
                        channel_id,
                        session_id,
                        source="runtime_ready",
                    )
                    # A resumed team brings its whole roster back with it and
                    # emits no member event for anyone who is merely persisted,
                    # so announce here as well as after the roster tools.
                    roster_team_name = ready_team_name
                    await _announce_team_roster(
                        channel_id,
                        session_id,
                        roster_team_name,
                        announced_members,
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
                    await _deliver(
                        {
                            "event_type": "chat.error",
                            "error": error_msg,
                            "reason": reason,
                            "session_id": session_id,
                            "rid": round_id,
                        },
                    )
                    await _deliver(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": round_id,
                            "is_processing": False,
                            "is_complete": True,
                        },
                    )
                    terminal_broadcasted = True
                    continue
                elif parsed.get("event_type") == "team.completed":
                    # Team completed this round — broadcast a single
                    # round-complete signal that also carries team stats.
                    logger.info(
                        "[TeamHelpers] team is completed: channel_id=%s session_id=%s member_count=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        parsed.get("member_count"),
                    )
                    await _deliver(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": round_id,
                            "is_processing": False,
                            "is_complete": True,
                            "member_count": parsed.get("member_count"),
                            "task_count": parsed.get("task_count"),
                        },
                    )
                    terminal_broadcasted = True
                    await _finalize_org_bound_team_after_idle(
                        session_id=session_id,
                        team_name=roster_team_name,
                    )
                    continue
                elif parsed.get("event_type") == "team.idle":
                    # A swarmflow workflow may still be running while the leader
                    # is idle; do not end the round until it reaches terminal.
                    tm = get_team_manager(channel_id)
                    wf_handler = tm.get_workflow_handler(session_id)
                    if wf_handler is not None and any(
                        _run_lamp_active(run) for run in wf_handler.get_run_states().values()
                    ):
                        logger.info(
                            "[TeamHelpers] team idle ignored (workflow still running): "
                            "channel_id=%s session_id=%s",
                            _resolve_channel_id(channel_id),
                            session_id,
                        )
                        # Park the marker: a later pause/stop that drains the
                        # active set must be able to release it (see
                        # _consume_workflow_events).
                        tm.hold_idle(
                            session_id,
                            {"rid": round_id, "member_count": parsed.get("member_count")},
                        )
                        continue
                    tm.pop_held_idle(session_id)
                    # Every member has been at rest for the framework's debounce
                    # window: nothing is producing output any more, even though
                    # the leader stream deliberately stays open in case the team
                    # gets woken again. Clients should stop showing the round as
                    # running, so this is reported exactly like team.completed —
                    # the difference (stream still open) is invisible to them,
                    # and later output re-opens the running state on its own.
                    logger.info(
                        "[TeamHelpers] team went idle: channel_id=%s session_id=%s member_count=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        parsed.get("member_count"),
                    )
                    await _deliver(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": round_id,
                            "is_processing": False,
                            "is_complete": True,
                            "member_count": parsed.get("member_count"),
                        },
                    )
                    terminal_broadcasted = True
                    # Do not finalize here: the leader stream stays open after
                    # idle. Org-bound pause happens in finally / team.completed.
                    continue
                elif (
                    is_leader
                    and parsed.get("event_type") == "chat.tool_result"
                    and str(parsed.get("tool_name") or "") in _ROSTER_MUTATING_TOOLS
                ):
                    # The leader just added members. They exist in the database
                    # but stay silent until something starts them, so announce
                    # the roster now — that is what makes them addressable, and
                    # addressing one is what starts it.
                    await _deliver(parsed)
                    await _announce_team_roster(
                        channel_id,
                        session_id,
                        roster_team_name,
                        announced_members,
                    )
                    continue
                elif parsed.get("event_type") == "chat.error":
                    await _deliver(parsed)
                    if is_leader:
                        await _deliver(
                            {
                                "event_type": "chat.final",
                                "content": "",
                                "session_id": session_id,
                                "rid": round_id,
                            },
                        )
                        await _deliver(
                            {
                                "event_type": "chat.processing_status",
                                "session_id": session_id,
                                "rid": round_id,
                                "is_processing": False,
                                "is_complete": True,
                            },
                        )
                        terminal_broadcasted = True
                    continue
                if parsed.get("event_type") == "chat.final":
                    # A persistent Team stream may emit several leader finals
                    # within one real round.  They are result content, not a
                    # lifecycle boundary.  Only team.completed/team.idle,
                    # leader error, or stream close emits the terminal
                    # processing_status; otherwise an early final can release
                    # admission and a later final/terminal pair can corrupt the
                    # next round's waiter and ownership.
                    await _deliver(parsed)
                    continue
                await _deliver(parsed)

        # If stream ended without any chunks, broadcast an error event
        if received_chunks == 0:
            logger.warning(
                "[TeamHelpers] stream ended with no output: channel_id=%s session_id=%s",
                _resolve_channel_id(channel_id),
                session_id,
            )
            await _deliver(
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
            await _deliver(
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
        # Flush & close the stream trace logger if one was opened.
        if lg is not None:
            try:
                lg.flush()
            except Exception as e:
                logger.warning(f"TeamStreamLogger flush failed, error is {e}")
        try:
            if not stream_cancelled and not terminal_broadcasted:
                # Broadcast team.completed so cron round watchers (both the
                # adapter's bounded round waiter and the cron
                # scheduler's own round_state) can finalise when the stream
                # ends normally without a terminal event.  A cancelled stream
                # must not re-enter bounded waiter backpressure during cleanup.
                await _broadcast_team_state_snapshot(channel_id, session_id)
                # Also broadcast chat.processing_status{is_processing:False} so
                # the frontend gets an explicit terminal signal even when the
                # agent-core team stream generator silently returns without
                # emitting team.completed / team.idle.
                try:
                    await _deliver(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": round_id,
                            "is_processing": False,
                            "is_complete": True,
                        },
                    )
                    logger.info(
                        "[TeamHelpers] team finally completed: channel_id=%s session_id=%s round_id=%s",
                        _resolve_channel_id(channel_id),
                        session_id,
                        round_id,
                    )
                except Exception:
                    logger.debug(
                        "[TeamHelpers] failed to broadcast team.completed on stream end: "
                        "session_id=%s",
                        session_id,
                    )
                await _finalize_org_bound_team_after_idle(
                    session_id=session_id,
                    team_name=roster_team_name,
                )
        finally:
            try:
                if writer is not None:
                    # Mirror the durable pipeline's stream end: only trailing
                    # reasoning gets a fallback record; half-streamed final
                    # text is deliberately not written as a chat.final.
                    await writer.flush_pending_reasoning()
            except Exception:
                logger.debug(
                    "[TeamHelpers] unattended history reasoning flush failed: "
                    "session_id=%s",
                    session_id,
                    exc_info=True,
                )
            # Registry release must run even if cancellation arrives while a
            # normal stream is delivering its final snapshot.
            team_manager = get_team_manager(channel_id)
            release_current_round = getattr(
                team_manager, "release_current_round", None
            )
            if callable(release_current_round):
                await release_current_round(session_id)
            team_manager.clear_pending_runtime(session_id)
            clear_active_runtime = getattr(team_manager, "clear_active_runtime", None)
            if callable(clear_active_runtime):
                clear_active_runtime(session_id)
            _pop_team_stream_task(
                team_manager,
                session_id,
                generation=generation,
            )


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
            await run_history_io(_persist_team_history_event, channel_id, session_id, event)
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
    # paused stays in_progress (same column, same start time) but freezes the
    # board's visual progress: the board eases in_progress tasks toward 85% on
    # wall-clock alone, so an untouched paused phase keeps creeping, while a
    # status change to blocked/pending snaps the bar to zero. progress_frozen
    # (set below) stops the easing clock instead; "running" thaws it.
    "paused": ("team.task.paused", "in_progress"),
    "completed": ("team.task.completed", "completed"),
    "failed": ("team.task.cancelled", "cancelled"),
    "stopped": ("team.task.cancelled", "cancelled"),
}

# At workflow terminal status, a phase still "planned" never started (script
# returned without calling agent(), or failed early). workflow_state keeps
# "planned" on purpose (TUI snapshot shows "not executed"), but the web task
# panel maps "planned" to "pending" (waiting), leaving a stale card forever.
# Folded here at the web conversion layer: terminal planned -> team.task.skipped
# / cancelled, clearing the card. type=skipped distinguishes it from a run
# aborted mid-flight (stopped -> team.task.cancelled). content carries an i18n
# key (prefixed "i18n:") so the frontend translates it per the current UI
# language; plain user/agent text is never prefixed.
_WF_TERMINAL_PLANNED_TASK: tuple[str, str] = ("team.task.skipped", "cancelled")
_WF_TERMINAL_PLANNED_CONTENT = "i18n:team.taskDetail.skipReasonPlanned"


def _team_event_envelope(
    category: str, session_id: str, event: dict[str, Any]
) -> dict[str, Any]:
    """Wrap an inner team event dict in the standard broadcast envelope.

    The emission time rides on the inner event: the frontend dedups identical
    payloads within 1.5s, so a phase status that legitimately repeats (a card
    re-entered between same-phase agent events) must carry a distinguishing
    timestamp or the second emission is dropped as a redelivery and the task
    card sticks on the stale status.
    """
    event = {**event, "timestamp": int(time.time() * 1000)}
    return {"event_type": category, "session_id": session_id, "event": event}


def _wf_spawned_member_event(
    run_id: str,
    team_id: str,
    agent: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Build the spawned ``team.member`` event for a workflow worker.

    Single construction point shared by the live conversion
    (``_workflow_updated_to_team_events``) and the team snapshot replay, so a
    mirrored member always carries the same identity fields. The caller
    supplies the member ``status``: the live path always spawns ``busy`` and
    lands the real status in a later status_changed, while the snapshot replay
    maps the agent node status directly (no follow-up delta exists there).
    """
    agent_id = str(agent.get("id") or "")
    event: dict[str, Any] = {
        "type": "team.member.spawned",
        "team_id": team_id,
        "member_id": f"{run_id}:{agent_id}",
        "name": agent.get("name") or agent_id,
        "status": status,
        "workflow_run_id": run_id,
    }
    # Expert nodes keep the same shape as plain workers (name = script label) and
    # carry the expert identity alongside, so the roster can show it as a hint
    # without losing the label — the one thing that tells two calls of one expert apart.
    if agent.get("agent_template"):
        event["agent_template"] = agent["agent_template"]
        event["display_name"] = agent.get("display_name") or agent["agent_template"]
    # Same identity fields the roster uses for real members, so the member
    # list resolves the same avatar as the workflow tree.
    if agent.get("agent_type"):
        event["cli_agent"] = agent["agent_type"]
    elif agent.get("kind") == "human":
        event["role"] = TeamRole.HUMAN_AGENT.value
        event["mode"] = "human"
    return event


def _wf_agent_status_to_member_status(agent_status: str) -> str:
    """Map a workflow agent node status onto the member busy/idle convention.

    The live mirror spawns a worker ``busy`` and only lands the raw terminal
    status through a later status_changed. The snapshot replay emits a single
    spawned event per node with no follow-up delta, so it folds that two-step
    result in: a node still producing (running, or awaiting a human reply) stays
    ``busy``, anything terminal maps to ``idle``.
    """
    return "busy" if agent_status in ("running", "waiting_for_human") else "idle"


@dataclass(slots=True)
class _WorkflowMirrorDedup:
    """Session-scoped dedup tables behind the ``workflow.updated`` → team mirror.

    The tables belong to the workflow handler (they must outlive one consumer
    loop so a resume replay is not re-emitted); this bundle only carries them
    by reference into the converter.
    """

    seen_phase: dict[str, str] = field(default_factory=dict)
    seen_agent: dict[str, str] = field(default_factory=dict)
    spawned_members: set[str] = field(default_factory=set)
    seen_activity: dict[str, set[tuple[str, str]]] = field(default_factory=dict)


def _workflow_updated_to_team_events(
    event: dict[str, Any],
    session_id: str,
    dedup: _WorkflowMirrorDedup,
) -> list[dict[str, Any]]:
    """Convert one ``workflow.updated`` event into web ``team.member`` / ``team.task`` events.

    Each swarmflow phase becomes a ``team.task`` and each worker (agent) becomes a
    ``team.member``. Only status *changes* produce events — the ``workflow.updated``
    delta repeatedly re-includes a running phase (once per agent that starts inside
    it), so ``dedup.seen_phase`` / ``dedup.seen_agent`` dedup by last-observed status.

    Worker tool activity mirrors onto the member call list through the same
    ``chat.tool_call`` / ``chat.tool_result`` frames real teammates produce
    (``role="teammate"`` + ``member_name`` gate the frontend into its existing
    hidden-teammate execution-event chain): a ``tool_call`` row emits one
    ``chat.tool_call`` frame, a paired ``tool_result_preview`` emits a matching
    ``chat.tool_result`` frame with the same ``tool_call_id`` (the frontend's
    existing call/result pairing links them), and an orphan ``tool_result`` row
    emits a lone ``chat.tool_result`` frame. ``dedup.seen_activity`` (per member,
    ``(type, tool_call_id)`` keys) dedups rows that a later delta re-includes.
    """
    if event.get("event_type") != "workflow.updated":
        return []

    wf = event.get("workflow") or {}
    run_id = str(wf.get("id") or "")
    team_id = str(wf.get("name") or run_id or "swarmflow")
    if not run_id:
        return []
    seen_phase = dedup.seen_phase
    seen_agent = dedup.seen_agent
    spawned_members = dedup.spawned_members
    seen_activity = dedup.seen_activity

    # At workflow terminal status, a still-planned phase never started; map it
    # via _WF_TERMINAL_PLANNED_TASK to skipped/cancelled with a reason string,
    # so the task panel does not leave a stale waiting card.
    wf_status = (wf.get("status") or "").strip()
    wf_terminal = wf_status in ("completed", "failed", "stopped")

    out: list[dict[str, Any]] = []

    for phase in wf.get("phases", []) or []:
        phase_id = phase.get("id")
        status = phase.get("status")
        if not phase_id or not status:
            continue
        task_id = f"{run_id}:{phase_id}"
        terminal_planned = wf_terminal and status == "planned"
        # Use a suffixed seen_phase key for terminal planned so the skipped
        # record does not clobber / get clobbered by the non-terminal pending
        # record, which would drop the second event via dedup.
        seen_key = f"{task_id}#skipped" if terminal_planned else task_id
        effective_status = "skipped" if terminal_planned else status
        if seen_phase.get(seen_key) != effective_status:
            seen_phase[seen_key] = effective_status
            mapping = (
                _WF_TERMINAL_PLANNED_TASK
                if terminal_planned
                else _WF_PHASE_STATUS_TO_TASK.get(status)
            )
            if mapping is not None:
                task_type, task_status = mapping
                task_event: dict[str, Any] = {
                    "type": task_type,
                    "team_id": team_id,
                    "task_id": task_id,
                    "title": phase.get("name") or phase_id,
                    "status": task_status,
                    "workflow_run_id": run_id,
                }
                if status in ("paused", "running"):
                    task_event["progress_frozen"] = status == "paused"
                if terminal_planned:
                    task_event["content"] = _WF_TERMINAL_PLANNED_CONTENT
                out.append(
                    _team_event_envelope("team.task", session_id, task_event)
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
                        _wf_spawned_member_event(run_id, team_id, agent, status="busy"),
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
                                "workflow_run_id": run_id,
                            },
                        )
                    )

            # Mirror worker tool activity into the member call list. Each row
            # becomes the same chat.tool_call / chat.tool_result frame real
            # teammates produce, keyed on the worker mirror; a paired
            # tool_result_preview emits a matching chat.tool_result frame with
            # the same tool_call_id so the frontend pairs call + result.
            activity = agent.get("activity") or []
            if activity:
                seen_rows = seen_activity.setdefault(member_id, set())
                for row in activity:
                    if not isinstance(row, dict):
                        continue
                    row_type = row.get("type")
                    tool_call_id = row.get("tool_call_id")
                    row_content = row.get("content") or ""
                    row_key = (
                        row_type,
                        tool_call_id if tool_call_id else f"no-id:{row_content}",
                    )
                    # role=teammate + member_name gate the frame into the
                    # frontend's hidden-teammate execution-event chain, exactly
                    # like _enrich_teammate_event does for real teammate frames.
                    base: dict[str, Any] = {
                        "session_id": session_id,
                        "role": TeamRole.TEAMMATE.value,
                        "member_name": member_id,
                    }
                    if row.get("timestamp"):
                        base["timestamp"] = row["timestamp"]
                    preview = row.get("tool_result_preview") if row_type == "tool_call" else None
                    preview_key = (
                        "tool_result",
                        tool_call_id if tool_call_id else f"no-id:{preview or ''}",
                    )
                    if row_key in seen_rows:
                        # The call row was mirrored before its result paired back
                        # (the call outlived the activity batch window): the
                        # preview is the only new information, so emit just its
                        # result frame — once.
                        if preview is not None and preview_key not in seen_rows:
                            seen_rows.add(preview_key)
                            out.append(_activity_result_frame(base, row, tool_call_id, preview))
                        continue
                    seen_rows.add(row_key)
                    if row_type == "tool_call":
                        frame: dict[str, Any] = {
                            **base,
                            "event_type": "chat.tool_call",
                            # name rides the nested tool_call (the normalizer's
                            # first candidate); description carries the row
                            # content so the member call list renders it
                            # (description is its first display candidate).
                            "tool_call": {
                                "name": row.get("tool_name") or "unknown",
                                "description": row_content,
                            },
                        }
                        if tool_call_id:
                            frame["tool_call"]["id"] = tool_call_id
                        out.append(frame)
                        if preview is not None:
                            seen_rows.add(preview_key)
                            out.append(_activity_result_frame(base, row, tool_call_id, preview))
                    elif row_type == "tool_result":
                        out.append(_activity_result_frame(base, row, tool_call_id, row_content))

    return out


def _activity_result_frame(
    base: dict[str, Any], row: dict[str, Any], tool_call_id: Any, result: str,
) -> dict[str, Any]:
    """One ``chat.tool_result`` frame for a mirrored activity row (paired or orphan)."""
    return {
        **base,
        "event_type": "chat.tool_result",
        "tool_name": row.get("tool_name") or "unknown",
        **({"tool_call_id": tool_call_id} if tool_call_id else {}),
        "result": result or "",
    }


def _detect_human_waiting_prompts(
    wf: dict[str, Any],
    session_id: str,
    seen_human_waiting: set[str],
) -> list[dict[str, Any]]:
    """检测新进入 waiting_for_human 的 human/human_session agent。

    为每个**首次**进入 waiting_for_human 的 agent 生成一个
    ``chat.ask_user_question`` 事件，使 web 端 ``InteractionPrompt``
    能展示 human_prompt 并收集用户回复。agent 离开该状态后从集合
    移除，允许后续多轮 human_session 再次触发。
    """
    out: list[dict[str, Any]] = []
    run_id = str(wf.get("id") or "")
    if not run_id:
        return out

    for phase in wf.get("phases", []) or []:
        phase_name = phase.get("name") or phase.get("id") or ""
        for agent in phase.get("agents", []) or []:
            agent_id = agent.get("id") or ""
            agent_status = agent.get("status") or ""
            member_key = f"{run_id}:{agent_id}"

            if agent_status == "waiting_for_human" and member_key not in seen_human_waiting:
                seen_human_waiting.add(member_key)
                correlation_id = agent.get("correlation_id") or agent_id
                agent_name = agent.get("name") or agent_id
                human_prompt = agent.get("human_prompt") or ""

                out.append({
                    "event_type": "chat.ask_user_question",
                    "session_id": session_id,
                    "request_id": f"swarmflow:{run_id}:{correlation_id}",
                    "source": "swarmflow_human",
                    "questions": [{
                        "question": human_prompt or "(SwarmFlow is waiting for your input)",
                        "header": f"{agent_name} · {phase_name}",
                        "options": [],
                        "multi_select": False,
                    }],
                    "swarmflow_meta": {
                        "run_id": run_id,
                        "correlation_id": correlation_id,
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                    },
                })
            elif agent_status != "waiting_for_human" and member_key in seen_human_waiting:
                seen_human_waiting.discard(member_key)

    return out


async def _consume_workflow_events(
    channel_id: str | None,
    session_id: str,
    workflow_handler: WorkflowMonitorHandler,
) -> None:
    """Consume workflow events in the background and broadcast them.

    所有通道都收到原始 ``workflow.updated`` 事件（供 web 端树视图渲染）。
    非 TUI 通道额外转换为 ``team.member`` / ``team.task`` 扁平事件（向后兼容），
    并检测 ``waiting_for_human`` agent 生成 ``chat.ask_user_question`` 事件。
    """
    is_tui = _resolve_channel_id(channel_id) == "tui"
    # Phase/agent dedup lives on the handler (session-scoped): this loop is
    # cancelled on team pause, and a resume relaunch replays the cached
    # prefix — a fresh per-loop table would re-emit the replay as new tasks.
    mirror_dedup = _WorkflowMirrorDedup(
        seen_phase=workflow_handler.seen_phase,
        seen_agent=workflow_handler.seen_agent,
        spawned_members=workflow_handler.spawned_members,
        seen_activity=workflow_handler.seen_activity,
    )
    seen_human_waiting: set[str] = set()
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
            wf_status = (wf.get("status") or "").strip()

            # ── 所有通道都广播原始 workflow.updated（供 web 树视图渲染）──
            # channel_id 随事件传下去：终态事件在 waiter 已被 cancel 拆除时，
            # broadcast_event 用它走 gateway server-push 兜底投递。
            await _broadcast_event(channel_id, session_id, event)

            if not is_tui and wf.get("budget_exhausted_scope") == "session":
                await _broadcast_event(
                    channel_id,
                    session_id,
                    {
                        "event_type": "chat.error",
                        "session_id": session_id,
                        "code": "SWARMFLOW_SESSION_BUDGET_EXHAUSTED",
                        "error": "Session budget exhausted",
                    },
                )

            # The idle guard may be holding a swallowed team.idle for this
            # session. A pause / stop takes the run out of the active set
            # without waking the leader, so nothing else will re-emit idle:
            # release it here, same shape the guard would have broadcast.
            # Natural completion / failure must NOT release it — the completion
            # injection wakes the leader, whose own idle turns the lamp off
            # after it reports; releasing on the last agent_completed turned
            # the lamp off ~12ms before that wake-up and it flickered back on.
            run_states = getattr(workflow_handler, "get_run_states", lambda: {})()
            if wf_status in ("paused", "stopped") and not any(
                _run_lamp_active(r) for r in run_states.values()
            ):
                held = get_team_manager(channel_id).pop_held_idle(session_id)
                if held is not None:
                    logger.info(
                        "[TeamHelpers] releasing held team.idle (no active run): "
                        "channel_id=%s session_id=%s",
                        _resolve_channel_id(channel_id), session_id,
                    )
                    await _broadcast_event(
                        channel_id,
                        session_id,
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "rid": held.get("rid"),
                            "is_processing": False,
                            "is_complete": True,
                            "member_count": held.get("member_count"),
                        },
                    )

            if is_tui:
                # TUI: 只需原始事件 + 终态检查
                if wf_status in ("completed", "failed", "stopped"):
                    logger.info(
                        "[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s",
                        _resolve_channel_id(channel_id), session_id, wf_status,
                    )
                continue

            # ── 非 TUI: 额外转换扁平 team.task/team.member 事件（向后兼容）──
            for team_ev in _workflow_updated_to_team_events(
                event, session_id, mirror_dedup,
            ):
                await run_history_io(_persist_team_history_event, channel_id, session_id, team_ev)
                await _broadcast_event(channel_id, session_id, team_ev)

            # ── 非 TUI: 检测 waiting_for_human → chat.ask_user_question ──
            for human_ev in _detect_human_waiting_prompts(wf, session_id, seen_human_waiting):
                await _broadcast_event(channel_id, session_id, human_ev)

            # ── 终态处理 ──
            if wf_status in ("completed", "failed", "stopped"):
                logger.info(
                    "[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s",
                    _resolve_channel_id(channel_id), session_id, wf_status,
                )
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


@dataclass
class _RoundHistoryContext:
    """Identity of the Team round that currently owns unattended history writes."""

    request_id: str
    cross_session_marker: dict[str, Any] | None


# Keyed by session_id only: Team runtime resources are identified by
# (session_id, generation) and channel_id never participates in instance
# selection, so a follow-up from any channel must hit the entry.
_ACTIVE_ROUND_HISTORY_CONTEXT: dict[str, _RoundHistoryContext] = {}

# Event types that end the current output segment: the durable pipeline
# flushes accumulated delta text before persisting them.
_UNATTENDED_BOUNDARY_EVENT_TYPES = (
    "chat.tool_call",
    "chat.ask_user_question",
    "harness.activate_interaction",
)


class _UnattendedTeamRoundHistoryWriter:
    """Persist Team chat events that no response waiter received.

    The session-level producer must keep consuming and persisting Runner
    events even when no foreground waiter is attached.  Team chat history is
    normally written by the waiter-attached response stream
    (``process_message_stream``'s durable pipeline); this writer mirrors
    those rules for the frames that land on nobody, so a mailbox-driven
    round stays visible when the user later opens the Session.

    Known divergences from the durable pipeline (accepted, follow-ups):
    teammate A2UI block buffering and ``context.compression_state`` are not
    mirrored, so those frames keep their raw payload.
    """

    def __init__(
        self,
        *,
        channel_id: str | None,
        session_id: str,
        request_id: str,
        cross_session_marker: dict[str, Any] | None,
        mode: str = "team",
    ) -> None:
        self._channel_id = channel_id
        self._session_id = session_id
        self._request_id = str(request_id or "").strip()
        self._cross_session_marker = cross_session_marker
        self._mode = mode
        self._pending_final_chunks: list[str] = []
        self._pending_final_started_at: float | None = None
        self._pending_final_order: Any = None
        self._pending_reasoning_chunks: list[str] = []
        self._reasoning_started_at_ms: float | None = None
        self._reasoning_updated_at_ms: float | None = None
        self._reasoning_order: Any = None
        self._durable_final_content = ""

    @property
    def request_id(self) -> str:
        """Return the request identity used for the records this writer emits."""
        return self._request_id

    @property
    def cross_session_marker(self) -> dict[str, Any] | None:
        """Return the cross-Session marker attached to the records."""
        return self._cross_session_marker

    async def observe(self, event: dict[str, Any]) -> None:
        """Apply the durable pipeline rules to one unattended event."""
        if not isinstance(event, dict):
            return
        evt_type = str(event.get("event_type") or "")
        recordable = (
            evt_type.startswith("chat.")
            or evt_type == "context.usage"
            or evt_type == "team.message"
        )
        if not recordable:
            return
        if evt_type.startswith("chat.processing_status"):
            # Control frames carry no restorable payload; the history shell
            # filter would drop them anyway, so skip the write entirely.
            return
        if evt_type == "chat.delta":
            content = str(event.get("content") or "")
            if content:
                if self._pending_final_started_at is None:
                    self._pending_final_started_at = time.time()
                    self._pending_final_order = event.get("output_order")
                self._pending_final_chunks.append(content)
            return
        if evt_type == "chat.reasoning":
            content = str(event.get("content") or "")
            if content:
                if self._reasoning_started_at_ms is None:
                    self._reasoning_started_at_ms = time.time() * 1000
                    self._reasoning_order = event.get("output_order")
                self._reasoning_updated_at_ms = time.time() * 1000
                self._pending_reasoning_chunks.append(content)
            return
        if evt_type in _UNATTENDED_BOUNDARY_EVENT_TYPES:
            await self.flush_pending_final()
            await self._record_event(event, evt_type)
            return
        if evt_type == "chat.final":
            # The event dict is shared with the broadcast path (and possibly
            # other observers), so final_mode is added to the record's extra
            # instead of mutating the payload in place.
            content = str(event.get("content") or "")
            if content:
                # A non-empty final carries the whole segment text; the
                # accumulated deltas are the same text and are discarded,
                # mirroring the durable pipeline's non-empty final branch.
                segment_started_at = self._pending_final_started_at
                self._reset_pending_final()
                await self._record_event(
                    event, evt_type, segment_started_at=segment_started_at
                )
            else:
                # An empty final is only a segment boundary: the bubble text
                # is whatever the deltas accumulated, so flush it as one
                # record (the empty final itself is filtered as a shell).
                await self.flush_pending_final()
            return
        await self._record_event(event, evt_type)

    async def handover(self, event: dict[str, Any]) -> None:
        """React to a delivered event after a waiter took over the stream.

        The waiter's own durable pipeline persists what it receives, so the
        handover must never duplicate a segment it will write itself: a
        non-empty final carries the whole segment (discard our deltas), while
        boundary events flush the pre-attach text that the consumer cannot
        see (its accumulation starts after the attach).
        """
        evt_type = str(event.get("event_type") or "")
        content = str(event.get("content") or "").strip()
        if evt_type == "chat.final" and content:
            self._reset_pending_final()
            # The consumer attaches only its own (post-attach) reasoning to
            # the final; keep the pre-attach reasoning as a standalone record.
            await self.flush_pending_reasoning()
            return
        if evt_type in _UNATTENDED_BOUNDARY_EVENT_TYPES or evt_type == "chat.final":
            await self.flush_pending_final()
            return
        # Other events leave the buffers untouched: the next boundary, a
        # non-empty final, or the stream end resolves them.

    async def flush_pending_final(self) -> None:
        """Flush accumulated delta text as one chat.final record."""
        pending_text = "".join(self._pending_final_chunks)
        segment_started_at = self._pending_final_started_at
        segment_order = self._pending_final_order
        self._reset_pending_final()
        if not pending_text or pending_text == self._durable_final_content:
            return
        extra_fields: dict[str, Any] = {}
        if segment_order is not None:
            extra_fields["output_order"] = segment_order
        extra_fields = self._attach_reasoning_content(extra_fields)
        extra_fields = with_cross_session_history_metadata(
            extra_fields, self._cross_session_marker
        ) or {}
        record_timestamp = resolve_final_record_timestamp(
            event_type="chat.final",
            segment_started_at=segment_started_at,
            extra_fields=extra_fields,
        )
        await run_history_io(
            append_history_record,
            session_id=self._session_id,
            request_id=self._request_id,
            channel_id=_resolve_channel_id(self._channel_id),
            role="assistant",
            event_type="chat.final",
            content=pending_text,
            timestamp=record_timestamp,
            extra=extra_fields if extra_fields else None,
            mode=self._mode,
        )
        self._durable_final_content = pending_text

    async def flush_pending_reasoning(self) -> None:
        """Persist trailing reasoning as one standalone chat.reasoning record."""
        if not self._pending_reasoning_chunks:
            return
        reasoning_content = "".join(self._pending_reasoning_chunks)
        started_at_ms = self._reasoning_started_at_ms
        order = self._reasoning_order
        updated_at_ms = self._reasoning_updated_at_ms
        self._reset_pending_reasoning()
        if not reasoning_content.strip():
            return
        extra_fields: dict[str, Any] = {"reasoning_content": reasoning_content}
        if order is not None:
            extra_fields["reasoning_output_order"] = order
        if updated_at_ms is not None:
            extra_fields["reasoning_updated_at"] = updated_at_ms
        extra_fields = with_cross_session_history_metadata(
            extra_fields, self._cross_session_marker
        ) or {}
        timestamp = (started_at_ms or time.time() * 1000) / 1000
        await run_history_io(
            append_history_record,
            session_id=self._session_id,
            request_id=self._request_id,
            channel_id=_resolve_channel_id(self._channel_id),
            role="assistant",
            event_type="chat.reasoning",
            content="",
            timestamp=timestamp,
            extra=extra_fields if extra_fields else None,
            mode=self._mode,
        )

    async def _record_event(
        self,
        event: dict[str, Any],
        evt_type: str,
        *,
        segment_started_at: float | None = None,
    ) -> None:
        """Persist one event as an assistant history record."""
        extra_fields = {
            key: value
            for key, value in event.items()
            if key not in ("event_type", "content")
        }
        if evt_type == "team.message":
            inner_event = event.get("event")
            if isinstance(inner_event, dict):
                for key, value in inner_event.items():
                    if key not in ("type", "timestamp", "content"):
                        extra_fields[key] = value
        if evt_type == "chat.final":
            extra_fields.setdefault("final_mode", FINAL_MODE_PATCH_SEGMENT)
        if evt_type in ("chat.final", "chat.tool_call"):
            extra_fields = self._attach_reasoning_content(extra_fields) or {}
        extra_fields = with_cross_session_history_metadata(
            extra_fields, self._cross_session_marker
        ) or {}
        record_timestamp = resolve_final_record_timestamp(
            event_type=evt_type,
            segment_started_at=(
                segment_started_at if evt_type == "chat.final" else None
            ),
            extra_fields=extra_fields,
        )
        await run_history_io(
            append_history_record,
            session_id=self._session_id,
            request_id=self._request_id,
            channel_id=_resolve_channel_id(self._channel_id),
            role="assistant",
            event_type=evt_type,
            content=event.get("content") or event.get("error") or "",
            timestamp=record_timestamp,
            extra=extra_fields if extra_fields else None,
            mode=self._mode,
        )
        if evt_type == "chat.final":
            self._durable_final_content = str(event.get("content") or "")

    def _attach_reasoning_content(
        self, extra_fields: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Merge accumulated reasoning into a record's extra fields."""
        if not self._pending_reasoning_chunks:
            return extra_fields
        reasoning_content = "".join(self._pending_reasoning_chunks)
        order = self._reasoning_order
        updated_at_ms = self._reasoning_updated_at_ms
        self._reset_pending_reasoning()
        if not reasoning_content.strip():
            return extra_fields
        result = dict(extra_fields or {})
        result["reasoning_content"] = reasoning_content
        if order is not None:
            result["reasoning_output_order"] = order
        if updated_at_ms is not None:
            result["reasoning_updated_at"] = updated_at_ms
        return result

    def _reset_pending_final(self) -> None:
        self._pending_final_chunks.clear()
        self._pending_final_started_at = None
        self._pending_final_order = None

    def _reset_pending_reasoning(self) -> None:
        self._pending_reasoning_chunks.clear()
        self._reasoning_started_at_ms = None
        self._reasoning_updated_at_ms = None
        self._reasoning_order = None


def _persist_team_history_event(
    channel_id: str | None,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """Persist team monitor events required by team.history.get panel restore."""
    evt_type = event.get("event_type")
    if evt_type not in {"team.member", "team.task"}:
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
            "team.member.registered",
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
    """Push status and approval events for signal-triggered evolution awaiting approval."""
    if not _team_evolution_host_events_enabled(rail):
        return

    from jiuwenswarm.runtime.host_services import RuntimeHostPushTransport

    push_context = EvolutionPushContext(
        transport=RuntimeHostPushTransport(),
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
            if not _team_evolution_host_events_enabled(rail):
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
