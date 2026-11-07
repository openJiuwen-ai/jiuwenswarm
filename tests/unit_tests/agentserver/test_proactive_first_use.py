# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Decision preparation preserves feedback, cancellation and model generations."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine


class DecisionAgent:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.closed = False
        self.close_count = 0

    async def invoke(self, payload):
        assert not self.closed
        self.calls.append(payload["kind"])
        return {}

    async def aclose(self):
        self.closed = True
        self.close_count += 1


@pytest.fixture
def decisions(monkeypatch):
    feedback_gate = asyncio.Event()
    feedback_gate.set()
    feedback_entered = asyncio.Event()
    state = SimpleNamespace(feedback_buffer=[], strategy_gradients=[], touch=lambda: None)
    analyzed = []

    def module(name, **exports):
        value = ModuleType(name)
        value.__dict__.update(exports)
        monkeypatch.setitem(sys.modules, name, value)

    async def analyze(_report, _skills, agent, **_kwargs):
        await agent.invoke({"kind": "decision"})
        analyzed.append(agent.name)
        return SimpleNamespace(decision=None)

    async def gradients(_feedbacks, _old, agent):
        await agent.invoke({"kind": "feedback"})
        feedback_entered.set()
        await feedback_gate.wait()
        return [{"rule": "preserve feedback"}]

    def consume(value):
        result = value.feedback_buffer
        value.feedback_buffer = []
        return result

    base = "jiuwenswarm.agents.harness.common.recommendation."
    module(base + "proactive_actions", _today_recommend_count=lambda: 0,
           _increment_daily_count=lambda: None, _get_all_skills=lambda: ([], []),
           _analyze_and_decide=analyze, _trigger_main_agent=lambda *_args: None)
    module(base + "profile_extractor", load_recommendation_state=lambda: state,
           save_recommendation_state=lambda _state: None)
    report = SimpleNamespace(is_empty=lambda: False, render_for_llm=lambda: "actual situation")

    async def build_report(**_kwargs):
        return report

    module(base + "situation_report", build_situation_report=build_report)
    module(base + "feedback_collector", consume_feedback_buffer=consume)
    module(base + "gradient_updater", update_gradients=gradients, render_decision_rules=lambda *_args, **_kwargs: "rules")
    module("jiuwenswarm.common.config", get_config=lambda: {"preferred_language": "en"})
    monkeypatch.setattr(ProactiveEngine, "run_maintenance", lambda _self: None)
    return SimpleNamespace(analyzed=analyzed, state=state, gate=feedback_gate, entered=feedback_entered)


@pytest.mark.asyncio
async def test_disabled_tick_never_constructs_decision_agent():
    engine = ProactiveEngine({"enabled": False})
    engine.set_proactive_agent_builder(lambda: pytest.fail("disabled tick must remain lazy"))
    assert await engine.tick_now() is False
    await engine.close()


@pytest.mark.asyncio
async def test_first_enabled_tick_builds_once_and_uses_existing_boolean_api(decisions):
    engine = ProactiveEngine({"enabled": True})
    calls = 0
    agent = DecisionAgent("prepared")

    def build():
        nonlocal calls
        calls += 1
        return agent

    engine.set_proactive_agent_builder(build)
    assert calls == 0
    assert await engine.tick_now() is False
    assert await engine.tick_now() is False
    assert calls == 1 and decisions.analyzed == ["prepared", "prepared"]
    await engine.close()
    assert agent.close_count == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_preparation(decisions):
    engine = ProactiveEngine({"enabled": True})
    entered, release = asyncio.Event(), asyncio.Event()
    builds = 0
    agent = DecisionAgent("shared")

    async def build():
        nonlocal builds
        builds += 1
        entered.set()
        await release.wait()
        return agent

    engine.set_proactive_agent_builder(build)
    first = asyncio.create_task(engine.tick_now())
    await entered.wait()
    second = asyncio.create_task(engine.tick_now())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    release.set()
    assert await second is False
    assert builds == 1 and decisions.analyzed == ["shared"]
    await engine.close()


@pytest.mark.asyncio
async def test_model_reload_preserves_old_feedback_and_decision_lease(decisions):
    engine = ProactiveEngine({"enabled": True})
    old, new = DecisionAgent("old"), DecisionAgent("new")
    engine.set_proactive_agent_builder(lambda: old)
    decisions.state.feedback_buffer = [{"feedback": "retain until applied"}]
    decisions.gate.clear()
    first = asyncio.create_task(engine.tick_now())
    await decisions.entered.wait()
    engine.rebuild_proactive_agent(lambda: new)
    assert not old.closed
    assert await engine.tick_now() is False
    assert decisions.analyzed == ["new"] and not old.closed
    decisions.gate.set()
    assert await first is False
    await asyncio.sleep(0)
    assert decisions.analyzed == ["new", "old"]
    assert old.calls == ["feedback", "decision"] and old.close_count == 1
    assert decisions.state.strategy_gradients == [{"rule": "preserve feedback"}]
    await engine.close()
    assert new.close_count == 1


@pytest.mark.asyncio
async def test_reload_during_preparation_discards_stale_generation(decisions):
    engine = ProactiveEngine({"enabled": True})
    entered, release = asyncio.Event(), asyncio.Event()
    old, new = DecisionAgent("old"), DecisionAgent("new")

    async def build_old():
        entered.set()
        await release.wait()
        return old

    engine.set_proactive_agent_builder(build_old)
    first = asyncio.create_task(engine.tick_now())
    await entered.wait()
    engine.rebuild_proactive_agent(lambda: new)
    assert await engine.tick_now() is False
    release.set()
    assert await first is False
    await asyncio.sleep(0)
    assert decisions.analyzed == ["new", "new"] and old.close_count == 1
    await engine.close()


@pytest.mark.asyncio
async def test_owner_close_cancels_owned_preparation_and_pending_tick():
    engine = ProactiveEngine({"enabled": True})
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def build():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine.set_proactive_agent_builder(build)
    tick = asyncio.create_task(engine.tick_now())
    await entered.wait()
    await engine.close()
    assert cancelled.is_set() and tick.cancelled()
    assert await engine.tick_now() is False


@pytest.mark.asyncio
async def test_preparation_failure_returns_false_then_retries_on_next_use(decisions):
    engine = ProactiveEngine({"enabled": True})
    attempts = 0
    agent = DecisionAgent("retry")

    def build():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("model unavailable")
        return agent

    engine.set_proactive_agent_builder(build)
    assert await engine.tick_now() is False
    assert await engine.tick_now() is False
    assert attempts == 2 and decisions.analyzed == ["retry"]
    await engine.close()


@pytest.mark.asyncio
async def test_existing_injected_agent_remains_compatible(decisions):
    engine = ProactiveEngine({"enabled": True})
    agent = DecisionAgent("injected")
    engine.set_proactive_agent(agent)
    assert await engine.tick_now() is False
    assert decisions.analyzed == ["injected"]
    await engine.close()


@pytest.mark.asyncio
async def test_reinjecting_same_agent_does_not_retire_live_object(decisions):
    engine = ProactiveEngine({"enabled": True})
    agent = DecisionAgent("injected")
    engine.set_proactive_agent(agent)
    assert await engine.tick_now() is False
    engine.set_proactive_agent(agent)
    await asyncio.sleep(0)
    assert not agent.closed
    assert await engine.tick_now() is False
    await engine.close()
    assert agent.close_count == 1


@pytest.mark.asyncio
async def test_invalid_reload_builder_keeps_existing_agent_and_failure_api(decisions):
    engine = ProactiveEngine({"enabled": True})
    agent = DecisionAgent("existing")
    engine.set_proactive_agent(agent)
    engine.rebuild_proactive_agent(None)
    assert await engine.tick_now() is False
    assert decisions.analyzed == ["existing"] and not agent.closed
    await engine.close()
    engine.rebuild_proactive_agent(lambda: DecisionAgent("closed"))
    assert await engine.tick_now() is False


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_cancel_owned_teardown(decisions):
    engine = ProactiveEngine({"enabled": True})
    entered, release = asyncio.Event(), asyncio.Event()
    agent = DecisionAgent("closing")

    async def close():
        entered.set()
        await release.wait()
        agent.closed = True
        agent.close_count += 1

    agent.aclose = close
    engine.set_proactive_agent(agent)
    assert await engine.tick_now() is False
    waiter = asyncio.create_task(engine.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not agent.closed and await engine.tick_now() is False
    release.set()
    await engine.close()
    assert agent.closed and agent.close_count == 1


@pytest.mark.asyncio
async def test_adapter_initialization_registers_builder_without_constructing(monkeypatch):
    from jiuwenswarm.server.runtime import proactive_adapter
    monkeypatch.setattr(proactive_adapter, "build_proactive_agent", lambda: pytest.fail("startup must not build"))
    server = SimpleNamespace(set_proactive_engine=lambda engine: setattr(server, "engine", engine),
                             get_agent_manager=lambda: SimpleNamespace(get_agent_nowait=lambda _channel: None))
    await proactive_adapter.init_proactive_engine(server, {"enabled": True})
    assert isinstance(server.engine, ProactiveEngine)
    await server.engine.close()
