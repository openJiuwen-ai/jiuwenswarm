"""Lifecycle tests for the unified telemetry runtime."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.provider import ProviderBundle
from jiuwenswarm.telemetry import runtime as runtime_module
from jiuwenswarm.telemetry.runtime import (
    ComponentStatus,
    RuntimeState,
    TelemetryRuntime,
)


class ProxyTracerProvider:
    pass


class ProxyLoggerProvider:
    pass


class _ProxyMeterProvider:
    pass


class _Provider:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.processors: list[object] = []
        self.flush_calls = 0
        self.shutdown_calls = 0

    def add_span_processor(self, processor: object) -> None:
        self.events.append("registry.install")
        self.processors.append(processor)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        self.events.append(f"{self.name}.flush")
        self.flush_calls += 1
        return True

    def shutdown(self) -> None:
        self.events.append(f"{self.name}.shutdown")
        self.shutdown_calls += 1


class _Globals:
    def __init__(self) -> None:
        self.tracer: object = ProxyTracerProvider()
        self.meter: object = _ProxyMeterProvider()
        self.logger: object = ProxyLoggerProvider()
        self.tracer_sets = 0
        self.meter_sets = 0
        self.logger_sets = 0

    def set_tracer(self, provider: object) -> None:
        self.tracer_sets += 1
        self.tracer = provider

    def set_meter(self, provider: object) -> None:
        self.meter_sets += 1
        self.meter = provider

    def set_logger(self, provider: object) -> None:
        self.logger_sets += 1
        self.logger = provider


class _Callbacks:
    def __init__(self, events: list[str], *, ordering: bool = True) -> None:
        self.events = events
        self.ordering = ordering
        self.register_calls = 0
        self.unregister_calls = 0
        self.register_gate: asyncio.Event | None = None
        self.unregister_errors: list[BaseException] = []

    async def register(self, framework: object) -> None:
        del framework
        self.events.append("callbacks.register")
        self.register_calls += 1
        if self.register_gate is not None:
            await self.register_gate.wait()

    async def unregister(self, framework: object) -> None:
        del framework
        self.events.append("callbacks.unregister")
        self.unregister_calls += 1
        if self.unregister_errors:
            raise self.unregister_errors.pop(0)

    def validate_core_ordering(self, framework: object) -> bool:
        del framework
        return self.ordering


class _Extension:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.shutdown_calls = 0
        self.shutdown_errors: list[BaseException] = []

    async def shutdown(self) -> None:
        self.events.append("extension.shutdown")
        self.shutdown_calls += 1
        if self.shutdown_errors:
            raise self.shutdown_errors.pop(0)


class _Registry:
    def __init__(self, extension: object | None = None) -> None:
        self.extension = extension
        self.callback_framework = object()

    def get_telemetry_provider_extension(self) -> object | None:
        return self.extension


class _ExtensionManager:
    def __init__(self, *, claim: bool = False) -> None:
        self.claim = claim
        self.claim_calls: list[object] = []

    def claim_loaded_extension(self, extension: object) -> bool:
        self.claim_calls.append(extension)
        return self.claim


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    owns_tracer: bool = True,
    owns_meter: bool = True,
    extension: object | None = None,
    claim_extension: bool = False,
    ordering: bool = True,
) -> SimpleNamespace:
    events: list[str] = []
    tracer = _Provider("tracer", events)
    meter = _Provider("meter", events)
    logger = _Provider("logger", events)
    bundle = ProviderBundle(
        tracer_provider=tracer,  # type: ignore[arg-type]
        meter_provider=meter,  # type: ignore[arg-type]
        logger_provider=logger,  # type: ignore[arg-type]
        owns_tracer=owns_tracer,
        owns_meter=owns_meter,
    )
    globals_state = _Globals()
    callbacks = _Callbacks(events, ordering=ordering)
    span_registry = Mock(name="span_registry")
    telemetry_metrics = Mock(name="telemetry_metrics")
    registry = _Registry(extension)
    extension_manager = _ExtensionManager(claim=claim_extension)
    init_core = Mock(side_effect=lambda *args, **kwargs: events.append("core.init"))
    stop_core = Mock(side_effect=lambda: events.append("core.shutdown"))
    provider_builder = Mock(return_value=bundle)
    metrics_builder = Mock(return_value=telemetry_metrics)
    registry_builder = Mock(return_value=span_registry)
    callbacks_builder = Mock(return_value=callbacks)

    monkeypatch.setattr(
        runtime_module,
        "load_telemetry_config",
        Mock(return_value=TelemetryConfig(enabled=enabled)),
    )
    monkeypatch.setattr(runtime_module, "build_provider_bundle", provider_builder)
    monkeypatch.setattr(runtime_module, "SpanRegistryProcessor", registry_builder)
    monkeypatch.setattr(runtime_module, "TelemetryMetrics", metrics_builder)
    monkeypatch.setattr(runtime_module, "RichTelemetryCallbacks", callbacks_builder)
    monkeypatch.setattr(runtime_module, "init_observability", init_core)
    monkeypatch.setattr(runtime_module, "shutdown_observability", stop_core)
    monkeypatch.setattr(
        runtime_module,
        "is_initialized",
        Mock(return_value=False),
    )
    monkeypatch.setattr(
        runtime_module,
        "ObservabilityConfig",
        Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        runtime_module.trace,
        "get_tracer_provider",
        lambda: globals_state.tracer,
    )
    monkeypatch.setattr(
        runtime_module.trace,
        "set_tracer_provider",
        globals_state.set_tracer,
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "get_meter_provider",
        lambda: globals_state.meter,
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "set_meter_provider",
        globals_state.set_meter,
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "get_logger_provider",
        lambda: globals_state.logger,
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "set_logger_provider",
        globals_state.set_logger,
    )
    return SimpleNamespace(
        bundle=bundle,
        callbacks=callbacks,
        callbacks_builder=callbacks_builder,
        events=events,
        extension_manager=extension_manager,
        globals=globals_state,
        init_core=init_core,
        meter=meter,
        logger=logger,
        metrics_builder=metrics_builder,
        provider_builder=provider_builder,
        registry=registry,
        registry_builder=registry_builder,
        span_registry=span_registry,
        stop_core=stop_core,
        telemetry_metrics=telemetry_metrics,
        tracer=tracer,
    )


def test_runtime_state_and_component_contracts_are_stable() -> None:
    assert [state.value for state in RuntimeState] == [
        "disabled",
        "starting",
        "active",
        "degraded",
        "stopped",
    ]
    status = ComponentStatus(active=True)
    with pytest.raises(FrozenInstanceError):
        status.active = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_disabled_start_has_no_provider_or_extension_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch, enabled=False)
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DISABLED
    assert runtime.is_unified_active() is False
    env.provider_builder.assert_not_called()
    assert env.extension_manager.claim_calls == []
    assert env.globals.tracer_sets == env.globals.meter_sets == 0


@pytest.mark.asyncio
async def test_unified_agent_runtime_warns_that_legacy_provider_config_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    warning = Mock()
    monkeypatch.setattr(runtime_module.logger, "warning", warning)
    monkeypatch.setattr(
        runtime_module,
        "get_config",
        lambda: {
            "agent_observability": {"enabled": True, "endpoint": "agent"},
            "team_observability": {"enabled": True, "endpoint": "team"},
        },
    )
    runtime = TelemetryRuntime()

    await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    message, conflicts = warning.call_args.args
    assert conflicts == "agent_observability, team_observability"
    assert "ignored" in message
    assert "restart" in message


@pytest.mark.asyncio
async def test_gateway_start_is_idempotent_and_does_not_initialize_agentcore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()

    first, second = await asyncio.gather(
        runtime.start(
            process_role="gateway",
            registry=env.registry,
            extension_manager=env.extension_manager,
        ),
        runtime.start(
            process_role="gateway",
            registry=env.registry,
            extension_manager=env.extension_manager,
        ),
    )

    assert first is second is RuntimeState.ACTIVE
    env.provider_builder.assert_called_once()
    env.registry_builder.assert_called_once()
    env.metrics_builder.assert_called_once_with(env.meter)
    env.init_core.assert_not_called()
    env.callbacks_builder.assert_not_called()


@pytest.mark.asyncio
async def test_agentserver_injects_provider_and_registers_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    trajectory_processor = Mock(name="trajectory_span_processor")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.observability_runtime.get_trajectory_span_processor",
        Mock(return_value=trajectory_processor),
    )
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.ACTIVE
    assert env.tracer.processors == [env.span_registry]
    _, kwargs = env.init_core.call_args
    assert kwargs == {
        "tracer_provider_override": env.tracer,
        "owns_provider": False,
        "additional_span_processors": (trajectory_processor,),
    }
    assert env.callbacks.register_calls == 1
    assert runtime.component_status()["agentcore"].active is True


@pytest.mark.asyncio
async def test_faas_role_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()

    with pytest.raises(ValueError, match="unsupported telemetry process role: faas"):
        await runtime.start(
            process_role="faas",  # type: ignore[arg-type]
            registry=env.registry,
            extension_manager=env.extension_manager,
        )


@pytest.mark.asyncio
async def test_agent_runtime_owns_session_observer_and_stuck_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()
    session_telemetry = Mock()
    checker_stopped = asyncio.Event()

    async def run_checker(stop_event: asyncio.Event) -> None:
        await stop_event.wait()
        checker_stopped.set()

    session_telemetry.run_stuck_checker = AsyncMock(side_effect=run_checker)
    runtime._session_telemetry = session_telemetry

    await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    session_telemetry.configure.assert_called_once_with(
        metrics=env.telemetry_metrics,
        stuck_threshold_ms=300000.0,
        stuck_check_interval_s=30.0,
    )
    await asyncio.sleep(0)
    session_telemetry.run_stuck_checker.assert_awaited_once()

    await runtime.stop()

    assert checker_stopped.is_set()
    session_telemetry.deactivate.assert_called_once_with(env.telemetry_metrics)


@pytest.mark.asyncio
async def test_invalid_extension_is_degraded_without_claim_or_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = _Extension([])
    env = _install_runtime_fakes(monkeypatch, extension=extension, claim_extension=True)
    env.provider_builder.side_effect = TypeError("invalid ProviderBundle")
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert "invalid ProviderBundle" in runtime.component_status()["extension"].error
    assert env.extension_manager.claim_calls == []
    assert env.globals.tracer_sets == env.globals.meter_sets == 0


@pytest.mark.asyncio
async def test_tracer_conflict_keeps_metrics_active_without_overwriting_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    existing_tracer = _Provider("existing", env.events)
    env.globals.tracer = existing_tracer
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert env.globals.tracer is existing_tracer
    assert env.globals.tracer_sets == 0
    assert env.globals.meter is env.meter
    assert runtime.component_status()["traces"].active is False
    assert runtime.component_status()["metrics"].active is True
    env.init_core.assert_not_called()


@pytest.mark.asyncio
async def test_ordering_failure_unregisters_callbacks_and_reports_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch, ordering=False)
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert env.callbacks.register_calls == 1
    assert env.callbacks.unregister_calls == 1
    assert runtime.component_status()["callbacks"].active is False
    assert "ordering" in runtime.component_status()["callbacks"].error.lower()


@pytest.mark.asyncio
async def test_stop_reverses_lifecycle_and_honors_provider_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = _Extension([])
    env = _install_runtime_fakes(
        monkeypatch,
        extension=extension,
        claim_extension=True,
    )
    extension.events = env.events
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    await runtime.stop()
    await runtime.stop()

    assert env.callbacks.unregister_calls == 1
    env.stop_core.assert_called_once_with()
    assert env.tracer.flush_calls == env.meter.flush_calls == 1
    assert env.tracer.shutdown_calls == env.meter.shutdown_calls == 1
    assert extension.shutdown_calls == 1
    assert env.extension_manager.claim_calls == [extension]
    assert runtime.component_status()["runtime"] == ComponentStatus(active=False)
    assert env.events.index("callbacks.unregister") < env.events.index("core.shutdown")
    assert env.events.index("core.shutdown") < env.events.index("tracer.flush")
    assert env.events.index("meter.shutdown") < env.events.index("extension.shutdown")


@pytest.mark.asyncio
async def test_external_providers_are_flushed_but_never_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(
        monkeypatch,
        owns_tracer=False,
        owns_meter=False,
    )
    env.globals.tracer = env.tracer
    env.globals.meter = env.meter
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    await runtime.stop()

    assert env.tracer.flush_calls == env.meter.flush_calls == 1
    assert env.tracer.shutdown_calls == env.meter.shutdown_calls == 0


@pytest.mark.asyncio
async def test_late_extension_does_not_switch_an_active_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )
    extension = _Extension(env.events)
    env.registry.extension = extension

    state = await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.ACTIVE
    env.provider_builder.assert_called_once()
    assert env.extension_manager.claim_calls == []


@pytest.mark.asyncio
async def test_cancelled_start_rolls_back_and_can_be_stopped_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    env.callbacks.register_gate = asyncio.Event()
    runtime = TelemetryRuntime()
    task = asyncio.create_task(
        runtime.start(
            process_role="agentserver",
            registry=env.registry,
            extension_manager=env.extension_manager,
        )
    )
    while env.callbacks.register_calls == 0:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.stop()

    assert env.callbacks.unregister_calls == 1
    env.stop_core.assert_called_once_with()
    assert env.tracer.shutdown_calls == env.meter.shutdown_calls == 1
    assert runtime.is_unified_active() is False


@pytest.mark.asyncio
async def test_component_status_is_a_read_only_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    statuses = runtime.component_status()
    with pytest.raises(TypeError):
        statuses["runtime"] = ComponentStatus(active=False)  # type: ignore[index]
    assert runtime.component_status()["runtime"].active is True


def test_runtime_contract_is_exported_from_telemetry_package() -> None:
    from jiuwenswarm.telemetry import (  # noqa: PLC0415
        ComponentStatus as ExportedComponentStatus,
    )
    from jiuwenswarm.telemetry import RuntimeState as ExportedRuntimeState  # noqa: PLC0415
    from jiuwenswarm.telemetry import TelemetryRuntime as ExportedRuntime  # noqa: PLC0415
    from jiuwenswarm.telemetry import get_telemetry_runtime  # noqa: PLC0415

    assert ExportedComponentStatus is ComponentStatus
    assert ExportedRuntimeState is RuntimeState
    assert ExportedRuntime is TelemetryRuntime
    assert get_telemetry_runtime() is get_telemetry_runtime()


@pytest.mark.asyncio
async def test_runtime_exposes_shared_components_for_boundary_instrumentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    runtime = TelemetryRuntime()

    assert runtime.tracer_provider is None
    assert runtime.meter_provider is None
    assert runtime.span_registry is None
    assert runtime.telemetry_metrics is None
    bindings = runtime.trace_bindings

    await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert runtime.tracer_provider is env.tracer
    assert runtime.meter_provider is env.meter
    assert runtime.span_registry is env.span_registry
    assert runtime.telemetry_metrics is env.telemetry_metrics
    assert runtime.trace_bindings is not bindings

    await runtime.stop()
    assert runtime.tracer_provider is None
    assert runtime.meter_provider is None
    assert runtime.span_registry is None
    assert runtime.telemetry_metrics is None


@pytest.mark.asyncio
async def test_global_setter_must_install_the_exact_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    monkeypatch.setattr(runtime_module.trace, "set_tracer_provider", Mock())
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert runtime.component_status()["traces"].active is False
    assert "install" in runtime.component_status()["traces"].error.lower()
    assert env.tracer.processors == []
    assert runtime.component_status()["metrics"].active is True


@pytest.mark.asyncio
async def test_preinitialized_agentcore_is_not_claimed_or_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    monkeypatch.setattr(runtime_module, "is_initialized", Mock(return_value=True))
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert "already initialized" in runtime.component_status()["agentcore"].error
    env.init_core.assert_not_called()
    await runtime.stop()
    env.stop_core.assert_not_called()


@pytest.mark.asyncio
async def test_callback_construction_failure_is_component_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    env.callbacks_builder.side_effect = RuntimeError("callback construction failed")
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert (
        "callback construction failed" in runtime.component_status()["callbacks"].error
    )
    assert runtime.component_status()["agentcore"].active is True
    await runtime.stop()
    env.stop_core.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("temporary unregister failure"), KeyboardInterrupt()],
)
async def test_failed_callback_unregister_is_retried_without_reclosing_resources(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    env.callbacks.unregister_errors.append(failure)
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    if isinstance(failure, Exception):
        await runtime.stop()
    else:
        with pytest.raises(type(failure)):
            await runtime.stop()
    await runtime.stop()

    assert env.callbacks.unregister_calls == 2
    env.stop_core.assert_called_once_with()
    assert env.tracer.shutdown_calls == env.meter.shutdown_calls == 1


@pytest.mark.asyncio
async def test_failed_claimed_extension_shutdown_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = _Extension([])
    extension.shutdown_errors.append(RuntimeError("temporary extension failure"))
    env = _install_runtime_fakes(
        monkeypatch,
        extension=extension,
        claim_extension=True,
    )
    runtime = TelemetryRuntime()
    await runtime.start(
        process_role="gateway",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    await runtime.stop()
    await runtime.stop()

    assert extension.shutdown_calls == 2
    assert env.tracer.shutdown_calls == env.meter.shutdown_calls == 1


@pytest.mark.asyncio
async def test_agentcore_init_failure_leaves_metric_component_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_runtime_fakes(monkeypatch)
    env.init_core.side_effect = RuntimeError("core init failed")
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="agentserver",
        registry=env.registry,
        extension_manager=env.extension_manager,
    )

    assert state is RuntimeState.DEGRADED
    assert runtime.component_status()["metrics"].active is True
    assert runtime.component_status()["agentcore"] == ComponentStatus(
        active=False,
        error="core init failed",
    )
    env.callbacks_builder.assert_not_called()
    await runtime.stop()
    env.stop_core.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_runtime_installs_registry_on_real_sdk_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    logger_provider = _Provider("logger", [])
    bundle = ProviderBundle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,  # type: ignore[arg-type]
    )
    globals_state = _Globals()
    monkeypatch.setattr(
        runtime_module,
        "load_telemetry_config",
        Mock(return_value=TelemetryConfig(enabled=True)),
    )
    monkeypatch.setattr(
        runtime_module,
        "build_provider_bundle",
        Mock(return_value=bundle),
    )
    monkeypatch.setattr(
        runtime_module.trace,
        "get_tracer_provider",
        lambda: globals_state.tracer,
    )
    monkeypatch.setattr(
        runtime_module.trace,
        "set_tracer_provider",
        globals_state.set_tracer,
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "get_meter_provider",
        lambda: globals_state.meter,
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "set_meter_provider",
        globals_state.set_meter,
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "get_logger_provider",
        lambda: globals_state.logger,
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "set_logger_provider",
        globals_state.set_logger,
    )
    runtime = TelemetryRuntime()

    state = await runtime.start(
        process_role="gateway",
        registry=_Registry(),
        extension_manager=_ExtensionManager(),
    )

    processors = tracer_provider._active_span_processor._span_processors
    assert state is RuntimeState.ACTIVE
    assert any(
        isinstance(processor, runtime_module.SpanRegistryProcessor)
        for processor in processors
    )
    assert runtime.component_status()["metrics"].active is True

    await runtime.stop()
    assert runtime.is_unified_active() is False
