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
