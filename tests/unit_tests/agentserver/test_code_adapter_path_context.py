from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.agent_evolving import trajectory

if not hasattr(trajectory, "InMemoryTrajectoryRegistry"):
    trajectory.InMemoryTrajectoryRegistry = MagicMock

from jiuwenswarm.common.path_provider import current_path_context
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


@pytest.mark.asyncio
async def test_runtime_update_binds_and_restores_path_session(monkeypatch):
    adapter = object.__new__(JiuwenSwarmCodeAdapter)
    runtime_config = SimpleNamespace(session_id="session-code")
    previous_session_id = current_path_context().session_id
    observed = []

    async def capture_context(received):
        assert received is runtime_config
        observed.append(current_path_context().session_id)

    monkeypatch.setattr(
        adapter,
        "_update_runtime_config_with_path_context",
        capture_context,
    )

    await adapter._update_runtime_config(runtime_config)

    assert observed == ["session-code"]
    assert current_path_context().session_id == previous_session_id
