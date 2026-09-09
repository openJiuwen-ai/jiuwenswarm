"""Embedded PersonalContext Host lifecycle contracts for AgentWebSocketServer."""

from __future__ import annotations

import asyncio
import ast
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


AGENT_WS_SERVER_PATH = (
    Path(__file__).parents[3] / "jiuwenswarm" / "server" / "agent_ws_server.py"
)


class _FakePersonalContextHost:
    instances: list["_FakePersonalContextHost"] = []

    def __init__(self, *, home: str | Path) -> None:
        self.home = Path(home)
        self.events: list[tuple[str, object]] = []
        self.runtime_enabled = False
        self.__class__.instances.append(self)

    async def start(self) -> None:
        self.events.append(("start", None))

    async def is_runtime_enabled(self) -> bool:
        return self.runtime_enabled

    async def stop(self, *, timeout_seconds: float = 30.0) -> None:
        self.events.append(("stop", timeout_seconds))


class _FakeWebSocketServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.events.append("ws-close")
        self.closed = True

    async def wait_closed(self) -> None:
        self.events.append("ws-wait-closed")


class _FailingWebSocketServer:
    def close(self) -> None:
        raise RuntimeError("close failed")

    async def wait_closed(self) -> None:
        raise AssertionError("wait_closed should not run after close failure")


@pytest.fixture(autouse=True)
def _reset_fake_host() -> None:
    _FakePersonalContextHost.instances.clear()


def _server(monkeypatch: pytest.MonkeyPatch) -> AgentWebSocketServer:
    monkeypatch.setattr(
        server_module, "PersonalContextHostAPI", _FakePersonalContextHost
    )
    return AgentWebSocketServer(host="127.0.0.1", port=0)


def test_personal_context_cancellation_handlers_do_not_raise_inside_except() -> None:
    tree = ast.parse(AGENT_WS_SERVER_PATH.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }

    for function_name in (
        "_start_personal_context_best_effort",
        "_stop_personal_context_best_effort",
    ):
        function = functions[function_name]
        for handler in ast.walk(function):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            if ast.unparse(handler.type) != "asyncio.CancelledError":
                continue
            assert not any(
                isinstance(statement, ast.Raise) and statement.exc is None
                for statement in handler.body
            ), function_name


@pytest.mark.asyncio
async def test_agentserver_constructs_one_host_at_fixed_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("C:/users/tester")))
    server = _server(monkeypatch)

    assert len(_FakePersonalContextHost.instances) == 1
    assert _FakePersonalContextHost.instances[0].home == Path(
        "C:/users/tester/.jiuwenswarm/.personal_context"
    )
    assert server._personal_context_host is _FakePersonalContextHost.instances[0]  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_start_opens_websocket_without_waiting_for_personal_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    personal_context_started = asyncio.Event()
    release_personal_context = asyncio.Event()
    events: list[str] = []

    async def _delayed_start() -> None:
        host.events.append(("start", None))
        personal_context_started.set()
        await release_personal_context.wait()

    async def _serve(*_args: object, **_kwargs: object) -> _FakeWebSocketServer:
        events.append("ws-serve")
        return _FakeWebSocketServer(events)

    host.start = _delayed_start  # type: ignore[method-assign]
    monkeypatch.setattr(server_module, "reset_harness_packages_state", lambda: None)
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    async def _checkpointer() -> None:
        return None

    monkeypatch.setattr(interface_deep, "ensure_persistent_checkpointer", _checkpointer)
    monkeypatch.setattr("websockets.legacy.server.serve", _serve)
    monkeypatch.setattr(server, "_bootstrap_internal_jiuwenbox", _noop_async)

    await server.start()
    task = server._personal_context_start_task  # pylint: disable=protected-access
    try:
        await asyncio.wait_for(personal_context_started.wait(), timeout=1.0)

        assert events == ["ws-serve"]
        assert task is not None
        release_personal_context.set()
        await task
    finally:
        release_personal_context.set()
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_start_restores_enabled_personal_context_state_to_agent_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    host.runtime_enabled = True
    manager_update = AsyncMock()
    server._agent_manager.set_personal_context_runtime_enabled = manager_update  # type: ignore[method-assign]
    events: list[str] = []

    async def _serve(*_args: object, **_kwargs: object) -> _FakeWebSocketServer:
        events.append("ws-serve")
        return _FakeWebSocketServer(events)

    monkeypatch.setattr(server_module, "reset_harness_packages_state", lambda: None)
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    async def _checkpointer() -> None:
        return None

    monkeypatch.setattr(interface_deep, "ensure_persistent_checkpointer", _checkpointer)
    monkeypatch.setattr("websockets.legacy.server.serve", _serve)
    monkeypatch.setattr(server, "_bootstrap_internal_jiuwenbox", _noop_async)

    await server.start()
    task = server._personal_context_start_task  # pylint: disable=protected-access
    assert task is not None
    await task

    assert events == ["ws-serve"]
    manager_update.assert_awaited_once_with(True)


@pytest.mark.asyncio
async def test_stop_calls_host_even_when_websocket_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)

    await server.stop()

    assert _FakePersonalContextHost.instances[0].events == [("stop", 30.0)]


@pytest.mark.asyncio
async def test_personal_context_start_failure_does_not_fail_agentserver_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    events: list[str] = []

    async def _failed_start() -> None:
        host.events.append(("start", None))
        raise RuntimeError("bad personal_context config")

    async def _serve(*_args: object, **_kwargs: object) -> _FakeWebSocketServer:
        events.append("ws-serve")
        return _FakeWebSocketServer(events)

    host.start = _failed_start  # type: ignore[method-assign]
    monkeypatch.setattr(server_module, "reset_harness_packages_state", lambda: None)
    monkeypatch.setattr("websockets.legacy.server.serve", _serve)
    monkeypatch.setattr(server, "_bootstrap_internal_jiuwenbox", _noop_async)

    await server.start()
    assert server._personal_context_start_task is not None  # pylint: disable=protected-access
    await server._personal_context_start_task  # pylint: disable=protected-access

    assert server._server is not None  # pylint: disable=protected-access
    assert events == ["ws-serve"]


@pytest.mark.asyncio
async def test_personal_context_stop_failure_does_not_change_normal_stop_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    events: list[str] = []

    async def _failed_stop(*, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        raise RuntimeError("PersonalContext stop failed")

    host.stop = _failed_stop  # type: ignore[method-assign]
    server._server = _FakeWebSocketServer(events)  # pylint: disable=protected-access
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks.cancel_pending_tasks",
        _noop_async,
    )
    monkeypatch.setattr(server._jiuwenbox_runner, "stop", _noop_async)

    await server.stop()

    assert events == ["ws-close", "ws-wait-closed"]


@pytest.mark.asyncio
async def test_stop_finishes_main_services_before_personal_context_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    events: list[str] = []

    async def _record_personal_context_stop(*, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        events.append("personal-context-stop")

    host.stop = _record_personal_context_stop  # type: ignore[method-assign]
    server._server = _FakeWebSocketServer(events)  # pylint: disable=protected-access
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks.cancel_pending_tasks",
        _noop_async,
    )
    monkeypatch.setattr(server._jiuwenbox_runner, "stop", _noop_async)

    await server.stop()

    assert events == ["ws-close", "ws-wait-closed", "personal-context-stop"]


@pytest.mark.asyncio
async def test_stop_calls_host_when_websocket_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]
    server._server = _FailingWebSocketServer()  # pylint: disable=protected-access

    with pytest.raises(RuntimeError, match="close failed"):
        await server.stop()

    assert host.events == [("stop", 30.0)]


@pytest.mark.asyncio
async def test_stop_does_not_swallow_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch)
    host = _FakePersonalContextHost.instances[0]

    async def _cancelled_stop(*, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        raise asyncio.CancelledError

    host.stop = _cancelled_stop  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await server.stop()


async def _noop_async() -> None:
    return None
