# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker request dispatch must bind the W3C parent context and restore it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from e2a.models import E2AEnvelope
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.ipc.protocol import dump_agent_request, load_agent_request
from jiuwenswarm.server.worker import dispatcher

_VALID_TRACEPARENT = f"00-{'1' * 32}-{'2' * 16}-01"


def _request(traceparent: str | None) -> AgentRequest:
    """Follow the Gateway envelope through AgentServer conversion and IPC."""
    envelope = E2AEnvelope.from_dict({
        "request_id": "request-1",
        "channel": "web",
        "method": "chat.send",
        "traceparent": traceparent,
        "channel_context": {"business_key": "preserved"},
    })
    request = e2a_to_agent_request(envelope)
    restored = load_agent_request(dump_agent_request(request))
    assert (restored.metadata or {}).get("traceparent") == traceparent
    assert restored.metadata["business_key"] == "preserved"
    assert envelope.channel_context == {"business_key": "preserved"}
    return restored


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(dispatcher, "activate_external_cli_runtime_paths", lambda: None)
    bridge = SimpleNamespace(holder=SimpleNamespace(generation=1, conn=None))
    return dispatcher.WorkerDispatcher(bridge)


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream", ["valid", "missing", "garbage"])
async def test_run_request_binds_parent_context_only_for_valid_input(worker, monkeypatch, upstream):
    seen = {}

    async def fake_dispatch(request, generation, dispatch_id):
        seen["span"] = trace.get_current_span()
        await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_dispatch_request", fake_dispatch)
    traceparent = {
        "valid": _VALID_TRACEPARENT,
        "missing": None,
        "garbage": "not-a-traceparent",
    }[upstream]
    before = otel_context.get_current()
    await worker._run_request(_request(traceparent), 1, "dispatch-1")
    assert otel_context.get_current() == before
    if upstream == "valid":
        assert seen["span"].get_span_context().trace_id == int("1" * 32, 16)
        assert seen["span"].get_span_context().span_id == int("2" * 16, 16)
    else:
        assert not seen["span"].get_span_context().is_valid


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_dispatch_failure_restores_context(worker, monkeypatch, cancelled):
    async def fake_dispatch(request, generation, dispatch_id):
        assert trace.get_current_span().get_span_context().trace_id == int("1" * 32, 16)
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(worker, "_dispatch_request", fake_dispatch)
    before = otel_context.get_current()
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await worker._run_request(_request(_VALID_TRACEPARENT), 1, "dispatch-1")
    else:
        with pytest.raises(RuntimeError):
            await worker._run_request(_request(_VALID_TRACEPARENT), 1, "dispatch-1")
    assert otel_context.get_current() == before


@pytest.mark.parametrize("traceparent", [_VALID_TRACEPARENT, "invalid", None])
def test_envelope_traceparent_takes_precedence_over_channel_metadata(traceparent):
    channel_value = f"00-{'3' * 32}-{'4' * 16}-01"
    envelope = E2AEnvelope.from_dict({
        "request_id": "request-1",
        "traceparent": traceparent,
        "channel_context": {"traceparent": channel_value},
    })
    restored = load_agent_request(dump_agent_request(e2a_to_agent_request(envelope)))
    if traceparent is None:
        assert restored.metadata.get("traceparent") == channel_value
    else:
        assert restored.metadata.get("traceparent") == traceparent


def test_envelope_without_traceparent_omits_metadata_key():
    envelope = E2AEnvelope.from_dict({"request_id": "request-1"})
    restored = load_agent_request(dump_agent_request(e2a_to_agent_request(envelope)))
    assert "traceparent" not in (restored.metadata or {})


@pytest.mark.asyncio
async def test_concurrent_requests_create_sdk_spans_with_isolated_parents(worker, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    async def dispatch(request, generation, dispatch_id):
        with tracer.start_as_current_span(dispatch_id):
            await asyncio.sleep(0)

    monkeypatch.setattr(worker, "_dispatch_request", dispatch)
    before = otel_context.get_current()
    try:
        await asyncio.gather(
            worker._run_request(_request(_VALID_TRACEPARENT), 1, "first"),
            worker._run_request(_request(f"00-{'3' * 32}-{'4' * 16}-01"), 1, "second"),
        )
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert spans["first"].context.trace_id == int("1" * 32, 16)
        assert spans["first"].parent.span_id == int("2" * 16, 16)
        assert spans["second"].context.trace_id == int("3" * 32, 16)
        assert spans["second"].parent.span_id == int("4" * 16, 16)
        assert otel_context.get_current() == before
    finally:
        provider.shutdown()
