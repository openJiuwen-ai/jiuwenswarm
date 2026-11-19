"""Unit tests for cross-channel file delivery fan_out injection.

Covers ``ChannelManager._inject_file_delivery_fanout`` and
``SendFileToolkit._normalize_target_channels`` — the core of the fix that
makes ``send_file_to_user`` route files to all channels joined to a team
session (e.g. Feishu) instead of only the originating channel.
"""
from __future__ import annotations

import pytest
from e2a.message import EventType, Message
from agent_gateway.channel_manager.channel_manager import ChannelManager
from agent_gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
from agent_gateway.routing.session_sharing import SessionSharingRegistry, SubRole

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit


class _FakeMessageHandler:
    """Minimal MessageHandler exposing only the accessors _inject needs."""

    def __init__(self, registry: SessionSharingRegistry) -> None:
        self._session_sharing = registry
        self._last_originators: dict[str, tuple[str, str]] = {}

    def get_session_sharing_registry(self) -> SessionSharingRegistry:
        return self._session_sharing

    def get_session_last_originator(self, session_id: str | None) -> tuple[str, str] | None:
        if not session_id:
            return None
        return self._last_originators.get(str(session_id))


async def _make_subscription(
    registry: SessionSharingRegistry, session_id: str, member_name: str, channel_id: str,
) -> None:
    rk = RoutingKey(
        user_id=f"u_{channel_id}",
        channel_id=channel_id,
        app_id="default",
        agent_ref=AgentRef("team", "default"),
        session_id=session_id,
    )
    dt = make_delivery_target(channel_id, chat_id=f"chat_{channel_id}", physical_user_id=f"u_{channel_id}")
    await registry.register(session_id, member_name, rk, dt)


def _make_channel_manager(registry: SessionSharingRegistry) -> tuple[ChannelManager, _FakeMessageHandler]:
    handler = _FakeMessageHandler(registry)
    return ChannelManager(handler), handler


def _make_file_msg(session_id: str, channel_id: str = "web", metadata: dict | None = None) -> Message:
    return Message(
        id="req-1",
        type="event",
        channel_id=channel_id,
        session_id=session_id,
        params={},
        timestamp=0.0,
        ok=True,
        payload={"event_type": "chat.file", "files": [{"path": "/tmp/a.txt", "name": "a.txt"}]},
        event_type=EventType.CHAT_FILE,
        metadata=metadata if metadata is not None else {},
    )


# ---------- _normalize_target_channels ----------


# ---------- _inject_file_delivery_fanout ----------


@pytest.mark.parametrize(
    "send_file_targets",
    [
        pytest.param(["feishu"], id="by-channel"),
        pytest.param(["reviewer-1"], id="by-member-name"),
    ],
)
async def test_inject_respects_explicit_send_file_targets(send_file_targets):
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata={"send_file_targets": send_file_targets})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result and result[0]["intent"] == "mention"
    assert "reviewer-1" in result[0]["member_names"]


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"send_file_targets": ["dingtalk"]}, id="explicit-target-no-match"),
        pytest.param(None, id="auto-origin-without-join"),
    ],
)
async def test_inject_no_matching_target_falls_back_to_godview(metadata):
    # Explicit target matching nothing, or auto web origin without a
    # last-originator (no /join): fall back to godview only, never misroute
    # to the feishu app.
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata=metadata)
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]


