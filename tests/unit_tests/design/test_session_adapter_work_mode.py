from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.gateway_adapter import session_adapter


@pytest.mark.asyncio
async def test_mode_filtered_session_list_scans_metadata_once(monkeypatch):
    calls: list[tuple[int, int]] = []

    def list_metadata(*, limit: int, offset: int):
        calls.append((limit, offset))
        return [
            {"session_id": "work", "work_mode": "work"},
            {"session_id": "design", "work_mode": "design"},
        ], 2

    monkeypatch.setattr(
        session_adapter,
        "get_all_sessions_metadata",
        list_metadata,
    )
    request = AgentRequest(
        request_id="request",
        channel_id="web",
        params={"work_mode": "design", "limit": 20, "offset": 0},
    )

    response = await session_adapter.SessionAdapter()._handle_list(request)

    assert response.ok is True
    assert response.payload is not None
    assert response.payload["total"] == 1
    assert [item["session_id"] for item in response.payload["sessions"]] == ["design"]
    assert calls == [(10**9, 0)]
