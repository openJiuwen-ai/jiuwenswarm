"""Regression tests for MCP timeout forwarding into openjiuwen clients."""

from __future__ import annotations

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

import openjiuwen.core.foundation.tool.mcp.client.sse_client as sse_module
import openjiuwen.core.foundation.tool.mcp.client.streamable_http_client as streamable_module
import openjiuwen.core.runner.resources_manager.tool_manager as tool_manager_module

from jiuwenswarm.server.runtime.mcp import call_timeout_patch


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    async def connect(self, *args, **kwargs):
        self.calls.append(("connect", kwargs.get("timeout")))
        return True

    async def call_tool(self, *args, **kwargs):
        self.calls.append(("call_tool", kwargs.get("timeout")))
        return "ok"

    async def list_tools(self, *args, **kwargs):
        self.calls.append(("list_tools", kwargs.get("timeout")))
        return []


def _install_fakes(monkeypatch) -> tuple[type[_FakeClient], type]:
    class FakeStreamable(_FakeClient):
        pass

    class FakeSse(_FakeClient):
        pass

    class FakeToolMgr:
        @staticmethod
        def _create_client(config):
            return FakeSse()

    monkeypatch.setattr(streamable_module, "StreamableHttpClient", FakeStreamable)
    monkeypatch.setattr(sse_module, "SseClient", FakeSse)
    monkeypatch.setattr(tool_manager_module, "ToolMgr", FakeToolMgr)
    return FakeSse, FakeToolMgr


@pytest.fixture(autouse=True)
def _reset_patch_state():
    call_timeout_patch._PATCHED = False
    call_timeout_patch._wrapped_methods.clear()
    yield
    call_timeout_patch._PATCHED = False
    call_timeout_patch._wrapped_methods.clear()


@pytest.mark.asyncio
async def test_configured_timeout_reaches_connect_and_calls(monkeypatch) -> None:
    fake_sse, _ = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_sse()
    client._jws_call_timeout = 3600
    await client.connect()
    await client.call_tool("demo", {})
    await client.list_tools()

    assert client.calls == [
        ("connect", 3600.0),
        ("call_tool", 3600.0),
        ("list_tools", 3600.0),
    ]


@pytest.mark.asyncio
async def test_unconfigured_timeout_keeps_connect_default(monkeypatch) -> None:
    fake_sse, _ = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_sse()
    await client.connect()
    await client.call_tool("demo", {})

    assert client.calls == [
        ("connect", None),
        ("call_tool", call_timeout_patch.DEFAULT_CALL_TIMEOUT),
    ]


def test_create_client_stamps_configured_timeout(monkeypatch) -> None:
    _, fake_tool_mgr = _install_fakes(monkeypatch)
    call_timeout_patch.apply_mcp_call_timeout_patch()

    client = fake_tool_mgr._create_client(
        SimpleNamespace(params={"timeout_s": 3600})
    )

    assert client._jws_call_timeout == 3600.0
