# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""End-to-end telemetry verifier.

Prerequisites (run separately; see scripts/verify-telemetry.sh):
    docker compose -f deploy/telemetry/docker-compose.telemetry.yml up -d

    # Then start gateway + agentserver with OTLP envs and drive three
    # representative flows (pure LLM / single tool / nested tool / one
    # tool-error). Once traces + metrics are flowing, run:
    pytest tests/system_tests/verify_telemetry_e2e.py -v
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
import pytest

logger = logging.getLogger(__name__)

JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
SERVICE = os.getenv("OTEL_SERVICE_NAME", "jiuwenclaw")

# Canonical Prometheus-normalized metric names for the 19 Plan-B metrics.
# The OTel → Prometheus normalization drops units, replaces dots with
# underscores, and appends `_total` on counters, `_count` on histogram
# sample counts, or bare name on gauges.
EXPECTED_METRICS = [
    "jiuwenclaw_request_duration_seconds_count",
    "jiuwenclaw_request_count_total",
    "jiuwenclaw_request_error_count_total",
    "jiuwenclaw_agent_duration_seconds_count",
    "gen_ai_client_operation_duration_seconds_count",
    "gen_ai_client_operation_count_total",
    "gen_ai_client_token_usage_total",
    "gen_ai_tool_duration_seconds_count",
    "gen_ai_tool_call_count_total",
    "gen_ai_tool_error_count_total",
    "jiuwenclaw_queue_depth",
    "jiuwenclaw_queue_enqueued_total",
    "jiuwenclaw_queue_dequeued_total",
    "jiuwenclaw_queue_wait_duration_milliseconds_count",
    "jiuwenclaw_message_processed_total",
    "jiuwenclaw_session_created_count_total",
    "jiuwenclaw_session_state_total",
    "jiuwenclaw_session_stuck_total",
    "jiuwenclaw_session_stuck_age_ms_milliseconds_count",
]


@pytest.fixture(scope="module")
def traffic_driven() -> None:
    """Wait until Jaeger has at least one trace for the service.

    The caller (scripts/verify-telemetry.sh) is responsible for having driven
    traffic before invoking these tests. If no traces land within 60s, skip
    the whole module.
    """
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{JAEGER_URL}/api/traces",
                params={"service": SERVICE, "limit": 1},
                timeout=5,
            )
            if r.status_code == 200 and r.json().get("data"):
                return
        except httpx.HTTPError as e:
            # Log transient failures but continue polling
            logger.debug("Jaeger query failed (will retry): %s", e)
        time.sleep(2)
    pytest.skip("No traces in Jaeger within 60s — driver/stack not ready?")


def _fetch_traces(limit: int = 20) -> list[dict[str, Any]]:
    r = httpx.get(
        f"{JAEGER_URL}/api/traces",
        params={"service": SERVICE, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def _query_prom(metric: str) -> float:
    r = httpx.get(
        f"{PROM_URL}/api/v1/query",
        params={"query": metric},
        timeout=10,
    )
    r.raise_for_status()
    result = r.json().get("data", {}).get("result", [])
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def test_entry_span(traffic_driven: None) -> None:
    traces = _fetch_traces()
    spans = [s for tr in traces for s in tr.get("spans", [])]
    entry = [s for s in spans if s["operationName"] == "jiuwenclaw.request"]
    if not entry:
        pytest.fail("no jiuwenclaw.request (ENTRY) span found")


def test_llm_parent(traffic_driven: None) -> None:
    traces = _fetch_traces()
    for tr in traces:
        by_id = {s["spanID"]: s for s in tr["spans"]}
        for span in tr["spans"]:
            if span["operationName"] != "gen_ai.chat":
                continue
            refs = span.get("references", [])
            parents = [
                by_id[r["spanID"]]
                for r in refs
                if r["refType"] == "CHILD_OF" and r["spanID"] in by_id
            ]
            if not any(
                p["operationName"] == "jiuwenclaw.agent.invoke" for p in parents
            ):
                pytest.fail("gen_ai.chat must parent to jiuwenclaw.agent.invoke")
            return
    pytest.fail("no gen_ai.chat span found in any trace")


def test_tool_parent(traffic_driven: None) -> None:
    traces = _fetch_traces()
    for tr in traces:
        by_id = {s["spanID"]: s for s in tr["spans"]}
        for span in tr["spans"]:
            if not span["operationName"].startswith("gen_ai.tool.execute"):
                continue
            refs = span.get("references", [])
            parents = [
                by_id[r["spanID"]]
                for r in refs
                if r["refType"] == "CHILD_OF" and r["spanID"] in by_id
            ]
            if not any(
                p["operationName"] == "jiuwenclaw.agent.invoke" for p in parents
            ):
                pytest.fail("gen_ai.tool.execute must parent to jiuwenclaw.agent.invoke")
            return
    pytest.fail("no gen_ai.tool.execute span found in any trace")


def test_cross_process_parent(traffic_driven: None) -> None:
    """ENTRY (gateway) and AGENT (agentserver) must share trace_id."""
    traces = _fetch_traces()
    for tr in traces:
        names = {s["operationName"] for s in tr["spans"]}
        if {"jiuwenclaw.request", "jiuwenclaw.agent.invoke"}.issubset(names):
            return
    pytest.fail(
        "no single trace contains both jiuwenclaw.request and jiuwenclaw.agent.invoke — "
        "cross-process propagation broken"
    )


@pytest.mark.parametrize("metric", EXPECTED_METRICS)
def test_metric_has_samples(traffic_driven: None, metric: str) -> None:
    value = _query_prom(metric)
    if value < 0:
        pytest.fail(f"metric {metric} query failed")
    # Count-type metrics should be > 0 after driven traffic; gauges (queue_depth)
    # may legitimately be 0, so treat zero as acceptable for gauges only.
    if metric.endswith("_total") or metric.endswith("_count"):
        if value <= 0:
            pytest.fail(f"metric {metric} has no samples")


def test_token_usage_positive(traffic_driven: None) -> None:
    if _query_prom("gen_ai_client_token_usage_total") <= 0:
        pytest.fail("gen_ai_client_token_usage_total should be positive")


def test_request_duration(traffic_driven: None) -> None:
    if _query_prom("jiuwenclaw_request_duration_seconds_count") <= 0:
        pytest.fail("jiuwenclaw_request_duration_seconds_count should be positive")


def test_request_count(traffic_driven: None) -> None:
    if _query_prom("jiuwenclaw_request_count_total") <= 0:
        pytest.fail("jiuwenclaw_request_count_total should be positive")


def test_request_error_count(traffic_driven: None) -> None:
    # Only verifies the metric is queryable — may legitimately be 0 if no
    # error traffic was driven.
    _query_prom("jiuwenclaw_request_error_count_total")


def test_tool_error_count(traffic_driven: None) -> None:
    """Requires the traffic driver to include one tool that raises."""
    if _query_prom("gen_ai_tool_error_count_total") <= 0:
        pytest.fail("gen_ai_tool_error_count_total should be positive")