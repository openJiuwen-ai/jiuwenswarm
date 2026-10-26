# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""SessionMap 持久化表 ``session_map``（与 ``SessionMapRepository`` record 字段对齐）。"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

SESSION_MAP_TABLE_DEF = TableDefinition(
    table_name="session_map",
    columns=[
        ColumnDefinition(
            "id",
            "integer",
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        ColumnDefinition("identity_key", "string", length=512, nullable=False),
        ColumnDefinition("session_id", "string", length=512, nullable=False),
        ColumnDefinition("service_id", "string", length=128, nullable=False),
        ColumnDefinition("agent_id", "string", length=256, nullable=True),
    ],
    indexes=[
        IndexDefinition(["identity_key"], unique=True),
    ],
)

__all__ = ("SESSION_MAP_TABLE_DEF",)
