"""Unified ownership and lifecycle for JiuwenSwarm telemetry components."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from opentelemetry import _logs, metrics, trace

from openjiuwen.agent_teams.observability import (
    ObservabilityConfig,
    init_observability,
    is_initialized,
    shutdown_observability,
)

from jiuwenswarm.common.config import get_config
from jiuwenswarm.telemetry.config import TelemetryConfig, load_telemetry_config
from jiuwenswarm.telemetry.enrichment.callbacks import RichTelemetryCallbacks
from jiuwenswarm.telemetry.metrics import TelemetryMetrics
from jiuwenswarm.telemetry.provider import ProviderBundle, build_provider_bundle
from jiuwenswarm.telemetry.request_context import TraceBindingRegistry
from jiuwenswarm.telemetry.session import SessionTelemetry, get_session_telemetry
from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor


ProcessRole = Literal["gateway", "agentserver"]
_LOGGER = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    """Preserve lookup of the former module logger without shadowing parameters."""
    if name == "logger":
        return _LOGGER
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class RuntimeState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ComponentStatus:
    active: bool
    error: str | None = None


_COMPONENTS = (
    "config",
    "extension",
    "traces",
    "metrics",
    "logs",
    "registry",
    "agentcore",
    "callbacks",
    "runtime",
)


class TelemetryRuntime:
    """Start and stop one process-wide telemetry graph exactly once."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = RuntimeState.STOPPED
        self._statuses = self._empty_statuses()
        self._bundle: ProviderBundle | None = None
        self._tracer_provider: Any | None = None
        self._meter_provider: Any | None = None
        self._logger_provider: Any | None = None
        self._span_registry: SpanRegistryProcessor | None = None
        self._telemetry_metrics: TelemetryMetrics | None = None
        self._trace_bindings = self._new_trace_bindings()
        self._callbacks: RichTelemetryCallbacks | None = None
        self._callbacks_registered = False
        self._callback_framework: Any | None = None
        self._agentcore_initialized = False
        self._claimed_extension: Any | None = None
        self._extension_shutdown = False
        self._session_telemetry: SessionTelemetry = get_session_telemetry()
        self._session_telemetry_active = False
        self._session_stuck_stop_event: asyncio.Event | None = None
        self._session_stuck_checker: asyncio.Task[None] | None = None

    async def start(
        self,
        *,
        process_role: ProcessRole,
        registry: Any,
        extension_manager: Any,
    ) -> RuntimeState:
        if process_role not in {"gateway", "agentserver"}:
            raise ValueError(f"unsupported telemetry process role: {process_role}")

        async with self._lock:
            if self._state in {
                RuntimeState.ACTIVE,
                RuntimeState.DEGRADED,
                RuntimeState.DISABLED,
            }:
                return self._state

            self._reset_for_start()
            self._state = RuntimeState.STARTING
            try:
                config = self._load_config()
                if config is None:
                    return self._finish_start(RuntimeState.DEGRADED)
                if not config.enabled:
                    return self._finish_start(RuntimeState.DISABLED)
                if process_role == "agentserver":
                    self._warn_legacy_provider_config()

                extension = self._get_extension(registry)
                bundle = self._build_bundle(config, registry, extension)
                if bundle is None:
                    return self._finish_start(RuntimeState.DEGRADED)
                self._bundle = bundle
                self._claim_extension(extension, extension_manager)

                tracer_provider = self._install_traces(bundle)
                meter_provider = self._install_metrics(bundle)
                logger_provider = self._install_logs(bundle)
                telemetry_metrics = self._create_metrics(meter_provider)

                if process_role == "agentserver":
                    self._start_session_telemetry(config, telemetry_metrics)

                if process_role == "agentserver":
                    await self._start_agent_components(
                        config,
                        registry,
                        tracer_provider,
                        telemetry_metrics,
                    )

                state = (
                    RuntimeState.DEGRADED
                    if any(status.error for status in self._statuses.values())
                    else RuntimeState.ACTIVE
                )
                return self._finish_start(state)
            except BaseException as error:
                if isinstance(error, Exception):
                    self._set_status("runtime", False, error)
                    return self._finish_start(RuntimeState.DEGRADED)
                await self._stop_locked()
                raise

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    def is_unified_active(self) -> bool:
        return self._state is RuntimeState.ACTIVE

    def component_status(self) -> Mapping[str, ComponentStatus]:
        return MappingProxyType(dict(self._statuses))

    @property
    def tracer_provider(self) -> Any | None:
        return self._tracer_provider

    @property
    def meter_provider(self) -> Any | None:
        return self._meter_provider

    @property
    def logger_provider(self) -> Any | None:
        return self._logger_provider

    @property
    def span_registry(self) -> SpanRegistryProcessor | None:
        return self._span_registry

    @property
    def telemetry_metrics(self) -> TelemetryMetrics | None:
        return self._telemetry_metrics

    @property
    def trace_bindings(self) -> TraceBindingRegistry:
        return self._trace_bindings

    def _load_config(self) -> TelemetryConfig | None:
        try:
            config = load_telemetry_config()
        except Exception as error:
            self._set_status("config", False, error)
            return None
        self._set_status("config", True)
        return config

    @staticmethod
    def _get_extension(registry: Any) -> Any | None:
        if registry is None:
            return None
        getter = getattr(registry, "get_telemetry_provider_extension", None)
        return getter() if callable(getter) else None

    def _build_bundle(
        self,
        config: TelemetryConfig,
        registry: Any,
        extension: Any | None,
    ) -> ProviderBundle | None:
        try:
            bundle = build_provider_bundle(config, registry=registry)
        except Exception as error:
            self._set_status(
                "extension" if extension is not None else "runtime", False, error
            )
            return None
        self._set_status("extension", extension is not None)
        return bundle

    def _claim_extension(self, extension: Any | None, extension_manager: Any) -> None:
        if extension is None or extension_manager is None:
            return
        claim = getattr(extension_manager, "claim_loaded_extension", None)
        if not callable(claim):
            return
        try:
            claimed = bool(claim(extension))
        except Exception as error:
            self._set_status("extension", False, error)
            return
        if claimed:
            self._claimed_extension = extension

    def _install_traces(self, bundle: ProviderBundle) -> Any | None:
        provider = bundle.tracer_provider
        if provider is None:
            self._set_status("traces", False, "tracer provider unavailable")
            return None
        error = self._install_global_provider(
            current=trace.get_tracer_provider(),
            candidate=provider,
            setter=trace.set_tracer_provider,
            getter=trace.get_tracer_provider,
            proxy_names={"ProxyTracerProvider"},
            signal="tracer",
        )
        if error is not None:
            self._set_status("traces", False, error)
            return None
        try:
            span_registry = SpanRegistryProcessor(max_spans=4096, ttl_seconds=900)
            provider.add_span_processor(span_registry)
        except Exception as install_error:
            self._set_status("registry", False, install_error)
            self._set_status("traces", False, install_error)
            return None
        self._set_status("registry", True)
        self._set_status("traces", True)
        self._tracer_provider = provider
        self._span_registry = span_registry
        return provider

    def _install_metrics(self, bundle: ProviderBundle) -> Any | None:
        provider = bundle.meter_provider
        if provider is None:
            self._set_status("metrics", False, "meter provider unavailable")
            return None
        error = self._install_global_provider(
            current=metrics.get_meter_provider(),
            candidate=provider,
            setter=metrics.set_meter_provider,
            getter=metrics.get_meter_provider,
            proxy_names={"_ProxyMeterProvider", "ProxyMeterProvider"},
            signal="meter",
        )
        if error is not None:
            self._set_status("metrics", False, error)
            return None
        self._set_status("metrics", True)
        self._meter_provider = provider
        return provider

    def _create_metrics(self, meter_provider: Any | None) -> TelemetryMetrics | None:
        if meter_provider is None:
            return None
        try:
            telemetry_metrics = TelemetryMetrics(meter_provider)
        except Exception as error:
            self._set_status("metrics", False, error)
            return None
        self._telemetry_metrics = telemetry_metrics
        return telemetry_metrics


    def _install_logs(self, bundle: ProviderBundle) -> Any | None:
        provider = bundle.logger_provider
        if provider is None:
            self._set_status("logs", False, "logger provider unavailable")
            return None
        error = self._install_global_provider(
            current=_logs.get_logger_provider(),
            candidate=provider,
            setter=_logs.set_logger_provider,
            getter=_logs.get_logger_provider,
            proxy_names={"ProxyLoggerProvider"},
            signal="logger",
        )
        if error is not None:
            self._set_status("logs", False, error)
            return None
        self._set_status("logs", True)
        self._logger_provider = provider
        return provider

    async def _start_agent_components(
        self,
        config: TelemetryConfig,
        registry: Any,
        tracer_provider: Any | None,
        telemetry_metrics: TelemetryMetrics | None,
    ) -> None:
        if tracer_provider is None:
            self._set_status("agentcore", False, "tracer provider unavailable")
            return
        if is_initialized():
            self._set_status(
                "agentcore",
                False,
                "AgentCore observability is already initialized outside this runtime",
            )
            return
        try:
            core_config = ObservabilityConfig(
                enabled=True,
                service_name=config.service_name,
                exporter="console",
                sample_rate=config.sample_rate,
                redact_prompts=config.redact_prompts,
                redact_completions=config.redact_completions,
                attribute_value_max_length=config.attribute_value_max_length,
                max_attributes=config.max_attributes,
                backend="otlp",
            )
            # Skill / team evolution rails subscribe to this processor; without
            # registering it on the unified TracerProvider, after_invoke never
            # receives LLM/tool spans and run_evolution is skipped silently.
            from jiuwenswarm.agents.harness.observability_runtime import (
                get_trajectory_span_processor,
            )

            trajectory_processor = get_trajectory_span_processor()
            extra_processors = (
                (trajectory_processor,) if trajectory_processor is not None else ()
            )
            init_observability(
                core_config,
                tracer_provider_override=tracer_provider,
                owns_provider=False,
                additional_span_processors=extra_processors,
            )
        except Exception as error:
            self._set_status("agentcore", False, error)
            return
        self._agentcore_initialized = True
        self._set_status("agentcore", True)

        if telemetry_metrics is None:
            self._set_status("callbacks", False, "meter provider unavailable")
            return
        framework = getattr(registry, "callback_framework", None)
        if framework is None:
            self._set_status("callbacks", False, "callback framework unavailable")
            return
        span_registry = self._span_registry
        if span_registry is None:
            self._set_status("callbacks", False, "span registry unavailable")
            return
        try:
            callbacks = RichTelemetryCallbacks(
                span_registry=span_registry,
                metrics=telemetry_metrics,
                config=config,
            )
        except Exception as error:
            self._set_status("callbacks", False, error)
            return
        self._callbacks = callbacks
        self._callback_framework = framework
        self._callbacks_registered = True
        try:
            await callbacks.register(framework)
            if not callbacks.validate_core_ordering(framework):
                raise RuntimeError("AgentCore callback ordering validation failed")
        except BaseException as error:
            if isinstance(error, Exception):
                try:
                    await callbacks.unregister(framework)
                except BaseException as cleanup_error:
                    self._set_status("callbacks", False, error)
                    if not isinstance(cleanup_error, Exception):
                        raise cleanup_error
                    return
                self._callbacks_registered = False
                self._callbacks = None
                self._callback_framework = None
                self._set_status("callbacks", False, error)
                return
            raise
        self._set_status("callbacks", True)


    async def _stop_locked(self) -> None:
        first_control_error: BaseException | None = None
        cleanup_errors: list[tuple[str, BaseException]] = []
        checker = self._session_stuck_checker
        stop_event = self._session_stuck_stop_event
        if stop_event is not None:
            stop_event.set()
        if checker is not None:
            try:
                await checker
            except BaseException as error:
                cleanup_errors.append(("metrics", error))
                if not isinstance(error, Exception):
                    first_control_error = error
            finally:
                self._session_stuck_checker = None
                self._session_stuck_stop_event = None
        if self._session_telemetry_active:
            try:
                self._session_telemetry.deactivate(self._telemetry_metrics)
            except BaseException as error:
                cleanup_errors.append(("metrics", error))
                if not isinstance(error, Exception) and first_control_error is None:
                    first_control_error = error
            finally:
                self._session_telemetry_active = False

        if self._callbacks is not None and self._callbacks_registered:
            try:
                await self._callbacks.unregister(self._callback_framework)
            except BaseException as error:
                cleanup_errors.append(("callbacks", error))
                if not isinstance(error, Exception) and first_control_error is None:
                    first_control_error = error
            else:
                self._callbacks_registered = False
                self._callbacks = None
                self._callback_framework = None

        if self._agentcore_initialized:
            try:
                shutdown_observability()
            except BaseException as error:
                cleanup_errors.append(("agentcore", error))
                if not isinstance(error, Exception) and first_control_error is None:
                    first_control_error = error
            else:
                self._agentcore_initialized = False

        bundle = self._bundle
        if bundle is not None:
            for provider in (bundle.tracer_provider, bundle.meter_provider, bundle.logger_provider):
                if provider is None:
                    continue
                try:
                    provider.force_flush()
                except BaseException as error:
                    cleanup_errors.append(("runtime", error))
                    if not isinstance(error, Exception) and first_control_error is None:
                        first_control_error = error
            for provider, owned in (
                (bundle.tracer_provider, bundle.owns_tracer),
                (bundle.meter_provider, bundle.owns_meter),
                (bundle.logger_provider, bundle.owns_logger),
            ):
                if provider is None or not owned:
                    continue
                try:
                    provider.shutdown()
                except BaseException as error:
                    cleanup_errors.append(("runtime", error))
                    if not isinstance(error, Exception) and first_control_error is None:
                        first_control_error = error

        if self._claimed_extension is not None and not self._extension_shutdown:
            try:
                await self._claimed_extension.shutdown()
            except BaseException as error:
                cleanup_errors.append(("extension", error))
                if not isinstance(error, Exception) and first_control_error is None:
                    first_control_error = error
            else:
                self._extension_shutdown = True
                self._claimed_extension = None

        self._bundle = None
        self._tracer_provider = None
        self._meter_provider = None
        self._logger_provider = None
        self._span_registry = None
        self._telemetry_metrics = None
        self._trace_bindings = self._new_trace_bindings()
        self._statuses = self._empty_statuses()
        for component, cleanup_error in cleanup_errors:
            self._set_status(component, False, cleanup_error)
        has_pending_cleanup = (
            self._callbacks_registered
            or self._agentcore_initialized
            or self._claimed_extension is not None
        )
        self._state = (
            RuntimeState.DEGRADED if has_pending_cleanup else RuntimeState.STOPPED
        )
        if cleanup_errors:
            self._set_status(
                "runtime",
                False,
                "telemetry cleanup incomplete"
                if has_pending_cleanup
                else "telemetry cleanup completed with errors",
            )
        if first_control_error is not None:
            raise first_control_error

    @staticmethod
    def _install_global_provider(
        *,
        current: Any,
        candidate: Any,
        setter: Any,
        getter: Any,
        proxy_names: set[str],
        signal: str,
    ) -> str | None:
        if current is candidate:
            return None
        if type(current).__name__ not in proxy_names:
            return f"different global {signal} provider is already installed"
        try:
            setter(candidate)
        except Exception as error:
            return str(error)
        if getter() is not candidate:
            return f"failed to install the requested global {signal} provider"
        return None

    def _set_status(
        self,
        component: str,
        active: bool,
        error: BaseException | str | None = None,
    ) -> None:
        error_text = None if error is None else str(error)
        self._statuses[component] = ComponentStatus(active=active, error=error_text)

    def _finish_start(self, state: RuntimeState) -> RuntimeState:
        self._state = state
        if state is RuntimeState.ACTIVE:
            self._set_status("runtime", True)
        elif self._statuses["runtime"].error is None:
            self._set_status("runtime", False)
        return state

    def _reset_for_start(self) -> None:
        self._statuses = self._empty_statuses()
        self._bundle = None
        self._tracer_provider = None
        self._meter_provider = None
        self._logger_provider = None
        self._span_registry = None
        self._telemetry_metrics = None
        self._trace_bindings = self._new_trace_bindings()
        self._callbacks = None
        self._callbacks_registered = False
        self._callback_framework = None
        self._agentcore_initialized = False
        self._claimed_extension = None
        self._extension_shutdown = False
        self._session_telemetry_active = False
        self._session_stuck_stop_event = None
        self._session_stuck_checker = None

    @staticmethod
    def _empty_statuses() -> dict[str, ComponentStatus]:
        return {name: ComponentStatus(active=False) for name in _COMPONENTS}

    @staticmethod
    def _new_trace_bindings() -> TraceBindingRegistry:
        return TraceBindingRegistry(max_bindings=4096, ttl_seconds=900)

    @staticmethod
    def _warn_legacy_provider_config() -> None:
        try:
            raw_config = get_config()
        except Exception:
            raw_config = None
        if not isinstance(raw_config, dict):
            return
        conflicts = []
        for name in ("agent_observability", "team_observability"):
            config = raw_config.get(name)
            if isinstance(config, dict) and bool(config.get("enabled", False)):
                conflicts.append(name)
        if conflicts:
            _LOGGER.warning(
                "telemetry.enabled=true: legacy provider settings for %s are ignored; "
                "restart the process after provider configuration changes",
                ", ".join(conflicts),
            )

    def _start_session_telemetry(
        self,
        config: TelemetryConfig,
        telemetry_metrics: TelemetryMetrics | None,
    ) -> None:
        if telemetry_metrics is None:
            return
        try:
            self._session_telemetry.configure(
                metrics=telemetry_metrics,
                stuck_threshold_ms=config.session_stuck_threshold_ms,
                stuck_check_interval_s=config.session_stuck_check_interval_s,
            )
            self._session_telemetry_active = True
            stop_event = asyncio.Event()
            self._session_stuck_stop_event = stop_event
            self._session_stuck_checker = asyncio.create_task(
                self._session_telemetry.run_stuck_checker(stop_event)
            )
        except Exception as error:
            self._set_status("metrics", False, error)


_TELEMETRY_RUNTIME = TelemetryRuntime()


def get_telemetry_runtime() -> TelemetryRuntime:
    return _TELEMETRY_RUNTIME


async def stop_process_telemetry(
    *,
    runtime: Any | None,
    extension_manager: Any | None,
    logger: Any,
    process_name: str,
) -> None:
    """Stop telemetry before the remaining extensions.

    Ordinary cleanup failures are degraded to warnings. Control exceptions are
    allowed to propagate, while the ``finally`` still gives remaining extensions
    their shutdown opportunity.
    """
    try:
        if runtime is not None:
            try:
                await runtime.stop()
            except Exception as error:
                logger.warning(
                    "[%s] telemetry runtime shutdown failed: %s",
                    process_name,
                    error,
                )
    finally:
        if extension_manager is not None:
            try:
                await extension_manager.shutdown_all_extensions()
            except Exception as error:
                logger.warning(
                    "[%s] remaining extension shutdown failed: %s",
                    process_name,
                    error,
                )


class ProcessTelemetryLifecycle:
    """Own process-entry cleanup from extension load through steady-state exit."""

    def __init__(self, *, logger: Any, process_name: str) -> None:
        self._logger = logger
        self._process_name = process_name
        self._runtime: Any | None = None
        self._extension_manager: Any | None = None

    def bind_extension_manager(self, extension_manager: Any) -> None:
        if (
            self._extension_manager is not None
            and self._extension_manager is not extension_manager
        ):
            raise RuntimeError(
                "process telemetry lifecycle already owns an extension manager"
            )
        self._extension_manager = extension_manager

    async def start(
        self,
        *,
        process_role: ProcessRole,
        registry: Any,
        extension_manager: Any,
    ) -> TelemetryRuntime:
        self.bind_extension_manager(extension_manager)
        self._runtime = get_telemetry_runtime()
        await self._runtime.start(
            process_role=process_role,
            registry=registry,
            extension_manager=extension_manager,
        )
        return self._runtime

    async def stop(self) -> None:
        runtime = self._runtime
        extension_manager = self._extension_manager
        if runtime is None and extension_manager is None:
            return
        try:
            await stop_process_telemetry(
                runtime=runtime,
                extension_manager=extension_manager,
                logger=self._logger,
                process_name=self._process_name,
            )
        finally:
            self._runtime = None
            self._extension_manager = None


__all__ = [
    "ComponentStatus",
    "ProcessTelemetryLifecycle",
    "RuntimeState",
    "TelemetryRuntime",
    "get_telemetry_runtime",
    "stop_process_telemetry",
]
