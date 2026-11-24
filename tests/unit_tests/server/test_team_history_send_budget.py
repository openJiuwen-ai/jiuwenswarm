# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for #6859: team-history pages must fit the WebSocket budget.

The team-history page is assembled against ``max_bytes - FRAME_OVERHEAD``, but
the final wire payload grows again when ``encode_agent_response_for_wire()``
adds its envelope.  With the page budget set equal to the WebSocket send budget
(6 MiB) and only 1 KiB of frame overhead, long team sessions crossed the limit
and ``send_wire_payload()`` replaced the whole page with an error frame.
"""

from __future__ import annotations

import json

from agent_gateway.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES

from jiuwenswarm.server.wire_truncate import (
    _TEAM_HISTORY_FRAME_OVERHEAD_BYTES,
    _TEAM_HISTORY_MAX_MAX_BYTES,
    _TEAM_HISTORY_MIN_MAX_BYTES,
    _json_wire_size,
    _select_history_record_page,
)


def test_team_history_max_budget_leaves_envelope_headroom() -> None:
    """A fully-populated page must stay below the send budget with room for E2A envelope fields."""
    # At least 1 MiB of headroom above the largest allowed page for the envelope.
    assert AGENT_WS_SEND_BUDGET_BYTES - _TEAM_HISTORY_MAX_MAX_BYTES >= 1024 * 1024
    # Frame overhead must absorb real envelope growth, not a token 1 KiB.
    assert _TEAM_HISTORY_FRAME_OVERHEAD_BYTES >= 32 * 1024


def test_max_budget_page_stays_within_wire_budget() -> None:
    """End-to-end: a page filled to MAX_MAX_BYTES still fits after envelope encoding."""
    # One 32 KiB record each; enough of them to fill the page budget.
    blob = "x" * (32 * 1024)
    records = [
        {
            "id": f"record-{idx}",
            "role": "teammate",
            "event_type": "chat.tool_result",
            "content": blob,
        }
        for idx in range(500)
    ]

    page, next_cursor = _select_history_record_page(
        records,
        cursor=0,
        limit=1000,
        max_bytes=_TEAM_HISTORY_MAX_MAX_BYTES,
        session_id="sess-budget",
    )
    assert page, "page should contain records"
    assert next_cursor > 0

    # Simulate the envelope that encode_agent_response_for_wire() adds.
    wire = {
        "request_id": "req-budget",
        "response_id": "resp-budget",
        "sequence": 1,
        "agent_ref": "agent-1",
        "metadata": {},
        "provenance": {},
        "timestamp": 0,
        "status": "ok",
        "response_kind": "sync",
        "payload": {
            "records": page,
            "session_id": "sess-budget",
            "has_more": next_cursor < len(records),
            "next_cursor": next_cursor,
            "total": len(records),
        },
    }
    encoded_size = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
    assert encoded_size <= AGENT_WS_SEND_BUDGET_BYTES


# --- !10766 review: small max_bytes must not be pinned to the minimum budget ---


def _uniform_records(count: int = 400) -> list[dict[str, object]]:
    """Build a stable set of uniformly-sized records for budget comparisons."""
    return [
        {
            "id": f"r-{idx:04d}",
            "role": "teammate",
            "event_type": "chat.tool_result",
            "content": "y" * 40,
        }
        for idx in range(count)
    ]


def _page_wire_size(max_bytes: int) -> int:
    page, _next = _select_history_record_page(
        _uniform_records(),
        cursor=0,
        limit=1000,
        max_bytes=max_bytes,
        session_id="sess",
    )
    return _json_wire_size({"records": page, "session_id": "sess"})


def test_small_max_bytes_is_not_pinned_to_minimum_budget() -> None:
    """A mid-range max_bytes must yield a proportionally larger page.

    The frame overhead is a cap (min(FRAME_OVERHEAD, max_bytes // 4)), not a flat
    deduction.  With the old flat 64 KiB deduction, every max_bytes in
    [MIN_MAX_BYTES, MIN_MAX_BYTES + 64 KiB] collapsed to the 2 KiB minimum and
    records >2 KiB were force-collapsed, costing many extra round-trips.  A
    32 KiB request must now fit several times more content than the 2 KiB floor.
    """
    tiny = _page_wire_size(_TEAM_HISTORY_MIN_MAX_BYTES)
    mid = _page_wire_size(32 * 1024)
    assert mid > tiny * 3, (
        f"32 KiB page ({mid} B) should be far larger than the 2 KiB floor "
        f"({tiny} B); got pinned to the minimum budget"
    )


def test_small_max_bytes_budget_scales_monotonically() -> None:
    """Across the affected range the page budget grows with max_bytes."""
    budgets = [4 * 1024, 16 * 1024, 32 * 1024, 64 * 1024]
    sizes = [_page_wire_size(mb) for mb in budgets]
    assert sizes == sorted(sizes), (
        f"page size should grow monotonically with max_bytes, got {sizes}"
    )
    # The 64 KiB request must be able to use well beyond the 2 KiB floor.
    assert sizes[-1] > _TEAM_HISTORY_MIN_MAX_BYTES * 4


def test_large_request_still_reserves_full_frame_overhead() -> None:
    """#6859 protection is intact: large pages still deduct the full 64 KiB."""
    blob = "x" * (32 * 1024)
    records = [
        {
            "id": f"r-{idx}",
            "role": "teammate",
            "event_type": "chat.tool_result",
            "content": blob,
        }
        for idx in range(300)
    ]
    page, _next = _select_history_record_page(
        records,
        cursor=0,
        limit=1000,
        max_bytes=_TEAM_HISTORY_MAX_MAX_BYTES,
        session_id="sess",
    )
    page_wire = _json_wire_size({"records": page, "session_id": "sess"})
    # The full overhead cap is deducted from a max-budget request.
    assert (
        _TEAM_HISTORY_MAX_MAX_BYTES - page_wire
        >= _TEAM_HISTORY_FRAME_OVERHEAD_BYTES
    )
    # And that page, once enveloped, still leaves >= 1 MiB under the wire budget
    # (the core #6859 contract).
    assert AGENT_WS_SEND_BUDGET_BYTES - _TEAM_HISTORY_MAX_MAX_BYTES >= 1024 * 1024
