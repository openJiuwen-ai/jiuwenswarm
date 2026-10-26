# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Integration tests for JiuwenBox remote MCP endpoint.

These tests exercise the ``/mcp`` Streamable HTTP transport against a running
jiuwenbox server. They rely on the same ``--server-endpoint`` fixture used by
the other integration tests, so they work for both TCP and Unix Domain Socket
deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

_UDS_SCHEME = "unix://"
# Placeholder hostname used only for URL/path construction over UDS. The real
# transport is the Unix socket; MCP DNS-rebinding protection checks the Host
# header, so we send ``Host: localhost`` explicitly for UDS connections.
_UDS_PLACEHOLDER_BASE_URL = "http://jiuwenbox"


def _is_uds_endpoint(endpoint: str) -> bool:
    return endpoint.startswith(_UDS_SCHEME)


def _api_base_url(server_endpoint: str) -> str:
    if _is_uds_endpoint(server_endpoint):
        return _UDS_PLACEHOLDER_BASE_URL
    return server_endpoint if "://" in server_endpoint else f"http://{server_endpoint}"


def _mcp_url(server_endpoint: str) -> str:
    return f"{_api_base_url(server_endpoint).rstrip('/')}/mcp"


@asynccontextmanager
async def _mcp_session(server_endpoint: str) -> AsyncGenerator[ClientSession, None]:
    """Open an initialized MCP client session over the configured transport."""
    mcp_url = _mcp_url(server_endpoint)
    if _is_uds_endpoint(server_endpoint):
        uds_path = server_endpoint[len(_UDS_SCHEME):]
        transport = httpx.AsyncHTTPTransport(uds=uds_path)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            headers={"Host": "localhost"},
        ) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
    else:
        async with streamable_http_client(mcp_url) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@pytest.fixture
def api_client(server_endpoint: str) -> httpx.Client:
    """Sync HTTP client for direct API calls (health check / cleanup)."""
    if _is_uds_endpoint(server_endpoint):
        uds_path = server_endpoint[len(_UDS_SCHEME):]
        transport = httpx.HTTPTransport(uds=uds_path)
        with httpx.Client(
            transport=transport,
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            headers={"Host": "localhost"},
        ) as client:
            yield client
    else:
        base_url = _api_base_url(server_endpoint)
        with httpx.Client(base_url=base_url) as client:
            yield client


@pytest.fixture
def tracked_sandbox_ids(api_client: httpx.Client) -> list[str]:
    """Track sandboxes created by MCP calls and clean them up afterwards."""
    ids: list[str] = []
    yield ids
    for sandbox_id in ids:
        try:
            response = api_client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            if response.status_code not in (200, 202, 204, 404):
                logger.warning(
                    "Unexpected status cleaning up sandbox %s: %s",
                    sandbox_id,
                    response.status_code,
                )
        except Exception as exc:
            logger.warning("Failed to cleanup sandbox %s: %s", sandbox_id, exc)


def _call_sandbox_run_command(
    session: ClientSession, **kwargs: Any
) -> Any:
    """Helper to call the ``sandbox_run_command`` MCP tool.

    Returns the raw :class:`mcp.types.CallToolResult` so callers can inspect
    ``isError`` and the JSON-encoded payload independently.
    """
    return session.call_tool("sandbox_run_command", kwargs)


def _parse_json_result(result: Any) -> dict[str, Any]:
    """Extract the JSON payload returned by ``sandbox_run_command``."""
    assert len(result.content) == 1, f"unexpected content count: {result.content}"
    text = result.content[0].text
    return json.loads(text)


class TestMCPConnection:
    @pytest.mark.asyncio
    async def test_initialize_and_list_tools(self, server_endpoint: str):
        async with _mcp_session(server_endpoint) as session:
            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            assert "sandbox_run_command" in tool_names


class TestMCPRunCommand:
    @pytest.mark.asyncio
    async def test_run_command_auto_creates_and_deletes_sandbox(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["echo", "hello-from-mcp"],
                timeout_seconds=10,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert "hello-from-mcp" in data["stdout"], data
            assert data["created_sandbox"] is True
            assert data["deleted_sandbox"] is True
            assert data["sandbox_id"]
            # Already deleted by the server; no need to track for cleanup.

    @pytest.mark.asyncio
    async def test_run_command_keep_sandbox_and_reuse(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            # First call creates a sandbox and keeps it.
            create_result = await _call_sandbox_run_command(
                session,
                command=["sh", "-c", "echo keep > /tmp/mcp-reuse.txt"],
                timeout_seconds=10,
                keep_sandbox=True,
            )
            assert not create_result.isError, create_result.content
            create_data = _parse_json_result(create_result)
            assert create_data["exit_code"] == 0, create_data
            assert create_data["created_sandbox"] is True
            assert create_data["deleted_sandbox"] is False
            sandbox_id = create_data["sandbox_id"]
            assert sandbox_id
            tracked_sandbox_ids.append(sandbox_id)

            # Second call reuses the same sandbox; supplying an existing
            # sandbox_id never causes deletion regardless of keep_sandbox.
            reuse_result = await _call_sandbox_run_command(
                session,
                command=["cat", "/tmp/mcp-reuse.txt"],
                timeout_seconds=10,
                sandbox_id=sandbox_id,
            )
            assert not reuse_result.isError, reuse_result.content
            reuse_data = _parse_json_result(reuse_result)
            assert reuse_data["exit_code"] == 0, reuse_data
            assert "keep" in reuse_data["stdout"], reuse_data
            assert reuse_data["created_sandbox"] is False
            assert reuse_data["deleted_sandbox"] is False
            # The tracked_sandbox_ids fixture will delete the sandbox via the
            # REST API after the test.

    @pytest.mark.asyncio
    async def test_run_command_empty_command_returns_error(
        self, server_endpoint: str
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=[],
                timeout_seconds=10,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == -1, data
            assert "must not be empty" in data["stderr"], data
            assert data["created_sandbox"] is False
            assert data["deleted_sandbox"] is False

    @pytest.mark.asyncio
    async def test_run_command_invalid_sandbox_id(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        fake_id = f"nonexistent-{uuid.uuid4().hex[:8]}"
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["echo", "x"],
                timeout_seconds=10,
                sandbox_id=fake_id,
            )
            # The server should surface the not-found error as an in-band
            # tool failure rather than an MCP protocol error.
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == -1, data
            assert data["sandbox_id"] == fake_id
            assert data["created_sandbox"] is False
            assert data["deleted_sandbox"] is False

    @pytest.mark.asyncio
    async def test_run_command_failure_returns_stderr(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["sh", "-c", "echo expected-stderr >&2; exit 42"],
                timeout_seconds=10,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 42, data
            assert "expected-stderr" in data["stderr"], data
            assert data["created_sandbox"] is True
            assert data["deleted_sandbox"] is True

    @pytest.mark.asyncio
    async def test_run_command_with_stdin(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["cat"],
                timeout_seconds=10,
                stdin="hello stdin",
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert "hello stdin" in data["stdout"], data

    @pytest.mark.asyncio
    async def test_run_command_with_env(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["sh", "-c", "echo $MCP_TEST_VAR"],
                timeout_seconds=10,
                env={"MCP_TEST_VAR": "mcp-env-value"},
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert "mcp-env-value" in data["stdout"], data

    @pytest.mark.asyncio
    async def test_run_command_with_workdir(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["pwd"],
                timeout_seconds=10,
                workdir="/tmp",
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert "/tmp" in data["stdout"], data

    @pytest.mark.asyncio
    async def test_run_command_timeout_clamping(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            # timeout_seconds < 1 is clamped to 1 so the command still runs.
            result = await _call_sandbox_run_command(
                session,
                command=["echo", "ok"],
                timeout_seconds=0,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert data["stdout"].strip() == "ok"

    @pytest.mark.asyncio
    async def test_run_command_timeout_actually_limits_execution(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["sleep", "30"],
                timeout_seconds=2,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            # ``timeout(1)`` exits with 124 when the wrapped command is killed
            # by SIGTERM after the allotted seconds.
            assert data["exit_code"] == 124, data
            assert 1500 <= data["duration_ms"] <= 3000, data
            assert data["created_sandbox"] is True
            assert data["deleted_sandbox"] is True

    @pytest.mark.asyncio
    async def test_run_command_concurrent_sessions(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        async def worker(index: int) -> str:
            async with _mcp_session(server_endpoint) as session:
                result = await _call_sandbox_run_command(
                    session,
                    command=["echo", f"worker-{index}"],
                    timeout_seconds=10,
                )
                data = _parse_json_result(result)
                assert data["exit_code"] == 0, data
                return data["stdout"].strip()

        outputs = await asyncio.gather(*[worker(i) for i in range(3)])
        for i, output in enumerate(outputs):
            assert output == f"worker-{i}", outputs


class TestMCPRunCommandEdgeCases:
    @pytest.mark.asyncio
    async def test_run_command_empty_sandbox_id_fails(
        self, server_endpoint: str
    ):
        """Empty-string sandbox_id must be treated as invalid, not as "auto-create"."""
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["echo", "x"],
                timeout_seconds=10,
                sandbox_id="",
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == -1, data
            assert data["sandbox_id"] == ""
            assert data["created_sandbox"] is False
            assert data["deleted_sandbox"] is False

    @pytest.mark.asyncio
    async def test_run_command_keep_sandbox_with_invalid_id_fails(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """keep_sandbox=True cannot create a sandbox when an invalid id is supplied."""
        fake_id = f"nonexistent-{uuid.uuid4().hex[:8]}"
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["echo", "x"],
                timeout_seconds=10,
                sandbox_id=fake_id,
                keep_sandbox=True,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == -1, data
            assert data["sandbox_id"] == fake_id
            assert data["created_sandbox"] is False
            assert data["deleted_sandbox"] is False

    @pytest.mark.asyncio
    async def test_run_command_reuse_after_manual_delete_fails(
        self,
        server_endpoint: str,
        api_client: httpx.Client,
        tracked_sandbox_ids: list[str],
    ):
        """A kept sandbox deleted via REST must no longer be reusable through MCP."""
        async with _mcp_session(server_endpoint) as session:
            create_result = await _call_sandbox_run_command(
                session,
                command=["echo", "create"],
                timeout_seconds=10,
                keep_sandbox=True,
            )
            assert not create_result.isError, create_result.content
            create_data = _parse_json_result(create_result)
            assert create_data["exit_code"] == 0, create_data
            sandbox_id = create_data["sandbox_id"]
            assert sandbox_id
            tracked_sandbox_ids.append(sandbox_id)

        # Delete outside the MCP session (simulating a lifecycle mismatch).
        response = api_client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert response.status_code in (200, 202, 204), response.status_code
        tracked_sandbox_ids.remove(sandbox_id)

        async with _mcp_session(server_endpoint) as session:
            reuse_result = await _call_sandbox_run_command(
                session,
                command=["echo", "reuse"],
                timeout_seconds=10,
                sandbox_id=sandbox_id,
            )
            assert not reuse_result.isError, reuse_result.content
            reuse_data = _parse_json_result(reuse_result)
            assert reuse_data["exit_code"] == -1, reuse_data
            assert reuse_data["created_sandbox"] is False
            assert reuse_data["deleted_sandbox"] is False

    @pytest.mark.asyncio
    async def test_run_command_nonexistent_workdir_fails(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """A missing working directory should surface as an in-band tool failure."""
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["pwd"],
                timeout_seconds=10,
                workdir="/tmp/jiuwenbox-nonexistent-workdir",
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] != 0, data
            assert data["created_sandbox"] is True
            assert data["deleted_sandbox"] is True

    @pytest.mark.asyncio
    async def test_run_command_env_none_and_empty_are_equivalent(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """env=None and env={} should both produce a successful, isolated execution."""
        async with _mcp_session(server_endpoint) as session:
            none_result = await _call_sandbox_run_command(
                session,
                command=["sh", "-c", "echo env-none"],
                timeout_seconds=10,
            )
            assert not none_result.isError, none_result.content
            none_data = _parse_json_result(none_result)
            assert none_data["exit_code"] == 0, none_data
            assert "env-none" in none_data["stdout"], none_data

            empty_result = await _call_sandbox_run_command(
                session,
                command=["sh", "-c", "echo env-empty"],
                timeout_seconds=10,
                env={},
            )
            assert not empty_result.isError, empty_result.content
            empty_data = _parse_json_result(empty_result)
            assert empty_data["exit_code"] == 0, empty_data
            assert "env-empty" in empty_data["stdout"], empty_data

    @pytest.mark.asyncio
    async def test_run_command_large_stdout_not_truncated_by_mcp(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """Large stdout must flow through the MCP transport without size limits."""
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=[
                    "python3",
                    "-c",
                    "print('A' * 20000)",
                ],
                timeout_seconds=10,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert "A" * 100 in data["stdout"], data
            assert len(data["stdout"]) >= 20000, data

    @pytest.mark.asyncio
    async def test_run_command_long_stdin(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """Multi-kilobyte stdin should be forwarded into the sandbox command."""
        payload = "X" * 8192
        async with _mcp_session(server_endpoint) as session:
            result = await _call_sandbox_run_command(
                session,
                command=["cat"],
                timeout_seconds=10,
                stdin=payload,
            )
            assert not result.isError, result.content
            data = _parse_json_result(result)
            assert data["exit_code"] == 0, data
            assert payload in data["stdout"], data

    @pytest.mark.asyncio
    async def test_run_command_many_parallel_keep_sandboxes(
        self, server_endpoint: str, tracked_sandbox_ids: list[str]
    ):
        """Each parallel worker that keeps its sandbox must get an independent id."""
        async def worker(index: int) -> str:
            async with _mcp_session(server_endpoint) as session:
                result = await _call_sandbox_run_command(
                    session,
                    command=["sh", "-c", f"echo worker-{index} > /tmp/keep.txt"],
                    timeout_seconds=10,
                    keep_sandbox=True,
                )
                data = _parse_json_result(result)
                assert data["exit_code"] == 0, data
                assert data["created_sandbox"] is True, data
                assert data["deleted_sandbox"] is False, data
                return data["sandbox_id"]

        sandbox_ids = await asyncio.gather(*[worker(i) for i in range(3)])
        assert len(set(sandbox_ids)) == 3, sandbox_ids
        tracked_sandbox_ids.extend(sandbox_ids)

        # Verify each sandbox is truly independent.
        async def verify(index: int, sandbox_id: str) -> None:
            async with _mcp_session(server_endpoint) as session:
                result = await _call_sandbox_run_command(
                    session,
                    command=["cat", "/tmp/keep.txt"],
                    timeout_seconds=10,
                    sandbox_id=sandbox_id,
                )
                data = _parse_json_result(result)
                assert data["exit_code"] == 0, data
                assert f"worker-{index}" in data["stdout"], data

        await asyncio.gather(*[
            verify(i, sandbox_id) for i, sandbox_id in enumerate(sandbox_ids)
        ])
