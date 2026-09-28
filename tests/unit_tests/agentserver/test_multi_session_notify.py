from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.multi_session_toolkits import (
    MultiSessionToolkit,
    SessionTask,
    Status,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager


def _completed_toolkit() -> MultiSessionToolkit:
    toolkit = object.__new__(MultiSessionToolkit)
    toolkit.session_id = "parent-session"
    toolkit.channel_id = "web"
    toolkit.request_id = "request-1"
    toolkit._agent_manager = None
    toolkit.sessions = [
        SessionTask(
            session_id="child-session",
            description="Summarize the result",
            status=Status.COMPLETED,
            result="done",
        )
    ]
    toolkit._send_task_notification = AsyncMock()
    return toolkit


@pytest.mark.asyncio
async def test_completed_batch_uses_current_agent_manager_contract() -> None:
    toolkit = _completed_toolkit()
    manager = MagicMock(spec=AgentManager)
    manager.get_agent_nowait.return_value = None
    server = MagicMock()
    server.get_agent_manager.return_value = manager
    server.get_agent.return_value = None

    with (
        patch(
            "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.current_instance",
            return_value=server,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.send_runtime_push",
            new=AsyncMock(return_value=True),
        ) as push,
    ):
        await toolkit.notify("child-session", Status.COMPLETED, result="done")

    manager.get_agent_nowait.assert_called_once_with("web")
    server.get_agent.assert_called_once()
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_batch_uses_injected_agent_manager() -> None:
    toolkit = _completed_toolkit()
    manager = MagicMock(spec=AgentManager)
    manager.get_agent_nowait.return_value = None
    toolkit._agent_manager = manager

    with (
        patch(
            "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.current_instance",
        ) as current_instance,
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.send_runtime_push",
            new=AsyncMock(return_value=True),
        ) as push,
    ):
        await toolkit.notify("child-session", Status.COMPLETED, result="done")

    current_instance.assert_not_called()
    manager.get_agent_nowait.assert_called_once_with("web")
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_without_runtime_host_keeps_local_summary() -> None:
    toolkit = _completed_toolkit()

    with (
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.send_runtime_push",
            new=AsyncMock(return_value=False),
        ) as push,
        patch(
            "jiuwenswarm.agents.harness.common.tools.multi_session_toolkits.logger"
        ) as toolkit_logger,
    ):
        await toolkit.notify("child-session", Status.COMPLETED, result="done")

    push.assert_awaited_once()
    info_messages = [
        " ".join(str(part) for part in call.args)
        for call in toolkit_logger.info.call_args_list
    ]
    assert any("Runtime push host unavailable" in message for message in info_messages)
    assert any("parent-session" in message for message in info_messages)


@pytest.mark.asyncio
async def test_agent_server_push_host_lifecycle_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime import host_services
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    monkeypatch.setattr(host_services, "_runtime_push_handler", None)
    monkeypatch.setattr(host_services, "_runtime_push_handlers", [])

    first = AgentWebSocketServer.__new__(AgentWebSocketServer)
    first._runtime_push_handler = None
    first._previous_runtime_push_handler = None

    async def first_push(_msg: dict) -> int:
        return 1

    first.send_push = first_push  # type: ignore[method-assign]

    second = AgentWebSocketServer.__new__(AgentWebSocketServer)
    second._runtime_push_handler = None
    second._previous_runtime_push_handler = None
    calls: list[str] = []

    async def second_push(_msg: dict) -> int:
        calls.append("second")
        return 1

    second.send_push = second_push  # type: ignore[method-assign]

    first._install_runtime_push_host()
    second._install_runtime_push_host()
    first._restore_runtime_push_host()
    assert await host_services.send_runtime_push({"value": 1}) is True
    assert calls == ["second"]
    second._restore_runtime_push_host()
    assert await host_services.send_runtime_push({"value": 2}) is False
