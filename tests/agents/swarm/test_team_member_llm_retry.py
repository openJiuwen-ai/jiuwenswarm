from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    TeamMemberNotifyingLLMRetryRail,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm import register_swarm_providers
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec, RailSpec
from openjiuwen.agent_teams.workflow.backends._member_spec import derive_member_spec


def _retry_context(exception: BaseException):
    requests: list[float] = []
    context = SimpleNamespace(
        exception=exception,
        request_retry=lambda delay_seconds=0.0: requests.append(delay_seconds),
    )
    return context, requests


def test_member_rail_matches_only_the_observed_404_stream_shape() -> None:
    observed = RuntimeError(
        "[181001] model call failed, reason: "
        "openAI API async stream error: NotFoundError: Error code: 404"
    )
    unrelated_404 = RuntimeError("NotFoundError: Error code: 404")
    authentication = RuntimeError("openAI API async stream error: AuthenticationError: 401")

    assert TeamMemberNotifyingLLMRetryRail._looks_like_transient_invoke(observed)
    assert not TeamMemberNotifyingLLMRetryRail._looks_like_transient_invoke(unrelated_404)
    assert not TeamMemberNotifyingLLMRetryRail._looks_like_transient_invoke(authentication)


@pytest.mark.asyncio
async def test_member_rail_requests_configured_retry_budget() -> None:
    rail = TeamMemberNotifyingLLMRetryRail(
        max_retries=2,
        backoff_seconds=[0.0],
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )
    exception = RuntimeError(
        "openAI API async stream error: NotFoundError: Error code: 404"
    )
    context, requests = _retry_context(exception)

    for _ in range(3):
        await rail.on_model_exception(context)

    assert requests == [0.0, 0.0]


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
def test_team_retry_provider_is_scoped_to_expert_team_roles(mode: str) -> None:
    config = {"execution_guard": {"llm_retry_rail": {"enabled": True}}}

    teammate_rails, _ = build_member_capability_specs(config, mode, "teammate")
    leader_rails, _ = build_member_capability_specs(config, mode, "leader")

    teammate_names = [spec.type for spec in teammate_rails]
    leader_names = [spec.type for spec in leader_rails]
    assert teammate_names.count(registry.TEAM_MEMBER_LLM_RETRY) == 1
    assert leader_names.count(registry.TEAM_MEMBER_LLM_RETRY) == 1


def test_member_retry_provider_is_disabled_without_the_existing_guard_switch() -> None:
    rails, _ = build_member_capability_specs(
        {"execution_guard": {"llm_retry_rail": {"enabled": False}}},
        "team",
        "teammate",
    )

    assert registry.TEAM_MEMBER_LLM_RETRY not in {spec.type for spec in rails}


def test_member_retry_provider_uses_configured_retry_settings() -> None:
    config = {
        "execution_guard": {
            "llm_retry_rail": {
                "enabled": True,
                "max_retries": 2,
                "backoff_seconds": [0.0, 0.25],
                "repeat_min_pattern_chars": 3,
                "repeat_max_pattern_chars": 32,
                "repeat_min_count": 4,
                "repeat_min_total_chars": 80,
                "repeat_window_chars": 512,
                "single_char_repeat_count": 90,
                "retry_transient_invoke_errors": False,
                "notify_user_on_retry": False,
                "notify_user_on_exhausted": False,
            }
        }
    }
    register_swarm_providers()
    rails, _ = build_member_capability_specs(config, "team", "teammate")
    retry_spec = next(
        spec for spec in rails if spec.type == registry.TEAM_MEMBER_LLM_RETRY
    )
    rail = retry_spec.build(
        language="cn",
        context=SwarmBuildContext(session_id="s1", mode="team"),
    )

    assert isinstance(rail, TeamMemberNotifyingLLMRetryRail)
    assert rail.max_retries == 2
    assert rail.backoff_seconds == [0.0, 0.25]
    assert rail.repeat_min_pattern_chars == 3
    assert rail.repeat_max_pattern_chars == 32
    assert rail.repeat_min_count == 4
    assert rail.repeat_min_total_chars == 80
    assert rail.repeat_window_chars == 512
    assert rail.single_char_repeat_count == 90
    assert rail.retry_transient_invoke_errors is False
    assert rail.notify_user_on_retry is False
    assert rail.notify_user_on_exhausted is False


def test_swarmflow_worker_inherits_the_team_retry_rail() -> None:
    config = {"execution_guard": {"llm_retry_rail": {"enabled": True}}}
    teammate_rails, _ = build_member_capability_specs(config, "team", "teammate")
    base = DeepAgentSpec(rails=teammate_rails, tools=[])

    worker = derive_member_spec(
        base,
        team_name="team",
        member_name="worker-1",
        system_prompt="worker",
        model=None,
    )

    assert [spec.type for spec in worker.rails].count(registry.TEAM_MEMBER_LLM_RETRY) == 1
