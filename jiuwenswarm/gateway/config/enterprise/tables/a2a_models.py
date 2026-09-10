# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Enterprise A2A local state and dispatch history tables."""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

A2A_OUTBOUND_USER_STATE_TABLE_DEF = TableDefinition(
    table_name="a2a_outbound_user_state",
    columns=[
        ColumnDefinition(
            "id", "integer", primary_key=True, autoincrement=True, nullable=False
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("user_enabled", "boolean", nullable=False, default=True),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[IndexDefinition(["template_id"], unique=True)],
)

A2A_OUTBOUND_RUNTIME_STATE_TABLE_DEF = TableDefinition(
    table_name="a2a_outbound_runtime_state",
    columns=[
        ColumnDefinition(
            "id", "integer", primary_key=True, autoincrement=True, nullable=False
        ),
        ColumnDefinition("template_id", "string", length=100, nullable=False),
        ColumnDefinition("availability", "string", length=32, nullable=False),
        ColumnDefinition("last_checked_at", "datetime", nullable=True),
        ColumnDefinition("last_success_at", "datetime", nullable=True),
        ColumnDefinition("last_error_code", "string", length=64, nullable=True),
        ColumnDefinition("last_error_summary", "string", length=512, nullable=True),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[IndexDefinition(["template_id"], unique=True)],
)

A2A_OUTBOUND_DISPATCH_TABLE_DEF = TableDefinition(
    table_name="a2a_outbound_dispatch",
    columns=[
        ColumnDefinition(
            "id", "integer", primary_key=True, autoincrement=True, nullable=False
        ),
        ColumnDefinition("dispatch_id", "string", length=100, nullable=False),
        ColumnDefinition("agent_id", "string", length=100, nullable=False),
        ColumnDefinition("agent_name", "string", length=128, nullable=True),
        ColumnDefinition("agent_revision", "integer", nullable=False),
        ColumnDefinition("mode", "string", length=16, nullable=False),
        ColumnDefinition("status", "string", length=32, nullable=False),
        ColumnDefinition("request_message_id", "string", length=100, nullable=False),
        ColumnDefinition("source_session_id", "string", length=512, nullable=False),
        ColumnDefinition("source_resource_id", "string", length=100, nullable=True),
        ColumnDefinition("remote_task_id", "string", length=256, nullable=True),
        ColumnDefinition("remote_context_id", "string", length=256, nullable=True),
        ColumnDefinition("accepted_at", "datetime", nullable=True),
        ColumnDefinition("finished_at", "datetime", nullable=True),
        ColumnDefinition("result", "json", nullable=True),
        ColumnDefinition("error_code", "string", length=64, nullable=True),
        ColumnDefinition("error_summary", "string", length=512, nullable=True),
        ColumnDefinition("last_polled_at", "datetime", nullable=True),
        ColumnDefinition("input_length", "integer", nullable=True),
        ColumnDefinition("input_content_type", "string", length=128, nullable=True),
        ColumnDefinition("input_digest", "string", length=128, nullable=True),
        ColumnDefinition("created_at", "datetime", nullable=False),
        ColumnDefinition("updated_at", "datetime", nullable=False),
    ],
    indexes=[
        IndexDefinition(["dispatch_id"], unique=True),
        IndexDefinition(["source_session_id"]),
        IndexDefinition(["source_resource_id"]),
        IndexDefinition(["created_at"]),
    ],
)

__all__ = (
    "A2A_OUTBOUND_DISPATCH_TABLE_DEF",
    "A2A_OUTBOUND_RUNTIME_STATE_TABLE_DEF",
    "A2A_OUTBOUND_USER_STATE_TABLE_DEF",
)
