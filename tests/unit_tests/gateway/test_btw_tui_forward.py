# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests that command.btw is correctly wired in TUI connection forwarding sets.

Verifies the changes from c44c8864 (add /btw command) that added
command.btw to CLI_FORWARD_REQ_METHODS and CLI_FORWARD_NO_LOCAL_HANDLER_METHODS.
"""

from __future__ import annotations

from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
    CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
    CLI_FORWARD_REQ_METHODS,
    CliHandlersBindParams,
    build_cli_route_binding,
    register_cli_handlers,
)


class FakeGatewayServer:
    """Fake GatewayServer for testing CLI handler registration."""

    def __init__(self) -> None:
        self.local_handlers: dict[str, dict] = {}  # path -> {method: handler}
        self.responses: list[dict] = []

    def register_local_handler(self, path: str, method: str, handler) -> None:
        if path not in self.local_handlers:
            self.local_handlers[path] = {}
        self.local_handlers[path][method] = handler

    async def send_response(
        self, ws, req_id, *, ok=True, payload=None, error=None, code=None
    ) -> None:
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload or {},
                "error": error,
                "code": code,
            }
        )


class TestBtwInForwardSets:
    """Verify command.btw is registered in the required forward sets."""

    @staticmethod
    def test_command_btw_in_cli_forward_req_methods():
        """command.btw must be in CLI_FORWARD_REQ_METHODS."""
        assert "command.btw" in CLI_FORWARD_REQ_METHODS

    @staticmethod
    def test_command_btw_in_cli_forward_no_local_handler_methods():
        """command.btw must be in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS."""
        assert "command.btw" in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS

    @staticmethod
    def test_route_binding_includes_command_btw():
        """build_cli_route_binding must include command.btw in forward methods."""
        from jiuwenswarm.gateway.channel_manager.tui.tui_connect import CliRouteBindParams

        bind_params = CliRouteBindParams(path="/tui")
        binding = build_cli_route_binding(bind_params)
        assert "command.btw" in binding.forward_methods

    @staticmethod
    def test_register_cli_handlers_does_not_create_local_handler_for_btw():
        """command.btw has no local handler — it is forwarded to agent server."""
        server = FakeGatewayServer()
        register_cli_handlers(
            CliHandlersBindParams(
                channel=server,
                agent_client=None,
                message_handler=None,
                on_config_saved=None,
                path="/tui",
            )
        )

        cli_handlers = server.local_handlers.get("/tui", {})
        # command.btw should NOT have a local handler
        assert "command.btw" not in cli_handlers

    @staticmethod
    def test_all_cli_forward_methods_are_strings():
        """All entries in CLI_FORWARD_REQ_METHODS should be strings."""
        for method in CLI_FORWARD_REQ_METHODS:
            assert isinstance(method, str), f"Expected str, got {type(method)}: {method}"

    @staticmethod
    def test_btw_not_in_both_local_and_forward():
        """command.btw in NO_LOCAL_HANDLER means no local handler registered."""
        assert "command.btw" in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS
        assert "command.btw" in CLI_FORWARD_REQ_METHODS


class TestBtwVsOtherCommands:
    """Compare command.btw with sibling commands to verify consistency."""

    @staticmethod
    def test_command_btw_like_command_recap():
        """command.btw should follow the same pattern as command.compact (forward, no local)."""
        assert "command.compact" in CLI_FORWARD_REQ_METHODS
        assert "command.compact" in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS
        # btw should match the pattern
        assert "command.btw" in CLI_FORWARD_REQ_METHODS
        assert "command.btw" in CLI_FORWARD_NO_LOCAL_HANDLER_METHODS

    @staticmethod
    def test_command_btw_is_frozenset():
        """CLI_FORWARD_REQ_METHODS is a frozenset, so membership checks are O(1)."""
        assert isinstance(CLI_FORWARD_REQ_METHODS, frozenset)
        assert isinstance(CLI_FORWARD_NO_LOCAL_HANDLER_METHODS, frozenset)
