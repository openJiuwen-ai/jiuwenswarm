"""Unit tests for cross-channel file delivery fan_out injection.

Covers ``ChannelManager._inject_file_delivery_fanout`` and
``SendFileToolkit._normalize_target_channels`` — the core of the fix that
makes ``send_file_to_user`` route files to all channels joined to a team
session (e.g. Feishu) instead of only the originating channel.
"""
from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry, SubRole


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("", []),
        ("feishu", ["feishu"]),
        ('["feishu","web"]', ["feishu", "web"]),
        (["feishu", "web"], ["feishu", "web"]),
        ([" feishu ", "", "web"], ["feishu", "web"]),
        ("reviewer-1", ["reviewer-1"]),
    ],
)
def test_normalize_target_channels(raw, expected):
    assert SendFileToolkit._normalize_target_channels(raw) == expected


# ---------- _inject_file_delivery_fanout ----------


async def test_inject_returns_none_for_non_file_event():
    reg = SessionSharingRegistry()
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1")
    result = await cm._inject_file_delivery_fanout(msg, "chat.final")
    assert result is None
    assert "fan_out_targets" not in (msg.metadata or {})


async def test_inject_returns_none_when_registry_empty():
    reg = SessionSharingRegistry()
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1")
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result is None
    # 纯 web 会话（无订阅）保持单 channel 兜底
    assert "fan_out_targets" not in (msg.metadata or {})


async def test_inject_auto_web_origin_defaults_to_godview_only():
    # web 发起：默认仅 godview（覆盖 web），不误投 feishu/xiaoyi（feishu 再需要时才发）。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    await _make_subscription(reg, "s1", "reviewer-2", "xiaoyi")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web")
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]
    assert msg.metadata["fan_out_targets"] == result


async def test_inject_respects_explicit_send_file_targets_by_channel():
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata={"send_file_targets": ["feishu"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result and result[0]["intent"] == "mention"
    assert "reviewer-1" in result[0]["member_names"]


async def test_inject_respects_explicit_send_file_targets_by_member_name():
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata={"send_file_targets": ["reviewer-1"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result and result[0]["intent"] == "mention"
    assert "reviewer-1" in result[0]["member_names"]


async def test_inject_explicit_target_no_match_falls_back_to_godview():
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata={"send_file_targets": ["dingtalk"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]


async def test_inject_explicit_target_excludes_godview_to_avoid_leak():
    # feishu 同时有 godview 订阅 + 人类成员订阅；指定 ["feishu"] 应只产出 mention target，
    # 不追加 godview intent（godview intent 全 session 广播会泄漏到 web godview）。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "feishu")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web", metadata={"send_file_targets": ["feishu"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert len(result) == 1
    assert result[0]["intent"] == "mention"
    assert "reviewer-1" in result[0]["member_names"]
    assert "GodView" not in result[0]["member_names"]


async def test_inject_auto_feishu_origin_routes_to_originator_only():
    # team 模式下 file msg 不携带发起者 member_name；按 session_id 反查最近发起者兜底定向。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    await _make_subscription(reg, "s1", "reviewer-2", "feishu")
    cm, handler = _make_channel_manager(reg)
    # 入站时记录最近发起者为 reviewer-1（feishu）
    handler._last_originators["s1"] = ("feishu", "reviewer-1")
    # file msg 不带 member_name（team 模式真实情况）
    msg = _make_file_msg("s1", channel_id="web")
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result and result[0]["intent"] == "mention"
    assert result[0]["member_names"] == ("reviewer-1",)


async def test_inject_auto_feishu_origin_without_join_falls_back_to_godview():
    # 无 member_name 且无 last-originator（web 发起 / 无人 /join）→ 仅 godview，不误投 feishu app。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="web")  # 无 member_name、无 last-originator
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]


async def test_inject_auto_web_after_feishu_does_not_leak_to_feishu():
    # web 竞态修复：feishu 用户1 要文件后 last-originator=feishu；web 紧接要文件，
    # web 入站无 member_name → 清空 last-originator → file msg 不再定向 feishu，回 godview（只投 web）。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, handler = _make_channel_manager(reg)
    # 模拟 feishu 用户1 入站后 last-originator=feishu reviewer-1
    handler._last_originators["s1"] = ("feishu", "reviewer-1")
    # web 紧接入站（无 member_name）→ message_handler 清空 last-originator
    handler._last_originators.pop("s1", None)
    # 此后 file msg 定向应回 godview，不再误投 feishu reviewer-1
    msg = _make_file_msg("s1", channel_id="web")
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]


async def test_inject_explicit_web_only_target_falls_back_to_godview():
    # 显式传 ["web"]：web 仅有 godview 订阅、无人类席位 → 无 mention 匹配 → 回退 godview。
    # （不追加显式 godview intent；godview 仅作无匹配兜底，覆盖 web。）
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="feishu", metadata={"send_file_targets": ["web"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result == [{"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}]


async def test_inject_explicit_feishu_role_with_web_drops_web():
    # 显式传 ["reviewer-1", "web"]：reviewer-1 有人类席位 → mention 定向；web 无人类席位
    # → 不追加 godview（避免 feishu godview 误收）。故只投 reviewer-1，web 不收。
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    await _make_subscription(reg, "s1", "reviewer-1", "feishu")
    cm, _ = _make_channel_manager(reg)
    msg = _make_file_msg("s1", channel_id="feishu", metadata={"send_file_targets": ["reviewer-1", "web"]})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert len(result) == 1
    assert result[0]["intent"] == "mention"
    assert result[0]["member_names"] == ("reviewer-1",)


async def test_inject_preserves_existing_fan_out_targets():
    reg = SessionSharingRegistry()
    await _make_subscription(reg, "s1", SubRole.GODVIEW, "web")
    cm, _ = _make_channel_manager(reg)
    existing = [{"intent": "mention", "mention_all": True, "member_names": [], "speaker": None}]
    msg = _make_file_msg("s1", channel_id="web", metadata={"fan_out_targets": existing})
    result = await cm._inject_file_delivery_fanout(msg, "chat.file")
    assert result is existing