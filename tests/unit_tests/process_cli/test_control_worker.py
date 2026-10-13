# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.channels.process_cli import control_worker


class FakeClient:
    def __init__(self):
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def close(self):
        self.calls.append("close")

    def list_model_capabilities(self):
        self.calls.append("model.list")
        return SimpleNamespace(to_dict=lambda: {"models": [{"selection_key": "test"}]})


@pytest.mark.asyncio
async def test_private_query_closes_runtime_before_return():
    client = FakeClient()
    result = await control_worker.run(
        SimpleNamespace(operation="model.list", timeout=1),
        client_factory=lambda: client,
    )
    assert client.calls == ["start", "model.list", "close"]
    assert result["status"] == "completed"
    assert result["data"]["models"][0]["selection_key"] == "test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("startup failed"), asyncio.CancelledError(), SystemExit(1)]
)
async def test_private_query_closes_after_partial_start(failure):
    class Client(FakeClient):
        async def start(self):
            self.calls.append("start")
            raise failure

    client = Client()
    result = await control_worker.run(
        SimpleNamespace(operation="model.list", timeout=1),
        client_factory=lambda: client,
    )
    assert result["status"] == "failed"
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
async def test_private_query_timeout_closes_runtime():
    class Client(FakeClient):
        async def start(self):
            self.calls.append("start")
            await asyncio.sleep(10)

    client = Client()
    result = await control_worker.run(
        SimpleNamespace(operation="model.list", timeout=0.01),
        client_factory=lambda: client,
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TIMEOUT"
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
async def test_private_cleanup_failure_invalidates_success():
    class Client(FakeClient):
        async def close(self):
            self.calls.append("close")
            raise RuntimeError("cleanup failed")

    client = Client()
    result = await control_worker.run(
        SimpleNamespace(operation="model.list", timeout=1),
        client_factory=lambda: client,
    )
    assert result["status"] == "failed"
    assert result["data"] is None
    assert result["error"]["code"] == "SHUTDOWN_FAILED"


@pytest.mark.parametrize(
    "operation,params",
    [("protocol.schema", {}), ("model.list", {"unexpected": True}), ("chat", {})],
)
def test_private_worker_rejects_unrelated_operations(operation, params):
    with pytest.raises(ValueError):
        control_worker.build_command(
            operation, cwd="D:/workspace", params=params, timeout=30
        )


def test_private_worker_preserves_search_text_that_starts_with_a_dash():
    command = control_worker.build_command(
        "session.list",
        cwd="D:/workspace",
        params={"search": "--hello 世界"},
        timeout=30,
    )
    assert command[-1] == "--search=--hello 世界"
