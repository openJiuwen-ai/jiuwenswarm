# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A resident Worker advertises its launch code across Front reconnections."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN, KIND_SHUTDOWN_DONE
from jiuwenswarm.server.supervisor import identity
from jiuwenswarm.server.worker import service, stub


@pytest.mark.asyncio
@pytest.mark.parametrize("first_hello_fails", [False, True])
async def test_resident_service_pins_identity_even_after_failed_first_hello(monkeypatch, first_hello_fails):
    disk = {"identity": "launch-code"}
    hash_current_disk = Mock(side_effect=lambda: disk["identity"])
    runtime_started = asyncio.Event()
    runtime = SimpleNamespace(shutdown=AsyncMock())

    class Connection:
        def __init__(self, index):
            self.index = index
            self.frames = []
            self.closed = False

        async def send(self, frame):
            self.frames.append(frame)
            if self.index == 0 and first_hello_fails and frame["kind"] == KIND_HELLO:
                raise OSError("first Front disappeared while receiving HELLO")

        async def recv(self):
            await runtime_started.wait()
            if self.index == 0:
                raise OSError("Front restarted after Runtime loaded")
            return {"kind": KIND_SHUTDOWN}

        async def close(self):
            self.closed = True

    connections = [Connection(0), Connection(1)]

    class Server:
        def __init__(self, _path):
            self.accepted = 0
            self.started = False
            self.closed = False

        async def start(self):
            self.started = True

        async def accept(self):
            assert hash_current_disk.call_count == 1
            if self.accepted:
                disk["identity"] = "new-disk-code"
            connection = connections[self.accepted]
            self.accepted += 1
            return connection

        async def close(self):
            self.closed = True

    server = Server("unused")

    async def load_runtime(_host, _port, _signals, _dispatcher, loaded):
        loaded["runtime"] = runtime
        runtime_started.set()
        return runtime

    loader = AsyncMock(side_effect=load_runtime)
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    monkeypatch.setattr(service, "IpcServer", lambda _path: server)
    monkeypatch.setattr(service, "_load_runtime", loader)

    # Exercise the actual service loop, HELLO, detach and explicit shutdown.
    # Fake Runtime owns no SDK resources, and the test uses no source mutation.
    await service.serve_worker("unused", host="127.0.0.1", port=0)

    hellos = [next(frame for frame in connection.frames if frame["kind"] == KIND_HELLO)
              for connection in connections]
    assert [frame["code_identity"] for frame in hellos] == ["launch-code", "launch-code"]
    assert hellos[0]["instance"] == hellos[1]["instance"]
    assert hellos[0]["pid"] == hellos[1]["pid"]
    assert hellos[0]["generation"] != hellos[1]["generation"]
    assert disk["identity"] == "new-disk-code"
    hash_current_disk.assert_called_once_with()
    loader.assert_awaited_once()
    runtime.shutdown.assert_awaited_once()
    assert server.started and server.closed
    assert all(connection.closed for connection in connections)
    assert connections[1].frames[-1] == {"kind": KIND_SHUTDOWN_DONE, "ok": True}


@pytest.mark.asyncio
async def test_standalone_hello_without_pinned_value_keeps_legacy_current_identity(monkeypatch):
    hash_current_disk = Mock(return_value="standalone-current-code")
    connection = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    await service._send_hello(connection, runtime_loaded=False, generation=1, instance="standalone")
    hash_current_disk.assert_called_once_with()
    assert connection.send.await_args.args[0]["code_identity"] == "standalone-current-code"


@pytest.mark.asyncio
async def test_stub_pins_identity_across_front_reconnections(monkeypatch):
    disk = {"identity": "stub-launch-code"}
    hash_current_disk = Mock(side_effect=lambda: disk["identity"])
    frames = [[], []]
    index = 0

    class Connection:
        def __init__(self, position):
            self.position = position

        async def send(self, frame):
            frames[self.position].append(frame)

        async def recv(self):
            if self.position == 0:
                disk["identity"] = "stub-new-disk-code"
                raise OSError("Front detached")
            return {"kind": KIND_SHUTDOWN}

    async def accept():
        nonlocal index
        connection = Connection(index)
        index += 1
        return connection

    server = SimpleNamespace(start=AsyncMock(), accept=accept, close=AsyncMock())
    monkeypatch.setattr(identity, "code_identity", hash_current_disk)
    monkeypatch.setattr(identity, "data_scope", lambda: "trusted-data-scope")
    monkeypatch.setattr(stub, "IpcServer", lambda _path: server)

    await stub.serve_stub("unused")

    hellos = [next(frame for frame in connection if frame["kind"] == KIND_HELLO) for connection in frames]
    assert [frame["code_identity"] for frame in hellos] == ["stub-launch-code", "stub-launch-code"]
    assert hellos[0]["instance"] == hellos[1]["instance"]
    hash_current_disk.assert_called_once_with()
    server.close.assert_awaited_once()
