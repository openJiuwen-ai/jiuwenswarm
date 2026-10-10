# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for mcp.* handlers in AgentWebSocketServer.

Mirrors the harness.* web-channel pattern: one ReqMethod per action
(CONNECTOR_LIST / CONNECTOR_SHOW), each with its own handler
(_handle_mcp_list / _handle_mcp_show). No ``params.action``
dispatch 鈥?the method name IS the action.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def _make_request(
    session_id: str = "sess-1",
    channel_id: str = "web",
    request_id: str = "req-1",
    req_method: ReqMethod = ReqMethod.MCP_LIST,
    params: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        session_id=session_id,
        channel_id=channel_id,
        req_method=req_method,
        params=params or {},
    )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _extract_payload(wire: dict[str, Any]) -> dict[str, Any]:
    """Extract AgentResponse payload from E2A/legacy wire.

    Verified wire shape: E2A complete places AgentResponse.payload at
    ``wire["body"]["result"]``; E2A error path nests it under
    ``wire["body"]["details"]`` + ``wire["provenance.details"]``.
    """
    body = wire.get("body")
    if isinstance(body, dict):
        result = body.get("result")
        if isinstance(result, dict) and "type" in result:
            return result
        details = body.get("details")
        if isinstance(details, dict) and "type" in details:
            return details
    for key in ("response", "metadata", "payload"):
        node = wire.get(key)
        if isinstance(node, dict):
            if "payload" in node and isinstance(node["payload"], dict) and "type" in node["payload"]:
                return node["payload"]
            if "type" in node:
                return node
    return _find_payload_recursive(wire)


def _find_payload_recursive(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if data.get("type") in (
            "list",
            "detail",
            "show_failed",
            "bad_request",
            "internal_error",
        ):
            return data
        for v in data.values():
            found = _find_payload_recursive(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_payload_recursive(item)
            if found:
                return found
    return {}


class TestHandleMcpList:
    """mcp.list: list marketplace MCPs (read-only)."""

    @pytest.mark.anyio
    async def test_list_returns_marketplace_connectors(self) -> None:
        """mcp.list returns {type:list, items:[...]} from marketplace registry."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_LIST)
        send_lock = asyncio.Lock()

        fake_marketplace = [
            {
                "name": "github",
                "display_name": "GitHub",
                "category": "devops",
                "integration_type": "remote-mcp",
                "connection_state": "disconnected",
                "has_bundled_skills": False,
            },
            {
                "name": "lovrabet-cli",
                "display_name": "Lovrabet CLI",
                "category": "tools",
                "integration_type": "cli",
                "connection_state": "disconnected",
                "has_bundled_skills": True,
            },
        ]

        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.list_marketplace_mcps",
            return_value=fake_marketplace,
        ):
            await server._handle_mcp_list(ws, request, send_lock)

        assert len(ws.sent) == 1
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "list"
        assert isinstance(payload["items"], list)
        assert len(payload["items"]) == 2
        names = {item["name"] for item in payload["items"]}
        assert names == {"github", "lovrabet-cli"}

    @pytest.mark.anyio
    async def test_list_passes_filter_param(self) -> None:
        """mcp.list forwards params.filter to list_marketplace_mcps; absent
        filter defaults to builtin, unknown falls back to builtin."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        send_lock = asyncio.Lock()

        captured: dict[str, Any] = {}

        def _fake(filter: str = "builtin") -> list[dict[str, Any]]:
            captured["filter"] = filter
            return [{"name": "github", "connection_state": "disconnected"}]

        # 显式 local → 透传 local
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.list_marketplace_mcps",
            side_effect=_fake,
        ):
            await server._handle_mcp_list(
                ws,
                _make_request(req_method=ReqMethod.MCP_LIST, params={"filter": "local"}),
                send_lock,
            )
        assert captured["filter"] == "local"

        # 无 filter → 兜底 builtin
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.list_marketplace_mcps",
            side_effect=_fake,
        ):
            await server._handle_mcp_list(
                ws,
                _make_request(req_method=ReqMethod.MCP_LIST, params={}),
                send_lock,
            )
        assert captured["filter"] == "builtin"

        # 非法值 → 兜底 builtin
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.list_marketplace_mcps",
            side_effect=_fake,
        ):
            await server._handle_mcp_list(
                ws,
                _make_request(req_method=ReqMethod.MCP_LIST, params={"filter": "bogus"}),
                send_lock,
            )
        assert captured["filter"] == "builtin"


class TestHandleMcpShow:
    """mcp.show: return one MCP's detail by params.name."""

    @pytest.mark.anyio
    async def test_show_returns_connector_detail(self) -> None:
        """mcp.show with name returns {type:detail, item:{...}}."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_SHOW,
            params={"name": "github"},
        )
        send_lock = asyncio.Lock()

        fake_detail = {
            "name": "github",
            "display_name": "GitHub",
            "category": "devops",
            "integration_type": "remote-mcp",
            "connection_state": "disconnected",
            "has_bundled_skills": False,
            "description": "Clone, push, and manage repositories on GitHub.",
            "mcp_spec": {"url": "https://api.githubcopilot.com/mcp/"},
            "bundled_skills": [],
        }

        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.get_mcp",
            return_value=fake_detail,
        ):
            await server._handle_mcp_show(ws, request, send_lock)

        assert len(ws.sent) == 1
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "detail"
        assert payload["item"]["name"] == "github"
        assert payload["item"]["integration_type"] == "remote-mcp"

    @pytest.mark.anyio
    async def test_show_unknown_name_returns_not_found(self) -> None:
        """mcp.show with unknown name -> ok=False, code=MCP_NOT_FOUND."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_SHOW,
            params={"name": "nonexistent"},
        )
        send_lock = asyncio.Lock()

        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.get_mcp",
            return_value=None,
        ):
            await server._handle_mcp_show(ws, request, send_lock)

        assert len(ws.sent) == 1
        wire = json.loads(ws.sent[0])
        assert wire.get("status") == "failed"
        details = wire.get("provenance", {}).get("details", {})
        assert details.get("ok") is False
        body_details = (wire.get("body") or {}).get("details", {})
        assert body_details.get("type") == "show_failed"
        assert body_details.get("code") == "MCP_NOT_FOUND"

    @pytest.mark.anyio
    async def test_show_missing_name_returns_bad_request(self) -> None:
        """mcp.show with no name -> ok=False, code=MCP_BAD_REQUEST."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_SHOW, params={})
        send_lock = asyncio.Lock()

        await server._handle_mcp_show(ws, request, send_lock)

        assert len(ws.sent) == 1
        wire = json.loads(ws.sent[0])
        assert wire.get("status") == "failed"
        body_details = (wire.get("body") or {}).get("details", {})
        assert body_details.get("type") == "bad_request"
        assert body_details.get("code") == "MCP_BAD_REQUEST"



class TestHandleMcpConnect:
    """mcp.connect: install marketplace MCP into state.json."""

    @pytest.mark.anyio
    async def test_connect_installs_without_loading_into_agent(self) -> None:
        """mcp.connect installs + connects (state=connected) but does NOT load
        the MCP into the agent — session-level enable is per chat.send. No
        apply_mcp_change, no reload; returns type=connected."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        probed = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
            async def probe_mcp_live_connection(self_inner, name):
                probed.append(name)
                return (True, "")
            def sync_mcp_credentials(self_inner):
                return True
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "github"})
        send_lock = asyncio.Lock()
        fake_entry = {"name": "github", "transport": "http", "url": "https://api.githubcopilot.com/mcp/"}
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value=fake_entry,
        ), patch(
            "jiuwenswarm.server.agent_ws_server.get_config",
            return_value={},
        ):
            await server._handle_mcp_connect(ws, request, send_lock)
        assert len(ws.sent) == 1
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "connected"
        assert payload["name"] == "github"
        assert payload["applied"] is True
        assert probed == ["github"]  # live-connect probe ran
        assert len(applied) == 0  # not loaded into the agent

    @pytest.mark.anyio
    async def test_connect_missing_name_returns_bad_request(self) -> None:
        """mcp.connect with no name -> MCP_BAD_REQUEST."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._agent_manager = type("AM", (), {"apply_mcp_change": lambda self_inner, n, a, **kw: None})()
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={})
        await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "bad_request"
        assert payload["code"] == "MCP_BAD_REQUEST"

    @pytest.mark.anyio
    async def test_connect_credentials_required_returns_prompt_no_reload(self) -> None:
        """Form B missing token: type=credentials_required, no reload triggered."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "github"})
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value={
                "name": "github",
                "integration_type": "remote-mcp",
                "credentials_required": True,
                "required_tokens": ["GITHUB_TOKEN"],
                "credential_kind": "token",
            },
        ):
            await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "credentials_required"
        assert payload["required_tokens"] == ["GITHUB_TOKEN"]
        assert len(applied) == 0  # no entry to reload 鈥?must not reload

    @pytest.mark.anyio
    async def test_connect_credentials_required_preserves_tokens_under_real_mask(self) -> None:
        """Real _mask_sensitive_fields must not clobber required_tokens list.

        Regression: the generic masker keys on 'token' substrings and replaces
        any such key with '***'. required_tokens contains 'token' so the whole
        list was stringified to '***', breaking the frontend's .every()/.map()
        (it received a string, not an array). The credentials_required branch
        must pass item fields through unmasked (they are metadata, not secrets).
        """
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        # Use the REAL _mask_sensitive_fields 鈥?no stub.
        applied = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
        server._agent_manager = _AM()
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "github"})
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value={
                "name": "github",
                "integration_type": "remote-mcp",
                "credentials_required": True,
                "required_tokens": ["GITHUB_TOKEN", "ANOTHER_TOKEN"],
                "credential_kind": "token",
            },
        ):
            await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "credentials_required"
        assert payload["required_tokens"] == ["GITHUB_TOKEN", "ANOTHER_TOKEN"]
        assert payload["credential_kind"] == "token"

    @pytest.mark.anyio
    async def test_connect_remote_unreachable_returns_failed_and_rolls_back(self, tmp_path) -> None:
        """Remote-mcp host down: probe fails, type=connect_failed, entry rolled back, no reload."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
            async def probe_mcp_live_connection(self_inner, name):
                return (False, "tcp connect timed out")
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "github"})
        fake_entry = {
            "name": "github",
            "transport": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            "enabled": True,
            "server_id_scope": "mcp:github",
        }
        removed = []
        # rollback_failed_connect routes github to the marketplace branch only
        # when (_packages_dir()/"github").is_dir() — CI has no marketplace
        # package cache, so stub a tmp dir containing a github/ subdir so the
        # rollback reliably hits remove_mcp_record (the assertion target).
        (tmp_path / "github").mkdir()
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value=fake_entry,
        ), patch(
            "jiuwenswarm.server.runtime.mcp.state_store.remove_mcp_record",
            side_effect=lambda n: removed.append(n) or {"name": n, "removed": True},
        ), patch(
            "jiuwenswarm.server.runtime.mcp.registry._packages_dir",
            return_value=tmp_path,
        ):
            await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "connect_failed"
        assert payload["code"] == "MCP_UNREACHABLE"
        assert "timed out" in payload["error"]
        assert removed == ["github"]   # entry rolled back
        assert len(applied) == 0     # unreachable → no reload

    @pytest.mark.anyio
    async def test_connect_remote_reachable_proceeds_to_connected(self) -> None:
        """Remote-mcp host up: probe passes, state flips to connected.
        No apply_mcp_change — the MCP loads into the agent only on chat.send."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        probed = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
            async def probe_mcp_live_connection(self_inner, name):
                probed.append(name)
                return (True, "")
            def sync_mcp_credentials(self_inner):
                return True
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "github"})
        fake_entry = {
            "name": "github",
            "transport": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "server_id_scope": "mcp:github",
        }
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value=fake_entry,
        ), patch(
            "jiuwenswarm.server.agent_ws_server.get_config",
            return_value={},
        ):
            await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "connected"
        assert payload["applied"] is True
        assert probed == ["github"]  # live-connect probe ran
        assert len(applied) == 0  # not loaded into the agent

    @pytest.mark.anyio
    async def test_connect_stdio_probe_failure_rolls_back(self) -> None:
        """stdio MCP: the live-connect probe (spawn + handshake) runs at
        connect time, not deferred to chat.send. If the probe fails the entry
        is rolled back and connect_failed is reported — the user never sees a
        phantom "connected" for an MCP whose subprocess can't start."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        probed = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
            async def probe_mcp_live_connection(self_inner, name):
                probed.append(name)
                return (False, "command 'npx' not found on PATH (install it or add to PATH)")
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_CONNECT, params={"name": "context7"})
        fake_entry = {
            "name": "context7",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp"],
            "server_id_scope": "mcp:context7",
        }
        rolled_back = []
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            return_value=fake_entry,
        ), patch(
            "jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect",
            side_effect=lambda n: rolled_back.append(n),
        ):
            await server._handle_mcp_connect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "connect_failed"
        assert payload["code"] == "MCP_UNREACHABLE"
        assert "npx" in payload["error"]
        assert probed == ["context7"]  # stdio was probed at connect time
        assert rolled_back == ["context7"]  # entry rolled back
        assert len(applied) == 0


class TestHandleMcpDisconnect:
    @pytest.mark.anyio
    async def test_disconnect_removes_and_reloads(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))
        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_DISCONNECT, params={"name": "github"})
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.disconnect_mcp",
            return_value={"name": "github", "transport": "http"},
        ), patch(
            "jiuwenswarm.server.agent_ws_server.get_config",
            return_value={},
        ):
            await server._handle_mcp_disconnect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "disconnected"
        assert payload["name"] == "github"
        assert len(applied) == 1

    @pytest.mark.anyio
    async def test_disconnect_unknown_returns_not_found(self) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._agent_manager = type("AM", (), {"apply_mcp_change": lambda self_inner, n, a, **kw: None})()
        ws = _FakeWS()
        request = _make_request(req_method=ReqMethod.MCP_DISCONNECT, params={"name": "nope"})
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.disconnect_mcp",
            side_effect=KeyError("mcp 'nope' not found"),
        ):
            await server._handle_mcp_disconnect(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "disconnect_failed"
        assert payload["code"] == "MCP_NOT_FOUND"


class TestHandleMcpDeleteCustom:
    """mcp.delete_custom handler: terminal teardown + wire mapping."""

    @pytest.mark.anyio
    async def test_delete_connected_tears_down_live_server(self) -> None:
        """Connected: clear_mcp_credentials is called BEFORE delete_custom_mcp
        so _mcp_env_keys can still read the credential store; then handler
        tears down live server + refreshes skill rails."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []
        cleared = []
        refreshed = []
        order = []  # tracks call sequence

        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action, enabled))

            def clear_mcp_credentials(self_inner, name):
                cleared.append(name)
                order.append("clear")

            async def refresh_skill_rails(self_inner):
                refreshed.append(True)
                order.append("refresh")

        def _delete_stub(name):
            order.append("delete")
            return {"name": "my-mcp", "removed": True, "was_connected": True}

        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_DELETE_CUSTOM, params={"name": "my-mcp"}
        )
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.delete_custom_mcp",
            side_effect=_delete_stub,
        ):
            await server._handle_mcp_delete_custom(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "deleted"
        assert payload["name"] == "my-mcp"
        assert payload["applied"] is True
        assert applied == [("my-mcp", "remove", True)]
        assert cleared == ["my-mcp"]
        assert refreshed == [True]
        # clear must precede delete so the credential store is still intact
        # when _mcp_env_keys reads it.
        assert order == ["clear", "delete", "refresh"]

    @pytest.mark.anyio
    async def test_delete_registered_skips_live_teardown(self) -> None:
        """Registered (not connected): apply_mcp_change never called."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        applied = []

        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                applied.append((name, action))

            def clear_mcp_credentials(self_inner, name):
                pass

            async def refresh_skill_rails(self_inner):
                pass

        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_DELETE_CUSTOM, params={"name": "draft-mcp"}
        )
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.delete_custom_mcp",
            return_value={"name": "draft-mcp", "removed": True, "was_connected": False},
        ):
            await server._handle_mcp_delete_custom(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "deleted"
        assert payload["applied"] is True
        assert applied == []  # not connected -> no live teardown

    @pytest.mark.anyio
    async def test_delete_builtin_returns_bad_request(self) -> None:
        """Built-in: ValueError -> bad_request / MCP_BAD_REQUEST."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._agent_manager = type(
            "AM", (), {"apply_mcp_change": lambda self_inner, n, a, **kw: None}
        )()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_DELETE_CUSTOM, params={"name": "amap"}
        )
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.delete_custom_mcp",
            side_effect=ValueError(
                "MCP 'amap' is a built-in marketplace package and cannot be deleted"
            ),
        ):
            await server._handle_mcp_delete_custom(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "bad_request"
        assert payload["code"] == "MCP_BAD_REQUEST"

    @pytest.mark.anyio
    async def test_delete_unknown_returns_not_found(self) -> None:
        """No state record -> KeyError -> delete_failed / MCP_NOT_FOUND."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        server._agent_manager = type(
            "AM", (), {"apply_mcp_change": lambda self_inner, n, a, **kw: None}
        )()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_DELETE_CUSTOM, params={"name": "nope"}
        )
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.delete_custom_mcp",
            side_effect=KeyError("mcp 'nope' not found in state"),
        ):
            await server._handle_mcp_delete_custom(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "delete_failed"
        assert payload["code"] == "MCP_NOT_FOUND"

    @pytest.mark.anyio
    async def test_delete_apply_failure_still_reports_state_deleted(self) -> None:
        """Live teardown raises after state already deleted -> ok=True,
        applied=False with the error (server clears on next reload)."""
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.__new__(AgentWebSocketServer)

        class _AM:
            async def apply_mcp_change(self_inner, name, action, *, enabled=True, target_channel_id=None):
                raise RuntimeError("cancel-scope teardown error")

            def clear_mcp_credentials(self_inner, name):
                pass

            async def refresh_skill_rails(self_inner):
                pass

        server._agent_manager = _AM()
        server._mask_sensitive_fields = lambda item: item
        ws = _FakeWS()
        request = _make_request(
            req_method=ReqMethod.MCP_DELETE_CUSTOM, params={"name": "my-mcp"}
        )
        with patch(
            "jiuwenswarm.server.runtime.mcp.registry.delete_custom_mcp",
            return_value={"name": "my-mcp", "removed": True, "was_connected": True},
        ):
            await server._handle_mcp_delete_custom(ws, request, asyncio.Lock())
        payload = _extract_payload(json.loads(ws.sent[0]))
        assert payload["type"] == "deleted"
        assert payload["applied"] is False
        assert "cancel-scope" in payload["error"]

