# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for /join team↔session 一致性校验与对外文案（路线 B：文案全在 gateway）。

一致性真伪交由下游 fetch_team_human_members 的 RPC 仲裁：按 team_name 直查 team.db，
输错 team → 查不到席位 → 走"不存在"文案。文案单一真相源在 _join_err_team_not_exist。
"""

from __future__ import annotations

from types import SimpleNamespace
import pytest

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
    ParsedChannelControl,
    ParsedControlAction,
)
from jiuwenswarm.gateway.message_handler.join_exit_handlers import (
    JoinExitHandlers,
    _join_err_team_not_exist,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry


# ── copy helper ──

def test_not_exist_message_contains_team() -> None:
    assert "jiuwen_team_sess_X" in _join_err_team_not_exist("jiuwen_team_sess_X")


def test_not_exist_message_contains_keyword() -> None:
    assert "不存在" in _join_err_team_not_exist("报数游戏小队")


def test_not_exist_message_fallback_for_empty_team_name() -> None:
    """team_name 空 → 文案用"未知"兜底，不崩。"""
    assert "未知" in _join_err_team_not_exist("")


# ── base name reaches register (regression for the removed local check) ──

class _FakeClient:
    def __init__(self, resp: AgentResponse) -> None:
        self._resp = resp

    async def send_request(self, _env) -> AgentResponse:
        return self._resp


def _make_msg(*, user_id: str = "ou_user1") -> Message:
    return Message(
        id="m1", type="req", channel_id="feishu", session_id="oc_chat_x",
        params={}, timestamp=0.0, ok=True, chat_id="oc_chat_x", user_id=user_id,
        metadata={"im_sender_user_id": user_id, "im_sender_name": "tester"},
    )


def _make_handler(resp: AgentResponse, registry: SessionSharingRegistry):
    client = _FakeClient(resp)
    notices: list[str] = []
    published: list = []

    async def _send_channel_notice(_uis, _ch, _sid, text):
        notices.append(text)

    async def _publish_robot_messages(msg):
        published.append(msg)

    host = SimpleNamespace(
        agent_client=client,
        get_session_sharing_registry=lambda: registry,
        send_channel_notice=_send_channel_notice,
        publish_robot_messages=_publish_robot_messages,
        extract_session_id_from_ref=MessageHandler.extract_session_id_from_ref,
        extract_team_name_from_ref=MessageHandler.extract_team_name_from_ref,
        resolve_app_id=lambda _msg: "default",
    )
    return JoinExitHandlers(host), notices


def _join_parsed(session_ref: str, member_name: str) -> ParsedChannelControl:
    return ParsedChannelControl(
        action=ParsedControlAction.JOIN_OK,
        session_ref=session_ref,
        member_name=member_name,
    )


@pytest.mark.anyio
async def test_base_team_name_reaches_register() -> None:
    """回归：自动建队持久化的 base 名（如"报数游戏小队"）必须能走到 register。

    旧本地预判用 base 名与 scoped 拼接结果比较，恒不等，导致每次 /join 都误判 mismatch。
    """
    sid = "sess_19fb0ea563b_0f180a8272f6"
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": [
            {"member_id": "player-2", "role": "human_agent"},
            {"member_id": "player-1", "role": "human_agent"},
        ]},
    )
    registry = SessionSharingRegistry()
    handler, notices = _make_handler(resp, registry)

    await handler.join_slash_handler(
        {}, "feishu", _make_msg(),
        _join_parsed(f"team_报数游戏小队_session_{sid}", "player-2"),
    )

    assert not any("不匹配" in n for n in notices), f"false mismatch: {notices}"
    assert not any("不存在" in n for n in notices), f"false not-exist: {notices}"
    subs = registry.lookup_member(sid, "player-2")
    assert len(subs) == 1
    assert subs[0].routing_key.session_id == sid
    assert subs[0].routing_key.agent_ref.id == "报数游戏小队"
    assert any("已加入 session" in n for n in notices)


@pytest.mark.anyio
async def test_scoped_team_name_still_works() -> None:
    """向后兼容：scoped 形式 team_name 仍应正常加入。"""
    sid = "sess_19f608d7a9c_f5ef621"
    scoped_name = f"jiuwen_team_{sid}"
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": [{"member_id": "player-1", "role": "human_agent"}]},
    )
    registry = SessionSharingRegistry()
    handler, notices = _make_handler(resp, registry)

    await handler.join_slash_handler(
        {}, "feishu", _make_msg(),
        _join_parsed(f"team_{scoped_name}_session_{sid}", "player-1"),
    )

    assert len(registry.lookup_member(sid, "player-1")) == 1
    assert not any("不匹配" in n for n in notices)


@pytest.mark.anyio
async def test_wrong_team_reports_not_exist() -> None:
    """输错 team（与 session 无关）→ 查不到席位 → 报"不存在"，不进 register。"""
    sid = "sess_real"
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=False, payload={"members": []},
    )
    registry = SessionSharingRegistry()
    handler, notices = _make_handler(resp, registry)

    await handler.join_slash_handler(
        {}, "feishu", _make_msg(),
        _join_parsed(f"team_完全不相关的队伍_session_{sid}", "player-1"),
    )

    assert any("不存在" in n for n in notices)
    assert registry.lookup_member(sid, "player-1") == []


@pytest.mark.anyio
async def test_member_not_in_team_reports_not_exist() -> None:
    """team 存在但 member 不在列 → 报"不存在"，不进 register。"""
    sid = "sess_x"
    resp = AgentResponse(
        request_id="r", channel_id="feishu", ok=True,
        payload={"members": [{"member_id": "player-1", "role": "human_agent"}]},
    )
    registry = SessionSharingRegistry()
    handler, notices = _make_handler(resp, registry)

    await handler.join_slash_handler(
        {}, "feishu", _make_msg(),
        _join_parsed(f"team_报数游戏小队_session_{sid}", "player-9"),
    )

    assert any("不存在" in n for n in notices)
    assert registry.lookup_member(sid, "player-9") == []
