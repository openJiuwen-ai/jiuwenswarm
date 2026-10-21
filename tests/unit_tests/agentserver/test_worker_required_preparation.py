"""Required code/configuration overlap while consumers retain a joined barrier."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.runtime.team_controller import LazyTeamExecutionController
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.server.worker import runtime_backend, runtime_bootstrap
from jiuwenswarm.server.worker.telemetry import StartupTelemetry


@pytest.mark.asyncio
async def test_required_configuration_and_sdk_import_overlap_before_runtime_patches(monkeypatch):
    barrier = threading.Barrier(2, timeout=2)
    complete = []

    def prepare(name):
        barrier.wait()
        complete.append(name)

    monkeypatch.setenv("JIUWENSWARM_RUNTIME_WORKSPACE_READY", "1")
    monkeypatch.setattr(runtime_backend, "_prepare_worker_configuration", lambda: prepare("config"))
    monkeypatch.setattr(runtime_backend, "_preload_sdk_execution_modules", lambda: prepare("sdk"))

    def apply_after_join():
        assert set(complete) == {"config", "sdk"}

    monkeypatch.setattr(runtime_backend, "_configure_openjiuwen_logging", apply_after_join)
    monkeypatch.setattr(runtime_backend, "_apply_runtime_entry_patches", apply_after_join)
    await asyncio.to_thread(runtime_backend.preload_runtime_backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["configuration", "sdk"])
async def test_required_preparation_failure_prevents_runtime_consumers(monkeypatch, failed_stage):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_WORKSPACE_READY", "1")

    def fail():
        raise ValueError("invalid model migration")

    monkeypatch.setattr(runtime_backend, "_prepare_worker_configuration",
                        fail if failed_stage == "configuration" else lambda: None)
    monkeypatch.setattr(runtime_backend, "_preload_sdk_execution_modules",
                        fail if failed_stage == "sdk" else lambda: None)
    monkeypatch.setattr(runtime_backend, "_configure_openjiuwen_logging",
                        lambda: pytest.fail("configuration did not become usable"))
    monkeypatch.setattr(runtime_backend, "_apply_runtime_entry_patches",
                        lambda: pytest.fail("configuration did not become usable"))
    with pytest.raises(ValueError, match="invalid model migration"):
        await asyncio.to_thread(runtime_backend.preload_runtime_backend)


@pytest.mark.asyncio
async def test_successful_construction_retains_cleanup_owner_when_parallel_extensions_fail(
    monkeypatch, worker_callback_framework,
):
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.worker import service

    loop = asyncio.get_running_loop()
    constructed = asyncio.Event()
    server = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    extension_error = OSError("extension directory cannot be read")

    def construct(**kwargs):
        assert kwargs == {"host": "127.0.0.1", "port": 18000}
        loop.call_soon_threadsafe(constructed.set)
        return server

    async def load_extensions(self, *, include_transport_extensions):
        assert include_transport_extensions is False
        await constructed.wait()
        raise extension_error

    monkeypatch.setattr(runtime_backend, "preload_runtime_backend", lambda: None)
    monkeypatch.setattr(runtime_backend, "preload_shared_execution_code", lambda: None)
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", construct)
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", load_extensions)
    signals = SimpleNamespace(failed=Mock())
    dispatcher = SimpleNamespace(bind_runtime=AsyncMock(), mark_failed=AsyncMock())
    loaded = {}
    with pytest.raises(OSError, match="extension directory cannot be read") as caught:
        await service._load_runtime("127.0.0.1", 18000, signals, dispatcher, loaded)

    assert caught.value is extension_error
    assert loaded["runtime"].server is server
    dispatcher.bind_runtime.assert_awaited_once_with(server)
    server.start.assert_not_awaited()
    signals.failed.assert_called_once_with(str(extension_error))
    dispatcher.mark_failed.assert_awaited_once_with(str(extension_error))
    # Shutdown can recover the exact constructed owner even though the loader
    # task failed, rather than losing it before any Runtime consumer started.
    failed_loader = asyncio.create_task(_raise(extension_error))
    assert await service._runtime_for_shutdown(failed_loader, loaded, timeout=1) is loaded["runtime"]


async def _raise(error):
    raise error


@pytest.mark.asyncio
async def test_execution_code_overlaps_construction_and_failure_retains_owner(
    monkeypatch, worker_callback_framework,
):
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    barrier = threading.Barrier(2, timeout=2)
    server = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    owners = []

    def construct(**kwargs):
        barrier.wait()
        return server

    def prepare_code():
        barrier.wait()
        raise ImportError("required execution module failed")

    async def own(value):
        owners.append(value)

    monkeypatch.setattr(runtime_backend, "preload_shared_execution_code", prepare_code)
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", construct)
    load_extensions = AsyncMock()
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", load_extensions)
    with pytest.raises(ImportError, match="required execution module failed"):
        await runtime_bootstrap.construct_runtime_server(
            "127.0.0.1", 18000, on_constructed=own, telemetry=StartupTelemetry()
        )
    assert owners == [server]
    server.start.assert_not_awaited()
    load_extensions.assert_not_awaited()


@pytest.mark.asyncio
async def test_extensions_observe_execution_patches_after_parallel_server_construction(
    monkeypatch, worker_callback_framework,
):
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    overlap = threading.Barrier(2, timeout=2)
    patches_installed = threading.Event()
    events = []
    server = SimpleNamespace(start=AsyncMock())

    def prepare_code():
        overlap.wait()
        patches_installed.set()

    def construct(**kwargs):
        overlap.wait()
        events.append("constructed")
        return server

    async def load_extensions(self, *, include_transport_extensions):
        assert include_transport_extensions is False
        assert patches_installed.is_set()
        events.append("extensions")

    async def retain_owner(value):
        assert value is server
        assert set(events) == {"constructed", "extensions"}
        events.append("retained")

    monkeypatch.setattr(runtime_backend, "preload_shared_execution_code", prepare_code)
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", construct)
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", load_extensions)

    actual = await runtime_bootstrap.construct_runtime_server(
        "127.0.0.1", 18000, on_constructed=retain_owner, telemetry=StartupTelemetry(),
    )

    assert actual is server
    assert events[-1] == "retained"
    server.start.assert_not_awaited()


def test_team_controller_preserves_reporter_without_constructing_unused_team_mode():
    calls = []
    reporter = object()
    controller = SimpleNamespace(set_session_inactive_reporter=lambda value: calls.append(value),
                                 quiesce_session=object())

    def factory():
        calls.append("constructed")
        return controller

    lazy = LazyTeamExecutionController(factory)
    lazy.set_session_inactive_reporter(reporter)
    assert calls == []
    assert lazy.quiesce_session is controller.quiesce_session
    assert calls == ["constructed", reporter]
    assert lazy.quiesce_session is controller.quiesce_session
    assert calls == ["constructed", reporter]


@pytest.mark.asyncio
async def test_first_team_turn_activates_existing_owner_before_execution(monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.runtime import request as request_module

    calls = []
    controller = SimpleNamespace(set_session_inactive_reporter=lambda callback: calls.append(callback))
    lazy = LazyTeamExecutionController(lambda: controller)
    runtime = AgentRuntime(team_execution_controller=lazy, initializer=AsyncMock())
    await runtime.start()
    prepared_agent = object()
    monkeypatch.setattr(request_module, "prepare_chat_turn",
                        AsyncMock(return_value=("team", None, prepared_agent)))
    try:
        request = AgentRequest(request_id="team", channel_id="web", session_id="team-first",
                               params={"mode": "team.work.normal"})
        assert not calls
        prepared = await runtime._prepare_chat_turn(request, "web")
        assert prepared[2] is prepared_agent
        assert calls == [runtime.record_session_inactive_by_id]
    finally:
        await runtime.close()
