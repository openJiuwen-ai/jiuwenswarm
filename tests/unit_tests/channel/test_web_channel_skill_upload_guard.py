# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 skills.import_upload 的 WS 入站不能绕过上传资格守卫。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.web import invoke as invoke_mod
from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

pytestmark = pytest.mark.unit


class _FakeWebSocket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.remote_address = ("127.0.0.1", 12345)
        self.request_headers = headers or {}

    async def send(self, data: str) -> None:  # pragma: no cover - 只满足接口
        return None


def _req(req_id: str, params: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id,
            "method": "skills.import_upload",
            "params": params or {},
        },
        ensure_ascii=False,
    )


def _patch_enterprise(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    import gateway.enterprise as enterprise_pkg

    monkeypatch.setattr(enterprise_pkg, "is_enterprise", lambda: enabled)
    monkeypatch.setattr(invoke_mod, "is_enterprise", lambda: enabled)


def _capture_responses(
    monkeypatch: pytest.MonkeyPatch, channel: WebChannel
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []

    async def _record(ws: Any, req_id: str, **kwargs: Any) -> None:
        responses.append({"id": req_id, **kwargs})

    monkeypatch.setattr(channel, "send_response", _record)
    return responses


@pytest.mark.asyncio
async def test_enterprise_ws_import_upload_fails_closed_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch, True)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    responses = _capture_responses(monkeypatch, channel)
    ws = _FakeWebSocket({"cookie": "jiuwenclaw_id=inst-1"})
    seen: list[Any] = []
    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        _req("req-upload", {"session_id": "s1", "path": "/tmp/a.zip"}),
        {},
    )

    assert seen == []
    assert responses[-1]["ok"] is False
    assert responses[-1]["code"] == "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE"


@pytest.mark.asyncio
async def test_enterprise_ws_import_upload_allowed_reaches_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.enterprise.capabilities.skill_upload import client as client_mod

    _patch_enterprise(monkeypatch, True)

    async def _allowed(**_kwargs: Any) -> dict[str, Any]:
        return {"allowed": True, "source": "user"}

    monkeypatch.setattr(client_mod, "query_upload_permission", _allowed)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    responses = _capture_responses(monkeypatch, channel)
    ws = _FakeWebSocket(
        {"cookie": "jiuwenclaw_id=inst-1", "authorization": "Bearer token"}
    )
    seen: list[Any] = []
    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        _req("req-upload", {"session_id": "s1", "path": "/tmp/a.zip"}),
        {},
    )

    assert len(seen) == 1
    assert all(
        response.get("code") != "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE"
        for response in responses
    )


@pytest.mark.asyncio
async def test_enterprise_ws_import_upload_uses_login_cookie_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.enterprise.capabilities.skill_upload import client as client_mod

    _patch_enterprise(monkeypatch, True)
    calls: list[dict[str, Any]] = []

    async def _allowed(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"allowed": True, "source": "user"}

    monkeypatch.setattr(client_mod, "query_upload_permission", _allowed)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    _capture_responses(monkeypatch, channel)
    # 浏览器 WS 握手带不了 Authorization 头，只能靠 Manager 同步的登录 cookie。
    ws = _FakeWebSocket(
        {"cookie": "jiuwenclaw_id=inst-1; openjiuwen_access_token=jwt-token"}
    )
    seen: list[Any] = []
    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        _req("req-upload", {"session_id": "s1", "path": "/tmp/a.zip"}),
        {},
    )

    assert len(seen) == 1
    assert calls[0]["authorization"] == "Bearer jwt-token"


@pytest.mark.asyncio
async def test_personal_ws_import_upload_skips_permission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_enterprise(monkeypatch, False)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    responses = _capture_responses(monkeypatch, channel)
    ws = _FakeWebSocket()
    seen: list[Any] = []
    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        _req("req-upload", {"session_id": "s1", "path": "/tmp/a.zip"}),
        {},
    )

    assert len(seen) == 1
    assert all(
        response.get("code") != "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE"
        for response in responses
    )
