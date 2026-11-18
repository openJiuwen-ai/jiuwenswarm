# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Worker request context must reach SDK run spans and restore on exit."""

import asyncio
from types import SimpleNamespace

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.server.ipc.protocol import dump_agent_request, load_agent_request
from jiuwenswarm.server.worker import dispatcher


def _request(digit):
    envelope = E2AEnvelope(
        request_id=f"request-{digit}", session_id=f"session-{digit}",
        channel="web", method="chat.send", params={},
        traceparent=f"00-{digit * 32}-{'2' * 16}-01",
    )
    return load_agent_request(dump_agent_request(e2a_to_agent_request(envelope)))


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(dispatcher, "activate_external_cli_runtime_paths", lambda: None)
    bridge = SimpleNamespace(holder=SimpleNamespace(generation=1, conn=None))
    return dispatcher.WorkerDispatcher(bridge)


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream", ["valid", None, "garbage"])
async def test_queued_and_concurrent_requests_parent_actual_sdk_spans(worker, monkeypatch, upstream):
    from openjiuwen.extensions.observability import setup
    from openjiuwen.harness.observability import setup as agent_setup
    from openjiuwen.harness.observability.run_span import open_agent_run_span

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(setup, "is_initialized", lambda: True)
    monkeypatch.setattr(setup, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(agent_setup, "is_tracing_enabled", lambda: True)

    class Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            await asyncio.sleep(0)
            root = open_agent_run_span(session_id=request.session_id, request_id=request.request_id)
            assert root is not None
            root.end()

    before = otel_context.get_current()
    try:
        queued = _request("1")
        concurrent = _request("3")
        if upstream != "valid":
            queued.traceparent = upstream
            concurrent.traceparent = upstream
        await worker._run_request(queued, 1)
        assert len(worker._queued) == 1
        assert otel_context.get_current() == before
        await worker.bind_runtime(Runtime())
        await asyncio.gather(worker._run_request(concurrent, 1), *list(worker._tasks))
        finished = exporter.get_finished_spans()
        assert len(finished) == 2
        trace_ids = {span.context.trace_id for span in finished}
        if upstream == "valid":
            assert trace_ids == {int("1" * 32, 16), int("3" * 32, 16)}
            assert all(span.parent.span_id == int("2" * 16, 16) for span in finished)
        else:
            assert len(trace_ids) == 2
            assert all(span.context.is_valid and span.parent is None for span in finished)
            assert trace_ids.isdisjoint({int("1" * 32, 16), int("3" * 32, 16)})
        assert otel_context.get_current() == before
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_dispatch_failure_restores_context_in_execution_task(worker, cancelled):
    class Runtime:
        async def dispatch_parsed_request(self, bridge, request, lock):
            assert trace.get_current_span().get_span_context().trace_id == int("1" * 32, 16)
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("dispatch failed")

    await worker.bind_runtime(Runtime())
    before = otel_context.get_current()
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await worker._run_request(_request("1"), 1)
    else:
        await worker._run_request(_request("1"), 1)
    assert otel_context.get_current() == before
