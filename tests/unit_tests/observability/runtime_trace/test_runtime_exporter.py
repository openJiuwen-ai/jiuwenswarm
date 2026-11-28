# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the optional OTLP exporter wiring."""

from __future__ import annotations

import subprocess
import sys
import time
import types

from jiuwenswarm.runtime_trace import lifecycle
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings
from jiuwenswarm.runtime_trace.recorder import _build_exporter, build_runtime_tracer


def _settings(**overrides) -> RuntimeTraceSettings:
    values = {
        "enabled": True,
        "slow_task_ms": 1000,
        "retention_days": 7,
        "exporter": "none",
        "endpoint": "",
        "headers": (),
    }
    values.update(overrides)
    return RuntimeTraceSettings(**values)


def test_none_exporter_does_not_import_otlp_exporter_modules() -> None:
    script = """
import sys
from jiuwenswarm.runtime_trace.config import RuntimeTraceSettings
from jiuwenswarm.runtime_trace.recorder import build_runtime_tracer
settings = RuntimeTraceSettings(enabled=True, slow_task_ms=1000, retention_days=7,
                                exporter="none", endpoint="", headers=())
provider, processor = build_runtime_tracer("test", settings)
hits = [name for name in sys.modules if name in (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "openjiuwen.extensions.observability.runtime",
    "openjiuwen.extensions.observability.exporters.langfuse",
)]
if hits:
    raise SystemExit("unexpected heavy modules: " + ",".join(hits[:5]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_http_exporter_gets_explicit_timeout(monkeypatch) -> None:
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    captured: dict = {}
    original = trace_exporter.OTLPSpanExporter.__init__

    def spy(self, *args, **kwargs):
        captured.update(kwargs)
        original(self, *args, **kwargs)

    monkeypatch.setattr(trace_exporter.OTLPSpanExporter, "__init__", spy)
    exporter = _build_exporter(
        _settings(
            exporter="otlp_http",
            endpoint="http://127.0.0.1:4318/v1/traces",
            headers=(("Authorization", "Bearer x"),),
        )
    )
    assert exporter is not None
    assert captured["timeout"] == 2.0
    assert captured["headers"] == {"Authorization": "Bearer x"}
    assert getattr(exporter, "_endpoint", "") == "http://127.0.0.1:4318/v1/traces"


def test_grpc_exporter_is_insecure_with_explicit_timeout() -> None:
    exporter = _build_exporter(
        _settings(exporter="otlp_grpc", endpoint="http://127.0.0.1:4317")
    )
    assert exporter is not None
    assert getattr(exporter, "_timeout", None) == 2.0


def test_console_exporter_is_available_for_debug() -> None:
    exporter = _build_exporter(_settings(exporter="console"))
    assert exporter is not None


def test_exporter_construction_failure_degrades_to_local_only(monkeypatch) -> None:
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    def broken(self, *args, **kwargs):
        raise ValueError("bad endpoint")

    monkeypatch.setattr(trace_exporter.OTLPSpanExporter, "__init__", broken)
    exporter = _build_exporter(
        _settings(exporter="otlp_http", endpoint="http://127.0.0.1:4318/v1/traces")
    )
    assert exporter is None


def test_provider_has_no_export_processor_when_disabled() -> None:
    provider, processor = build_runtime_tracer("test", _settings())
    assert processor is not None
    # Only the record processor is attached; exporter processing would add a
    # second one.
    assert len(provider._active_span_processor._span_processors) == 1


def test_flush_exporter_with_budget_abandons_blocked_flush(monkeypatch) -> None:
    class _BlockingProvider:
        def __init__(self) -> None:
            self.shutdown_called = False

        def force_flush(self, timeout_millis=None):
            time.sleep(1.0)
            return True

        def shutdown(self) -> None:
            self.shutdown_called = True

    provider = _BlockingProvider()
    started = time.monotonic()
    lifecycle._flush_exporter_with_budget(provider, time.monotonic() + 0.1)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert provider.shutdown_called is False


def test_shutdown_never_calls_provider_shutdown(monkeypatch) -> None:
    class _Provider:
        def __init__(self) -> None:
            self.flushed = 0
            self.shutdown_called = False

        def force_flush(self, timeout_millis=None):
            self.flushed += 1
            return True

        def shutdown(self) -> None:
            self.shutdown_called = True

    class _Processor:
        def remove_consumer(self, sink, timeout_millis=30000):
            return None

    class _Writer:
        def request_stop(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    class _Recorder:
        shutdown_handle = None

    provider = _Provider()
    components = types.SimpleNamespace(
        recorder=_Recorder(),
        provider=provider,
        processor=_Processor(),
        sink=object(),
        writer=_Writer(),
        clock=None,
        settings=_settings(),
        role="front",
        run_id="r",
    )
    monkeypatch.setattr(lifecycle, "_components", components)
    assert lifecycle.shutdown_runtime_trace(deadline_mono=time.monotonic() + 1) is True
    assert provider.flushed == 1
    assert provider.shutdown_called is False
    monkeypatch.setattr(lifecycle, "_components", None)
