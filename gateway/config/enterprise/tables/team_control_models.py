# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Team 控制面三张表：team_entity、team_membership、team_route_binding。

企业版落在 Gateway MySQL 库（``gateway_<namespace>``）里，
实例内所有账号共用表，归属写在 ``owner_*`` / ``user_id`` 行上。
主键和唯一键用定长 ``string``，避免 MySQL 对 TEXT 建索引。
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

TEAM_ENTITY_TABLE_DEF = TableDefinition(
    table_name="team_entity",
    columns=[
        ColumnDefinition("team_id", "string", length=64, primary_key=True, nullable=False),
        ColumnDefinition("team_name", "string", length=128, nullable=False, unique=True),
        ColumnDefinition("owner_user_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("owner_group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("owner_bot_id", "string", length=256, nullable=False, default=""),
        ColumnDefinition("status", "string", length=32, nullable=False),
        ColumnDefinition("runtime_mode", "string", length=32, nullable=False, default="local"),
        ColumnDefinition("template_id", "string", length=100, nullable=False, default=""),
        ColumnDefinition("team_session_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("runtime_team_name", "string", length=128, nullable=False, default=""),
        ColumnDefinition("agent_service_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("gateway_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
        ColumnDefinition("extra", "json", nullable=False, default={}),
    ],
    indexes=[
        IndexDefinition(
            ["owner_group_id", "owner_bot_id", "owner_user_id"],
            name="idx_team_entity_owner",
        ),
        IndexDefinition(["team_session_id"], name="idx_team_entity_session"),
    ],
)

TEAM_MEMBERSHIP_TABLE_DEF = TableDefinition(
    table_name="team_membership",
    columns=[
        ColumnDefinition(
            "membership_id", "string", length=64, primary_key=True, nullable=False
        ),
        ColumnDefinition("team_id", "string", length=64, nullable=False),
        ColumnDefinition("user_id", "string", length=64, nullable=False),
        ColumnDefinition("group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("bot_id", "string", length=256, nullable=False, default=""),
        ColumnDefinition("member_name", "string", length=128, nullable=False),
        ColumnDefinition("member_type", "string", length=32, nullable=False, default="human"),
        ColumnDefinition("display_name", "string", length=128, nullable=False, default=""),
        ColumnDefinition("permissions", "json", nullable=False, default=[]),
        ColumnDefinition("status", "string", length=32, nullable=False),
        ColumnDefinition("expires_at", "double", nullable=True),
        ColumnDefinition("joined_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
    ],
    indexes=[
        IndexDefinition(
            ["team_id", "user_id"],
            unique=True,
            name="uq_team_membership_team_user",
        ),
        IndexDefinition(
            ["team_id", "member_name"],
            unique=True,
            name="uq_team_membership_team_member",
        ),
        IndexDefinition(["team_id", "status"], name="idx_team_membership_team"),
    ],
)

TEAM_ROUTE_BINDING_TABLE_DEF = TableDefinition(
    table_name="team_route_binding",
    columns=[
        ColumnDefinition(
            "team_session_id", "string", length=128, primary_key=True, nullable=False
        ),
        ColumnDefinition("team_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("endpoint", "string", length=512, nullable=False),
        ColumnDefinition("agent_service_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("route_request_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("owner_user_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("owner_group_id", "string", length=64, nullable=False, default=""),
        ColumnDefinition("owner_bot_id", "string", length=256, nullable=False, default=""),
        ColumnDefinition("gateway_id", "string", length=128, nullable=False, default=""),
        ColumnDefinition("status", "string", length=32, nullable=False),
        ColumnDefinition("route_version", "integer", nullable=False, default=1),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
        ColumnDefinition("last_heartbeat_at", "double", nullable=False, default=0),
    ],
    indexes=[
        IndexDefinition(
            ["team_id", "updated_at"],
            name="idx_team_route_team_id_updated",
        ),
    ],
)

TEAM_CONTROL_TABLES: tuple[TableDefinition, ...] = (
    TEAM_ENTITY_TABLE_DEF,
    TEAM_MEMBERSHIP_TABLE_DEF,
    TEAM_ROUTE_BINDING_TABLE_DEF,
)

__all__ = (
    "TEAM_CONTROL_TABLES",
    "TEAM_ENTITY_TABLE_DEF",
    "TEAM_MEMBERSHIP_TABLE_DEF",
    "TEAM_ROUTE_BINDING_TABLE_DEF",
)
