from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.team import kv_cache_team_delete_guard


@pytest.mark.asyncio
async def test_enabled_affinity_retains_runner_until_kvc_release() -> None:
    stop_runtime = AsyncMock(return_value=True)

    with patch.object(kv_cache_team_delete_guard, "is_enabled", return_value=True):
        result = await kv_cache_team_delete_guard.stop_runtime_before_terminal_delete(
            stop_runtime,
            session_id="team-session",
            reason="delete: ",
        )

    assert result is True
    stop_runtime.assert_awaited_once_with(
        "team-session",
        reason="delete: ",
        stop_runner=False,
    )


@pytest.mark.asyncio
async def test_disabled_affinity_preserves_original_stop_call() -> None:
    stop_runtime = AsyncMock(return_value=True)

    with patch.object(kv_cache_team_delete_guard, "is_enabled", return_value=False):
        result = await kv_cache_team_delete_guard.stop_runtime_before_terminal_delete(
            stop_runtime,
            session_id="team-session",
            reason="delete: ",
        )

    assert result is True
    stop_runtime.assert_awaited_once_with(
        "team-session",
        reason="delete: ",
    )
