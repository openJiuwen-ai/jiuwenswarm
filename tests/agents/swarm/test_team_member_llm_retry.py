# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team retry wiring, bounded stream-404 retries, and inherited worker specs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec, RailSpec
from openjiuwen.agent_teams.workflow.backends._member_spec import derive_member_spec
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs

from jiuwenswarm.agents.harness.common.rails import llm_retry_notify_rail
from jiuwenswarm.agents.swarm import register_swarm_providers
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.context import SwarmBuildContext


_RETRY_PROVIDER = "swarm.team_member_llm_retry"
_STREAM_404 = "openAI API async stream error: NotFoundError: Error code: 404"


def _retry_spec(
    settings: dict[str, Any], mode: str = "team", role: str = "teammate"
) -> RailSpec:
    config = {"execution_guard": {"llm_retry_rail": {"enabled": True, **settings}}}
    rails, _ = build_member_capability_specs(config, mode, role)
    retries = [spec for spec in rails if spec.type == _RETRY_PROVIDER]
    assert len(retries) == 1, "Team member must mount exactly one configured retry rail"
    return retries[0]


def _build_rail(settings: dict[str, Any]):
    register_swarm_providers()
    return _retry_spec(settings).build(
        language="cn", context=SwarmBuildContext(session_id="s1", mode="team")
    )


def _ctx(message: str = _STREAM_404) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(messages=[], tools=None, model_context=None),
        exception=RuntimeError(message),
    )


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_retry_rail_is_mounted_once_for_each_team_profile(mode: str, role: str) -> None:
    _retry_spec({}, mode, role)


@pytest.mark.parametrize(
    "config",
    [{}, {"execution_guard": {}}, {"execution_guard": {"llm_retry_rail": {"enabled": False}}}],
)
def test_retry_provider_requires_existing_guard_switch(config: dict[str, Any]) -> None:
    rails, _ = build_member_capability_specs(config, "team", "teammate")
    assert _RETRY_PROVIDER not in {spec.type for spec in rails}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_STREAM_404, True),
        (_STREAM_404.upper(), True),
        ("Connection error", True),
        ("NotFoundError: Error code: 404", False),
        ("openAI API async stream error: Error code: 404", False),
        ("openAI API async stream error: AuthenticationError: 401", False),
    ],
)
@pytest.mark.asyncio
async def test_only_observed_stream_404_and_transient_errors_request_retry(
    message: str, expected: bool
) -> None:
    rail = _build_rail({"notify_user_on_retry": False})
    ctx = _ctx(message)

    await rail.on_model_exception(ctx)

    assert (ctx.consume_retry_request() is not None) is expected
    assert not llm_retry_notify_rail.NotifyingLLMRetryRail._looks_like_transient_invoke(
        RuntimeError(_STREAM_404)
    )


@pytest.mark.asyncio
async def test_configured_retry_budget_and_detection_settings_are_preserved(monkeypatch) -> None:
    monkeypatch.setattr(llm_retry_notify_rail.random, "uniform", lambda *_: 1.0)
    rail = _build_rail(
        {
            "max_retries": 3,
            "repeat_min_pattern_chars": 3,
            "repeat_max_pattern_chars": 32,
            "repeat_min_count": 4,
            "repeat_min_total_chars": 80,
            "repeat_window_chars": 512,
            "single_char_repeat_count": 90,
            "notify_user_on_retry": False,
            "notify_user_on_exhausted": False,
        }
    )
    ctx = _ctx()
    delays = []

    for _ in range(4):
        await rail.on_model_exception(ctx)
        request = ctx.consume_retry_request()
        if request is not None:
            delays.append(request.delay_seconds)

    assert delays == [5.0, 10.0, 20.0]
    assert rail.repeat_min_pattern_chars == 3
    assert rail.repeat_max_pattern_chars == 32
    assert rail.repeat_min_count == 4
    assert rail.repeat_min_total_chars == 80
    assert rail.repeat_window_chars == 512
    assert rail.single_char_repeat_count == 90


@pytest.mark.asyncio
async def test_transient_retry_can_be_disabled_for_team_stream_404() -> None:
    rail = _build_rail({"retry_transient_invoke_errors": False})
    ctx = _ctx()

    await rail.on_model_exception(ctx)

    assert ctx.consume_retry_request() is None


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.asyncio
async def test_retry_and_exhaustion_notifications_respect_config(notify: bool) -> None:
    rail = _build_rail(
        {
            "max_retries": 1,
            "notify_user_on_retry": notify,
            "notify_user_on_exhausted": notify,
        }
    )
    events = []

    async def write_stream(event):
        events.append(event)

    ctx = _ctx()
    ctx.session = SimpleNamespace(write_stream=write_stream)

    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is not None
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None

    assert [event.type for event in events] == (["retry_notification", "error"] if notify else [])
    if notify:
        assert events[0].payload["attempt"] == 1
        assert events[0].payload["max_attempts"] == 1
        assert events[1].payload["error_type"] == "llm_retry_exhausted"


@pytest.mark.asyncio
async def test_team_retry_keeps_per_context_budgets() -> None:
    rail = _build_rail(
        {"max_retries": 1, "notify_user_on_exhausted": False}
    )
    first, second = _ctx(), _ctx()
    await rail.before_invoke(first)
    await rail.on_model_exception(first)
    assert first.consume_retry_request() is not None

    await rail.before_invoke(second)
    await rail.on_model_exception(first)
    assert first.consume_retry_request() is None
    await rail.on_model_exception(second)
    assert second.consume_retry_request() is not None


@pytest.mark.parametrize("settings", [{}, {"backoff_seconds": [0.0, 0.25]}])
@pytest.mark.asyncio
async def test_team_retry_keeps_current_default_exponential_backoff(settings, monkeypatch) -> None:
    monkeypatch.setattr(llm_retry_notify_rail.random, "uniform", lambda *_: 1.0)
    rail = _build_rail(settings)
    ctx = _ctx()
    delays = []

    for _ in range(2):
        await rail.on_model_exception(ctx)
        request = ctx.consume_retry_request()
        assert request is not None
        delays.append(request.delay_seconds)

    assert delays == [5.0, 10.0]


@pytest.mark.asyncio
async def test_swarmflow_worker_keeps_retry_settings_after_serialization(monkeypatch) -> None:
    monkeypatch.setattr(llm_retry_notify_rail.random, "uniform", lambda *_: 1.0)
    base = DeepAgentSpec(rails=[_retry_spec({"max_retries": 1})])
    worker = derive_member_spec(
        base, team_name="team", member_name="worker-1", system_prompt="worker", model=None
    )
    restored = DeepAgentSpec.model_validate_json(worker.model_dump_json())
    register_swarm_providers()
    rail = restored.rails[0].build(language="cn", context=SwarmBuildContext(mode="team"))
    ctx = _ctx()

    await rail.on_model_exception(ctx)
    request = ctx.consume_retry_request()
    assert request is not None
    assert request.delay_seconds == 5.0
    await rail.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None
