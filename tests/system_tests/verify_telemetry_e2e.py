"""Fresh-evidence gate for unified telemetry topology and the 21 metrics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from scripts.e2e_telemetry_trace import (
    DEFAULT_EVIDENCE,
    EXPECTED_METRICS,
    FORBIDDEN_SPAN_NAMES,
    FORBIDDEN_SPAN_PREFIXES,
    EvidenceError,
    load_fresh_evidence,
)


pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    path = Path(os.getenv("TELEMETRY_E2E_EVIDENCE", str(DEFAULT_EVIDENCE)))
    max_age = float(os.getenv("TELEMETRY_E2E_MAX_AGE_SECONDS", "900"))
    try:
        return load_fresh_evidence(path, max_age_seconds=max_age)
    except EvidenceError as error:
        pytest.fail(str(error))


def _flow_spans(evidence: dict[str, Any], flow: dict[str, Any]) -> list[dict[str, Any]]:
    request_id = flow["request_id"]
    return [
        span
        for span in evidence["spans"]
        if (span.get("attributes") or {}).get("jiuwenswarm.request.id") == request_id
    ]


def test_streaming_and_non_streaming_websocket_flows_are_fresh(
    evidence: dict[str, Any],
) -> None:
    success = {
        (flow["mode"], flow["streaming"])
        for flow in evidence["flows"]
        if flow.get("scenario") == "success"
        and flow.get("transport") == "websocket"
        and flow.get("terminal_event") != "chat.error"
    }
    assert ("code.normal", True) in success
    assert ("agent.plan", False) in success
    started = evidence["run_started_unix_nano"]
    assert all(flow["started_unix_nano"] >= started for flow in evidence["flows"])


def test_gateway_and_agent_roots_share_trace_and_parent_chain(
    evidence: dict[str, Any],
) -> None:
    all_spans = evidence["spans"]
    by_trace_and_id = {(span["trace_id"], span["span_id"]): span for span in all_spans}
    assert len(by_trace_and_id) == len(all_spans), "duplicate exported span identity"

    for flow in evidence["flows"]:
        if (
            flow.get("scenario") != "success"
            or flow.get("entrypoint") != "gateway"
        ):
            continue
        spans = _flow_spans(evidence, flow)
        gateways = [span for span in spans if span["name"] == "channel.request"]
        assert len(gateways) == 1
        roots = [
            span
            for span in spans
            if span["name"].startswith("agent.")
            and span.get("parent_span_id") == gateways[0]["span_id"]
            and (span.get("attributes") or {}).get("jiuwenswarm.mode")
            in (
                {"agent", flow["mode"]}
                if flow["mode"] == "agent.plan"
                else {flow["mode"]}
            )
        ]
        assert len(roots) == 1
        assert gateways[0]["trace_id"] == roots[0]["trace_id"]
        assert roots[0]["parent_span_id"] == gateways[0]["span_id"]

    for span in all_spans:
        parent_id = span.get("parent_span_id")
        if parent_id:
            assert (span["trace_id"], parent_id) in by_trace_and_id, (
                f"orphan span {span['name']} parent={parent_id}"
            )


def test_core_spans_are_enriched_without_enterprise_duplicates(
    evidence: dict[str, Any],
) -> None:
    names = [span["name"] for span in evidence["spans"]]
    assert "llm.call" in names
    assert any(name.startswith("tool.") for name in names)
    assert not FORBIDDEN_SPAN_NAMES.intersection(names)
    assert not any(name.startswith(FORBIDDEN_SPAN_PREFIXES) for name in names)
    llm = next(
        span
        for span in evidence["spans"]
        if span["name"] == "llm.call"
        and "gen_ai.usage.input_tokens" in span["attributes"]
    )
    attrs = llm["attributes"]
    assert attrs["gen_ai.request.model"]
    assert attrs["gen_ai.usage.input_tokens"] >= 0
    assert attrs["gen_ai.span.type"] == "model"
    assert attrs["jiuwenswarm.request.id"]


def test_all_21_metrics_have_points_from_this_run(evidence: dict[str, Any]) -> None:
    started = evidence["run_started_unix_nano"]
    fresh_names = {
        point["name"]
        for point in evidence["metrics"]
        if point["time_unix_nano"] >= started
    }
    assert EXPECTED_METRICS <= fresh_names
    assert len(EXPECTED_METRICS) == 21
