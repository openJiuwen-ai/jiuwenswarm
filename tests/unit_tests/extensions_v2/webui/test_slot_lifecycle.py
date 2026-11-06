"""Global slots keep connection lifetime and chat session facts separate."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, SessionContext

import jiuwenswarm.extensions_v2.assembly as assembly
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.handler_ops.session_ops import register_session_ops
from jiuwenswarm.extensions_v2.event_types.session_queries import register_session_query_events
from jiuwenswarm.extensions_v2.webui import constants
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.call_context import current_connection
from jiuwenswarm.extensions_v2.webui.constants import SlotScope, SlotSpec
from jiuwenswarm.extensions_v2.webui.models import (
    RenderNode, RenderStatus, RenderTarget, UiActionInput, UiRenderInput,
)
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime
from jiuwenswarm.extensions_v2.webui.transport import RenderTransport
from jiuwenswarm.gateway.channel_manager.web.webui_handlers import register_webui_handlers


@pytest.fixture(autouse=True)
def allow_application_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "SLOT_SPECS", {
        **constants.SLOT_SPECS, "application.test": SlotSpec("list", SlotScope.APPLICATION),
    })


class Push:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_push(self, message: dict) -> bool:
        self.messages.append(message)
        return True


async def build_runtime() -> tuple[WebUIRuntime, Push]:
    bus = ExtensionEventBus()
    push = Push()
    runtime = WebUIRuntime(bus=bus, transport=RenderTransport(push=push))
    runtime.register_events()
    register_session_query_events(bus)
    register_session_ops(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    return runtime, push


def target(connection_id: str = "", *, scope: str = "application") -> RenderTarget:
    return RenderTarget("", "application.test", "pkg", "home", scope=scope, connection_id=connection_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "invalid"])
async def test_scope_errors_are_sent_to_the_trusted_connection(scope: str) -> None:
    runtime, push = await build_runtime()
    output = await runtime.render(
        UiRenderInput(target(scope=scope), RenderNode("Text")),
        SessionContext("real-chat", metadata={"ws_id": "sock-1"}),
    )
    assert output.status is RenderStatus.REJECTED
    message = push.messages[-1]
    assert message["metadata"] == {"ws_id": "sock-1"}
    assert "session_id" not in message
    assert message["payload"]["target"]["scope"] == "application"
    assert message["payload"]["code"] == constants.CODE_SCHEMA_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_id,scope", [
    ("conversation.panel.right", "application"),
    ("conversation.panel.right", "invalid"),
    ("application.settings", "invalid"),
])
@pytest.mark.parametrize("connection_id", ["", "other-socket"])
async def test_session_scope_errors_are_sent_to_the_chat(slot_id: str, scope: str, connection_id: str) -> None:
    runtime, push = await build_runtime()
    chat_target = RenderTarget(
        "real-chat", slot_id, "pkg", "home",
        scope=scope, connection_id=connection_id,
    )
    output = await runtime.render(
        UiRenderInput(chat_target, RenderNode("Text")),
        SessionContext("real-chat", metadata={"ws_id": "sock-1"}),
    )
    assert output.status is RenderStatus.REJECTED
    assert len(push.messages) == 1
    message = push.messages[0]
    assert message["session_id"] == "real-chat"
    assert "metadata" not in message
    assert message["payload"]["target"] == {
        "scope": "session",
        "session_id": "real-chat",
        "slot_id": slot_id,
        "package_id": "pkg",
        "instance_key": "home",
    }
    assert message["payload"]["code"] == constants.CODE_SCHEMA_ERROR
    assert runtime.registry.snapshot() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["session", "application"])
@pytest.mark.parametrize("rejected", [False, True])
async def test_settings_render_and_errors_preserve_the_allowed_scope(scope: str, rejected: bool) -> None:
    runtime, push = await build_runtime()
    session_id = "real-chat" if scope == "session" else ""
    instance_key = "bad|key" if rejected else "setting"
    settings_target = RenderTarget(
        session_id, "application.settings", "pkg", instance_key, scope=scope,
    )
    output = await runtime.render(
        UiRenderInput(settings_target, RenderNode("Text")),
        SessionContext("real-chat", metadata={"ws_id": "sock-1"}),
    )
    assert output.status is (RenderStatus.REJECTED if rejected else RenderStatus.DELIVERED)
    assert len(push.messages) == 1
    message = push.messages[0]
    assert message["payload"]["target"] == settings_target.to_payload()
    if scope == "application":
        assert message["metadata"] == {"ws_id": "sock-1"}
        assert "session_id" not in message
    else:
        assert message["session_id"] == session_id
        assert "metadata" not in message
    if rejected:
        assert message["payload"]["code"] == constants.CODE_SCHEMA_ERROR
        assert runtime.registry.snapshot() == ()


@pytest.mark.asyncio
async def test_application_settings_connection_errors_use_the_trusted_connection() -> None:
    runtime, push = await build_runtime()
    settings_target = RenderTarget(
        "", "application.settings", "pkg", "setting", scope="application", connection_id="other-socket",
    )
    output = await runtime.render(
        UiRenderInput(settings_target, RenderNode("Text")),
        SessionContext("real-chat", metadata={"ws_id": "sock-1"}),
    )
    assert output.status is RenderStatus.REJECTED
    assert len(push.messages) == 1
    message = push.messages[0]
    assert message["metadata"] == {"ws_id": "sock-1"}
    assert "session_id" not in message
    assert message["payload"]["target"] == settings_target.to_payload()
    assert message["payload"]["code"] == constants.CODE_SCHEMA_ERROR
    assert runtime.registry.snapshot() == ()


@pytest.mark.asyncio
async def test_application_handler_has_no_chat_and_can_render() -> None:
    runtime, push = await build_runtime()
    observed: list[object] = []

    async def handler(event, ops, next_handler):
        observed.append(ops.session_context)
        connection = current_connection()
        observed.append((connection.connection_id, connection.user_id))
        with pytest.raises(RuntimeError, match="SessionContext"):
            await ops.session.cwd()
        await ops.ui.render(UiRenderInput(target(), RenderNode("Text")))
        return await next_handler(event)

    runtime.bus.register_event_handler("ui.ready", handler, {})
    result = await runtime.dispatch(
        UiActionInput("ui.ready", target("sock-1")), user_id="user-1",
    )
    assert result.accepted is True
    assert observed == [None, ("sock-1", "user-1")]
    assert push.messages[-1]["metadata"] == {"ws_id": "sock-1"}
    assert current_connection() is None


@pytest.mark.asyncio
async def test_drop_connection_preserves_other_connections_and_chats() -> None:
    runtime, push = await build_runtime()
    for connection_id in ("sock-1", "sock-2"):
        await runtime.render(UiRenderInput(target(connection_id), RenderNode("Text")))
    chat_target = RenderTarget("chat-1", "conversation.input.below", "pkg", "home")
    await runtime.render(UiRenderInput(chat_target, RenderNode("Text")), "chat-1")
    sent = len(push.messages)

    await runtime.drop_connection("sock-1")
    await runtime.drop_connection("sock-1")

    assert runtime.registry.get(target("sock-1")) is None
    assert runtime.registry.get(target("sock-2")) is not None
    assert runtime.registry.get(chat_target) is not None
    assert len(push.messages) == sent


@pytest.mark.asyncio
async def test_closed_connection_cannot_be_recreated_by_an_awaiting_handler() -> None:
    runtime, push = await build_runtime()
    entered, resume = asyncio.Event(), asyncio.Event()
    results = []

    async def handler(event, ops, next_handler):
        entered.set()
        await resume.wait()
        results.append(await ops.ui.render(UiRenderInput(target(), RenderNode("Text"))))
        return await next_handler(event)

    runtime.bus.register_event_handler("ui.ready", handler, {})
    task = asyncio.create_task(runtime.dispatch(UiActionInput("ui.ready", target("sock-1"))))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await runtime.drop_connection("sock-1")
        resume.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert results[0].status is RenderStatus.DELIVERY_FAILED
    assert runtime.registry.snapshot() == ()
    assert push.messages == []
    assert current_connection() is None


@pytest.mark.asyncio
async def test_connection_context_is_concurrent_and_cancel_safe() -> None:
    runtime, push = await build_runtime()
    entered, resume = asyncio.Event(), asyncio.Event()
    contexts = []

    async def handler(event, ops, next_handler):
        contexts.append(current_connection().connection_id)
        if len(contexts) == 2:
            entered.set()
        await resume.wait()
        await ops.ui.render(UiRenderInput(target(), RenderNode("Text")))
        return await next_handler(event)

    runtime.bus.register_event_handler("ui.ready", handler, {})
    tasks = [asyncio.create_task(runtime.dispatch(UiActionInput("ui.ready", target(cid))))
             for cid in ("sock-1", "sock-2")]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        tasks[0].cancel()
        resume.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1].accepted is True
    assert [message["metadata"]["ws_id"] for message in push.messages] == ["sock-2"]
    assert current_connection() is None


@pytest.mark.asyncio
async def test_closed_socket_does_not_cancel_chat_scoped_rendering() -> None:
    runtime, push = await build_runtime()
    entered, resume = asyncio.Event(), asyncio.Event()
    chat_target = RenderTarget("chat-1", "conversation.input.below", "pkg", "home")
    results = []

    async def handler(event, ops, next_handler):
        assert ops.session_context.session_id == "chat-1"
        entered.set()
        await resume.wait()
        results.append(await ops.ui.render(UiRenderInput(chat_target, RenderNode("Text"))))
        return await next_handler(event)

    runtime.bus.register_event_handler("ui.ready", handler, {})
    task = asyncio.create_task(runtime.dispatch(
        UiActionInput("ui.ready", chat_target), connection_id="sock-1",
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await runtime.drop_connection("sock-1")
        resume.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert results[0].status is RenderStatus.DELIVERED
    assert runtime.registry.get(chat_target) is not None
    assert push.messages[-1]["session_id"] == "chat-1"


@pytest.mark.asyncio
async def test_closed_connection_cannot_recreate_a_view_after_waiting_for_its_lock(monkeypatch) -> None:
    runtime, push = await build_runtime()
    entered = asyncio.Event()
    original_claim = runtime.registry.claim

    @asynccontextmanager
    async def claim(render_target):
        entered.set()
        async with original_claim(render_target) as value:
            yield value

    monkeypatch.setattr(runtime.registry, "claim", claim)
    async with original_claim(target("sock-1")):
        task = asyncio.create_task(runtime.render(UiRenderInput(target("sock-1"), RenderNode("Text"))))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await runtime.drop_connection("sock-1")
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    try:
        output = await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert output.status is RenderStatus.DELIVERY_FAILED
    assert runtime.registry.snapshot() == ()
    assert push.messages == []


@pytest.mark.asyncio
async def test_gateway_disconnect_notification_reaches_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _ = await build_runtime()
    await runtime.render(UiRenderInput(target("sock-1"), RenderNode("Text")))
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: runtime)
    adapter = WebUIDispatchAdapter()
    captured = []

    class Client:
        async def send_request(self, envelope):
            captured.append(envelope)
            response = await adapter.handle(e2a_to_agent_request(envelope))
            return SimpleNamespace(ok=response.ok, payload=response.payload)

    class Channel:
        channel_id = "web"

        def register_method(self, name, handler):
            assert name != ReqMethod.WEBUI_CONNECTION_CLOSED.value

        def on_disconnect(self, callback):
            self.disconnect = callback

        def connection_user_id(self, ws):
            return "user-1"

    channel = Channel()
    register_webui_handlers(channel, lambda: Client())
    await channel.disconnect(SimpleNamespace(_jiuwen_ws_id="sock-1"), set())
    assert len(captured) == 1
    assert e2a_to_agent_request(captured[0]).metadata["ws_id"] == "sock-1"
    assert captured[0].user_id == "user-1"
    assert runtime.registry.snapshot() == ()
