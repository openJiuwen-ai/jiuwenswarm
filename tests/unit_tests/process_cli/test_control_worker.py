# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.channels.process_cli import control_worker


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def close(self) -> None:
        self.calls.append("close")

    def list_model_capabilities(self) -> SimpleNamespace:
        self.calls.append("model.list")
        return SimpleNamespace(to_dict=lambda: {"models": [{"selection_key": "test"}]})


@pytest.mark.asyncio
async def test_private_query_closes_runtime_before_return() -> None:
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
    "failure", [RuntimeError("startup failed"), asyncio.CancelledError()]
)
async def test_private_query_closes_after_partial_start(failure: BaseException) -> None:
    class Client(FakeClient):
        async def start(self) -> None:
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
@pytest.mark.parametrize("stage", ["start", "close"])
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_process_exit_propagates_after_runtime_cleanup(
    stage: str,
    exit_code: int,
) -> None:
    client = FakeClient()
    start = AsyncMock(wraps=client.start)
    close = AsyncMock(wraps=client.close)
    failing_operation = start if stage == "start" else close
    failing_operation.side_effect = SystemExit(exit_code)
    runtime = SimpleNamespace(
        start=start,
        close=close,
        list_model_capabilities=client.list_model_capabilities,
    )

    with pytest.raises(SystemExit) as caught:
        await control_worker.run(
            SimpleNamespace(operation="model.list", timeout=1),
            client_factory=lambda: runtime,
        )

    assert caught.value.code == exit_code
    start.assert_awaited_once()
    close.assert_awaited_once()
    assert ("model.list" in client.calls) == (stage == "close")


@pytest.mark.asyncio
async def test_private_query_timeout_closes_runtime() -> None:
    class Client(FakeClient):
        async def start(self) -> None:
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
async def test_private_cleanup_failure_invalidates_success() -> None:
    class Client(FakeClient):
        async def close(self) -> None:
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
def test_private_worker_rejects_unrelated_operations(
    operation: str, params: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        control_worker.build_command(
            operation, cwd="D:/workspace", params=params, timeout=30
        )


def test_private_worker_preserves_search_text_that_starts_with_a_dash() -> None:
    command = control_worker.build_command(
        "session.list",
        cwd="D:/workspace",
        params={"search": "--hello 世界"},
        timeout=30,
    )
    assert command[-1] == "--search=--hello 世界"
