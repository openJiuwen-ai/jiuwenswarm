# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Team quota tables in the Gateway MySQL database.

``team_quota_org_account`` is only the mutex row for an organization's team
count. Occupancy stays in ``team_quota_lease``.
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

TEAM_QUOTA_POLICY_TABLE_DEF = TableDefinition(
    table_name="team_quota_policy",
    columns=[
        ColumnDefinition("policy_id", "string", length=64, primary_key=True, nullable=False),
        ColumnDefinition("policy_name", "string", length=128, nullable=False, default=""),
        ColumnDefinition("policy_desc", "string", length=512, nullable=True),
        ColumnDefinition("match_expr", "json", nullable=True),
        ColumnDefinition("priority", "integer", nullable=False, default=0),
        ColumnDefinition("max_teams_per_user", "integer", nullable=False, default=5),
        ColumnDefinition("max_teammates_per_team", "integer", nullable=False, default=5),
        ColumnDefinition("max_teams_per_org", "integer", nullable=False, default=0),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("source", "string", length=32, nullable=False, default="manual"),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("created_by", "string", length=64, nullable=True),
        ColumnDefinition("updated_at", "datetime", nullable=False),
        ColumnDefinition("updated_by", "string", length=64, nullable=True),
    ],
    indexes=[
        IndexDefinition(["enabled", "priority"], name="idx_team_quota_policy_pri"),
    ],
)

TEAM_QUOTA_ACCOUNT_TABLE_DEF = TableDefinition(
    table_name="team_quota_account",
    columns=[
        ColumnDefinition("id", "integer", primary_key=True, autoincrement=True, nullable=False),
        ColumnDefinition("user_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("bot_id", "string", length=256, nullable=False, default=""),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
    ],
    indexes=[
        IndexDefinition(
            ["user_id", "group_id", "bot_id"],
            unique=True,
            name="uq_team_quota_account_owner",
        ),
    ],
)

TEAM_QUOTA_ORG_ACCOUNT_TABLE_DEF = TableDefinition(
    table_name="team_quota_org_account",
    columns=[
        ColumnDefinition("group_id", "string", length=64, primary_key=True, nullable=False),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
    ],
)

TEAM_QUOTA_LEASE_TABLE_DEF = TableDefinition(
    table_name="team_quota_lease",
    columns=[
        ColumnDefinition("lease_id", "string", length=64, primary_key=True, nullable=False),
        ColumnDefinition("kind", "string", length=32, nullable=False),
        ColumnDefinition("idempotency_key", "string", length=128, nullable=False),
        ColumnDefinition("user_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("bot_id", "string", length=256, nullable=False, default=""),
        ColumnDefinition("team_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("member_name", "string", length=128, nullable=False, default=""),
        ColumnDefinition("status", "string", length=16, nullable=False),
        ColumnDefinition("expires_at", "double", nullable=True),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
    ],
    indexes=[
        IndexDefinition(
            ["kind", "idempotency_key"],
            unique=True,
            name="uq_team_quota_lease_idemp",
        ),
        IndexDefinition(
            ["kind", "user_id", "group_id", "bot_id", "status"],
            name="idx_team_quota_lease_owner",
        ),
        IndexDefinition(["kind", "team_id", "status"], name="idx_team_quota_lease_team"),
        IndexDefinition(["kind", "group_id", "status"], name="idx_team_quota_lease_org"),
    ],
)

TEAM_QUOTA_TABLES: tuple[TableDefinition, ...] = (
    TEAM_QUOTA_POLICY_TABLE_DEF,
    TEAM_QUOTA_ACCOUNT_TABLE_DEF,
    TEAM_QUOTA_ORG_ACCOUNT_TABLE_DEF,
    TEAM_QUOTA_LEASE_TABLE_DEF,
)


async def ensure_team_quota_org_column(engine: AsyncEngine) -> None:
    """Add ``max_teams_per_org`` and the org lease index on databases created earlier."""
    table_name = TEAM_QUOTA_POLICY_TABLE_DEF.table_name

    def upgrade(connection):
        inspector = inspect(connection)
        quote = connection.dialect.identifier_preparer.quote
        if inspector.has_table(table_name):
            cols = {col["name"] for col in inspector.get_columns(table_name)}
            if "max_teams_per_org" not in cols:
                connection.execute(text(
                    f"ALTER TABLE {quote(table_name)} "
                    f"ADD COLUMN {quote('max_teams_per_org')} INTEGER NOT NULL DEFAULT 0"
                ))
        lease = TEAM_QUOTA_LEASE_TABLE_DEF.table_name
        if inspector.has_table(lease):
            names = {idx.get("name") for idx in inspector.get_indexes(lease)}
            if "idx_team_quota_lease_org" not in names:
                connection.execute(text(
                    f"CREATE INDEX {quote('idx_team_quota_lease_org')} "
                    f"ON {quote(lease)} (kind, group_id, status)"
                ))

    def ready(connection):
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return True
        cols = {col["name"] for col in inspector.get_columns(table_name)}
        return "max_teams_per_org" in cols

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    except DBAPIError as exc:
        async with engine.connect() as connection:
            if not await connection.run_sync(ready):
                raise exc
