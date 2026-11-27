# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tests for TelemetryRail hook failure safety and circuit breaker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenclaw.telemetry.instrumentors.telemetry_rail import TelemetryRail


class _BoomInputs:
    """Object whose `inputs` attribute raises — forces hook internals to explode."""

    @property
    def inputs(self):
        raise RuntimeError("boom")


async def test_before_invoke_exception_is_swallowed():
    rail = TelemetryRail()
    # Must NOT raise
    await rail.before_invoke(_BoomInputs())


async def test_after_invoke_exception_is_swallowed():
    rail = TelemetryRail()

    class BoomError:
        @property
        def error(self):
            raise RuntimeError("boom")

    # Must NOT raise even with a poisonous ctx
    await rail.after_invoke(BoomError())


async def test_before_model_call_exception_is_swallowed():
    rail = TelemetryRail()

    # before_model_call reads ctx.model / ctx.messages; a plain bad object works
    class Bad:
        @property
        def model(self):
            raise RuntimeError("boom")

    await rail.before_model_call(Bad())


async def test_circuit_breaker_trips_after_threshold(monkeypatch):
    monkeypatch.setenv("OTEL_HOOK_FAILURE_THRESHOLD", "3")
    rail = TelemetryRail()

    for _ in range(5):
        await rail.before_invoke(_BoomInputs())

    assert rail._degraded is True
    assert rail._failure_count >= 3


async def test_degraded_rail_is_noop():
    rail = TelemetryRail()
    rail._degraded = True
    # Happy-path ctx — when not degraded this would try to start a span
    ctx = SimpleNamespace(inputs=SimpleNamespace(conversation_id="c1"))
    await rail.before_invoke(ctx)
    # After moving to ContextVar, check _agent_span_ctx instead of instance attr
    from jiuwenclaw.telemetry.instrumentors.telemetry_rail import _agent_span_ctx
    assert _agent_span_ctx.get() is None
