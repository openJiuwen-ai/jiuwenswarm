# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.db_store import GatewayDbCronJobStore
from jiuwenswarm.gateway.storage.access import (
    clear_persistent_store,
    set_persistent_store,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend


@pytest.mark.asyncio
async def test_enterprise_cron_crud_via_persistent_store() -> None:
    store = InMemoryPersistentBackend()
    set_persistent_store(store)
    cron = GatewayDbCronJobStore()
    try:
        created = await cron.create_job(
            name="n1",
            cron_expr="*/5 * * * *",
            timezone="Asia/Shanghai",
            description="d",
            targets="web",
        )
        assert created.id
        got = await cron.get_job(created.id)
        assert got is not None
        assert got.name == "n1"

        updated = await cron.update_job(created.id, {"name": "n2"})
        assert updated.name == "n2"

        jobs = await cron.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].name == "n2"

        assert await cron.delete_job(created.id) is True
        assert await cron.get_job(created.id) is None
    finally:
        clear_persistent_store()
