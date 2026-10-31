"""企业库存储、认领开关和启动时选存法。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from gateway.cron.db_schedule import (
    claim_periodic_run,
    datetime_to_db_value,
    should_use_db_schedule,
)
from gateway.cron.db_store import (
    GatewayDbCronJobStore,
    _row_to_job,
)
from gateway.cron.models import CronJob
from gateway.storage.access import (
    clear_persistent_store,
    set_persistent_store,
)
from gateway.runtime.cron.store import FileCronJobStore


class _MemoryPersistent:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def ensure_ready(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        del name
        for row in self.rows:
            if row.get("job_id") == key.get("job_id"):
                return dict(row)
        return None

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        del name
        self.rows.append(dict(record))
        return dict(record)

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        del name
        for row in self.rows:
            if row.get("job_id") == key.get("job_id"):
                row.update(updates)
                return dict(row)
        return None

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        del name
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.get("job_id") != key.get("job_id")]
        return len(self.rows) != before

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        del name, order_by, offset
        filters = filters or {}
        out = [
            dict(row)
            for row in self.rows
            if all(row.get(key) == value for key, value in filters.items())
        ]
        if limit is not None:
            out = out[:limit]
        return out


def _sample_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "job_id": "job-1",
        "group_id": "g1",
        "bot_id": "b1",
        "user_id": "u1",
        "name": "提醒",
        "description": "回复消息",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "wake_offset_seconds": 0,
        "enabled": 1,
        "expired": 0,
        "delete_after_run": 0,
        "mode": "agent",
        "targets": "web",
        "created_at": "2026-03-28T01:00:00+00:00",
        "updated_at": "2026-03-28T01:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_blank_db_identity_stays_empty():
    job = _row_to_job(_sample_row(group_id=None, bot_id="  ", user_id=None))
    assert job is not None
    assert (job.group_id, job.bot_id, job.user_id) == ("", "", "")
    row = GatewayDbCronJobStore._job_to_row(job)
    assert (row["group_id"], row["bot_id"], row["user_id"]) == ("", "", "")
    assert "service_id" not in row
    assert "agent_id" not in row


def test_explicit_default_identity_is_kept():
    job = _row_to_job(_sample_row(group_id="default", bot_id="default", user_id="default"))
    assert job is not None
    assert (job.group_id, job.bot_id, job.user_id) == ("default", "default", "default")


@pytest.fixture
def memory_store():
    store = _MemoryPersistent()
    set_persistent_store(store)
    yield store
    clear_persistent_store()


@pytest.mark.asyncio
async def test_db_store_create_same_job_id_is_idempotent(memory_store):
    store = GatewayDbCronJobStore()
    first = await store.create_job(
        job_id="fixed-id-1",
        name="早安1",
        cron_expr="0 0 8 * * ? *",
        timezone="Asia/Shanghai",
        description="你好",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="zxy",
    )
    second = await store.create_job(
        job_id="fixed-id-1",
        name="早安1-重试",
        cron_expr="0 0 8 * * ? *",
        timezone="Asia/Shanghai",
        description="你好",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="zxy",
    )
    assert second.id == first.id
    assert second.name == first.name
    assert len(memory_store.rows) == 1


@pytest.mark.asyncio
async def test_db_store_create_blank_identity_stays_empty(memory_store):
    store = GatewayDbCronJobStore()
    job = await store.create_job(
        name="空身份",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="提醒",
        targets="web",
    )
    assert (job.group_id, job.bot_id, job.user_id) == ("", "", "")
    assert memory_store.rows[0]["group_id"] == ""
    assert "service_id" not in memory_store.rows[0]

    saved = await store.get_job(job.id)
    assert saved is not None
    assert saved.group_id == ""


@pytest.mark.asyncio
async def test_db_store_update_keeps_identity(memory_store):
    store = GatewayDbCronJobStore()
    job = await store.create_job(
        name="锁定",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="提醒",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="u1",
    )
    updated = await store.update_job(
        job.id,
        {"name": "改名", "group_id": "g2", "user_id": "u2", "job_id": "other"},
    )
    assert updated.name == "改名"
    assert updated.id == job.id
    assert (updated.group_id, updated.bot_id, updated.user_id) == ("g1", "b1", "u1")


def test_enterprise_registry_registers_cron_job_table():
    from gateway.storage_assembly.layouts import build_gateway_store_registry

    registry = build_gateway_store_registry()
    layout = registry.get("cron_job")
    assert layout is not None
    assert layout.db is not None
    assert layout.db.table == "cron_job"
    assert layout.file is None


def test_should_use_db_schedule_follows_edition(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("GATEWAY_DB_HOST", "127.0.0.1")
    assert should_use_db_schedule() is False

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    assert should_use_db_schedule() is True


def test_datetime_to_db_value_is_naive_utc():
    aware = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)
    stored = datetime_to_db_value(aware)
    assert stored.tzinfo is None
    assert stored.hour == 1


@pytest.mark.asyncio
async def test_claim_periodic_run_uses_one_row_update(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_execute(sql: str, params: dict[str, Any]) -> int:
        captured["sql"] = sql
        captured["params"] = params
        return 1

    monkeypatch.setattr(
        "gateway.cron.db_schedule._execute_claim_sql",
        _fake_execute,
    )
    job = CronJob(
        id="job-1",
        name="周期",
        enabled=True,
        cron_expr="0 0 9 * * ? *",
        timezone="UTC",
        description="提醒",
        targets="web",
    )
    fire_at = datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc)
    ok = await claim_periodic_run(job, fire_at)
    assert ok is True
    assert "UPDATE cron_job" in captured["sql"]
    assert captured["params"]["job_id"] == "job-1"
    assert "service_id" not in captured["params"]


@pytest.mark.asyncio
async def test_gateway_factory_picks_store(monkeypatch):
    from gateway.cron.factory import create_gateway_cron_store

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    personal = await create_gateway_cron_store({"gateway": {}})
    assert isinstance(personal, FileCronJobStore)

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("GATEWAY_DB_HOST", "127.0.0.1")
    enterprise = await create_gateway_cron_store(
        {"gateway": {"cron": {"store_backend": "etcd"}}}
    )
    assert isinstance(enterprise, GatewayDbCronJobStore)

    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    with pytest.raises(RuntimeError, match="数据库地址"):
        await create_gateway_cron_store({"gateway": {"cron": {"store_backend": "file"}}})
