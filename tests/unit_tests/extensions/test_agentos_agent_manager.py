from __future__ import annotations

import asyncio
import time

import pytest

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentCreateFailed,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    AgentRuntime,
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


def test_normalize_agent_type_keeps_registry_case() -> None:
    assert AgentRuntime.normalize_agent_type("Claude-Code") == "Claude-Code"
    assert AgentRuntime.normalize_agent_type("  claude-code  ") == "claude-code"
    assert AgentRuntime.normalize_agent_type("JiuwenSwarm") == "jiuwenswarm"
    assert AgentRuntime.normalize_agent_type("JIUWENSWARM") == "jiuwenswarm"
    assert AgentRuntime.normalize_agent_type("") == "jiuwenswarm"
    assert AgentRuntime.normalize_agent_type(None) == "jiuwenswarm"


@pytest.mark.asyncio
async def test_get_or_create_reuses_builtin_ignoring_case() -> None:
    agent_manager = AgentManager()
    first = await agent_manager.get_or_create_agent("u1", "JiuwenSwarm")
    second = await agent_manager.get_or_create_agent("u1", "jiuwenswarm")
    mixed = await agent_manager.get_or_create_agent("u1", "Claude-Code")
    assert first.key == second.key == ("u1", "jiuwenswarm")
    assert first.info.agent_type == "jiuwenswarm"
    assert mixed.key == ("u1", "Claude-Code")
    assert mixed.info.agent_type == "Claude-Code"


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


async def _create_shared_alias(manager, session, sandbox="same"):
    async def creator(info):
        info.sandbox_id = sandbox
        return info

    return await manager.get_or_create_agent(
        "user", "test", key_values={"session_id": session}, creator=creator,
    )


def _shared_alias_manager():
    return AgentManager(key_fields=["user_id", "agent_type", "session_id"])


async def test_forced_pop_removes_aliases_but_not_other_sandboxes():
    manager = _shared_alias_manager()
    first = await _create_shared_alias(manager, "one")
    sibling = await _create_shared_alias(manager, "two")
    unrelated = await _create_shared_alias(manager, "three", "different")

    assert await manager.is_current(first)
    assert await manager.pop_agent("user", "test", key_values={"session_id": "one"},
                                   expected_agent_id="wrong", include_shared_sandbox=True) is None
    assert await manager.is_current(sibling)
    popped = await manager.pop_agent("user", "test", key_values={"session_id": "one"},
                                     include_shared_sandbox=True)
    assert popped.info.agent_id == first.info.agent_id
    assert popped.is_deleted()
    assert not await manager.is_current(first)
    assert not await manager.is_current(sibling)
    assert await manager.is_current(unrelated)


@pytest.mark.parametrize("blocked", ["held", "recent", "failed"])
async def test_idle_pop_refuses_until_every_alias_is_idle(blocked):
    manager = _shared_alias_manager()
    first = await _create_shared_alias(manager, "one")
    sibling = await _create_shared_alias(manager, "two")
    manager._runtimes[first.key].last_active_at = time.time() - 100
    other = manager._runtimes[sibling.key]
    other.last_active_at = time.time() - 100
    if blocked == "held":
        other.task_count = 1
    elif blocked == "recent":
        other.last_active_at = time.time()
    else:
        other.mark_failed(RuntimeError("failure"))
    assert await manager.pop_if_idle(first.key, 30, include_shared_sandbox=True) is None
    assert await manager.get_agent("user", "test", key_values={"session_id": "one"}) is not None
    assert await manager.get_agent("user", "test", key_values={"session_id": "two"}) is not None


async def test_idle_pop_removes_idle_aliases_and_default_pop_remains_scoped():
    manager = _shared_alias_manager()
    first = await _create_shared_alias(manager, "one")
    sibling = await _create_shared_alias(manager, "two")
    for runtime in manager._runtimes.values():
        runtime.last_active_at = time.time() - 100
    assert (await manager.pop_if_idle(first.key, 30, include_shared_sandbox=True)).is_deleted()
    assert not await manager.is_current(sibling)

    first = await _create_shared_alias(manager, "one")
    sibling = await _create_shared_alias(manager, "two")
    await manager.pop_agent("user", "test", key_values={"session_id": "one"})
    assert not await manager.is_current(first)
    assert await manager.is_current(sibling)


async def test_wait_for_creation_times_out_instead_of_blocking_forever():
    manager = AgentManager(creating_timeout_seconds=0.05)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def creator(info):
        entered.set()
        await never.wait()
        return info

    creation = asyncio.create_task(
        manager.get_or_create_agent("u1", "jiuwenswarm", creator=creator)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime = await manager.get_agent("u1", "jiuwenswarm")
        assert runtime is not None
        assert await manager.wait_for_creation(
            "u1", "jiuwenswarm", expected_agent_id=runtime.info.agent_id,
        ) is False
    finally:
        never.set()
        creation.cancel()
        await asyncio.gather(creation, return_exceptions=True)


async def test_idle_pop_refuses_while_shared_sandbox_is_creating():
    manager = _shared_alias_manager()
    ready = await _create_shared_alias(manager, "one")
    manager._runtimes[ready.key].last_active_at = time.time() - 100
    entered = asyncio.Event()
    release = asyncio.Event()

    async def creator(info):
        entered.set()
        await release.wait()
        info.sandbox_id = "same"
        return info

    creating = asyncio.create_task(
        manager.get_or_create_agent(
            "user", "test", key_values={"session_id": "two"}, creator=creator,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await manager.pop_if_idle(
            ready.key, 30, include_shared_sandbox=True,
        ) is None
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)
