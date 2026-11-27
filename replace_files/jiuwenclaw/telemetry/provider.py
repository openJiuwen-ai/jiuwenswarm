# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TracerProvider and MeterProvider initialization with OTLP/Console exporters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics._internal.instrument import (
    Counter,
    Histogram,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)

_CUMULATIVE_TEMPORALITY = {
    Counter: AggregationTemporality.CUMULATIVE,
    UpDownCounter: AggregationTemporality.CUMULATIVE,
    Histogram: AggregationTemporality.CUMULATIVE,
    ObservableCounter: AggregationTemporality.CUMULATIVE,
    ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableGauge: AggregationTemporality.CUMULATIVE,
}

from jiuwenclaw.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenclaw.telemetry.config import TelemetryConfig


@dataclass
class ProviderBundle:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    resource: Resource | None = None  # 暴露 Resource 供 metrics 获取 claw_id


def init_providers(cfg: TelemetryConfig) -> ProviderBundle:
    """Initialize OTel providers using default config or a registered extension."""
    bundle = _build_extension_provider_bundle(cfg) or build_default_providers(cfg)

    install_providers(bundle)

    # 将 Resource 传递给 metrics 模块，用于获取 claw_id
    from jiuwenclaw.telemetry.metrics import set_resource
    set_resource(bundle.resource)

    return bundle


def build_default_providers(cfg: TelemetryConfig) -> ProviderBundle:
    """Build the default OTel TracerProvider + MeterProvider bundle."""
    resource_attrs = {
        SERVICE_NAME: cfg.service_name,
        "service.version": "0.1.5",
    }
    if cfg.claw_id is not None:
        resource_attrs[JIUWENCLAW_CLAW_ID] = cfg.claw_id

    resource = Resource.create(resource_attrs)

    tracer_provider = _build_tracer_provider(cfg, resource)
    meter_provider = _build_meter_provider(cfg, resource)
    return ProviderBundle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        resource=resource,
    )


def install_providers(bundle: ProviderBundle) -> None:
    """Install OTel providers into the global SDK state."""
    if bundle.tracer_provider is not None:
        trace.set_tracer_provider(bundle.tracer_provider)
    if bundle.meter_provider is not None:
        metrics.set_meter_provider(bundle.meter_provider)


def _coerce_provider_bundle(value: Any) -> ProviderBundle:
    if isinstance(value, ProviderBundle):
        return value

    tracer_provider = getattr(value, "tracer_provider", None)
    meter_provider = getattr(value, "meter_provider", None)
    if tracer_provider is None and meter_provider is None:
        raise TypeError("Telemetry provider extension must return a ProviderBundle-like object")

    if tracer_provider is not None and not isinstance(tracer_provider, TracerProvider):
        raise TypeError(
            f"tracer_provider must be a TracerProvider instance, got {type(tracer_provider).__name__}"
        )
    if meter_provider is not None and not isinstance(meter_provider, MeterProvider):
        raise TypeError(
            f"meter_provider must be a MeterProvider instance, got {type(meter_provider).__name__}"
        )

    # 从 Provider 获取 Resource（用于 metrics 获取 claw_id）
    resource = getattr(value, "resource", None)
    if resource is None and tracer_provider is not None:
        resource = getattr(tracer_provider, "resource", None)
    if resource is None and meter_provider is not None:
        resource = getattr(meter_provider, "resource", None)

    return ProviderBundle(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        resource=resource,
    )


def _build_extension_provider_bundle(cfg: TelemetryConfig) -> ProviderBundle | None:
    try:
        from jiuwenclaw.extensions.registry import ExtensionRegistry

        registry = ExtensionRegistry.get_instance()
    except RuntimeError:
        return None

    extension = registry.get_telemetry_provider_extension()
    if extension is None:
        return None

    from jiuwenclaw.utils import logger

    result = extension.build_providers(cfg)
    if result is None:
        logger.warning("[Telemetry] TelemetryProviderExtension.build_providers returned None, falling back to defaults")
        return None

    try:
        bundle = _coerce_provider_bundle(result)
    except TypeError:
        logger.warning(
            "[Telemetry] TelemetryProviderExtension.build_providers returned invalid object: %s, "
            "falling back to defaults",
            type(result).__name__,
        )
        return None

    if bundle.tracer_provider is None and bundle.meter_provider is None:
        logger.warning(
            "[Telemetry] TelemetryProviderExtension.build_providers returned empty bundle, "
            "falling back to defaults"
        )
        return None

    try:
        extension_name = extension.metadata.name or type(extension).__name__
    except Exception:
        extension_name = type(extension).__name__
    logger.info("[Telemetry] 使用扩展 TelemetryProviderExtension: %s", extension_name)
    return bundle


def _build_tracer_provider(cfg: TelemetryConfig, resource: Resource) -> TracerProvider:
    tracer_provider = TracerProvider(resource=resource)
    exporter = cfg.traces_exporter

    if exporter == "otlp":
        span_exporter = _create_otlp_span_exporter(cfg, signal="traces")
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    elif exporter == "console":
        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter != "none":
        raise ValueError(f"Unsupported traces exporter: {exporter}")

    return tracer_provider


def _build_meter_provider(cfg: TelemetryConfig, resource: Resource) -> MeterProvider:
    metric_readers = []
    exporter = cfg.metrics_exporter

    if exporter == "otlp":
        metric_exporter = _create_otlp_metric_exporter(cfg, signal="metrics")
        metric_readers.append(
            PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30000)
        )
    elif exporter == "console":
        metric_readers.append(
            PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=3000)
        )
    elif exporter != "none":
        raise ValueError(f"Unsupported metrics exporter: {exporter}")

    return MeterProvider(resource=resource, metric_readers=metric_readers)


def _create_otlp_span_exporter(cfg: TelemetryConfig, signal: str = "traces"):
    """Create OTLP span exporter based on protocol config."""
    protocol = getattr(cfg, f"{signal}_protocol")
    endpoint = getattr(cfg, f"{signal}_endpoint")
    headers = getattr(cfg, f"{signal}_headers")
    if protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        return OTLPSpanExporter(endpoint=endpoint, headers=headers)


def _create_otlp_metric_exporter(cfg: TelemetryConfig, signal: str = "metrics"):
    """Create OTLP metric exporter based on protocol config."""
    protocol = getattr(cfg, f"{signal}_protocol")
    endpoint = getattr(cfg, f"{signal}_endpoint")
    headers = getattr(cfg, f"{signal}_headers")
    if protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        return OTLPMetricExporter(
            endpoint=f"{endpoint}/v1/metrics",
            headers=headers,
            preferred_temporality=_CUMULATIVE_TEMPORALITY,
        )
    else:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        return OTLPMetricExporter(
            endpoint=endpoint,
            headers=headers,
            preferred_temporality=_CUMULATIVE_TEMPORALITY,
        )
