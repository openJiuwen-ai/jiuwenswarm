"""Wire-level group routing, authentication and provision completion."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_provisioner import (
    SessionProvisionState,
    SessionProvisionCommitTiming,
)
from jiuwenswarm.server.runtime.group_requests import dispatch_group_request
from jiuwenswarm.server.runtime.group_service import GROUP_METHODS, GroupError


def request(method, **params):
    return AgentRequest(
        request_id="request",
        req_method=ReqMethod(method),
        params=params,
        channel_id="web",
        user_id="owner",
    )


@pytest.fixture
def host():
    service = SimpleNamespace(
        execution_list=AsyncMock(return_value={"executions": []}),
        execution_get=AsyncMock(return_value={"execution_id": "exec-one"}),
        get=Mock(return_value={"group_id": "group-one"}),
        list=Mock(return_value={"groups": [], "total": 0}),
        update=AsyncMock(return_value={"name": "Updated"}),
        add_member=AsyncMock(
            return_value={"status": "registered", "event_type": "group.member.added"}
        ),
        history=AsyncMock(return_value={"messages": []}),
        delete=AsyncMock(return_value={"deleted": True}),
        owned=Mock(return_value={"mode": "team"}),
        runtime=SimpleNamespace(
            abort_session_provision=AsyncMock(), commit_session_provision=AsyncMock()
        ),
    )
    return SimpleNamespace(
        _group_service=service, get_group_service=Mock(return_value=service)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(GROUP_METHODS - {"group.create"}))
async def test_management_methods_return_wire_result(host, method):
    ws = SimpleNamespace(send=AsyncMock())
    handled = await dispatch_group_request(
        host, ws, request(method, group_id="group-one"), asyncio.Lock()
    )
    assert handled
    host.get_group_service.assert_called_once_with()
    assert len(ws.send.await_args_list) == 1
    wire = json.loads(ws.send.await_args.args[0])
    assert wire["request_id"] == "request"
    assert "response_kind" in wire
    name = {
        "group.member.add": "add_member", "group.history.get": "history",
        "group.execution.list": "execution_list", "group.execution.get": "execution_get",
    }.get(
        method, method.split(".")[1]
    )
    assert getattr(host._group_service, name).call_args.kwargs["user_id"] == "owner"


@pytest.mark.asyncio
async def test_create_commits_only_after_wire_delivery(host):
    prepared = SimpleNamespace(state=SessionProvisionState.PREPARED)
    host._group_service.prepare_create = AsyncMock(
        return_value=({"group_id": "group-one"}, prepared, True)
    )
    events = []

    async def send(wire):
        events.append("delivered")

    async def commit(*args, **kwargs):
        assert kwargs["timing"] is SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY
        events.append("committed")
        prepared.state = SessionProvisionState.COMMITTED

    host._group_service.runtime.commit_session_provision.side_effect = commit
    assert await dispatch_group_request(
        host,
        SimpleNamespace(send=send),
        request("group.create", name="Group"),
        asyncio.Lock(),
    )
    assert events == ["delivered", "committed"]
    host._group_service.runtime.abort_session_provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnected_create_aborts_and_does_not_leave_group(
    host, tmp_path, monkeypatch
):
    from jiuwenswarm.server.runtime import group_requests

    path = tmp_path / "group.json"
    path.write_text("{}")
    monkeypatch.setattr(group_requests, "group_path", lambda _: path)
    prepared = SimpleNamespace(state=SessionProvisionState.PREPARED)
    host._group_service.prepare_create = AsyncMock(
        return_value=({"group_id": "group-one"}, prepared, True)
    )
    ws = SimpleNamespace(send=AsyncMock(side_effect=ConnectionError("closed")))
    with pytest.raises(ConnectionError):
        await dispatch_group_request(
            host, ws, request("group.create", name="Group"), asyncio.Lock()
        )
    host._group_service.runtime.abort_session_provision.assert_awaited_once()
    host._group_service.runtime.commit_session_provision.assert_not_awaited()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "team.work.normal"])
async def test_group_chat_normalizes_mode_and_keeps_existing_chat_path(host, mode):
    req = request("chat.send", group_id="group-one", content="hello", mentions=["expert"])
    assert not req.session_id
    if mode is not None:
        req.params["mode"] = mode
    ws = SimpleNamespace(send=AsyncMock())
    assert not await dispatch_group_request(host, ws, req, asyncio.Lock())
    assert req.session_id == "group-one"
    assert req.params["type"] == "group_chat" and req.params["mentions"] == ["expert"]
    assert req.metadata["mode"] == "team"
    ws.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_checks_group_ownership_and_rejects_session_mismatch(host):
    ws = SimpleNamespace(send=AsyncMock())
    host._group_service.owned.side_effect = GroupError("NOT_FOUND", "group not found")
    assert await dispatch_group_request(
        host, ws, request("chat.send", group_id="group-one"), asyncio.Lock()
    )
    wire = json.loads(ws.send.await_args.args[0])
    assert "NOT_FOUND" in json.dumps(wire)
    req = request("chat.send", group_id="group-one")
    req.session_id = "another-group"
    assert await dispatch_group_request(host, ws, req, asyncio.Lock())
    assert "BAD_REQUEST" in ws.send.await_args.args[0]


def test_group_methods_are_forwarded_by_gateway_and_no_remove_method():
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_REQ_METHODS,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
    )

    assert GROUP_METHODS <= _FORWARD_REQ_METHODS
    assert GROUP_METHODS <= _FORWARD_NO_LOCAL_HANDLER_METHODS
    assert "group.member.remove" not in {method.value for method in ReqMethod}


def test_chat_send_contract_declares_optional_group_id():
    from typing import get_type_hints

    from jiuwenswarm.common.schema.chat_send import ChatSendParams

    assert "group_id" in ChatSendParams.__optional_keys__
    assert get_type_hints(ChatSendParams)["group_id"] is str


@pytest.mark.asyncio
async def test_group_only_chat_routes_through_web_gateway(host):
    from jiuwenswarm.gateway.app_gateway import _normalize_gateway_message
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = SimpleNamespace(send=AsyncMock(), remote_address=("127.0.0.1", 12345))
    received = []

    async def forward(message):
        normalized = _normalize_gateway_message(message)
        req = request("chat.send", **normalized.params)
        req.session_id = normalized.session_id
        handled = await dispatch_group_request(host, ws, req, asyncio.Lock())
        received.append((handled, req))
        return True

    channel.on_message(forward)
    await channel._handle_raw_message(ws, json.dumps({
        "type": "req", "id": "group-send", "method": "chat.send",
        "params": {"group_id": "group-one", "content": "hello", "mode": "team.work.normal"},
    }), {})
    handled, req = received[0]
    assert not handled
    assert req.session_id == "group-one"
    assert req.params["type"] == "group_chat"
    host._group_service.owned.assert_called_once_with("group-one", "owner", "web")
    ws.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_answer_is_owned_control_input_and_does_not_start_another_task(host, monkeypatch):
    from jiuwenswarm.server.runtime.group_questions import GroupQuestions

    answer = AsyncMock(return_value={"request_id": "question", "accepted": True})
    monkeypatch.setattr(GroupQuestions, "answer", answer)
    ws = SimpleNamespace(send=AsyncMock())
    req = request("chat.send", group_id="group-one", source="ask_user_interrupt", request_id="question",
                  answers=[{"question": "Budget?", "selected_options": ["Low"]}])
    assert await dispatch_group_request(host, ws, req, asyncio.Lock())
    host.get_group_service().owned.assert_called_once_with("group-one", "owner", "web")
    answer.assert_awaited_once_with("question", req.params["answers"])
    assert "accepted" in ws.send.await_args.args[0]


@pytest.mark.asyncio
async def test_group_answer_cannot_bypass_group_ownership(host, monkeypatch):
    from jiuwenswarm.server.runtime.group_questions import GroupQuestions

    answer = AsyncMock()
    monkeypatch.setattr(GroupQuestions, "answer", answer)
    host.get_group_service().owned.side_effect = GroupError("NOT_FOUND", "group not found")
    ws = SimpleNamespace(send=AsyncMock())
    req = request("chat.send", group_id="group-one", source="ask_user_interrupt", request_id="question", answers=[])
    assert await dispatch_group_request(host, ws, req, asyncio.Lock())
    answer.assert_not_awaited()
    assert "NOT_FOUND" in ws.send.await_args.args[0]
