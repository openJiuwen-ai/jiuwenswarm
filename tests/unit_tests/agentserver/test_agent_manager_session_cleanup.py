from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.agent_manager import AgentManager


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
async def test_same_key_creation_waits_for_old_root_cleanup() -> None:
    manager = _SlowCreateAgentManager()
    old_agent = _BlockingRootCleanupAgent()
    cache_key = "code:normal:/tmp/shared-project"
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
