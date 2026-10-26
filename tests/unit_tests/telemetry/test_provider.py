from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider

from jiuwenswarm.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.provider import (
    ProviderBundle,
    _create_otlp_metric_exporter,
    _create_otlp_span_exporter,
    _otlp_http_endpoint,
    build_default_providers,
    build_provider_bundle,
    coerce_provider_bundle,
)
from jiuwenswarm.telemetry import provider as provider_module


class _ConstructionAborted(BaseException):
    pass


def _shutdown_bundle(bundle: ProviderBundle) -> None:
    if bundle.tracer_provider is not None:
        bundle.tracer_provider.shutdown()
    if bundle.meter_provider is not None:
        bundle.meter_provider.shutdown()


def test_provider_bundle_defaults_to_owning_providers() -> None:
    bundle = ProviderBundle()

    assert bundle.owns_tracer is True
    assert bundle.owns_meter is True


def test_coerce_provider_bundle_preserves_ownership_flags() -> None:
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    try:
        bundle = coerce_provider_bundle(
            SimpleNamespace(
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                owns_tracer=False,
                owns_meter=False,
            )
        )

        assert bundle.owns_tracer is False
        assert bundle.owns_meter is False
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_coerce_provider_bundle_defaults_ownership_and_infers_resource() -> None:
    resource = Resource({SERVICE_NAME: "extension"})
    tracer_provider = TracerProvider(resource=resource)
    try:
        bundle = coerce_provider_bundle(
            SimpleNamespace(tracer_provider=tracer_provider, meter_provider=None)
        )

        assert bundle.resource is resource
        assert bundle.owns_tracer is True
        assert bundle.owns_meter is True
    finally:
        tracer_provider.shutdown()


def test_coerce_provider_bundle_infers_resource_from_meter_provider() -> None:
    resource = Resource({SERVICE_NAME: "metrics-extension"})
    meter_provider = MeterProvider(resource=resource)
    try:
        bundle = coerce_provider_bundle(
            SimpleNamespace(tracer_provider=None, meter_provider=meter_provider)
        )

        assert bundle.resource is resource
    finally:
        meter_provider.shutdown()


def test_coerce_provider_bundle_rejects_conflicting_provider_resources() -> None:
    tracer_provider = TracerProvider(resource=Resource({SERVICE_NAME: "traces"}))
    meter_provider = MeterProvider(resource=Resource({SERVICE_NAME: "metrics"}))
    try:
        with pytest.raises(TypeError, match="conflicting Resource"):
            coerce_provider_bundle(
                SimpleNamespace(
                    tracer_provider=tracer_provider,
                    meter_provider=meter_provider,
                )
            )
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_coerce_provider_bundle_rejects_explicit_provider_resource_conflict() -> None:
    provider_resource = Resource({SERVICE_NAME: "provider"})
    explicit_resource = Resource({SERVICE_NAME: "explicit"})
    tracer_provider = TracerProvider(resource=provider_resource)
    try:
        with pytest.raises(TypeError, match="conflicting Resource"):
            coerce_provider_bundle(
                SimpleNamespace(
                    tracer_provider=tracer_provider,
                    meter_provider=None,
                    resource=explicit_resource,
                )
            )
    finally:
        tracer_provider.shutdown()


def test_coerce_provider_bundle_accepts_distinct_equal_resources() -> None:
    tracer_resource = Resource({SERVICE_NAME: "shared"})
    meter_resource = Resource({SERVICE_NAME: "shared"})
    explicit_resource = Resource({SERVICE_NAME: "shared"})
    assert tracer_resource is not meter_resource
    tracer_provider = TracerProvider(resource=tracer_resource)
    meter_provider = MeterProvider(resource=meter_resource)
    try:
        bundle = coerce_provider_bundle(
            SimpleNamespace(
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                resource=explicit_resource,
            )
        )

        assert bundle.resource is explicit_resource
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (object(), "ProviderBundle"),
        (SimpleNamespace(tracer_provider="bad", meter_provider=None), "tracer_provider"),
        (SimpleNamespace(tracer_provider=None, meter_provider="bad"), "meter_provider"),
        (
            SimpleNamespace(
                tracer_provider=TracerProvider(),
                meter_provider=None,
                resource="bad",
            ),
            "resource",
        ),
        (
            SimpleNamespace(
                tracer_provider=TracerProvider(),
                meter_provider=None,
                owns_tracer=1,
            ),
            "owns_tracer",
        ),
    ],
)
def test_coerce_provider_bundle_rejects_invalid_values(value, message: str) -> None:
    try:
        with pytest.raises(TypeError, match=message):
            coerce_provider_bundle(value)
    finally:
        provider = getattr(value, "tracer_provider", None)
        if isinstance(provider, TracerProvider):
            provider.shutdown()


def test_invalid_provider_bundle_instance_is_validated() -> None:
    with pytest.raises(TypeError, match="meter_provider"):
        coerce_provider_bundle(ProviderBundle(meter_provider="bad"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        ProviderBundle(),
        SimpleNamespace(tracer_provider=None, meter_provider=None),
    ],
)
def test_coerce_provider_bundle_rejects_empty_bundle(value) -> None:
    with pytest.raises(TypeError, match="at least one provider"):
        coerce_provider_bundle(value)


def test_build_provider_bundle_uses_extension_bundle() -> None:
    tracer_provider = TracerProvider()
    extension = Mock()
    extension.build_providers.return_value = SimpleNamespace(
        tracer_provider=tracer_provider,
        meter_provider=None,
        owns_tracer=False,
    )
    registry = Mock()
    registry.get_telemetry_provider_extension.return_value = extension
    cfg = TelemetryConfig(enabled=True)
    try:
        bundle = build_provider_bundle(cfg, registry=registry)

        assert bundle.tracer_provider is tracer_provider
        assert bundle.owns_tracer is False
        extension.build_providers.assert_called_once_with(cfg)
    finally:
        tracer_provider.shutdown()


@pytest.mark.parametrize("registry", [None, Mock()])
def test_build_provider_bundle_falls_back_when_extension_is_absent(registry) -> None:
    if registry is not None:
        registry.get_telemetry_provider_extension.return_value = None
    bundle = build_provider_bundle(
        TelemetryConfig(enabled=True, traces_exporter="none", metrics_exporter="none"),
        registry=registry,
    )
    try:
        assert isinstance(bundle.tracer_provider, TracerProvider)
        assert isinstance(bundle.meter_provider, MeterProvider)
    finally:
        _shutdown_bundle(bundle)


def test_build_provider_bundle_falls_back_when_extension_returns_none() -> None:
    extension = Mock()
    extension.build_providers.return_value = None
    registry = Mock()
    registry.get_telemetry_provider_extension.return_value = extension

    bundle = build_provider_bundle(
        TelemetryConfig(enabled=True, traces_exporter="none", metrics_exporter="none"),
        registry=registry,
    )
    try:
        assert isinstance(bundle.tracer_provider, TracerProvider)
        assert isinstance(bundle.meter_provider, MeterProvider)
    finally:
        _shutdown_bundle(bundle)


def test_invalid_extension_bundle_does_not_fall_back() -> None:
    extension = Mock()
    extension.build_providers.return_value = object()
    registry = Mock()
    registry.get_telemetry_provider_extension.return_value = extension

    with pytest.raises(TypeError, match="ProviderBundle"):
        build_provider_bundle(TelemetryConfig(enabled=True), registry=registry)


def test_empty_extension_bundle_does_not_fall_back(monkeypatch) -> None:
    extension = Mock()
    extension.build_providers.return_value = ProviderBundle()
    registry = Mock()
    registry.get_telemetry_provider_extension.return_value = extension
    default_builder = Mock()
    monkeypatch.setattr(provider_module, "build_default_providers", default_builder)

    with pytest.raises(TypeError, match="at least one provider"):
        build_provider_bundle(TelemetryConfig(enabled=True), registry=registry)

    default_builder.assert_not_called()


def test_extension_exception_propagates() -> None:
    extension = Mock()
    extension.build_providers.side_effect = RuntimeError("extension failed")
    registry = Mock()
    registry.get_telemetry_provider_extension.return_value = extension

    with pytest.raises(RuntimeError, match="extension failed"):
        build_provider_bundle(TelemetryConfig(enabled=True), registry=registry)


def test_disabled_default_bundle_does_not_create_or_own_providers() -> None:
    bundle = build_default_providers(
        TelemetryConfig(enabled=False, traces_exporter="console", metrics_exporter="console")
    )

    assert bundle == ProviderBundle(
        owns_tracer=False, owns_meter=False, owns_logger=False
    )


def test_default_resource_uses_enterprise_service_and_real_swarm_version() -> None:
    bundle = build_default_providers(TelemetryConfig(enabled=True))
    try:
        assert bundle.resource.attributes[SERVICE_NAME] == "jiuwenclaw"
        assert bundle.resource.attributes["service.version"] == (
            provider_module._package_version()
        )
    finally:
        _shutdown_bundle(bundle)


def test_none_exporters_create_shutdown_safe_owned_providers() -> None:
    bundle = build_default_providers(
        TelemetryConfig(
            enabled=True,
            service_name="test-service",
            claw_id="claw-123",
            traces_exporter="none",
            metrics_exporter="none",
        )
    )
    try:
        assert bundle.owns_tracer is True
        assert bundle.owns_meter is True
        assert bundle.tracer_provider.resource is bundle.resource
        assert bundle.resource.attributes[SERVICE_NAME] == "test-service"
        assert bundle.resource.attributes[JIUWENCLAW_CLAW_ID] == "claw-123"
        assert bundle.resource.attributes["telemetry.sdk.language"] == "python"
        assert bundle.resource.attributes["telemetry.sdk.name"] == "opentelemetry"
        assert "telemetry.sdk.version" in bundle.resource.attributes
        assert not bundle.tracer_provider._active_span_processor._span_processors
        assert not bundle.meter_provider._sdk_config.metric_readers
    finally:
        _shutdown_bundle(bundle)


def test_console_exporters_create_shutdown_safe_processors_and_readers() -> None:
    bundle = build_default_providers(
        TelemetryConfig(
            enabled=True,
            traces_exporter="console",
            metrics_exporter="console",
        )
    )
    try:
        assert len(bundle.tracer_provider._active_span_processor._span_processors) == 1
        assert len(bundle.meter_provider._sdk_config.metric_readers) == 1
    finally:
        _shutdown_bundle(bundle)


@pytest.mark.parametrize(
    ("endpoint", "path", "expected"),
    [
        ("", "/v1/traces", "/v1/traces"),
        ("https://collector", "/v1/traces", "https://collector/v1/traces"),
        ("https://collector/", "/v1/metrics", "https://collector/v1/metrics"),
        (
            "https://collector/v1/traces/",
            "/v1/traces",
            "https://collector/v1/traces",
        ),
    ],
)
def test_otlp_http_endpoint(endpoint: str, path: str, expected: str) -> None:
    assert _otlp_http_endpoint(endpoint, path) == expected


def test_http_otlp_exporters_receive_native_header_dicts(monkeypatch) -> None:
    span_exporter = object()
    metric_exporter = object()
    span_constructor = Mock(return_value=span_exporter)
    metric_constructor = Mock(return_value=metric_exporter)
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        span_constructor,
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter",
        metric_constructor,
    )
    trace_headers = {"authorization": "trace-token"}
    metric_headers = {"authorization": "metric-token"}
    cfg = TelemetryConfig(
        enabled=True,
        endpoint="https://collector",
        traces_protocol="http",
        traces_endpoint="https://collector",
        traces_headers=trace_headers,
        metrics_protocol="http",
        metrics_endpoint="https://collector",
        metrics_headers=metric_headers,
    )

    assert _create_otlp_span_exporter(cfg) is span_exporter
    assert _create_otlp_metric_exporter(cfg) is metric_exporter
    assert span_constructor.call_args.kwargs["headers"] is trace_headers
    assert metric_constructor.call_args.kwargs["headers"] is metric_headers
    assert span_constructor.call_args.kwargs["endpoint"] == "https://collector/v1/traces"
    assert metric_constructor.call_args.kwargs["endpoint"] == "https://collector/v1/metrics"


def test_http_otlp_exporters_preserve_distinct_signal_endpoints(monkeypatch) -> None:
    span_constructor = Mock(return_value=object())
    metric_constructor = Mock(return_value=object())
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        span_constructor,
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter",
        metric_constructor,
    )
    cfg = TelemetryConfig(
        enabled=True,
        endpoint="https://collector/common",
        traces_protocol="http",
        traces_endpoint="https://collector/custom/trace-ingest",
        metrics_protocol="http",
        metrics_endpoint="https://collector/custom/metric-ingest",
    )

    _create_otlp_span_exporter(cfg)
    _create_otlp_metric_exporter(cfg)

    assert span_constructor.call_args.kwargs["endpoint"] == (
        "https://collector/custom/trace-ingest"
    )
    assert metric_constructor.call_args.kwargs["endpoint"] == (
        "https://collector/custom/metric-ingest"
    )


def test_grpc_otlp_exporters_receive_native_header_dicts(monkeypatch) -> None:
    span_exporter = object()
    metric_exporter = object()
    span_constructor = Mock(return_value=span_exporter)
    metric_constructor = Mock(return_value=metric_exporter)
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        span_constructor,
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter",
        metric_constructor,
    )
    trace_headers = {"authorization": "trace-token"}
    metric_headers = {"authorization": "metric-token"}
    cfg = TelemetryConfig(
        enabled=True,
        traces_protocol="grpc",
        traces_endpoint="collector:4317",
        traces_headers=trace_headers,
        metrics_protocol="grpc",
        metrics_endpoint="collector:4317",
        metrics_headers=metric_headers,
    )

    assert _create_otlp_span_exporter(cfg) is span_exporter
    assert _create_otlp_metric_exporter(cfg) is metric_exporter
    assert span_constructor.call_args.kwargs["headers"] is trace_headers
    assert metric_constructor.call_args.kwargs["headers"] is metric_headers


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_unsupported_otlp_protocol_is_rejected(signal: str) -> None:
    cfg = TelemetryConfig(
        enabled=True,
        traces_protocol="invalid",
        metrics_protocol="invalid",
    )
    builder = (
        _create_otlp_span_exporter if signal == "traces" else _create_otlp_metric_exporter
    )

    with pytest.raises(ValueError, match="Unsupported .* protocol"):
        builder(cfg)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("traces_exporter", "zipkin", "Unsupported traces exporter"),
        ("metrics_exporter", "prometheus", "Unsupported metrics exporter"),
    ],
)
def test_unsupported_exporter_is_rejected(field: str, value: str, message: str) -> None:
    kwargs = {field: value}
    cfg = TelemetryConfig(enabled=True, **kwargs)

    with pytest.raises(ValueError, match=message):
        build_default_providers(cfg)


def test_tracer_provider_is_shutdown_when_otlp_exporter_construction_fails(
    monkeypatch,
) -> None:
    tracer_provider = Mock()
    monkeypatch.setattr(
        provider_module, "TracerProvider", Mock(return_value=tracer_provider)
    )
    monkeypatch.setattr(
        provider_module,
        "_create_otlp_span_exporter",
        Mock(side_effect=RuntimeError("exporter failed")),
    )

    with pytest.raises(RuntimeError, match="exporter failed"):
        build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="otlp",
                metrics_exporter="none",
            )
        )

    tracer_provider.shutdown.assert_called_once_with()


def test_span_exporter_is_shutdown_when_processor_construction_fails(
    monkeypatch,
) -> None:
    failure = _ConstructionAborted("processor construction aborted")
    tracer_provider = Mock()
    exporter = Mock()
    exporter.shutdown.side_effect = RuntimeError("cleanup failed")
    monkeypatch.setattr(
        provider_module, "TracerProvider", Mock(return_value=tracer_provider)
    )
    monkeypatch.setattr(
        provider_module, "_create_otlp_span_exporter", Mock(return_value=exporter)
    )
    monkeypatch.setattr(
        provider_module, "BatchSpanProcessor", Mock(side_effect=failure)
    )

    with pytest.raises(_ConstructionAborted) as raised:
        build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="otlp",
                metrics_exporter="none",
            )
        )

    assert raised.value is failure
    exporter.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()


def test_span_processor_is_shutdown_when_provider_add_fails(monkeypatch) -> None:
    failure = _ConstructionAborted("processor add aborted")
    tracer_provider = Mock()
    tracer_provider.add_span_processor.side_effect = failure
    exporter = Mock()
    processor = Mock()
    processor.shutdown.side_effect = exporter.shutdown
    monkeypatch.setattr(
        provider_module, "TracerProvider", Mock(return_value=tracer_provider)
    )
    monkeypatch.setattr(
        provider_module, "_create_otlp_span_exporter", Mock(return_value=exporter)
    )
    monkeypatch.setattr(
        provider_module, "BatchSpanProcessor", Mock(return_value=processor)
    )

    with pytest.raises(_ConstructionAborted) as raised:
        build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="otlp",
                metrics_exporter="none",
            )
        )

    assert raised.value is failure
    processor.shutdown.assert_called_once_with()
    exporter.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()


def test_metric_exporter_is_shutdown_when_reader_construction_fails(
    monkeypatch,
) -> None:
    failure = _ConstructionAborted("reader construction aborted")
    tracer_provider = Mock()
    exporter = Mock()
    monkeypatch.setattr(
        provider_module, "TracerProvider", Mock(return_value=tracer_provider)
    )
    monkeypatch.setattr(
        provider_module, "_create_otlp_metric_exporter", Mock(return_value=exporter)
    )
    monkeypatch.setattr(
        provider_module,
        "PeriodicExportingMetricReader",
        Mock(side_effect=failure),
    )

    with pytest.raises(_ConstructionAborted) as raised:
        build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="otlp",
            )
        )

    assert raised.value is failure
    exporter.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()


def test_metric_reader_is_shutdown_when_meter_provider_construction_fails(
    monkeypatch,
) -> None:
    failure = _ConstructionAborted("meter provider construction aborted")
    tracer_provider = Mock()
    exporter = Mock()
    reader = Mock()

    def shutdown_reader() -> None:
        exporter.shutdown()
        raise RuntimeError("cleanup failed")

    reader.shutdown.side_effect = shutdown_reader
    monkeypatch.setattr(
        provider_module, "TracerProvider", Mock(return_value=tracer_provider)
    )
    monkeypatch.setattr(
        provider_module, "_create_otlp_metric_exporter", Mock(return_value=exporter)
    )
    monkeypatch.setattr(
        provider_module,
        "PeriodicExportingMetricReader",
        Mock(return_value=reader),
    )
    monkeypatch.setattr(
        provider_module, "MeterProvider", Mock(side_effect=failure)
    )

    with pytest.raises(_ConstructionAborted) as raised:
        build_default_providers(
            TelemetryConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="otlp",
            )
        )

    assert raised.value is failure
    reader.shutdown.assert_called_once_with()
    exporter.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()


def test_build_default_providers_does_not_install_global_providers(monkeypatch) -> None:
    set_tracer_provider = Mock()
    set_meter_provider = Mock()
    monkeypatch.setattr(trace, "set_tracer_provider", set_tracer_provider)
    monkeypatch.setattr(metrics, "set_meter_provider", set_meter_provider)

    bundle = build_default_providers(
        TelemetryConfig(enabled=True, traces_exporter="none", metrics_exporter="none")
    )
    try:
        set_tracer_provider.assert_not_called()
        set_meter_provider.assert_not_called()
    finally:
        _shutdown_bundle(bundle)
