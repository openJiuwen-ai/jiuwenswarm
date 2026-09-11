"""复用本机会话时的渠道 mode 注入规则（_apply_channel_state）。

背景：手机控 PC（xiaoyi conv_* 反查命中 desktop_* 会话）等跨端续聊场景，
复用即会话已锁定 mode（team / design.team / code.* 等）。注入闸门只看
``mode_from_command``（/mode、/switch 指令置位）：无命令史不注入，让会话
锁定的 metadata mode 接管；有命令史保留注入——/mode 切换是既有特性。
config.yaml 的 default_mode 只是渠道级默认偏好，不构成对复用会话的显式
意图（注入会被 AgentServer 当显式覆盖 metadata.mode，腐蚀 team 锁定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from collections.abc import AsyncIterator

import pytest

from jiuwenswarm.common.schema import Message
from jiuwenswarm.gateway.message_handler.external_conv_session import to_local_conv_id
from jiuwenswarm.gateway.message_handler.message_handler import (
    ChannelMode,
    MessageHandler,
)


class _FakeAgentClient:
    sent_requests: list[object] = []

    @staticmethod
    async def send_request(env: object) -> SimpleNamespace:
        _FakeAgentClient.sent_requests.append(env)
        raise AssertionError("session.create must not run when conv maps to desktop")

    @staticmethod
    async def send_request_stream(env: object) -> AsyncIterator[object]:
        if False:  # pragma: no cover
            yield env
        return


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        _FakeAgentClient.sent_requests = []
        return cls(_FakeAgentClient())


def _make_msg(session_id: str, *, metadata: dict | None = None) -> Message:
    return Message(
        id="req-1",
        type="req",
        channel_id="xiaoyi",
        session_id=session_id,
        params={"query": "继续"},
        timestamp=1.0,
        ok=True,
        metadata=metadata or {},
    )


def _setup_reused_desktop_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None = None
) -> tuple[str, str]:
    desktop = "desktop_1a061727c1f_65690918081e"
    conv = to_local_conv_id(desktop)
    desk_dir = tmp_path / desktop
    desk_dir.mkdir()
    metadata = {
        "session_id": desktop,
        "title": "今天天气真好",
        "last_message_at": 100.0,
        "channel_id": "desktop",
    }
    if mode is not None:
        metadata["mode"] = mode
    (desk_dir / "metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    return desktop, conv


@pytest.mark.asyncio
async def test_reused_session_marks_reused_and_skips_default_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用 desktop 会话：打 reused_local_session 标记，且未显式切 mode 时不注入。"""
    desktop, conv = _setup_reused_desktop_session(tmp_path, monkeypatch)

    handler = _TestMessageHandler.create()
    msg = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(msg)

    assert msg.session_id == desktop
    assert msg.metadata["external_session_id"] == conv
    assert msg.metadata["reused_local_session"] is True

    handler._apply_channel_state(msg)
    assert "mode" not in msg.params
    assert "mode" not in msg.metadata


@pytest.mark.asyncio
async def test_reused_session_cached_alias_still_skips_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二轮命中别名缓存（不再走反查）时，复用标记仍然生效。"""
    desktop, conv = _setup_reused_desktop_session(tmp_path, monkeypatch)

    handler = _TestMessageHandler.create()
    first = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(first)

    second = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(second)
    assert second.session_id == desktop
    assert second.metadata["reused_local_session"] is True

    handler._apply_channel_state(second)
    assert "mode" not in second.params


@pytest.mark.asyncio
async def test_reused_session_with_command_mode_still_injects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用会话 + 用户用 /mode 指令显式切过：保留注入（/mode 切换是既有特性）。

    mode_from_command 只由 /mode、/switch 指令置位；config default_mode 不置位。
    """
    desktop, conv = _setup_reused_desktop_session(tmp_path, monkeypatch)

    handler = _TestMessageHandler.create()
    msg = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(msg)

    state = handler.get_or_create_channel_state(msg)
    state.mode = ChannelMode.TEAM
    state.mode_explicit = True
    state.mode_from_command = True  # 模拟 /mode 指令置位

    handler._apply_channel_state(msg)
    assert msg.params["mode"] == "team"
    assert msg.metadata["mode"] == "team"


def test_fresh_session_injects_default_mode() -> None:
    """非复用（无 reused 标记）：照旧注入渠道默认 mode。"""
    handler = _TestMessageHandler.create()
    msg = _make_msg("xiaoyi_sess_new")
    handler._apply_channel_state(msg)
    assert msg.params["mode"] == "agent"
    assert msg.metadata["mode"] == "agent"


def test_config_default_mode_not_explicit_for_reused_session() -> None:
    """config.yaml 的 default_mode 只是渠道默认偏好，对复用已锁定会话不构成显式意图。"""
    handler = _TestMessageHandler.create()
    handler._get_config_raw = lambda: {
        "channels": {"xiaoyi": {"default_mode": "team"}}
    }
    msg = _make_msg(
        "xiaoyi_sess_cfg",
        metadata={"external_session_id": "conv_x", "reused_local_session": True},
    )
    handler._apply_channel_state(msg)
    assert "mode" not in msg.params
    assert "mode" not in msg.metadata


def test_slash_mode_command_sets_explicit_flag() -> None:
    """/mode 处理器经由 state.mode_explicit / mode_from_command 标记显式意图。"""
    handler = _TestMessageHandler.create()
    msg = _make_msg("xiaoyi_sess_flag")
    state = handler.get_or_create_channel_state(msg)
    assert state.mode_explicit is False
    assert state.mode_from_command is False
    assert state.mode is ChannelMode.AGENT


@pytest.mark.asyncio
async def test_reused_team_session_registers_godview_without_injected_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用 team 会话 + params 无 mode（复用保护不注入）→ 按磁盘锁定 mode 注册 GodView。

    断链实证：复用保护下 params.mode 为空，旧判定 is_team_mode('')=False 且会话
    无订阅 → 不注册 → 服务端 leader 内容帧（fan_out=godview）在
    dispatch_to_session 静默丢弃，手机端收不到正文。
    """
    desktop, conv = _setup_reused_desktop_session(tmp_path, monkeypatch, mode="design.team")
    handler = _TestMessageHandler.create()
    msg = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(msg)
    handler._apply_channel_state(msg)
    assert "mode" not in msg.params  # 复用保护仍然生效

    await handler._maybe_register_godview(msg)

    from jiuwenswarm.gateway.routing.session_sharing import SubRole

    subs = handler._session_sharing.lookup_member(desktop, SubRole.GODVIEW)
    assert subs, "team 会话应注册 GodView"
    assert subs[0].routing_key.channel_id == "xiaoyi"


@pytest.mark.asyncio
async def test_reused_non_team_session_does_not_register_godview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复用非 team 会话（锁定 mode=agent）→ 不注册 GodView（不扩大注册面）。"""
    desktop, conv = _setup_reused_desktop_session(tmp_path, monkeypatch, mode="agent")
    handler = _TestMessageHandler.create()
    msg = _make_msg(conv, metadata={"xiaoyi_session_id": conv})
    await handler._resolve_external_channel_session(msg)
    handler._apply_channel_state(msg)

    await handler._maybe_register_godview(msg)

    assert not handler._session_sharing.lookup_all(desktop)
