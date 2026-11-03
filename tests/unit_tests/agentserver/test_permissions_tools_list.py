# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime import tool_catalog


def test_permissions_tools_list_method_is_declared() -> None:
    assert getattr(ReqMethod, "PERMISSIONS_TOOLS_LIST", None) is not None


def test_normalize_permissions_tool_level_maps_guard_to_ask() -> None:
    normalize = getattr(config, "normalize_permissions_tool_level", None)
    assert callable(normalize)
    assert normalize("guard") == "ask"
    assert normalize("ASK") == "ask"
    assert normalize("allow") == "allow"
    assert normalize({"*": "deny"}) == "deny"
    assert normalize("invalid") is None


def test_build_permissions_tools_list_view_merges_runtime_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "permissions": {
                "defaults": "guard",
                "tools": {"bash": "deny", "manual_only": "allow"},
            }
        },
    )
    monkeypatch.setattr(
        config,
        "_effective_permissions",
        lambda: {
            "defaults": "guard",
            "tools": {"bash": "deny", "manual_only": "allow"},
        },
    )
    build_view = getattr(config, "build_permissions_tools_list_view", None)
    assert callable(build_view)

    payload = build_view(
        {
            "bash": {
                "name": "bash",
                "description": "Run shell commands.",
                "short_description": "Run shell commands.",
            },
            "read_file": {
                "name": "read_file",
                "description": "Read a file.",
                "short_description": "Read a file.",
            },
        }
    )

    assert payload["default_level"] == "ask"
    by_name = {item["name"]: item for item in payload["tools"]}
    assert set(by_name) == {"bash", "read_file", "manual_only"}
    assert by_name["bash"] == {
        "name": "bash",
        "short_description": "Run shell commands.",
        "level": "deny",
        "configured": True,
        "registered": True,
    }
    assert by_name["read_file"]["registered"] is True
    assert by_name["manual_only"]["registered"] is False


def test_permissions_tools_list_uses_stable_metadata_only_to_enrich_visible_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "preferred_language": "en",
            "permissions": {
                "defaults": "ask",
                "tools": {"spawn_external_cli": "allow"},
            },
        },
    )
    monkeypatch.setattr(
        config,
        "_effective_permissions",
        lambda: {"defaults": "ask", "tools": {"spawn_external_cli": "allow"}},
    )
    build_view = getattr(config, "build_permissions_tools_list_view", None)
    assert callable(build_view)

    payload = build_view({})

    by_name = {item["name"]: item for item in payload["tools"]}
    assert set(by_name) == {"spawn_external_cli"}
    assert by_name["spawn_external_cli"]["short_description"]
    assert by_name["spawn_external_cli"]["registered"] is False


def test_permissions_tools_list_rpc_and_get_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_method = getattr(ReqMethod, "PERMISSIONS_TOOLS_LIST", None)
    assert list_method is not None
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"permissions": {"defaults": "ask", "tools": {"bash": "allow"}}},
    )
    monkeypatch.setattr(
        config,
        "_effective_permissions",
        lambda: {"defaults": "ask", "tools": {"bash": "allow"}},
    )
    list_request = AgentRequest(
        request_id="list-1",
        channel_id="web",
        session_id="default",
        req_method=list_method,
        params={},
    )
    list_response = permissions_config_rpc.dispatch_permissions_config_request(
        list_request,
        get_runtime_tools_catalog=lambda: {
            "bash": {
                "name": "bash",
                "description": "Run shell.",
                "short_description": "Run shell.",
            }
        },
    )
    assert list_response.ok is True
    assert list_response.payload["default_level"] == "ask"
    assert list_response.payload["tools"][0]["registered"] is True

    get_request = AgentRequest(
        request_id="get-1",
        channel_id="web",
        session_id="default",
        req_method=ReqMethod.PERMISSIONS_TOOLS_GET,
        params={},
    )
    get_response = permissions_config_rpc.dispatch_permissions_config_request(get_request)
    assert get_response.ok is True
    assert get_response.payload == {"tools": {"bash": "allow"}}


@pytest.mark.anyio
async def test_permissions_tools_list_handler_uses_runtime_catalog_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = object()
    reload_agents_config = AsyncMock()
    manager = SimpleNamespace(
        iter_jiuwenswarm_instances=lambda: [swarm],
        reload_agents_config=reload_agents_config,
    )
    server = object.__new__(AgentWebSocketServer)
    server._agent_manager = manager

    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"permissions": {"defaults": "ask", "tools": {}}},
    )
    monkeypatch.setattr(
        config,
        "_effective_permissions",
        lambda: {"defaults": "ask", "tools": {}},
    )
    catalog_calls: list[list[object]] = []

    def collect_catalog(swarms):
        catalog_calls.append(list(swarms))
        return {
            "bash": {
                "name": "bash",
                "description": "Run shell commands.",
                "short_description": "Run shell commands.",
            }
        }

    monkeypatch.setattr(tool_catalog, "collect_tools_catalog_from_swarms", collect_catalog)
    sent: list[dict] = []

    async def send_wire(_ws, wire):
        sent.append(wire)
        return True

    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send_wire)
    request = AgentRequest(
        request_id="list-handler-1",
        channel_id="web",
        session_id="default",
        req_method=ReqMethod.PERMISSIONS_TOOLS_LIST,
        params={},
    )

    # handler 已搬到 handlers/permissions.py，改为经 ctx 直接调模块函数。
    from jiuwenswarm.server.handlers import permissions as permissions_handlers
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    class _FakeWS:
        async def send(self, payload):
            sent.append(payload)

    ctx = RequestContext(
        request=request,
        sink=WSSink(_FakeWS(), asyncio.Lock()),
        services=AgentServerServices(server),
        connection_id="test",
    )
    await permissions_handlers.handle_permissions_config(ctx)

    assert catalog_calls == [[swarm]]
    assert sent
    reload_agents_config.assert_not_awaited()
