# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Minimal chat.send + catalog RPC tests: MCP union, mount, gates, routing."""

from __future__ import annotations

import typing
from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.chat_send import ChatSendParams
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
    CLI_FORWARD_REQ_METHODS,
)
from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime import extension_package_manager as catalog

from tests.unit_tests.server.extensions.conftest import (
    AGENT_TEMPLATES,
    PLUGIN_PACKAGES,
    seed_package,
)

_LIFECYCLE_METHODS: tuple[tuple[ReqMethod, str], ...] = (
    (ReqMethod.AGENT_TEMPLATES_CREATE, "agent_templates.create"),
    (ReqMethod.AGENT_TEMPLATES_INSTALL, "agent_templates.install"),
    (ReqMethod.AGENT_TEMPLATES_UNINSTALL, "agent_templates.uninstall"),
    (ReqMethod.PLUGIN_PACKAGES_CREATE, "plugin_packages.create"),
    (ReqMethod.PLUGIN_PACKAGES_INSTALL, "plugin_packages.install"),
    (ReqMethod.PLUGIN_PACKAGES_UNINSTALL, "plugin_packages.uninstall"),
)

_CATALOG_METHODS: tuple[ReqMethod, ...] = (
    ReqMethod.AGENT_TEMPLATES_LIST,
    ReqMethod.AGENT_TEMPLATES_SHOW,
    ReqMethod.AGENT_TEMPLATES_FILE_LIST,
    ReqMethod.AGENT_TEMPLATES_FILE_READ,
    ReqMethod.PLUGIN_PACKAGES_LIST,
    ReqMethod.PLUGIN_PACKAGES_SHOW,
) + tuple(member for member, _ in _LIFECYCLE_METHODS)


def _iface():
    try:
        from jiuwenswarm.server.runtime.agent_adapter import interface as iface
    except ModuleNotFoundError as exc:
        pytest.skip(f"interface import blocked: {exc}")
    return iface


def _req(
    params: dict,
    *,
    method: ReqMethod | None = None,
    request_id: str = "r1",
    channel_id: str = "c1",
    session_id: str = "s1",
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id=channel_id,
        session_id=session_id,
        req_method=method,
        params=params,
    )


@pytest.fixture
def deep_adapter(monkeypatch, extension_workspace):
    try:
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep as ideep
    except ModuleNotFoundError as exc:
        pytest.skip(f"interface_deep import blocked: {exc}")

    adapter = ideep.JiuWenSwarmDeepAdapter()
    instance = MagicMock()
    rec = MagicMock()
    rec.load_id = "rec"
    instance.load_agent_template = AsyncMock(return_value=rec)
    instance.load_plugin = AsyncMock(return_value=rec)
    instance.unload_extension = AsyncMock(return_value=[])
    monkeypatch.setattr(adapter, "_is_session_live", lambda _sid: False)
    adapter._instance = instance
    seed_package(extension_workspace, AGENT_TEMPLATES, "alpha", installed=True)
    seed_package(extension_workspace, PLUGIN_PACKAGES, "beta", installed=True)
    return adapter


class TestChatSendParamsAndMcpUnion:
    """chat.send field types and params.mcp ∪ equipment connectors."""

    @pytest.mark.parametrize("field,expected", [
        ("agent_template_name", str),
        ("plugin_names", list),
    ])
    def test_equipment_fields_optional_not_nullable(self, field: str, expected: type) -> None:
        assert field in ChatSendParams.__optional_keys__
        hint = get_type_hints(ChatSendParams).get(field)
        if expected is str:
            assert hint is str
        else:
            args = typing.get_args(hint)
            assert len(args) == 1 and args[0] is str
        inner = typing.get_args(ChatSendParams.__annotations__[field])[0]
        assert getattr(inner, "__origin__", None) is not typing.Union

    def test_compute_mcp_needed_unions_without_reading_session(
        self, extension_workspace
    ) -> None:
        iface = _iface()
        seed_package(
            extension_workspace, AGENT_TEMPLATES, "alpha", connectors=["feishu"]
        )
        seed_package(
            extension_workspace, PLUGIN_PACKAGES, "beta", connectors=["mail"]
        )
        assert iface.compute_chat_send_mcp_needed(
            {"agent_template_name": "alpha", "mcp": ["custom", "feishu"]}
        ) == ["custom", "feishu"]
        assert iface.compute_chat_send_mcp_needed(
            {"agent_template_name": "alpha", "mcp": []}
        ) == ["feishu"]
        assert iface.compute_chat_send_mcp_needed(
            {"agent_template_name": "", "plugin_names": ["beta"]}
        ) == ["mail"]
        assert iface.compute_chat_send_mcp_needed({"query": "hi", "mcp": ["x"]}) == ["x"]
        assert iface.compute_chat_send_mcp_needed({"query": "hi"}) == []


class TestChatSendMountAndGates:
    """Differential load/unload, skip modes, marketplace and connector gates."""

    async def test_load_switch_unload_plugins_and_order(self, deep_adapter, extension_workspace):
        adapter = deep_adapter
        seed_package(extension_workspace, AGENT_TEMPLATES, "gamma", installed=True)
        seed_package(extension_workspace, PLUGIN_PACKAGES, "delta", installed=True)

        resp = await adapter._ensure_chat_extensions(
            _req({"agent_template_name": "alpha", "plugin_names": ["beta"]})
        )
        assert resp is None
        adapter._instance.load_agent_template.assert_awaited()
        adapter._instance.load_plugin.assert_awaited()

        call_order: list[str] = []

        async def _unload(_rec):
            call_order.append("unload")
            return []

        async def _load_at(*_a, **_k):
            call_order.append("load_at")
            rec = MagicMock()
            rec.load_id = "gamma"
            return rec

        adapter._instance.unload_extension.side_effect = _unload
        adapter._instance.load_agent_template.side_effect = _load_at
        adapter._instance.load_plugin.reset_mock()

        resp = await adapter._ensure_chat_extensions(
            _req({"agent_template_name": "gamma", "plugin_names": []})
        )
        assert resp is None
        first_load = call_order.index("load_at")
        assert call_order[:first_load].count("unload") >= 2
        assert adapter._loaded_agent_template[0] == "gamma"
        assert adapter._loaded_plugins == {}

        adapter._instance.unload_extension.side_effect = None
        adapter._instance.unload_extension.reset_mock()
        resp = await adapter._ensure_chat_extensions(_req({"agent_template_name": ""}))
        assert resp is None
        adapter._instance.unload_extension.assert_awaited()
        assert adapter._loaded_agent_template is None

    @pytest.mark.parametrize(
        "setup,params,needle",
        [
            ("expert_not_installed", {"agent_template_name": "alpha"}, "not installed"),
            ("plugin_not_installed", {"plugin_names": ["beta"]}, "plugin not installed"),
            ("connector_unready", {"agent_template_name": "conn-expert"}, "connector not connected"),
        ],
    )
    async def test_send_gates_reject(
        self, monkeypatch, deep_adapter, extension_workspace, setup, params, needle
    ):
        adapter = deep_adapter
        if setup == "expert_not_installed":
            catalog.upsert_agent_template_marketplace_entry(
                "alpha", installed=False, source="local"
            )
        elif setup == "plugin_not_installed":
            catalog.upsert_plugin_marketplace_entry(
                "beta", installed=False, source="local"
            )
        else:
            seed_package(
                extension_workspace,
                AGENT_TEMPLATES,
                "conn-expert",
                installed=True,
                connectors=["feishu"],
            )
            monkeypatch.setattr(catalog, "unready_connectors", lambda names: list(names))
        resp = await adapter._ensure_chat_extensions(_req(params))
        assert resp is not None and resp.ok is False
        assert needle in resp.payload["error"]
        adapter._instance.load_agent_template.assert_not_called()
        adapter._instance.load_plugin.assert_not_called()

    async def test_fresh_turn_rejects_equipment_change(self, monkeypatch, deep_adapter):
        adapter = deep_adapter
        monkeypatch.setattr(adapter, "_is_session_live", lambda _sid: True)
        resp = await adapter._ensure_chat_extensions(
            _req({"agent_template_name": "alpha"})
        )
        assert resp is not None and resp.ok is False
        assert "fresh" in resp.payload["error"]
        adapter._instance.load_agent_template.assert_not_called()

        monkeypatch.setattr(adapter, "_is_session_live", lambda _sid: False)
        assert await adapter._ensure_chat_extensions(
            _req({"agent_template_name": "alpha"})
        ) is None
        monkeypatch.setattr(adapter, "_is_session_live", lambda _sid: True)
        adapter._instance.load_agent_template.reset_mock()
        assert await adapter._ensure_chat_extensions(
            _req({"agent_template_name": "alpha"})
        ) is None
        adapter._instance.load_agent_template.assert_not_called()

    @pytest.mark.parametrize("mode", ["team", "team.plan", "code.team", "auto_harness"])
    async def test_skip_modes_do_not_touch_equipment(self, deep_adapter, mode):
        adapter = deep_adapter
        resp = await adapter._ensure_chat_extensions(
            _req({"mode": mode, "agent_template_name": "alpha"})
        )
        assert resp is None
        adapter._instance.load_agent_template.assert_not_called()
        adapter._instance.unload_extension.assert_not_called()


class TestPackageCatalogReqMethodRouting:
    """12 catalog RPCs are stateless; install pending payload; uninstall unloads first."""

    def test_catalog_methods_are_stateless_and_forwarded(self) -> None:
        assert not hasattr(ReqMethod, "PLUGIN_PACKAGES_TOGGLE")
        for method in _CATALOG_METHODS:
            req = AgentRequest(request_id="r", channel_id="c", req_method=method)
            assert AgentWebSocketServer._is_stateless_method_request(req) is True
        assert AgentWebSocketServer._is_stateless_method_request(
            AgentRequest(request_id="r", channel_id="c", req_method=ReqMethod.CHAT_SEND)
        ) is False
        for _, value in _LIFECYCLE_METHODS:
            assert value in app_web_handlers._FORWARD_REQ_METHODS
            assert value in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS
            assert value in CLI_FORWARD_REQ_METHODS
            assert value in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS

    def test_legacy_plugins_routes_stay_separate(self) -> None:
        iface = _iface()
        for method in (
            ReqMethod.PLUGINS_LIST,
            ReqMethod.PLUGINS_INSTALL,
            ReqMethod.PLUGINS_UNINSTALL,
            ReqMethod.PLUGINS_ENABLE,
            ReqMethod.PLUGINS_DISABLE,
            ReqMethod.PLUGINS_RELOAD,
        ):
            assert method in iface._PLUGIN_ROUTES
            assert method not in iface._PACKAGE_ROUTES

    async def test_install_pending_payload_and_uninstall_unloads_first(
        self, monkeypatch, extension_workspace
    ) -> None:
        iface = _iface()
        seed_package(
            extension_workspace,
            AGENT_TEMPLATES,
            "needs-auth",
            connectors=["feishu"],
        )
        monkeypatch.setattr(catalog, "unready_connectors", lambda names: list(names))
        req = _req(
            {"id": "needs-auth"},
            method=ReqMethod.AGENT_TEMPLATES_INSTALL,
        )
        resp = await iface.JiuWenSwarm._handle_package_catalog_request(None, req)
        assert isinstance(resp, AgentResponse)
        assert resp.ok is False
        assert resp.payload["pending_connectors"] == ["feishu"]
        assert resp.payload["error"] == "connector not connected: feishu"
        assert catalog.read_agent_template_marketplace_entries() == []

        calls: list[tuple[str, str]] = []
        deleted: list[bool] = []

        async def _unload(self, kind, package_id):
            calls.append((kind, package_id))

        async def _unload_fail(self, kind, package_id):
            raise RuntimeError("unload boom")

        monkeypatch.setattr(iface.JiuWenSwarm, "_unload_live_equipment", _unload)
        monkeypatch.setattr(
            iface.package_manager,
            "uninstall_equipment_with_notice",
            lambda kind, params: {"kind": kind, "id": params.get("id")},
        )
        owner = iface.JiuWenSwarm.__new__(iface.JiuWenSwarm)
        uninstall_req = _req(
            {"id": "alpha"}, method=ReqMethod.AGENT_TEMPLATES_UNINSTALL
        )
        ok_resp = await iface.JiuWenSwarm._handle_package_catalog_request(
            owner, uninstall_req
        )
        assert ok_resp.ok is True
        assert calls == [(AGENT_TEMPLATES, "alpha")]

        monkeypatch.setattr(iface.JiuWenSwarm, "_unload_live_equipment", _unload_fail)
        monkeypatch.setattr(
            iface.package_manager,
            "uninstall_equipment_with_notice",
            lambda *_a, **_k: deleted.append(True) or {},
        )
        fail_resp = await iface.JiuWenSwarm._handle_package_catalog_request(
            owner, uninstall_req
        )
        assert fail_resp.ok is False
        assert "unload boom" in str(fail_resp.payload.get("error", ""))
        assert deleted == []
