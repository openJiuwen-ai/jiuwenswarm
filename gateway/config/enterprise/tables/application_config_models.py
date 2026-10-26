# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

CHANNEL_CONFIG_TABLE_DEF = TableDefinition(
    table_name="channel_config",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        # 业务主键；与 DbRowChannelCodec / config_poll.row_key 对齐
        ColumnDefinition("channel_id", "string", length=128, nullable=False),
        ColumnDefinition("channel_name", "string", length=128, nullable=False),
        ColumnDefinition("channel_type", "string", length=64, nullable=False),
        ColumnDefinition(
            "bot_id",
            "string",
            length=128,
            nullable=False,
            default="",
        ),
        # ChannelManager 使用的 body（enabled / apps / send_file_allowed 等）
        ColumnDefinition("config", "json", nullable=False),
        ColumnDefinition(
            "status",
            "string",
            length=32,
            nullable=False,
            default="active",
        ),
        ColumnDefinition("revision", "integer", nullable=False, default=1),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["channel_id"], unique=True),
    ],
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
        ColumnDefinition("revision", "integer", nullable=False, default=1),
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
        ColumnDefinition("revision", "integer", nullable=False, default=1),
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

PACKAGE_ALLOWLIST_TABLE_DEF = TableDefinition(
    table_name="package_allowlist",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("packages", "json", nullable=False),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

CRON_POLICY_TABLE_DEF = TableDefinition(
    table_name="cron_policy",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        # 界面允许同时处于运行中的定时任务数。0 表示不能再从界面启用新任务。
        ColumnDefinition("max_jobs_per_user", "integer", nullable=False, default=5),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

AUDIT_LOG_CONFIG_TABLE_DEF = TableDefinition(
    table_name="audit_log_config",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("schema_version", "string", length=32, nullable=False, default="1.0.0"),
        ColumnDefinition("otel_enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("otel_endpoint", "string", length=512, nullable=True),
        ColumnDefinition("otel_protocol", "string", length=16, nullable=False, default="grpc"),
        ColumnDefinition("data_center", "string", length=32, nullable=False, default="N"),
        ColumnDefinition("system_code", "string", length=64, nullable=False, default="-"),
        ColumnDefinition("node", "string", length=128, nullable=True),
        ColumnDefinition("ntp_servers", "json", nullable=False),
        ColumnDefinition(
            "ntp_sync_interval",
            "string",
            length=32,
            nullable=False,
            default="300s",
        ),
        ColumnDefinition("ntp_max_offset_ms", "integer", nullable=False, default=500),
        ColumnDefinition("ntp_failover", "boolean", nullable=False, default=True),
        ColumnDefinition("body", "json", nullable=False),
        ColumnDefinition("source", "string", length=16, nullable=False, default="manager"),
        # 变更号：更新时由数据库自增，config_poll 用来发现这一行变过。
        ColumnDefinition("revision", "integer", nullable=False, default=1),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[],
)

# config_poll 用 revision 发现变更。更新由数据库执行 revision = revision + 1。
POLL_REVISION_TABLES = frozenset({
    CHANNEL_CONFIG_TABLE_DEF.table_name,
    LOG_MASKING_RULE_TABLE_DEF.table_name,
    LOGGING_CONFIG_TABLE_DEF.table_name,
    AUDIT_LOG_CONFIG_TABLE_DEF.table_name,
})
