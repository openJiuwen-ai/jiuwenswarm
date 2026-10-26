"""Fresh-evidence gate for request/session/channel isolation and terminals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from scripts.e2e_telemetry_trace import (
    DEFAULT_EVIDENCE,
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


def _request_spans(evidence: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    return [
        span
        for span in evidence["spans"]
        if (span.get("attributes") or {}).get("jiuwenclaw.request.id") == request_id
    ]


def test_each_flow_keeps_request_session_and_channel_isolated(
    evidence: dict[str, Any],
) -> None:
    request_ids = [flow["request_id"] for flow in evidence["flows"]]
    assert len(request_ids) == len(set(request_ids))
    for flow in evidence["flows"]:
        spans = _request_spans(evidence, flow["request_id"])
        assert spans, f"no spans for request {flow['request_id']}"
        for span in spans:
            attrs = span["attributes"]
            assert attrs["jiuwenclaw.request.id"] == flow["request_id"]
            assert attrs["jiuwenclaw.session.id"] == flow["session_id"]
            assert attrs["jiuwenclaw.channel.id"] == flow["channel_id"]


def test_cancelled_flow_is_terminal_without_ending_other_requests(
    evidence: dict[str, Any],
) -> None:
    cancelled = [flow for flow in evidence["flows"] if flow.get("scenario") == "cancel"]
    assert cancelled, "no fresh cancellation flow"
    for flow in cancelled:
        assert flow["terminal_event"] == "chat.interrupt_result"
        spans = _request_spans(evidence, flow["request_id"])
        assert any(
            (span.get("attributes") or {}).get("jiuwenswarm.canceled") is True
            for span in spans
        )
    assert any(
        flow.get("scenario") == "success" and flow.get("terminal_event") != "chat.error"
        for flow in evidence["flows"]
    )


def test_low_threshold_stuck_metric_matches_live_session(
    evidence: dict[str, Any],
) -> None:
    stuck_flows = [
        flow for flow in evidence["flows"] if flow.get("scenario") == "stuck"
    ]
    assert stuck_flows, "no fresh low-threshold stuck flow"
    for flow in stuck_flows:
        points = [
            point
            for point in evidence["metrics"]
            if point["name"]
            in {
                "jiuwenclaw.session.stuck",
                "jiuwenclaw.session.stuck_age_ms",
            }
            and point["time_unix_nano"] >= flow["started_unix_nano"]
            and (point.get("attributes") or {}).get("jiuwenclaw.session.id")
            == flow["session_id"]
        ]
        assert {point["name"] for point in points} == {
            "jiuwenclaw.session.stuck",
            "jiuwenclaw.session.stuck_age_ms",
        }
