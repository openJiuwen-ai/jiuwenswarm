# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``mcp.cancel_connect`` (registry-level cancel semantics).

``cancel_connect`` marks a name cancelled (so the wait_auth poller / connect
flow unwinds), kills any pending authWaitForExit CLI proc, and rolls back only
a still-``connecting`` state.json record. Idempotent; a fresh ``connect_mcp``
clears the marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def test_cancel_connect_marks_and_kills_pending_proc() -> None:
    from jiuwenswarm.server.runtime.mcp import registry

    with (
        patch("jiuwenswarm.server.runtime.mcp.cli_driver.cancel_pending_auth_proc") as kill,
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record") as get_rec,
        patch("jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect") as rollback,
    ):
        get_rec.return_value = {"name": "feishu", "state": "connecting"}
        result = registry.cancel_connect("feishu")
    try:
        assert result == {"type": "cancelled", "name": "feishu"}
        assert registry.was_connect_cancelled("feishu")
        kill.assert_called_once_with("feishu")
        rollback.assert_called_once_with("feishu")
    finally:
        registry.clear_connect_cancel("feishu")


def test_cancel_connect_skips_rollback_for_connected_record() -> None:
    from jiuwenswarm.server.runtime.mcp import registry

    with (
        patch("jiuwenswarm.server.runtime.mcp.cli_driver.cancel_pending_auth_proc") as kill,
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record") as get_rec,
        patch("jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect") as rollback,
    ):
        get_rec.return_value = {"name": "feishu", "state": "connected"}
        registry.cancel_connect("feishu")
    try:
        # Auth may have just completed — never unlink a connected MCP.
        rollback.assert_not_called()
        kill.assert_called_once_with("feishu")
        assert registry.was_connect_cancelled("feishu")
    finally:
        registry.clear_connect_cancel("feishu")


def test_cancel_connect_with_no_record_is_safe() -> None:
    from jiuwenswarm.server.runtime.mcp import registry

    with (
        patch("jiuwenswarm.server.runtime.mcp.cli_driver.cancel_pending_auth_proc"),
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record") as get_rec,
        patch("jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect") as rollback,
    ):
        get_rec.return_value = None
        result = registry.cancel_connect("dingtalk")
    try:
        assert result == {"type": "cancelled", "name": "dingtalk"}
        rollback.assert_not_called()
    finally:
        registry.clear_connect_cancel("dingtalk")


def test_fresh_connect_clears_stale_cancel_marker() -> None:
    """A new connect supersedes a prior cancel (retry path)."""
    from jiuwenswarm.server.runtime.mcp import registry

    with (
        patch("jiuwenswarm.server.runtime.mcp.cli_driver.cancel_pending_auth_proc"),
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record"),
        patch("jiuwenswarm.server.runtime.mcp.registry.rollback_failed_connect"),
    ):
        registry.cancel_connect("feishu")
    try:
        assert registry.was_connect_cancelled("feishu")
        # _resolve_package is patched to None → falls into the custom-MCP
        # branch and raises KeyError for a missing record — reaching the
        # clear_connect_cancel call is all we assert here.
        with (
            patch("jiuwenswarm.server.runtime.mcp.registry._resolve_package", return_value=None),
            patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record", return_value=None),
        ):
            with pytest.raises(KeyError):
                registry.connect_mcp("feishu")
        assert not registry.was_connect_cancelled("feishu")
    finally:
        registry.clear_connect_cancel("feishu")


@pytest.mark.asyncio
async def test_host_oauth_cancel_during_probe_returns_cancelled() -> None:
    """Cancelling while the host-OAuth probe runs must not let its late success
    flip the record back to connected."""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.mcp import oauth_flow, registry

    syncs: list[str] = []

    class _Manager:
        async def probe_mcp_live_connection(self, name: str):
            registry.cancel_connect(name)  # user cancels mid-probe
            return True, ""

        def sync_mcp_credentials(self) -> None:
            syncs.append("sync")

    class _Server:
        _agent_manager = _Manager()

        @staticmethod
        def _mask_sensitive_fields(item: dict) -> dict:
            return item

    with (
        patch.object(oauth_flow, "load_oauth_credentials", return_value={"access_token": "t"}),
        patch.object(registry, "complete_host_oauth_auth", return_value={"name": "canva"}),
        patch("jiuwenswarm.server.runtime.mcp.cli_driver.cancel_pending_auth_proc"),
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record", return_value=None),
        patch("jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state") as set_state,
        patch("jiuwenswarm.common.mcp_config.discard_probed_mcp") as discard,
    ):
        result = await AgentWebSocketServer._await_host_oauth_auth(
            _Server(), "canva", max_attempts=1, delay=0,
        )
    try:
        assert result == {"type": "cancelled", "name": "canva"}
        set_state.assert_not_called()
        discard.assert_awaited_once_with("canva")
        assert syncs == []
    finally:
        registry.clear_connect_cancel("canva")


@pytest.mark.asyncio
async def test_discard_probed_mcp_removes_server_by_name() -> None:
    """A probe cached the server in Runner.resource_mgr; cancel must evict it."""
    from openjiuwen.core.runner import Runner

    from jiuwenswarm.common.mcp_config import discard_probed_mcp

    remove = AsyncMock()
    with patch.object(Runner.resource_mgr, "remove_mcp_server", new=remove):
        await discard_probed_mcp("canva")
    remove.assert_awaited_once_with(server_name="canva", ignore_exception=True)
