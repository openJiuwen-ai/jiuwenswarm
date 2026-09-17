from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentCreateFailed,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    normalize_agent_key_fields,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus


@pytest.mark.asyncio
async def test_get_or_create_agent_is_single_flight() -> None:
    agent_manager = AgentManager()
    creator_started = asyncio.Event()
    allow_creator = asyncio.Event()
    create_calls = 0

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        creator_started.set()
        await allow_creator.wait()
        agent.sandbox_id = "sandbox-1"
        return agent

    first = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await creator_started.wait()
    second = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )

    creating = await agent_manager.list_user_agents("u1")
    assert len(creating) == 1
    assert creating[0].info.status is AgentStatus.CREATING

    allow_creator.set()
    first_agent, second_agent = await asyncio.gather(first, second)
    assert create_calls == 1
    assert first_agent.info.agent_id == second_agent.info.agent_id
    assert first_agent.info.status is AgentStatus.READY
    assert first_agent.info.sandbox_id == "sandbox-1"


@pytest.mark.asyncio
async def test_waiting_for_creation_times_out() -> None:
    agent_manager = AgentManager(creating_timeout_seconds=0.1)
    creator_started = asyncio.Event()
    never = asyncio.Event()

    async def creator(agent: AgentInfo) -> AgentInfo:
        creator_started.set()
        await never.wait()
        return agent

    owner = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await creator_started.wait()

    with pytest.raises(AgentCreatingTimeout, match="AGENT_CREATING_TIMEOUT"):
        await agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)

    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    failed = await agent_manager.list_user_agents("u1")
    assert failed[0].info.status is AgentStatus.FAILED


@pytest.mark.asyncio
async def test_failed_create_does_not_retry() -> None:
    agent_manager = AgentManager()
    create_calls = 0

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)

    with pytest.raises(AgentCreateFailed, match="AGENT_CREATE_FAILED"):
        await agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)

    assert create_calls == 1
    failed = await agent_manager.list_user_agents("u1")
    assert failed[0].info.status is AgentStatus.FAILED


@pytest.mark.asyncio
async def test_cancelled_create_does_not_retry() -> None:
    agent_manager = AgentManager()
    create_calls = 0
    creator_started = asyncio.Event()
    never = asyncio.Event()

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        creator_started.set()
        await never.wait()
        return agent

    owner = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await creator_started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    with pytest.raises(AgentCreateFailed, match="AGENT_CREATE_FAILED"):
        await agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)

    assert create_calls == 1


@pytest.mark.asyncio
async def test_agent_manager_get_delete_and_list() -> None:
    agent_manager = AgentManager()
    created = await agent_manager.get_or_create_agent("u1", "jiuwenswarm")

    fetched = await agent_manager.get_agent("u1", "jiuwenswarm")
    assert fetched is not None
    assert fetched.info == created.info
    assert [
        item.info.agent_id for item in await agent_manager.list_user_agents("u1")
    ] == [created.info.agent_id]

    await agent_manager.delete_agent("u1", "jiuwenswarm")
    assert await agent_manager.get_agent("u1", "jiuwenswarm") is None
    assert await agent_manager.list_user_agents("u1") == []


@pytest.mark.asyncio
async def test_delete_during_creation_wakes_waiter() -> None:
    agent_manager = AgentManager(creating_timeout_seconds=5.0)
    creator_started = asyncio.Event()
    allow_creator = asyncio.Event()
    create_calls = 0

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        creator_started.set()
        await allow_creator.wait()
        agent.sandbox_id = "sandbox-1"
        return agent

    owner = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await creator_started.wait()
    waiter = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await asyncio.sleep(0.05)

    await agent_manager.delete_agent("u1", "jiuwenswarm")

    with pytest.raises(AgentDeleted, match="AGENT_DELETED"):
        await waiter

    allow_creator.set()
    with pytest.raises(AgentDeleted, match="AGENT_DELETED"):
        await owner

    assert await agent_manager.get_agent("u1", "jiuwenswarm") is None
    assert create_calls == 1


@pytest.mark.asyncio
async def test_get_or_create_after_delete_during_creation_can_retry() -> None:
    agent_manager = AgentManager()
    creator_started = asyncio.Event()
    allow_creator = asyncio.Event()
    create_calls = 0

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        creator_started.set()
        await allow_creator.wait()
        agent.sandbox_id = f"sandbox-{create_calls}"
        return agent

    owner = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await creator_started.wait()
    waiter = asyncio.create_task(
        agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    await asyncio.sleep(0.05)

    await agent_manager.delete_agent("u1", "jiuwenswarm")

    allow_creator.set()
    results = await asyncio.gather(waiter, owner, return_exceptions=True)
    assert all(isinstance(result, AgentDeleted) for result in results)

    recreated = await agent_manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    assert recreated.info.status is AgentStatus.READY
    assert recreated.info.sandbox_id == "sandbox-2"
    assert create_calls == 2


def test_normalize_agent_key_fields_defaults_and_aliases() -> None:
    assert normalize_agent_key_fields(None) == ("user_id", "agent_type")
    assert normalize_agent_key_fields("user_id+agent_type") == (
        "user_id",
        "agent_type",
    )
    assert normalize_agent_key_fields(
        ["user_id", "agent_type", "session_id"]
    ) == ("user_id", "agent_type", "session_id")
    with pytest.raises(ValueError, match="unsupported agent_key_field"):
        normalize_agent_key_fields(["user_id", "channel"])
    with pytest.raises(ValueError, match="must include user_id and agent_type"):
        normalize_agent_key_fields(["user_id"])


@pytest.mark.asyncio
async def test_acquire_and_release_track_task_count() -> None:
    agent_manager = AgentManager()

    first = await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", acquire=True
    )
    assert first.task_count == 1

    second = await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", acquire=True
    )
    assert second.task_count == 2

    await agent_manager.release(first.key)
    await agent_manager.release(first.key)
    fetched = await agent_manager.get_agent("u1", "jiuwenswarm")
    assert fetched is not None
    assert fetched.task_count == 0

    # Extra release stays floored at zero; release on a missing key is a no-op.
    await agent_manager.release(first.key)
    fetched = await agent_manager.get_agent("u1", "jiuwenswarm")
    assert fetched.task_count == 0
    await agent_manager.delete_agent("u1", "jiuwenswarm")
    await agent_manager.release(first.key)


@pytest.mark.asyncio
async def test_get_agent_acquire_holds_ready_runtime() -> None:
    agent_manager = AgentManager()
    created = await agent_manager.get_or_create_agent("u1", "claude")
    assert created.task_count == 0

    held = await agent_manager.get_agent("u1", "claude", acquire=True)
    assert held is not None
    assert held.task_count == 1
    assert await agent_manager.pop_if_idle(held.key, 0.0) is None

    plain = await agent_manager.get_agent("u1", "claude")
    assert plain is not None
    assert plain.task_count == 1

    await agent_manager.release(held.key)
    released = await agent_manager.get_agent("u1", "claude")
    assert released is not None
    assert released.task_count == 0


@pytest.mark.asyncio
async def test_pop_if_idle_respects_holds_and_staleness() -> None:
    agent_manager = AgentManager()

    async def creator(agent: AgentInfo) -> AgentInfo:
        agent.sandbox_id = "sandbox-1"
        return agent

    held = await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", creator=creator, acquire=True
    )
    key = held.key

    # Held (task_count > 0): never reclaimed even when "stale".
    await asyncio.sleep(0.05)
    assert await agent_manager.pop_if_idle(key, 0.01) is None

    # Released but not idle long enough.
    await agent_manager.release(key)
    assert await agent_manager.pop_if_idle(key, 60.0) is None

    # Released and stale: reclaimed atomically with sandbox info preserved.
    await asyncio.sleep(0.05)
    reclaimed = await agent_manager.pop_if_idle(key, 0.01)
    assert reclaimed is not None
    assert reclaimed.info.status is AgentStatus.DELETED
    assert reclaimed.info.sandbox_id == "sandbox-1"
    assert await agent_manager.get_agent("u1", "jiuwenswarm") is None

    # Missing key after pop.
    assert await agent_manager.pop_if_idle(key, 0.01) is None


@pytest.mark.asyncio
async def test_pop_if_idle_disabled_with_nonpositive_timeout() -> None:
    agent_manager = AgentManager()
    runtime = await agent_manager.get_or_create_agent("u1", "jiuwenswarm")
    await asyncio.sleep(0.05)

    assert await agent_manager.pop_if_idle(runtime.key, 0) is None
    assert await agent_manager.pop_if_idle(runtime.key, -1) is None
    assert await agent_manager.get_agent("u1", "jiuwenswarm") is not None
    assert await agent_manager.list_keys() == [runtime.key]


@pytest.mark.asyncio
async def test_session_scoped_key_creates_independent_agents() -> None:
    agent_manager = AgentManager(
        key_fields=["user_id", "agent_type", "session_id"]
    )
    create_calls = 0

    async def creator(agent: AgentInfo) -> AgentInfo:
        nonlocal create_calls
        create_calls += 1
        agent.sandbox_id = f"sandbox-{create_calls}"
        return agent

    first = await agent_manager.get_or_create_agent(
        "u1",
        "jiuwenswarm",
        key_values={"session_id": "sess-1"},
        creator=creator,
    )
    second = await agent_manager.get_or_create_agent(
        "u1",
        "jiuwenswarm",
        key_values={"session_id": "sess-2"},
        creator=creator,
    )
    reused = await agent_manager.get_or_create_agent(
        "u1",
        "jiuwenswarm",
        key_values={"session_id": "sess-1"},
        creator=creator,
    )

    assert create_calls == 2
    assert first.info.agent_id != second.info.agent_id
    assert reused.info.agent_id == first.info.agent_id
    assert first.key == ("u1", "jiuwenswarm", "sess-1")
    assert second.key == ("u1", "jiuwenswarm", "sess-2")

    await agent_manager.delete_agent(
        "u1", "jiuwenswarm", key_values={"session_id": "sess-1"}
    )
    assert await agent_manager.get_agent(
        "u1", "jiuwenswarm", key_values={"session_id": "sess-1"}
    ) is None
    assert await agent_manager.get_agent(
        "u1", "jiuwenswarm", key_values={"session_id": "sess-2"}
    ) is not None
