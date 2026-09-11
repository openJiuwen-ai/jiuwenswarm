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
        assert result["total"] == 2
        assert [item["dispatch_id"] for item in result["items"]] == ["a2"]
    finally:
        await handler.disconnect()
