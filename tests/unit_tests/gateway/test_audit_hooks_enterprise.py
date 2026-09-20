# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""UT：session_delete / chat_interrupt / agent_push_handle / web_http_internal_error。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _CapturingAudit:
    def __init__(self) -> None:
        self.ua: list[dict[str, Any]] = []
        self.evt: list[dict[str, Any]] = []

    def emit_ua(self, **kwargs: Any) -> None:
        self.ua.append(kwargs)

    def emit_evt(self, **kwargs: Any) -> None:
        self.evt.append(kwargs)


@pytest.fixture
def audit_cap(monkeypatch: pytest.MonkeyPatch) -> _CapturingAudit:
    cap = _CapturingAudit()
    monkeypatch.setattr(
        "jiuwenswarm.common.audit_emit.emit_audit_ua",
        cap.emit_ua,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.audit_emit.emit_audit_evt",
        cap.emit_evt,
    )
    return cap


class _TestMH(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMH":
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        client = SimpleNamespace(
            server_ready=True,
            send_request=AsyncMock(
                return_value=SimpleNamespace(ok=True, payload={}, metadata=None)
            ),
        )
        return cls(client)


@pytest.mark.asyncio
async def test_agent_push_handle_emits_evt_on_parse_failure(
    audit_cap: _CapturingAudit,
) -> None:
    mh = _TestMH.create()
    await mh._handle_agent_server_push({"not": "a valid wire chunk"})
    assert len(audit_cap.evt) == 1
    assert audit_cap.evt[0]["PROC"] == "agent_push_handle"
    assert audit_cap.evt[0]["EVT"] == "agent_push_parse_failed"
    assert audit_cap.ua == []


@pytest.mark.asyncio
async def test_web_http_unary_emits_internal_error_evt(
    audit_cap: _CapturingAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway.channel_manager.web import web_http_app as app

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(app, "dispatch_http_request", _boom)
    request = MagicMock()
    request.headers = {"x-request-id": "req-err-1"}
    response = await app._unary(request, MagicMock(), "session.list", {})
    assert response.status_code == 500
    assert len(audit_cap.evt) == 1
    assert audit_cap.evt[0]["PROC"] == "web_http_internal_error"
    assert audit_cap.evt[0]["method"] == "session.list"
    assert "dispatch exploded" in audit_cap.evt[0]["MSG"]


@pytest.mark.asyncio
async def test_chat_interrupt_emits_ua_on_cancel_ok(
    audit_cap: _CapturingAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mh = _TestMH.create()

    async def _ok_cancel(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(mh, "_cancel_agent_work_for_session", _ok_cancel)
    msg = Message(
        id="req-cancel-1",
        type="req",
        channel_id="web",
        session_id="sess-1",
        params={"intent": "cancel"},
        timestamp=0.0,
        ok=True,
        req_method=ReqMethod.CHAT_CANCEL,
        user_id="user1",
    )
    await mh.publish_user_messages(msg)
    mh._running = True
    loop_task = asyncio.create_task(mh._forward_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while not audit_cap.ua and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        mh._running = False
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
    assert audit_cap.ua[0]["PROC"] == "chat_interrupt"
    assert audit_cap.ua[0]["UA"] == "chat_interrupt"
    assert audit_cap.ua[0]["UID"] == "user1"
    assert audit_cap.ua[0]["session_id"] == "sess-1"
    assert audit_cap.evt == []


@pytest.mark.asyncio
async def test_chat_interrupt_emits_evt_on_cancel_false(
    audit_cap: _CapturingAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mh = _TestMH.create()

    async def _fail_cancel(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(mh, "_cancel_agent_work_for_session", _fail_cancel)
    msg = Message(
        id="req-cancel-2",
        type="req",
        channel_id="web",
        session_id="sess-2",
        params={"intent": "cancel"},
        timestamp=0.0,
        ok=True,
        req_method=ReqMethod.CHAT_CANCEL,
        user_id="user2",
    )
    await mh.publish_user_messages(msg)
    mh._running = True
    loop_task = asyncio.create_task(mh._forward_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while not audit_cap.evt and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        mh._running = False
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
    assert len(audit_cap.evt) == 1
    assert audit_cap.evt[0]["PROC"] == "chat_interrupt"
    assert audit_cap.evt[0]["EVT"] == "chat_interrupt"
    assert audit_cap.ua == []


@pytest.mark.asyncio
async def test_session_delete_emits_ua_and_evt(
    audit_cap: _CapturingAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        WebHandlersBindParams,
        _register_web_handlers,
    )

    class _Chan:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}
            self.responses: list[dict[str, Any]] = []

        def register_method(self, name: str, handler: Any) -> None:
            self.handlers[name] = handler

        def on_connect(self, *_a: Any, **_k: Any) -> None:
            return None

        def on_disconnect(self, *_a: Any, **_k: Any) -> None:
            return None

        async def send_response(self, *_a: Any, **kwargs: Any) -> None:
            self.responses.append(kwargs)

    channel = _Chan()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))
    handler = channel.handlers["session.delete"]

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.session_index.is_remote_storage",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ws_identity_scope",
        lambda _ws: (None, None),
    )

    deleted = {"value": True}

    def _fake_delete(*_a: Any, **_k: Any) -> bool:
        return bool(deleted["value"])

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.history_store.api.delete_session_sync",
        _fake_delete,
    )

    await handler(
        MagicMock(),
        "req-del-1",
        {"session_id": "sess-del-1"},
        "sess-del-1",
        user_id="user1",
    )
    assert len(audit_cap.ua) == 1
    assert audit_cap.ua[0]["PROC"] == "session_delete"
    assert audit_cap.ua[0]["UA"] == "session_delete"
    assert audit_cap.ua[0]["UID"] == "user1"
    assert audit_cap.ua[0]["MSG"] == "deleted"

    audit_cap.ua.clear()
    deleted["value"] = False
    await handler(
        MagicMock(),
        "req-del-2",
        {"session_id": "sess-del-2"},
        "sess-del-2",
        user_id="user1",
    )
    assert len(audit_cap.evt) == 1
    assert audit_cap.evt[0]["PROC"] == "session_delete"
    assert audit_cap.evt[0]["MSG"] == "ownership_denied_or_missing"
    assert audit_cap.ua == []
