# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.gateway.config_poll.db import (
    list_table_records,
    row_snapshot,
    row_stamp,
)


def test_row_snapshot_uses_stable_updated_at_stamp() -> None:
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    snap_a = row_snapshot("logging_config", [{"updated_at": ts}])
    snap_b = row_snapshot("logging_config", [{"updated_at": ts.isoformat()}])
    assert snap_a == snap_b == {"default": ts.isoformat()}


def test_row_snapshot_detects_delete_and_update() -> None:
    ts1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    before = row_snapshot(
        "channel_config",
        [{"channel_id": "c1", "updated_at": ts1.isoformat()}],
    )
    after_update = row_snapshot(
        "channel_config",
        [{"channel_id": "c1", "updated_at": ts2.isoformat()}],
    )
    after_delete = row_snapshot("channel_config", [])
    assert before != after_update
    assert before != after_delete
    assert row_stamp({"updated_at": None}) == ""


@pytest.mark.asyncio
async def test_list_table_records_channel_config() -> None:
    list_mock = AsyncMock(return_value=[{"channel_id": "web", "status": "active"}])
    with patch(
        "jiuwenswarm.server.runtime.enterprise_config.db_queries.list_records",
        list_mock,
    ):
        rows = await list_table_records("channel_config")
    list_mock.assert_awaited_once_with("channel_config")
    assert rows == [{"channel_id": "web", "status": "active"}]


@pytest.mark.asyncio
async def test_list_table_records_logging_without_instance_filter() -> None:
    list_mock = AsyncMock(return_value=[{"level": "INFO"}])
    with patch(
        "jiuwenswarm.server.runtime.enterprise_config.db_queries.list_records",
        list_mock,
    ):
        rows = await list_table_records("logging_config")
    list_mock.assert_awaited_once_with("logging_config")
    assert rows == [{"level": "INFO"}]
