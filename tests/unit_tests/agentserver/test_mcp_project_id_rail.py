from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.hooks.mcp_project_id_rail import McpProjectIdRail
from jiuwenswarm.server.runtime.session import session_metadata


def _context(tool_name: str, arguments: object, *, mcp_scope: object) -> SimpleNamespace:
    manager = SimpleNamespace(
        _resolve_mcp_tool_scope=lambda _name: mcp_scope,
        _tools={tool_name: SimpleNamespace(input_params={
            "type": "object",
            "properties": {key: {"type": "string"} for key in (
                "content", "session_id", "project_id", "project_dir",
            )},
        })},
    )
    tool_call = SimpleNamespace(id="call-1", arguments=arguments)
    return SimpleNamespace(
        agent=SimpleNamespace(ability_manager=manager),
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=arguments,
            tool_call=tool_call,
        ),
    )


@pytest.mark.asyncio
async def test_mcp_scope_parses_json_and_overwrites_untrusted_values(monkeypatch) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda session_id: {
            "project_id": "default",
            "project_dir": "C:/workspace/project-42",
        },
    )
    raw = json.dumps(
        {
            "content": "remember this",
            "session_id": "model-session",
            "project_id": "model-project",
            "project_dir": "C:/model/path",
        }
    )
    ctx = _context(
        "mcp_example_memory_store",
        raw,
        mcp_scope=("example", "memory_store"),
    )

    await McpProjectIdRail(session_id="session-42").before_tool_call(ctx)

    expected = {
        "content": "remember this",
        "session_id": "session-42",
        "project_id": "default",
        "project_dir": "C:/workspace/project-42",
    }
    assert ctx.inputs.tool_args == expected
    assert ctx.inputs.tool_call.arguments == expected


@pytest.mark.asyncio
async def test_mcp_scope_recognizes_registered_tool_without_dot(monkeypatch) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda _session_id: {"project_id": "project-42"},
    )
    ctx = _context(
        "mcp_example_memory_store",
        {"content": "remember this"},
        mcp_scope=("example", "memory_store"),
    )

    await McpProjectIdRail(session_id="session-42").before_tool_call(ctx)

    assert ctx.inputs.tool_args["session_id"] == "session-42"
    assert ctx.inputs.tool_args["project_id"] == "project-42"


@pytest.mark.asyncio
async def test_non_mcp_dotted_tool_is_untouched(monkeypatch) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda _session_id: pytest.fail("non-MCP tool must not resolve metadata"),
    )
    arguments = {"project_id": "tool-owned-value"}
    ctx = _context("builtin.tool", arguments, mcp_scope=None)

    await McpProjectIdRail(session_id="session-42").before_tool_call(ctx)

    assert ctx.inputs.tool_args == {"project_id": "tool-owned-value"}


@pytest.mark.asyncio
async def test_mcp_without_scope_schema_is_untouched(monkeypatch) -> None:
    monkeypatch.setattr(session_metadata, "get_session_metadata",
                        lambda _sid: pytest.fail("unrelated MCP must not resolve metadata"))
    ctx = _context("mcp_browser_navigate", {"url": "https://example.com"},
                   mcp_scope=("browser", "navigate"))
    ctx.agent.ability_manager._tools[ctx.inputs.tool_name].input_params = {
        "type": "object", "properties": {"url": {"type": "string"}},
    }
    await McpProjectIdRail(session_id="session-42").before_tool_call(ctx)
    assert ctx.inputs.tool_args == {"url": "https://example.com"}


@pytest.mark.asyncio
async def test_bound_business_session_wins_over_internal_session(monkeypatch) -> None:
    calls = []

    def metadata(sid):
        calls.append(sid)
        return {"project_id": "project-42"}

    monkeypatch.setattr(session_metadata, "get_session_metadata", metadata)
    ctx = _context("mcp_example_store", {"content": "x"}, mcp_scope=("example", "store"))
    ctx.session = SimpleNamespace(get_session_id=lambda: "internal-loop-session")
    await McpProjectIdRail(session_id="business-session").before_tool_call(ctx)
    assert calls == ["business-session"]
    assert ctx.inputs.tool_args["session_id"] == "business-session"


@pytest.mark.asyncio
async def test_missing_trusted_scope_removes_model_values(monkeypatch) -> None:
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda _sid: {})
    monkeypatch.delenv("GSPD_CELIAWORK_PROJECT_ID", raising=False)
    rail = McpProjectIdRail(session_id="business-session")
    monkeypatch.setattr(rail, "_resolve_project_binding", lambda _ctx: {})
    ctx = _context("mcp_example_store", {"content": "x", "project_id": "fake", "project_dir": "fake"},
                   mcp_scope=("example", "store"))
    await rail.before_tool_call(ctx)
    assert ctx.inputs.tool_call.arguments == {"content": "x", "session_id": "business-session"}
