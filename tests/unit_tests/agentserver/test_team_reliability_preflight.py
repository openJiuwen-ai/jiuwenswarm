# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolated checks against the editable SDK used by the Team host.

These tests deliberately use temporary SQLite files and no external model.
They characterize recovery/completion boundaries, not full migration quality
or automatic replay of a crashed Team's external tool effects.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.runtime.pool import ActiveTeam, TeamRuntimePool
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.database import engine as engine_module
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness import Workspace
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.goal.evaluation import GoalEvaluator
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import GoalAssessmentStatus, GoalRecord, GoalStatus
from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.task_loop.event_manager import EventManager


@pytest.mark.asyncio
@pytest.mark.parametrize("release_mode", ["cancel", "watchdog"])
async def test_saturated_reader_pool_returns_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    """Checkout saturation alone must not permanently consume read slots."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'team.db').as_posix()}",
        pool_size=2,
        max_overflow=0,
        pool_timeout=0.03,
    )
    sessions = engine_module.DbSessions(async_sessionmaker(engine))
    watchdog = 0.2 if release_mode == "watchdog" else 5.0
    monkeypatch.setattr(engine_module, "_DB_SESSION_WATCHDOG_SECONDS", watchdog)
    entered = asyncio.Queue()
    holders: list[asyncio.Task[None]] = []

    async def hold() -> None:
        async with sessions.read() as session:
            assert (await session.execute(text("SELECT 1"))).scalar() == 1
            entered.put_nowait(True)
            await asyncio.Event().wait()

    try:
        holders = [asyncio.create_task(hold()) for _ in range(2)]
        for _ in holders:
            await asyncio.wait_for(entered.get(), timeout=2)
        assert engine.pool.checkedout() == 2
        with pytest.raises(PoolTimeoutError):
            async with sessions.read() as session:
                await session.execute(text("SELECT 1"))

        if release_mode == "cancel":
            for task in holders:
                task.cancel()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*holders, return_exceptions=True), timeout=2,
        )
        expected = asyncio.CancelledError if release_mode == "cancel" else TimeoutError
        assert all(isinstance(outcome, expected) for outcome in outcomes)
        assert engine.pool.checkedout() == 0
        async with sessions.read() as session:
            assert (await session.execute(text("SELECT 2"))).scalar() == 2
    finally:
        for task in holders:
            task.cancel()
        await asyncio.gather(*holders, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_reader_bursts_do_not_leak_connections(tmp_path: Path) -> None:
    """Repeated bursts above pool capacity keep the event loop and slots live."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'team.db').as_posix()}",
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
    )
    sessions = engine_module.DbSessions(async_sessionmaker(engine))
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.001)

    async def read_one() -> int:
        async with sessions.read() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    pulse = asyncio.create_task(heartbeat())
    try:
        for _ in range(10):
            results = await asyncio.wait_for(
                asyncio.gather(*(read_one() for _ in range(32))), timeout=5,
            )
            assert results == [1] * 32
            assert engine.pool.checkedout() == 0
        assert ticks > 1
    finally:
        running = False
        await pulse
        await engine.dispose()


@pytest.mark.asyncio
async def test_reader_watchdog_does_not_interrupt_native_sqlite_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout cancels Python work, not a stuck native driver operation.

    The controlled SQLite function is released in finally. This documents
    why the watchdog alone cannot prove a wedged-driver recovery is bounded.
    """
    started = threading.Event()
    release = threading.Event()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'driver.db').as_posix()}",
        pool_size=1,
        max_overflow=0,
    )

    def block_driver() -> int:
        started.set()
        release.wait(timeout=3)
        return 1

    def register_function(connection: Any, record: object) -> None:
        connection.create_function("controlled_block", 0, block_driver)

    sql_event.listen(engine.sync_engine, "connect", register_function)
    sessions = engine_module.DbSessions(async_sessionmaker(engine))
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    monkeypatch.setattr(engine_module, "_DB_SESSION_WATCHDOG_SECONDS", 0.03)

    async def read() -> None:
        async with sessions.read() as session:
            await session.execute(text("SELECT controlled_block()"))

    task = asyncio.create_task(read())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.1)
        assert not task.done(), "driver cleanup still waits for native work after watchdog expiry"
        release.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=2)
        assert engine.pool.checkedout() == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_task_loop_returns_partial_without_implicit_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the real ReAct/TaskLoop with a deterministic no-network model.

    This is a normal DeepAgent invoke, not an entire Team. Team coordination
    must separately protect represented in-flight tasks (tested below).
    """
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="isolated-test",
            api_base="http://127.0.0.1:1", verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model="isolated-partial"),
    )
    invoke = AsyncMock(return_value=AssistantMessage(
        content="PARTIAL: implemented A; B remains unimplemented.",
    ))
    stream_calls = []

    async def stream(self: Model, *args: Any, **kwargs: Any) -> AsyncIterator[AssistantMessageChunk]:
        stream_calls.append((args, kwargs))
        yield AssistantMessageChunk(content="PARTIAL: implemented A; B remains unimplemented.")

    monkeypatch.setattr(Model, "invoke", invoke)
    monkeypatch.setattr(Model, "stream", stream)
    agent = DeepAgent(AgentCard(name="isolated-partial", description="preflight"))
    agent.configure(DeepAgentConfig(
        model=model,
        workspace=Workspace(root_path=str(tmp_path)),
        auto_create_workspace=False,
        enable_task_loop=True,
        enable_read_image_multimodal=False,
        completion_timeout=3,
    ))
    session = create_agent_session(session_id=agent.card.id, card=agent.card)
    await session.pre_run(inputs={})
    try:
        result = await asyncio.wait_for(agent.invoke({
            "query": "Implement both A and B; do not stop after A. Acceptance requires both.",
        }, session=session), timeout=5)
        assert result["result_type"] == "answer"
        assert "PARTIAL" in result["output"]
        assert len(stream_calls) + invoke.await_count == 1
        assert agent.goal_manager is None
    finally:
        await agent.stop()
        await agent.cleanup_task_resources()
        await session.post_run()


@pytest.mark.asyncio
async def test_replacing_runtime_pool_does_not_restore_persisted_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate process-local pool replacement while keeping the real DB file.

    This is not a real Worker kill/restart or a cold Team rehydration test.
    It pins down that restore_resumable_runtime only reattaches a warm entry.
    """
    from jiuwenswarm.agents.harness.team import team_manager as manager_module

    sid = "isolated-pool-replacement"
    team_name = "isolated-pool-team"
    token = set_session_id(sid)
    database = TeamDatabase(DatabaseConfig(
        db_type="sqlite", connection_string=str(tmp_path / "team.db"),
    ))
    runtime = SimpleNamespace(pool=TeamRuntimePool())
    monkeypatch.setattr(manager_module, "_runner_team_runtime_manager", lambda runner: runtime)
    monkeypatch.setattr(manager_module.TeamManager, "_lookup_session_team_name", lambda self, session: team_name)
    try:
        await database.initialize()
        await database.team.create_team(team_name, "Isolated team", "leader")
        await database.task.create_task("t1", team_name, "Outstanding work", "Finish B", "in_progress")
        await runtime.pool.add(ActiveTeam(
            team_name=team_name, agent=SimpleNamespace(), current_session_id=sid,
        ))
        warm = manager_module.TeamManager()
        assert await warm.restore_resumable_runtime(sid) is True

        runtime.pool = TeamRuntimePool()
        replacement = manager_module.TeamManager()
        assert await replacement.wait_for_resumable_runtime(sid, timeout_sec=0) is False
        assert replacement.is_runtime_active(sid) is False
        tasks = await database.task.get_team_tasks(team_name)
        assert [task.status for task in tasks] == ["in_progress"]
        assert await runtime.pool.list_team_names() == []
    finally:
        await database.close()
        reset_session_id(token)


@pytest.mark.asyncio
async def test_real_goal_loop_continues_after_scripted_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing explicit Goal path really schedules another attempt.

    Both model surfaces are replaced before execution; the second attempt is
    held at an async barrier so this test cannot become an unbounded loop.
    """
    second_attempt = asyncio.Event()
    release = asyncio.Event()
    calls = []
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="isolated-test",
            api_base="http://127.0.0.1:1", verify_ssl=False,
        ),
        model_config=ModelRequestConfig(model="isolated-goal"),
    )

    async def stream(self: Model, *args: Any, **kwargs: Any) -> AsyncIterator[AssistantMessageChunk]:
        calls.append((args, kwargs))
        if len(calls) >= 2:
            second_attempt.set()
            await release.wait()
        yield AssistantMessageChunk(content="PARTIAL: B remains unimplemented.")

    monkeypatch.setattr(Model, "stream", stream)
    monkeypatch.setattr(Model, "invoke", AsyncMock(return_value=AssistantMessage(
        content='{"status":"continue","evidence":"B is missing","remaining_work":"Implement B"}',
    )))
    agent = DeepAgent(AgentCard(name="isolated-goal", description="preflight"))
    agent.configure(DeepAgentConfig(
        model=model,
        workspace=Workspace(root_path=str(tmp_path)),
        auto_create_workspace=False,
        enable_task_loop=True,
        enable_read_image_multimodal=False,
        completion_timeout=3,
    ))
    session = create_agent_session(session_id=agent.card.id, card=agent.card)
    await session.pre_run(inputs={})
    try:
        await agent.start(session=session)
        assert await agent.attach_output() is not None
        await agent.goal_manager.set("Implement A and B; acceptance requires both")
        await asyncio.wait_for(second_attempt.wait(), timeout=5)
        assert len(calls) == 2
        record = await agent.goal_manager.get()
        assert record.status is GoalStatus.ACTIVE
        assert record.last_assessment.status is GoalAssessmentStatus.CONTINUE
        paused = await agent.goal_manager.pause()
        assert paused.status is GoalStatus.PAUSED
    finally:
        release.set()
        await agent.stop()
        await agent.cleanup_task_resources()
        await session.post_run()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", ["pending", "in_progress", "completed", "cancelled"])
async def test_partial_answer_is_not_the_team_completion_predicate(task_status: str) -> None:
    """Exercise the real completion predicate with a scripted partial answer.

    This is a component-boundary probe, not a full model/Team invocation:
    the board, roster, and mailbox are controlled stand-ins. It demonstrates
    both protection for represented work and the absence of an independent
    product-goal gate once the represented board becomes terminal.
    """
    model = SimpleNamespace(invoke=AsyncMock(return_value={
        "output": "PARTIAL: implemented A; requirement B remains unimplemented.",
        "result_type": "answer",
    }))
    answer = await model.invoke("Implement A and B; acceptance requires both.")
    rail = TaskCompletionRail()
    await rail.after_task_iteration(AgentCallbackContext(
        agent=SimpleNamespace(loop_coordinator=None),
        inputs=SimpleNamespace(result=answer),
    ))
    assert rail._goal_manager is None
    assert rail.build_evaluators() == []

    backend = SimpleNamespace(
        team_name="isolated-preflight",
        task_manager=SimpleNamespace(list_tasks=AsyncMock(return_value=[
            SimpleNamespace(status=task_status),
        ])),
        db=SimpleNamespace(member=SimpleNamespace(get_team_members=AsyncMock(return_value=[
            SimpleNamespace(status="ready"),
        ]))),
        message_manager=SimpleNamespace(has_unread_messages=AsyncMock(return_value=False)),
    )
    result = await TeamBackend.is_team_completed(backend)
    if task_status in {"pending", "in_progress"}:
        assert result is None
    else:
        assert result is not None
        assert result.task_count == 1
        assert result.member_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output_attached", [True, False])
async def test_explicit_goal_continues_partial_only_with_output_consumer(output_attached: bool) -> None:
    """Existing Goal machinery keeps missing/partial reports non-terminal.

    A Goal must be explicitly bound and have an output lease for continuation;
    merely enabling the outer task loop is not the same lifecycle contract.
    """
    record = GoalRecord.create(session_id="isolated-preflight", objective="Implement A and B")
    store = SimpleNamespace(
        session_id=record.session_id,
        load=lambda: record,
        save=lambda updated: None,
    )
    events = EventManager()
    notify = SimpleNamespace(count=0)

    def notify_work() -> None:
        notify.count += 1

    manager = GoalManager(
        store=store,
        event_manager=events,
        control_lock=asyncio.Lock(),
        has_output_stream=lambda: output_attached,
        cancel_active_round=AsyncMock(),
        emit_event=lambda event: None,
        notify_work=notify_work,
    )
    assessment = GoalEvaluator().assess(
        record=record,
        final_output="PARTIAL: B is not implemented",
    )
    assert assessment.status is GoalAssessmentStatus.CONTINUE
    updated = await manager.apply_assessment(
        goal_id=record.goal_id,
        revision=record.revision,
        assessment=assessment,
    )
    assert updated.status is GoalStatus.ACTIVE
    assert (events.next_work() is not None) is output_attached
    assert notify.count == int(output_attached)
