# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility for older openjiuwen evolution-rail constructors."""

import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from jiuwenswarm.common.openjiuwen_rail_compat import (
    _wrap_init_for_extra_kwargs,
    call_attach_output,
    filter_unsupported_kwargs,
    install_evolution_rail_kwargs_compat,
    _install_model_continue_compat,
)


@pytest.fixture
def legacy_continue_context(monkeypatch):
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

    # Exercise the bridge even when the test SDK later gains the native API.
    monkeypatch.setattr(AgentCallbackContext, "request_model_continue", None, raising=False)
    _install_model_continue_compat(AgentCallbackContext)
    return AgentCallbackContext


def test_continue_bridge_preserves_native_implementation():
    class NativeContext:
        def request_model_continue(self):
            return "native"

    original = NativeContext.request_model_continue
    _install_model_continue_compat(NativeContext)
    assert NativeContext.request_model_continue is original


def test_continue_bridge_preserves_reminder_and_is_idempotent(legacy_continue_context):
    cls = legacy_continue_context
    original = cls.request_model_continue
    _install_model_continue_compat(cls)
    assert cls.request_model_continue is original
    ctx = cls(agent=None)
    queue = asyncio.Queue()
    ctx.bind_steering_queue(queue)
    ctx.push_steering("Only output the final answer")
    ctx.request_model_continue()
    ctx.request_model_continue()
    assert ctx.drain_steering() == ["Only output the final answer"]
    assert ctx.close_steering_if_empty()


def test_continue_bridge_requires_active_queue(legacy_continue_context):
    ctx = legacy_continue_context(agent=None)
    with pytest.raises(RuntimeError, match="active steering queue"):
        ctx.request_model_continue()


def test_continue_bridge_without_reminder_is_one_shot(legacy_continue_context):
    ctx = legacy_continue_context(agent=None)
    ctx.bind_steering_queue(asyncio.Queue())
    ctx.request_model_continue()
    assert not ctx.close_steering_if_empty()
    assert len(ctx.drain_steering()) == 1
    assert ctx.close_steering_if_empty()


@pytest.mark.parametrize("pending_reminder", [False, True])
def test_continue_bridge_rejects_stale_queue(legacy_continue_context, pending_reminder):
    ctx = legacy_continue_context(agent=None)
    queue = asyncio.Queue()
    queue.steering_inbox = SimpleNamespace(current=False)
    ctx.bind_steering_queue(queue)
    if pending_reminder:
        queue.put_nowait("existing reminder")
    with pytest.raises(RuntimeError, match="active steering queue"):
        ctx.request_model_continue()
    assert queue.qsize() == int(pending_reminder)
    if pending_reminder:
        assert queue.get_nowait() == "existing reminder"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_limit,max_iterations,expected_output", [
    (1, 3, "42"),
    (10, 2, "Max iterations reached without completion"),
])
async def test_continue_bridge_real_react_loop_and_budget(
    legacy_continue_context, monkeypatch, retry_limit, max_iterations, expected_output,
):
    from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    class BoundedGuard(AgentRail):
        requests = 0

        async def after_model_call(self, ctx):
            if self.requests < retry_limit:
                ctx.push_steering("Only output the corrected final answer")
                ctx.request_model_continue()
                self.requests += 1

    agent = ReActAgent(card=AgentCard(name="compat-continue-regression"))
    agent.configure(ReActAgentConfig(model_name="mock-model", max_iterations=max_iterations))
    context = MagicMock()
    context.add_messages = AsyncMock()
    context.get_context_window = AsyncMock(return_value=SimpleNamespace(
        get_messages=lambda: [], get_tools=lambda: None,
    ))
    engine = MagicMock()
    engine.create_context = AsyncMock(return_value=context)
    engine.save_contexts = AsyncMock()
    agent.context_engine = engine
    llm = SimpleNamespace(invoke=AsyncMock(side_effect=[
        AssistantMessage(content="Draft answer", finish_reason="stop"),
        AssistantMessage(content="42", finish_reason="stop"),
    ]))
    monkeypatch.setattr(agent, "_get_llm", lambda: llm)
    session = MagicMock()
    session.get_state.return_value = None
    session.get_session_id.return_value = "compat-continue-test"
    session.write_stream = AsyncMock()
    guard = BoundedGuard()
    await agent.register_rail(guard)
    try:
        result = await agent.invoke({"query": "answer", "_steering_queue": asyncio.Queue()}, session=session)
    finally:
        await agent.unregister_rail(guard)
    assert result["output"] == expected_output
    assert llm.invoke.await_count == 2
    assert guard.requests == min(2, retry_limit)
    if retry_limit > max_iterations:
        assert result["finish_reason"] == "max_iterations"
    assert any(
        isinstance(call.args[0], UserMessage) and "corrected final answer" in call.args[0].content
        for call in context.add_messages.call_args_list if call.args
    )


def test_filter_drops_signal_trigger_when_constructor_lacks_it():
    def _init(self, *, review_trigger=None):
        del self
        return review_trigger

    filtered = filter_unsupported_kwargs(
        _init,
        {"review_trigger": False, "signal_trigger": True},
    )
    assert filtered == {"review_trigger": False}


def test_filter_drops_unknown_dataclass_field():
    """Official DeepAgentConfig is a dataclass without VAR_KEYWORD."""
    from dataclasses import dataclass

    @dataclass
    class _LegacyDeepAgentConfig:
        enable_task_loop: bool = False

    filtered = filter_unsupported_kwargs(
        _LegacyDeepAgentConfig.__init__,
        {"enable_task_loop": True, "enable_subagent_runtime": True},
    )
    assert filtered == {"enable_task_loop": True}
    _LegacyDeepAgentConfig(**filtered)


def test_filter_keeps_kwargs_when_func_has_var_keyword():
    """Official create_deep_agent still has **config_kwargs; do not use it as the gate."""
    def _factory(*, model: str, **config_kwargs):
        del model, config_kwargs

    filtered = filter_unsupported_kwargs(
        _factory,
        {"enable_subagent_runtime": True},
    )
    assert filtered == {"enable_subagent_runtime": True}


def test_wrapped_rail_init_ignores_unknown_kwargs():
    class DummyRail:
        def __init__(self, *, review_trigger=None):
            self.review_trigger = review_trigger

    _wrap_init_for_extra_kwargs(DummyRail)
    rail = DummyRail(review_trigger=False, signal_trigger=True)
    assert rail.review_trigger is False


def test_call_attach_output_drops_steal_on_legacy_sdk():
    class Legacy:
        async def attach_output(self):
            return "ok"

    assert asyncio.run(call_attach_output(Legacy(), steal=True)) == "ok"


def test_call_attach_output_passes_steal_when_supported():
    class Newer:
        def __init__(self):
            self.seen = None

        async def attach_output(self, *, steal=False):
            self.seen = steal
            return "ok"

    inst = Newer()
    assert asyncio.run(call_attach_output(inst, steal=True)) == "ok"
    assert inst.seen is True


def test_install_evolution_rail_kwargs_compat_is_idempotent():
    install_evolution_rail_kwargs_compat()
    install_evolution_rail_kwargs_compat()
    from openjiuwen.harness.rails import SkillEvolutionRail

    assert getattr(SkillEvolutionRail.__init__, "_jiuwenswarm_kwargs_compat", False)
