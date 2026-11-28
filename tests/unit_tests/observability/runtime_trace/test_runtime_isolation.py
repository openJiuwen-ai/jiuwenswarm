# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolation tests: private providers never leak into the global one."""

from __future__ import annotations

import subprocess
import sys

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

from jiuwenswarm.runtime_trace import lifecycle, state
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings


class _CountingProcessor(SpanProcessor):
    def __init__(self) -> None:
        self.spans = []

    def on_start(self, span, parent_context=None) -> None:
        return None

    def on_end(self, span) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def test_runtime_spans_do_not_enter_global_provider(tmp_path, monkeypatch) -> None:
    counter = _CountingProcessor()
    global_provider = TracerProvider()
    global_provider.add_span_processor(counter)
    processors_before = list(
        global_provider._active_span_processor._span_processors
    )

    monkeypatch.setattr(lifecycle, "_runtime_root", lambda: tmp_path / "runtime")
    with lifecycle._lock:
        lifecycle._components = None
    state.clear_state()
    try:
        settings = RuntimeTraceSettings(
            enabled=True, slow_task_ms=1000, retention_days=7, exporter="none"
        )
        recorder = lifecycle.attach_runtime_trace(role="worker", settings=settings)
        assert recorder is not None

        business_tracer = global_provider.get_tracer("business")
        with business_tracer.start_as_current_span("session.turn"):
            root = recorder.start("startup", level="root")
            recorder.end(root, "success")

        names = [span.name for span in counter.spans]
        assert names == ["session.turn"]
        assert (
            list(global_provider._active_span_processor._span_processors)
            == processors_before
        )
        lifecycle.shutdown_runtime_trace()
    finally:
        state.clear_state()
        with lifecycle._lock:
            lifecycle._components = None


def test_disabled_imports_add_no_runtime_modules() -> None:
    script = """
import sys
import jiuwenswarm.server.worker.main  # noqa: F401
import jiuwenswarm.server.worker.service  # noqa: F401
import jiuwenswarm.server.app_agentserver  # noqa: F401
forbidden = (
    "aiosqlite",
    "jiuwenswarm.observability",
    "openjiuwen.extensions.observability.span_record_processor",
    "jiuwenswarm.runtime_trace.recorder",
    "jiuwenswarm.runtime_trace.writer",
    "jiuwenswarm.runtime_trace.store",
    "jiuwenswarm.runtime_trace.lifecycle",
)
hits = []
for name in sys.modules:
    for prefix in forbidden:
        if name == prefix or name.startswith(prefix + "."):
            hits.append(name)
if hits:
    raise SystemExit("unexpected modules: " + ",".join(sorted(hits)[:20]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_stub_worker_imports_no_otel() -> None:
    script = """
import sys
import jiuwenswarm.server.worker.stub  # noqa: F401
bad = [name for name in sys.modules if name.startswith(("opentelemetry", "openjiuwen"))]
if bad:
    raise SystemExit("unexpected modules: " + ",".join(sorted(bad)[:10]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
