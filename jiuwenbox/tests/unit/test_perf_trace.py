"""Perf collection stays off unless configure() is given a path."""

from __future__ import annotations

from jiuwenbox import perf_trace


def test_span_is_noop_until_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(perf_trace.ENV_PERF_LOG, raising=False)
    perf_trace.configure(None)
    log_path = tmp_path / "create.perf"
    with perf_trace.span("create.popen", "sb"):
        pass
    perf_trace.begin("create", "sb").close()
    assert not log_path.exists()
    assert perf_trace.enabled() is False


def test_configure_appends_step_timings(tmp_path):
    log_path = tmp_path / "logs" / "create.perf"
    perf_trace.configure(str(log_path))
    try:
        assert perf_trace.enabled() is True
        with perf_trace.span("uplink.veth_host", "sb-1"):
            pass
        perf_trace.begin("create", "sb-1").close()
    finally:
        perf_trace.configure(None)

    lines = [
        line for line in log_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    steps = [line.split("\t")[3] for line in lines]
    assert steps == ["uplink.veth_host", "create"]
    for line in lines:
        start_ms, elapsed_ms, sandbox_id, _step = line.split("\t")
        assert float(start_ms) >= 0
        assert float(elapsed_ms) >= 0
        assert sandbox_id == "sb-1"
    assert perf_trace.enabled() is False
