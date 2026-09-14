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
async def test_member_rail_requests_exactly_three_retries() -> None:
    rail = TeamMemberNotifyingLLMRetryRail(
        max_retries=3,
        backoff_seconds=[0.0],
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )
    exception = RuntimeError(
        "openAI API async stream error: NotFoundError: Error code: 404"
    )
    context, requests = _retry_context(exception)

    for _ in range(4):
        await rail.on_model_exception(context)

    assert requests == [0.0, 0.0, 0.0]


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


def test_member_retry_provider_builds_three_retry_rail() -> None:
    register_swarm_providers()
    rail = RailSpec(type=registry.TEAM_MEMBER_LLM_RETRY).build(
        language="cn",
        context=SwarmBuildContext(session_id="s1", mode="team"),
    )

    assert isinstance(rail, TeamMemberNotifyingLLMRetryRail)
    assert rail.max_retries == 3


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
