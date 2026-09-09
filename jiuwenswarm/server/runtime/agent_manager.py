# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentManager - 管理 Agent 实例."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, NamedTuple, TYPE_CHECKING
from weakref import WeakValueDictionary

from jiuwenswarm.common.e2a.acp.protocol import build_acp_initialize_result
from jiuwenswarm.agents.harness.team import get_team_manager
from jiuwenswarm.common.config import get_config, get_default_models
from jiuwenswarm.common.mode_matrix import (
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    deprecate_mode,
)

if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


logger = logging.getLogger(__name__)


ACP_DEFAULT_CAPABILITIES: dict[str, Any] = build_acp_initialize_result()


def _normalize_channel_id(channel_id: str | None) -> str:
    return str(channel_id or "default").strip() or "default"


def _normalize_mode(mode: str | None) -> str:
    return str(mode or "agent").strip() or "agent"


def _normalize_sub_mode(sub_mode: str | None) -> str:
    return str(sub_mode or "").strip()


def _normalize_project_dir(project_dir: str | None) -> str:
    raw = str(project_dir or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw))).casefold()
    except Exception:
        return raw


# 单 agent 的 plan 是**会话运行期状态**（``DeepAgentState.plan_mode``），不是另一种
# agent 装配：plan 与非 plan 用的是同一套 rails 和工具，差别只在模型这一轮能看到
# 哪些工具。所以这里把 plan 子模式并回它的普通形态，用户开关 Plan 时命中同一个
# 实例——否则换实例就等于换掉 ``context_engine``，整段对话历史会凭空消失。
#
# 集群不在此列：``team.plan`` 解析出的子模式是 ``team``，本来就不带 plan。
_PLAN_SUB_MODE_ALIASES: dict[str, str] = {
    "agent": "",
    "code": "normal",
}


def collapse_plan_sub_mode(mode: str | None, sub_mode: str | None) -> str:
    """把单 agent 的 ``plan`` 子模式并回普通子模式。

    Args:
        mode: 归一化前后的 manager mode。
        sub_mode: 归一化前后的子模式。

    Returns:
        并轨后的子模式；非单 agent plan 时原样返回。
    """
    sub_mode_key = _normalize_sub_mode(sub_mode)
    if sub_mode_key != "plan":
        return sub_mode_key
    return _PLAN_SUB_MODE_ALIASES.get(_normalize_mode(mode), sub_mode_key)


def _make_agent_cache_key(mode: str | None, sub_mode: str | None, project_dir: str | None) -> str:
    mode_key = _normalize_mode(mode)
    sub_mode_key = collapse_plan_sub_mode(mode_key, sub_mode)
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

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, "JiuWenSwarm"]] = {}
        # Snapshot of the optional PersonalContext runtime switch.  New
        # cached agents inherit it before their first rail synchronization.
        self._personal_context_runtime_enabled: bool = False
        # 记录每个 (channel_id, mode) 的创建参数, 便于 recreate_agent 立刻重建
        self._agent_create_params: dict[str, dict[str, dict[str, Any]]] = {}
        self._client_capabilities_by_channel: dict[str, dict[str, Any]] = {}
        self._latest_env_overrides: dict[str, Any] = {}
        # reload 串行锁: 防止并发 reload 叠加导致内存爆炸
        self._reload_lock: asyncio.Lock = asyncio.Lock()
        self._last_reload_fingerprint: str | None = None
        self._latest_effective_config: dict[str, Any] | None = None
        # A cached root may be returned before its first session processor or
        # child adapter exists. Track the request task that borrowed it so
        # disconnect cleanup cannot tear it down in that gap.
        self._agent_borrowers: dict[int, set[asyncio.Task]] = {}
        self._agent_pins: dict[int, int] = {}
        self._heartbeat_service: Any | None = None
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

    def set_heartbeat_service(self, service: Any | None) -> None:
        """Inject the process-owned Heartbeat service into single and Team agents."""
        self._heartbeat_service = service
        from jiuwenswarm.agents.swarm.context import set_heartbeat_job_service

        set_heartbeat_job_service(service)
        for agents in self.agents.values():
            for agent in agents.values():
                agent.set_heartbeat_service(service)

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
        # state.json MCP enabled set (TUI global-default switch). config.yaml
        # changes alone don't cover MCP enable/disable / add / remove written
        # to state.json — without this in the fingerprint, those ops hit
        # fingerprint==last and reload is skipped, so the TUI agent never
        # picks up the change (tools don't load / unload).
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                list_tui_enabled_mcps,
            )
            mcp_enabled = sorted(
                str(r.get("name", "")) for r in list_tui_enabled_mcps()
            )
        except Exception:  # noqa: BLE001
            mcp_enabled = []
        payload = {
            "config": config,
            "env": env if isinstance(env, dict) else {},
            "agent_topology": agent_topology,
            "target_channel_id": str(target_channel_id or "").strip() or None,
            "target_session_id": str(target_session_id or "").strip() or None,
            "reload_scopes": reload_scopes if reload_scopes is not None else [],
            "mcp_enabled": mcp_enabled,
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

        for env_key, env_value in self._latest_env_overrides.items():
            key = str(env_key)
            if env_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(env_value)
        channel_key = _normalize_channel_id(agent_key)
        mode_key = _normalize_mode(mode)
        # 用并轨后的子模式装配实例，和缓存键保持同一套语义。
        sub_mode_key = collapse_plan_sub_mode(mode_key, sub_mode)
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
        agent = JiuWenSwarm()
        agent.set_heartbeat_service(self._heartbeat_service)
        setter = getattr(agent, "set_personal_context_runtime_enabled", None)
        if callable(setter):
            setter(self._personal_context_runtime_enabled)
        await agent.create_instance(config, mode=mode_key, sub_mode=sub_mode_key or None)
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

    async def set_personal_context_runtime_enabled(self, enabled: bool) -> None:
        """Broadcast the Host switch to already cached Agent facades.

        The manager deliberately iterates only existing wrappers.  Toggling
        PersonalContext must not create an Agent or otherwise affect normal
        AgentServer work when the feature is disabled.
        """

        self._personal_context_runtime_enabled = bool(enabled)
        for channel_agents in list(self.agents.values()):
            if not isinstance(channel_agents, dict):
                continue
            for agent in list(channel_agents.values()):
                cancelled: asyncio.CancelledError | None = None
                try:
                    setter = getattr(
                        agent, "set_personal_context_runtime_enabled", None
                    )
                    if callable(setter):
                        setter(self._personal_context_runtime_enabled)
                    refresher = getattr(agent, "refresh_personal_context_rail", None)
                    if callable(refresher):
                        await refresher()
                except asyncio.CancelledError as exc:
                    cancelled = exc
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentManager] PersonalContext Rail refresh failed: %s",
                        type(exc).__name__,
                    )
                if cancelled is not None:
                    raise cancelled

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

    async def cancel_all_inflight_work(
        self,
        reason: str = "[gateway ws disconnect] ",
        *,
        exclude_session_ids: set[str] | None = None,
    ) -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时：取消所有已创建 Agent 实例上的在途任务。"""
        for modes in list(self.agents.values()):
            for agent in list(modes.values()):
                try:
                    await agent.cancel_inflight_work(
                        reason,
                        exclude_session_ids=exclude_session_ids,
                    )
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

    async def release_subagent_runtime_for_session(
        self,
        *,
        channel_id: str | None,
        session_id: str,
        reason: str = "session_deleted",
    ) -> bool:
        """Release subagent control owned by the channel's existing Agent.

        Product Session deletion historically performed this lookup in
        AgentServer.  Keeping it here preserves the same first-Agent lookup and
        adapter selection while hiding Agent/Adapter internals behind the
        Runtime-owned manager boundary.
        """
        agent = self.get_agent_nowait(channel_id=channel_id or "")
        adapter = self._resolve_runtime_adapter(agent)
        release_runtime = getattr(
            adapter,
            "release_subagent_runtime_for_session",
            None,
        )
        if not callable(release_runtime):
            return False
        await release_runtime(session_id, reason=reason)
        return True

    @staticmethod
    def _resolve_runtime_adapter(agent: Any) -> Any:
        """Resolve the same adapter shape used by the legacy Server path."""
        if agent is None:
            return None
        for attr in ("_adapter", "adapter", "_active_adapter"):
            inner = getattr(agent, attr, None)
            if inner is not None and hasattr(inner, "apply_sandbox_runtime_patch"):
                return inner
        if hasattr(agent, "apply_sandbox_runtime_patch"):
            return agent
        return None

    async def apply_mcp_change(
        self, name: str, action: str, *, enabled: bool = True,
        target_channel_id: str | None = None,
    ) -> bool:
        """Phase-2: targeted single-MCP change, no full config reload.

        Fans out to every live agent instance (or just target_channel_id's
        agents if given) and asks its adapter to add/remove/toggle that one
        MCP — bypassing reload_agents_config's heavy resync of the entire
        mcp.servers list. The agent reads the merged get_mcp_servers() so a
        state.json write done just before this call is visible.

        Returns True if at least one adapter applied it. Raises RuntimeError
        when NO adapter applied it — so a failed register (e.g. the MCP
        server returned an error, or the SDK raised a cancel-scope error
        during add) surfaces to the caller instead of silently returning
        False and letting the connect handler report "connected". The first
        adapter's error reason is carried in the message. A single-adapter
        failure among several successes still returns True (no raise).
        """
        if target_channel_id:
            channels = [(target_channel_id, self.agents.get(target_channel_id, {}))]
        else:
            channels = list(self.agents.items())
        applied_any = False
        first_error: str = ""
        for channel_key, channel_agents in channels:
            if not isinstance(channel_agents, dict):
                continue
            for _cache_key, agent in list(channel_agents.items()):
                try:
                    ok = await agent.apply_mcp_change(name, action, enabled=enabled)
                    if ok:
                        applied_any = True
                    elif not first_error:
                        first_error = (
                            f"adapter on {channel_key} returned ok=False for "
                            f"'{name}'/{action} (register/unregister rejected)"
                        )
                except Exception as exc:  # noqa: BLE001
                    # register_mcp_by_name surfaces plain Exceptions only
                    # (openjiuwen add_tool_server coerces cancel-scope errors to
                    # WorkflowError). Collect the first failure so the caller
                    # can report it; raise if none succeeded.
                    logger.warning(
                        "[AgentManager] apply_mcp_change '%s'/%s on %s failed: %s",
                        name, action, channel_key, exc,
                    )
                    if not first_error:
                        first_error = str(exc) or repr(exc)
        if not applied_any:
            # No adapter succeeded. For "remove" on a skill-only / pure-CLI MCP
            # (no server entry — get_mcp_server_config returns None), there was
            # never a server to unregister, so ok=False from every adapter is
            # the expected no-op, not a failure. Treating it as a failure would
            # make disconnect raise "register/unregister rejected" for MCPs
            # that legitimately have no MCP server. register_mcp_by_name mirrors
            # this: it returns True (no-op) when the entry is None.
            if action in ("remove", "toggle"):
                from jiuwenswarm.common.config import get_mcp_server_config
                if get_mcp_server_config(name) is None:
                    logger.debug(
                        "[AgentManager] apply_mcp_change '%s'/%s: no server entry "
                        "(skill-only / pure-CLI); no-op success",
                        name, action,
                    )
                    return True
            # No adapter succeeded — surface the failure so the connect/
            # disconnect handler reports failure to the frontend instead of
            # a stale "connected"/"disconnected".
            raise RuntimeError(first_error or f"MCP '{name}' {action} failed")
        return applied_any

    async def probe_mcp_live_connection(self, name: str) -> tuple[bool, str]:
        """Live-connect probe for one MCP (connect-time preflight).

        Thin entry point for the connect handler; the probe logic lives in
        ``mcp_config.probe_mcp_live_connection``. That function talks to the
        process-level ``Runner.resource_mgr`` directly — no adapter instance
        needed — so cold-start (no conversation yet) still validates the MCP
        and caches the spawned stdio subprocess / HTTP connection for the
        first chat turn's reconcile to reuse (no duplicate spawn).
        """
        from jiuwenswarm.common.mcp_config import probe_mcp_live_connection as _probe
        return await _probe(name)

    def sync_mcp_credentials(self) -> None:
        """Sync connected MCPs' tokens into os.environ.

        os.environ is process-global, so syncing on any one live agent covers
        the whole process. Stops on the first agent that actually syncs
        (returns True); if an agent has no adapter yet (cold-start race) or
        throws, falls through to the next live agent instead of giving up
        after the first. No-op if no agent can sync (the cold-start path
        syncs inside _build_configured_subagents instead).
        """
        for channel_agents in self.agents.values():
            if not isinstance(channel_agents, dict):
                continue
            for _cache_key, agent in channel_agents.items():
                try:
                    if agent.sync_mcp_credentials():
                        return  # synced — env is process-global
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[AgentManager] sync_mcp_credentials on %s failed: %s",
                        _cache_key, exc,
                    )
                    continue  # try the next live agent

    def clear_mcp_credentials(self, name: str) -> None:
        """Clear a disconnected MCP's token env vars from os.environ.

        Stops on the first agent that clears (env is process-global); if an
        agent has no adapter yet or throws, falls through to the next.
        """
        for channel_agents in self.agents.values():
            if not isinstance(channel_agents, dict):
                continue
            for _cache_key, agent in channel_agents.items():
                try:
                    if agent.clear_mcp_credentials(name):
                        return  # cleared — env is process-global
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[AgentManager] clear_mcp_credentials '%s' on %s failed: %s",
                        name, _cache_key, exc,
                    )
                    continue  # try the next live agent

    async def refresh_skill_rails(self) -> None:
        """Reload every live agent's SkillUseRail so MCP bundled skills
        (installed/uninstalled by skill_installer) surface without a full
        reload_agents_config. Fans out to all live agents; each agent's adapter
        reloads its parent + session child skill rails.
        """
        for channel_key, channel_agents in self.agents.items():
            if not isinstance(channel_agents, dict):
                continue
            for _cache_key, agent in list(channel_agents.items()):
                try:
                    await agent.refresh_skill_rails()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentManager] refresh_skill_rails on %s/%s failed: %s",
                        channel_key, _cache_key, exc,
                    )

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
        session_id = (
            f"{channel_key}_{int(time.time() * 1000):x}_"
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
        persist_session: bool = False,
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
        # persist_session 不属于 WarmKey：同一预热 Agent 可服务开启或关闭的
        # Session，避免为布尔开关复制预热槽。但它属于 session.create 的幂等
        # 身份，同一 create_token 不允许用不同值重试。
        create_signature = (key, bool(prewarm_eligible), bool(persist_session))
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
            sub_mode: str = None
    ) -> "JiuWenSwarm | None":
        """获取 Agent 实例（自动创建）.

        如果 agent 不存在，会自动创建（仅用于非 ACP 场景）。

        Args:
            channel_id: 通道 ID
            mode: 每个模式对应的实例
            project_dir: user project dir (e.g. trusted_dirs[0])
            sub_mode: 子模式

        Returns:
            JiuWenSwarm | None: Agent 实例
        """
        channel_key = _normalize_channel_id(channel_id)
        mode_key = _normalize_mode(mode)
        sub_mode_key = collapse_plan_sub_mode(mode_key, sub_mode)
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
            # Surface the channel id to the adapter so session-scoped children
            # can branch their MCP load strategy (TUI = global config.yaml ∪
            # state.json enabled; web = session-level via chat.send's mcp field,
            # init loads nothing).
            config["channel_id"] = channel_key
            if channel_key == "acp":
                config = {
                    **config,
                    **_build_acp_agent_config()
                }
            agent = await self._create_agent(
                channel_key,
                mode_key,
                config,
                sub_mode_key or None,
                cache_key=cache_key,
            )
            return self._borrow_agent(agent)

    def get_agent_for_session_nowait(
        self,
        channel_id: str,
        session_id: str,
    ) -> "JiuWenSwarm | None":
        """Return the cached channel agent that owns ``session_id`` runtime."""
        sid = str(session_id or "").strip()
        if not sid:
            return None

        channel_key = _normalize_channel_id(channel_id)
        channel_agents = self.agents.get(channel_key, {})
        if not isinstance(channel_agents, dict):
            return None

        for cache_key, agent in channel_agents.items():
            has_runtime = getattr(agent, "has_session_runtime", None)
            if not callable(has_runtime):
                continue
            try:
                if has_runtime(sid):
                    return self._borrow_agent(agent)
            except Exception:
                logger.exception(
                    "[AgentManager] session runtime lookup failed: "
                    "channel_id=%s session_id=%s cache_key=%s",
                    channel_key,
                    sid,
                    cache_key,
                )
        return None

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
        requested_sub_mode = (
            collapse_plan_sub_mode(mode, sub_mode) if sub_mode is not None else ""
        )
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
                # 默认回落优先取"普通 agent"实例：旧串 "agent" 与新 canonical
                # agent.work.* 都要命中。deprecate 归一把新旧形式统一成
                # agent.work.normal / agent.work.plan 再判定。
                if deprecate_mode(getattr(agent, "_jiuwenswarm_agent_mode", "")) in (
                    NEW_AGENT_WORK_NORMAL,
                    NEW_AGENT_WORK_PLAN,
                ):
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
        # 单 agent 模式热生效：work(agent) 与 code。manager_mode 在上游已归一，
        # cache_key 前缀只会是 agent 或 code。code.team / team.* 走独立
        # TeamAgent 体系，cache_key 前缀为 code.team/team，严格相等不命中。
        target_modes = {"agent", "code"}

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

    async def reload_agents_config(
        self,
        config,
        env,
        *,
        target_channel_id: str | None = None,
        target_session_id: str | None = None,
        reload_scopes: set[str] | None = None,
    ) -> None:
        """reload agent config.

        使用 ``self._reload_lock`` 串行化, 避免高频触发(如批量 MCP 增删)时多个
        reload 并发叠加, 同时重建大量 agent 实例导致内存暴涨被 OOM kill.

        ``reload_scopes`` 含 ``"model"`` 或 ``"multimodal"`` 时, 配置属于所有
        channel 共享的全局配置段, 此时忽略 ``target_channel_id`` 的窄化,
        fan-out 到全部 channel。否则 web 保存后只有 web 通道被热更新,
        IM 长连接通道的 session adapter 会继续使用旧配置。
        """
        async with self._reload_lock:
            self._latest_env_overrides = dict(env) if isinstance(env, dict) else {}
            for env_key, env_value in self._latest_env_overrides.items():
                key = str(env_key)
                if env_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(env_value)

            target_channel = str(target_channel_id or "").strip() or None
            target_session = str(target_session_id or "").strip() or None
            # 对话模型和多模态工具都是全局共享配置，必须广播到所有 channel。
            scope_set = set(reload_scopes) if reload_scopes else set()
            model_scope = "model" in scope_set
            global_scope = bool(scope_set & {"model", "multimodal"})
            effective_target_channel = None if global_scope else target_channel
            if target_channel and global_scope:
                logger.info(
                    "[AgentManager] global config scopes=%s changed via channel=%s; "
                    "fan-out reload to all channels",
                    sorted(scope_set & {"model", "multimodal"}),
                    target_channel,
                )
            effective_config = config
            if effective_config is None:
                try:
                    effective_config = get_config()
                except Exception:
                    effective_config = None
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
                return
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
                for _, agent in list(agents.items()):
                    reload_kwargs = {
                        "config_base": effective_config,
                        "env_overrides": self._latest_env_overrides,
                    }
                    if target_session:
                        reload_kwargs["target_session_id"] = target_session
                    if scope_set:
                        reload_kwargs["reload_scopes"] = scope_set
                    await agent.reload_agent_config(**reload_kwargs)
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
            if reload_completed:
                self._last_reload_fingerprint = fingerprint
                if isinstance(effective_config, dict):
                    self._latest_effective_config = dict(effective_config)
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

        class _ConnDiffKey(NamedTuple):
            # 新声明下同一 api_base 可能对应不同 endpoint_profile / auth_mode / api_mode，
            # 这些会影响连接身份(如 affinity 走 custom_headers 不带 Authorization)。
            # 纳入 diff key 避免误关/漏关连接池。core 侧 connection_key 已按归一 api_base
            # + 鉴权分桶，此处 diff 至少不比 Client 更粗。
            client_provider: str
            endpoint_profile: Any
            auth_mode: Any
            api_mode: Any
            api_key: str
            api_base: str
            verify_ssl: bool
            ssl_cert: Any

        def _diff_key(cfg: Any) -> _ConnDiffKey:
            return _ConnDiffKey(
                client_provider=str(cfg.client_provider),
                endpoint_profile=getattr(cfg, "endpoint_profile", None),
                auth_mode=getattr(cfg, "auth_mode", None),
                api_mode=getattr(cfg, "api_mode", None),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                verify_ssl=cfg.verify_ssl,
                ssl_cert=cfg.ssl_cert,
            )

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

    async def process_message(self, request: Any) -> Any:
        """处理非流式请求.

        Args:
            request: AgentRequest 对象

        Returns:
            AgentResponse 对象
        """
        try:
            await self.wait_for_session_prewarm(getattr(request, "session_id", None))
            channel_id = getattr(request, "channel_id", "")
            params = getattr(request, "params", {}) if isinstance(getattr(request, "params", {}), dict) else {}
            mode_full = params.get("mode", "agent")
            mode = str(mode_full).split(".")[0] if mode_full else "agent"
            project_dir = params.get("project_dir")

            agent = await self.get_agent(
                channel_id=channel_id,
                mode=mode,
                project_dir=project_dir,
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
            params = getattr(request, "params", {}) if isinstance(getattr(request, "params", {}), dict) else {}
            mode_full = params.get("mode", "agent")
            mode = str(mode_full).split(".")[0] if mode_full else "agent"
            project_dir = params.get("project_dir")

            agent = await self.get_agent(
                channel_id=channel_id,
                mode=mode,
                project_dir=project_dir,
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
