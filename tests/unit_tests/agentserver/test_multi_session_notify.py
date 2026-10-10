from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.multi_session_toolkits import (
    MultiSessionToolkit,
    SessionTask,
    Status,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager


@pytest.mark.asyncio
async def test_completed_batch_uses_current_agent_manager_contract() -> None:
    toolkit = object.__new__(MultiSessionToolkit)
    toolkit.session_id = "parent-session"
    toolkit.channel_id = "web"
    toolkit.request_id = "request-1"
    toolkit.sessions = [
        SessionTask(
            session_id="child-session",
            description="Summarize the result",
            status=Status.COMPLETED,
            result="done",
        )
    ]
    toolkit._send_task_notification = AsyncMock()
    manager = MagicMock(spec=AgentManager)
    manager.get_agent_nowait.return_value = None

    with (
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.get_current_agent_manager",
            return_value=manager,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.send_runtime_push",
            new=AsyncMock(return_value=True),
        ) as push,
    ):
        await toolkit.notify("child-session", Status.COMPLETED, result="done")

    manager.get_agent_nowait.assert_called_once_with("web")
    push.assert_awaited_once()
