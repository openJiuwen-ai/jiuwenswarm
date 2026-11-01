# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""模板表：model_template、embedding_template、extension_config_template、
skill_prebuilt_template、mcp_template、permissions_template（与企业级数据模型对齐）。
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

MODEL_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="model_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("model_type", "json", nullable=False),
        ColumnDefinition("model_tags", "json", nullable=True),
        ColumnDefinition("api_base", "string", length=512, nullable=False),
        ColumnDefinition("api_key", "string", length=4096, nullable=False),
        ColumnDefinition("model_id", "string", length=128, nullable=False),
        ColumnDefinition("model_provider", "string", length=64, nullable=False),
        ColumnDefinition("parameters", "json", nullable=True),
        ColumnDefinition("timeout", "integer", nullable=False, default=60),
        ColumnDefinition("retry_count", "integer", nullable=False, default=3),
        ColumnDefinition("enable_streaming", "boolean", nullable=False, default=True),
        ColumnDefinition("enable_function_calling", "boolean", nullable=False, default=True),
        ColumnDefinition("verify_ssl", "boolean", nullable=False, default=False),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

EMBEDDING_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="embedding_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("embed_tags", "json", nullable=True),
        ColumnDefinition("api_base", "string", length=512, nullable=False),
        ColumnDefinition("api_key", "string", length=4096, nullable=False),
        ColumnDefinition("model_id", "string", length=128, nullable=False),
        ColumnDefinition("model_provider", "string", length=64, nullable=False),
        ColumnDefinition("parameters", "json", nullable=True),
        ColumnDefinition("client_config", "json", nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

EXTENSION_CONFIG_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="extension_config_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("component", "string", length=32, nullable=False),
        ColumnDefinition("hook_type", "string", length=32, nullable=False),
        ColumnDefinition("hook_config", "json", nullable=False),
        ColumnDefinition("custom_config", "json", nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

SKILL_PREBUILT_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="skill_prebuilt_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("skill_id", "string", length=512, nullable=False),
        ColumnDefinition("package_url", "string", length=2048, nullable=True),
        ColumnDefinition("source_id", "string", length=64, nullable=True),
        ColumnDefinition("version_id", "string", length=128, nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

MCP_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="mcp_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("mcp_entry", "json", nullable=False),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

PERMISSIONS_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="permissions_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("body", "json", nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

AGENT_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="agent_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("agent_tags", "json", nullable=True),
        ColumnDefinition("template_ref", "json", nullable=True),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["template_id"], unique=True),
    ],
)

A2A_OUTBOUND_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="a2a_outbound_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("template_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("a2a_tags", "json", nullable=True),
        ColumnDefinition("source_url", "string", length=2048, nullable=False),
        ColumnDefinition("card_path", "string", length=512, nullable=False),
        ColumnDefinition("agent_card", "json", nullable=False),
        ColumnDefinition("card_fingerprint", "string", length=128, nullable=False),
        ColumnDefinition("card_revision", "integer", nullable=False),
        ColumnDefinition("selected_interface", "json", nullable=False),
        ColumnDefinition("credential_ref", "string", length=512, nullable=True),
        ColumnDefinition("connect_timeout_seconds", "float", nullable=False),
        ColumnDefinition("sync_wait_seconds", "float", nullable=False),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[IndexDefinition(["template_id"], unique=True)],
)

A2A_ACCESS_POLICY_TEMPLATE_TABLE_DEF = TableDefinition(
    table_name="a2a_access_policy_template",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("policy_id", "string", length=100, nullable=False),
        ColumnDefinition("policy_name", "string", length=128, nullable=False),
        ColumnDefinition("description", "string", length=512, nullable=True),
        ColumnDefinition("mode", "string", length=16, nullable=False),
        ColumnDefinition("member_template_ids", "json", nullable=False),
        ColumnDefinition("enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("revision", "integer", nullable=False),
        ColumnDefinition("data", "json", nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[IndexDefinition(["policy_id"], unique=True)],
)
