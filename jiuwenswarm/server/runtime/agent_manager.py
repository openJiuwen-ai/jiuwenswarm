# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentManager - 管理 Agent 实例."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, TYPE_CHECKING
from weakref import WeakValueDictionary

from jiuwenswarm.common.e2a.acp.protocol import build_acp_initialize_result
from jiuwenswarm.agents.harness.team import get_team_manager
from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.local_env_config import (
    apply_env_overrides_to_active,
    apply_env_removals,
    bind_task_env_overlay,
    build_effective_env_overlay,
    promote_staged_env,
    replace_active_env,
    reset_task_env_overlay,
    seal_env_mapping,
    stage_env_overrides,
)
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    infer_multimodal_env_removals,
    merge_reload_env_snapshot,
    sync_multimodal_env_omission_state,
)
from jiuwenswarm.server.runtime.reload_result import (
    ReloadAggregateResult,
    ReloadResult,
    log_agent_config_hot_reload,
    log_agent_config_hot_reload_replay,
    log_reload_config_changes,
)
from jiuwenswarm.agents.harness.common.rails.skill_credential_injection_rail import (
    coalesce_config_skill_envs,
)

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


logger = logging.getLogger(__name__)


ACP_DEFAULT_CAPABILITIES: dict[str, Any] = build_acp_initialize_result()

# Disk control-plane RPCs: list/rollback archives via EvolutionStore only.
# Must not call create_instance() (which constructs llm clients).
_DISK_ONLY_EVOLUTION_METHODS: frozenset[str] = frozenset(
    {
        "skills.evolution.archives",
        "skills.evolution.rollback",
    }
)


def _normalize_channel_id(channel_id: str | None) -> str:
    return str(channel_id or "default").strip() or "default"


def _session_id_prefix_for_channel(channel_id: str | None) -> str:
    """Path-safe session id prefix; may differ from logical ``channel_id``.

    Cron scheduler uses channel ``__cron__`` for routing, but ids like
    ``__cron___{ts}_{uuid}`` fail ``sanitize_session_id`` and cannot be
    used as sessions directory names.
    """
    channel_key = _normalize_channel_id(channel_id)
    if channel_key == "__cron__":
        return "cron"
    return channel_key


def _normalize_mode(mode: str | None) -> str:
    return str(mode or "agent").strip() or "agent"


def _normalize_sub_mode(sub_mode: str | None) -> str:
    return str(sub_mode or "").strip()


def _normalize_project_dir(project_dir: str | None) -> str:
    raw = str(project_dir or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
    except Exception:
        return raw


def _make_agent_cache_key(mode: str | None, sub_mode: str | None, project_dir: str | None) -> str:
    mode_key = _normalize_mode(mode)
    sub_mode_key = _normalize_sub_mode(sub_mode)
    project_key = _normalize_project_dir(project_dir)
    return f"{mode_key}:{sub_mode_key}:{project_key}"


def _build_acp_agent_config(extra_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the dedicated ACP agent profile config.

    ACP sessions should use ACP-native filesystem/terminal tools instead of the
    default openjiuwen filesystem/bash toolchain.
    """
    config: dict[str, Any] = {
        "agent_name": "acp_agent",
        "channel_id": "acp",
        "tool_profile": "acp",
        "enable_filesystem_rail": True,
    }
    if isinstance(extra_config, dict):
        config.update(extra_config)
    config["channel_id"] = "acp"
    config["tool_profile"] = "acp"
    return config


class AgentManager:
    """管理多个 Agent 实例.

    支持多种通道:
    - "acp": ACP 协议通道
    - "default": 默认通道
    """

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        service_id: str | None = None,
        user_workspace_dir: Any | None = None,
        config_base: Any | None = None,
        env_overrides: dict[str, Any] | None = None,
        last_reload_trace_id: str | None = None,
        env_agent_id: str | None = None,
        env_service_id: str | None = None,
        workspace_key: str | None = None,
    ) -> None:
        self.agents: dict[str, dict[str, "JiuWenSwarm"]] = {}
        # 记录每个 (channel_id, mode) 的创建参数, 便于 recreate_agent 立刻重建
        self._agent_create_params: dict[str, dict[str, dict[str, Any]]] = {}
        self._client_capabilities_by_channel: dict[str, dict[str, Any]] = {}
        self._latest_env_overrides: dict[str, Any] = dict(env_overrides) if env_overrides else {}
        self._latest_env_snapshot: dict[str, Any] | None = None
        if isinstance(config_base, dict):
            self._latest_config_base: dict[str, Any] | None = dict(config_base)
        else:
            self._latest_config_base = config_base
        self._last_reload_trace_id: str | None = last_reload_trace_id
        # 租户环境命名空间标识（用于 env tip 袋的 (service_id, agent_id) 键）
        self.agent_id = agent_id or "default"
        self.service_id = service_id or "default"
        self._env_agent_id: str = env_agent_id or agent_id or "default"
        self._env_service_id: str = env_service_id or service_id or "default"
        self._workspace_key: str = (workspace_key or "").strip() or "default"
        self._user_workspace_dir = user_workspace_dir
        self._skill_manager = None
        self._skill_manager_lock = asyncio.Lock()
        if env_overrides is not None and isinstance(env_overrides, dict):
            omission_removals = infer_multimodal_env_removals(
                None,
                env_overrides,
                active_env=None,
                service_id=self._env_service_id,
                agent_id=self._env_agent_id,
            )
            if omission_removals:
                apply_env_removals(
                    omission_removals,
                    service_id=self._env_service_id,
                    agent_id=self._env_agent_id,
                )
            sync_multimodal_env_omission_state(
                omission_removals,
                env_overrides,
                service_id=self._env_service_id,
                agent_id=self._env_agent_id,
            )
            self._latest_env_overrides = seal_env_mapping(
                merge_reload_env_snapshot(None, env_overrides)
            )
            apply_env_overrides_to_active(
                env_overrides,
                service_id=self._env_service_id,
                agent_id=self._env_agent_id,
            )
        # reload 串行锁: 防止并发 reload 叠加导致内存爆炸
        self._reload_lock: asyncio.Lock = asyncio.Lock()
        self._last_reload_fingerprint: str | None = None
        self._latest_effective_config: dict[str, Any] | None = None
        # A cached root may be returned before its first session processor or
        # child adapter exists. Track the request task that borrowed it so
        # disconnect cleanup cannot tear it down in that gap.
        self._agent_borrowers: dict[int, set[asyncio.Task]] = {}
        self._agent_pins: dict[int, int] = {}
        self._pending_tui_retirements: set[int] = set()
        self._retirement_tasks: dict[int, asyncio.Task] = {}
        self._agent_create_locks: WeakValueDictionary[
            tuple[str, str], asyncio.Lock
        ] = WeakValueDictionary()
        # 上一次默认模型的连接身份快照 (diff_key -> ModelClientConfig), 用于
        # 模型热更新后关闭"已被删除/改掉凭证"的 LLM 连接 (增量关闭)。
        self._last_model_conn_configs: dict[tuple, Any] = {}
        self._session_create_tokens: dict[tuple[str, str], tuple[Any, Any]] = {}
        self._session_create_token_lock = asyncio.Lock()
        from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool

        self.warm_pool = AgentWarmPool(self)
        if self._user_workspace_dir is not None and is_enterprise():
            logger.info(
                "[AgentManager] enterprise init: agent_id=%s service_id=%s "
                "workspace_key=%s user_workspace=%s",
                self.agent_id,
                self.service_id,
                self._workspace_key,
                self._user_workspace_dir,
            )

    @property
    def env_agent_id(self) -> str:
        """Return the env namespace agent_id for this manager."""
        return self._env_agent_id

    @property
    def env_service_id(self) -> str:
        """Return the env namespace service_id for this manager."""
        return self._env_service_id

    def _get_agent_create_lock(
        self,
        channel_key: str,
        cache_key: str,
    ) -> asyncio.Lock:
        lock_key = (channel_key, cache_key)
        create_lock = self._agent_create_locks.get(lock_key)
        if create_lock is None:
            create_lock = asyncio.Lock()
            self._agent_create_locks[lock_key] = create_lock
        return create_lock

    async def _get_or_create_skill_manager(self):
        """Return the one SkillManager owned by this tenant workspace."""
        if self._skill_manager is not None:
            return self._skill_manager
        async with self._skill_manager_lock:
            if self._skill_manager is not None:
                return self._skill_manager
            from pathlib import Path

            from jiuwenswarm.common.utils import (
                collapse_nested_agent_workspace_dir,
                get_agent_workspace_dir,
                get_agent_workspace_relative_dir,
            )
            from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
            from jiuwenswarm.server.runtime.skill.workspace_provider import (
                SkillWorkspaceProvider,
                SkillWorkspaceUnavailable,
            )

            if self._user_workspace_dir is not None:
                workspace_dir = collapse_nested_agent_workspace_dir(
                    Path(self._user_workspace_dir) / get_agent_workspace_relative_dir()
                )
            elif is_enterprise():
                raise SkillWorkspaceUnavailable(
                    "enterprise tenant workspace was not resolved"
                )
            else:
                workspace_dir = collapse_nested_agent_workspace_dir(
                    get_agent_workspace_dir()
                )
            self._skill_manager, _created = SkillWorkspaceProvider().get_or_create_manager(
                workspace_dir,
                require_valid_state=is_enterprise(),
                factory=lambda ready: SkillManager(
                    workspace_dir=str(ready.workspace_dir),
                    service_id=self.service_id,
                    agent_id=self.agent_id,
                ),
            )
            return self._skill_manager

    def _borrow_agent(self, agent: "JiuWenSwarm") -> "JiuWenSwarm":
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            return agent
        agent_id = id(agent)
        borrowers = self._agent_borrowers.setdefault(agent_id, set())
        if task in borrowers:
            return agent
        borrowers.add(task)
        task.add_done_callback(
            lambda completed, aid=agent_id: self._release_agent_borrower(
                aid, completed
            )
        )
        return agent

    def _release_agent_borrower(
        self,
        agent_id: int,
        task: asyncio.Task,
    ) -> None:
        borrowers = self._agent_borrowers.get(agent_id)
        if borrowers is None:
            return
        borrowers.discard(task)
        if borrowers:
            return
        self._agent_borrowers.pop(agent_id, None)
        self._schedule_pending_tui_retirement(agent_id)

    def pin_agent(self, agent: "JiuWenSwarm") -> None:
        """Keep a cached agent alive for a persistent background owner."""
        agent_id = id(agent)
        self._agent_pins[agent_id] = self._agent_pins.get(agent_id, 0) + 1

    def unpin_agent(self, agent: "JiuWenSwarm") -> None:
        """Release one persistent background ownership reference."""
        agent_id = id(agent)
        remaining = self._agent_pins.get(agent_id, 0) - 1
        if remaining > 0:
            self._agent_pins[agent_id] = remaining
            return
        self._agent_pins.pop(agent_id, None)
        self._schedule_pending_tui_retirement(agent_id)

    def _has_agent_borrowers(
        self,
        agent: "JiuWenSwarm",
        *,
        exclude: asyncio.Task | None = None,
    ) -> bool:
        agent_id = id(agent)
        borrowers = self._agent_borrowers.get(agent_id)
        if not borrowers:
            return False
        live = {task for task in borrowers if not task.done()}
        if live:
            self._agent_borrowers[agent_id] = live
        else:
            self._agent_borrowers.pop(agent_id, None)
        return any(task is not exclude for task in live)

    def _schedule_pending_tui_retirement(self, agent_id: int) -> None:
        if agent_id not in self._pending_tui_retirements:
            return
        if agent_id in self._agent_pins or self._agent_borrowers.get(agent_id):
            return
        existing = self._retirement_tasks.get(agent_id)
        if existing is not None and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._retire_pending_tui_agent(agent_id)
            )
        except RuntimeError:
            return
        self._retirement_tasks[agent_id] = task
        task.add_done_callback(
            lambda completed, aid=agent_id: self._finish_retirement_task(
                aid, completed
            )
        )

    def _finish_retirement_task(
        self,
        agent_id: int,
        task: asyncio.Task,
    ) -> None:
        if self._retirement_tasks.get(agent_id) is task:
            self._retirement_tasks.pop(agent_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "[AgentManager] deferred TUI root retirement failed: agent_id=%s",
                agent_id,
            )

    async def _retire_pending_tui_agent(self, agent_id: int) -> None:
        channel_agents = self.agents.get("tui")
        if not isinstance(channel_agents, dict):
            self._pending_tui_retirements.discard(agent_id)
            return
        for cache_key, agent in list(channel_agents.items()):
            if id(agent) != agent_id:
                continue
            await self._retire_tui_agent_if_idle(
                cache_key,
                agent,
                channel_agents,
            )
            return
        self._pending_tui_retirements.discard(agent_id)

    async def _retire_tui_agent_if_idle(
        self,
        cache_key: str,
        agent: "JiuWenSwarm",
        channel_agents: dict[str, "JiuWenSwarm"],
        *,
        exclude_borrower: asyncio.Task | None = None,
    ) -> bool:
        create_lock = self._get_agent_create_lock("tui", cache_key)
        async with create_lock:
            return await self._retire_tui_agent_if_idle_locked(
                cache_key,
                agent,
                channel_agents,
                exclude_borrower=exclude_borrower,
            )

    async def _retire_tui_agent_if_idle_locked(
        self,
        cache_key: str,
        agent: "JiuWenSwarm",
        channel_agents: dict[str, "JiuWenSwarm"],
        *,
        exclude_borrower: asyncio.Task | None = None,
    ) -> bool:
        agent_id = id(agent)
        if (
            self._agent_pins.get(agent_id, 0) > 0
            or self._has_agent_borrowers(agent, exclude=exclude_borrower)
        ):
            self._pending_tui_retirements.add(agent_id)
            return False

        has_runtime = getattr(agent, "has_session_runtime", None)
        if not callable(has_runtime):
            return False
        try:
            if bool(has_runtime()):
                self._pending_tui_retirements.discard(agent_id)
                return False
        except Exception:
            logger.exception(
                "[AgentManager] has_session_runtime failed: cache_key=%s",
                cache_key,
            )
            raise
        if channel_agents.get(cache_key) is not agent:
            self._pending_tui_retirements.discard(agent_id)
            return False

        # Detach before awaiting cleanup so a new request creates a fresh
        # root rather than receiving one that is being torn down.
        channel_agents.pop(cache_key, None)
        channel_params = self._agent_create_params.get("tui")
        create_params = None
        if isinstance(channel_params, dict):
            create_params = channel_params.pop(cache_key, None)
        self._pending_tui_retirements.discard(agent_id)
        try:
            await agent.cleanup()
        except Exception:
            logger.exception(
                "[AgentManager] idle TUI root agent cleanup failed: cache_key=%s",
                cache_key,
            )
            restored_agents = self.agents.setdefault("tui", channel_agents)
            if cache_key not in restored_agents:
                restored_agents[cache_key] = agent
                if create_params is not None:
                    self._agent_create_params.setdefault("tui", {})[
                        cache_key
                    ] = create_params
            raise
        else:
            logger.info(
                "[AgentManager] idle TUI root agent removed: cache_key=%s",
                cache_key,
            )
        if not channel_agents and self.agents.get("tui") is channel_agents:
            self.agents.pop("tui", None)
        if isinstance(channel_params, dict) and not channel_params:
            self._agent_create_params.pop("tui", None)
        return True


    @staticmethod
    def _reload_fingerprint(
        config: Any,
        env: Any,
        *,
        agent_topology: Any,
        target_channel_id: str | None,
        target_session_id: str | None,
        reload_scopes: list[str] | None = None,
    ) -> str:
        payload = {
            "config": config,
            "env": env if isinstance(env, dict) else {},
            "agent_topology": agent_topology,
            "target_channel_id": str(target_channel_id or "").strip() or None,
            "target_session_id": str(target_session_id or "").strip() or None,
            "reload_scopes": reload_scopes if reload_scopes is not None else [],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=repr)

    def _reload_agent_topology(self, target_channel_id: str | None = None) -> dict[str, list[tuple[str, int]]]:
        channel_items = (
            [(target_channel_id, self.agents.get(target_channel_id, {}))]
            if target_channel_id
            else self.agents.items()
        )
        topology: dict[str, list[tuple[str, int]]] = {}
        for channel_id, agents in channel_items:
            if not isinstance(agents, dict):
                topology[str(channel_id)] = []
                continue
            topology[str(channel_id)] = sorted((str(agent_key), id(agent)) for agent_key, agent in agents.items())
        return topology

    def iter_jiuwenswarm_instances(self) -> list["JiuWenSwarm"]:
        """Return initialized agents from the current two-level cache."""
        instances: list["JiuWenSwarm"] = []
        for channel_agents in self.agents.values():
            if not isinstance(channel_agents, dict):
                continue
            instances.extend(
                agent for agent in channel_agents.values() if agent is not None
            )
        return instances

    async def _create_agent(
        self,
        agent_key: str,
        mode: str = "agent",
        config: dict[str, Any] | None = None,
        sub_mode: str = None,
        cache_key: str | None = None,
    ) -> "JiuWenSwarm":
        """创建 Agent 实例.

        Args:
            agent_key: Agent 键（如 "acp" 或 "default"）
            config: 可选配置
            sub_mode: 子模式
        Returns:
            JiuWenSwarm 实例
        """
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        overlay_token = None
        try:
            if self._latest_env_overrides:
                effective_overlay = build_effective_env_overlay(self._latest_env_overrides)
                overlay_token = bind_task_env_overlay(effective_overlay)
            channel_key = _normalize_channel_id(agent_key)
            mode_key = _normalize_mode(mode)
            sub_mode_key = _normalize_sub_mode(sub_mode)
            project_dir = _normalize_project_dir((config or {}).get("project_dir"))
            if project_dir:
                config = dict(config or {})
                config["project_dir"] = project_dir
            agent_cache_key = cache_key or _make_agent_cache_key(mode_key, sub_mode_key, project_dir)
            logger.info(
                "[AgentManager] Creating %s agent (mode=%s, sub_mode=%s, project_dir=%s)",
                channel_key,
                mode_key,
                sub_mode_key or None,
                project_dir or None,
            )
            agent = JiuWenSwarm(
                user_workspace_dir=str(self._user_workspace_dir)
                if self._user_workspace_dir is not None
                else None,
                agent_id=self.agent_id,
                service_id=self.service_id,
                skill_manager=await self._get_or_create_skill_manager(),
            )
            setattr(agent, "_env_agent_id", self._env_agent_id)
            setattr(agent, "_env_service_id", self._env_service_id)
            setattr(agent, "_workspace_key", self._workspace_key)
            if self._user_workspace_dir is not None:
                setattr(agent, "_user_workspace_dir", self._user_workspace_dir)
            await agent.create_instance(
                config,
                mode=mode_key,
                sub_mode=sub_mode_key or None,
                config_base=self._latest_config_base,
            )
            setattr(agent, "_jiuwenswarm_agent_cache_key", agent_cache_key)
            setattr(agent, "_jiuwenswarm_agent_mode", mode_key)
            setattr(agent, "_jiuwenswarm_agent_sub_mode", sub_mode_key)
            setattr(agent, "_jiuwenswarm_agent_project_dir", project_dir)
            self.agents.setdefault(channel_key, {})[agent_cache_key] = agent
            # 记录创建参数, recreate_agent() 时可原样复用
            self._agent_create_params.setdefault(channel_key, {})[agent_cache_key] = {
                "mode": mode_key,
                "sub_mode": sub_mode_key or None,
                "config": dict(config or {}),
                "cache_key": agent_cache_key,
            }
            logger.info("[AgentManager] %s agent created cache_key=%s", channel_key, agent_cache_key)
            return agent
        finally:
            if overlay_token is not None:
                reset_task_env_overlay(overlay_token)

    async def initialize(
        self, channel_id: str = "", extra_config: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """初始化 AgentManager.

        对于 ACP 通道，创建 agent 并返回 capabilities。

        Args:
            channel_id: 通道 ID
            extra_config: 额外配置（如 protocol_version, client_capabilities）

        Returns:
            对于 ACP 通道，返回 capabilities；对于其他通道，返回 None
        """
        channel_key = _normalize_channel_id(channel_id)
        if channel_key == "acp":
            logger.info("[AgentManager] ACP initialize")
            if extra_config:
                client_capabilities = extra_config.get("client_capabilities")
                if isinstance(client_capabilities, dict):
                    self._client_capabilities_by_channel["acp"] = dict(client_capabilities)

            if "acp" in self.agents:
                logger.info("[AgentManager] Resetting ACP agent")
                for agent in self.agents.get("acp", {}).values():
                    if hasattr(agent, "cleanup"):
                        try:
                            await agent.cleanup()
                        except Exception as e:
                            logger.warning("[AgentManager] ACP agent cleanup failed: %s", e)
                del self.agents["acp"]

            config = _build_acp_agent_config(extra_config)
            await self._create_agent("acp", "code", config)

            return ACP_DEFAULT_CAPABILITIES.copy()
        return None

    async def cancel_all_inflight_work(self, reason: str = "[gateway ws disconnect] ") -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时：取消所有已创建 Agent 实例上的在途任务。"""
        for modes in list(self.agents.values()):
            for agent in list(modes.values()):
                try:
                    await agent.cancel_inflight_work(reason)
                except Exception:
                    logger.exception("[AgentManager] cancel_inflight_work failed")

    async def cleanup_session_runtime(self, *, channel_id: str = "", session_id: str) -> bool:
        """Release in-memory runtime for one session across existing channel agents.

        Iterates every cached agent on the channel and calls its
        ``cleanup_session_runtime(session_id)``. For ``tui`` channel agents that
        become idle after cleanup (no pins, no borrowers, no remaining session
        runtime), the cached root agent itself is retired so short-lived TUI
        processes do not accumulate one root per project.

        Returns:
            ``True`` if at least one agent reported cleanup, ``False`` when the
            channel has no matching agents or none exposes a cleanup hook.

        Raises:
            RuntimeError: if one or more agents failed to clean up -- an agent's
                ``cleanup_session_runtime`` raised, the post-cleanup
                ``has_session_runtime`` check raised, or runtime was still
                retained after cleanup. Failures are aggregated across all
                agents and raised once after the loop; partial successes are not
                rolled back. Callers needing best-effort semantics must wrap the
                call in try/except.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return False
        channel_key = _normalize_channel_id(channel_id)
        channel_agents = self.agents.get(channel_key, {})
        if not isinstance(channel_agents, dict):
            return False

        cleaned = False
        failed_agents = 0
        for cache_key, agent in list(channel_agents.items()):
            cleanup_fn = getattr(agent, "cleanup_session_runtime", None)
            if not callable(cleanup_fn):
                continue
            try:
                session_cleaned = bool(await cleanup_fn(sid))
                cleaned = session_cleaned or cleaned
            except Exception:
                failed_agents += 1
                logger.exception(
                    "[AgentManager] cleanup_session_runtime failed: channel_id=%s session_id=%s",
                    channel_key,
                    sid,
                )
                continue

            has_runtime = getattr(agent, "has_session_runtime", None)
            try:
                session_retained = bool(has_runtime(sid)) if callable(has_runtime) else False
            except Exception:
                failed_agents += 1
                logger.exception(
                    "[AgentManager] session runtime state check failed: "
                    "channel_id=%s session_id=%s",
                    channel_key,
                    sid,
                )
                continue
            if session_retained:
                failed_agents += 1
                logger.warning(
                    "[AgentManager] session runtime remains after cleanup: "
                    "channel_id=%s session_id=%s cache_key=%s",
                    channel_key,
                    sid,
                    cache_key,
                )
                continue

            if channel_key != "tui":
                continue
            try:
                await self._retire_tui_agent_if_idle(
                    cache_key,
                    agent,
                    channel_agents,
                    exclude_borrower=asyncio.current_task(),
                )
            except Exception:
                failed_agents += 1
                continue

        if not channel_agents and self.agents.get(channel_key) is channel_agents:
            self.agents.pop(channel_key, None)
        channel_params = self._agent_create_params.get(channel_key)
        if isinstance(channel_params, dict) and not channel_params:
            self._agent_create_params.pop(channel_key, None)
        if failed_agents:
            raise RuntimeError(
                "cleanup_session_runtime failed for "
                f"{failed_agents} agent(s): channel_id={channel_key} "
                f"session_id={sid}"
            )
        return cleaned

    def get_client_capabilities(self, channel_id: str = "") -> dict[str, Any]:
        channel_key = str(channel_id or "").strip()
        caps = self._client_capabilities_by_channel.get(channel_key)
        return dict(caps) if isinstance(caps, dict) else {}

    async def create_session(self, channel_id: str = "", session_id: str | None = None) -> str:
        """创建会话.

        Args:
            channel_id: 通道 ID

        Returns:
            会话 ID
        """
        explicit_session_id = str(session_id or "").strip()
        if explicit_session_id:
            logger.info("[AgentManager] session ensured: channel_id=%s session_id=%s", channel_id, explicit_session_id)
            return explicit_session_id
        channel_key = _normalize_channel_id(channel_id)
        session_prefix = _session_id_prefix_for_channel(channel_id)
        session_id = (
            f"{session_prefix}_{int(time.time() * 1000):x}_"
            f"{uuid.uuid4().hex[:12]}"
        )
        logger.info(
            "[AgentManager] session id allocated: channel_id=%s session_id=%s",
            channel_key,
            session_id,
        )
        return session_id

    async def sync_prewarm_channels(
        self,
        enabled_channels: list[str],
        *,
        config: Any | None = None,
        env: Any = None,
    ) -> dict[str, int]:
        return await self.warm_pool.sync(
            enabled_channels,
            config=(
                config
                if config is not None
                else self._latest_effective_config or get_config()
            ),
            env=env if env is not None else self._latest_env_overrides,
        )

    async def claim_prewarmed_session(
        self,
        *,
        channel_id: str,
        project_id: str,
        project_dir: str | None,
        work_mode: str,
        is_swarm: bool,
        prewarm_eligible: bool = True,
        create_token: str | None = None,
    ):
        token = str(create_token or "").strip()
        key = self.warm_pool.make_key(
            channel_id=channel_id,
            project_id=project_id,
            project_dir=project_dir,
            work_mode=work_mode,
            is_swarm=is_swarm,
        )
        create_signature = (key, bool(prewarm_eligible))
        token_key = (key.channel_id, token)
        async with self._session_create_token_lock:
            if token:
                existing = self._session_create_tokens.get(token_key)
                if existing is not None:
                    existing_key, claim = existing
                    if existing_key != create_signature:
                        raise ValueError(
                            "create_token was already used with different session parameters"
                        )
                    return claim
            if prewarm_eligible and not is_swarm:
                claim = await self.warm_pool.claim(key)
            else:
                from jiuwenswarm.server.runtime.agent_warm_pool import WarmClaim

                claim = WarmClaim(
                    await self.create_session(channel_id=channel_id),
                    False,
                    "bypassed",
                )
            if token:
                self._session_create_tokens[token_key] = (create_signature, claim)
            return claim

    async def wait_for_session_prewarm(self, session_id: str | None) -> None:
        if session_id:
            await self.warm_pool.wait_for_session(session_id)

    async def begin_foreground_chat(self) -> None:
        await self.warm_pool.begin_foreground()

    async def end_foreground_chat(self) -> None:
        await self.warm_pool.end_foreground()

    def activate_session_prewarm(self, session_id: str | None) -> None:
        """Mark a claimed prewarm workspace as a normal persisted session."""
        if session_id:
            self.warm_pool.clear_marker(session_id)

    async def release_session_prewarm_claim(self, session_id: str | None) -> None:
        if session_id:
            await self.warm_pool.release_claim_pin(session_id)

    async def get_agent(
            self,
            channel_id: str = "",
            mode: str = "agent",
            project_dir: str = None,
            sub_mode: str = None,
            request: Any | None = None,
    ) -> "JiuWenSwarm | None":
        """获取 Agent 实例（自动创建）.

        如果 agent 不存在，会自动创建（仅用于非 ACP 场景）。

        Args:
            channel_id: 通道 ID
            mode: 每个模式对应的实例
            project_dir: user project dir (e.g. trusted_dirs[0])
            sub_mode: 子模式
            request: 可选 AgentRequest，企业版用于模型策略 routing 上下文

        Returns:
            JiuWenSwarm | None: Agent 实例
        """
        channel_key = _normalize_channel_id(channel_id)
        mode_key = _normalize_mode(mode)
        sub_mode_key = _normalize_sub_mode(sub_mode)
        project_key = _normalize_project_dir(project_dir)
        cache_key = _make_agent_cache_key(mode_key, sub_mode_key, project_key)
        channel_agents = self.agents.get(channel_key, {})
        if cache_key in channel_agents:
            return self._borrow_agent(channel_agents[cache_key])

        create_lock = self._get_agent_create_lock(channel_key, cache_key)
        async with create_lock:
            existing = self.agents.get(channel_key, {}).get(cache_key)
            if existing is not None:
                return self._borrow_agent(existing)

            config = {}
            if project_key:
                config["project_dir"] = project_key
            if channel_key == "acp":
                config = {
                    **config,
                    **_build_acp_agent_config()
                }
            # 企业版：创建 agent 时附带完整 request，供 create_instance 加载企业配置
            if request is not None and is_enterprise():
                config = {**config, "request": request}
            agent = await self._create_agent(
                channel_key,
                mode_key,
                config,
                sub_mode_key or None,
                cache_key=cache_key,
            )
            return self._borrow_agent(agent)

    def get_agent_nowait(
        self,
        channel_id: str = "",
        mode: str | None = None,
        project_dir: str | None = None,
        sub_mode: str | None = None,
    ) -> "JiuWenSwarm | None":
        """获取 Agent 实例（同步，不自动创建）.

        Args:
            channel_id: 通道 ID

        Returns:
            JiuWenSwarm | None: Agent 实例，如果不存在则返回 None
        """
        channel_key = _normalize_channel_id(channel_id)
        channel_agents = self.agents.get(channel_key, {})
        if not isinstance(channel_agents, dict):
            return None

        if mode is not None or project_dir is not None or sub_mode is not None:
            cache_key = _make_agent_cache_key(mode, sub_mode, project_dir)
            agent = channel_agents.get(cache_key)
            if agent is not None:
                return self._borrow_agent(agent)

        requested_mode = _normalize_mode(mode) if mode is not None else ""
        requested_sub_mode = _normalize_sub_mode(sub_mode) if sub_mode is not None else ""
        requested_project_dir = _normalize_project_dir(project_dir) if project_dir is not None else ""
        for agent in channel_agents.values():
            if requested_mode and getattr(agent, "_jiuwenswarm_agent_mode", "") != requested_mode:
                continue
            if requested_sub_mode and getattr(agent, "_jiuwenswarm_agent_sub_mode", "") != requested_sub_mode:
                continue
            if requested_project_dir and getattr(agent, "_jiuwenswarm_agent_project_dir", "") != requested_project_dir:
                continue
            return self._borrow_agent(agent)

        if mode is None and project_dir is None and sub_mode is None:
            for agent in channel_agents.values():
                if getattr(agent, "_jiuwenswarm_agent_mode", "") == "agent":
                    return self._borrow_agent(agent)
            agent = next(iter(channel_agents.values()), None)
            return self._borrow_agent(agent) if agent is not None else None
        return None

    async def broadcast_package_change_to_single_agents(
        self,
        package_id: str,
        config_path: str,
        operation: str,
        channel_id: str | None = None,
        skip_instance: Any | None = None,
    ) -> None:
        """Broadcast package change to single-agent (agent mode) instances only.

        This ensures deactivation affects all relevant agent instances, not just the current one.
        Does NOT affect team mode agents.

        Args:
            package_id: The package ID being activated/deactivated.
            config_path: Absolute path to harness_config.yaml.
            operation: "activate" or "deactivate".
            channel_id: Optional channel ID to limit broadcast scope.
            skip_instance: Optional agent instance to skip (already processed by caller).
        """
        # plan / fast 已合并为单一 agent；agent.fast / agent.plan 作为历史 token 仍兼容匹配。
        target_modes = {"agent", "agent.fast", "agent.plan"}

        for channel_key, channel_agents in self.agents.items():
            # Limit to specific channel if provided
            if channel_id and channel_key != _normalize_channel_id(channel_id):
                continue

            for cache_key, agent in channel_agents.items():
                # Parse mode from cache_key: "mode:sub_mode:project"
                mode = cache_key.split(":")[0] if ":" in cache_key else ""
                if mode not in target_modes:
                    continue  # Skip team and other modes

                instance = await agent.ensure_instance()
                if instance is None:
                    continue

                fanout = getattr(
                    agent,
                    "apply_package_change_to_session_adapters",
                    None,
                )
                if callable(fanout):
                    try:
                        await fanout(operation, config_path)
                    except Exception as exc:
                        logger.warning(
                            "[AgentManager] session-adapter fanout failed for "
                            "package %s on agent %s: %s",
                            package_id,
                            cache_key,
                            exc,
                        )

                # Skip the instance that was already processed by the caller
                if skip_instance is not None and instance is skip_instance:
                    logger.debug(
                        "[AgentManager] Skipping already processed agent %s for package %s",
                        cache_key,
                        package_id,
                    )
                    continue

                try:
                    if operation == "deactivate":
                        await instance.unload_harness_config(config_path)
                        logger.info(
                            "[AgentManager] Unloaded package %s from agent %s (channel=%s)",
                            package_id,
                            cache_key,
                            channel_key,
                        )
                    elif operation == "activate":
                        await instance.load_harness_config(config_path)
                        logger.info(
                            "[AgentManager] Loaded package %s to agent %s (channel=%s)",
                            package_id,
                            cache_key,
                            channel_key,
                        )
                except Exception as exc:
                    logger.warning(
                        "[AgentManager] Failed to %s package %s on agent %s: %s",
                        operation,
                        package_id,
                        cache_key,
                        exc,
                    )

    def is_working(self) -> bool:
        """True when any managed agent reports in-flight work."""
        for agents in self.agents.values():
            if not isinstance(agents, dict):
                continue
            for agent in agents.values():
                checker = getattr(agent, "is_working", None)
                if callable(checker):
                    try:
                        if checker():
                            return True
                    except Exception:
                        logger.warning(
                            "[AgentManager] is_working checker failed; treating as not working",
                            exc_info=True,
                        )
        return False

    async def reload_agents_config(
        self,
        config,
        env,
        *,
        target_channel_id: str | None = None,
        target_session_id: str | None = None,
        reload_scopes: set[str] | None = None,
        reload_trace_id: str | None = None,
    ) -> ReloadAggregateResult:
        """reload agent config.

        使用 ``self._reload_lock`` 串行化, 避免高频触发(如批量 MCP 增删)时多个
        reload 并发叠加, 同时重建大量 agent 实例导致内存暴涨被 OOM kill.

        Env is staged into tip bags; ``promote_staged_env`` runs only when idle so
        in-flight requests keep reading the previous tip (or a sealed overlay).

        ``reload_scopes`` 含 ``"model"`` 时, 模型配置属于所有 channel 共享的
        全局配置段, 此时忽略 ``target_channel_id`` 的窄化, fan-out 到全部
        channel——否则 web 保存模型后只有 web 通道被热更新, IM 长连接通道
        (xiaoyi 等)的 session adapter 会继续用旧错误模型, 直到用户手动
        /new_session 才恢复.
        """
        aggregate = ReloadAggregateResult()
        async with self._reload_lock:
            if reload_trace_id:
                self._last_reload_trace_id = reload_trace_id
            self._latest_env_overrides = dict(env) if isinstance(env, dict) else {}

            # 推断多模态环境变量移除并应用
            previous_env = self._latest_env_snapshot
            multimodal_removals = infer_multimodal_env_removals(
                previous_env, self._latest_env_overrides,
            )
            if multimodal_removals:
                apply_env_removals(multimodal_removals)
            sync_multimodal_env_omission_state(
                multimodal_removals, self._latest_env_overrides,
            )

            # 更新 env snapshot
            self._latest_env_snapshot = merge_reload_env_snapshot(
                previous_env, self._latest_env_overrides,
            )

            # Stage tip only; promote when idle (in-flight tasks keep old tip).
            stage_env_overrides(self._latest_env_overrides)

            target_channel = str(target_channel_id or "").strip() or None
            target_session = str(target_session_id or "").strip() or None
            # model 是全局共享配置: 变更时必须广播到所有 channel, 否则非触发
            # 通道(如 IM 长连接 xiaoyi)的 agent 不会收到热更新, 旧模型残留。
            scope_set = set(reload_scopes) if reload_scopes else set()
            model_scope = "model" in scope_set
            effective_target_channel = None if model_scope else target_channel
            if target_channel and model_scope:
                logger.info(
                    "[AgentManager] model config changed via channel=%s; fan-out reload "
                    "to all channels (model is global)",
                    target_channel,
                )
            effective_config = config
            if effective_config is None:
                try:
                    effective_config = get_config()
                except Exception:
                    effective_config = None
            previous_config = self._latest_config_base
            if isinstance(effective_config, dict):
                effective_config = coalesce_config_skill_envs(effective_config, previous_config)
            fingerprint = self._reload_fingerprint(
                effective_config,
                self._latest_env_overrides,
                agent_topology=self._reload_agent_topology(effective_target_channel),
                target_channel_id=effective_target_channel,
                target_session_id=target_session,
                reload_scopes=sorted(scope_set) if scope_set else None,
            )
            if fingerprint == self._last_reload_fingerprint:
                logger.info(
                    "[AgentManager] reload agent config skipped: unchanged scope/config/env "
                    "(channel=%s session=%s)",
                    effective_target_channel or "*",
                    target_session or "*",
                )
                return aggregate
            channel_items = (
                [(effective_target_channel, self.agents.get(effective_target_channel, {}))]
                if effective_target_channel
                else list(self.agents.items())
            )
            reload_completed = True

            for channel_id, agents in channel_items:
                if not isinstance(agents, dict):
                    reload_completed = False
                    logger.warning(
                        "[AgentManager] unexpected agents entry for channel %s: %r",
                        channel_id,
                        type(agents),
                    )
                    continue
                for agent_key, agent in list(agents.items()):
                    reload_kwargs = {
                        "config_base": effective_config,
                        "env_overrides": self._latest_env_overrides,
                    }
                    if target_session:
                        reload_kwargs["target_session_id"] = target_session
                    try:
                        result = await agent.reload_agent_config(**reload_kwargs)
                        if isinstance(result, ReloadResult):
                            aggregate.merge(
                                result,
                                session_key=f"{channel_id}:{agent_key}",
                            )
                        else:
                            aggregate.applied += 1
                    except Exception as exc:
                        reload_completed = False
                        logger.exception(
                            "[AgentManager] reload_agent_config failed: channel=%s agent=%s",
                            channel_id,
                            agent_key,
                        )
                        aggregate.failed.append(
                            {
                                "session": f"{channel_id}:{agent_key}",
                                "error": str(exc),
                            }
                        )
                try:
                    team_config = effective_config if isinstance(effective_config, dict) else get_config()
                    await get_team_manager(channel_id).update_evolution_config(team_config)
                except Exception as exc:
                    reload_completed = False
                    logger.warning(
                        "[AgentManager] team evolution config hot-update failed: channel=%s error=%s",
                        channel_id,
                        exc,
                    )
                logger.info(f"channel {channel_id} reload agent config success.")
            idle = not self.is_working()
            if idle:
                promote_staged_env()
            else:
                logger.info(
                    "[AgentManager] promote_staged_env deferred (agents working); "
                    "trace=%s",
                    self._last_reload_trace_id or "unknown",
                )
            if reload_completed:
                self._last_reload_fingerprint = fingerprint
                if isinstance(effective_config, dict):
                    self._latest_effective_config = dict(effective_config)
                    self._latest_config_base = dict(effective_config)
                asyncio.create_task(
                    self.warm_pool.refresh(
                        config=effective_config,
                        env=self._latest_env_overrides,
                    ),
                    name="agent-prewarm-config-refresh",
                )
            # 模型配置变更时, 关闭已被删除/改掉凭证的旧 LLM 连接 (增量关闭)。
            if model_scope:
                await self._evict_stale_llm_clients(effective_config)
        return aggregate

    async def apply_sync_config(
        self,
        config: dict[str, Any],
        env: dict[str, Any],
    ) -> ReloadAggregateResult:
        """Apply sync_agents_configs write-through config/env to live adapters."""
        previous_config = self._latest_config_base
        config = coalesce_config_skill_envs(config, previous_config)
        self._latest_config_base = config
        self._latest_env_overrides = seal_env_mapping(env)
        replace_active_env(
            env,
            service_id=self.env_service_id,
            agent_id=self.env_agent_id,
            clear_staged=True,
        )

        aggregate = ReloadAggregateResult()
        for channel_id, channel_agents in self.agents.items():
            if not isinstance(channel_agents, dict):
                continue
            for cache_key, agent in channel_agents.items():
                session_key = f"{channel_id}:{cache_key}"
                try:
                    if agent.is_working():
                        ensure_fn = getattr(agent, "ensure_adapter", None)
                        adapter = ensure_fn() if callable(ensure_fn) else None
                        queue_fn = getattr(adapter, "queue_pending_reload", None) if adapter else None
                        if callable(queue_fn):
                            queue_fn(config, env)
                            aggregate.deferred += 1
                            continue
                    ensure_fn = getattr(agent, "ensure_adapter", None)
                    adapter = ensure_fn() if callable(ensure_fn) else None
                    reload_fn = getattr(adapter, "reload_agent_config", None) if adapter else None
                    if callable(reload_fn):
                        result = await reload_fn(
                            config,
                            env,
                            _force_apply=True,
                        )
                    else:
                        result = await agent.reload_agent_config(
                            config_base=config,
                            env_overrides=env,
                        )
                    if isinstance(result, ReloadResult):
                        aggregate.merge(result, session_key=session_key)
                    else:
                        aggregate.applied += 1
                except Exception as exc:
                    logger.exception(
                        "[AgentManager] apply_sync_config failed for adapter"
                    )
                    aggregate.failed.append({"session": session_key, "error": str(exc)})

        try:
            from jiuwenswarm.agents.harness.common.tools.browser_tools import (
                notify_browser_runtime_after_reload,
            )

            notify_browser_runtime_after_reload(
                idle=not self.is_working(),
                service_id=self.env_service_id,
                agent_id=self.env_agent_id,
            )
        except Exception:
            logger.exception(
                "[AgentManager] browser runtime sync-config notify failed"
            )
        return aggregate

    async def _evict_stale_llm_clients(self, effective_config: Any) -> None:
        """模型热更新后, 关闭"已从 models.defaults 删除/改掉凭证"的 LLM 连接。

        agent-core 的 HTTP client 缓存是进程级、被所有组件共享的, 因此这里只做
        **增量关闭**(上次默认集 - 本次默认集), 绝不碰其它组件仍在使用的连接。
        被删除/更新的模型即使有在途调用也会立即断开——用户既然不再使用它, 就不该
        让它继续偷偷消耗 token。被误伤的连接(若有)也会在下次调用时自愈重建。

        注意: 进程启动后的第一次模型变更, 因无历史快照, 只登记不关闭; 之后的变更
        才做真正的 diff 关闭。
        """
        try:
            from openjiuwen.core.foundation.llm import ModelClientConfig
            from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
                OpenAIModelClient,
            )
            from openjiuwen.core.foundation.llm.model_clients.anthropic_model_client import (
                AnthropicModelClient,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentManager] LLM client evict skipped (import failed): %s", exc)
            return

        def _diff_key(cfg: Any) -> tuple:
            return (str(cfg.client_provider), cfg.api_key, cfg.api_base, cfg.verify_ssl, cfg.ssl_cert)

        new_configs: dict[tuple, Any] = {}
        try:
            entries = get_default_models(effective_config if isinstance(effective_config, dict) else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentManager] build live model configs failed: %s", exc)
            return
        for entry in entries or []:
            mcc = (entry or {}).get("model_client_config") or {}
            mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
            if not mcc_fields.get("client_provider"):
                mcc_fields["client_provider"] = "OpenAI"
            try:
                cfg = ModelClientConfig(**mcc_fields)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentManager] skip invalid model config during evict: %s", exc)
                continue
            new_configs[_diff_key(cfg)] = cfg

        removed = [cfg for key, cfg in self._last_model_conn_configs.items() if key not in new_configs]
        self._last_model_conn_configs = new_configs
        if not removed:
            return
        for client_cls in (OpenAIModelClient, AnthropicModelClient):
            try:
                await client_cls.aclose_connections(removed)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentManager] %s.aclose_connections failed: %s", client_cls.__name__, exc)

    async def recreate_agent(self, channel_id: str, *, immediate: bool = True) -> None:
        """重建指定 channel 的所有 agent 实例.

        用于 ``/sandbox enable/disable`` 等需要重新构建 ``SysOperationCard`` 的场景.
        步骤:
        1. 备份现有 (mode -> create_params) 映射;
        2. cleanup 并删除现有 agent 实例;
        3. 若 ``immediate=True``, 依据备份的参数立即重新调用 ``_create_agent()``,
           使新的 SysOperation 生效不必等到下次 ``get_agent()``;
           ``immediate=False`` 则按原行为, 下次 ``get_agent()`` 时再重建.

        Args:
            channel_id: 通道 ID.
            immediate: 是否立即重建 (默认 True).
        """
        channel_key = channel_id or "default"
        agents = self.agents.get(channel_key)
        if not agents:
            logger.info(
                "[AgentManager] recreate_agent: no active agent on channel %s, skip",
                channel_key,
            )
            return

        # 1. 备份 (mode -> create_params)
        existing_modes = list(agents.keys())
        backup_params: dict[str, dict[str, Any]] = {}
        channel_params = self._agent_create_params.get(channel_key) or {}
        for mode_key in existing_modes:
            params = channel_params.get(mode_key)
            if params is None:
                # 未记录创建参数 (理论上 _create_agent 一定记录), 兜底使用 mode_key
                params = {"mode": mode_key, "sub_mode": None, "config": None}
            backup_params[mode_key] = dict(params)

        # 2. cleanup + 删除
        for mode_key, agent in list(agents.items()):
            if hasattr(agent, "cleanup"):
                try:
                    await agent.cleanup()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentManager] recreate cleanup failed (mode=%s): %s",
                        mode_key,
                        exc,
                    )
        del self.agents[channel_key]
        self._agent_create_params.pop(channel_key, None)
        logger.info(
            "[AgentManager] recreate_agent: channel %s agents dropped (modes=%s)",
            channel_key,
            existing_modes,
        )

        if not immediate:
            logger.info(
                "[AgentManager] recreate_agent: channel %s will rebuild on next get_agent()",
                channel_key,
            )

        # 3. 立即按原参数重建
        for mode_key, params in backup_params.items():
            try:
                await self._create_agent(
                    channel_key,
                    mode=params.get("mode") or mode_key,
                    config=params.get("config"),
                    sub_mode=params.get("sub_mode"),
                    cache_key=params.get("cache_key") or mode_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[AgentManager] recreate_agent: rebuild failed (mode=%s): %s",
                    mode_key,
                    exc,
                )
        logger.info(
            "[AgentManager] recreate_agent: channel %s rebuilt (modes=%s)",
            channel_key,
            existing_modes,
        )

    async def _process_disk_only_evolution(self, request: Any) -> Any:
        """Handle archives/rollback without create_instance / LLM client.

        Prefer a warm agent if one already exists; otherwise use an ephemeral
        JiuWenSwarm that only needs DeepAdapter + EvolutionStore.
        """
        from jiuwenswarm.server.runtime.agent_adapter.evolution_version import (
            disk_only_evolution_skill_dirs,
        )
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
        from jiuwenswarm.server.runtime.agent_adapter.session_skill_dirs import (
            bind_session_registered_skill_dirs,
            reset_session_registered_skill_dirs,
        )

        channel_id = getattr(request, "channel_id", "") or "default"
        params = getattr(request, "params", {}) if isinstance(getattr(request, "params", {}), dict) else {}
        mode_full = params.get("mode", "agent")
        mode = str(mode_full).split(".")[0] if mode_full else "agent"
        workspace_dir = params.get("workspace_dir")

        bound_skill_dirs = disk_only_evolution_skill_dirs(params)
        skill_dirs_token = None
        overlay_token = None
        try:
            if bound_skill_dirs:
                skill_dirs_token = bind_session_registered_skill_dirs(bound_skill_dirs)

            existing = self.get_agent_nowait(
                channel_id,
                mode,
                project_dir=workspace_dir,
            )
            if existing is not None:
                return await existing.process_message(request)

            agent = JiuWenSwarm(
                user_workspace_dir=str(self._user_workspace_dir)
                if self._user_workspace_dir
                else None,
                agent_id=self.agent_id,
                service_id=self.service_id,
                skill_manager=await self._get_or_create_skill_manager(),
            )
            setattr(agent, "_env_agent_id", self._env_agent_id)
            setattr(agent, "_env_service_id", self._env_service_id)
            setattr(agent, "_workspace_key", self._workspace_key)
            if self._latest_env_overrides:
                overlay = build_effective_env_overlay(self._latest_env_overrides)
                if overlay:
                    overlay_token = bind_task_env_overlay(overlay)
            logger.info(
                "[AgentManager] disk-only evolution RPC via ephemeral agent "
                "(skip create_instance): method=%s channel=%s",
                getattr(getattr(request, "req_method", None), "value", getattr(request, "req_method", None)),
                channel_id,
            )
            return await agent.process_message(request)
        finally:
            if overlay_token is not None:
                reset_task_env_overlay(overlay_token)
            if skill_dirs_token is not None:
                reset_session_registered_skill_dirs(skill_dirs_token)

    def _resolve_request_mode(self, request: Any, params: dict) -> str:
        """解析请求的 agent 模式.

        优先取 params["mode"]；interrupt resume 请求（权限审批/确认/ask_user）
        未携带 mode 时，从 session metadata 读取原始请求的 mode，避免从
        code.normal 降级为 agent，导致 adapter 实例不匹配、无法恢复原
        interaction；其余情况默认 "agent"。

        Args:
            request: AgentRequest 对象
            params: request.params 字典

        Returns:
            完整 mode 字符串（如 "code.normal"、"agent.plan"）
        """
        mode_full = params.get("mode")
        if mode_full:
            return str(mode_full)

        source = str(params.get("source") or "").strip()
        answers = params.get("answers")
        req_id = str(params.get("request_id") or "").strip()
        if (
            source in {"permission_interrupt", "confirm_interrupt", "ask_user_interrupt"}
            and isinstance(answers, list)
            and bool(req_id)
        ):
            sid = getattr(request, "session_id", "") or ""
            if sid:
                try:
                    from jiuwenswarm.common.utils import resolve_tenant_sessions_dir
                    from jiuwenswarm.server.runtime.session.session_metadata import (
                        get_session_metadata,
                    )
                    sessions_root = resolve_tenant_sessions_dir(
                        getattr(self, "_workspace_key", None) or "default",
                    )
                    meta = get_session_metadata(
                        sid, cache_bust=True, enable_writeback=False,
                        sessions_root=sessions_root,
                    )
                    stored_mode = str(meta.get("mode") or "").strip()
                    if stored_mode and stored_mode != "unknown":
                        # 回写 params，让下游 run_stream 等也能拿到正确 mode
                        params["mode"] = stored_mode
                        logger.info(
                            "[AgentManager] interrupt resume mode resolved"
                            " from session metadata: session_id=%s"
                            " mode=%s source=%s",
                            sid, stored_mode, source,
                        )
                        return stored_mode
                except Exception:
                    logger.debug(
                        "[AgentManager] resolve mode from session metadata failed",
                        exc_info=True,
                    )
        return "agent"

    def _agent_lookup_from_request(self, request: Any) -> tuple[str, str | None, str | None]:
        """Resolve get_agent keys from a chat request.

        Must match ``AgentWebSocketServer._prepare_code_mode_chat_turn`` so the
        tenant-pool path reuses the same cached instance after plan-mode sync.
        """
        from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

        params = getattr(request, "params", {}) if isinstance(getattr(request, "params", {}), dict) else {}
        mode_full = self._resolve_request_mode(request, params)
        mode, sub_mode, _canonical = resolve_agent_request_mode(mode_full)
        agent_mode = "agent" if mode == "auto_harness" else mode
        project_dir = (
            params.get("project_dir")
            or params.get("workspace_dir")
            or params.get("cwd")
        )
        if isinstance(project_dir, str):
            project_dir = project_dir.strip() or None
        else:
            project_dir = None
        source = str(params.get("source") or "").strip()
        is_interrupt_continuation = (
            source
            in {"permission_interrupt", "confirm_interrupt", "ask_user_interrupt"}
            and isinstance(params.get("answers"), list)
            and bool(str(params.get("request_id") or "").strip())
        )
        if project_dir is None and is_interrupt_continuation:
            sid = str(getattr(request, "session_id", "") or "").strip()
            if sid:
                try:
                    from jiuwenswarm.common.utils import resolve_tenant_sessions_dir
                    from jiuwenswarm.server.runtime.session.session_metadata import (
                        get_session_metadata,
                    )

                    sessions_root = resolve_tenant_sessions_dir(
                        getattr(self, "_workspace_key", None) or "default",
                    )
                    meta = get_session_metadata(
                        sid,
                        cache_bust=True,
                        enable_writeback=False,
                        sessions_root=sessions_root,
                    )
                    stored_project_dir = (
                        meta.get("project_dir") if isinstance(meta, dict) else None
                    )
                    if isinstance(stored_project_dir, str) and stored_project_dir.strip():
                        project_dir = stored_project_dir.strip()
                        params["project_dir"] = project_dir
                except Exception:
                    logger.debug(
                        "[AgentManager] resolve project_dir from session metadata failed",
                        exc_info=True,
                    )
        return agent_mode, sub_mode, project_dir

    async def process_message(self, request: Any) -> Any:
        """处理非流式请求.

        Args:
            request: AgentRequest 对象

        Returns:
            AgentResponse 对象
        """
        try:
            await self.wait_for_session_prewarm(getattr(request, "session_id", None))
            req_method = getattr(request, "req_method", None)
            req_method_value = getattr(req_method, "value", req_method)
            if isinstance(req_method_value, str) and req_method_value in _DISK_ONLY_EVOLUTION_METHODS:
                return await self._process_disk_only_evolution(request)

            channel_id = getattr(request, "channel_id", "")
            mode, sub_mode, project_dir = self._agent_lookup_from_request(request)

            agent = await self.get_agent(
                channel_id=channel_id,
                mode=mode,
                project_dir=project_dir,
                sub_mode=sub_mode,
            )
            if agent is None:
                raise RuntimeError(f"[AgentManager] No agent available for channel {channel_id}")

            return await agent.process_message(request)
        except Exception as e:
            logger.error(f"[AgentManager] Error in process_message: {e}", exc_info=True)
            raise

    async def process_message_stream(self, request: Any):
        """处理流式请求.

        Args:
            request: AgentRequest 对象

        Yields:
            AgentResponseChunk 对象
        """
        try:
            await self.wait_for_session_prewarm(getattr(request, "session_id", None))
            channel_id = getattr(request, "channel_id", "")
            mode, sub_mode, project_dir = self._agent_lookup_from_request(request)

            agent = await self.get_agent(
                channel_id=channel_id,
                mode=mode,
                project_dir=project_dir,
                sub_mode=sub_mode,
            )
            if agent is None:
                raise RuntimeError(f"[AgentManager] No agent available for channel {channel_id}")

            # 流式处理
            async for chunk in agent.process_message_stream(request):
                yield chunk
        except Exception as e:
            logger.error(f"[AgentManager] Error in process_message_stream: {e}", exc_info=True)
            raise

    async def cleanup(self) -> None:
        """清理所有 agent 实例."""
        await self.warm_pool.close()
        retirement_tasks = [
            task
            for task in self._retirement_tasks.values()
            if task is not asyncio.current_task() and not task.done()
        ]
        if retirement_tasks:
            await asyncio.gather(*retirement_tasks, return_exceptions=True)
        for key, agents in list(self.agents.items()):
            for agent in agents.values():
                if hasattr(agent, "cleanup"):
                    try:
                        await agent.cleanup()
                    except Exception as e:
                        logger.warning("[AgentManager] Agent cleanup failed: %s", e)
            del self.agents[key]
        self._agent_create_params.clear()
        self._client_capabilities_by_channel.clear()
        self._session_create_tokens.clear()
        self._agent_borrowers.clear()
        self._agent_pins.clear()
        self._pending_tui_retirements.clear()
        self._retirement_tasks.clear()
        self._agent_create_locks.clear()
        logger.info("[AgentManager] All agents cleaned up")
