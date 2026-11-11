# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.send 的 mcp_runtime_params / mcp_tool_scope 在注册面装载路径上的行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_request_scoped_office_claw_mcp import (
    _bare_session_adapter,
    _request_with_connectors,
    _ResourceManager,
)

import jiuwenswarm.common.mcp_server_registry as registry_mod
from jiuwenswarm.common.mcp_config import (
    CONNECTOR_NO_TOOLS,
    CONNECTOR_RUNTIME_PARAMS_INVALID,
    office_claw_tool_identity_value,
)
from jiuwenswarm.common.mcp_server_registry import (
    CachedServerRecord,
    McpRegistryChatError,
    McpRuntimeParam,
    McpServerDeclaration,
    McpServerEntry,
    build_registered_mcp_tools,
    reset_mcp_server_registry_for_tests,
    retire_request_owned_mcp_workers,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep

_INVOCATION = "OFFICE_CLAW_INVOCATION_ID"
_OFFICE_CLAW = McpServerDeclaration(
    runtime_params=(
        McpRuntimeParam(_INVOCATION, "meta"),
        McpRuntimeParam("OFFICE_CLAW_THREAD_ID", "meta", required=False),
    ),
    system=True,
)
_HEADER = McpServerDeclaration(runtime_params=(McpRuntimeParam("X-Token", "header"),))
_PLAIN = McpServerDeclaration()


@pytest.fixture
async def install(monkeypatch):
    """entries: {注册名: (工具名列表, 声明)}；返回按请求装载的协程函数。"""
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", _ResourceManager())
    registered: list[tuple] = []

    async def _install(entries, params, *, request_id="req-1", session_id="s-1", adapter=None):
        registry = SimpleNamespace(
            snapshot_for_chat=AsyncMock(return_value=[
                (name, [{"name": t, "input_params": {}} for t in tools], {"command": "node"})
                for name, (tools, _) in entries.items()
            ]),
            chat_declarations=lambda names: {n: d for n, (_, d) in entries.items()},
            get_server=AsyncMock(return_value=None),
        )
        monkeypatch.setattr(interface_deep, "get_mcp_server_registry", lambda: registry)
        request = _request_with_connectors(request_id=request_id, session_id=session_id)
        request.params.update({"mcp_server_list": list(entries), **params})
        adapter = adapter or _bare_session_adapter()
        registration = await adapter.register_request_scoped_office_claw_mcp(request)
        registered.append((adapter, registration))
        return registration

    yield _install
    for adapter, registration in registered:
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)


def _tools(registration, server: str) -> list:
    return [t for t in registration.tool_instances if t.server_name == server]


@pytest.mark.asyncio
async def test_missing_required_param_skips_only_that_server(install) -> None:
    entries = {
        "u1:feishu": (["send"], _HEADER),
        "u1:docs": (["read"], _PLAIN),
        "office-claw-single-agent": (["office_claw_list_connectors"], _OFFICE_CLAW),
    }
    registration = await install(entries, {
        "mcp_runtime_params": {
            "office-claw-single-agent": {_INVOCATION: "inv-1", "OFFICE_CLAW_THREAD_ID": "th-1"},
            "u1:docs": {"UNDECLARED": "dropped"},
            "not-listed": {"X": "ignored"},
        },
    })
    assert registration.tool_names == ("office_claw_list_connectors", "read")
    assert registration.invocation_id == "inv-1"
    (catalog,) = _tools(registration, "office-claw-single-agent")
    (docs,) = _tools(registration, "u1:docs")
    assert office_claw_tool_identity_value(catalog, _INVOCATION) == "inv-1"
    assert interface_deep._office_claw_thread_id_from_tools([docs, catalog]) == "th-1"
    assert catalog._unavailable_connectors == {"feishu": CONNECTOR_RUNTIME_PARAMS_INVALID}
    assert docs._runtime_params == {} and docs._unavailable_connectors == {}


@pytest.mark.asyncio
async def test_tool_scope_narrows_per_server(install) -> None:
    entries = {
        "office-claw": (["office_claw_list_connectors"], _OFFICE_CLAW),
        "u1:a": (["x", "y", "z"], _PLAIN),
        "u1:b": (["p"], _PLAIN),
        "u1:c": (["q"], _PLAIN),
    }
    registration = await install(entries, {
        "mcp_runtime_params": {"office-claw": {_INVOCATION: "inv-1"}},
        "mcp_tool_scope": {"u1:a": ["x", "ghost"], "u1:b": []},
    })
    assert registration.tool_names == ("office_claw_list_connectors", "x", "q")
    (catalog,) = _tools(registration, "office-claw")
    assert catalog._unavailable_connectors == {"b": CONNECTOR_NO_TOOLS}


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_params(install) -> None:
    entries = {"office-claw": (["office_claw_post_message"], _OFFICE_CLAW)}
    first, second = await asyncio.gather(*[
        install(
            entries, {"mcp_runtime_params": {"office-claw": {_INVOCATION: f"inv-{i}"}}},
            request_id=f"req-{i}", session_id=f"s-{i}",
        )
        for i in (1, 2)
    ])
    assert [r.invocation_id for r in (first, second)] == ["inv-1", "inv-2"]
    assert [
        office_claw_tool_identity_value(r.tool_instances[0], _INVOCATION) for r in (first, second)
    ] == ["inv-1", "inv-2"]
    assert first.tool_ids != second.tool_ids


@pytest.mark.asyncio
async def test_office_claw_entry_is_dropped_whole_on_name_conflict(install) -> None:
    adapter = _bare_session_adapter()
    adapter._instance.ability_manager.cards["office_claw_b"] = SimpleNamespace(id="builtin")
    entries = {
        "u1:docs": (["read"], _PLAIN),
        "office-claw": (["office_claw_a", "office_claw_b"], _OFFICE_CLAW),
    }
    registration = await install(
        entries, {"mcp_runtime_params": {"office-claw": {_INVOCATION: "inv-1"}}}, adapter=adapter,
    )
    assert registration.tool_names == ("read",)
    assert registration.invocation_id == ""


@pytest.mark.asyncio
async def test_request_owned_connection_is_closed_with_the_request(install, monkeypatch) -> None:
    declaration = McpServerDeclaration(
        runtime_params=(McpRuntimeParam("X-Cap", "header"),), connection_scope="request",
    )
    registration = await install(
        {"pptx-mcp": (["get_slide", "update_shape"], declaration), "u1:docs": (["read"], _PLAIN)},
        {"mcp_runtime_params": {"pptx-mcp": {"x-cap": "cap-1"}}},
    )
    slide = _tools(registration, "pptx-mcp")[0]
    assert slide._runtime_params == {"X-Cap": "cap-1"} and slide.request_owned_worker.startswith("r")
    retire = AsyncMock()
    pool = SimpleNamespace(retire_variant=retire)
    monkeypatch.setattr(
        registry_mod, "get_mcp_server_registry", lambda: SimpleNamespace(worker_pool=pool),
    )
    await _bare_session_adapter().cleanup_request_scoped_office_claw_mcp(registration)
    retire.assert_awaited_once_with("pptx-mcp", slide.request_owned_worker, reason="request_end")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"mcp_runtime_params": []}, {"mcp_tool_scope": "x"}, {"mcp_tool_scope": {"u1:docs": "read"}}],
)
async def test_malformed_chat_fields_fail_the_request(install, params) -> None:
    with pytest.raises(McpRegistryChatError):
        await install({"u1:docs": (["read"], _PLAIN)}, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_first", [True, False])
async def test_approval_resume_replays_with_the_resume_requests_params(install, cleanup_first) -> None:
    """审批恢复是同一会话的一条新请求：被中断的工具调用重放时必须用恢复请求的参数。"""
    adapter = _bare_session_adapter()
    entries = {"office-claw": (["office_claw_post_message"], _OFFICE_CLAW)}

    def params(invocation: str) -> dict:
        return {"mcp_runtime_params": {"office-claw": {_INVOCATION: invocation}}}

    interrupted = await install(entries, params("inv-A"), request_id="req-A", adapter=adapter)
    if cleanup_first:
        await adapter.cleanup_request_scoped_office_claw_mcp(interrupted)
    resumed = await install(entries, params("inv-B"), request_id="req-B", adapter=adapter)
    if not cleanup_first:
        await adapter.cleanup_request_scoped_office_claw_mcp(interrupted)
    card = adapter._instance.ability_manager.cards["office_claw_post_message"]
    live = interface_deep.Runner.resource_mgr.tools
    assert card.id == resumed.tool_ids[0] != interrupted.tool_ids[0]
    assert list(live) == list(resumed.tool_ids)
    assert office_claw_tool_identity_value(live[card.id], _INVOCATION) == "inv-B"
    assert resumed.invocation_id == "inv-B" and adapter._active_office_claw_mcp is resumed


@pytest.mark.asyncio
async def test_public_builder_matches_the_chat_load_path(install, monkeypatch) -> None:
    declaration = McpServerDeclaration(
        runtime_params=(McpRuntimeParam("X-Cap", "header"),), connection_scope="request",
    )
    tools = ["get_slide", "update_shape", "delete_slide"]
    chat_params = {
        "mcp_runtime_params": {"pptx-mcp": {"X-Cap": "cap-1"}},
        "mcp_tool_scope": {"pptx-mcp": ["get_slide", "update_shape"]},
    }
    loaded = await install({"pptx-mcp": (tools, declaration)}, chat_params)
    registry = reset_mcp_server_registry_for_tests()
    registry._registry["pptx-mcp"] = McpServerEntry(
        name="pptx-mcp", config={"type": "streamable-http"}, declaration=declaration,
    )
    registry._cache["pptx-mcp"] = CachedServerRecord(
        name="pptx-mcp", tools=[{"name": t, "input_params": {}} for t in tools], version=1,
        last_scan_at=0.0, last_scan_ok=True, connect_params={"command": "node"},
    )
    request = _request_with_connectors(request_id="req-1", session_id="s-1")
    built = await build_registered_mcp_tools(
        request, "pptx-mcp", {"X-Cap": "cap-1"}, ["get_slide", "update_shape"],
    )

    def shape(tool) -> tuple:
        return (tool.card.id, tool.card.name, tool.server_name, tool._runtime_params,
                tool._runtime_via, tool.request_owned_worker, tool._use_global_pool, tool._params)

    assert [shape(t) for t in built] == [shape(t) for t in loaded.tool_instances]
    assert len(await build_registered_mcp_tools(request, "pptx-mcp", {"X-Cap": "cap-1"})) == 3
    with pytest.raises(McpRegistryChatError, match="missing required runtime params: X-Cap"):
        await build_registered_mcp_tools(request, "pptx-mcp", {})
    with pytest.raises(McpRegistryChatError, match="unknown mcp server"):
        await build_registered_mcp_tools(request, "missing", {})
    retire = AsyncMock()
    monkeypatch.setattr(registry.worker_pool, "retire_variant", retire)
    await retire_request_owned_mcp_workers(built, request_id="req-1")
    retire.assert_awaited_once_with("pptx-mcp", built[0].request_owned_worker, reason="request_end")


@pytest.mark.asyncio
async def test_cleanup_releases_sessions_even_if_closing_owned_connections_fails(monkeypatch) -> None:
    released = AsyncMock()
    monkeypatch.setattr(interface_deep, "retire_request_owned_mcp_workers", AsyncMock(side_effect=RuntimeError("x")))
    monkeypatch.setattr(interface_deep, "release_request_scoped_mcp_sessions", released)
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", _ResourceManager())
    registration = interface_deep.OfficeClawMcpRegistration("req-1", (), ())
    await _bare_session_adapter().cleanup_request_scoped_office_claw_mcp(registration)
    released.assert_awaited_once_with("req-1")
