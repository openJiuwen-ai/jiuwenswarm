from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.a2x import a2x_registry_runtime as _runtime
from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
    replace_teammate_agent_card_after_bootstrap,
    register_blank_agent_if_teammate,
)


@pytest.mark.asyncio
async def test_replace_teammate_agent_card_raises_for_missing_required_fields() -> None:
    client = AsyncMock()
    with pytest.raises(ValueError, match="missing required dataset/service_id/member_name"):
        await replace_teammate_agent_card_after_bootstrap(
            client,
            dataset="team_pool",
            service_id="",
            member_name="team-1",
            source="test",
        )


@pytest.mark.asyncio
async def test_replace_teammate_agent_card_raises_for_missing_client() -> None:
    with pytest.raises(RuntimeError, match="missing A2X client"):
        await replace_teammate_agent_card_after_bootstrap(
            None,
            dataset="team_pool",
            service_id="sid-1",
            member_name="team-1",
            source="test",
        )


@pytest.mark.asyncio
async def test_replace_teammate_agent_card_accepts_configured_payload() -> None:
    client = AsyncMock()
    ok = await replace_teammate_agent_card_after_bootstrap(
        client,
        dataset="team_pool",
        service_id="sid-1",
        member_name="team-1",
        source="test",
        description="负责拆解任务",
        status="busy",
        skills=[{"name": "plan", "description": "子任务拆解"}],
    )
    assert ok is True
    client.replace_agent_card.assert_awaited_once()
    dataset, service_id, card = client.replace_agent_card.await_args.args
    assert dataset == "team_pool"
    assert service_id == "sid-1"
    assert card["name"] == "team-1"
    assert card["description"] == "负责拆解任务"
    assert card["status"] == "busy"
    assert card["skills"] == [{"name": "plan", "description": "子任务拆解"}]


@pytest.mark.asyncio
async def test_cached_blank_registration_is_exposed_on_later_client() -> None:
    registered_endpoints = getattr(_runtime, "_REGISTERED" "_BLANK_ENDPOINTS")
    registered_details = getattr(_runtime, "_REGISTERED" "_BLANK_REGISTRATIONS")
    registered_endpoints.clear()
    registered_details.clear()
    config = {
        "distributed_mode": True,
        "role": "teammate",
        "base_url": "http://127.0.0.1:8000",
        "dataset": "team_pool",
        "endpoint": "tcp://127.0.0.1:28610",
    }
    first_client = SimpleNamespace(register_blank_agent=AsyncMock(return_value=SimpleNamespace(service_id="sid-1")))
    second_client = SimpleNamespace(register_blank_agent=AsyncMock())

    try:
        assert await register_blank_agent_if_teammate(first_client, config, source="daemon") is True
        assert await register_blank_agent_if_teammate(second_client, config, source="deep-agent-init") is True
    finally:
        registered_endpoints.clear()
        registered_details.clear()

    first_client.register_blank_agent.assert_awaited_once()
    second_client.register_blank_agent.assert_not_awaited()
    assert getattr(second_client, "_jiuwen" "_blank_agent_registration") == {
        "dataset": "team_pool",
        "service_id": "sid-1",
        "endpoint": "tcp://127.0.0.1:28610",
    }
    assert getattr(second_client, "_jiuwen" "_blank_agent_dataset") == "team_pool"
    assert getattr(second_client, "_jiuwen" "_blank_agent_service_id") == "sid-1"
    assert getattr(second_client, "_jiuwen" "_blank_agent_endpoint") == "tcp://127.0.0.1:28610"
