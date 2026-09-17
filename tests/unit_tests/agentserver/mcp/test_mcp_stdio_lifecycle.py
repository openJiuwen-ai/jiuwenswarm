# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
Unit tests for MCP stdio client lifecycle fixes.

These tests verify:
1. StdioClient owner task mechanism - enter/exit in the same task
2. disconnect() does not swallow CancelledError/RuntimeError
3. ToolMgr.remove_tool_server() disconnects before removing from resource map
4. ToolMgr.add_tool_server() cleans up on failure
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _load_patched_tool_manager():
    """Load ToolMgr and McpServerResource from patch sources.

    The patch sources version has different remove_tool_server/add_tool_server
    behavior (keeps resource on disconnect failure, calls disconnect on add
    failure) that the installed openjiuwen does not have.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "patched_tool_manager",
        "jiuwenswarm/agents/harness/common/tools/browser-move/src/"
        "openjiuwen_patch_sources/openjiuwen/core/runner/"
        "resources_manager/tool_manager.py",
    )
    patched = importlib.util.module_from_spec(spec)
    sys.modules["patched_tool_manager"] = patched
    spec.loader.exec_module(patched)
    return patched.ToolMgr, patched.McpServerResource


class TestStdioClientOwnerTask:
    """Tests for StdioClient owner task lifecycle mechanism.

    Imports the patched StdioClient (with owner-task pattern) from patch
    sources instead of the installed openjiuwen base class, since the
    installed base StdioClient does not have the owner-task pattern.
    """

    @staticmethod
    def _load_patched_stdio_client():
        import importlib.util
        import sys
        from openjiuwen.core.foundation.tool.mcp.client.mcp_client import McpClient

        # Patch sources StdioClient calls super().__init__(server_path)
        # with a string, but installed McpClient expects a McpServerConfig.
        # Mock McpClient.__init__ to accept the string signature.
        original_init = McpClient.__init__

        def _mock_init(self, server_path=None, *args, **kwargs):
            self._server_path = server_path

        McpClient.__init__ = _mock_init

        spec = importlib.util.spec_from_file_location(
            "patched_stdio_client",
            "jiuwenswarm/agents/harness/common/tools/browser-move/src/"
            "openjiuwen_patch_sources/openjiuwen/core/foundation/tool/mcp/"
            "client/stdio_client.py",
        )
        patched = importlib.util.module_from_spec(spec)
        sys.modules["patched_stdio_client"] = patched
        spec.loader.exec_module(patched)
        return patched.StdioClient

    @pytest.mark.asyncio
    async def test_disconnect_not_swallow_cancelled_error(self):
        """Verify that CancelledError is re-raised, not swallowed."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-1",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True
                assert client._owner_task is not None

                disconnect_task = asyncio.create_task(client.disconnect())
                disconnect_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await disconnect_task

    @pytest.mark.asyncio
    async def test_disconnect_with_timeout(self):
        """Verify that disconnect respects timeout."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-2",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                result = await client.disconnect(timeout=0.5)
                assert result is True

    @pytest.mark.asyncio
    async def test_force_close_cleans_up_owner_task(self):
        """Verify that _force_close properly cleans up the owner task."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-3",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                await client._force_close()
                assert client._owner_task is None
                assert client._session is None
                assert client._client is None
                assert client._is_disconnected is True

    @pytest.mark.asyncio
    async def test_connect_while_connecting_returns_false(self):
        """Verify that connecting while already connecting returns False."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-4",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            never_complete = asyncio.Future()
            mock_session.initialize = AsyncMock(return_value=never_complete)

            with patch("mcp.ClientSession", return_value=mock_session):
                connect_task = asyncio.create_task(client.connect(timeout=30.0))

                await asyncio.sleep(0.1)

                result = await client.connect(timeout=5.0)
                assert result is False

                connect_task.cancel()
                never_complete.cancel()
                try:
                    await connect_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected_returns_true(self):
        """Verify that disconnect when not connected returns True."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient("stdio://test-server-5", "test-server", {})
        result = await client.disconnect()
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_failure_cleans_up_owner_task(self):
        """Verify that failed connect properly cleans up the owner task."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-5a",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("connection failed"))

            connected = await client.connect(timeout=5.0)
            assert connected is False
            assert client._owner_task is None
            assert client._session is None
            assert client._client is None

    @pytest.mark.asyncio
    async def test_disconnect_after_disconnect_is_idempotent(self):
        """Verify that calling disconnect multiple times is safe."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-5b",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                result1 = await client.disconnect()
                assert result1 is True

                result2 = await client.disconnect()
                assert result2 is True

    @pytest.mark.asyncio
    async def test_owner_task_cancellation_cleanup(self):
        """Verify that owner task cancellation is properly handled."""
        StdioClient = self._load_patched_stdio_client()

        client = StdioClient(
            "stdio://test-server-5c",
            "test-server",
            {"command": "python", "args": ["-c", "print('ok')"]},
        )

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True
                assert client._owner_task is not None

                client._owner_task.cancel()
                await asyncio.sleep(0.2)

                assert client._is_disconnected is True
                # _run_owner's finally block sets _owner_task = None
                # after the task completes, so we check for None
                # instead of calling .done() on the cleared reference.
                assert client._owner_task is None


class TestToolMgrRemoveToolServer:
    """Tests for ToolMgr.remove_tool_server() ordering fix."""

    @pytest.mark.asyncio
    async def test_remove_tool_server_disconnects_before_pop(self):
        """Verify that disconnect is called before removing from resource map."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr, McpServerResource
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.disconnect.return_value = True

        cfg = McpServerConfig(
            server_id="test-server-6",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        resource = McpServerResource(
            config=cfg,
            client=mock_client,
            tool_ids=["tool1", "tool2"],
            last_update_time=0.0,
        )
        tool_mgr._mcp_server_resources["test-server-6"] = resource
        tool_mgr._mcp_server_name_to_ids["test-server"] = ["test-server-6"]

        assert "test-server-6" in tool_mgr._mcp_server_resources

        await tool_mgr.remove_tool_server("test-server-6")

        mock_client.disconnect.assert_called_once()
        assert "test-server-6" not in tool_mgr._mcp_server_resources

    @pytest.mark.asyncio
    async def test_remove_tool_server_keeps_resource_on_disconnect_failure(self):
        """Verify that resource is kept when disconnect fails."""
        ToolMgr, McpServerResource = _load_patched_tool_manager()
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.disconnect.return_value = False

        cfg = McpServerConfig(
            server_id="test-server-7",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        resource = McpServerResource(
            config=cfg,
            client=mock_client,
            tool_ids=["tool1", "tool2"],
            last_update_time=0.0,
        )
        tool_mgr._mcp_server_resources["test-server-7"] = resource
        tool_mgr._mcp_server_name_to_ids["test-server"] = ["test-server-7"]

        await tool_mgr.remove_tool_server("test-server-7")

        assert "test-server-7" in tool_mgr._mcp_server_resources

    @pytest.mark.asyncio
    async def test_remove_tool_server_keeps_resource_on_exception(self):
        """Verify that resource is kept when disconnect raises exception."""
        ToolMgr, McpServerResource = _load_patched_tool_manager()
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = RuntimeError("disconnect failed")

        cfg = McpServerConfig(
            server_id="test-server-8",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        resource = McpServerResource(
            config=cfg,
            client=mock_client,
            tool_ids=["tool1", "tool2"],
            last_update_time=0.0,
        )
        tool_mgr._mcp_server_resources["test-server-8"] = resource
        tool_mgr._mcp_server_name_to_ids["test-server"] = ["test-server-8"]

        await tool_mgr.remove_tool_server("test-server-8")

        assert "test-server-8" in tool_mgr._mcp_server_resources


class TestToolMgrAddToolServer:
    """Tests for ToolMgr.add_tool_server() cleanup fix."""

    @pytest.mark.asyncio
    async def test_add_tool_server_cleans_up_on_refresh_failure(self):
        """Verify that client is disconnected when refresh_mcp_tools fails."""
        ToolMgr, _ = _load_patched_tool_manager()
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.connect.return_value = True
        mock_client.disconnect.return_value = True

        cfg = McpServerConfig(
            server_id="test-server-9",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        with patch.object(tool_mgr, '_create_client', return_value=mock_client):
            with patch.object(tool_mgr, '_inner_refresh_mcp_tools', side_effect=RuntimeError("refresh failed")):
                with pytest.raises(Exception):
                    await tool_mgr.add_tool_server(cfg)

        mock_client.connect.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_tool_server_no_cleanup_on_connect_failure(self):
        """Verify that disconnect is not called when connect fails."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.connect.return_value = False

        cfg = McpServerConfig(
            server_id="test-server-10",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        with patch.object(tool_mgr, '_create_client', return_value=mock_client):
            with pytest.raises(Exception):
                await tool_mgr.add_tool_server(cfg)

        mock_client.connect.assert_called_once()
        mock_client.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_tool_server_no_cleanup_on_early_exception(self):
        """Verify that disconnect is not called when exception occurs before connect."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()

        cfg = McpServerConfig(
            server_id="test-server-11",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        with patch.object(tool_mgr, '_create_client', side_effect=RuntimeError("create failed")):
            with pytest.raises(Exception):
                await tool_mgr.add_tool_server(cfg)

        mock_client.connect.assert_not_called()
        mock_client.disconnect.assert_not_called()


class TestBrowserMoveStdioClientOwnerTask:
    """Tests for BrowserMoveStdioClient owner task lifecycle mechanism."""

    @pytest.mark.asyncio
    async def test_disconnect_not_swallow_cancelled_error(self):
        """Verify that CancelledError is re-raised, not swallowed."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("browser_move_stdio_client", "jiuwenswarm/agents/harness/common/tools/browser-move/src/playwright_runtime/clients/stdio_client.py")
        browser_move_stdio_client = importlib.util.module_from_spec(spec)
        sys.modules["browser_move_stdio_client"] = browser_move_stdio_client
        spec.loader.exec_module(browser_move_stdio_client)
        BrowserMoveStdioClient = browser_move_stdio_client.BrowserMoveStdioClient
        from openjiuwen.core.foundation.tool import McpServerConfig

        cfg = McpServerConfig(
            server_id="bm-test-server-1",
            server_name="bm-test-server",
            server_path="stdio://bm-test-server",
            client_type="stdio",
            params={"command": "python", "args": ["-c", "print('ok')"]},
        )

        client = BrowserMoveStdioClient(cfg)

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True
                assert client._owner_task is not None

                disconnect_task = asyncio.create_task(client.disconnect())
                disconnect_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await disconnect_task

    @pytest.mark.asyncio
    async def test_disconnect_with_timeout(self):
        """Verify that disconnect respects timeout."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("browser_move_stdio_client", "jiuwenswarm/agents/harness/common/tools/browser-move/src/playwright_runtime/clients/stdio_client.py")
        browser_move_stdio_client = importlib.util.module_from_spec(spec)
        sys.modules["browser_move_stdio_client"] = browser_move_stdio_client
        spec.loader.exec_module(browser_move_stdio_client)
        BrowserMoveStdioClient = browser_move_stdio_client.BrowserMoveStdioClient
        from openjiuwen.core.foundation.tool import McpServerConfig

        cfg = McpServerConfig(
            server_id="bm-test-server-2",
            server_name="bm-test-server",
            server_path="stdio://bm-test-server",
            client_type="stdio",
            params={"command": "python", "args": ["-c", "print('ok')"]},
        )

        client = BrowserMoveStdioClient(cfg)

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                result = await client.disconnect(timeout=0.5)
                assert result is True

    @pytest.mark.asyncio
    async def test_force_close_cleans_up_owner_task(self):
        """Verify that _force_close properly cleans up the owner task."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("browser_move_stdio_client", "jiuwenswarm/agents/harness/common/tools/browser-move/src/playwright_runtime/clients/stdio_client.py")
        browser_move_stdio_client = importlib.util.module_from_spec(spec)
        sys.modules["browser_move_stdio_client"] = browser_move_stdio_client
        spec.loader.exec_module(browser_move_stdio_client)
        BrowserMoveStdioClient = browser_move_stdio_client.BrowserMoveStdioClient
        from openjiuwen.core.foundation.tool import McpServerConfig

        cfg = McpServerConfig(
            server_id="bm-test-server-3",
            server_name="bm-test-server",
            server_path="stdio://bm-test-server",
            client_type="stdio",
            params={"command": "python", "args": ["-c", "print('ok')"]},
        )

        client = BrowserMoveStdioClient(cfg)

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                await client._force_close()
                assert client._owner_task is None
                assert client._session is None
                assert client._client is None
                assert client._is_disconnected is True

    @pytest.mark.asyncio
    async def test_connect_failure_cleans_up_owner_task(self):
        """Verify that failed connect properly cleans up the owner task."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("browser_move_stdio_client", "jiuwenswarm/agents/harness/common/tools/browser-move/src/playwright_runtime/clients/stdio_client.py")
        browser_move_stdio_client = importlib.util.module_from_spec(spec)
        sys.modules["browser_move_stdio_client"] = browser_move_stdio_client
        spec.loader.exec_module(browser_move_stdio_client)
        BrowserMoveStdioClient = browser_move_stdio_client.BrowserMoveStdioClient
        from openjiuwen.core.foundation.tool import McpServerConfig

        cfg = McpServerConfig(
            server_id="bm-test-server-4",
            server_name="bm-test-server",
            server_path="stdio://bm-test-server",
            client_type="stdio",
            params={"command": "python", "args": ["-c", "print('ok')"]},
        )

        client = BrowserMoveStdioClient(cfg)

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("connection failed"))

            connected = await client.connect(timeout=5.0)
            assert connected is False
            assert client._owner_task is None
            assert client._session is None
            assert client._client is None

    @pytest.mark.asyncio
    async def test_disconnect_after_disconnect_is_idempotent(self):
        """Verify that calling disconnect multiple times is safe."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("browser_move_stdio_client", "jiuwenswarm/agents/harness/common/tools/browser-move/src/playwright_runtime/clients/stdio_client.py")
        browser_move_stdio_client = importlib.util.module_from_spec(spec)
        sys.modules["browser_move_stdio_client"] = browser_move_stdio_client
        spec.loader.exec_module(browser_move_stdio_client)
        BrowserMoveStdioClient = browser_move_stdio_client.BrowserMoveStdioClient
        from openjiuwen.core.foundation.tool import McpServerConfig

        cfg = McpServerConfig(
            server_id="bm-test-server-5",
            server_name="bm-test-server",
            server_path="stdio://bm-test-server",
            client_type="stdio",
            params={"command": "python", "args": ["-c", "print('ok')"]},
        )

        client = BrowserMoveStdioClient(cfg)

        with patch("mcp.client.stdio.stdio_client") as mock_stdio_client:
            mock_stdio_client.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()

            with patch("mcp.ClientSession", return_value=mock_session):
                connected = await client.connect(timeout=5.0)
                assert connected is True

                result1 = await client.disconnect()
                assert result1 is True

                result2 = await client.disconnect()
                assert result2 is True


class TestToolMgrRemoveToolServerEdgeCases:
    """Edge case tests for ToolMgr.remove_tool_server()."""

    @pytest.mark.asyncio
    async def test_remove_tool_server_ignore_not_exist_default(self):
        """Verify that remove_tool_server ignores non-existent server by default."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

        tool_mgr = ToolMgr()
        result = await tool_mgr.remove_tool_server("nonexistent-server")
        assert result == []

    @pytest.mark.asyncio
    async def test_remove_tool_server_raises_on_not_exist(self):
        """Verify that remove_tool_server raises when ignore_not_exist is False."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

        tool_mgr = ToolMgr()
        with pytest.raises(Exception):
            await tool_mgr.remove_tool_server("nonexistent-server", ignore_not_exist=False)

    @pytest.mark.asyncio
    async def test_remove_tool_server_cleanup_tool_ids(self):
        """Verify that remove_tool_server properly cleans up tool_ids."""
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr, McpServerResource
        from openjiuwen.core.foundation.tool import McpServerConfig

        tool_mgr = ToolMgr()

        mock_client = AsyncMock()
        mock_client.disconnect.return_value = True

        cfg = McpServerConfig(
            server_id="test-server-12",
            server_name="test-server",
            server_path="stdio://test-server",
            client_type="stdio",
        )

        resource = McpServerResource(
            config=cfg,
            client=mock_client,
            tool_ids=["tool1", "tool2"],
            last_update_time=0.0,
        )
        tool_mgr._mcp_server_resources["test-server-12"] = resource
        tool_mgr._mcp_server_name_to_ids["test-server"] = ["test-server-12"]

        result = await tool_mgr.remove_tool_server("test-server-12")

        assert result == ["tool1", "tool2"]
        assert "test-server-12" not in tool_mgr._mcp_server_resources
        assert "test-server" not in tool_mgr._mcp_server_name_to_ids