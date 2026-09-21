# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP contract tests for session-complete trajectory usage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.gateway.channel_manager.web.trajectory_http import TrajectoryHttpService
from jiuwenswarm.observability.config import TrajectoryStoreSettings


class _UsageReader:
    async def get_session_request_usage(self, session_id: str):
        return ([{
            "trace_id": "1" * 32,
            "inference_id": "inference-1",
            "subject_id": "main",
            "start_time_unix_nano": "10",
            "usage": {"input": 2, "total": 3},
            "cumulative_usage": {"input": 5, "total": 8},
        }], "epoch-1")


@pytest.mark.asyncio
async def test_http_exposes_session_scoped_request_usage(tmp_path: Path) -> None:
    settings = TrajectoryStoreSettings(
        enabled=True,
        database_path=tmp_path,
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=20,
        poll_interval_ms=2000,
    )
    service = TrajectoryHttpService(
        settings,
        reader=_UsageReader(),
        metadata_loader=lambda _session_id: {
            "mode": "agent.work.normal",
            "team_name": "",
        },
    )

    response = await service.get_session_usage("session-1")
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["scope"] == "session"
    assert payload["items"][0]["cumulative_usage"] == {"input": 5, "total": 8}
