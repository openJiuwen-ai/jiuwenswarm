# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import builtins
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.channels.process_cli import query, query_entry
from jiuwenswarm.channels.process_cli.protocol.query import (
    OneShotQueryInput,
    OneShotQueryResult,
)


def document(operation="mode.list", **overrides):
    return {
        "schema_version": "0.1",
        "type": "query",
        "request_id": "q1",
        "operation": operation,
        **overrides,
    }


@pytest.mark.parametrize(
    "operation,params,method",
    [
        ("mode.list", {}, "list_mode_capabilities"),
        ("mode.resolve", {"requested": "agent.code.plan"}, "resolve_mode_capability"),
        ("model.list", {}, "list_model_capabilities"),
        ("model.resolve", {"requested": "model"}, "resolve_model_capability"),
        ("session.get", {"session_id": "owned"}, "get_session"),
        ("session.list", {"limit": 2, "offset": 1}, "list_sessions"),
        ("permission.get", {"session_id": "owned"}, "get_permission_snapshot"),
        ("mcp.validate", {"references": ["server"]}, "validate_mcp_references"),
    ],
)
@pytest.mark.asyncio
async def test_query_public_dispatch_and_close(operation, params, method):
    calls = []
    value = SimpleNamespace(to_dict=lambda: {"safe": True})
    client = SimpleNamespace(
        start=AsyncMock(side_effect=lambda: calls.append("start")),
        close=AsyncMock(side_effect=lambda: calls.append("close")),
    )
    methods = (
        "list_mode_capabilities",
        "resolve_mode_capability",
        "list_model_capabilities",
        "resolve_model_capability",
        "get_session",
        "list_sessions",
        "get_permission_snapshot",
        "validate_mcp_references",
    )
    for name in methods:
        setattr(client, name, Mock(return_value=value))
    result = await query.run_query(
        OneShotQueryInput.from_dict(document(operation, params=params)),
        "q1",
        client_factory=lambda: client,
    )
    assert result.exit_code == 0
    assert result.to_dict()["session_id"] is None
    assert calls == ["start", "close"]
    getattr(client, method).assert_called_once()
    for name in set(methods) - {method}:
        getattr(client, name).assert_not_called()
    if operation.startswith("session.") or operation == "permission.get":
        assert getattr(client, method).call_args.args[0].channel_id == "process_cli"


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "run"},
        {"schema_version": "1.0"},
        {"operation": "session.delete"},
        {"operation": []},
        {"params": {"channel_id": "tui"}},
        {"input": "do not run"},
        {"timeout_seconds": True},
        {"timeout_seconds": float("inf")},
        {"operation": "session.list", "params": {"limit": 0}},
        {"operation": "session.list", "params": {"limit": 201}},
        {"operation": "session.list", "params": {"offset": True}},
        {"operation": "session.get", "params": {}},
        {"operation": "session.get", "params": {"session_id": " "}},
        {"operation": "mcp.validate", "params": {"references": "server"}},
        {"operation": "mcp.validate", "params": {"references": [None]}},
    ],
)
def test_query_schema_rejects_before_runtime(overrides):
    with pytest.raises((TypeError, ValueError)):
        OneShotQueryInput.from_dict({**document(), **overrides})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("secret"), builtins.SystemExit(9), asyncio.CancelledError()],
)
async def test_query_partial_start_closes(failure):
    client = SimpleNamespace(start=AsyncMock(side_effect=failure), close=AsyncMock())
    result = await query.run_query(
        OneShotQueryInput(operation="mode.list"), "q1", client_factory=lambda: client
    )
    assert result.exit_code == (
        130 if isinstance(failure, asyncio.CancelledError) else 1
    )
    assert "secret" not in json.dumps(result.to_dict())
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_timeout_cleanup_failure_preserves_primary_error(monkeypatch):
    async def start():
        await asyncio.sleep(1)

    client = SimpleNamespace(
        start=start, close=AsyncMock(side_effect=ValueError("secret"))
    )
    result = await query.run_query(
        OneShotQueryInput(operation="mode.list", timeout_seconds=0.01),
        "q1",
        client_factory=lambda: client,
    )
    assert result.exit_code == 124
    assert result.error.code == "TIMEOUT"
    assert result.error.details["cleanup_errors"] == ("runtime_close",)


@pytest.mark.asyncio
async def test_query_close_failure_never_returns_success():
    client = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(side_effect=RuntimeError()),
        list_mode_capabilities=lambda: SimpleNamespace(to_dict=lambda: {}),
    )
    result = await query.run_query(
        OneShotQueryInput(operation="mode.list"), "q1", client_factory=lambda: client
    )
    assert result.exit_code == 1
    assert result.error.code == "SHUTDOWN_FAILED"
    assert result.data is None


@pytest.mark.asyncio
async def test_missing_session_is_null_not_adopted():
    client = SimpleNamespace(get_session=lambda _: None)
    assert query.dispatch_query(
        client,
        OneShotQueryInput(operation="session.get", params={"session_id": "missing"}),
    ) == {"session": None}


def test_query_terminal_after_runtime_close(monkeypatch, capsys):
    async def read(_):
        return OneShotQueryInput(operation="mode.list", request_id="q1")

    async def run(request, request_id):
        assert capsys.readouterr().out == ""
        return OneShotQueryResult(
            request_id=request_id, operation=request.operation, data={"modes": []}
        )

    monkeypatch.setattr(query_entry, "_read_query", read)
    monkeypatch.setattr(query, "run_query", run)
    assert query_entry.execute_query_source("-") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["type"] == "query_result"
    assert result["sequence"] == 0
    assert result["request_id"] == "q1"


def test_query_conflicts_without_runtime(monkeypatch, capsys):
    monkeypatch.setattr(
        query_entry, "_read_query", Mock(side_effect=AssertionError("not called"))
    )
    assert query_entry.execute_query_source("-", conflicting_arguments=True) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_synchronous_query_cannot_silently_exceed_deadline():
    def catalog():
        time.sleep(0.03)
        return SimpleNamespace(to_dict=lambda: {})

    client = SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(), list_mode_capabilities=catalog
    )
    result = await query.run_query(
        OneShotQueryInput(operation="mode.list", timeout_seconds=0.005),
        "q1",
        client_factory=lambda: client,
    )
    assert result.exit_code == 124
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "source",
    [
        '{"schema_version":"0.1","type":"query","operation":"mode.list","operation":"model.list"}',
        '{"schema_version":"0.1","type":"query","operation":"session.delete"}',
        '{"schema_version":"0.1","type":"query","operation":"session.list","params":{"channel_id":"tui"}}',
        '{"schema_version":"0.1","type":"query","operation":"mode.list","timeout_seconds":NaN}',
        "[]",
        "not json",
        "",
    ],
)
def test_invalid_query_real_entry_never_imports_runtime(source):
    code = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('jiuwenswarm.runtime', 'jiuwenswarm.channels.process_cli.client', 'jiuwenswarm.channels.process_cli.repl')):
            raise AssertionError('Runtime imported for invalid input')
sys.meta_path.insert(0, Guard())
from jiuwenswarm.channels.process_cli.main import main
main()
"""
    process = subprocess.run(
        [sys.executable, "-c", code, "--query-json", "-"],
        input=source.encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 2, process.stderr
    records = process.stdout.decode("utf-8").splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["error"]["code"] == "INVALID_INPUT"
