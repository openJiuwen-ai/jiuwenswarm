"""Build OpenTelemetry providers without installing them globally."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from jiuwenswarm.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenswarm.telemetry.config import TelemetryConfig

_CUMULATIVE_TEMPORALITY = {
    Counter: AggregationTemporality.CUMULATIVE,
    UpDownCounter: AggregationTemporality.CUMULATIVE,
    Histogram: AggregationTemporality.CUMULATIVE,
    ObservableCounter: AggregationTemporality.CUMULATIVE,
    ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableGauge: AggregationTemporality.CUMULATIVE,
}


@dataclass
class ProviderBundle:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    resource: Resource | None = None
    owns_tracer: bool = True
    owns_meter: bool = True
    owns_logger: bool = True


def _package_version() -> str:
    try:
        return version("jiuwenswarm")
    except PackageNotFoundError:
        return "0.2.3"


def _provider_resource(provider: Any) -> Resource | None:
    resource = getattr(provider, "resource", None)
    if resource is not None:
        return resource
    sdk_config = getattr(provider, "_sdk_config", None)
    return getattr(sdk_config, "resource", None)


def _shutdown_quietly(resource: Any) -> None:
    """Best-effort rollback that never replaces the construction failure."""
    try:
        resource.shutdown()
    except BaseException:
        pass


def coerce_provider_bundle(value: Any) -> ProviderBundle:
    """Validate and normalize an extension's provider bundle."""
    has_tracer = hasattr(value, "tracer_provider")
    has_meter = hasattr(value, "meter_provider")
    if not has_tracer and not has_meter:
        raise TypeError(
            "Telemetry provider extension must return a ProviderBundle-like object"
        )

    tracer_provider = getattr(value, "tracer_provider", None)
    meter_provider = getattr(value, "meter_provider", None)
    if tracer_provider is not None and not isinstance(tracer_provider, TracerProvider):
        raise TypeError(
            "tracer_provider must be a TracerProvider instance or None, "
            f"got {type(tracer_provider).__name__}"
        )
    if meter_provider is not None and not isinstance(meter_provider, MeterProvider):
        raise TypeError(
            "meter_provider must be a MeterProvider instance or None, "
            f"got {type(meter_provider).__name__}"
        )
    if tracer_provider is None and meter_provider is None:
        raise TypeError("ProviderBundle must contain at least one provider")

    explicit_resource = getattr(value, "resource", None)
    tracer_resource = (
        _provider_resource(tracer_provider) if tracer_provider is not None else None
    )
    meter_resource = (
        _provider_resource(meter_provider) if meter_provider is not None else None
    )
    resources = [
        resource
        for resource in (explicit_resource, tracer_resource, meter_resource)
        if resource is not None
    ]
    for resource in resources:
        if not isinstance(resource, Resource):
            raise TypeError(
                "resource must be a Resource instance or None, "
                f"got {type(resource).__name__}"
            )
    if resources and any(resource != resources[0] for resource in resources[1:]):
        raise TypeError("ProviderBundle contains conflicting Resource values")
    resource = resources[0] if resources else None

    owns_tracer = getattr(value, "owns_tracer", True)
    owns_meter = getattr(value, "owns_meter", True)
    if not isinstance(owns_tracer, bool):
        raise TypeError(
            f"owns_tracer must be a bool, got {type(owns_tracer).__name__}"
        )
    if not isinstance(owns_meter, bool):
        raise TypeError(f"owns_meter must be a bool, got {type(owns_meter).__name__}")
    logger_provider = getattr(value, "logger_provider", None)
    if logger_provider is not None and not isinstance(logger_provider, LoggerProvider):
        raise TypeError(
            "logger_provider must be a LoggerProvider instance or None, "
            f"got {type(logger_provider).__name__}"
        )
    owns_logger = getattr(value, "owns_logger", True)
    if not isinstance(owns_logger, bool):
        raise TypeError(f"owns_logger must be a bool, got {type(owns_logger).__name__}")

    return ProviderBundle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        resource=resource,
        owns_tracer=owns_tracer,
        owns_meter=owns_meter,
        owns_logger=owns_logger,
    )


def build_provider_bundle(
    cfg: TelemetryConfig,
    *,
    registry: Any | None,
) -> ProviderBundle:
    extension = (
        registry.get_telemetry_provider_extension() if registry is not None else None
    )
    if extension is None:
        return build_default_providers(cfg)
    value = extension.build_providers(cfg)
    if value is None:
        return build_default_providers(cfg)
    return coerce_provider_bundle(value)


def build_default_providers(cfg: TelemetryConfig) -> ProviderBundle:
    """Build default providers; installation belongs to TelemetryRuntime."""
    if not cfg.enabled:
        return ProviderBundle(owns_tracer=False, owns_meter=False, owns_logger=False)

    attributes = {
        SERVICE_NAME: cfg.service_name,
        "service.version": _package_version(),
    }
    if cfg.claw_id is not None:
        attributes[JIUWENCLAW_CLAW_ID] = cfg.claw_id
    resource = Resource.create(attributes)

    tracer_provider = _build_tracer_provider(cfg, resource)
    try:
        meter_provider = _build_meter_provider(cfg, resource)
    except BaseException:
        _shutdown_quietly(tracer_provider)
        raise
    try:
        logger_provider = _build_logger_provider(cfg, resource)
    except BaseException:
        _shutdown_quietly(tracer_provider)
        _shutdown_quietly(meter_provider)
        raise
    return ProviderBundle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        resource=resource,
    )


def _build_tracer_provider(
    cfg: TelemetryConfig, resource: Resource
) -> TracerProvider:
    provider = TracerProvider(resource=resource)
    try:
        if cfg.traces_exporter == "none":
            return provider
        if cfg.traces_exporter == "console":
            _add_span_exporter(
                provider,
                ConsoleSpanExporter(),
                SimpleSpanProcessor,
            )
            return provider
        if cfg.traces_exporter == "otlp":
            _add_span_exporter(
                provider,
                _create_otlp_span_exporter(cfg),
                lambda exporter: BatchSpanProcessor(
                    exporter, max_export_batch_size=256
                ),
            )
            return provider
        raise ValueError(f"Unsupported traces exporter: {cfg.traces_exporter}")
    except BaseException:
        _shutdown_quietly(provider)
        raise


def _add_span_exporter(provider: TracerProvider, exporter: Any, factory: Any) -> None:
    try:
        processor = factory(exporter)
    except BaseException:
        _shutdown_quietly(exporter)
        raise
    try:
        provider.add_span_processor(processor)
    except BaseException:
        _shutdown_quietly(processor)
        raise


def _build_meter_provider(cfg: TelemetryConfig, resource: Resource) -> MeterProvider:
    readers = []
    if cfg.metrics_exporter == "console":
        readers.append(
            _build_metric_reader(
                ConsoleMetricExporter(), export_interval_millis=3000
            )
        )
    elif cfg.metrics_exporter == "otlp":
        readers.append(
            _build_metric_reader(
                _create_otlp_metric_exporter(cfg), export_interval_millis=30000
            )
        )
    elif cfg.metrics_exporter != "none":
        raise ValueError(f"Unsupported metrics exporter: {cfg.metrics_exporter}")
    try:
        return MeterProvider(resource=resource, metric_readers=readers)
    except BaseException:
        for reader in readers:
            _shutdown_quietly(reader)
        raise


def _build_metric_reader(exporter: Any, *, export_interval_millis: int):
    try:
        return PeriodicExportingMetricReader(
            exporter, export_interval_millis=export_interval_millis
        )
    except BaseException:
        _shutdown_quietly(exporter)
        raise


def _build_logger_provider(cfg: TelemetryConfig, resource: Resource) -> LoggerProvider:
    provider = LoggerProvider(resource=resource)
    try:
        if cfg.logs_exporter == "none":
            return provider
        if cfg.logs_exporter == "console":
            from opentelemetry.sdk._logs.export import (
                ConsoleLogRecordExporter,
                SimpleLogRecordProcessor,
            )

            provider.add_log_record_processor(
                SimpleLogRecordProcessor(ConsoleLogRecordExporter())
            )
            return provider
        if cfg.logs_exporter == "otlp":
            provider.add_log_record_processor(
                BatchLogRecordProcessor(_create_otlp_log_exporter(cfg))
            )
            return provider
        raise ValueError(f"Unsupported logs exporter: {cfg.logs_exporter}")
    except BaseException:
        _shutdown_quietly(provider)
        raise


def _create_otlp_log_exporter(cfg: TelemetryConfig):
    if cfg.logs_protocol == "http":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        return OTLPLogExporter(
            endpoint=_signal_http_endpoint(cfg, "logs", "/v1/logs"),
            headers=cfg.logs_headers,
        )
    if cfg.logs_protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        return OTLPLogExporter(
            endpoint=cfg.logs_endpoint,
            headers=cfg.logs_headers,
        )
    raise ValueError(f"Unsupported logs protocol: {cfg.logs_protocol}")


def _otlp_http_endpoint(endpoint: str, path: str) -> str:
    if not endpoint:
        return path
    normalized = endpoint.rstrip("/")
    if normalized.endswith(path):
        return normalized
    return f"{normalized}{path}"


def _signal_http_endpoint(cfg: TelemetryConfig, signal: str, path: str) -> str:
    """Resolve HTTP endpoint without changing a distinct signal endpoint.

    Identical common and signal values coalesce to common-endpoint semantics,
    so the standard signal path is appended unless it is already present.
    """
    endpoint = getattr(cfg, f"{signal}_endpoint")
    if endpoint != cfg.endpoint:
        return endpoint
    return _otlp_http_endpoint(endpoint, path)


def _create_otlp_span_exporter(cfg: TelemetryConfig):
    if cfg.traces_protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(
            endpoint=_signal_http_endpoint(cfg, "traces", "/v1/traces"),
            headers=cfg.traces_headers,
        )
    if cfg.traces_protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(
            endpoint=cfg.traces_endpoint,
            headers=cfg.traces_headers,
        )
    raise ValueError(f"Unsupported traces protocol: {cfg.traces_protocol}")


def _create_otlp_metric_exporter(cfg: TelemetryConfig):
    if cfg.metrics_protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        return OTLPMetricExporter(
            endpoint=_signal_http_endpoint(cfg, "metrics", "/v1/metrics"),
            headers=cfg.metrics_headers,
            preferred_temporality=_CUMULATIVE_TEMPORALITY,
        )
    if cfg.metrics_protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        return OTLPMetricExporter(
            endpoint=cfg.metrics_endpoint,
            headers=cfg.metrics_headers,
            preferred_temporality=_CUMULATIVE_TEMPORALITY,
        )
    raise ValueError(f"Unsupported metrics protocol: {cfg.metrics_protocol}")
