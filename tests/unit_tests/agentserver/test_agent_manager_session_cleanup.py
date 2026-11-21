from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_manager import (
    AgentManager,
    _session_agent_max_live_default,
)


class _SessionRuntimeAgent:
    def __init__(self, *, has_session_runtime: bool) -> None:
        self._has_session_runtime = has_session_runtime
        self.cleaned_sessions: list[str] = []
        self.cleaned = False

    async def cleanup_session_runtime(self, session_id: str) -> bool:
        self.cleaned_sessions.append(session_id)
        return True

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        if session_id is not None:
            return False
        return self._has_session_runtime

    async def cleanup(self) -> None:
        self.cleaned = True


class _FailingSessionRuntimeAgent(_SessionRuntimeAgent):
    async def cleanup_session_runtime(self, session_id: str) -> bool:
        raise RuntimeError(f"cleanup failed: {session_id}")


class _RetainedSessionRuntimeAgent(_SessionRuntimeAgent):
    async def cleanup_session_runtime(self, session_id: str) -> bool:
        self.cleaned_sessions.append(session_id)
        return False

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        return True


class _BlockingRootCleanupAgent(_SessionRuntimeAgent):
    def __init__(self) -> None:
        super().__init__(has_session_runtime=False)
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()

    async def cleanup(self) -> None:
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        await super().cleanup()


class _FailingRootCleanupAgent(_SessionRuntimeAgent):
    async def cleanup(self) -> None:
        raise RuntimeError("root cleanup failed")


class _SlowCreateAgentManager(AgentManager):
    def __init__(self) -> None:
        super().__init__()
        self.create_count = 0
        self.create_started = asyncio.Event()
        self.allow_create = asyncio.Event()

    async def _create_agent(
        self,
        agent_key: str,
        mode: str = "agent",
        config: dict | None = None,
        sub_mode: str | None = None,
        cache_key: str | None = None,
    ) -> _SessionRuntimeAgent:
        self.create_count += 1
        self.create_started.set()
        await self.allow_create.wait()
        agent = _SessionRuntimeAgent(has_session_runtime=False)
        self.agents.setdefault(agent_key, {})[str(cache_key)] = agent
        return agent


@pytest.mark.asyncio
async def test_cleanup_session_runtime_reclaims_idle_tui_root_agent() -> None:
    manager = AgentManager()
    agent = _SessionRuntimeAgent(has_session_runtime=False)
    manager.agents["tui"] = {"code:normal:/tmp/case-1": agent}
    manager._agent_create_params["tui"] = {
        "code:normal:/tmp/case-1": {"mode": "code"}
    }

    assert await manager.cleanup_session_runtime(
        channel_id="tui",
        session_id="tui_case_1",
    )

    assert agent.cleaned_sessions == ["tui_case_1"]
    assert agent.cleaned is True
    assert "tui" not in manager.agents
    assert "tui" not in manager._agent_create_params


@pytest.mark.asyncio
async def test_concurrent_get_agent_creates_one_cached_root() -> None:
    manager = _SlowCreateAgentManager()
    first = asyncio.create_task(
        manager.get_agent("tui", "code", "/tmp/shared-project", "normal")
    )
    await manager.create_started.wait()
    second = asyncio.create_task(
        manager.get_agent("tui", "code", "/tmp/shared-project", "normal")
    )
    await asyncio.sleep(0)

    manager.allow_create.set()
    first_agent, second_agent = await asyncio.gather(first, second)

    assert manager.create_count == 1
    assert first_agent is second_agent


@pytest.mark.asyncio
async def test_projectless_sessions_use_distinct_agent_instances() -> None:
    manager = _SlowCreateAgentManager()
    manager.allow_create.set()

    first = await manager.get_agent("web", "agent", None, session_id="session-a")
    second = await manager.get_agent("web", "agent", None, session_id="session-b")
    first_again = await manager.get_agent("web", "agent", None, session_id="session-a")

    assert first is first_again
    assert first is not second
    assert manager.create_count == 2


@pytest.mark.asyncio
async def test_projectless_session_agent_is_released_after_cleanup() -> None:
    manager = _SlowCreateAgentManager()
    manager.allow_create.set()
    agent = await manager.get_agent("web", "agent", None, session_id="session-a")
    # borrow 时已写入 LRU 时间戳，显式删除必须同步清掉，否则 stale 条目
    # 随会话删除累积，且 id() 复用会让新 agent 继承旧时间戳。
    assert id(agent) in manager._agent_last_used

    assert await manager.cleanup_session_runtime(channel_id="web", session_id="session-a")
    assert agent.cleaned is True
    assert not manager.agents.get("web")
    assert id(agent) not in manager._agent_last_used


@pytest.mark.asyncio
async def test_same_key_creation_waits_for_old_root_cleanup() -> None:
    from jiuwenswarm.server.runtime.agent_manager import _make_agent_cache_key

    manager = _SlowCreateAgentManager()
    old_agent = _BlockingRootCleanupAgent()
    # 与 get_agent 内部使用相同的缓存 key 生成逻辑，避免 Windows 下路径
    # 规范化（normcase/abspath）后 key 不匹配、创建锁无法复用。
    cache_key = _make_agent_cache_key("code", "normal", "/tmp/shared-project")
    manager.agents["tui"] = {cache_key: old_agent}

    cleanup_task = asyncio.create_task(
        manager.cleanup_session_runtime(
            channel_id="tui",
            session_id="tui_old_session",
        )
    )
    await old_agent.cleanup_started.wait()
    create_task = asyncio.create_task(
        manager.get_agent("tui", "code", "/tmp/shared-project", "normal")
    )
    await asyncio.sleep(0)

    assert manager.create_count == 0

    old_agent.allow_cleanup.set()
    await manager.create_started.wait()
    manager.allow_create.set()
    new_agent = await create_task
    await cleanup_task

    assert old_agent.cleaned is True
    assert new_agent is not old_agent
    assert manager.create_count == 1


@pytest.mark.asyncio
async def test_failed_root_cleanup_restores_cached_agent() -> None:
    manager = AgentManager()
    agent = _FailingRootCleanupAgent(has_session_runtime=False)
    cache_key = "code:normal:/tmp/cleanup-failed"
    manager.agents["tui"] = {cache_key: agent}
    manager._agent_create_params["tui"] = {cache_key: {"mode": "code"}}

    with pytest.raises(RuntimeError, match="failed for 1 agent"):
        await manager.cleanup_session_runtime(
            channel_id="tui",
            session_id="tui_cleanup_failed",
        )

    assert manager.agents["tui"][cache_key] is agent
    assert manager._agent_create_params["tui"][cache_key] == {"mode": "code"}


@pytest.mark.asyncio
async def test_cleanup_session_runtime_keeps_tui_root_agent_with_other_sessions() -> None:
    manager = AgentManager()
    agent = _SessionRuntimeAgent(has_session_runtime=True)
    cache_key = "code:normal:/tmp/shared-project"
    manager.agents["tui"] = {cache_key: agent}
    manager._agent_create_params["tui"] = {
        cache_key: {"mode": "code"}
    }

    assert await manager.cleanup_session_runtime(
        channel_id="tui",
        session_id="tui_case_1",
    )

    assert agent.cleaned_sessions == ["tui_case_1"]
    assert agent.cleaned is False
    assert manager.agents["tui"][cache_key] is agent


@pytest.mark.asyncio
async def test_repeated_tui_projects_do_not_accumulate_root_agents() -> None:
    manager = AgentManager()

    for index in range(20):
        cache_key = f"code:normal:/tmp/case-{index}"
        agent = _SessionRuntimeAgent(has_session_runtime=False)
        manager.agents.setdefault("tui", {})[cache_key] = agent
        manager._agent_create_params.setdefault("tui", {})[cache_key] = {
            "mode": "code"
        }

        assert await manager.cleanup_session_runtime(
            channel_id="tui",
            session_id=f"tui_case_{index}",
        )
        assert agent.cleaned is True
        assert "tui" not in manager.agents
        assert "tui" not in manager._agent_create_params


@pytest.mark.asyncio
async def test_cleanup_waits_for_borrowed_tui_root_agent() -> None:
    manager = AgentManager()
    agent = _SessionRuntimeAgent(has_session_runtime=False)
    cache_key = "code:normal:/tmp/shared-project"
    manager.agents["tui"] = {cache_key: agent}
    manager._agent_create_params["tui"] = {
        cache_key: {"mode": "code"}
    }
    borrowed = asyncio.Event()
    release = asyncio.Event()

    async def use_agent() -> None:
        assert manager.get_agent_nowait("tui") is agent
        borrowed.set()
        await release.wait()
        assert agent.cleaned is False

    user_task = asyncio.create_task(use_agent())
    await borrowed.wait()

    assert await manager.cleanup_session_runtime(
        channel_id="tui",
        session_id="tui_old_session",
    )
    assert manager.agents["tui"][cache_key] is agent
    assert agent.cleaned is False

    release.set()
    await user_task
    for _ in range(100):
        if agent.cleaned:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert agent.cleaned is True
    assert "tui" not in manager.agents
    assert manager._agent_borrowers == {}
    assert manager._pending_tui_retirements == set()
    assert manager._retirement_tasks == {}


@pytest.mark.asyncio
async def test_cleanup_waits_for_pinned_tui_root_agent() -> None:
    manager = AgentManager()
    agent = _SessionRuntimeAgent(has_session_runtime=False)
    cache_key = "agent::/tmp/scheduled-project"
    manager.agents["tui"] = {cache_key: agent}
    manager.pin_agent(agent)

    assert await manager.cleanup_session_runtime(
        channel_id="tui",
        session_id="tui_scheduler_owner",
    )
    assert manager.agents["tui"][cache_key] is agent
    assert agent.cleaned is False

    manager.unpin_agent(agent)
    for _ in range(100):
        if agent.cleaned:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert agent.cleaned is True
    assert "tui" not in manager.agents
    assert manager._agent_pins == {}
    assert manager._pending_tui_retirements == set()
    assert manager._retirement_tasks == {}


@pytest.mark.asyncio
async def test_cleanup_session_runtime_propagates_agent_failure() -> None:
    manager = AgentManager()
    agent = _FailingSessionRuntimeAgent(has_session_runtime=False)
    manager.agents["tui"] = {"code:normal:/tmp/failing-project": agent}

    with pytest.raises(RuntimeError, match="failed for 1 agent"):
        await manager.cleanup_session_runtime(
            channel_id="tui",
            session_id="tui_failed_session",
        )


@pytest.mark.asyncio
async def test_cleanup_session_runtime_rejects_retained_session_state() -> None:
    manager = AgentManager()
    agent = _RetainedSessionRuntimeAgent(has_session_runtime=True)
    manager.agents["tui"] = {"code:normal:/tmp/busy-project": agent}

    with pytest.raises(RuntimeError, match="failed for 1 agent"):
        await manager.cleanup_session_runtime(
            channel_id="tui",
            session_id="tui_busy_session",
        )


@pytest.mark.asyncio
async def test_release_subagent_runtime_uses_existing_agent_adapter() -> None:
    """Ported from develop e3ee2dec test_agent_manager_session_cleanup.py.

    Origin: 移植. Dest AgentManager already exposes this facade; the
    develop tests were missed when Commit 5 landed.
    """
    calls: list[tuple[str, str]] = []

    class Adapter:
        async def release_subagent_runtime_for_session(
            self,
            session_id: str,
            *,
            reason: str,
        ) -> None:
            calls.append((session_id, reason))

        async def apply_sandbox_runtime_patch(self) -> None:
            return None

    class Agent:
        def __init__(self) -> None:
            self._adapter = Adapter()

    manager = AgentManager()
    manager.agents["web"] = {"agent::": Agent()}
    released = await manager.release_subagent_runtime_for_session(
        channel_id="web",
        session_id="delete-session",
        reason="session_deleted",
    )
    assert released is True
    assert calls == [("delete-session", "session_deleted")]


@pytest.mark.asyncio
async def test_release_subagent_runtime_targets_projectless_session_owner() -> None:
    calls: list[str] = []

    class Adapter:
        def __init__(self, label: str) -> None:
            self.label = label

        async def release_subagent_runtime_for_session(self, _session_id: str, *, reason: str) -> None:
            calls.append(self.label)

    manager = AgentManager()
    manager.agents["web"] = {
        "agent:::session:session-a": SimpleNamespace(_adapter=Adapter("a")),
        "agent:::session:session-b": SimpleNamespace(_adapter=Adapter("b")),
    }

    assert await manager.release_subagent_runtime_for_session(
        channel_id="web", session_id="session-b", reason="session_deleted",
    )
    assert calls == ["b"]


@pytest.mark.asyncio
async def test_release_subagent_runtime_is_noop_without_existing_adapter() -> None:
    """Ported from develop e3ee2dec test_agent_manager_session_cleanup.py.

    Origin: 移植.
    """
    manager = AgentManager()
    released = await manager.release_subagent_runtime_for_session(
        channel_id="web",
        session_id="missing-session",
    )
    assert released is False


@pytest.mark.asyncio
async def test_create_session_cron_channel_uses_path_safe_prefix() -> None:
    from jiuwenswarm.server.runtime.session.session_metadata import resolve_session_subdir

    manager = AgentManager()
    session_id = await manager.create_session(channel_id="__cron__")

    assert session_id.startswith("cron_")
    assert not session_id.startswith("__cron__")
    assert resolve_session_subdir(session_id) is not None


def test_session_agent_max_live_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_SESSION_AGENT_MAX_LIVE", "7")
    assert _session_agent_max_live_default() == 7


def test_session_agent_max_live_env_zero_disables_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_SESSION_AGENT_MAX_LIVE", "0")
    assert _session_agent_max_live_default() == 0


def test_session_agent_max_live_env_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("abc", "-1", "   "):
        monkeypatch.setenv("JIUWENSWARM_SESSION_AGENT_MAX_LIVE", bad)
        assert _session_agent_max_live_default() == 50


def test_session_agent_max_live_env_absent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_SESSION_AGENT_MAX_LIVE", raising=False)
    assert _session_agent_max_live_default() == 50


@pytest.mark.asyncio
async def test_tui_retirement_clears_agent_last_used_entry() -> None:
    manager = AgentManager()
    agent = _SessionRuntimeAgent(has_session_runtime=False)
    cache_key = "agent::/tmp/retire-lru-bookkeeping"
    manager.agents["tui"] = {cache_key: agent}
    manager._agent_create_params["tui"] = {cache_key: {"mode": "agent"}}
    manager._agent_last_used[id(agent)] = 1.0

    assert await manager.cleanup_session_runtime(
        channel_id="tui",
        session_id="tui_lru_bookkeeping",
    )

    assert agent.cleaned is True
    assert "tui" not in manager.agents
    assert id(agent) not in manager._agent_last_used


@pytest.mark.asyncio
async def test_tui_retirement_cleanup_failure_restores_lru_timestamp() -> None:
    manager = AgentManager()
    agent = _BlockingThenFailingCleanupAgent()
    cache_key = "agent::/tmp/tui-retire-fail"
    manager.agents["tui"] = {cache_key: agent}
    manager._agent_create_params["tui"] = {cache_key: {"mode": "agent"}}
    manager._agent_last_used[id(agent)] = 1.0

    task = asyncio.create_task(
        manager.cleanup_session_runtime(
            channel_id="tui",
            session_id="tui_retire_fail",
        )
    )
    await agent.cleanup_started.wait()
    agent.allow_cleanup.set()
    with pytest.raises(RuntimeError, match="failed for 1 agent"):
        await asyncio.wait_for(task, timeout=2)

    # 回插须与另两处（LRU 驱逐、cleanup_session_runtime）一致地补时间戳。
    assert manager.agents["tui"][cache_key] is agent
    assert manager._agent_create_params["tui"][cache_key] == {"mode": "agent"}
    assert id(agent) in manager._agent_last_used


def _seed_session_agent(
    manager: AgentManager,
    channel_key: str,
    cache_key: str,
    *,
    last_used: float = 0.0,
) -> _SessionRuntimeAgent:
    agent = _SessionRuntimeAgent(has_session_runtime=False)
    manager.agents.setdefault(channel_key, {})[cache_key] = agent
    manager._agent_create_params.setdefault(channel_key, {})[cache_key] = {
        "mode": "agent"
    }
    manager._agent_last_used[id(agent)] = last_used
    return agent


@pytest.mark.asyncio
async def test_lru_cap_evicts_least_recently_used_session_agent_only() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 2
    # 共享项目 agent 时间戳最旧，但键不含 :session:，此 cap 永不触碰它。
    shared = _seed_session_agent(
        manager, "web", "agent::/tmp/shared-project", last_used=0.5
    )
    old = _seed_session_agent(
        manager, "web", "agent:::session:session-old", last_used=1.0
    )
    _seed_session_agent(manager, "web", "agent:::session:session-mid", last_used=2.0)
    _seed_session_agent(manager, "web", "agent:::session:session-new", last_used=3.0)

    await manager._evict_lru_session_agents_if_over_cap("web")

    assert old.cleaned is True
    assert shared.cleaned is False
    assert set(manager.agents["web"]) == {
        "agent::/tmp/shared-project",
        "agent:::session:session-mid",
        "agent:::session:session-new",
    }
    assert id(old) not in manager._agent_last_used
    assert "agent:::session:session-old" not in manager._agent_create_params["web"]


@pytest.mark.asyncio
async def test_lru_cap_skips_pinned_and_permission_pending_agents() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 2
    pinned = _seed_session_agent(
        manager, "web", "agent:::session:session-pinned", last_used=1.0
    )
    manager.pin_agent(pinned)
    permission_pending = _seed_session_agent(
        manager, "web", "agent:::session:session-perm", last_used=2.0
    )
    permission_pending._sessions_awaiting_tool_permission = {"session-perm"}
    fallback = _seed_session_agent(
        manager, "web", "agent:::session:session-new", last_used=3.0
    )

    await manager._evict_lru_session_agents_if_over_cap("web")

    # 钉住与等待工具授权的 agent 都被跳过，超额部分由最年轻的兜底驱逐。
    assert pinned.cleaned is False
    assert permission_pending.cleaned is False
    assert fallback.cleaned is True
    assert "agent:::session:session-new" not in manager.agents["web"]


@pytest.mark.asyncio
async def test_lru_cap_restores_agent_when_cleanup_fails() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 1
    failing = _FailingRootCleanupAgent(has_session_runtime=False)
    cache_key = "agent:::session:session-fail"
    manager.agents["web"] = {cache_key: failing}
    manager._agent_create_params["web"] = {cache_key: {"mode": "agent"}}
    manager._agent_last_used[id(failing)] = 1.0
    # 陪衬 agent 使池内 :session: 计数(2)超过 cap(1)，驱逐才会真正触发。
    _seed_session_agent(manager, "web", "agent:::session:session-other", last_used=2.0)

    await manager._evict_lru_session_agents_if_over_cap("web")

    assert manager.agents["web"][cache_key] is failing
    assert manager._agent_create_params["web"][cache_key] == {"mode": "agent"}
    assert failing.cleaned is False
    # 回插时间戳记为当前时刻，失败实例不得以缺省 0.0 插队被反复选中。
    assert id(failing) in manager._agent_last_used


class _BlockingThenFailingCleanupAgent(_SessionRuntimeAgent):
    def __init__(self) -> None:
        super().__init__(has_session_runtime=False)
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()

    async def cleanup(self) -> None:
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        raise RuntimeError("late cleanup failure")


@pytest.mark.asyncio
async def test_lru_cap_cleanup_failure_keeps_concurrently_rebuilt_agent() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 1
    failing = _BlockingThenFailingCleanupAgent()
    key = "agent:::session:session-race"
    manager.agents["web"] = {key: failing}
    manager._agent_create_params["web"] = {key: {"mode": "agent"}}
    manager._agent_last_used[id(failing)] = 1.0
    _seed_session_agent(manager, "web", "agent:::session:session-other", last_used=2.0)

    evict_task = asyncio.create_task(
        manager._evict_lru_session_agents_if_over_cap("web")
    )
    await failing.cleanup_started.wait()
    # 模拟 cleanup 挂起期间同 session 并发重建：新 Agent 已进缓存并记录参数。
    rebuilt = _SessionRuntimeAgent(has_session_runtime=False)
    manager.agents["web"][key] = rebuilt
    manager._agent_create_params["web"][key] = {"mode": "agent", "config": "rebuilt"}
    failing.allow_cleanup.set()
    await asyncio.wait_for(evict_task, timeout=2)

    # 失败实例必须被丢弃，绝不覆盖并发重建的新 Agent。
    assert manager.agents["web"][key] is rebuilt
    assert manager._agent_create_params["web"][key] == {"mode": "agent", "config": "rebuilt"}


@pytest.mark.asyncio
async def test_lru_cap_skips_agent_with_live_borrower() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 1
    busy = _seed_session_agent(
        manager, "web", "agent:::session:session-busy", last_used=1.0
    )
    holder = asyncio.create_task(asyncio.sleep(60))
    manager._agent_borrowers[id(busy)] = {holder}
    fallback = _seed_session_agent(
        manager, "web", "agent:::session:session-idle", last_used=2.0
    )

    try:
        await manager._evict_lru_session_agents_if_over_cap("web")
    finally:
        holder.cancel()

    # 最旧但 borrower 存活的 agent 被跳过，超额由次旧兜底。
    assert busy.cleaned is False
    assert fallback.cleaned is True


@pytest.mark.asyncio
async def test_lru_cap_never_evicts_the_requested_key() -> None:
    manager = AgentManager()
    manager._session_agent_max_live = 1
    keep = _seed_session_agent(
        manager, "web", "agent:::session:session-keep", last_used=1.0
    )
    old = _seed_session_agent(
        manager, "web", "agent:::session:session-old", last_used=2.0
    )

    await manager._evict_lru_session_agents_if_over_cap(
        "web", skip_cache_key="agent:::session:session-keep"
    )

    assert keep.cleaned is False
    assert old.cleaned is True


@pytest.mark.asyncio
async def test_get_agent_enforces_session_agent_cap_on_entry() -> None:
    manager = _SlowCreateAgentManager()
    manager._session_agent_max_live = 2
    manager.allow_create.set()
    first = await manager.get_agent("web", "agent", None, session_id="session-a")
    second = await manager.get_agent("web", "agent", None, session_id="session-b")
    third = await manager.get_agent("web", "agent", None, session_id="session-c")

    # 第三次创建后池内 3 个；下一次进入 get_agent 时先驱逐最久未用的 session-a。
    fourth = await manager.get_agent("web", "agent", None, session_id="session-d")

    assert first.cleaned is True
    assert second.cleaned is False
    assert third.cleaned is False
    assert fourth.cleaned is False
    assert set(manager.agents["web"]) == {
        "agent:::session:session-b",
        "agent:::session:session-c",
        "agent:::session:session-d",
    }
