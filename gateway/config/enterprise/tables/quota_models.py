# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""配额相关表定义。"""

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

WORKSPACE_QUOTA_POLICY_TABLE_DEF = TableDefinition(
    table_name="workspace_quota_policy",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("policy_id", "string", length=64, nullable=False),
        ColumnDefinition("policy_name", "string", length=128, nullable=False, default=""),
        ColumnDefinition("policy_desc", "string", length=512, nullable=True),
        ColumnDefinition("match_expr", "json", nullable=True),
        ColumnDefinition("priority", "integer", nullable=False, default=0),
        ColumnDefinition("limit_bytes", "bigint", nullable=False),
        ColumnDefinition("soft_percent", "integer", nullable=False, default=80),
        ColumnDefinition("hard_percent", "integer", nullable=False, default=100),
        ColumnDefinition("source", "string", length=32, nullable=False, default="manual"),
        ColumnDefinition("source_order_num", "string", length=64, nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("created_by", "string", length=64, nullable=True),
        ColumnDefinition("updated_at", "datetime", nullable=False),
        ColumnDefinition("updated_by", "string", length=64, nullable=True),
    ],
    indexes=[
        IndexDefinition(["policy_id"], unique=True),
    ],
)

WORKSPACE_QUOTA_USAGE_TABLE_DEF = TableDefinition(
    table_name="workspace_quota_usage",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("user_id", "string", length=64, nullable=False),
        ColumnDefinition("group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("bot_id", "string", length=64, nullable=False),
        ColumnDefinition("used_bytes", "bigint", nullable=False),
        ColumnDefinition("reported_at", "datetime", nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("created_by", "string", length=64, nullable=True),
        ColumnDefinition("updated_at", "datetime", nullable=False),
        ColumnDefinition("updated_by", "string", length=64, nullable=True),
    ],
    indexes=[
        IndexDefinition(["user_id", "group_id", "bot_id"], unique=True),
    ],
)

QUOTA_TABLE_DEFINITIONS = (
    WORKSPACE_QUOTA_POLICY_TABLE_DEF,
    WORKSPACE_QUOTA_USAGE_TABLE_DEF,
)
