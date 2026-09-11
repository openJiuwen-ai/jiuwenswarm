"""Exercise real SQLite schema upgrades without assigning legacy ownership."""

from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import inspect, text
from openjiuwen_runtime.foundation.db.sqlite_handler import SQLiteHandler

from jiuwenswarm.gateway.config.enterprise.tables.a2a_models import (
    A2A_OUTBOUND_DISPATCH_TABLE_DEF,
)
from jiuwenswarm.gateway.config.enterprise.tables.table_init import init_all_tables
from jiuwenswarm.gateway.config.enterprise.tables import table_init
from jiuwenswarm.gateway.a2a_manager.outbound.registry import A2AOutboundRegistry
from jiuwenswarm.gateway.storage.backends.db.persistent_store import DbPersistentBackend
from jiuwenswarm.gateway.storage_assembly import (
    build_gateway_store_registry,
    create_a2a_outbound_repository,
)


@pytest.mark.asyncio
@pytest.mark.filterwarnings(
    "ignore:This declarative base already contains a class:sqlalchemy.exc.SAWarning"
)
@pytest.mark.parametrize("legacy", [True, False])
async def test_a2a_schema_upgrade_is_idempotent_and_does_not_claim_history(
    tmp_path, monkeypatch, legacy
):
    handler = SQLiteHandler(str(tmp_path / "gateway.db"))
    await handler.connect()
    table = A2A_OUTBOUND_DISPATCH_TABLE_DEF
    monkeypatch.setattr(table_init, "ALL_TABLE_DEFINITIONS", (table,))
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    try:
        if legacy:
            old = replace(
                table,
                columns=[col for col in table.columns if col.name != "source_user_id"],
                indexes=[
                    idx for idx in table.indexes if "source_user_id" not in idx.columns
                ],
            )
            await handler.init_table(old)
            await handler.create(
                table.table_name,
                {
                    "dispatch_id": "legacy",
                    "agent_id": "agent",
                    "agent_revision": 1,
                    "mode": "async",
                    "status": "completed",
                    "request_message_id": "request",
                    "source_session_id": "session",
                    "created_at": datetime(2026, 9, 1),
                    "updated_at": datetime(2026, 9, 1),
                },
            )
        await init_all_tables(handler)
        await init_all_tables(handler)
        async with handler.get_engine().connect() as connection:
            columns = await connection.run_sync(
                lambda c: inspect(c).get_columns(table.table_name)
            )
            owner = next(col for col in columns if col["name"] == "source_user_id")
            assert owner["nullable"] is True
            indexes = await connection.run_sync(
                lambda c: inspect(c).get_indexes(table.table_name)
            )
            assert (
                sum(
                    idx["column_names"] == ["source_user_id", "created_at"]
                    for idx in indexes
                )
                == 1
            )
            if legacy:
                result = await connection.execute(
                    text(
                        "SELECT source_user_id FROM a2a_outbound_dispatch WHERE dispatch_id='legacy'"
                    )
                )
                assert result.one() == (None,)

        # Verify the actual database filter, not just the in-memory backend.
        class Connection:
            async def ensure_ready(self):
                return handler

        store = DbPersistentBackend(Connection(), build_gateway_store_registry())
        repository = create_a2a_outbound_repository(store)
        for name, user, day in [
            ("a1", "alice", 1),
            ("a2", "alice", 2),
            ("b1", "bob", 3),
            ("long", "u" * 256, 4),
        ]:
            await handler.create(
                table.table_name,
                {
                    "dispatch_id": name,
                    "agent_id": "agent",
                    "agent_revision": 1,
                    "mode": "async",
                    "status": "completed",
                    "request_message_id": name,
                    "source_session_id": "session",
                    "source_user_id": user,
                    "created_at": datetime(2026, 9, day),
                    "updated_at": datetime(2026, 9, day),
                },
            )
        result = await A2AOutboundRegistry(repository).list_dispatches(
            limit=1, source_user_id="alice"
        )
        long_result = await A2AOutboundRegistry(repository).list_dispatches(
            limit=1, source_user_id="u" * 256
        )
        assert long_result["total"] == 1
        assert long_result["items"][0]["dispatch_id"] == "long"
        assert result["total"] == 2
        assert [item["dispatch_id"] for item in result["items"]] == ["a2"]
    finally:
        await handler.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect_name,length,expected", [
    ("postgresql", 64, "ALTER COLUMN source_user_id TYPE VARCHAR(256)"),
    ("mysql", 64, "MODIFY COLUMN source_user_id VARCHAR(256) NULL"),
    ("postgresql", 256, None), ("mysql", 512, None), ("sqlite", 64, None),
])
async def test_existing_owner_column_widening(monkeypatch, dialect_name, length, expected):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy import String
    from sqlalchemy.dialects import mysql, postgresql, sqlite
    from jiuwenswarm.gateway.config.enterprise.tables import a2a_migration

    dialect = {"mysql": mysql, "postgresql": postgresql, "sqlite": sqlite}[dialect_name].dialect()
    owner = {"name": "source_user_id", "type": String(length)}
    statements = []
    inspector = SimpleNamespace(has_table=lambda name: True, get_columns=lambda name: [owner])
    monkeypatch.setattr(a2a_migration, "inspect", lambda connection: inspector)

    class Connection:
        async def run_sync(self, callback):
            return callback(self)

        def execute(self, statement):
            statements.append(str(statement))
            owner["type"] = String(256)

    connection = Connection()
    connection.dialect = dialect

    @asynccontextmanager
    async def begin():
        yield connection

    engine = SimpleNamespace(begin=begin)
    await a2a_migration.ensure_dispatch_user_column(engine)
    await a2a_migration.ensure_dispatch_user_column(engine)
    assert statements == ([] if expected is None else [f"ALTER TABLE a2a_outbound_dispatch {expected}"])


@pytest.mark.asyncio
@pytest.mark.parametrize("final_length", [64, 256])
async def test_failed_widening_requires_verified_final_length(monkeypatch, final_length):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy import String
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.exc import DBAPIError
    from jiuwenswarm.gateway.config.enterprise.tables import a2a_migration

    owner = {"name": "source_user_id", "type": String(64)}
    events = []
    failure = DBAPIError("ALTER TABLE", {}, Exception("DDL failed"))
    inspector = SimpleNamespace(has_table=lambda name: True, get_columns=lambda name: [owner])
    monkeypatch.setattr(a2a_migration, "inspect", lambda connection: inspector)

    class Connection:
        dialect = postgresql.dialect()

        async def run_sync(self, callback):
            return callback(self)

        def execute(self, statement):
            raise failure

    @asynccontextmanager
    async def begin():
        try:
            yield Connection()
        finally:
            events.append("rollback")
            owner["type"] = String(final_length)

    @asynccontextmanager
    async def connect():
        events.append("verify")
        yield Connection()

    engine = SimpleNamespace(begin=begin, connect=connect)
    if final_length == 256:
        await a2a_migration.ensure_dispatch_user_column(engine)
    else:
        with pytest.raises(DBAPIError) as caught:
            await a2a_migration.ensure_dispatch_user_column(engine)
        assert caught.value is failure
    assert events == ["rollback", "verify"]
