"""Process-entry lifecycle contracts for unified telemetry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.telemetry import runtime as runtime_module


class _Runtime:
    def __init__(
        self,
        events: list[object],
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ):
        self._events = events
        self._start_error = start_error
        self._stop_error = stop_error

    async def start(self, *, process_role, registry, extension_manager):
        self._events.append(
            ("runtime.start", process_role, registry, extension_manager)
        )
        if self._start_error is not None:
            raise self._start_error

    async def stop(self) -> None:
        self._events.append("runtime.stop")
        if self._stop_error is not None:
            raise self._stop_error


class _ExtensionManager:
    def __init__(
        self,
        events: list[object],
        *,
        load_error: BaseException | None = None,
        shutdown_error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._load_error = load_error
        self._shutdown_error = shutdown_error

    async def load_all_extensions(self) -> None:
        self._events.append("extensions.load")
        if self._load_error is not None:
            raise self._load_error

    async def shutdown_all_extensions(self) -> None:
        self._events.append("extensions.shutdown")
        if self._shutdown_error is not None:
            raise self._shutdown_error


@pytest.mark.asyncio
@pytest.mark.parametrize("process_role", ["agentserver", "gateway"])
async def test_process_lifecycle_orders_extensions_and_runtime(
    monkeypatch: pytest.MonkeyPatch,
    process_role: str,
) -> None:
    events: list[object] = []
    registry = object()
    manager = _ExtensionManager(events)
    runtime = _Runtime(events)
    monkeypatch.setattr(runtime_module, "_TELEMETRY_RUNTIME", runtime)
    lifecycle = runtime_module.ProcessTelemetryLifecycle(
        logger=SimpleNamespace(warning=lambda *_args: None),
        process_name=process_role,
    )

    await manager.load_all_extensions()
    started = await lifecycle.start(
        process_role=process_role,
        registry=registry,
        extension_manager=manager,
    )
    await lifecycle.stop()

    assert started is runtime
    assert events == [
        "extensions.load",
        ("runtime.start", process_role, registry, manager),
        "runtime.stop",
        "extensions.shutdown",
    ]


@pytest.mark.asyncio
async def test_process_cleanup_degrades_ordinary_errors_and_keeps_order() -> None:
    events: list[object] = []
    warnings: list[tuple[object, ...]] = []
    runtime = _Runtime(events, stop_error=RuntimeError("runtime stop failed"))
    manager = _ExtensionManager(
        events,
        shutdown_error=RuntimeError("extension shutdown failed"),
    )

    await runtime_module.stop_process_telemetry(
        runtime=runtime,
        extension_manager=manager,
        logger=SimpleNamespace(warning=lambda *args: warnings.append(args)),
        process_name="agentserver",
    )

    assert events == ["runtime.stop", "extensions.shutdown"]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_lifecycle_guard_cleans_partial_runtime_when_start_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    registry = object()
    manager = _ExtensionManager(events)
    runtime = _Runtime(events, start_error=asyncio.CancelledError())
    monkeypatch.setattr(runtime_module, "_TELEMETRY_RUNTIME", runtime)
    lifecycle = runtime_module.ProcessTelemetryLifecycle(
        logger=SimpleNamespace(warning=lambda *_args: None),
        process_name="Gateway",
    )

    await manager.load_all_extensions()
    with pytest.raises(asyncio.CancelledError):
        await lifecycle.start(
            process_role="gateway",
            registry=registry,
            extension_manager=manager,
        )
    await lifecycle.stop()

    assert events == [
        "extensions.load",
        ("runtime.start", "gateway", registry, manager),
        "runtime.stop",
        "extensions.shutdown",
    ]


@pytest.mark.asyncio
async def test_lifecycle_guard_cleans_partial_extensions_when_load_is_cancelled() -> (
    None
):
    events: list[object] = []
    manager = _ExtensionManager(events, load_error=asyncio.CancelledError())
    lifecycle = runtime_module.ProcessTelemetryLifecycle(
        logger=SimpleNamespace(warning=lambda *_args: None),
        process_name="AgentServer",
    )
    lifecycle.bind_extension_manager(manager)

    with pytest.raises(asyncio.CancelledError):
        await manager.load_all_extensions()
    await lifecycle.stop()

    assert events == ["extensions.load", "extensions.shutdown"]


@pytest.mark.asyncio
async def test_process_cleanup_propagates_cancellation_after_remaining_extensions() -> (
    None
):
    events: list[object] = []
    runtime = _Runtime(events, stop_error=asyncio.CancelledError())
    manager = _ExtensionManager(events)

    with pytest.raises(asyncio.CancelledError):
        await runtime_module.stop_process_telemetry(
            runtime=runtime,
            extension_manager=manager,
            logger=SimpleNamespace(warning=lambda *_args: None),
            process_name="gateway",
        )

    assert events == ["runtime.stop", "extensions.shutdown"]


@pytest.mark.asyncio
async def test_gateway_restart_runs_after_telemetry_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway import app_gateway

    events: list[str] = []

    class _Lifecycle:
        def __init__(self, **_kwargs) -> None:
            pass

        async def stop(self) -> None:
            events.extend(["runtime.stop", "extensions.shutdown"])

    async def _request_restart(*_args) -> bool:
        events.append("gateway.run")
        return True

    monkeypatch.setattr(runtime_module, "ProcessTelemetryLifecycle", _Lifecycle)
    monkeypatch.setattr(app_gateway, "_run_with_telemetry", _request_restart)
    monkeypatch.setattr(
        app_gateway,
        "_exec_gateway_restart",
        lambda: events.append("gateway.exec"),
    )

    await app_gateway._run("ws://agentserver", "127.0.0.1", 8080, "/")

    assert events == [
        "gateway.run",
        "runtime.stop",
        "extensions.shutdown",
        "gateway.exec",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("built_in", [True, False])
async def test_gateway_connects_raw_client_before_wrapping_and_building_handler(
    monkeypatch: pytest.MonkeyPatch,
    built_in: bool,
) -> None:
    from jiuwenswarm.gateway import app_gateway
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.telemetry import gateway_client as gateway_client_module

    events: list[object] = []

    class _ExtensionClient:
        async def connect(self, uri: str) -> None:
            events.append(("extension.connect", uri))

    raw_client = WebSocketAgentServerClient() if built_in else _ExtensionClient()
    proxy = object()

    async def _connect_with_retry(client, uri, **kwargs) -> None:
        assert client is raw_client
        events.append(("built-in.connect", uri, kwargs))

    def _wrap(client, *, target_uri):
        assert client is raw_client
        events.append(("wrap", target_uri))
        return proxy

    class _MessageHandler:
        def __init__(self, client) -> None:
            events.append(("handler", client))

    monkeypatch.setattr(app_gateway, "_connect_with_retry", _connect_with_retry)
    monkeypatch.setattr(gateway_client_module, "wrap_gateway_agent_client", _wrap)

    selected, handler = await app_gateway._connect_wrap_and_create_message_handler(
        raw_client,
        agent_server_url="ws://agent.test:9766/ws",
        max_retries=4,
        retry_interval=0.25,
        message_handler_factory=_MessageHandler,
    )

    assert selected is proxy
    assert isinstance(handler, _MessageHandler)
    if built_in:
        assert events == [
            (
                "built-in.connect",
                "ws://agent.test:9766/ws",
                {"max_retries": 4, "interval": 0.25},
            ),
            ("wrap", "ws://agent.test:9766/ws"),
            ("handler", proxy),
        ]
    else:
        assert events == [
            ("extension.connect", ""),
            ("wrap", None),
            ("handler", proxy),
        ]
