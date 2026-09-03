# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team lifecycle manager."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.agent.team_agent import TeamAgent
from openjiuwen.agent_teams.paths import team_home
from openjiuwen.agent_teams.runtime.pool import RuntimeState
from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.core.runner import Runner
from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.common.logging import server_logger
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.rails import (
    SkillEvolutionRail,
    SkillUseRail,
    TeamSkillCreateRail,
    TeamSkillEvolutionRail,
)
from jiuwenswarm.agents.harness.team.bootstrap import configure_agent_teams_home
from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.common.utils import get_user_workspace_dir

configure_agent_teams_home()

from jiuwenswarm.agents.harness.team.config_loader import (
    load_team_spec_dict,
)
from jiuwenswarm.agents.harness.team.distributed_runtime import (
    ensure_postgresql_for_leader,
    extract_pg_endpoint,
    fallback_distributed_to_local,
    is_distributed_mode,
    missing_distributed_dependencies,
    is_pg_available,
    is_postgresql_storage,
    normalize_distributed_transport_fields,
    parse_port,
    run_command,
    runtime_member_name,
    runtime_role,
    try_start_pg_cluster,
)
from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenswarm.agents.harness.team import kv_cache_hooks
from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
    notify_remote_members_trace_context_update,
    release_a2x_reservations_for_session,
)
from jiuwenswarm.agents.harness.team.team_skill_links import sync_skill_dir_links
from jiuwenswarm.common.config import (
    get_config,
    get_default_models,
    get_evolution_auto_scan_enabled,
    get_evolution_review_trigger_enabled,
    get_evolution_signal_trigger_enabled,
    get_skill_create_enabled,
)
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
    MemberInfo,
    RuntimeInfo,
    TeamWorkspaceInfo,
    build_member_rails,
)
from jiuwenswarm.common.utils import get_agent_skills_dir
from jiuwenswarm.common.invocation_context import (
    TRACE_CONTEXT_METADATA_KEY,
    TRACE_HEADER_EXPORTER_METADATA_KEY,
    trace_context_from_dict,
)
from jiuwenswarm.common.invocation_context.model_trace import export_trace_headers_for_name
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

logger = logging.getLogger(__name__)

# Wall-clock cap for a single external command (pg_isready, systemctl, etc.).
_SUBPROCESS_TIMEOUT_SEC = 120.0
# After pg_ctlcluster/systemd reports start, the server may still be initializing.
_PG_POST_START_READY_MAX_SEC = 30.0
_PG_POST_START_READY_INIT_SLEEP = 0.4
_PG_POST_START_READY_MAX_SLEEP = 2.0
_PG_POST_START_READY_BACKOFF = 1.45
_PG_POST_START_LOG_EVERY_SEC = 5.0
_TEAM_STREAM_EXIT_GRACE_TIMEOUT_SEC = 1.5
# Bound each frontend waiter's event backlog.  Producers await a free slot, so
# a slow or disconnected TUI consumer cannot make the process retain every
# event emitted by a long-running team session.
TEAM_EVENT_QUEUE_MAXSIZE = 64
_WAITER_PUT_RECHECK_TIMEOUT_SEC = 0.1


def _safe_payload_preview(payload: Any) -> str:
    """Render an interact payload as a bounded single-line log fragment.

    Args:
        payload: Raw interact payload, either plain text or a team message
            object (``GodViewMessage`` / ``HumanAgentMessage`` / ...).

    Returns:
        Clipped text of the payload body, prefixed with the payload type when
        it is not a plain string.
    """
    if isinstance(payload, str):
        return preview_text(payload)
    body = getattr(payload, "body", None)
    text = body if isinstance(body, str) else str(payload)
    return f"<{type(payload).__name__}>{preview_text(text)}"

# ── Team Observability ──────────────────────────────────────
# Tracks whether observability is currently active so we can
# detect config toggles (enabled → disabled or vice-versa)
# and init / shutdown accordingly on each team request.
_observability_active: bool = False


def sync_team_observability() -> None:
    """Synchronize observability state with current config.

    Called before each ``Runner.run_agent_team_streaming`` so that
    hot-reloading the ``team_observability.enabled`` flag takes
    effect immediately:

    * disabled → enabled : ``init_observability()``
    * enabled → disabled : ``shutdown_observability()``
    * unchanged          : no-op
    """
    global _observability_active
    cfg = get_config().get("team_observability", {}) or {}
    want_enabled = bool(cfg.get("enabled", False))

    if want_enabled and not _observability_active:
        try:
            from openjiuwen.agent_teams.observability import (
                ObservabilityConfig,
                init_observability,
                is_initialized,
            )
            if is_initialized():
                _observability_active = True
                return
            obs_cfg = ObservabilityConfig(
                enabled=True,
                service_name=cfg.get("service_name", "jiuwenswarm"),
                exporter=cfg.get("exporter", "otlp_grpc"),
                endpoint=cfg.get("endpoint", "http://localhost:4317"),
                sample_rate=cfg.get("sample_rate", 1.0),
                attribute_value_max_length=cfg.get("attribute_value_max_length", 10240),
                redact_prompts=cfg.get("redact_prompts", False),
                redact_completions=cfg.get("redact_completions", False),
                langfuse_public_key=cfg.get("langfuse_public_key", ""),
                langfuse_secret_key=cfg.get("langfuse_secret_key", ""),
                traces_dir=cfg.get("traces_dir") or str(get_user_workspace_dir() / ".trace"),
                file_retention_days=cfg.get("file_retention_days", 7),
            )
            init_observability(obs_cfg)
            _observability_active = True
            if obs_cfg.exporter == "file":
                logger.info(
                    "[TeamObservability] enabled: exporter=%s traces_dir=%s",
                    obs_cfg.exporter, obs_cfg.traces_dir,
                )
            else:
                logger.info(
                    "[TeamObservability] enabled: exporter=%s endpoint=%s",
                    obs_cfg.exporter, obs_cfg.endpoint,
                )
        except Exception as exc:
            logger.warning("[TeamObservability] init failed: %s", exc)

    elif not want_enabled and _observability_active:
        shutdown_team_observability()


def shutdown_team_observability() -> None:
    """Shutdown team observability (called on disable or process exit)."""
    global _observability_active
    if not _observability_active:
        return
    try:
        from openjiuwen.agent_teams.observability import shutdown_observability
        shutdown_observability()
        _observability_active = False
        logger.info("[TeamObservability] disabled")
    except Exception as exc:
        logger.warning("[TeamObservability] shutdown failed: %s", exc)


@dataclass
class TeamRailMountContext:
    """Context needed to rebuild team rails after a hot config toggle."""

    agent: Any
    member_info: MemberInfo
    runtime: RuntimeInfo
    team_workspace: TeamWorkspaceInfo


async def _stop_team_messager(team_agent: Any, *, session_id: str) -> None:
    """Stop a team's mailbox transport so per-team ZMQ sockets release their ports."""
    infra = getattr(team_agent, "infra", None)
    messager = getattr(infra, "messager", None) if infra is not None else None
    stop = getattr(messager, "stop", None)
    if not callable(stop):
        return
    try:
        await stop()
        logger.info("[TeamManager] team messager stopped: session_id=%s", session_id)
    except Exception as exc:
        logger.warning("[TeamManager] team messager stop failed: session_id=%s error=%s", session_id, exc)


def _collect_team_package_skill_dirs(agent_group_name: str) -> list[Path]:
    """收集专家团包挂载到 member 的技能目录（去重保序），供退团时 member 级清理。

    团包 skills 以绝对路径 SkillSpec 去重合并进各成员模板（共享 + 成员私有，
    见 ``load_agent_group_package``），此处复用同一权威加载器还原"当初挂了
    哪些目录"，与单专家 ``_collect_expert_skill_dirs`` 同思路。包缓存缺失
    （如 LocalDir 源不落缓存）或解析失败时返回空列表——member 实例随 team
    停止销毁、skills 随之 GC，跳过 purge 不造成功能问题。
    """
    from jiuwenswarm.server.runtime.expert.agent_group import load_agent_group_package
    from jiuwenswarm.server.runtime.expert.expert_store import get_cached_expert_package_dir

    package_dir = get_cached_expert_package_dir(agent_group_name)
    if package_dir is None:
        return []
    try:
        templates = load_agent_group_package(package_dir)
    except Exception as exc:
        logger.warning(
            "[TeamManager] 解析专家团包技能目录失败（跳过 member skills purge）: "
            "group=%s error=%s",
            agent_group_name,
            exc,
        )
        return []
    dirs: list[Path] = []
    seen: set[str] = set()
    for template in templates.values():
        for skill in getattr(template, "skills", None) or []:
            directory = getattr(skill, "dir", None)
            if not directory:
                continue
            resolved = Path(str(directory)).expanduser().resolve()
            key = str(resolved)
            if key not in seen:
                seen.add(key)
                dirs.append(resolved)
    return dirs


def _iter_local_member_runtimes(team_agent: Any) -> list[Any]:
    """枚举 leader TeamAgent 下本进程可触达的 member runtime（leader 自身 + in-process teammate）。

    subprocess/distributed teammate 的 skills 在其自有进程内，随进程停止消亡，
    不在本进程清理范围；runtime 经 duck-typing 取用，缺任一节略过。
    """
    runtimes: list[Any] = []
    leader_runtime = getattr(team_agent, "harness", None)
    if leader_runtime is not None:
        runtimes.append(leader_runtime)
    spawn_manager = getattr(team_agent, "spawn_manager", None)
    handles = getattr(spawn_manager, "spawned_handles", None) or {}
    for handle in list(handles.values()):
        member_agent = getattr(handle, "agent_ref", None)
        runtime = getattr(member_agent, "harness", None) if member_agent is not None else None
        if runtime is not None and runtime not in runtimes:
            runtimes.append(runtime)
    return runtimes


async def _purge_runtime_skill_mounts(runtime: Any, skill_dirs: list[Path]) -> None:
    """清一个 member runtime 的 SkillUseRail 上残留的团包 skills 挂载（幂等）。

    member 级 ``_purge_expert_skill_mounts``：NativeHarness.stop/dispose 只
    teardown per-agent tools、移除 sys_operation，不调 ``unload_extension``，
    团包快照热挂的 skills 由此显式释放。清理语义与 agent-core 当前 ``_unbind``
    SKILL 分支对齐（删 mount_root + 清 config.skills + 空 enabled 置 None +
    重扫）；runtime 不带 find_rails（非 DeepAgent 系 runtime）时跳过。
    """
    finder = getattr(runtime, "find_rails", None)
    if not callable(finder):
        return
    rails = finder(SkillUseRail)
    if not rails:
        return
    config = getattr(runtime, "deep_config", None)
    for leaf in skill_dirs:
        if not leaf.exists():
            continue
        is_leaf = (leaf / "SKILL.md").is_file()
        mount_root = str(leaf.parent) if is_leaf else str(leaf)
        skill_name = leaf.name if is_leaf else ""
        for rail in rails:
            current = list(getattr(rail, "skills_dir", None) or [])
            mounted = {str(Path(item).expanduser().resolve()) for item in current}
            if mount_root not in mounted:
                continue
            rail.skills_dir = [
                item
                for item in current
                if str(Path(item).expanduser().resolve()) != mount_root
            ]
            if skill_name and getattr(rail, "enabled_skills", None) is not None:
                rail.enabled_skills.discard(skill_name)
                if not rail.enabled_skills:
                    # 空 set 在 agent-core _filter_skills 里 = 不过滤，
                    # 必须置 None 才表示「无 enabled 限制」
                    rail.enabled_skills = None
            rail.enable_cache = False
            rail.clear_skills()
            if rail.skills_dir:
                try:
                    await rail.reload_skills()
                except Exception as exc:
                    logger.warning("[TeamManager] member skill purge reload failed: %s", exc)
            break
        # config.skills 同步摘除（与 agent-core _unbind SKILL 分支同规）
        raw_config_skills = getattr(config, "skills", None)
        if raw_config_skills:
            values = [raw_config_skills] if isinstance(raw_config_skills, str) else list(raw_config_skills)
            config.skills = [
                item
                for item in values
                if str(Path(str(item)).expanduser().resolve()) != mount_root
            ] or None


def _runner_team_runtime_manager(runner: Any) -> Any:
    """Return Runner's team runtime manager without calling its protected method."""
    attr_name = "_team_runtime_manager"
    manager = vars(runner).get(attr_name)
    if manager is None:
        from openjiuwen.agent_teams.runtime import TeamRuntimeManager

        manager = TeamRuntimeManager()
        setattr(runner, attr_name, manager)
    return manager


class TeamManager:
    """Manage team instances across sessions."""

    def __init__(self):
        # These TeamAgent objects are auxiliary runtimes used only by the
        # distributed teammate bootstrap path. Local leader execution is owned
        # by Runner's TeamRuntimePool instead.
        self._team_agents: dict[str, TeamAgent] = {}
        self._runner_team_agents: dict[str, TeamAgent] = {}
        self._team_monitors: dict[str, TeamMonitorHandler] = {}
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._bootstrap_lock = asyncio.Lock()
        self._distributed_switch_lock = asyncio.Lock()
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # 当 cancel 请求到达时设置，通知正在执行的 pause 操作中止自身并让 cancel 执行
        self._cancel_requested: dict[str, bool] = {}
        # 追踪当前正在执行的 pause 任务，供 cancel 抢占取消
        self._active_pause_tasks: dict[str, asyncio.Task] = {}
        self._active_team_names: dict[str, str] = {}
        self._pending_team_names: dict[str, str] = {}
        # session_id → list of (request_id, asyncio.Queue) waiters
        self._pending_waiters: dict[str, list[tuple[str, asyncio.Queue]]] = {}
        # session_id → cron team round completion state. Lifetime-coupled to
        # _pending_waiters: set by _try_finish_cron_team_stream, popped by the
        # finisher coroutines once the cron stream ends.
        self._cron_team_completion: dict[str, dict[str, Any]] = {}
        # Sessions that have successfully initialized a team runtime at least once.
        # Persists across stream lifecycles so follow-up requests are not
        # misidentified as first requests.
        self._initialized_sessions: set[str] = set()
        # session_id → TeamSkillEvolutionRail instance (set by customizer, used for drain/approval)
        self._team_skill_rails: dict[str, Any] = {}
        # session_id → member SkillEvolutionRail instances
        self._team_member_skill_evolution_rails: dict[str, list[Any]] = {}
        # session_id → TeamSkillCreateRail instance
        self._team_skill_create_rails: dict[str, Any] = {}
        # session_id → context used to rebuild team rails on config enable
        self._team_rail_contexts: dict[str, TeamRailMountContext] = {}
        # session_id → live rails and owning DeepAgent, for hot-unregister
        self._team_live_rails: dict[str, list[tuple[Any, Any]]] = {}
        # session_id → evolution watcher task
        self._team_evolution_watchers: dict[str, asyncio.Task] = {}
        # session_id → runtime_ready requested a watcher before the rail registered
        self._pending_team_evolution_watcher_sessions: set[str] = set()
        # session_id -> team workspace skills directory used as the shared link view.
        self._team_shared_skill_link_targets: dict[str, Path] = {}
        # session_id → workflow handler instance
        self._workflow_handlers: dict[str, Any] = {}
        # session_id → True once a team-building event (team.member,
        # team.task, workflow.updated) has been broadcast in the current
        # round.  Reset when a new round starts.
        self._seen_team_events: dict[str, bool] = {}
        # session_id → True after workflow.updated(status=completed/…)
        # is received.  When True, chat.final is no longer suppressed
        # even if seen_team_events is True.
        self._workflow_completed: dict[str, bool] = {}

    def has_stream_task(self, session_id: str) -> bool:
        return session_id in self._stream_tasks

    def pop_stream_task(self, session_id: str) -> asyncio.Task | None:
        return self._stream_tasks.pop(session_id, None)

    def is_session_initialized(self, session_id: str) -> bool:
        """Return whether the session has ever initialized a team runtime."""
        return session_id in self._initialized_sessions

    def clear_session_initialized(self, session_id: str) -> None:
        """Clear the initialized marker for a session (e.g. on stream end)."""
        self._initialized_sessions.discard(session_id)

    def has_waiters(self, session_id: str) -> bool:
        """Return whether there are pending waiters for the given session."""
        return bool(self._pending_waiters.get(session_id))

    def add_waiter(self, session_id: str, request_id: str, queue: asyncio.Queue) -> None:
        """Register a waiter queue for a session's event stream."""
        self._pending_waiters.setdefault(session_id, []).append((request_id, queue))

    def remove_waiter(self, session_id: str, request_id: str) -> None:
        """Remove a waiter by request_id; clean up empty lists."""
        waiters = self._pending_waiters.get(session_id)
        if waiters is None:
            return
        remaining = [(rid, q) for rid, q in waiters if rid != request_id]
        if remaining:
            self._pending_waiters[session_id] = remaining
        else:
            self._pending_waiters.pop(session_id, None)

    async def broadcast_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Broadcast an event with backpressure to every active waiter.

        The short timed wait is only used while a queue is full.  It lets a
        producer notice that ``remove_waiter`` detached a disconnected client
        instead of remaining blocked forever on that orphaned queue.
        """
        waiters = list(self._pending_waiters.get(session_id, ()))

        async def _put_to_waiter(
            request_id: str,
            queue: asyncio.Queue,
        ) -> None:
            queued_event = dict(event)
            while any(
                rid == request_id and registered_queue is queue
                for rid, registered_queue in self._pending_waiters.get(session_id, ())
            ):
                try:
                    await asyncio.wait_for(
                        queue.put(queued_event),
                        timeout=_WAITER_PUT_RECHECK_TIMEOUT_SEC,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    logger.debug(
                        "[TeamManager] broadcast failed: session_id=%s request_id=%s",
                        session_id,
                        request_id,
                        exc_info=True,
                    )
                    break

        await asyncio.gather(*(
            _put_to_waiter(request_id, queue)
            for request_id, queue in waiters
        ))

    # --- seen_team_events tracking ---
    # A session enters "team" mode once any team-building event (team.member,
    # team.task, workflow.updated, team.runtime_ready) is broadcast.  While
    # the flag is set, chat.final must NOT be forwarded to the frontend
    # because the team may still be running; only team.completed (via
    # chat.processing_status is_complete=True) should finalize the round.

    def mark_seen_team_events(self, session_id: str) -> None:
        """Record that a team-building event has been broadcast for this session."""
        self._seen_team_events[session_id] = True

    def has_seen_team_events(self, session_id: str) -> bool:
        """Return whether any team-building event has been broadcast in this round."""
        return self._seen_team_events.get(session_id, False)

    def reset_seen_team_events(self, session_id: str) -> None:
        """Reset the flag at the start of a new conversation round."""
        self._seen_team_events.pop(session_id, None)

    def mark_workflow_completed(self, session_id: str) -> None:
        """Mark that the workflow has reached a terminal status."""
        self._workflow_completed[session_id] = True

    def is_workflow_completed(self, session_id: str) -> bool:
        return self._workflow_completed.get(session_id, False)

    def reset_workflow_completed(self, session_id: str) -> None:
        self._workflow_completed.pop(session_id, None)

    def get_waiters(self, session_id: str) -> list[tuple[str, asyncio.Queue]]:
        """Return the (request_id, queue) pairs waiting on the given session."""
        return self._pending_waiters.get(session_id, [])

    def get_cron_completion(self, session_id: str) -> dict[str, Any] | None:
        """Return the cron team round completion state for the session, if any."""
        return self._cron_team_completion.get(session_id)

    def setdefault_cron_completion(
        self, session_id: str, default: dict[str, Any]
    ) -> dict[str, Any]:
        """Get or create the cron team round completion state for the session."""
        return self._cron_team_completion.setdefault(session_id, default)

    def pop_cron_completion(self, session_id: str) -> dict[str, Any] | None:
        """Drop the cron team round completion state for the session."""
        return self._cron_team_completion.pop(session_id, None)

    def is_runtime_active(self, session_id: str) -> bool:
        """Return whether a Runner-owned runtime is active for the session."""
        return session_id in self._active_team_names

    def is_runtime_pending(self, session_id: str) -> bool:
        """Return whether runtime activation is pending for the session."""
        return session_id in self._pending_team_names

    def get_active_team_name(self, session_id: str) -> str | None:
        """Return the active Runner-owned team name for the session."""
        return self._active_team_names.get(session_id)

    def get_runtime_team_snapshot(self) -> dict[str, dict[str, str]]:
        """Return session -> team runtime status for active/pending team runs."""
        snapshot: dict[str, dict[str, str]] = {}
        for sid, team_name in self._active_team_names.items():
            snapshot[sid] = {"team_name": team_name, "state": "active"}
        for sid, team_name in self._pending_team_names.items():
            snapshot.setdefault(sid, {"team_name": team_name, "state": "pending"})
        return snapshot

    def _get_lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        """Return the lock that serializes lifecycle operations for a session."""
        if self._is_distributed_mode(get_config()):
            return self._bootstrap_lock

        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def get_monitor(self, session_id: str) -> TeamMonitorHandler | None:
        return self._team_monitors.get(session_id)

    def get_team_evolution_watcher(self, session_id: str) -> asyncio.Task | None:
        return self._team_evolution_watchers.get(session_id)

    def register_team_evolution_watcher(self, session_id: str, task: asyncio.Task) -> None:
        self._team_evolution_watchers[session_id] = task

    def pop_team_evolution_watcher(self, session_id: str) -> asyncio.Task | None:
        return self._team_evolution_watchers.pop(session_id, None)

    def mark_team_evolution_watcher_deferred(self, session_id: str) -> None:
        self._pending_team_evolution_watcher_sessions.add(session_id)

    def consume_team_evolution_watcher_deferred(self, session_id: str) -> bool:
        if session_id not in self._pending_team_evolution_watcher_sessions:
            return False
        self._pending_team_evolution_watcher_sessions.discard(session_id)
        return True

    @staticmethod
    def _is_distributed_mode(config_base: dict[str, Any]) -> bool:
        return is_distributed_mode(config_base)

    @staticmethod
    def _runtime_role(config_base: dict[str, Any]) -> str:
        return runtime_role(config_base)

    @staticmethod
    def _runtime_member_name(config_base: dict[str, Any], team_cfg: dict[str, Any]) -> str | None:
        return runtime_member_name(config_base, team_cfg)

    @staticmethod
    def _normalize_distributed_transport_fields(
        config_base: dict[str, Any],
        team_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        return normalize_distributed_transport_fields(config_base, team_cfg)

    @staticmethod
    def normalize_distributed_transport_fields(
        config_base: dict[str, Any],
        team_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        """Public wrapper for distributed transport normalization."""
        return TeamManager._normalize_distributed_transport_fields(config_base, team_cfg)

    @staticmethod
    def _parse_port(value: Any, default: int, field_name: str) -> int:
        return parse_port(value, default, field_name)

    @staticmethod
    def parse_port(value: Any, default: int, field_name: str) -> int:
        """Public wrapper for validated port parsing."""
        return TeamManager._parse_port(value, default, field_name)

    @staticmethod
    def _normalize_team_identity_fields(team_cfg: dict[str, Any]) -> dict[str, Any]:
        normalized_cfg = copy.deepcopy(team_cfg)
        leader_cfg = normalized_cfg.get("leader", {})
        if isinstance(leader_cfg, dict):
            display_name = str(leader_cfg.get("display_name", "")).strip()
            name = str(leader_cfg.get("name", "")).strip()
            if display_name and not name:
                leader_cfg["name"] = display_name
            elif name and not display_name:
                leader_cfg["display_name"] = name

        members = normalized_cfg.get("predefined_members", [])
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, dict):
                    continue
                display_name = str(member.get("display_name", "")).strip()
                name = str(member.get("name", "")).strip()
                if display_name and not name:
                    member["name"] = display_name
                elif name and not display_name:
                    member["display_name"] = name
        return normalized_cfg

    @staticmethod
    def build_session_scoped_team_name(team_name: str, session_id: str) -> str:
        """构造 session 作用域的 team_name（对外公开 API）。

        供网关等外部模块做 team/session 一致性校验时复用，避免直接访问受保护成员。
        """
        base_name = str(team_name or "").strip() or "team"
        session_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "").strip())
        session_suffix = session_suffix.strip("._-")
        if not session_suffix:
            return base_name
        if base_name.endswith(f"_{session_suffix}"):
            return base_name
        return f"{base_name}_{session_suffix}"

    @staticmethod
    def _apply_session_scoped_team_name(
        spec: TeamAgentSpec,
        *,
        session_id: str,
    ) -> None:
        spec.team_name = TeamManager.build_session_scoped_team_name(
            spec.team_name,
            session_id,
        )

    @staticmethod
    def _load_team_spec(
        session_id: str,
        *,
        requested_model_name: str | None = None,
        template_id: str | None = None,
        template_snapshot: dict[str, Any] | None = None,
        strict_template: bool = False,
    ) -> TeamAgentSpec:
        config_base = get_config()
        # Keep dependency checks scoped to distributed mode to make the
        # control flow explicit at the call site (local mode bypasses checks).
        if TeamManager._is_distributed_mode(config_base):
            missing = missing_distributed_dependencies(config_base)
            if missing:
                missing_list = ", ".join(missing)
                logger.warning(
                    "[TeamManager][MISSING_DISTRIBUTE_DEPS] missing=%s",
                    missing_list,
                )
                logger.error(
                    "[TeamManager][FALLBACK_TO_LOCAL] "
                    "distributed runtime is not available; downgraded to local mode "
                    "for current process"
                )
                logger.warning(
                    "[TeamManager][ACTION] install via: "
                    "pip install -e \".[distribute]\" or uv sync --extra distribute"
                )
                config_base = fallback_distributed_to_local(config_base)

        spec_dict = load_team_spec_dict(
            config_base=config_base,
            requested_model_name=requested_model_name,
            template_id=template_id,
            template_snapshot=template_snapshot,
            strict_template=strict_template,
        )
        spec_dict = TeamManager._normalize_team_identity_fields(spec_dict)
        if TeamManager._is_distributed_mode(config_base):
            spec_dict = TeamManager._normalize_distributed_transport_fields(config_base, spec_dict)

        # When models.defaults has more than one entry, populate model_pool
        # and set model_pool_strategy to by_model_name so team members
        # can be assigned different model endpoints from the pool.
        default_models = get_default_models(config_base)
        if len(default_models) > 1:
            from openjiuwen.agent_teams.schema.team import ModelPoolEntry

            pool_entries: list[dict] = []
            for entry in default_models:
                mcc = entry.get("model_client_config") or {}
                mco = entry.get("model_config_obj") or {}
                if not mcc.get("model_name"):
                    continue
                # Map the internal ``reasoning_level`` hint to provider-specific
                # params here; leaving it raw would let ``ModelRequestConfig``
                # (extra=allow) forward it to ``AsyncCompletions.create()`` and
                # raise ``unexpected keyword argument 'reasoning_level'``.
                request_config = build_reasoning_model_request_kwargs(
                    model_client_config=mcc,
                    model_config_obj=mco,
                    model_name=mcc["model_name"],
                )
                request_config.pop("model", None)
                pool_entry = ModelPoolEntry(
                    model_name=mcc["model_name"],
                    api_key=mcc.get("api_key", ""),
                    api_base_url=mcc.get("api_base", ""),
                    api_provider=mcc.get("client_provider", ""),
                    metadata={
                        "client": {
                            k: v for k, v in mcc.items()
                            if k not in ("model_name", "api_key", "api_base", "client_provider") and v is not None
                        },
                        "request": request_config,
                    },
                )
                pool_entries.append(pool_entry.model_dump())

            if pool_entries:
                spec_dict["model_pool"] = pool_entries
                spec_dict["model_pool_strategy"] = "by_model_name"

        return TeamAgentSpec.model_validate(spec_dict)

    @staticmethod
    def _lookup_bound_team_identity(session_id: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
        metadata = get_session_metadata(session_id, cache_bust=True)
        team_name = str(metadata.get("team_name") or "").strip()
        template_id = str(metadata.get("team_template_id") or "").strip()
        template_snapshot: dict[str, Any] | None = None
        if team_name:
            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
            from jiuwenswarm.server.runtime.team_entity_store import ensure_team_entity, ensure_team_entity_for_binding

            binding = get_team_binding_store().get(team_name)
            if binding is not None:
                if not template_id:
                    template_id = binding.template_id
                entity = ensure_team_entity_for_binding(binding, config_base=get_config())
            else:
                legacy_snapshot = (
                    copy.deepcopy(metadata.get("team_template_snapshot"))
                    if isinstance(metadata.get("team_template_snapshot"), dict)
                    else None
                )
                entity = ensure_team_entity(
                    team_name=team_name,
                    template_id=template_id,
                    template_snapshot=legacy_snapshot,
                    config_base=get_config(),
                )
            if entity is not None:
                template_id = entity.template_id
                template_snapshot = copy.deepcopy(entity.template_snapshot)
        return team_name or None, template_id or None, template_snapshot

    def _load_session_team_spec(
        self,
        session_id: str,
        *,
        requested_model_name: str | None = None,
    ) -> tuple[TeamAgentSpec, bool]:
        team_name, template_id, template_snapshot = self._lookup_bound_team_identity(session_id)
        load_kwargs: dict[str, Any] = {}
        if requested_model_name is not None:
            load_kwargs["requested_model_name"] = requested_model_name
        if template_id is not None:
            load_kwargs["template_id"] = template_id
            load_kwargs["strict_template"] = template_snapshot is None
        if template_snapshot is not None:
            load_kwargs["template_snapshot"] = template_snapshot
        spec = self._load_team_spec(session_id, **load_kwargs)
        if team_name:
            spec.team_name = team_name
            return spec, True
        return spec, False


    @staticmethod
    def _resolve_session_agent_group(session_id: str) -> str | None:
        """本会话绑定的专家团包名（expert_id），未绑定返回 None。

        判定只读 session metadata（expert_type=="team" 且 expert_id 非空），
        零磁盘零网络假设——包缓存缺失等异常由下游组装路径显式报错。
        """
        metadata = get_session_metadata(session_id, cache_bust=True)
        if str(metadata.get("expert_type") or "agent") != "team":
            return None
        expert_id = str(metadata.get("expert_id") or "").strip()
        return expert_id or None

    async def get_swarm_enriched_team_spec(
        self,
        session_id: str,
        *,
        mode: str,
        project_dir: str | None = None,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
        requested_model_name: str | None = None,
    ) -> TeamAgentSpec:
        """Build a team spec via provider-based assembly (no parent DeepAgent).

        Sources every member capability from the shared config source through
        ``enrich_team_spec_for_swarm`` instead of inheriting from a pre-built
        single agent, so creating a team never requires constructing one first.

        Args:
            session_id: Active session id.
            mode: Request mode (e.g. "team").
            project_dir: Resolved project directory, if any.
            request_id: Originating request id, if any.
            channel_id: Raw channel id from the request, if any.
            request_metadata: Request metadata mapping.

        Returns:
            The enriched ``TeamAgentSpec`` ready to build (``build_context`` set;
            assembly is fully declarative, no imperative post-processing).
        """
        from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm

        config_base = get_config()
        await self._ensure_postgresql_for_leader(config_base)
        spec, has_binding = self._load_session_team_spec(
            session_id,
            requested_model_name=requested_model_name,
        )
        if not has_binding:
            self._apply_session_scoped_team_name(spec, session_id=session_id)
        self.apply_team_plan_mode(spec, request_metadata=request_metadata)
        agent_group_name = self._resolve_session_agent_group(session_id)
        agent_group_package_dir = None
        if agent_group_name:
            # 装配点 _apply_agent_group 是同步函数不能自持 fetch，
            # 在此 async 边界预取（缓存优先 + miss 回退 fetch，与单专家
            # _apply_expert 的 package_dir=None 兜底语义对齐）——否则本地调试
            # 源（LocalDir 不落缓存）下装配抛 FileNotFoundError 。
            from jiuwenswarm.server.runtime.expert.expert_store import (
                resolve_expert_package_dir,
            )

            agent_group_package_dir = await resolve_expert_package_dir(agent_group_name)
        enrich_team_spec_for_swarm(
            spec,
            session_id=session_id,
            mode=mode,
            project_dir=project_dir,
            request_id=request_id,
            channel_id=channel_id,
            request_metadata=request_metadata,
            agent_group_name=agent_group_name,
            agent_group_package_dir=agent_group_package_dir,
        )
        self._apply_trace_context(spec, request_metadata=request_metadata)
        return spec

    @staticmethod
    def apply_team_plan_mode(
        spec: TeamAgentSpec,
        *,
        request_metadata: dict[str, Any] | None,
    ) -> None:
        mode = str((request_metadata or {}).get("mode") or "").strip().lower()
        if mode == "team.plan":
            try:
                spec.enable_team_plan = True
            except (AttributeError, ValueError):
                object.__setattr__(spec, "enable_team_plan", True)

    @staticmethod
    def _apply_trace_context(
        spec: TeamAgentSpec,
        *,
        request_metadata: dict[str, Any] | None,
    ) -> dict[str, str]:
        """Export a transported TraceContext at each Team model boundary."""
        payload = (request_metadata or {}).get(TRACE_CONTEXT_METADATA_KEY)
        if payload is None:
            return {}
        try:
            trace = trace_context_from_dict(payload)
        except ValueError as exc:
            logger.warning("[TeamManager] ignored invalid trace context: %s", exc)
            return {}
        trace_headers = export_trace_headers_for_name(
            trace,
            (request_metadata or {}).get(TRACE_HEADER_EXPORTER_METADATA_KEY),
        )
        if not trace_headers:
            return {}

        def merge_headers(metadata: dict[str, Any] | None) -> dict[str, Any]:
            merged_metadata = dict(metadata or {})
            client_config = dict(merged_metadata.get("client") or {})
            custom_headers = dict(client_config.get("custom_headers") or {})
            client_config["custom_headers"] = {**custom_headers, **trace_headers}
            merged_metadata["client"] = client_config
            return merged_metadata

        def merge_serialized_model_configs(value: Any) -> None:
            """Inject headers into model configs carried by declarative capability specs."""
            if isinstance(value, dict):
                client_config = value.get("model_client_config")
                if isinstance(client_config, dict):
                    custom_headers = dict(client_config.get("custom_headers") or {})
                    client_config["custom_headers"] = {
                        **custom_headers,
                        **trace_headers,
                    }
                for child in value.values():
                    merge_serialized_model_configs(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    merge_serialized_model_configs(child)

        def merge_agent_capability_model_configs(agent_spec: Any) -> None:
            for rail_spec in getattr(agent_spec, "rails", None) or ():
                merge_serialized_model_configs(getattr(rail_spec, "params", None))
            for subagent_spec in getattr(agent_spec, "subagents", None) or ():
                merge_agent_capability_model_configs(subagent_spec)

        for pool_entry in spec.model_pool:
            pool_entry.metadata = merge_headers(pool_entry.metadata)
        if spec.model_router is not None:
            spec.model_router.metadata = merge_headers(spec.model_router.metadata)
        for agent_spec in spec.agents.values():
            if agent_spec.model is not None:
                client_config = agent_spec.model.model_client_config
                custom_headers = dict(client_config.custom_headers or {})
                client_config.custom_headers = {**custom_headers, **trace_headers}
            merge_agent_capability_model_configs(agent_spec)
        return trace_headers

    def _refresh_live_evolution_rail_trace(
        self,
        session_id: str,
        trace_headers: dict[str, str],
    ) -> None:
        """Refresh long-lived evolution rails with the current request trace."""
        if not trace_headers:
            return
        rails = [
            self._team_skill_rails.get(session_id),
            *self._team_member_skill_evolution_rails.get(session_id, []),
        ]
        for rail in rails:
            if rail is None:
                continue
            try:
                evolver = getattr(rail, "evolver")
                current_llm = evolver.llm
                client_config = copy.deepcopy(current_llm.model_client_config)
                client_config.custom_headers = {
                    **dict(client_config.custom_headers or {}),
                    **trace_headers,
                }
                update_llm = getattr(rail, "update_llm")
                update_llm(
                    Model(
                        model_client_config=client_config,
                        model_config=copy.deepcopy(current_llm.model_config),
                    ),
                    evolver.model,
                )
            except Exception as exc:
                logger.warning(
                    "[TeamManager] failed to refresh evolution rail trace: %s",
                    exc,
                )

    async def _refresh_active_team_trace(
        self,
        session_id: str,
        team_name: str,
        request_metadata: dict[str, Any] | None,
    ) -> Any | None:
        if not request_metadata or TRACE_CONTEXT_METADATA_KEY not in request_metadata:
            return
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        pool = getattr(runtime_mgr, "pool", None) if runtime_mgr is not None else None
        get_active = getattr(pool, "get", None)
        if not callable(get_active):
            return
        active = await get_active(team_name)
        team_agent = getattr(active, "agent", None)
        configurator = getattr(team_agent, "_configurator", None)
        context = getattr(configurator, "ctx", None)
        spec = getattr(context, "team_spec", None)
        if spec is None:
            return
        trace_headers = self._apply_trace_context(spec, request_metadata=request_metadata)
        update_model_pool = getattr(team_agent, "update_model_pool", None)
        if callable(update_model_pool):
            update_model_pool(list(spec.model_pool))
        self._refresh_live_evolution_rail_trace(session_id, trace_headers)
        return team_agent

    async def prepare_runtime_activation(self, session_id: str, team_name: str) -> None:
        if self._is_distributed_mode(get_config()):
            async with self._distributed_switch_lock:
                await self._wait_same_session_runner_runtime_released(session_id)
                await self._stop_stale_distributed_sessions(
                    session_id,
                    reason="switch runtime: ",
                )
                self._pending_team_names[session_id] = team_name
            return

        self._pending_team_names[session_id] = team_name

    async def _wait_same_session_runner_runtime_released(
        self,
        session_id: str,
        *,
        timeout_sec: float = 5.0,
        poll_interval_sec: float = 0.1,
    ) -> None:
        """Before same-session rebuild, wait old Runner runtime/messager to stop."""
        if not self._is_distributed_mode(get_config()):
            return
        if not self.is_runtime_active(session_id):
            return
        if self.has_stream_task(session_id):
            return

        # Best-effort eager stop for the cached Runner-owned team agent transport.
        await self._stop_runner_team_agent_transport(session_id)

        team_name = self._resolve_session_team_name(session_id)
        if not team_name:
            return

        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while time.monotonic() < deadline:
            active_team = await runtime_mgr.pool.get(team_name)
            if active_team is None:
                logger.info(
                    "[TeamManager] same-session runtime released before rebuild: "
                    "session_id=%s team_name=%s",
                    session_id,
                    team_name,
                )
                return
            await asyncio.sleep(max(0.02, poll_interval_sec))

        logger.warning(
            "[TeamManager] same-session runtime still active before rebuild timeout: "
            "session_id=%s team_name=%s timeout=%.1fs",
            session_id,
            team_name,
            timeout_sec,
        )

    async def prepare_session_switch(
        self,
        target_session_id: str,
        reason: str = "",
        previous_session_id: str | None = None,
    ) -> None:
        """Enforce the distributed runtime's single-session switch policy.

        Local Runner-owned teams use session-scoped team names and may stay
        active concurrently. Distributed deployments retain the existing
        single-session behavior because their bootstrap resources are scoped to
        one active session per channel.
        """
        normalized_previous = str(previous_session_id or "").strip()
        pre_signaled_session_ids: set[str] = set()
        if normalized_previous and normalized_previous != target_session_id:
            await self.offload_session_kv_cache(
                normalized_previous,
                reason=f"{reason}session-switch",
            )
            pre_signaled_session_ids.add(normalized_previous)

        if not self._is_distributed_mode(get_config()):
            logger.info(
                "[TeamManager] %sprepare_session_switch skipped for local runtime target=%s",
                reason,
                target_session_id,
            )
            return

        async with self._distributed_switch_lock:
            await self._stop_stale_distributed_sessions(
                target_session_id,
                reason=reason,
                pre_signaled_session_ids=pre_signaled_session_ids,
            )

    async def offload_session_kv_cache(self, session_id: str, reason: str = "") -> bool:
        """Dispatch KVC offload for a Team session without changing runtime state."""
        return await kv_cache_hooks.dispatch_for_session(
            "offload",
            session_id=session_id,
            reason=reason,
            resolve_team_name=self._lookup_session_team_name,
        )

    async def prefetch_session_kv_cache(self, session_id: str, reason: str = "") -> bool:
        """Dispatch KVC prefetch for a historical Team session without resuming it."""
        return await kv_cache_hooks.dispatch_for_session(
            "prefetch",
            session_id=session_id,
            reason=f"{reason}history-resume",
            resolve_team_name=self._lookup_session_team_name,
        )

    async def _stop_stale_distributed_sessions(
        self,
        target_session_id: str,
        *,
        reason: str,
        pre_signaled_session_ids: set[str] | None = None,
    ) -> None:
        """Stop active or pending distributed sessions except the target."""
        stale_sessions = [
            session_id
            for session_id in self._active_team_names
            if session_id != target_session_id
        ]
        stale_sessions.extend(
            session_id
            for session_id in self._pending_team_names
            if session_id != target_session_id
        )
        logger.info(
            "[TeamManager] %sprepare_session_switch target=%s active=%s pending=%s stale=%s",
            reason,
            target_session_id,
            list(self._active_team_names),
            list(self._pending_team_names),
            list(dict.fromkeys(stale_sessions)),
        )

        already_signaled = pre_signaled_session_ids or set()
        for stale_session_id in dict.fromkeys(stale_sessions):
            if stale_session_id not in already_signaled:
                await self.offload_session_kv_cache(
                    stale_session_id,
                    reason=f"{reason}session-switch",
                )
            await self.stop_session_runtime(
                stale_session_id,
                reason=reason,
            )

    def commit_runtime_ready(self, session_id: str, team_name: str) -> None:
        self._active_team_names[session_id] = team_name
        self._pending_team_names.pop(session_id, None)
        self._initialized_sessions.add(session_id)
        logger.info(
            "[TeamManager] commit_runtime_ready session_id=%s team_name=%s active=%s pending=%s",
            session_id,
            team_name,
            list(self._active_team_names),
            list(self._pending_team_names),
        )

    def clear_pending_runtime(self, session_id: str) -> None:
        self._pending_team_names.pop(session_id, None)

    def clear_active_runtime(self, session_id: str) -> None:
        removed = self._active_team_names.pop(session_id, None)
        if removed is not None:
            logger.info(
                "[TeamManager] clear_active_runtime: session_id=%s team_name=%s remaining_active=%s",
                session_id, removed, list(self._active_team_names),
            )

    def _lookup_session_team_name(self, session_id: str) -> str | None:
        active_team_name = self._active_team_names.get(session_id)
        if active_team_name:
            return active_team_name
        pending_team_name = self._pending_team_names.get(session_id)
        if pending_team_name:
            return pending_team_name

        metadata = get_session_metadata(session_id)
        team_name = str(metadata.get("team_name") or "").strip()
        return team_name or None

    def _resolve_session_team_name(self, session_id: str) -> str | None:
        team_name = self._lookup_session_team_name(session_id)
        if team_name:
            return team_name

        logger.warning(
            "[TeamManager] failed to resolve team_name from active/pending/metadata: session_id=%s",
            session_id,
        )
        return None

    async def _resolve_resumable_runner_entry(self, session_id: str) -> tuple[str, Any] | None:
        """Return a same-session paused/running Runner pool entry when resumable."""
        team_name = self._lookup_session_team_name(session_id)
        if not team_name:
            return None

        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        entry = await runtime_mgr.pool.get(team_name)
        if entry is None or getattr(entry, "current_session_id", None) != session_id:
            return None
        # Trust the Runner pool over claw-local active/pending markers here.
        # The local markers can be stale after a team.plan round pauses on
        # exit_plan_mode, but the pool still owns the resumable runtime.
        if getattr(entry, "state", None) not in {RuntimeState.PAUSED, RuntimeState.RUNNING}:
            return None
        return team_name, entry

    async def has_resumable_runtime(self, session_id: str) -> bool:
        return await self._resolve_resumable_runner_entry(session_id) is not None

    async def session_has_runtime(self, session_id: str) -> bool:
        return (
            self.is_runtime_active(session_id)
            or self.is_runtime_pending(session_id)
            or self.has_stream_task(session_id)
            or await self.has_resumable_runtime(session_id)
        )

    def _restore_active_runtime(self, session_id: str, team_name: str) -> None:
        self._active_team_names[session_id] = team_name
        self._pending_team_names.pop(session_id, None)
        logger.info(
            "[TeamManager] restored resumable runtime: session_id=%s team_name=%s active=%s pending=%s",
            session_id,
            team_name,
            list(self._active_team_names),
            list(self._pending_team_names),
        )

    async def restore_resumable_runtime(self, session_id: str) -> bool:
        resolved = await self._resolve_resumable_runner_entry(session_id)
        if resolved is None:
            return False
        team_name, _entry = resolved
        self._restore_active_runtime(session_id, team_name)
        return True

    async def wait_for_resumable_runtime(
        self,
        session_id: str,
        *,
        timeout_sec: float = 1.0,
        poll_interval_sec: float = 0.05,
    ) -> bool:
        """Best-effort wait for a paused/running Runner pool entry to become resumable."""
        if self.is_runtime_active(session_id):
            return True
        if await self.restore_resumable_runtime(session_id):
            return True

        deadline = time.monotonic() + max(0.0, timeout_sec)
        sleep_sec = max(0.01, poll_interval_sec)
        while time.monotonic() < deadline:
            await asyncio.sleep(sleep_sec)
            if await self.restore_resumable_runtime(session_id):
                logger.info(
                    "[TeamManager] recovered resumable runtime after wait: session_id=%s",
                    session_id,
                )
                return True
        return self.is_runtime_active(session_id)

    @staticmethod
    def _resolve_delete_session_team_name(session_id: str) -> str | None:
        metadata = get_session_metadata(session_id)
        team_name = str(metadata.get("team_name") or "").strip()
        if team_name:
            return team_name

        logger.warning(
            "[TeamManager] failed to resolve delete team_name from metadata: session_id=%s",
            session_id,
        )
        return None


    @staticmethod
    def _is_postgresql_storage(team_cfg: dict[str, Any]) -> bool:
        return is_postgresql_storage(team_cfg)

    @staticmethod
    def _extract_pg_endpoint(team_cfg: dict[str, Any]) -> tuple[str, int]:
        return extract_pg_endpoint(team_cfg)

    @staticmethod
    async def _run_command(*args: str) -> tuple[int, str]:
        return await run_command(*args, subprocess_timeout_sec=_SUBPROCESS_TIMEOUT_SEC)

    async def _is_pg_available(self, host: str, port: int) -> bool:
        return await is_pg_available(host, port, subprocess_timeout_sec=_SUBPROCESS_TIMEOUT_SEC)

    async def _try_start_pg_cluster(self) -> bool:
        return await try_start_pg_cluster(subprocess_timeout_sec=_SUBPROCESS_TIMEOUT_SEC)

    async def _ensure_postgresql_for_leader(self, config_base: dict[str, Any]) -> None:
        await ensure_postgresql_for_leader(
            config_base,
            subprocess_timeout_sec=_SUBPROCESS_TIMEOUT_SEC,
            post_start_ready_max_sec=_PG_POST_START_READY_MAX_SEC,
            post_start_ready_init_sleep=_PG_POST_START_READY_INIT_SLEEP,
            post_start_ready_max_sleep=_PG_POST_START_READY_MAX_SLEEP,
            post_start_ready_backoff=_PG_POST_START_READY_BACKOFF,
            post_start_log_every_sec=_PG_POST_START_LOG_EVERY_SEC,
        )

    @staticmethod
    def _initialize_team_shared_skill_links(spec: TeamAgentSpec) -> None:
        """Initialize team shared skill links from the global skill root."""
        global_skills_dir = get_agent_skills_dir()
        if not global_skills_dir.exists():
            logger.warning("[TeamManager] global_skills_dir does not exist: %s", global_skills_dir)
            return

        # Resolve team workspace path
        ws_config = spec.workspace
        ws_path = ws_config.root_path if ws_config and ws_config.root_path else None
        if not ws_path:
            ws_path = str(team_home(spec.team_name) / "team-workspace")

        team_shared_skills_dir = Path(ws_path) / "skills"

        team_shared_skills_dir.mkdir(parents=True, exist_ok=True)
        sync_skill_dir_links(global_skills_dir, team_shared_skills_dir)

        logger.info("[TeamManager] Initialized team shared skill links: %s", team_shared_skills_dir)

    @staticmethod
    def _resolve_team_shared_skills_dir(spec: TeamAgentSpec) -> Path:
        ws_config = spec.workspace
        ws_path = ws_config.root_path if ws_config and ws_config.root_path else None
        if not ws_path:
            ws_path = str(team_home(spec.team_name) / "team-workspace")
        return Path(ws_path) / "skills"

    @staticmethod
    def ensure_team_shared_skills_initialized(spec: TeamAgentSpec) -> None:
        """Ensure team shared skills are available in the team workspace."""
        TeamManager._initialize_team_shared_skill_links(spec)

    def ensure_team_shared_skills_ready_for_session(self, session_id: str, spec: TeamAgentSpec) -> None:
        """Ensure team shared skills are initialized and registered for refresh."""
        self.ensure_team_shared_skills_initialized(spec)
        self.register_team_shared_skill_link_target(
            session_id,
            self._resolve_team_shared_skills_dir(spec),
        )

    def register_team_shared_skill_link_target(self, session_id: str, target: Path) -> None:
        """Register the team shared skills directory for link refresh."""
        self._team_shared_skill_link_targets[session_id] = target

    def refresh_team_shared_skill_links(self, session_id: str) -> bool:
        """Refresh team shared skill links from global skills."""
        target = self._team_shared_skill_link_targets.get(session_id)
        if target is None:
            logger.debug("[TeamManager] no team shared skill link target for session_id=%s", session_id)
            return False
        global_skills_dir = get_agent_skills_dir()
        if not global_skills_dir.exists():
            logger.warning("[TeamManager] global_skills_dir does not exist: %s", global_skills_dir)
            return False
        sync_skill_dir_links(global_skills_dir, target)
        logger.info("[TeamManager] Refreshed team shared skill links: session_id=%s target=%s", session_id, target)
        return True

    def refresh_all_team_shared_skill_links(self) -> int:
        """Refresh every registered team shared skill link view."""
        refreshed = 0
        for session_id in list(self._team_shared_skill_link_targets):
            if self.refresh_team_shared_skill_links(session_id):
                refreshed += 1
        return refreshed

    async def create_team(
        self,
        session_id: str,
        deep_agent: DeepAgent,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> TeamAgent:
        """Build an auxiliary TeamAgent for distributed teammate bootstrap.

        Local leader requests are built and owned by Runner's TeamRuntimePool;
        they must not use this cache.
        """
        config_base = get_config()
        await self._ensure_postgresql_for_leader(config_base)
        logger.info("[TeamManager] building TeamAgentSpec: session_id=%s", session_id)
        spec, has_binding = self._load_session_team_spec(session_id)
        if not has_binding:
            self._apply_session_scoped_team_name(
                spec,
                session_id=session_id,
            )

        resolved_mode = str((request_metadata or {}).get("mode") or "").strip()
        # Provider-based assembly: source every member capability from the shared
        # config source, no pre-built parent DeepAgent / customizer. Mirrors
        # get_swarm_enriched_team_spec so a team rebuilt here (e.g. the distributed
        # teammate's auxiliary leader) carries provider declarations plus the
        # serializable build_context_seed.
        from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm

        self.apply_team_plan_mode(spec, request_metadata=request_metadata)
        enrich_team_spec_for_swarm(
            spec,
            session_id=session_id,
            mode=resolved_mode,
            project_dir=(request_metadata or {}).get("project_dir"),
            request_id=request_id,
            channel_id=channel_id,
            request_metadata=request_metadata,
        )
        self._apply_trace_context(spec, request_metadata=request_metadata)

        logger.info("[TeamManager] TeamAgentSpec ready: team_name=%s", spec.team_name)

        token = set_session_id(session_id)
        try:
            logger.info("[TeamManager] creating TeamAgent from spec")
            team_agent = spec.build()
            team_agent.channel_id = channel_id  # 记录 channel，供 _destroy_other_sessions 按 channel 隔离
            self._team_agents[session_id] = team_agent
            # After build, initialize team shared skill links.
            self.ensure_team_shared_skills_ready_for_session(session_id, spec)

            if self._is_distributed_mode(config_base):
                try:
                    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
                        attach_build_team_post_tool_registration_hook,
                        attach_clean_team_distributed_teardown_wrapper,
                        attach_distributed_local_spawn_guard,
                        attach_remote_bootstrap_ack_listener,
                        attach_shutdown_member_remote_cleanup_wrapper,
                    )

                    attach_distributed_local_spawn_guard(
                        team_agent,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    attach_build_team_post_tool_registration_hook(
                        team_agent,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    attach_shutdown_member_remote_cleanup_wrapper(
                        team_agent,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    attach_clean_team_distributed_teardown_wrapper(
                        team_agent,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    attach_remote_bootstrap_ack_listener(
                        team_agent,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] remote_member_bootstrap wrapper attach failed: %s",
                        exc,
                    )
            logger.info(
                "[TeamManager] Team created: session_id=%s, team_name=%s",
                session_id,
                spec.team_name,
            )
            return team_agent
        finally:
            reset_session_id(token)

    async def get_or_create_team(
        self,
        session_id: str,
        deep_agent: DeepAgent,
        request_id: str | None = None,
        channel_id: str | None = None,
        request_metadata: dict[str, Any] | None = None,
    ) -> TeamAgent:
        """Return the distributed bootstrap TeamAgent for a session.

        This cache is only used by remote member bootstrap on distributed
        teammate processes. Distributed channels intentionally retain a single
        cached session and destroy the previous auxiliary TeamAgent on switch.
        """
        async with self._bootstrap_lock:
            team_agent = self._team_agents.get(session_id)
            if team_agent is not None:
                return team_agent

            await self._destroy_other_sessions(session_id, channel_id)
            return await self.create_team(
                session_id,
                deep_agent,
                request_id,
                channel_id,
                request_metadata,
            )

    async def interact(
        self,
        session_id: str,
        user_input: Any,
        *,
        request_metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        try:
            if not self.is_runtime_active(session_id):
                restored = await self.wait_for_resumable_runtime(session_id)
                if restored:
                    logger.info(
                        "[TeamManager] interact restored paused runtime before delivery: session_id=%s",
                        session_id,
                    )

            team_name = self.get_active_team_name(session_id)
            if not team_name:
                logger.warning(
                    "[TeamManager] interact ignored for non-active team session: "
                    "session_id=%s active_sessions=%s reason=not_active",
                    session_id,
                    list(self._active_team_names),
                )
                return False, "not_active"

            team_agent = await self._refresh_active_team_trace(
                session_id,
                team_name,
                request_metadata,
            )
            if team_agent is not None:
                await notify_remote_members_trace_context_update(
                    team_agent,
                    session_id,
                    request_metadata=request_metadata,
                )

            # Last stop before the message enters the team runner interact path.
            server_logger.info(
                "[AgentServer] team message entering runner interact: session_id=%s team=%s payload=%s",
                session_id,
                team_name,
                _safe_payload_preview(user_input),
            )
            result = await Runner.interact_agent_team(
                user_input,
                team_name=team_name,
                session_id=session_id,
            )
            if not result:
                reason = getattr(result, "reason", None) or "runner_failed"
                logger.warning(
                    "[TeamManager] interact failed against runner runtime: session_id=%s team=%s reason=%s",
                    session_id,
                    team_name,
                    reason,
                )
                return False, reason
            return True, None
        except Exception as exc:
            logger.error("[TeamManager] interact failed: session_id=%s, error=%s", session_id, exc)
            return False, "exception"

    # TeamSkillEvolutionRail accessors.

    def get_team_skill_rail(self, session_id: str) -> Any | None:
        return self._team_skill_rails.get(session_id)

    def get_team_skill_create_rail(self, session_id: str) -> Any | None:
        return self._team_skill_create_rails.get(session_id)

    def find_team_skill_rail_for_request(self, request_id: str) -> Any | None:
        """Find the TeamSkillEvolutionRail that owns a pending approval with this request_id."""
        for rail in self._team_skill_rails.values():
            if request_id in getattr(rail, "_pending_approval_snapshots", {}):
                return rail
            if request_id in getattr(rail, "_pending_governance", {}):
                return rail
        return None

    async def drain_team_skill_events(self, session_id: str) -> list[dict]:
        """Drain buffered approval events from this session's TeamSkillEvolutionRail."""
        rail = self._team_skill_rails.get(session_id)
        if rail is None:
            return []
        return await rail.drain_pending_approval_events()

    def register_team_skill_rail(self, session_id: str, rail: Any) -> None:
        """Register a TeamSkillEvolutionRail instance for the given session."""
        self._team_skill_rails[session_id] = rail

    def register_team_member_skill_evolution_rail(self, session_id: str, rail: Any) -> None:
        """Register a member SkillEvolutionRail instance for hot config updates."""
        rails = self._team_member_skill_evolution_rails.setdefault(session_id, [])
        if rail not in rails:
            rails.append(rail)

    def register_team_skill_create_rail(self, session_id: str, rail: Any) -> None:
        """Register a TeamSkillCreateRail instance for hot config updates."""
        self._team_skill_create_rails[session_id] = rail

    def register_team_rail_context(self, session_id: str, context: TeamRailMountContext) -> None:
        """Register session context needed to rebuild missing team rails."""
        if getattr(context.member_info, "role", None) == "leader":
            self._team_rail_contexts[session_id] = context

    def get_team_rail_context(self, session_id: str) -> TeamRailMountContext | None:
        """Return the stored leader rail mount context for a session."""
        return self._team_rail_contexts.get(session_id)

    def register_team_live_rail(self, session_id: str, agent: Any, rail: Any) -> None:
        """Remember a live rail owner so hot reload can unregister mounted rails."""
        rails = self._team_live_rails.setdefault(session_id, [])
        entry = (agent, rail)
        if entry not in rails:
            rails.append(entry)

    def _clear_team_rail_registries(self, session_id: str) -> None:
        self._team_skill_rails.pop(session_id, None)
        self._team_member_skill_evolution_rails.pop(session_id, None)
        self._team_skill_create_rails.pop(session_id, None)
        self._team_rail_contexts.pop(session_id, None)
        self._team_live_rails.pop(session_id, None)
        self._team_shared_skill_link_targets.pop(session_id, None)

    def _clear_terminal_session_markers(self, session_id: str) -> None:
        """Release process-wide markers only for non-resumable teardown."""
        self.clear_session_initialized(session_id)
        self.reset_seen_team_events(session_id)
        self.reset_workflow_completed(session_id)
        self.pop_cron_completion(session_id)
        self._pending_team_evolution_watcher_sessions.discard(session_id)

    async def _cancel_team_evolution_watcher(self, session_id: str) -> None:
        watcher_task = self._team_evolution_watchers.pop(session_id, None)
        if watcher_task and not watcher_task.done():
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "[TeamManager] evolution watcher stop failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

    async def _unregister_live_rail(self, session_id: str, rail: Any) -> None:
        live_rails = self._team_live_rails.get(session_id, [])
        remaining: list[tuple[Any, Any]] = []
        for agent, live_rail in live_rails:
            if live_rail is not rail:
                remaining.append((agent, live_rail))
                continue
            unregister = getattr(agent, "unregister_rail", None)
            if callable(unregister):
                try:
                    result = unregister(live_rail)
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] live rail unregister failed: session_id=%s rail=%s error=%s",
                        session_id,
                        type(live_rail).__name__,
                        exc,
                    )
        if remaining:
            self._team_live_rails[session_id] = remaining
        else:
            self._team_live_rails.pop(session_id, None)

    def _build_and_mount_member_rails_for_context(
        self,
        session_id: str,
        context: TeamRailMountContext,
        *,
        mount_team_skill_rail: bool,
        mount_team_skill_create_rail: bool,
        mount_skill_evolution_rail: bool,
    ) -> tuple[Any | None, Any | None]:
        """Rebuild team rails for a session using the stored mount context."""
        latest_config = get_config()
        context.team_workspace.config = latest_config
        member_rails = build_member_rails(
            member_info=context.member_info,
            runtime=context.runtime,
            team_workspace=context.team_workspace,
        )
        team_skill_rail: Any | None = None
        team_skill_create_rail: Any | None = None
        for rail in member_rails:
            if isinstance(rail, TeamSkillEvolutionRail) and mount_team_skill_rail:
                context.agent.add_rail(rail)
                self.register_team_live_rail(session_id, context.agent, rail)
                team_skill_rail = rail
            elif isinstance(rail, SkillEvolutionRail) and mount_skill_evolution_rail:
                context.agent.add_rail(rail)
                self.register_team_member_skill_evolution_rail(session_id, rail)
            elif isinstance(rail, TeamSkillCreateRail) and mount_team_skill_create_rail:
                context.agent.add_rail(rail)
                self.register_team_live_rail(session_id, context.agent, rail)
                team_skill_create_rail = rail

        if team_skill_rail is not None:
            self.register_team_skill_rail(session_id, team_skill_rail)
        if team_skill_create_rail is not None:
            self.register_team_skill_create_rail(session_id, team_skill_create_rail)
        return team_skill_rail, team_skill_create_rail

    async def update_evolution_config(self, config: dict[str, Any] | None) -> None:
        """Hot-update team evolution rails for existing team runtimes."""
        auto_scan_enabled = get_evolution_auto_scan_enabled(config)
        signal_trigger_enabled = get_evolution_signal_trigger_enabled(
            config,
            fallback=auto_scan_enabled,
        )
        review_trigger_enabled = get_evolution_review_trigger_enabled(
            config,
            fallback=auto_scan_enabled,
        )
        skill_create_enabled = get_skill_create_enabled(config)

        for rails in self._team_member_skill_evolution_rails.values():
            for rail in rails:
                try:
                    rail.signal_trigger = signal_trigger_enabled
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] SkillEvolutionRail signal_trigger update failed: %s",
                        exc,
                    )

        for rail in self._team_skill_rails.values():
            try:
                rail.review_trigger = review_trigger_enabled
            except Exception as exc:
                logger.warning(
                    "[TeamManager] TeamSkillEvolutionRail review_trigger update failed: %s",
                    exc,
                )

        if not skill_create_enabled:
            for session_id, rail in list(self._team_skill_create_rails.items()):
                await self._unregister_live_rail(session_id, rail)
                self._team_skill_create_rails.pop(session_id, None)
            return

        for session_id, context in list(self._team_rail_contexts.items()):
            if session_id in self._team_skill_create_rails:
                continue
            self._build_and_mount_member_rails_for_context(
                session_id,
                context,
                mount_team_skill_rail=False,
                mount_team_skill_create_rail=True,
                mount_skill_evolution_rail=False,
            )

    async def destroy_team(self, session_id: str) -> bool:
        async with self._bootstrap_lock:
            return await self._destroy_team(session_id)

    async def _destroy_other_sessions(self, current_session_id: str, channel_id: str | None = None) -> None:
        """Destroy stale distributed bootstrap TeamAgents on session switch.

        按 channel_id 过滤：singleton 模式下多个 channel 共享同一个 TeamManager，
        只销毁同 channel 的旧 session，不影响其他 channel 正在运行的 team 实例。
        """
        stale_session_ids = []
        for sid, agent in self._team_agents.items():
            if sid != current_session_id and (
                channel_id is None or getattr(agent, 'channel_id', None) == channel_id
            ):
                stale_session_ids.append(sid)
        for stale_session_id in stale_session_ids:
            await self._destroy_team(stale_session_id)

    async def _destroy_team(self, session_id: str) -> bool:
        await self._cleanup_runtime_locals(session_id)

        team_agent = self._team_agents.pop(session_id, None)
        cleaned = False
        try:
            if team_agent is None:
                logger.info("[TeamManager] no in-memory team for session_id=%s", session_id)
                return False

            token = set_session_id(session_id)
            try:
                try:
                    await self._purge_team_member_skill_mounts(session_id, team_agent)
                    cleaned = await team_agent.destroy_team(force=True)
                finally:
                    await release_a2x_reservations_for_session(session_id, team_agent=team_agent)
                    await _stop_team_messager(team_agent, session_id=session_id)
            finally:
                reset_session_id(token)

            logger.info(
                "[TeamManager] Team cleaned via core API: session_id=%s cleaned=%s",
                session_id,
                cleaned,
            )
        except Exception as exc:
            logger.error(
                "[TeamManager] destroy team failed: session_id=%s error=%s",
                session_id,
                exc,
            )

        return cleaned

    async def cleanup_all(self) -> None:
        async with self._bootstrap_lock:
            session_ids = list(self._team_agents.keys())
            for session_id in session_ids:
                await self._destroy_team(session_id)
            logger.info("[TeamManager] all teams cleaned")

    def get_team_agent(self, session_id: str) -> TeamAgent | None:
        return self._team_agents.get(session_id)

    def get_monitor_handler(self, session_id: str) -> TeamMonitorHandler | None:
        return self._team_monitors.get(session_id)

    def register_monitor(self, session_id: str, handler: TeamMonitorHandler) -> None:
        self._team_monitors[session_id] = handler

    def register_workflow_handler(self, session_id: str, handler: Any) -> None:
        self._workflow_handlers[session_id] = handler

    def get_workflow_handler(self, session_id: str) -> Any | None:
        return self._workflow_handlers.get(session_id)

    def pop_workflow_handler(self, session_id: str) -> Any | None:
        return self._workflow_handlers.pop(session_id, None)

    def register_stream_task(self, session_id: str, task: asyncio.Task) -> None:
        self._stream_tasks[session_id] = task

    def _has_local_team_runtime(self, session_id: str) -> bool:
        """Return whether the session should use the legacy in-memory TeamAgent path."""
        return self._is_distributed_mode(get_config()) and session_id in self._team_agents

    async def attach_distributed_hooks_for_runner_runtime(
        self,
        team_name: str,
        session_id: str,
        channel_id: str | None = None,
    ) -> bool:
        """Attach distributed bootstrap hooks to Runner-owned TeamAgent.

        When team streaming uses Runner.run_agent_team_streaming(), the actual
        TeamAgent is created and cached by openjiuwen TeamRuntimeManager pool,
        not by TeamManager.create_team(). This method retrieves the Runner-owned
        TeamAgent from GLOBAL_RUNNER's pool and attaches distributed hooks.

        Args:
            team_name: Team name to look up in Runner pool.
            session_id: Session identifier for hook context.
            channel_id: Channel identifier for hook context.

        Returns:
            True if hooks attached successfully, False otherwise.
        """
        config_base = get_config()
        if not self._is_distributed_mode(config_base):
            logger.debug(
                "[TeamManager] non-distributed mode; skip Runner runtime hooks "
                "team_name=%s session_id=%s",
                team_name,
                session_id,
            )
            return False

        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        active_team = await runtime_mgr.pool.get(team_name)
        if active_team is None:
            logger.warning(
                "[TeamManager] Runner pool has no active team for distributed hooks "
                "team_name=%s session_id=%s",
                team_name,
                session_id,
            )
            return False

        team_agent = active_team.agent
        if team_agent is None:
            logger.warning(
                "[TeamManager] ActiveTeam has no agent instance for distributed hooks "
                "team_name=%s session_id=%s",
                team_name,
                session_id,
            )
            return False

        self._runner_team_agents[session_id] = team_agent

        try:
            from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
                attach_build_team_post_tool_registration_hook,
                attach_clean_team_distributed_teardown_wrapper,
                attach_distributed_local_spawn_guard,
                attach_remote_bootstrap_ack_listener,
                attach_shutdown_member_remote_cleanup_wrapper,
            )

            attach_distributed_local_spawn_guard(
                team_agent,
                session_id=session_id,
                channel_id=channel_id,
            )
            attach_build_team_post_tool_registration_hook(
                team_agent,
                session_id=session_id,
                channel_id=channel_id,
            )
            attach_shutdown_member_remote_cleanup_wrapper(
                team_agent,
                session_id=session_id,
                channel_id=channel_id,
            )
            attach_clean_team_distributed_teardown_wrapper(
                team_agent,
                session_id=session_id,
                channel_id=channel_id,
            )
            attach_remote_bootstrap_ack_listener(
                team_agent,
                session_id=session_id,
                channel_id=channel_id,
            )
            logger.info(
                "[TeamManager] distributed hooks attached to Runner-owned TeamAgent "
                "team_name=%s session_id=%s channel_id=%s",
                team_name,
                session_id,
                channel_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[TeamManager] distributed hooks attach failed for Runner-owned TeamAgent "
                "team_name=%s session_id=%s error=%s",
                team_name,
                session_id,
                exc,
            )
            return False

    async def _purge_team_member_skill_mounts(
        self, session_id: str, team_agent: TeamAgent | None = None
    ) -> None:
        """停 team 运行时前显式释放 member 快照挂载的团包 skills（幂等、best-effort）。

        专家团版"单专家 _purge_expert_skill_mounts"：NativeHarness.stop/dispose
        不调 unload_extension，团包快照热挂的 skills 仅靠实例 GC 释放，这里在
        harness 还活着时补做显式清理（语义同 agent-core 当前 _unbind SKILL 分支）。

        team_agent 传入时清该 leader 下的 member（legacy 内存路径）；为 None 时
        经 Runner 的 TeamRuntimePool 按 session 枚举 leader（本地模式主路径——
        本地 leader 由 Runner 的 pool 持有，见 _has_local_team_runtime）。
        非专家团会话（metadata 无 team 绑定）/包缓存缺失/解析失败均为空操作。
        """
        try:
            agent_group_name = self._resolve_session_agent_group(session_id)
        except Exception:
            agent_group_name = None
        if not agent_group_name:
            return
        skill_dirs = _collect_team_package_skill_dirs(agent_group_name)
        if not skill_dirs:
            return
        if team_agent is not None:
            leaders: list[Any] = [team_agent]
        else:
            leaders = []
            try:
                manager = _runner_team_runtime_manager(Runner)
                pool = getattr(manager, "pool", None)
                if pool is not None:
                    leaders = [
                        entry.agent
                        for entry in await pool.teams_for_session(session_id)
                        if getattr(entry, "agent", None) is not None
                    ]
            except Exception as exc:
                logger.warning(
                    "[TeamManager] 枚举 Runner pool team 失败（跳过 member skills purge）: "
                    "session_id=%s error=%s",
                    session_id,
                    exc,
                )
                return
        for leader in leaders:
            for runtime in _iter_local_member_runtimes(leader):
                try:
                    await _purge_runtime_skill_mounts(runtime, skill_dirs)
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] purge member skill mounts failed: session_id=%s error=%s",
                        session_id,
                        exc,
                    )

    async def _stop_local_team_runtime(self, session_id: str, team_agent: TeamAgent) -> bool:
        await self._purge_team_member_skill_mounts(session_id, team_agent)
        stopped = False
        stop_coordination = getattr(team_agent, "stop_coordination", None) or getattr(
            team_agent,
            "_stop_coordination",
            None,
        )
        if callable(stop_coordination):
            try:
                await stop_coordination()
                stopped = True
            except Exception as exc:
                logger.warning(
                    "[TeamManager] stop local team coordination failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        try:
            await release_a2x_reservations_for_session(session_id, team_agent=team_agent)
        except Exception as exc:
            logger.warning(
                "[TeamManager] release A2X reservations failed: session_id=%s error=%s",
                session_id,
                exc,
            )
        try:
            await _stop_team_messager(team_agent, session_id=session_id)
        except Exception as exc:
            logger.warning(
                "[TeamManager] stop local team messager failed: session_id=%s error=%s",
                session_id,
                exc,
            )
        return stopped

    async def _stop_runner_team_agent_transport(self, session_id: str) -> None:
        if not self._is_distributed_mode(get_config()):
            self._runner_team_agents.pop(session_id, None)
            return

        team_agent = self._runner_team_agents.pop(session_id, None)
        if team_agent is None:
            return

        stop_coordination = getattr(team_agent, "stop_coordination", None)
        if callable(stop_coordination):
            try:
                await stop_coordination()
            except Exception as exc:
                logger.warning(
                    "[TeamManager] stop Runner-owned team coordination failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        try:
            await _stop_team_messager(team_agent, session_id=session_id)
        except Exception as exc:
            logger.warning(
                "[TeamManager] stop Runner-owned team messager failed: session_id=%s error=%s",
                session_id,
                exc,
            )

    async def _cleanup_runtime_locals(
        self, session_id: str, *, finalize_workflows: bool = True
    ) -> None:
        watcher_task = self._team_evolution_watchers.pop(session_id, None)
        if watcher_task and not watcher_task.done():
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "[TeamManager] evolution watcher stop failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        stream_task = self._stream_tasks.pop(session_id, None)
        if stream_task and not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "[TeamManager] stream stop failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        monitor_handler = self._team_monitors.pop(session_id, None)
        if monitor_handler is not None:
            try:
                await monitor_handler.stop()
            except Exception as exc:
                logger.warning(
                    "[TeamManager] monitor stop failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        workflow_handler = self.pop_workflow_handler(session_id)
        if workflow_handler is not None:
            try:
                # On non-resumable teardown the team runtime (and the swarmflow
                # background task it drives) is gone, so no further workflow
                # events can arrive — finalize any still-running run to a
                # terminal status before stopping, otherwise the checkpoint
                # would keep it 'running' forever. Pause keeps the runtime
                # parked and resumable in place, so it opts out.
                if finalize_workflows:
                    workflow_handler.finalize_pending_runs()
                await workflow_handler.stop()
                logger.info(
                    "[WF_DBG cleanup] workflow handler stopped: session_id=%s "
                    "finalized=%s",
                    session_id,
                    finalize_workflows,
                )
            except Exception as exc:
                logger.warning(
                    "[WF_DBG cleanup] workflow handler stop failed: session_id=%s error=%s",
                    session_id,
                    exc,
                )

        self._clear_team_rail_registries(session_id)

    async def _stop_runner_team_runtime(
        self, session_id: str, team_name: str, caller: str
    ) -> bool:
        """Stop Runner-owned team runtime, with proper cancellation and error handling.

        Args:
            session_id: The session ID
            team_name: The team name to stop
            caller: Caller identifier for logging (e.g., "cancel", "terminate", "pause")

        Returns:
            True if stop was successful, False otherwise
        """
        try:
            result = await Runner.stop_agent_team(
                team_name=team_name,
                session_id=session_id,
            )
            logger.info(
                "[TeamManager] %s: Runner pool entry removed: "
                "session_id=%s team_name=%s",
                caller,
                session_id,
                team_name,
            )
            return result
        except asyncio.CancelledError:
            logger.warning(
                "[TeamManager] %s: Runner stop cancelled: "
                "session_id=%s team_name=%s",
                caller,
                session_id,
                team_name,
            )
            return False
        except Exception as exc:
            logger.warning(
                "[TeamManager] %s: Runner stop failed: "
                "session_id=%s team_name=%s error=%s",
                caller,
                session_id,
                team_name,
                exc,
            )
            return False

    async def _finalize_runtime_cleanup(self, session_id: str, caller: str) -> None:
        """Finalize runtime cleanup: cleanup locals and clear active/pending registrations."""
        logger.info(
            "[TeamManager] %s: executing cleanup, session_id=%s",
            caller,
            session_id,
        )
        await self._cleanup_runtime_locals(session_id)
        logger.info(
            "[TeamManager] %s: cleanup done, clearing active, session_id=%s",
            caller,
            session_id,
        )
        self.clear_active_runtime(session_id)
        self.clear_pending_runtime(session_id)
        # These round/session markers live on the process-wide TeamManager.
        # TUI disconnect cancels the async event generator before its normal
        # tail can clear them, so terminal runtime cleanup must own the
        # idempotent release as well.
        self._clear_terminal_session_markers(session_id)
        logger.info(
            "[TeamManager] %s: clear done, session_id=%s",
            caller,
            session_id,
        )

    async def _wait_for_stream_task_exit(
        self,
        session_id: str,
        *,
        timeout_sec: float = _TEAM_STREAM_EXIT_GRACE_TIMEOUT_SEC,
    ) -> bool:
        """Wait briefly for a team stream task to finish its own cleanup."""
        stream_task = self._stream_tasks.get(session_id)
        if stream_task is None or stream_task.done():
            return True

        try:
            await asyncio.wait_for(
                asyncio.shield(stream_task),
                timeout=timeout_sec,
            )
            logger.info(
                "[TeamManager] stream task exited within grace timeout: "
                "session_id=%s timeout_sec=%.1f",
                session_id,
                timeout_sec,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "[TeamManager] stream task did not exit within grace timeout: "
                "session_id=%s timeout_sec=%.1f; cancelling during cleanup",
                session_id,
                timeout_sec,
            )
            return False
        except asyncio.CancelledError:
            if stream_task.done() and stream_task.cancelled():
                logger.info(
                    "[TeamManager] stream task was cancelled during grace timeout: "
                    "session_id=%s",
                    session_id,
                )
                return True
            raise
        except Exception as exc:
            logger.warning(
                "[TeamManager] stream task exited with error during grace timeout: "
                "session_id=%s error=%s",
                session_id,
                exc,
            )
            return True

    async def terminate_session_runtime(self, session_id: str, reason: str = "") -> bool:
        """Stop-like teardown for the current team session runtime.

        This stops the foreground stream/monitor owned by claw and then asks the
        Runner-owned team runtime to enter the stop state. Used for explicit
        team stop so the same session can resume later.
        """
        async with self._get_lifecycle_lock(session_id):
            has_stream_task = session_id in self._stream_tasks
            has_local_team_runtime = self._has_local_team_runtime(session_id)
            has_team_runtime = (
                has_local_team_runtime
                or session_id in self._team_monitors
                or self.is_runtime_active(session_id)
                or self.is_runtime_pending(session_id)
            )
            if not has_stream_task and not has_team_runtime:
                self._clear_terminal_session_markers(session_id)
                return False
            logger.info(
                "[TeamManager] %s terminate team session runtime: session_id=%s",
                reason,
                session_id,
            )

            # Resolve team_name early before cleanup, from active/pending/metadata
            team_name = self._resolve_session_team_name(session_id)

            await kv_cache_hooks.dispatch_signal(
                "offload",
                session_id=session_id,
                team_name=team_name,
                reason=f"{reason}team-terminate",
            )

            # Stop Runner-owned runtime first before cleaning locals
            # to avoid gate/teardown races
            if team_name:
                await self._stop_runner_team_runtime(session_id, team_name, "terminate")

            if has_local_team_runtime:
                cleaned = await self._destroy_team(session_id)
            else:
                cleaned = False

            await self._finalize_runtime_cleanup(session_id, "terminate")
        logger.info(
            "[TeamManager] %steam session terminated: session_id=%s cleaned=%s",
            reason,
            session_id,
            cleaned,
        )
        return True

    async def cancel_session_runtime(self, session_id: str, reason: str = "") -> bool:
        """Cancel the current team session runtime, removing it from Runner pool.

        Unlike pause/terminate, this fully stops the Runner-owned team runtime
        so it is removed from the pool. This prevents subsequent sessions from
        hitting "present in pool but missing from DB" reject_inconsistent errors.

        Used for team cancel intent where the session should not be resumed.
        """
        logger.info(
            "[TeamManager] cancel_session_runtime 入口: session_id=%s reason=%s",
            session_id, reason,
        )
        # 通知正在执行的 pause 操作中止自身，让 cancel 尽快获取 lifecycle lock
        self._cancel_requested[session_id] = True

        # 抢占：主动取消正在执行的 pause 任务，使其抛出 CancelledError 释放锁
        pause_task = self._active_pause_tasks.get(session_id)
        if pause_task and not pause_task.done():
            pause_task.cancel()
            logger.info(
                "[TeamManager] cancel: preempting pause task, session_id=%s",
                session_id,
            )

        # 如果 lifecycle lock 被其他操作（如 pause）持有，先尝试直接停止 Runner
        # 以避免 cancel 被 pause 阻塞长达数分钟
        lock = self._get_lifecycle_lock(session_id)
        if lock.locked():
            team_name = self._resolve_session_team_name(session_id)
            if team_name:
                await self._stop_runner_team_runtime(
                    session_id, team_name, "cancel: forced"
                )


        async with self._get_lifecycle_lock(session_id):
            # 清理 cancel_requested 标志
            self._cancel_requested.pop(session_id, None)

            has_stream_task = session_id in self._stream_tasks
            has_local_team_runtime = self._has_local_team_runtime(session_id)
            has_team_runtime = (
                has_local_team_runtime
                or session_id in self._team_monitors
                or self.is_runtime_active(session_id)
                or self.is_runtime_pending(session_id)
            )
            if not has_stream_task and not has_team_runtime:
                self._clear_terminal_session_markers(session_id)
                return False

            logger.info(
                "[TeamManager] %s cancel team session runtime: session_id=%s",
                reason,
                session_id,
            )

            # Resolve team_name early before cleanup, from active/pending/metadata
            team_name = self._resolve_session_team_name(session_id)

            # Stop Runner-owned runtime first before cancelling stream task
            # to avoid gate/teardown races and ensure pool removal
            runner_stopped = False
            if team_name:
                runner_stopped = await self._stop_runner_team_runtime(
                    session_id, team_name, "cancel"
                )
                await self._stop_runner_team_agent_transport(session_id)

            cleaned = False

            await self._finalize_runtime_cleanup(session_id, "cancel")

        logger.info(
            "[TeamManager] %steam session cancelled: session_id=%s cleaned=%s runner_stopped=%s",
            reason,
            session_id,
            cleaned,
            runner_stopped,
        )
        return True

    async def stop_session_runtime(
        self,
        session_id: str,
        reason: str = "",
        *,
        stop_runner: bool = True,
    ) -> bool:
        """Stop local runtime resources and, by default, the Runner runtime.

        Permanent delete callers leave the Runner runtime alive until
        ``Runner.delete_agent_team(force=True)`` so agent-core can snapshot its
        member bindings before performing the equivalent stop itself.
        """
        async with self._get_lifecycle_lock(session_id):
            has_stream_task = session_id in self._stream_tasks
            has_local_team_runtime = self._has_local_team_runtime(session_id)
            has_team_runtime = (
                has_local_team_runtime
                or session_id in self._runner_team_agents
                or session_id in self._team_monitors
                or self.is_runtime_active(session_id)
                or self.is_runtime_pending(session_id)
            )
            if not has_stream_task and not has_team_runtime:
                self._clear_terminal_session_markers(session_id)
                return False

            logger.info(
                "[TeamManager] %s stop team session runtime: session_id=%s",
                reason,
                session_id,
            )
            team_agent = self._team_agents.pop(session_id, None) if has_local_team_runtime else None
            await self._cleanup_runtime_locals(session_id)

            stopped = False
            if has_local_team_runtime and team_agent is not None:
                stopped = await self._stop_local_team_runtime(session_id, team_agent)

            team_name = self._resolve_session_team_name(session_id)

            if team_name and stop_runner:
                # 本地模式 leader/member 由 Runner 的 TeamRuntimePool 持有
                # （_has_local_team_runtime 仅 distributed 为真）——stop 之前
                # 先从 pool 取 leader 补做 member 级 skills 释放（幂等，
                # 与 legacy 路径 _stop_local_team_runtime 内的清理重叠亦不冲突）。
                await self._purge_team_member_skill_mounts(session_id)
                try:
                    runner_stopped = await Runner.stop_agent_team(team_name=team_name, session_id=session_id)
                    stopped = runner_stopped or stopped
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] runner stop failed: session_id=%s team_name=%s error=%s",
                        session_id,
                        team_name,
                        exc,
                    )
            if not has_local_team_runtime:
                try:
                    team_agent = self._runner_team_agents.get(session_id)
                    await release_a2x_reservations_for_session(session_id, team_agent=team_agent)
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] release A2X reservations failed: session_id=%s error=%s",
                        session_id,
                        exc,
                    )
                if stop_runner:
                    await self._stop_runner_team_agent_transport(session_id)
                else:
                    # Runner.delete_agent_team(force=True) still owns the
                    # actual TeamAgent stop; drop only Jiuwenswarm's mirror.
                    self._runner_team_agents.pop(session_id, None)

            self.clear_active_runtime(session_id)
            self.clear_pending_runtime(session_id)
            self._clear_terminal_session_markers(session_id)

        logger.info(
            "[TeamManager] %steam session stopped: session_id=%s stopped=%s",
            reason,
            session_id,
            stopped,
        )
        return True

    async def pause_session_runtime(self, session_id: str, reason: str = "") -> bool:
        """Pause the current team runtime for this session.

        Team runtimes are persistent. The current implementation pauses by
        tearing down the foreground stream task and parking the Runner-owned
        runtime in paused state so a later `chat.send` can resume it.
        """
        async with self._get_lifecycle_lock(session_id):
            has_stream_task = session_id in self._stream_tasks
            has_local_team_runtime = self._has_local_team_runtime(session_id)
            has_team_runtime = (
                has_local_team_runtime
                or session_id in self._team_monitors
                or self.is_runtime_active(session_id)
                or self.is_runtime_pending(session_id)
            )
            if not has_stream_task and not has_team_runtime:
                return False

            # 如果 cancel 请求已到达，中止 pause 并让 cancel 执行
            if self._cancel_requested.get(session_id):
                logger.info(
                    "[TeamManager] %s pause aborted: cancel requested for session_id=%s",
                    reason, session_id,
                )
                return False

            logger.info(
                "[TeamManager] %s pause team session runtime: session_id=%s",
                reason,
                session_id,
            )

            team_name = self._resolve_session_team_name(session_id)
            runner_paused = False
            if team_name:
                try:
                    # 再次检查 cancel 标志，避免在等待 Runner.pause 时 cancel 已到达
                    if self._cancel_requested.get(session_id):
                        logger.info(
                            "[TeamManager] %s pause aborted before Runner.pause: session_id=%s",
                            reason, session_id,
                        )
                        return False

                    # 注册当前 pause 任务，供 cancel 抢占取消
                    self._active_pause_tasks[session_id] = asyncio.current_task()
                    try:
                        runner_paused = await Runner.pause_agent_team(
                            team_name=team_name,
                            session_id=session_id,
                        )
                    except asyncio.CancelledError:
                        logger.info(
                            "[TeamManager] %s pause aborted: cancelled by cancel request, session_id=%s",
                            reason, session_id,
                        )
                        team_name = self._resolve_session_team_name(session_id)
                        if team_name:
                            await self._stop_runner_team_runtime(
                                session_id, team_name, "pause aborted"
                            )
                        await self._finalize_runtime_cleanup(session_id, "pause aborted")
                        return False
                    finally:
                        self._active_pause_tasks.pop(session_id, None)

                except Exception as exc:
                    logger.warning(
                        "[TeamManager] runner pause failed: session_id=%s team_name=%s error=%s",
                        session_id,
                        team_name,
                        exc,
                    )

            if runner_paused:
                await self._wait_for_stream_task_exit(session_id)

            # Pause parks the runtime in place (resumable via a later chat.send),
            # so running workflows may still continue — do NOT finalize them.
            await self._cleanup_runtime_locals(session_id, finalize_workflows=False)
            self.clear_active_runtime(session_id)
            self.clear_pending_runtime(session_id)

        logger.info(
            "[TeamManager] %steam session paused: session_id=%s runner_paused=%s",
            reason,
            session_id,
            runner_paused,
        )
        return True

    async def delete_session_runtime(self, session_id: str, reason: str = "") -> bool:
        """Delete a team-mode session and its session-scoped team data.

        Jiuwenswarm scopes team names by session id, so deleting a
        team-mode session should delete the corresponding Agent Team
        before the caller removes the local session directory. If the
        team name cannot be resolved from session metadata, fall back to
        releasing only the session checkpoint.
        """
        team_name = self._resolve_delete_session_team_name(session_id)
        await kv_cache_hooks.stop_runtime_before_terminal_delete(
            self.stop_session_runtime,
            session_id=session_id,
            reason=reason,
        )

        try:
            if team_name:
                await Runner.delete_agent_team(
                    team_name=team_name,
                    session_ids=[session_id],
                    force=True,
                )
            else:
                logger.warning(
                    "[TeamManager] delete session runtime fell back to session release: "
                    "session_id=%s reason=missing_team_name",
                    session_id,
                )
                await Runner.release(session_id)
            logger.info(
                "[TeamManager] %steam session deleted: session_id=%s team_name=%s",
                reason,
                session_id,
                team_name,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[TeamManager] failed to delete team session runtime: session_id=%s team_name=%s error=%s",
                session_id,
                team_name,
                exc,
            )
            return False

    async def _cancel_stream_task(self, session_id: str, reason: str) -> None:
        """Cancel one stream task while serializing its lifecycle operations."""
        async with self._get_lifecycle_lock(session_id):
            task = self._stream_tasks.get(session_id)
            if task is None:
                return
            if not task.done():
                logger.info(
                    "[TeamManager] %s cancel stream task session_id=%s",
                    reason,
                    session_id,
                )
                task.cancel()
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "[TeamManager] stream task await after cancel failed session_id=%s: %s",
                        session_id,
                        exc,
                    )
            if self._stream_tasks.get(session_id) is task:
                self._stream_tasks.pop(session_id, None)

    async def cancel_all_stream_tasks(self, reason: str = "") -> None:
        """Cancel Team stream tasks after AgentServer disconnects."""
        session_ids = list(self._stream_tasks)
        await asyncio.gather(
            *(self._cancel_stream_task(session_id, reason) for session_id in session_ids),
        )


# Singleton TeamManager: all channels share one instance. Team runtime state
# (_stream_tasks / _initialized_sessions / _active_team_names /
# _pending_waiters / _cron_team_completion / rails / watchers) is indexed by
# session_id and is per-session, never per-channel. Sharing the instance lets a
# bridged follow-up request (e.g. a /join member replying from feishu while the
# originating web stream is still
# alive) see the originating channel's runtime markers so it is correctly
# routed through interact() instead of being misidentified as a first request
# and colliding with the Runner team pool.
_team_manager: TeamManager | None = None


def get_team_manager(channel_id: str | None = None) -> TeamManager:
    """Return the singleton TeamManager instance (channel_id is ignored)."""
    global _team_manager
    if _team_manager is None:
        _team_manager = TeamManager()
    return _team_manager


def find_team_skill_rail_across_managers(request_id: str) -> Any | None:
    """Find the TeamSkillEvolutionRail that owns a pending request."""
    return get_team_manager().find_team_skill_rail_for_request(request_id)


def refresh_team_shared_skill_links_across_managers(session_id: str | None = None) -> bool:
    """Refresh team shared skill links on the singleton manager."""
    tm = get_team_manager()
    if session_id is None:
        return tm.refresh_all_team_shared_skill_links() > 0
    return tm.refresh_team_shared_skill_links(session_id)


async def cancel_all_team_stream_tasks_across_managers(reason: str = "") -> None:
    """Cancel all team stream tasks on the singleton manager."""
    await get_team_manager().cancel_all_stream_tasks(reason=reason)


async def stop_team_session_runtime_across_managers(
    session_id: str,
    reason: str = "",
    *,
    stop_runner: bool = True,
) -> bool:
    """Stop a team session runtime on the singleton manager."""
    return await get_team_manager().stop_session_runtime(
        session_id,
        reason=reason,
        stop_runner=stop_runner,
    )


def get_all_team_managers() -> list[TeamManager]:
    """Return the singleton manager wrapped in a list (callers iterate this)."""
    return [get_team_manager()]


def reset_team_manager(channel_id: str | None = None) -> None:
    """Reset the singleton TeamManager (channel_id is ignored)."""
    global _team_manager
    _team_manager = None
