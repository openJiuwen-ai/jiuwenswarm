"""The host forwards the waiting line already written on a participation decision."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.gateway.routing.keys import ChannelKey
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot

_NOTICE = "好的，我先想想，马上回复你。"


def _decision(decision: str, execution_id: str = "exec_1", notice: str | None = _NOTICE):
    return SimpleNamespace(
        event_type=core.EventType.PARTICIPATION_DECIDED,
        namespace_id="local",
        request_id="req_1",
        execution_id=execution_id,
        payload=SimpleNamespace(decision=decision, notice=None if decision == "ignore" else notice),
    )


def _manager(route: RequestRouteSnapshot | None, sent: list[dict[str, object]]):
    async def sender(job: dict[str, object]) -> None:
        sent.append(job)

    manager = object.__new__(AgentManager)
    manager._thinking_notice_sender = sender
    manager._thinking_notices_sent = set()
    manager._external_event_observer = None
    manager._delivery_service = None
    manager._sessions = SimpleNamespace(
        get_request=lambda namespace_id, request_id: SimpleNamespace(
            record=SimpleNamespace(route_snapshot=route)
        )
    )
    return manager


@pytest.mark.parametrize("decision", ["respond", "intervene"])
async def test_a_reply_decision_sends_one_notice_on_the_request_channel(decision):
    sent: list[dict[str, object]] = []
    route = RequestRouteSnapshot(
        channel="feishu", target_id="oc_group", app_id="cli_app", source_message_id="om_1",
    )
    manager = _manager(route, sent)

    manager._observe_event(_decision(decision))
    manager._observe_event(_decision(decision))
    await asyncio.wait_for(_until(lambda: sent), timeout=2)

    assert len(sent) == 1
    assert sent[0]["channel"] == "feishu"
    assert sent[0]["app_id"] == "cli_app"
    assert sent[0]["chat_id"] == "oc_group"
    assert sent[0]["source_message_id"] == "om_1"
    assert sent[0]["text"] == _NOTICE
    assert sent[0]["idempotency_key"] == "thinking:exec_1"


@pytest.mark.parametrize("decision", ["ignore", "respond"])
async def test_only_a_reply_on_an_external_channel_sends_a_notice(decision):
    sent: list[dict[str, object]] = []
    route = RequestRouteSnapshot(channel="web", target_id="conv-1") if decision == "respond" else RequestRouteSnapshot(
        channel="feishu", target_id="oc_group", app_id="cli_app", source_message_id="om_1",
    )
    manager = _manager(route, sent)
    event = _decision("ignore") if decision == "ignore" else _decision("intervene", "exec_web")
    if decision == "respond":
        event = _decision("respond", "exec_web")

    manager._observe_event(event)
    await asyncio.sleep(0.05)

    assert sent == []


async def test_a_reply_without_a_core_notice_is_not_rewritten():
    sent: list[dict[str, object]] = []
    route = RequestRouteSnapshot(
        channel="feishu", target_id="oc_group", app_id="cli_app", source_message_id="om_1",
    )
    manager = _manager(route, sent)

    manager._observe_event(_decision("respond", notice=None))
    await asyncio.sleep(0.05)

    assert sent == []


class _PersistentChannel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_persistent_result(self, **kwargs):
        self.calls.append(kwargs)
        return {"outcome": "delivered", "external_message_id": "om_notice"}

    async def send(self, msg) -> None:
        raise AssertionError("persistent send is used when the channel provides it")


class _GenericChannel:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, msg) -> None:
        self.messages.append(msg)


def _channels() -> ChannelManager:
    return ChannelManager(object.__new__(MessageHandler))


async def test_a_channel_with_persistent_send_receives_the_notice_there():
    manager = _channels()
    channel = _PersistentChannel()
    manager._channels[ChannelKey("feishu", "cli_app")] = channel

    result = await manager.deliver_thinking_notice({
        "channel": "feishu",
        "app_id": "cli_app",
        "chat_id": "oc_group",
        "source_message_id": "om_1",
        "text": _NOTICE,
        "idempotency_key": "thinking:exec_1",
    })

    assert result["outcome"] == "delivered"
    assert channel.calls == [{
        "chat_id": "oc_group",
        "text": _NOTICE,
        "source_message_id": "om_1",
        "idempotency_key": "thinking:exec_1",
    }]


async def test_a_channel_without_persistent_send_uses_its_ordinary_send():
    manager = _channels()
    channel = _GenericChannel()
    manager._channels[ChannelKey("wecom", "wecom-app")] = channel

    result = await manager.deliver_thinking_notice({
        "channel": "wecom",
        "app_id": "wecom-app",
        "chat_id": "chat-9",
        "source_message_id": "msg-9",
        "text": "收到，稍等一下，我看完就回复。",
        "idempotency_key": "thinking:exec_2",
    })

    assert result["outcome"] == "delivered"
    assert result["external_message_id"] == "thinking:exec_2"
    [message] = channel.messages
    assert message.channel_id == "wecom"
    assert message.app_id == "wecom-app"
    assert message.chat_id == "chat-9"
    assert message.payload["content"] == "收到，稍等一下，我看完就回复。"


async def _until(ready) -> None:
    while not ready():
        await asyncio.sleep(0.01)
