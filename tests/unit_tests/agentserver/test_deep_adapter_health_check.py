# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HealthCheck probes must never enter the normal agent task pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@pytest.mark.asyncio
async def test_health_check_is_acknowledged_without_mutating_or_executing_query(
) -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    request = AgentRequest(
        request_id="probe-1",
        channel_id="__health_check__",
        session_id="health_check_probe-1",
        params={"query": "must remain untouched"},
        metadata={"trace": "health-check"},
    )

    response = await adapter.handle_heartbeat(request)

    assert response is not None
    assert response.ok is True
    assert response.payload == {"health_check": "HEALTH_CHECK_OK"}
    assert response.metadata == {"trace": "health-check"}
    assert request.params == {"query": "must remain untouched"}


@pytest.mark.asyncio
async def test_normal_session_does_not_use_health_check_short_circuit() -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    request = AgentRequest(
        request_id="chat-1",
        channel_id="web",
        session_id="session-1",
        params={"query": "continue the task"},
    )

    assert await adapter.handle_heartbeat(request) is None


@pytest.mark.asyncio
async def test_legacy_heartbeat_session_does_not_use_health_check_short_circuit() -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    request = AgentRequest(
        request_id="probe-old",
        channel_id="__health_check__",
        session_id="heartbeat_legacy-probe-1",
        params={"query": "obsolete probe"},
    )

    assert await adapter.handle_heartbeat(request) is None


@pytest.mark.asyncio
async def test_root_health_check_does_not_create_a_session_agent() -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False  # pylint: disable=protected-access
    adapter._get_or_create_session_adapter = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("health check must not create an agent")
    )
    request = AgentRequest(
        request_id="probe-root",
        channel_id="__health_check__",
        session_id="health_check_root",
        params={"query": "probe"},
    )

    response = await adapter.handle_heartbeat(request)

    assert response is not None
    assert response.payload == {"health_check": "HEALTH_CHECK_OK"}
    adapter._get_or_create_session_adapter.assert_not_awaited()
