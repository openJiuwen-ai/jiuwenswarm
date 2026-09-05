from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.hooks.mcp_project_id_rail import McpProjectIdRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.session import session_metadata


def _context(*, session_id: str | None = None, tool_args: object = None):
    """Build the small part of AgentCallbackContext used by the rail."""
    inputs = SimpleNamespace(
        tool_name="mcp.server.tool",
        tool_args=tool_args,
    )
    if session_id is not None:
        inputs.session_id = session_id
    return SimpleNamespace(inputs=inputs)


def test_resolve_project_binding_prefers_session_metadata_over_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda session_id: {
            "project_id": f"session-{session_id}",
            "project_dir": "/session/project",
        },
    )
    monkeypatch.setenv("GSPD_CELIAWORK_PROJECT_ID", "environment-project")
    token = interface_deep._CRON_TOOL_METADATA.set(
        {"project_id": "cron-project", "project_dir": "/cron/project"}
    )
    try:
        binding = McpProjectIdRail()._resolve_project_binding(
            _context(session_id="session-1")
        )
    finally:
        interface_deep._CRON_TOOL_METADATA.reset(token)

    assert binding == {
        "project_id": "session-session-1",
        "project_dir": "/session/project",
    }


def test_resolve_project_binding_fills_missing_session_fields_from_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda _session_id: {"project_id": "session-project"},
    )
    token = interface_deep._CRON_TOOL_METADATA.set(
        {"project_id": "cron-project", "project_dir": "/cron/project"}
    )
    try:
        binding = McpProjectIdRail()._resolve_project_binding(
            _context(session_id="session-1")
        )
    finally:
        interface_deep._CRON_TOOL_METADATA.reset(token)

    assert binding == {
        "project_id": "session-project",
        "project_dir": "/cron/project",
    }


def test_resolve_project_binding_falls_back_to_cron_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSPD_CELIAWORK_PROJECT_ID", "environment-project")
    token = interface_deep._CRON_TOOL_METADATA.set(
        {"project_id": "cron-project", "project_dir": "/cron/project"}
    )
    try:
        binding = McpProjectIdRail()._resolve_project_binding(_context())
    finally:
        interface_deep._CRON_TOOL_METADATA.reset(token)

    assert binding == {
        "project_id": "cron-project",
        "project_dir": "/cron/project",
    }


def test_resolve_project_binding_falls_back_to_environment_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSPD_CELIAWORK_PROJECT_ID", "environment-project")
    token = interface_deep._CRON_TOOL_METADATA.set(None)
    try:
        binding = McpProjectIdRail()._resolve_project_binding(_context())
    finally:
        interface_deep._CRON_TOOL_METADATA.reset(token)

    assert binding == {"project_id": "environment-project"}


@pytest.mark.asyncio
async def test_before_tool_call_injects_missing_project_dir_with_existing_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rail = McpProjectIdRail()
    monkeypatch.setattr(
        rail,
        "_resolve_project_binding",
        lambda _ctx: {"project_id": "resolved-project", "project_dir": "/workspace"},
    )
    ctx = _context(tool_args={"query": "hello", "project_id": "explicit-project"})

    await rail.before_tool_call(ctx)

    assert ctx.inputs.tool_args == {
        "query": "hello",
        "project_id": "explicit-project",
        "project_dir": "/workspace",
    }


def test_resolve_project_id_matches_project_binding_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda _session_id: {
            "project_id": "session-project",
            "project_dir": "/session/project",
        },
    )
    ctx = _context(session_id="session-1")
    rail = McpProjectIdRail()

    binding = rail._resolve_project_binding(ctx)

    assert rail._resolve_project_id(ctx) == binding[rail.PROJECT_ID_KEY]
