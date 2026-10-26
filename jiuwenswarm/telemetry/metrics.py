"""Enterprise-compatible metric catalog backed by an explicit meter provider."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal
from weakref import WeakKeyDictionary

from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Meter,
    Observation,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource

from jiuwenswarm.extensions.identity_provider import IdentityStore
from jiuwenswarm.telemetry.attributes import JIUWENCLAW_CLAW_ID
from jiuwenswarm.telemetry.attributes import JIUWENCLAW_SESSION_ID


MetricKind = Literal["counter", "histogram", "observable_gauge"]


@dataclass(frozen=True)
class MetricSpec:
    kind: MetricKind
    unit: str
    description: str


METRIC_SPECS: dict[str, MetricSpec] = {
    "jiuwenclaw.request.duration": MetricSpec("histogram", "s", "Request duration"),
    "jiuwenclaw.request.count": MetricSpec("counter", "{request}", "Requests"),
    "jiuwenclaw.request.error.count": MetricSpec(
        "counter", "{request}", "Request errors"
    ),
    "jiuwenclaw.agent.duration": MetricSpec("histogram", "s", "Agent duration"),
    "gen_ai.client.operation.duration": MetricSpec("histogram", "s", "LLM duration"),
    "gen_ai.client.operation.count": MetricSpec("counter", "{call}", "LLM calls"),
    "gen_ai.client.token.usage": MetricSpec("counter", "{token}", "LLM tokens"),
    "gen_ai.tool.duration": MetricSpec("histogram", "s", "Tool duration"),
    "gen_ai.tool.call.count": MetricSpec("counter", "{call}", "Tool calls"),
    "gen_ai.tool.error.count": MetricSpec("counter", "{call}", "Tool errors"),
    "jiuwenclaw.session.active": MetricSpec(
        "observable_gauge", "{session}", "Active sessions"
    ),
    "jiuwenclaw.session.created.count": MetricSpec(
        "counter", "{session}", "Created sessions"
    ),
    "jiuwenclaw.session.state": MetricSpec(
        "counter", "{transition}", "Session transitions"
    ),
    "jiuwenclaw.session.stuck": MetricSpec(
        "counter", "{occurrence}", "Stuck sessions"
    ),
    "jiuwenclaw.session.stuck_age_ms": MetricSpec(
        "histogram", "ms", "Stuck session age"
    ),
    "gen_ai.skill.call.count": MetricSpec("counter", "{call}", "Skill calls"),
    "gen_ai.skill.duration": MetricSpec("histogram", "s", "Skill duration"),
    "gen_ai.skill.error.count": MetricSpec("counter", "{call}", "Skill errors"),
    "gen_ai.tool.token.usage": MetricSpec(
        "counter", "{token}", "Tool definition tokens"
    ),
    "gen_ai.skill.token.usage": MetricSpec(
        "counter", "{token}", "Skill context tokens"
    ),
    "gen_ai.client.token.first_token_duration": MetricSpec(
        "histogram", "s", "Time to first token"
    ),
    "jiuwenclaw.llm.cache_hit_rate": MetricSpec(
        "histogram", "1", "LLM KV-cache hit rate"
    ),
}


# Compatibility name retained from Enterprise TelemetryRail. Request entrypoints
# own the token lifecycle so concurrent asyncio tasks remain isolated.
metrics_session_id: ContextVar[str | None] = ContextVar(
    "metrics_session_id", default=None
)
metrics_channel_id: ContextVar[str | None] = ContextVar(
    "metrics_channel_id", default=None
)
metrics_mode_id: ContextVar[str | None] = ContextVar(
    "metrics_mode_id", default=None
)
metrics_adapter_mode_id: ContextVar[str | None] = ContextVar(
    "metrics_adapter_mode_id", default=None
)


class _SessionActiveObserverState:
    """Provider-scoped callback state shared by all TelemetryMetrics facades."""

    def __init__(self, resource: Resource) -> None:
        self._resource = resource
        self._lock = Lock()
        self._observer: Callable[[], int] | None = None

    def replace(self, observer: Callable[[], int] | None) -> None:
        with self._lock:
            self._observer = observer

    def observe(self, _options: CallbackOptions) -> Iterable[Observation]:
        with self._lock:
            observer = self._observer
        if observer is None:
            return []
        try:
            active_sessions = max(int(observer()), 0)
        except Exception:
            return []
        return [
            Observation(
                active_sessions,
                attributes=_common_attributes(self._resource, None),
            )
        ]


_OBSERVER_STATES: WeakKeyDictionary[MeterProvider, _SessionActiveObserverState] = (
    WeakKeyDictionary()
)
_OBSERVER_STATES_LOCK = Lock()


class TelemetryMetrics:
    """Create and record the fixed metric catalog on one explicit provider."""

    def __init__(self, meter_provider: MeterProvider) -> None:
        if not isinstance(meter_provider, MeterProvider):
            raise TypeError("meter_provider must be a MeterProvider")

        self._resource = _provider_resource(meter_provider)
        meter = meter_provider.get_meter("jiuwenswarm.telemetry.metrics")
        self._session_active_state = _provider_observer_state(
            meter_provider,
            meter,
            self._resource,
        )
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

        for name, spec in METRIC_SPECS.items():
            if spec.kind == "counter":
                self._counters[name] = meter.create_counter(
                    name=name,
                    unit=spec.unit,
                    description=spec.description,
                )
            elif spec.kind == "histogram":
                self._histograms[name] = meter.create_histogram(
                    name=name,
                    unit=spec.unit,
                    description=spec.description,
                )

    def add(
        self,
        name: str,
        value: int | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Add to a catalog counter or raise a stable error for invalid use."""
        self._require_kind(name, "counter")
        self._counters[name].add(value, self._common_attributes(attributes))

    def record(
        self,
        name: str,
        value: int | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a catalog histogram or raise a stable error for invalid use."""
        self._require_kind(name, "histogram")
        self._histograms[name].record(value, self._common_attributes(attributes))

    def set_session_active_observer(self, observer: Callable[[], int] | None) -> None:
        """Replace or clear the callback used by the active-session gauge."""
        if observer is not None and not callable(observer):
            raise TypeError("session active observer must be callable or None")
        self._session_active_state.replace(observer)

    @property
    def session_active_observer_identity(self) -> object:
        """Return the provider-scoped identity used by the active gauge."""
        return self._session_active_state

    @staticmethod
    def _require_kind(name: str, expected: MetricKind) -> None:
        spec = METRIC_SPECS.get(name)
        if spec is None:
            raise KeyError(f"unknown metric '{name}'")
        if spec.kind != expected:
            raise TypeError(f"metric '{name}' is {spec.kind}; expected {expected}")

    def _common_attributes(
        self, attributes: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Merge labels with deterministic caller < resource/context < identity precedence.

        Empty strings and ``None`` are omitted. A new mapping is always returned,
        so neither precedence resolution nor filtering mutates the caller's input.
        """
        return _common_attributes(self._resource, attributes)


def _provider_observer_state(
    meter_provider: MeterProvider,
    meter: Meter,
    resource: Resource,
) -> _SessionActiveObserverState:
    with _OBSERVER_STATES_LOCK:
        state = _OBSERVER_STATES.get(meter_provider)
        if state is not None:
            return state

        state = _SessionActiveObserverState(resource)
        spec = METRIC_SPECS["jiuwenclaw.session.active"]
        meter.create_observable_gauge(
            name="jiuwenclaw.session.active",
            callbacks=[state.observe],
            unit=spec.unit,
            description=spec.description,
        )
        _OBSERVER_STATES[meter_provider] = state
        return state


def _common_attributes(
    resource: Resource,
    attributes: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = {
        key: value
        for key, value in dict(attributes or {}).items()
        if value is not None and value != ""
    }

    claw_id = resource.attributes.get(JIUWENCLAW_CLAW_ID)
    if claw_id is not None and claw_id != "":
        merged[JIUWENCLAW_CLAW_ID] = claw_id

    session_id = metrics_session_id.get()
    if session_id:
        merged[JIUWENCLAW_SESSION_ID] = session_id

    try:
        identity = IdentityStore.get_identity()
    except Exception:
        identity = None
    if identity is not None:
        for key, value in (
            ("user_id", identity.user_id),
            ("domain_id", identity.domain_id),
            ("app_id", identity.app_id),
        ):
            if value is not None and value != "":
                merged[key] = value
    return merged


def _provider_resource(meter_provider: MeterProvider) -> Resource:
    public_resource = getattr(meter_provider, "resource", None)
    if public_resource is not None:
        if not isinstance(public_resource, Resource):
            raise TypeError("meter_provider.resource must be a Resource")
        return public_resource

    sdk_config = getattr(meter_provider, "_sdk_config", None)
    resource = getattr(sdk_config, "resource", None)
    if not isinstance(resource, Resource):
        raise TypeError("meter_provider must expose a Resource")
    return resource
