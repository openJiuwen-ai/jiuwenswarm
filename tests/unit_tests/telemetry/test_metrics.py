from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.export import Gauge
from opentelemetry.sdk.metrics.export import Histogram
from opentelemetry.sdk.metrics.export import MetricsData
from opentelemetry.sdk.metrics.export import Sum
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenswarm.telemetry.attributes import JIUWENCLAW_SESSION_ID
from jiuwenswarm.telemetry.metrics import METRIC_SPECS
from jiuwenswarm.telemetry.metrics import TelemetryMetrics
from jiuwenswarm.telemetry.metrics import metrics_session_id


EXPECTED_SPECS = {
    "jiuwenclaw.request.duration": ("histogram", "s", "Request duration"),
    "jiuwenclaw.request.count": ("counter", "{request}", "Requests"),
    "jiuwenclaw.request.error.count": ("counter", "{request}", "Request errors"),
    "jiuwenclaw.agent.duration": ("histogram", "s", "Agent duration"),
    "gen_ai.client.operation.duration": ("histogram", "s", "LLM duration"),
    "gen_ai.client.operation.count": ("counter", "{call}", "LLM calls"),
    "gen_ai.client.token.usage": ("counter", "{token}", "LLM tokens"),
    "gen_ai.tool.duration": ("histogram", "s", "Tool duration"),
    "gen_ai.tool.call.count": ("counter", "{call}", "Tool calls"),
    "gen_ai.tool.error.count": ("counter", "{call}", "Tool errors"),
    "jiuwenclaw.session.active": (
        "observable_gauge",
        "{session}",
        "Active sessions",
    ),
    "jiuwenclaw.session.created.count": (
        "counter",
        "{session}",
        "Created sessions",
    ),
    "jiuwenclaw.session.state": (
        "counter",
        "{transition}",
        "Session transitions",
    ),
    "jiuwenclaw.session.stuck": (
        "counter",
        "{occurrence}",
        "Stuck sessions",
    ),
    "jiuwenclaw.session.stuck_age_ms": (
        "histogram",
        "ms",
        "Stuck session age",
    ),
    "gen_ai.skill.call.count": ("counter", "{call}", "Skill calls"),
    "gen_ai.skill.duration": ("histogram", "s", "Skill duration"),
    "gen_ai.skill.error.count": ("counter", "{call}", "Skill errors"),
    "gen_ai.tool.token.usage": (
        "counter",
        "{token}",
        "Tool definition tokens",
    ),
    "gen_ai.skill.token.usage": (
        "counter",
        "{token}",
        "Skill context tokens",
    ),
    "gen_ai.client.token.first_token_duration": (
        "histogram",
        "s",
        "Time to first token",
    ),
    "jiuwenclaw.llm.cache_hit_rate": (
        "histogram",
        "1",
        "LLM KV-cache hit rate",
    ),
}

ACTIVE_SPECS = [
    (name, kind, unit, description)
    for name, (kind, unit, description) in EXPECTED_SPECS.items()
    if kind != "observable_gauge"
]


class _PublicResourceMeterProvider(MeterProvider):
    def __init__(
        self,
        reader: InMemoryMetricReader,
        *,
        public_resource: object,
    ) -> None:
        super().__init__(
            metric_readers=[reader],
            resource=Resource({JIUWENCLAW_CLAW_ID: "private-claw"}),
        )
        self.resource = public_resource


@pytest.fixture(autouse=True)
def _reset_common_attribute_context() -> Iterator[None]:
    identity_token = IdentityStore.set_identity(None)
    session_token = metrics_session_id.set(None)
    try:
        yield
    finally:
        metrics_session_id.reset(session_token)
        IdentityStore.clear(identity_token)


@contextmanager
def _metrics_provider(
    *, resource: Resource | None = None
) -> Iterator[tuple[InMemoryMetricReader, MeterProvider, TelemetryMetrics]]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], resource=resource)
    try:
        yield reader, provider, TelemetryMetrics(provider)
    finally:
        provider.shutdown()


def _metrics(data: MetricsData | None) -> list:
    if data is None:
        return []
    return [
        metric
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    ]


def _metric(reader: InMemoryMetricReader, name: str):
    matches = [metric for metric in _metrics(reader.get_metrics_data()) if metric.name == name]
    assert len(matches) == 1
    return matches[0]


def test_catalog_contains_exactly_22_metrics_with_compatible_kinds_and_units() -> None:
    assert {
        name: (spec.kind, spec.unit, spec.description)
        for name, spec in METRIC_SPECS.items()
    } == EXPECTED_SPECS
    assert len(METRIC_SPECS) == 22


def test_metrics_record_when_trace_sampler_drops_spans() -> None:
    tracer_provider = TracerProvider(sampler=ALWAYS_OFF)
    try:
        span = tracer_provider.get_tracer(__name__).start_span("dropped")
        assert not span.is_recording()
        with _metrics_provider() as (reader, _provider, telemetry_metrics):
            telemetry_metrics.add(
                "jiuwenclaw.request.count", 1, {"channel": "web"}
            )

            assert _metric(reader, "jiuwenclaw.request.count").name == (
                "jiuwenclaw.request.count"
            )
        span.end()
    finally:
        tracer_provider.shutdown()


@pytest.mark.parametrize(("name", "kind", "unit", "description"), ACTIVE_SPECS)
def test_every_active_metric_collects_name_unit_value_kind_and_labels(
    name: str, kind: str, unit: str, description: str
) -> None:
    with _metrics_provider() as (reader, _provider, telemetry_metrics):
        if kind == "counter":
            telemetry_metrics.add(name, 2, {"case": "catalog"})
        else:
            telemetry_metrics.record(name, 2.5, {"case": "catalog"})

        metric = _metric(reader, name)
        point = metric.data.data_points[0]
        assert metric.unit == unit
        assert metric.description == description
        assert dict(point.attributes) == {"case": "catalog"}
        if kind == "counter":
            assert isinstance(metric.data, Sum)
            assert point.value == 2
        else:
            assert isinstance(metric.data, Histogram)
            assert point.count == 1
            assert point.sum == 2.5


def test_active_session_gauge_observer_can_be_replaced_and_cleared() -> None:
    with _metrics_provider() as (reader, _provider, telemetry_metrics):
        telemetry_metrics.set_session_active_observer(lambda: 7)
        metric = _metric(reader, "jiuwenclaw.session.active")
        assert isinstance(metric.data, Gauge)
        assert metric.unit == "{session}"
        assert metric.description == "Active sessions"
        point = metric.data.data_points[0]
        assert point.value == 7

        repeated_point = _metric(
            reader, "jiuwenclaw.session.active"
        ).data.data_points[0]
        assert repeated_point.value == 7

        telemetry_metrics.set_session_active_observer(lambda: -3)
        point = _metric(reader, "jiuwenclaw.session.active").data.data_points[0]
        assert point.value == 0

        telemetry_metrics.set_session_active_observer(None)
        names = {metric.name for metric in _metrics(reader.get_metrics_data())}
        assert "jiuwenclaw.session.active" not in names


def test_active_session_gauge_recovers_after_observer_exception() -> None:
    def fail() -> int:
        raise RuntimeError("observer failed")

    with _metrics_provider() as (reader, _provider, telemetry_metrics):
        telemetry_metrics.set_session_active_observer(fail)
        assert reader.get_metrics_data() is None

        telemetry_metrics.set_session_active_observer(lambda: 5)
        point = _metric(reader, "jiuwenclaw.session.active").data.data_points[0]
        assert point.value == 5


def test_active_session_observer_is_provider_scoped_last_write_wins() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        first = TelemetryMetrics(provider)
        second = TelemetryMetrics(provider)

        first.set_session_active_observer(lambda: 1)
        second.set_session_active_observer(lambda: 2)
        metrics = [
            metric
            for metric in _metrics(reader.get_metrics_data())
            if metric.name == "jiuwenclaw.session.active"
        ]
        assert len(metrics) == 1
        assert len(metrics[0].data.data_points) == 1
        assert metrics[0].data.data_points[0].value == 2

        first.set_session_active_observer(None)
        assert "jiuwenclaw.session.active" not in {
            metric.name for metric in _metrics(reader.get_metrics_data())
        }

        first.set_session_active_observer(lambda: 3)
        assert (
            _metric(reader, "jiuwenclaw.session.active").data.data_points[0].value
            == 3
        )

        second.set_session_active_observer(None)
        assert "jiuwenclaw.session.active" not in {
            metric.name for metric in _metrics(reader.get_metrics_data())
        }
    finally:
        provider.shutdown()


def test_active_session_observer_can_replace_itself_without_deadlock() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        first = TelemetryMetrics(provider)
        second = TelemetryMetrics(provider)

        def replace_during_observation() -> int:
            second.set_session_active_observer(lambda: 4)
            return 3

        first.set_session_active_observer(replace_during_observation)
        assert (
            _metric(reader, "jiuwenclaw.session.active").data.data_points[0].value
            == 3
        )
        assert (
            _metric(reader, "jiuwenclaw.session.active").data.data_points[0].value
            == 4
        )
    finally:
        provider.shutdown()


def test_active_session_observers_are_isolated_between_providers() -> None:
    reader_a = InMemoryMetricReader()
    reader_b = InMemoryMetricReader()
    provider_a = MeterProvider(metric_readers=[reader_a])
    provider_b = MeterProvider(metric_readers=[reader_b])
    try:
        metrics_a = TelemetryMetrics(provider_a)
        metrics_b = TelemetryMetrics(provider_b)
        metrics_a.set_session_active_observer(lambda: 1)
        metrics_b.set_session_active_observer(lambda: 2)

        assert (
            _metric(reader_a, "jiuwenclaw.session.active").data.data_points[0].value
            == 1
        )
        assert (
            _metric(reader_b, "jiuwenclaw.session.active").data.data_points[0].value
            == 2
        )
    finally:
        provider_a.shutdown()
        provider_b.shutdown()


def test_public_meter_provider_resource_is_preferred_and_collects() -> None:
    reader = InMemoryMetricReader()
    provider = _PublicResourceMeterProvider(
        reader,
        public_resource=Resource({JIUWENCLAW_CLAW_ID: "public-claw"}),
    )
    try:
        telemetry_metrics = TelemetryMetrics(provider)
        telemetry_metrics.add("jiuwenclaw.request.count", 1)

        attributes = dict(
            _metric(reader, "jiuwenclaw.request.count").data.data_points[0].attributes
        )
        assert attributes[JIUWENCLAW_CLAW_ID] == "public-claw"
    finally:
        provider.shutdown()


def test_invalid_public_meter_provider_resource_is_rejected() -> None:
    reader = InMemoryMetricReader()
    provider = _PublicResourceMeterProvider(reader, public_resource="not-a-resource")
    try:
        with pytest.raises(
            TypeError,
            match="meter_provider.resource must be a Resource",
        ):
            TelemetryMetrics(provider)
    finally:
        provider.shutdown()


def test_common_attributes_have_documented_precedence_and_do_not_mutate_input() -> None:
    caller_attributes = {
        JIUWENCLAW_CLAW_ID: "caller-claw",
        JIUWENCLAW_SESSION_ID: "caller-session",
        "user_id": "caller-user",
        "domain_id": "caller-domain",
        "app_id": "caller-app",
        "custom": "kept",
        "empty": "",
        "none": None,
    }
    original = dict(caller_attributes)
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="identity-user", domain_id="identity-domain", app_id="identity-app")
    )
    session_token = metrics_session_id.set("context-session")
    resource = Resource({JIUWENCLAW_CLAW_ID: "resource-claw"})
    with _metrics_provider(resource=resource) as (reader, _provider, telemetry_metrics):
        try:
            telemetry_metrics.add(
                "jiuwenclaw.request.count", 1, caller_attributes
            )
        finally:
            metrics_session_id.reset(session_token)
            IdentityStore.clear(identity_token)

        telemetry_metrics.add(
            "jiuwenclaw.request.count", 2, {"phase": "after-reset"}
        )
        points = _metric(reader, "jiuwenclaw.request.count").data.data_points
        first_attributes = next(
            dict(point.attributes)
            for point in points
            if point.attributes.get("custom") == "kept"
        )
        second_attributes = next(
            dict(point.attributes)
            for point in points
            if point.attributes.get("phase") == "after-reset"
        )

    assert caller_attributes == original
    assert first_attributes == {
        JIUWENCLAW_CLAW_ID: "resource-claw",
        JIUWENCLAW_SESSION_ID: "context-session",
        "user_id": "identity-user",
        "domain_id": "identity-domain",
        "app_id": "identity-app",
        "custom": "kept",
    }
    assert second_attributes == {
        JIUWENCLAW_CLAW_ID: "resource-claw",
        "phase": "after-reset",
    }


def test_common_attributes_omit_empty_configured_values() -> None:
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="", domain_id=None, app_id="")
    )
    session_token = metrics_session_id.set("")
    resource = Resource({JIUWENCLAW_CLAW_ID: ""})
    try:
        with _metrics_provider(resource=resource) as (reader, _provider, telemetry_metrics):
            telemetry_metrics.add("jiuwenclaw.request.count", 1)
            attributes = dict(
                _metric(reader, "jiuwenclaw.request.count").data.data_points[0].attributes
            )
    finally:
        metrics_session_id.reset(session_token)
        IdentityStore.clear(identity_token)

    assert attributes == {}


@pytest.mark.parametrize(
    ("method", "name", "error_type", "message"),
    [
        ("add", "missing.metric", KeyError, "unknown metric 'missing.metric'"),
        ("record", "missing.metric", KeyError, "unknown metric 'missing.metric'"),
        (
            "add",
            "jiuwenclaw.request.duration",
            TypeError,
            "metric 'jiuwenclaw.request.duration' is histogram; expected counter",
        ),
        (
            "record",
            "jiuwenclaw.request.count",
            TypeError,
            "metric 'jiuwenclaw.request.count' is counter; expected histogram",
        ),
        (
            "add",
            "jiuwenclaw.session.active",
            TypeError,
            "metric 'jiuwenclaw.session.active' is observable_gauge; expected counter",
        ),
        (
            "record",
            "jiuwenclaw.session.active",
            TypeError,
            "metric 'jiuwenclaw.session.active' is observable_gauge; expected histogram",
        ),
    ],
)
def test_recording_api_reports_deterministic_errors(
    method: str, name: str, error_type: type[Exception], message: str
) -> None:
    with _metrics_provider() as (_reader, _provider, telemetry_metrics):
        with pytest.raises(error_type) as caught:
            getattr(telemetry_metrics, method)(name, 1)

    assert caught.value.args == (message,)


def test_telemetry_metrics_uses_only_the_explicit_meter_provider(monkeypatch) -> None:
    def reject_global_meter(*_args, **_kwargs):
        raise AssertionError("global meter must not be used")

    monkeypatch.setattr(otel_metrics, "get_meter", reject_global_meter)
    with _metrics_provider() as (reader, _provider, telemetry_metrics):
        telemetry_metrics.add("jiuwenclaw.request.count", 1)
        assert _metric(reader, "jiuwenclaw.request.count").data.data_points[0].value == 1


def test_metrics_are_separated_between_explicit_providers() -> None:
    reader_a = InMemoryMetricReader()
    reader_b = InMemoryMetricReader()
    provider_a = MeterProvider(metric_readers=[reader_a])
    provider_b = MeterProvider(metric_readers=[reader_b])
    try:
        metrics_a = TelemetryMetrics(provider_a)
        TelemetryMetrics(provider_b)
        metrics_a.add("jiuwenclaw.request.count", 4)

        assert _metric(reader_a, "jiuwenclaw.request.count").data.data_points[0].value == 4
        assert reader_b.get_metrics_data() is None
    finally:
        provider_a.shutdown()
        provider_b.shutdown()
