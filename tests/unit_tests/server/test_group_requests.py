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
    name = {"group.member.add": "add_member", "group.history.get": "history"}.get(
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
async def test_group_chat_normalizes_mode_and_keeps_existing_chat_path(host):
    req = request("chat.send", group_id="group-one", query="hello", mentions=["expert"])
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
    from gateway.channel_manager.web.app_web_handlers import (
        _FORWARD_REQ_METHODS,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
    )

    assert GROUP_METHODS <= _FORWARD_REQ_METHODS
    assert GROUP_METHODS <= _FORWARD_NO_LOCAL_HANDLER_METHODS
    assert "group.member.remove" not in {method.value for method in ReqMethod}
