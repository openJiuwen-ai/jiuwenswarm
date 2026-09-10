# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

LOG_MASKING_RULE_TABLE_DEF = TableDefinition(
    table_name="log_masking_rule",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("rule_id", "string", length=64, nullable=False),
        ColumnDefinition("rule_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("pattern", "string", length=512, nullable=False),
        ColumnDefinition(
            "replacement",
            "string",
            length=64,
            nullable=False,
            default="******",
        ),
        ColumnDefinition("priority", "integer", nullable=False),
        ColumnDefinition(
            "with_fingerprint",
            "boolean",
            nullable=False,
            default=False,
        ),
        ColumnDefinition("source", "string", length=16, nullable=False),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["rule_id"], unique=True),
    ],
)

LOGGING_CONFIG_TABLE_DEF = TableDefinition(
    table_name="logging_config",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("level", "string", length=16, nullable=False, default="INFO"),
        ColumnDefinition("console_level", "string", length=16, nullable=True),
        ColumnDefinition("gateway", "string", length=16, nullable=True),
        ColumnDefinition("channel", "string", length=16, nullable=True),
        ColumnDefinition("agent_server", "string", length=16, nullable=True),
        ColumnDefinition("full", "string", length=16, nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

TASK_MEMORY_CONFIG_TABLE_DEF = TableDefinition(
    table_name="task_memory_config",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("enabled", "boolean", nullable=False, default=False),
        ColumnDefinition("llm_model", "string", length=256, nullable=True),
        ColumnDefinition("embedding_model", "string", length=256, nullable=True),
        ColumnDefinition("api_key", "string", length=512, nullable=True),
        ColumnDefinition("api_base", "string", length=1024, nullable=True),
        ColumnDefinition("retrieval_algo", "string", length=64, nullable=True),
        ColumnDefinition("summary_algo", "string", length=64, nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

MEMORY_CONFIG_TABLE_DEF = TableDefinition(
    table_name="memory_config",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("body", "json", nullable=True),
        ColumnDefinition("source", "string", length=16, nullable=False, default="manager"),
        ColumnDefinition("revision", "integer", nullable=False, default=1),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)
