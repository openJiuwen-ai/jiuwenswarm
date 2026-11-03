# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""config_poll 用 revision 发现变更；写入由数据库自增。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# 包入口会加载 scheduler。这里只取快照函数，避免把调度依赖带进来。
_db_path = (
    Path(__file__).resolve().parents[3] / "gateway/config_poll/db.py"
)
_spec = importlib.util.spec_from_file_location("config_poll_db_under_test", _db_path)
assert _spec is not None and _spec.loader is not None
_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_db)
row_snapshot = _db.row_snapshot
from gateway.storage.backends.db.persistent_store import DbPersistentBackend
from gateway.storage.revision import RevisionConflictError
from gateway.storage.registry.store_registry import (
    DbLayout,
    StoreLayout,
    StoreRegistry,
)
from openjiuwen_runtime.foundation.db.sqlite_handler import SQLiteHandler
from openjiuwen_runtime.foundation.db.table_def import ColumnDefinition, TableDefinition


@pytest.fixture(autouse=True)
def _isolated_runtime_orm_registry(monkeypatch):
    # Each case creates an independent database and may declare a different
    # schema under the same table name. Do not reuse Runtime's process registry.
    from sqlalchemy.orm import DeclarativeBase
    from openjiuwen_runtime.foundation.db import sqlalchemy_handler

    class TestBase(DeclarativeBase):
        pass

    monkeypatch.setattr(sqlalchemy_handler, "Base", TestBase)
    yield
    TestBase.registry.dispose()
    TestBase.metadata.clear()


def test_snapshot_ignores_updated_at_and_sees_revision_or_new_id() -> None:
    original = {
        "channel_id": "web",
        "id": 3,
        "revision": 5,
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    same_version_other_clock = dict(original, updated_at="2030-06-01T00:00:00+00:00")
    assert row_snapshot("channel_config", [original]) == row_snapshot(
        "channel_config", [same_version_other_clock]
    )

    bumped = dict(original, revision=6)
    assert row_snapshot("channel_config", [bumped]) != row_snapshot(
        "channel_config", [original]
    )

    # 同一 channel_id 删掉再建：revision 回到 1，但自增 id 变了。
    recreated = dict(original, id=4, revision=1)
    assert row_snapshot("channel_config", [recreated]) != row_snapshot(
        "channel_config", [original]
    )


class _HandlerConnection:
    def __init__(self, handler: SQLiteHandler) -> None:
        self._handler = handler

    async def ensure_ready(self) -> SQLiteHandler:
        return self._handler

    async def close(self) -> None:
        await self._handler.disconnect()


@pytest.mark.asyncio
async def test_update_increments_revision_in_the_database(tmp_path) -> None:
    handler = SQLiteHandler(str(tmp_path / "revision.db"))
    await handler.init_database()
    await handler.connect()
    await handler.init_table(
        TableDefinition(
            table_name="logging_config",
            columns=[
                ColumnDefinition(
                    "id",
                    "integer",
                    primary_key=True,
                    autoincrement=True,
                    nullable=False,
                ),
                ColumnDefinition("level", "string", length=16, nullable=False),
                ColumnDefinition("revision", "integer", nullable=False, default=1),
            ],
        )
    )
    registry = StoreRegistry()
    registry.register(
        "logging_config",
        StoreLayout(db=DbLayout(table="logging_config")),
    )
    store = DbPersistentBackend(_HandlerConnection(handler), registry)

    created = await store.create("logging_config", {"level": "INFO"})
    assert created["revision"] == 1
    row_id = created["id"]

    first = await store.update(
        "logging_config",
        {"id": row_id},
        {"level": "DEBUG", "revision": 1},
    )
    assert first is not None and first["revision"] == 2
    assert first["level"] == "DEBUG"

    with pytest.raises(RevisionConflictError):
        await store.update(
            "logging_config",
            {"id": row_id},
            {"level": "WARNING", "revision": 1},
        )
    current = await store.get("logging_config", {"id": row_id})
    assert current is not None
    assert current["level"] == "DEBUG"
    assert current["revision"] == 2

    second = await store.update(
        "logging_config",
        {"id": row_id},
        {"level": "WARNING", "revision": current["revision"]},
    )
    assert second is not None and second["revision"] == 3
    assert second["level"] == "WARNING"

    missing = await store.update(
        "logging_config",
        {"id": 999},
        {"level": "INFO"},
    )
    assert missing is None
    with pytest.raises(RevisionConflictError):
        await store.update(
            "logging_config",
            {"id": row_id},
            {"level": "ERROR"},
        )
    unchanged = await store.get("logging_config", {"id": row_id})
    assert unchanged is not None
    assert unchanged["level"] == "WARNING"
    assert unchanged["revision"] == 3

    await handler.disconnect()


@pytest.mark.asyncio
async def test_unchanged_update_still_returns_the_row(tmp_path) -> None:
    """SQLite 对值没变的 UPDATE 报 rowcount 0，不能当成行不存在。"""
    handler = SQLiteHandler(str(tmp_path / "unchanged.db"))
    await handler.init_database()
    await handler.connect()
    await handler.init_table(
        TableDefinition(
            table_name="cron_policy",
            columns=[
                ColumnDefinition(
                    "id",
                    "integer",
                    primary_key=True,
                    autoincrement=True,
                    nullable=False,
                ),
                ColumnDefinition("max_jobs_per_user", "integer", nullable=False),
            ],
        )
    )
    registry = StoreRegistry()
    registry.register(
        "cron_policy",
        StoreLayout(db=DbLayout(table="cron_policy")),
    )
    store = DbPersistentBackend(_HandlerConnection(handler), registry)
    created = await store.create("cron_policy", {"max_jobs_per_user": 5})
    updated = await store.update(
        "cron_policy",
        {"id": created["id"]},
        {"max_jobs_per_user": 5},
    )
    assert updated is not None
    assert updated["id"] == created["id"]
    assert updated["max_jobs_per_user"] == 5
    assert len(await store.list("cron_policy")) == 1
    await handler.disconnect()


@pytest.mark.asyncio
async def test_channel_merge_creates_missing_row_then_keeps_other_fields(tmp_path) -> None:
    from gateway.config.channel.codec import DbRowChannelCodec
    from gateway.config.channel.repository import ChannelConfigRepository

    handler = SQLiteHandler(str(tmp_path / "channel.db"))
    await handler.init_database()
    await handler.connect()
    await handler.init_table(
        TableDefinition(
            table_name="channel_config",
            columns=[
                ColumnDefinition(
                    "id",
                    "integer",
                    primary_key=True,
                    autoincrement=True,
                    nullable=False,
                ),
                ColumnDefinition("channel_id", "string", length=128, nullable=False),
                ColumnDefinition("channel_name", "string", length=128, nullable=False),
                ColumnDefinition("channel_type", "string", length=64, nullable=False),
                ColumnDefinition("bot_id", "string", length=128, nullable=False),
                ColumnDefinition("config", "json", nullable=False),
                ColumnDefinition("status", "string", length=32, nullable=False),
                ColumnDefinition("revision", "integer", nullable=False, default=1),
            ],
        )
    )
    registry = StoreRegistry()
    registry.register(
        "channel_config",
        StoreLayout(db=DbLayout(table="channel_config")),
    )
    store = DbPersistentBackend(_HandlerConnection(handler), registry)
    repo = ChannelConfigRepository(store, DbRowChannelCodec())

    created = await repo.merge_body("web", {"send_file_allowed": True})
    assert created.revision == 1
    assert created.body["send_file_allowed"] is True

    merged = await repo.merge_body("web", {"enabled": True})
    assert merged.revision == 2
    assert merged.body["send_file_allowed"] is True
    assert merged.body["enabled"] is True
    await handler.disconnect()


class _HideFirstList:
    """第一次 list 假装还没有行，用来覆盖「插入和更新撞在一起」。"""

    def __init__(self, inner: DbPersistentBackend) -> None:
        self._inner = inner
        self.lists = 0

    async def list(self, name: str, **kwargs: object) -> list[dict]:
        self.lists += 1
        if self.lists == 1:
            return []
        return await self._inner.list(name, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_logging_merge_retries_when_row_appears_before_update(tmp_path) -> None:
    from gateway.config.logging.repository import (
        LOGGING_LEVEL_FIELDS,
        LoggingConfigRepository,
    )
    from gateway.config.section import DbFlatSectionCodec

    handler = SQLiteHandler(str(tmp_path / "logging-race.db"))
    await handler.init_database()
    await handler.connect()
    await handler.init_table(
        TableDefinition(
            table_name="logging_config",
            columns=[
                ColumnDefinition(
                    "id",
                    "integer",
                    primary_key=True,
                    autoincrement=True,
                    nullable=False,
                ),
                ColumnDefinition("level", "string", length=16, nullable=False),
                ColumnDefinition("console_level", "string", length=16, nullable=True),
                ColumnDefinition("revision", "integer", nullable=False, default=1),
                ColumnDefinition("updated_at", "datetime", nullable=True),
            ],
        )
    )
    registry = StoreRegistry()
    registry.register(
        "logging_config",
        StoreLayout(db=DbLayout(table="logging_config")),
    )
    store = DbPersistentBackend(_HandlerConnection(handler), registry)
    await store.create(
        "logging_config",
        {"level": "INFO", "console_level": "WARNING"},
    )
    repo = LoggingConfigRepository(
        _HideFirstList(store),
        DbFlatSectionCodec(LOGGING_LEVEL_FIELDS),
    )
    try:
        saved = await repo.merge_levels({"level": "DEBUG"})
        assert saved.body["level"] == "DEBUG"
        assert saved.body["console_level"] == "WARNING"
        current = await store.get("logging_config", {})
        assert current is not None
        assert current["revision"] == 2
    finally:
        await handler.disconnect()
