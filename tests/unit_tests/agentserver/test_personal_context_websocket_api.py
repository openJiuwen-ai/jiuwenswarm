# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PersonalContext WebSocket API routing and Graph streaming tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError

from jiuwenswarm.common.e2a import wire_codec
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.gateway.routing import agent_client
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.personal_context import ws_handler
from jiuwenswarm.server.personal_context.ws_handler import (
    handle_personal_context_request,
)


def test_personal_context_does_not_extend_generic_e2a_codec_or_gateway_client() -> None:
    assert not hasattr(wire_codec, "validate_personal_context_e2a_request_dict")
    assert not hasattr(wire_codec, "encode_personal_context_request_for_wire")
    assert not hasattr(wire_codec, "encode_personal_context_response_for_wire")
    assert not hasattr(wire_codec, "encode_personal_context_chunk_for_wire")
    assert not hasattr(agent_client, "_is_personal_context_method")


def test_agent_server_delegates_personal_context_requests_to_module_handler() -> None:
    assert callable(ws_handler.handle_personal_context_request)
    assert not hasattr(AgentWebSocketServer, "_handle_personal_context_request")


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: BaseException | None = None
        self.graph: dict[str, object] = {
            "context_ready": True,
            "nodes": [],
            "edges": [],
        }
        self.tree: dict[str, object] = {
            "context_ready": True,
            "nodes": [],
            "edges": [],
        }

    def _record(self, name: str, value: object, result: object) -> object:
        if self.failure is not None:
            raise self.failure
        self.calls.append((name, value))
        return result

    async def get_status(self) -> dict[str, object]:
        return self._record("get_status", None, {"state": "RUNNING"})

    async def set_collection_enabled(self, enabled: bool) -> dict[str, object]:
        return self._record(
            "set_collection_enabled",
            enabled,
            {"collection_enabled": enabled},
        )

    async def set_agent_use_enabled(self, enabled: bool) -> dict[str, object]:
        return self._record(
            "set_agent_use_enabled",
            enabled,
            {"agent_use_enabled": enabled},
        )

    async def get_runtime_config(self) -> dict[str, object]:
        return self._record(
            "get_runtime_config",
            None,
            {"strategy_profile": "rules"},
        )

    async def patch_runtime_config(
        self,
        patch: dict[str, object],
    ) -> dict[str, object]:
        return self._record("patch_runtime_config", patch, dict(patch))

    async def select_model(self, model_index: int) -> dict[str, object]:
        if type(model_index) is not int or model_index < 0:
            raise ValueError("model_index must be a non-negative integer")
        return self._record(
            "select_model",
            model_index,
            {"model_index": model_index},
        )

    async def list_fetch_services(self) -> list[dict[str, object]]:
        return self._record(
            "list_fetch_services",
            None,
            [{"service_id": "github-main", "state": "STOPPED"}],
        )

    async def create_fetch_service(
        self,
        service: dict[str, object],
    ) -> dict[str, object]:
        return self._record("create_fetch_service", service, dict(service))

    async def delete_fetch_service(self, service_id: str) -> None:
        self._record("delete_fetch_service", service_id, None)

    async def patch_fetch_service(
        self,
        service_id: str,
        patch: dict[str, object],
    ) -> dict[str, object]:
        return self._record(
            "patch_fetch_service",
            (service_id, patch),
            {"service_id": service_id, **patch},
        )

    async def set_fetch_service_enabled(
        self,
        service_id: str,
        enabled: bool,
    ) -> None:
        self._record("set_fetch_service_enabled", (service_id, enabled), None)

    async def run_fetch(
        self,
        *,
        service_id: str | None = None,
    ) -> dict[str, object]:
        return self._record(
            "run_fetch",
            service_id,
            {
                "state": "accepted",
                "service_ids": [service_id] if service_id else ["github-main"],
            },
        )

    async def get_fetch_run_status(
        self,
        service_id: str | None = None,
    ) -> dict[str, object]:
        return self._record(
            "get_fetch_run_status",
            service_id,
            {"service_id": service_id, "state": "RUNNING"},
        )

    async def get_authorization_status(self, provider: str) -> dict[str, object]:
        return self._record(
            "get_authorization_status",
            provider,
            {
                "provider": provider,
                "state": "authorized",
                "verification_url": None,
                "expires_at": None,
                "error": None,
            },
        )

    async def authorize_provider(self, provider: str) -> dict[str, object]:
        return self._record(
            "authorize_provider",
            provider,
            {
                "provider": provider,
                "state": "authorized",
                "verification_url": None,
                "expires_at": None,
                "error": None,
            },
        )

    async def get_graph(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        return self._record("get_graph", (root_id, depth), self.graph)

    async def get_tree(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        return self._record("get_tree", (root_id, depth), self.tree)

    async def search_graph(self, query: str) -> dict[str, object]:
        return self._record("search_graph", query, {"results": []})

    async def get_graph_page(self, node_id: str) -> dict[str, object]:
        return self._record(
            "get_graph_page",
            node_id,
            {"node_id": node_id, "markdown": "# PersonalContext\n"},
        )

    async def get_source(self, source_id: str) -> dict[str, object]:
        return self._record(
            "get_source",
            source_id,
            {"source_id": source_id, "title": "Source"},
        )


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.open = True


@pytest.fixture
def capture_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _capture(ws: _FakeWebSocket, payload: dict[str, object]) -> bool:
        ws.sent.append(payload)
        return True

    monkeypatch.setattr(server_module, "send_wire_payload", _capture)
    monkeypatch.setattr(ws_handler, "send_wire_payload", _capture)


def _server() -> tuple[AgentWebSocketServer, _FakeHost]:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    host = _FakeHost()
    server._personal_context_host = host
    return server, host


def _request(
    method: ReqMethod,
    params: dict[str, object] | None = None,
    *,
    is_stream: bool = False,
) -> AgentRequest:
    return AgentRequest(
        request_id="personal-context-1",
        channel_id="web",
        req_method=method,
        params=params or {},
        is_stream=is_stream,
        agent_ref={"mode": "agent", "id": "agent-1"},
    )


def _canonical(
    method: str,
    params: dict[str, object] | None = None,
    *,
    is_stream: bool = False,
) -> str:
    return json.dumps(
        {
            "protocol_version": "1.0",
            "request_id": "personal-context-wire-1",
            "channel": "web",
            "method": method,
            "params": params or {},
            "is_stream": is_stream,
            "agent_ref": {"mode": "agent", "id": "agent-1"},
        }
    )


SERVICE_PAYLOAD: dict[str, object] = {
    "service_id": "local-files-1",
    "provider": "local_files",
    "enabled": True,
    "interval_seconds": 3_600,
    "max_items_per_run": 100,
    "time_range": {"mode": "all"},
    "source": {"root_dir": "D:/context"},
    "credentials": {},
}


PERSONAL_CONTEXT_HOST_CALLS = [
    (ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS, {}, "get_status", None, None),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_COLLECTION,
        {},
        "set_collection_enabled",
        True,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_COLLECTION,
        {},
        "set_collection_enabled",
        False,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE,
        {},
        "set_agent_use_enabled",
        True,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE,
        {},
        "set_agent_use_enabled",
        False,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_GET_CONFIG,
        {},
        "get_runtime_config",
        None,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_PATCH_CONFIG,
        {"patch": {"strategy_profile": "rules"}},
        "patch_runtime_config",
        {"strategy_profile": "rules"},
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL,
        {"model_index": 0},
        "select_model",
        0,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_LIST_SERVICES,
        {},
        "list_fetch_services",
        None,
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_CREATE_SERVICE,
        {"service": SERVICE_PAYLOAD},
        "create_fetch_service",
        SERVICE_PAYLOAD,
        SERVICE_PAYLOAD,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE,
        {"service_id": "local-files-1"},
        "delete_fetch_service",
        "local-files-1",
        {"ok": True},
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_PATCH_SERVICE,
        {"service_id": "github-main", "patch": {"interval_seconds": 10_800}},
        "patch_fetch_service",
        ("github-main", {"interval_seconds": 10_800}),
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_START_SERVICE,
        {"service_id": " github-main "},
        "set_fetch_service_enabled",
        ("github-main", True),
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_STOP_SERVICE,
        {"service_id": "github-main"},
        "set_fetch_service_enabled",
        ("github-main", False),
        None,
    ),
    (ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ALL, {}, "run_fetch", None, None),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ONE,
        {"service_id": "github-main"},
        "run_fetch",
        "github-main",
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS,
        {"service_id": "github-main"},
        "get_fetch_run_status",
        "github-main",
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS,
        {"provider": "feishu"},
        "get_authorization_status",
        "feishu",
        {
            "provider": "feishu",
            "state": "authorized",
            "verification_url": None,
            "expires_at": None,
            "error": None,
        },
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_FETCH_AUTHORIZE_PROVIDER,
        {"provider": "feishu"},
        "authorize_provider",
        "feishu",
        {
            "provider": "feishu",
            "state": "authorized",
            "verification_url": None,
            "expires_at": None,
            "error": None,
        },
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_SEARCH_PAGES,
        {"query": "  主动上下文  "},
        "search_graph",
        "主动上下文",
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_NODE,
        {"node_id": "page:topics/personal_context.md"},
        "get_graph_page",
        "page:topics/personal_context.md",
        None,
    ),
    (
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_SOURCE,
        {"source_id": "src_abc"},
        "get_source",
        "src_abc",
        {"source_id": "src_abc", "title": "Source"},
    ),
]


def test_agentserver_registers_canonical_personal_context_methods() -> None:
    assert server_module._PERSONAL_CONTEXT_REQ_METHODS == {
        item for item in ReqMethod if item.value.startswith("personal_context.")
    }
    assert len(server_module._PERSONAL_CONTEXT_REQ_METHODS) == 25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "host_method", "host_argument", "expected_result"),
    PERSONAL_CONTEXT_HOST_CALLS,
)
async def test_non_graph_methods_call_exact_host_operation(
    capture_wire: None,
    method: ReqMethod,
    params: dict[str, object],
    host_method: str,
    host_argument: object,
    expected_result: dict[str, object] | None,
) -> None:
    server, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host, ws, _request(method, params), asyncio.Lock()
    )

    assert host.calls == [(host_method, host_argument)]
    assert len(ws.sent) == 1
    assert ws.sent[0]["protocol_version"] == "1.0"
    assert ws.sent[0]["is_final"] is True
    assert ws.sent[0]["status"] == "succeeded"
    assert ws.sent[0]["agent_ref"] == {"mode": "agent", "id": "agent-1"}
    if expected_result is not None:
        response = parse_agent_server_wire_unary(ws.sent[0])
        assert response.payload == expected_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "enabled"),
    [
        (ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE, True),
        (ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE, False),
    ],
)
async def test_runtime_switch_notifies_agent_manager_after_host_success(
    capture_wire: None,
    method: ReqMethod,
    enabled: bool,
) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()
    notifications: list[tuple[bool, list[tuple[str, object]]]] = []

    async def _runtime_changed(value: bool) -> None:
        notifications.append((value, list(host.calls)))

    await handle_personal_context_request(
        host,
        ws,
        _request(method),
        asyncio.Lock(),
        runtime_enabled_changed=_runtime_changed,
    )

    assert host.calls == [("set_agent_use_enabled", enabled)]
    assert notifications == [(enabled, host.calls)]
    assert ws.sent[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_runtime_switch_callback_failure_is_fail_open(
    capture_wire: None,
) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()

    async def _failed_callback(_enabled: bool) -> None:
        raise RuntimeError("manager refresh failed")

    await handle_personal_context_request(
        host,
        ws,
        _request(ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE),
        asyncio.Lock(),
        runtime_enabled_changed=_failed_callback,
    )

    assert host.calls == [("set_agent_use_enabled", True)]
    assert ws.sent[0]["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "invalid_params", "valid_params", "valid_host_call"),
    [
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_CREATE_SERVICE,
            {},
            {"service": SERVICE_PAYLOAD},
            ("create_fetch_service", SERVICE_PAYLOAD),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_CREATE_SERVICE,
            {"service": "not-an-object"},
            {"service": SERVICE_PAYLOAD},
            ("create_fetch_service", SERVICE_PAYLOAD),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE,
            {},
            {"service_id": "local-files-1"},
            ("delete_fetch_service", "local-files-1"),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE,
            {"service_id": "   "},
            {"service_id": "local-files-1"},
            ("delete_fetch_service", "local-files-1"),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE,
            {"service_id": 1},
            {"service_id": "local-files-1"},
            ("delete_fetch_service", "local-files-1"),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS,
            {},
            {"provider": "feishu"},
            ("get_authorization_status", "feishu"),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS,
            {"provider": "   "},
            {"provider": "feishu"},
            ("get_authorization_status", "feishu"),
        ),
        (
            ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS,
            {"provider": 1},
            {"provider": "feishu"},
            ("get_authorization_status", "feishu"),
        ),
    ],
)
async def test_source_management_bad_request_keeps_connection_available(
    capture_wire: None,
    method: ReqMethod,
    invalid_params: dict[str, object],
    valid_params: dict[str, object],
    valid_host_call: tuple[str, object],
) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(method, invalid_params),
        asyncio.Lock(),
    )

    assert host.calls == []
    assert ws.open is True
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["body"]["details"]["code"] == "BAD_REQUEST"

    await handle_personal_context_request(
        host,
        ws,
        _request(method, valid_params),
        asyncio.Lock(),
    )

    assert host.calls == [valid_host_call]
    assert len(ws.sent) == 2
    assert ws.sent[1]["status"] == "succeeded"
    assert ws.sent[1]["is_final"] is True


@pytest.mark.asyncio
async def test_select_model_rejects_old_origin_index_parameter(
    capture_wire: None,
) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(ReqMethod.PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL, {"origin_index": 0}),
        asyncio.Lock(),
    )

    assert host.calls == []
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["body"]["details"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_non_graph_stream_request_is_one_completed_chunk(
    capture_wire: None,
) -> None:
    server, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS, is_stream=True),
        asyncio.Lock(),
    )

    assert host.calls == [("get_status", None)]
    assert len(ws.sent) == 1
    assert ws.sent[0]["is_stream"] is True
    assert ws.sent[0]["is_final"] is True


@pytest.mark.asyncio
async def test_graph_stream_is_bounded_ordered_and_final(
    capture_wire: None,
) -> None:
    server, host = _server()
    host.graph = {
        "context_ready": True,
        "nodes": [{"id": f"page:{index}"} for index in range(450)],
        "edges": [{"source": f"page:{index}"} for index in range(250)],
    }
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(
            ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH,
            {"root_id": None, "depth": 3},
            is_stream=True,
        ),
        asyncio.Lock(),
    )

    assert host.calls == [("get_graph", (None, 3))]
    assert [frame["sequence"] for frame in ws.sent] == list(range(7))
    assert [frame["is_final"] for frame in ws.sent] == [
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    events = [
        frame["body"]["result"] if frame["is_final"] else frame["body"]["delta"]
        for frame in ws.sent
    ]
    assert [event["event_type"] for event in events] == [
        "personal_context.context.start",
        "personal_context.context.nodes",
        "personal_context.context.nodes",
        "personal_context.context.nodes",
        "personal_context.context.edges",
        "personal_context.context.edges",
        "personal_context.context.end",
    ]
    assert events[0]["root_id"] is None
    assert events[0]["depth"] == 3
    assert all(len(event.get("nodes", [])) <= 200 for event in events)
    assert all(len(event.get("edges", [])) <= 200 for event in events)
    assert events[-1]["node_count"] == 450
    assert events[-1]["edge_count"] == 250


@pytest.mark.asyncio
async def test_tree_stream_passes_root_and_depth_to_host(capture_wire: None) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(
            ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_TREE,
            {"root_id": "page:topics", "depth": 1},
            is_stream=True,
        ),
        asyncio.Lock(),
    )

    assert host.calls == [("get_tree", ("page:topics", 1))]
    assert ws.sent[0]["body"]["delta"]["event_type"] == "personal_context.context.start"
    assert ws.sent[0]["body"]["delta"]["root_id"] == "page:topics"
    assert ws.sent[0]["body"]["delta"]["depth"] == 1
    assert ws.sent[-1]["is_final"] is True


@pytest.mark.asyncio
async def test_graph_rejects_depth_above_contract_limit(capture_wire: None) -> None:
    _server_instance, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(
            ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH,
            {"root_id": None, "depth": 11},
            is_stream=True,
        ),
        asyncio.Lock(),
    )

    assert host.calls == []
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["body"]["details"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_graph_requires_stream_request(capture_wire: None) -> None:
    server, host = _server()
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH),
        asyncio.Lock(),
    )

    assert host.calls == []
    assert ws.sent[0]["status"] == "failed"
    assert ws.sent[0]["body"]["details"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_canonical_personal_context_bypasses_chat_hooks_and_agent_creation(
    capture_wire: None,
) -> None:
    server, host = _server()
    server._trigger_before_chat_request_hook = AsyncMock(
        side_effect=AssertionError("PersonalContext must bypass chat hooks")
    )
    server._handle_unary = AsyncMock(
        side_effect=AssertionError("PersonalContext must bypass Agent creation")
    )
    ws = _FakeWebSocket()

    await server._handle_message(
        ws,
        _canonical("personal_context.runtime.status"),
        asyncio.Lock(),
    )

    assert host.calls == [("get_status", None)]
    server._trigger_before_chat_request_hook.assert_not_awaited()
    server._handle_unary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "host_call"),
    [
        ("personal_context.runtime.status", {}, ("get_status", None)),
        ("personal_context.runtime.get_config", {}, ("get_runtime_config", None)),
        ("personal_context.fetch.list_services", {}, ("list_fetch_services", None)),
        (
            "personal_context.fetch.get_run_status",
            {},
            ("get_fetch_run_status", None),
        ),
        (
            "personal_context.context.search_pages",
            {"query": "context"},
            ("search_graph", "context"),
        ),
        (
            "personal_context.context.get_node",
            {"node_id": "page:description.md"},
            ("get_graph_page", "page:description.md"),
        ),
    ],
)
async def test_canonical_wire_round_trip_for_query_methods(
    capture_wire: None,
    method: str,
    params: dict[str, object],
    host_call: tuple[str, object],
) -> None:
    server, host = _server()
    ws = _FakeWebSocket()

    await server._handle_message(ws, _canonical(method, params), asyncio.Lock())

    response = parse_agent_server_wire_unary(ws.sent[0])
    assert host.calls == [host_call]
    assert response.ok is True


@pytest.mark.asyncio
async def test_personal_context_failure_keeps_connection_and_ordinary_request_available(
    capture_wire: None,
) -> None:
    server, host = _server()
    host.failure = RuntimeError("PersonalContext start failed")
    ws = _FakeWebSocket()

    await server._handle_message(
        ws,
        _canonical("personal_context.runtime.start_collection"),
        asyncio.Lock(),
    )

    assert ws.sent[0]["response_kind"] == "e2a.error"
    assert ws.open is True

    server._trigger_before_chat_request_hook = AsyncMock()
    server._ensure_auto_team_binding_for_chat = AsyncMock()

    async def ordinary_response(
        target_ws: _FakeWebSocket,
        request: AgentRequest,
        _send_lock: asyncio.Lock,
    ) -> None:
        wire = server_module.encode_agent_response_for_wire(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"content": "ordinary agent still works"},
            ),
            response_id=request.request_id,
        )
        await server_module.send_wire_payload(target_ws, wire)

    server._handle_unary = AsyncMock(side_effect=ordinary_response)
    await server._handle_message(
        ws,
        json.dumps(
            {
                "protocol_version": "1.0",
                "request_id": "ordinary-1",
                "channel": "web",
                "method": "chat.send",
                "params": {"query": "hello"},
            }
        ),
        asyncio.Lock(),
    )

    assert ws.sent[1]["status"] == "succeeded"
    server._handle_unary.assert_awaited_once()


@pytest.mark.asyncio
async def test_core_error_is_returned_as_final_e2a_error(capture_wire: None) -> None:
    server, host = _server()
    host.failure = BaseError(StatusCode.ERROR, msg="safe PersonalContext error")
    ws = _FakeWebSocket()

    await handle_personal_context_request(
        host,
        ws,
        _request(ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS),
        asyncio.Lock(),
    )

    assert ws.sent[0]["response_kind"] == "e2a.error"
    assert ws.sent[0]["body"]["details"] == {
        "error": "safe PersonalContext error",
        "code": StatusCode.ERROR.code,
        "status": "ERROR",
    }


@pytest.mark.asyncio
async def test_handler_propagates_cancellation(capture_wire: None) -> None:
    server, host = _server()
    host.failure = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handle_personal_context_request(
            host,
            _FakeWebSocket(),
            _request(ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS),
            asyncio.Lock(),
        )
