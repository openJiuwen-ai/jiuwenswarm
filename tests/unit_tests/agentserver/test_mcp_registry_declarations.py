# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration declarations, system entry ownership and capabilities of mcp.server.*."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import jiuwenswarm.common.mcp_server_registry as registry_mod
from jiuwenswarm.common.mcp_server_registry import (
    McpRegistryChatError,
    McpRegistrySettings,
    McpServerRegistry,
    config_fingerprint,
    extract_mcp_runtime_params,
    extract_mcp_tool_scope,
    mcp_registry_capabilities,
    reset_mcp_server_registry_for_tests,
    resolve_runtime_params,
)
from jiuwenswarm.server.handlers import mcp_servers as mcp_server_handlers

_HEADER = {"name": "X-Token", "via": "header"}
_META = {"name": "CALL_ID", "via": "meta"}
_IDENTITY = {
    "OFFICE_CLAW_MCP_COMMAND": "node",
    "OFFICE_CLAW_MCP_ARGS_JSON": json.dumps(["/relay/mcp-server/dist/index.js"]),
    "OFFICE_CLAW_MCP_CWD": "/relay",
}


def _remote(name: str = "conn", **extra) -> dict:
    return {"name": name, "type": "streamable-http", "url": "https://example.com/mcp", **extra}


def _stdio(name: str = "local", **extra) -> dict:
    return {"name": name, "command": "node", "args": ["server.js"], **extra}


def _office_claw(name: str = "office-claw", **extra) -> dict:
    config = {
        "name": name,
        "command": "node",
        "args": ["/relay/mcp-server/dist/index.js"],
        "cwd": "/relay",
        "env": {"OFFICE_CLAW_API_URL": "https://127.0.0.1:3004", "OFFICE_CLAW_MCP_SHARED": "1"},
        "system": True,
    }
    return {**config, **extra}


@pytest.fixture
def seen(monkeypatch) -> list[dict]:
    configs: list[dict] = []

    async def discover(name, config):
        configs.append(dict(config))
        return [{"name": f"{name}_tool", "description": "", "input_params": {}}], {"_mcp_client_type": "x"}

    monkeypatch.setattr(registry_mod, "list_request_mcp_server_tools", discover)
    return configs


@pytest.fixture
def all_supported(monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_SUPPORTED_RUNTIME_PARAM_VIAS", frozenset({"meta", "header", "env"}))
    monkeypatch.setattr(registry_mod, "_SUPPORTED_CONNECTION_SCOPES", frozenset({"request"}))
    monkeypatch.setattr(registry_mod, "_SCAN_PARAMS_SUPPORTED", True)


@pytest.fixture
def registry() -> McpServerRegistry:
    return McpServerRegistry(settings=McpRegistrySettings())


@pytest.mark.asyncio
async def test_registration_only_fields_are_fingerprinted_but_not_discovered(registry, seen) -> None:
    added = await registry.add_servers([_remote(version="v1", system=True)], owner_id="a")
    assert added[0]["ok"] is True
    assert seen == [{"type": "streamable-http", "url": "https://example.com/mcp"}]
    listed = (await registry.list_servers())[0]["config"]
    assert listed["version"] == "v1" and listed["system"] is True
    same = await registry.update_servers([_remote(version="v1", system=True)], owner_id="a")
    assert same[0]["skipped"] is True
    changed = await registry.update_servers([_remote(version="v2", system=True)], owner_id="a")
    assert changed[0]["ok"] is True and "skipped" not in changed[0]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_declarations_are_rejected_until_supported(registry, seen, monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_SUPPORTED_RUNTIME_PARAM_VIAS", frozenset())
    monkeypatch.setattr(registry_mod, "_SUPPORTED_CONNECTION_SCOPES", frozenset())
    monkeypatch.setattr(registry_mod, "_SCAN_PARAMS_SUPPORTED", False)
    results = await registry.add_servers([
        _remote("a", runtime_params=[_HEADER]),
        _remote("b", scan_params={}),
        _remote("c", connection_scope="request"),
    ])
    assert [r["code"] for r in results] == ["INVALID_RUNTIME_PARAMS"] * 3
    assert seen == [] and await registry.list_servers() == []


@pytest.mark.parametrize(
    "server",
    [
        _remote(runtime_params=[_META]),
        _stdio(runtime_params=[_HEADER]),
        _remote(runtime_params=[_HEADER, {"name": "x-token", "via": "header"}]),
        _stdio(runtime_params=[_META, {"name": "E", "via": "env"}]),
        _stdio(runtime_params=[{**_META, "group_key": True}, {"name": "U", "via": "meta", "group_key": True}]),
        _stdio(runtime_params=[{"name": "E", "via": "env", "group_key": True}]),
        _stdio(runtime_params=[{**_META, "group_key": True, "required": False}]),
        _stdio(runtime_params=[{"name": f"P{i}", "via": "meta"} for i in range(17)]),
        _stdio(runtime_params=[{"name": "bad\nname", "via": "meta"}]),
        _stdio(runtime_params=[{"name": "P", "via": "query"}]),
        _stdio(runtime_params="CALL_ID"),
        _remote(runtime_params=[_HEADER], scan_params={"Other": "v"}),
        _remote(runtime_params=[_HEADER], scan_params={"X-Token": ""}),
        _remote(runtime_params=[_HEADER], scan_params={"X-Token": "v" * 32769}),
        _stdio(runtime_params=[_META], scan_params={"CALL_ID": "v"}),
        _stdio(runtime_params=[_META], connection_scope="request"),
        _remote(runtime_params=[_HEADER], connection_scope="session"),
        _remote(system="yes"),
        _remote("with.dot", system=True),
        _remote(version=3),
    ],
)
@pytest.mark.asyncio
async def test_invalid_declarations_are_rejected(registry, seen, all_supported, server) -> None:
    result = (await registry.add_servers([server]))[0]
    assert result["ok"] is False and result["code"] == "INVALID_RUNTIME_PARAMS"
    assert seen == []


@pytest.mark.asyncio
async def test_scan_params_are_neither_stored_nor_fingerprinted(registry, seen, all_supported) -> None:
    server = _remote(runtime_params=[_HEADER], connection_scope="request", scan_params={"X-Token": "secret-1"})
    assert (await registry.add_servers([server]))[0]["ok"] is True
    entry = registry._registry["conn"]
    assert "scan_params" not in entry.config and "secret-1" not in json.dumps(await registry.get_server("conn"))
    assert entry.declaration.connection_scope == "request"
    assert [(p.name, p.via, p.required) for p in entry.declaration.runtime_params] == [("X-Token", "header", True)]
    assert config_fingerprint(entry.config) == config_fingerprint(
        {k: v for k, v in server.items() if k not in {"name", "scan_params"}}
    )
    again = await registry.update_servers([{**server, "scan_params": {"X-Token": "secret-2"}}])
    assert again[0]["skipped"] is True


@pytest.mark.asyncio
async def test_system_entry_is_guarded_while_its_owner_is_connected(registry, seen) -> None:
    await registry.add_servers([_remote(system=True, version="v1")], owner_id="a")
    same = await registry.update_servers([_remote(system=True, version="v1")], owner_id="b")
    assert same[0]["skipped"] is True and registry._registry["conn"].owner_id == "a"
    changed = await registry.update_servers([_remote(system=True, version="v2")], owner_id="b")
    removed = await registry.remove_servers(["conn"], owner_id="b")
    assert changed[0]["code"] == removed[0]["code"] == "SYSTEM_ENTRY_OWNER_CONFLICT"
    assert registry._registry["conn"].config["version"] == "v1"
    registry.release_owner("a")
    taken = await registry.update_servers([_remote(system=True, version="v2")], owner_id="b")
    assert taken[0]["ok"] is True and registry._registry["conn"].owner_id == "b"
    stale = await registry.update_servers([_remote(system=True, version="v3")], owner_id="a")
    assert stale[0]["code"] == "SYSTEM_ENTRY_OWNER_CONFLICT"
    assert (await registry.remove_servers(["conn"], owner_id="b"))[0]["ok"] is True


@pytest.mark.asyncio
async def test_released_system_entry_is_adopted_by_an_identical_registration(registry, seen) -> None:
    await registry.add_servers([_remote(system=True)], owner_id="a")
    registry.release_owner("a")
    assert registry._registry["conn"].owner_id == ""
    assert (await registry.update_servers([_remote(system=True)], owner_id="b"))[0]["skipped"] is True
    assert registry._registry["conn"].owner_id == "b"


@pytest.mark.asyncio
async def test_user_connectors_have_no_owner(registry, seen) -> None:
    await registry.add_servers([_remote()], owner_id="a")
    assert registry._registry["conn"].owner_id == ""
    assert (await registry.update_servers([_remote(version="v2")], owner_id="b"))[0]["ok"] is True
    assert (await registry.remove_servers(["conn"], owner_id="c"))[0]["ok"] is True


@pytest.mark.asyncio
async def test_office_claw_entries_keep_startup_identity_check(registry, seen, monkeypatch) -> None:
    for key, value in _IDENTITY.items():
        monkeypatch.setenv(key, value)
    ok = await registry.add_servers([_office_claw(), _office_claw("office-claw-single-agent")], owner_id="a")
    assert [r["ok"] for r in ok] == [True, True]
    assert seen[0]["env"]["OFFICE_CLAW_MCP_SHARED"] == "1" and "system" not in seen[0]
    bad = await registry.update_servers([
        _office_claw(args=["/tmp/other.js"]),
        _office_claw("office-claw-single-agent", env={"NODE_OPTIONS": "--require /tmp/x.js"}),
    ], owner_id="a")
    assert [r["code"] for r in bad] == ["SYSTEM_ENTRY_IDENTITY_MISMATCH"] * 2
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_office_claw_entry_is_rejected_without_startup_identity(registry, seen, monkeypatch) -> None:
    for key in _IDENTITY:
        monkeypatch.delenv(key, raising=False)
    result = (await registry.add_servers([_office_claw()]))[0]
    assert result["code"] == "SYSTEM_ENTRY_IDENTITY_MISMATCH" and seen == []


@pytest.mark.asyncio
async def test_scan_failure_reports_code(registry, monkeypatch) -> None:
    async def discover(name, config):
        return [], {}
    monkeypatch.setattr(registry_mod, "list_request_mcp_server_tools", discover)
    result = (await registry.add_servers([_remote()]))[0]
    assert result == {"name": "conn", "ok": False, "error": "discovery failed", "code": "SCAN_FAILED"}


def test_capabilities_report_only_enabled_features(monkeypatch) -> None:
    assert mcp_registry_capabilities() == {
        "runtime_params": ["meta", "header", "env"],
        "scan_params": True,
        "tool_scope": True,
        "connection_scope": ["request"],
        "rescan": True,
        "system_entries": True,
    }
    monkeypatch.setattr(registry_mod, "_SUPPORTED_RUNTIME_PARAM_VIAS", frozenset({"env"}))
    assert mcp_registry_capabilities()["runtime_params"] == ["env"]


@pytest.mark.asyncio
async def test_handlers_pass_connection_and_list_capabilities(seen, monkeypatch) -> None:
    registry = reset_mcp_server_registry_for_tests()
    monkeypatch.setattr(
        mcp_server_handlers, "encode_agent_response_for_wire",
        lambda resp, response_id: {"ok": resp.ok, "payload": resp.payload},
    )
    sent: list = []

    def ctx(params: dict, connection_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            request=SimpleNamespace(params=params, request_id="r", channel_id="officeclaw"),
            sink=SimpleNamespace(send_wire=AsyncMock(side_effect=sent.append)),
            connection_id=connection_id,
        )

    await mcp_server_handlers.handle_mcp_server_add(ctx({"servers": [_remote(system=True)]}, "ws-1"))
    assert registry._registry["conn"].owner_id == "ws-1"
    await mcp_server_handlers.handle_mcp_server_update(
        ctx({"servers": [_remote(system=True, version="v2")]}, "ws-2")
    )
    await mcp_server_handlers.handle_mcp_server_remove(ctx({"names": ["conn"]}, "ws-2"))
    assert [sent[i]["payload"]["results"][0]["code"] for i in (1, 2)] == ["SYSTEM_ENTRY_OWNER_CONFLICT"] * 2
    await mcp_server_handlers.handle_mcp_server_list(ctx({}, "ws-2"))
    assert sent[3]["payload"]["capabilities"] == mcp_registry_capabilities()
    assert [s["name"] for s in sent[3]["payload"]["servers"]] == ["conn"]


def _declaration(*params: dict) -> registry_mod.McpServerDeclaration:
    return registry_mod.McpServerDeclaration(
        runtime_params=tuple(registry_mod.McpRuntimeParam(**p) for p in params)
    )


@pytest.mark.parametrize(
    "field,extract",
    [("mcp_runtime_params", extract_mcp_runtime_params), ("mcp_tool_scope", extract_mcp_tool_scope)],
)
def test_chat_fields_default_empty_and_reject_non_objects(field, extract) -> None:
    assert extract({}) == {}
    assert extract({field: None}) == {}
    with pytest.raises(McpRegistryChatError, match=field):
        extract({field: ["conn"]})


def test_tool_scope_keeps_empty_lists_and_rejects_bad_values() -> None:
    scope = extract_mcp_tool_scope({"mcp_tool_scope": {"a": ["x", " y ", ""], "b": []}})
    assert scope == {"a": frozenset({"x", "y"}), "b": frozenset()}
    for bad in ("x", [1], None):
        with pytest.raises(McpRegistryChatError, match="mcp_tool_scope"):
            extract_mcp_tool_scope({"mcp_tool_scope": {"a": bad}})


def test_runtime_params_resolve_by_declaration() -> None:
    declaration = _declaration(
        {"name": "CALL_ID", "via": "meta"}, {"name": "API_URL", "via": "meta", "required": False}
    )
    assert resolve_runtime_params(declaration, {"CALL_ID": "c1"}) == ({"CALL_ID": "c1"}, "", [])
    values, reason, dropped = resolve_runtime_params(
        declaration, {"CALL_ID": "c1", "API_URL": "u", "call_id": "x", "OTHER": "y"}
    )
    assert (values, reason, dropped) == ({"CALL_ID": "c1", "API_URL": "u"}, "", ["call_id", "OTHER"])
    assert resolve_runtime_params(registry_mod.McpServerDeclaration(), None) == ({}, "", [])


def test_runtime_param_header_names_ignore_case() -> None:
    declaration = _declaration({"name": "X-Token", "via": "header"})
    assert resolve_runtime_params(declaration, {"x-token": "t"}) == ({"X-Token": "t"}, "", [])


@pytest.mark.parametrize(
    "raw,reason",
    [
        (None, "missing required runtime params: CALL_ID"),
        ({"API_URL": "u"}, "missing required runtime params: CALL_ID"),
        ("c1", "must be an object"),
        ({"CALL_ID": 1}, "invalid value for runtime param: CALL_ID"),
        ({"CALL_ID": ""}, "invalid value for runtime param: CALL_ID"),
        ({"CALL_ID": "c1", "API_URL": None}, "invalid value for runtime param: API_URL"),
        ({"CALL_ID": "x" * (registry_mod.MAX_RUNTIME_PARAM_VALUE_LEN + 1)}, "invalid value"),
        (
            {"CALL_ID": "x" * 32768, "API_URL": "y" * 32768, "EXTRA": "z"},
            "total length limit",
        ),
    ],
)
def test_runtime_params_skip_reasons_never_echo_values(raw, reason) -> None:
    declaration = _declaration(
        {"name": "CALL_ID", "via": "meta"},
        {"name": "API_URL", "via": "meta", "required": False},
        {"name": "EXTRA", "via": "meta", "required": False},
    )
    values, got, _dropped = resolve_runtime_params(declaration, raw)
    assert values == {} and reason in got
    assert "xxx" not in got and "c1" not in got


@pytest.mark.asyncio
async def test_chat_declarations_expose_registered_entries(seen, all_supported) -> None:
    registry = McpServerRegistry(McpRegistrySettings())
    await registry.add_servers([_stdio("local", runtime_params=[_META], system=True), _remote()])
    declarations = registry.chat_declarations(["local", "conn", "missing"])
    assert set(declarations) == {"local", "conn"}
    assert declarations["local"].system and declarations["local"].runtime_params[0].name == "CALL_ID"
    assert declarations["conn"] == registry_mod.McpServerDeclaration()


@pytest.mark.asyncio
async def test_office_claw_env_runtime_params_follow_the_startup_env_whitelist(
    registry, seen, all_supported, monkeypatch,
) -> None:
    for key, value in _IDENTITY.items():
        monkeypatch.setenv(key, value)
    injected = await registry.add_servers([
        _office_claw(runtime_params=[{"name": "NODE_OPTIONS", "via": "env"}]),
        _office_claw("office-claw-single-agent", runtime_params=[{"name": "OFFICE_CLAW_THREAD_ID", "via": "env"}]),
    ], owner_id="a")
    assert [r["code"] for r in injected] == ["INVALID_RUNTIME_PARAMS"] * 2 and seen == []
    allowed = await registry.add_servers([
        _office_claw(runtime_params=[{"name": "OFFICE_CLAW_INVOCATION_ID", "via": "env"}]),
        _stdio(runtime_params=[{"name": "NODE_OPTIONS", "via": "env"}]),
    ], owner_id="a")
    assert [r["ok"] for r in allowed] == [True, True]
