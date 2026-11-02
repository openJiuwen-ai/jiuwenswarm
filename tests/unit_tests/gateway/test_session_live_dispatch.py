"""企业版团队直播帧对 Web 只投一次，不按每个 chat.send 连接号重发。"""
from __future__ import annotations

import asyncio

from jiuwenswarm.common.schema.message import Message
from gateway.routing.keys import AgentRef, ChannelKey, RoutingKey, make_delivery_target
from gateway.routing.session_sharing import (
    LogicalTarget,
    SessionDispatcher,
    SessionSharingRegistry,
    SubRole,
)


class _RecordingChannel:
    def __init__(self) -> None:
        self.sends: list[object] = []

    async def send(self, msg, routing_target=None) -> None:
        self.sends.append(routing_target)


class _Channels:
    def __init__(self, mapping: dict[ChannelKey, _RecordingChannel]) -> None:
        self._mapping = mapping
        self.enabled_channels: list[str] = []

    def get_channel(self, channel_id):
        if isinstance(channel_id, ChannelKey):
            return self._mapping.get(channel_id)
        for key, channel in self._mapping.items():
            if key.channel_id == channel_id:
                return channel
        return None


def _message(*, session_live: bool) -> Message:
    metadata: dict = {"fan_out_targets": [{"intent": "godview"}]}
    if session_live:
        metadata["delivery_scope"] = "session_live"
    return Message(
        id="live-1",
        type="event",
        channel_id="web",
        app_id="default",
        session_id="sess-live",
        params={},
        timestamp=0,
        ok=True,
        payload={"event_type": "chat.delta", "content": "我来", "session_id": "sess-live"},
        metadata=metadata,
    )


async def _registry() -> SessionSharingRegistry:
    registry = SessionSharingRegistry()
    session_id = "sess-live"
    for ws_id in ("ws-1", "ws-2", "ws-3"):
        routing_key = RoutingKey(
            user_id="web-user",
            channel_id="web",
            app_id="default",
            agent_ref=AgentRef("team", "default"),
            session_id=session_id,
        )
        await registry.register(
            session_id,
            SubRole.GODVIEW,
            routing_key,
            make_delivery_target("web", ws_id=ws_id),
        )
    feishu_key = RoutingKey(
        user_id="feishu-user",
        channel_id="feishu",
        app_id="default",
        agent_ref=AgentRef("team", "default"),
        session_id=session_id,
    )
    await registry.register(
        session_id,
        SubRole.GODVIEW,
        feishu_key,
        make_delivery_target("feishu", chat_id="oc_group"),
    )
    return registry


def _ws_ids(sends: list[object]) -> list[str]:
    return [getattr(getattr(item, "delivery", None), "ws_id", "") for item in sends]


def test_session_live_reaches_web_once_and_feishu_once() -> None:
    async def _run() -> None:
        web = _RecordingChannel()
        feishu = _RecordingChannel()
        channels = _Channels({
            ChannelKey("web", "default"): web,
            ChannelKey("feishu", "default"): feishu,
        })

        await SessionDispatcher.dispatch(
            _message(session_live=True),
            "sess-live",
            [LogicalTarget(intent="godview")],
            channels,
            await _registry(),
        )

        assert len(web.sends) == 1
        assert web.sends[0] is None
        assert len(feishu.sends) == 1

    asyncio.run(_run())


def test_without_session_live_each_web_socket_is_delivered() -> None:
    async def _run() -> None:
        web = _RecordingChannel()
        feishu = _RecordingChannel()
        channels = _Channels({
            ChannelKey("web", "default"): web,
            ChannelKey("feishu", "default"): feishu,
        })

        await SessionDispatcher.dispatch(
            _message(session_live=False),
            "sess-live",
            [LogicalTarget(intent="godview")],
            channels,
            await _registry(),
        )

        assert sorted(_ws_ids(web.sends)) == ["ws-1", "ws-2", "ws-3"]
        assert len(feishu.sends) == 1

    asyncio.run(_run())
