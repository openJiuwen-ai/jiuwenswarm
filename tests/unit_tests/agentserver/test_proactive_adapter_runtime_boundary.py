from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime import proactive_adapter
from jiuwenswarm.runtime.host_services import (
    install_runtime_push_handler,
    restore_runtime_push_handler,
)


class _AgentManager:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def get_agent_nowait(self, _channel_id: str) -> Any:
        return self.agent


async def _wait_for_background_push(session_id: str) -> None:
    key = f"web:{session_id}"
    deadline = asyncio.get_running_loop().time() + 2.0
    while (
        key in proactive_adapter._proactive_push_inflight
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)
    assert key not in proactive_adapter._proactive_push_inflight


def test_proactive_adapter_has_no_agentserver_import() -> None:
    source = Path(proactive_adapter.__file__).read_text(encoding="utf-8")
    assert "jiuwenswarm.server.agent_ws_server" not in source
    assert "WebSocketGatewayPushTransport" not in source


def test_transport_neutral_adapter_resolver_supports_wrapped_and_direct_agents() -> None:
    adapter = SimpleNamespace(is_deep_agent_executing_for_session=lambda _sid: False)
    wrapped = SimpleNamespace(_adapter=adapter)

    assert proactive_adapter.resolve_proactive_adapter(wrapped) is adapter
    assert proactive_adapter.resolve_proactive_adapter(adapter) is adapter
    assert proactive_adapter.resolve_proactive_adapter(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_trigger_uses_injected_manager_and_runtime_push_transport() -> None:
    pushed: list[dict[str, Any]] = []
    delivered = asyncio.Event()

    class _Agent:
        async def process_message_stream(self, _request):
            yield SimpleNamespace(
                request_id="chunk-1",
                payload={"content": "hello", "event_type": "chat.final"},
                is_complete=True,
            )

    class _PushTransport:
        async def send_push(self, message: dict[str, Any]) -> bool:
            pushed.append(message)
            return True

    server = SimpleNamespace(
        get_agent_manager=lambda: pytest.fail("explicit manager must be used"),
        send_push=lambda _message: pytest.fail("server transport must not be used"),
    )
    request = proactive_adapter.ProactiveTriggerRequest(
        session_id="session-transport",
        channel_id="web",
        query="recommend",
        decision=SimpleNamespace(type="tip", target="user"),
        on_delivered=lambda _msg: delivered.set(),
    )

    triggered = await proactive_adapter.trigger_main_agent(
        server,
        request,
        agent_manager=_AgentManager(_Agent()),
        push_transport=_PushTransport(),
    )
    await _wait_for_background_push(request.session_id)

    assert triggered is True
    assert delivered.is_set()
    assert pushed == [
        {
            "request_id": "chunk-1",
            "channel_id": "web",
            "session_id": "session-transport",
            "payload": {
                "content": "hello",
                "event_type": "chat.final",
                "source": "proactive_recommendation",
                "proactive_type": "tip",
                "proactive_target": "user",
            },
            "is_complete": True,
        }
    ]


@pytest.mark.asyncio
async def test_agentserver_host_path_preserves_push_behavior(monkeypatch) -> None:
    pushed: list[dict[str, Any]] = []
    delivered = asyncio.Event()

    class _Agent:
        async def process_message_stream(self, _request):
            yield SimpleNamespace(
                request_id="server-chunk",
                payload={"content": "server path", "event_type": "chat.final"},
                is_complete=True,
            )

    async def server_send_push(message: dict[str, Any]) -> bool:
        pushed.append(message)
        return True

    manager = _AgentManager(_Agent())
    server = SimpleNamespace(
        get_agent_manager=lambda: manager,
        send_push=server_send_push,
    )
    monkeypatch.setattr(
        proactive_adapter,
        "get_current_agent_manager",
        lambda: None,
    )
    previous = install_runtime_push_handler(server.send_push)
    request = proactive_adapter.ProactiveTriggerRequest(
        session_id="session-server-host",
        channel_id="web",
        query="recommend",
        decision=SimpleNamespace(type="tip", target="user"),
        on_delivered=lambda _msg: delivered.set(),
    )
    try:
        triggered = await proactive_adapter.trigger_main_agent(server, request)
        await _wait_for_background_push(request.session_id)
    finally:
        restore_runtime_push_handler(server.send_push, previous)

    assert triggered is True
    assert delivered.is_set()
    assert pushed[0]["request_id"] == "server-chunk"
    assert pushed[0]["payload"]["source"] == "proactive_recommendation"


@pytest.mark.asyncio
async def test_trigger_push_unavailable_does_not_report_delivery() -> None:
    delivered = asyncio.Event()

    class _Agent:
        async def process_message_stream(self, _request):
            yield SimpleNamespace(
                request_id="chunk-1",
                payload={"content": "hello", "event_type": "chat.final"},
                is_complete=True,
            )

    class _UnavailableTransport:
        # 真实 send_push 失败时返回 False（不抛异常），见 AgentWebSocketServer.send_push。
        async def send_push(self, _message: dict[str, Any]) -> bool:
            return False

    request = proactive_adapter.ProactiveTriggerRequest(
        session_id="session-unavailable",
        channel_id="web",
        query="recommend",
        decision=SimpleNamespace(type="tip", target="user"),
        on_delivered=lambda _msg: delivered.set(),
    )

    triggered = await proactive_adapter.trigger_main_agent(
        SimpleNamespace(),
        request,
        agent_manager=_AgentManager(_Agent()),
        push_transport=_UnavailableTransport(),
    )
    await _wait_for_background_push(request.session_id)

    assert triggered is True
    assert delivered.is_set() is False


def test_runtime_context_agent_manager_has_priority(monkeypatch) -> None:
    context_manager = object()
    explicit_manager = object()
    monkeypatch.setattr(
        proactive_adapter,
        "get_current_agent_manager",
        lambda: context_manager,
    )

    assert (
        proactive_adapter._resolve_agent_manager(
            SimpleNamespace(),
            explicit_manager,
        )
        is context_manager
    )


@pytest.mark.asyncio
async def test_notification_without_runtime_host_returns_false(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _Engine:
        def __init__(self, _config: dict[str, Any]) -> None:
            pass

        def set_proactive_agent(self, agent: Any) -> None:
            captured["agent"] = agent

        def set_check_agent_available_callback(self, callback: Any) -> None:
            captured["check"] = callback

        def set_send_notification_callback(self, callback: Any) -> None:
            captured["notify"] = callback

        def set_trigger_main_agent_callback(self, callback: Any) -> None:
            captured["trigger"] = callback

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.recommendation.proactive_engine.ProactiveEngine",
        _Engine,
    )
    monkeypatch.setattr(proactive_adapter, "build_proactive_agent", lambda: object())

    async def unavailable(_message: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(proactive_adapter, "send_runtime_push", unavailable)
    server = SimpleNamespace(
        get_agent_manager=lambda: _AgentManager(None),
        set_proactive_engine=lambda engine: captured.setdefault("engine", engine),
        send_push=lambda _message: pytest.fail("server transport must not be used"),
    )

    await proactive_adapter.init_proactive_engine(server, {})

    assert await captured["notify"]("web", "notice") is False
