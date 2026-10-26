# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.gateway.config_poll.db import list_table_records
from jiuwenswarm.gateway.storage.access import (
    clear_persistent_store,
    set_persistent_store,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend


@pytest.mark.asyncio
async def test_list_table_records_via_persistent_store() -> None:
    store = InMemoryPersistentBackend()
    await store.create(
        "logging_config",
        {
            "level": "INFO",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )
    set_persistent_store(store)
    try:
        rows = await list_table_records("logging_config")
        assert len(rows) == 1
        assert rows[0]["level"] == "INFO"
        assert await list_table_records("unknown_table") == []
    finally:
        clear_persistent_store()


@pytest.mark.asyncio
async def test_list_table_records_without_store() -> None:
    clear_persistent_store()
    with patch(
        "jiuwenswarm.server.runtime.enterprise_config.db_queries.list_records",
        new=AsyncMock(return_value=[]),
    ):
        assert await list_table_records("logging_config") == []
