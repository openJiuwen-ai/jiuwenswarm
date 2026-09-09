# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""Web 会话历史表定义（sessions / messages）。

供 mysql/pg 经 foundation handler ``init_table`` 注册 ORM model，使高层 CRUD
(``list_records`` / ``get`` / ``create`` / ``update``) 可用。

仅在 Web 历史库走 mysql/pg（企业版）时由 ``db_actor`` 惰性 import；
个人版（纯内存）不会加载本模块，故不引入 foundation 依赖。
"""

from __future__ import annotations

from openjiuwen_runtime.foundation.db.table_def import (
    ColumnDefinition,
    IndexDefinition,
    TableDefinition,
)

# 时间戳列使用 double（64 位）：MySQL 的 FLOAT 为 32 位单精度，存储
# epoch 秒时间戳会产生量化失真；PG 的 float/double 均为 64 位。
# content 用 mediumtext：mysql → MEDIUMTEXT(16MB)，pg → TEXT(无界)。
# string 列必须带 length：MySQL 的 VARCHAR 不允许省略长度（PG 的裸 VARCHAR
# 合法，会掩盖问题），缺 length 会让 create_all 在 MySQL 上直接编译失败——
# 即企业版 MySQL 部署下历史列表为空的根因。长度对齐平台惯例（64/128/255）。

SESSIONS_TABLE_DEF = TableDefinition(
    table_name="sessions",
    columns=[
        ColumnDefinition("session_id", "string", length=128, primary_key=True, nullable=False),
        ColumnDefinition("user", "string", length=64, nullable=False, default="guest"),
        ColumnDefinition("title", "string", length=255, nullable=True),
        ColumnDefinition("message_count", "integer", nullable=False, default=0),
        ColumnDefinition("last_preview", "string", length=255, nullable=True),
        ColumnDefinition("created_at", "double", nullable=False),
        ColumnDefinition("updated_at", "double", nullable=False),
        # 置顶状态（remote 模式 session.pin 的持久化字段；本地模式存 agent/sessions 元数据）
        ColumnDefinition("pinned", "boolean", nullable=False, default=False),
        ColumnDefinition("pin_order", "integer", nullable=False, default=0),
        # 身份口径（与 pod workspace_{key} 的三元组目录对齐；空 = 存量行通配，见 identity.py）
        ColumnDefinition("group_id", "string", length=128, nullable=True),
        ColumnDefinition("bot_id", "string", length=128, nullable=True),
        # 项目/定时任务归属与排序口径（project.get_sessions / get_cron_sessions 切 PG 的事实源）
        ColumnDefinition("project_id", "string", length=128, nullable=True),
        ColumnDefinition("cron_id", "string", length=128, nullable=True),
        ColumnDefinition("work_mode", "string", length=32, nullable=True),
        ColumnDefinition("last_user_message_at", "double", nullable=True),
    ],
    indexes=[
        IndexDefinition(["user", "updated_at"], unique=False),
        IndexDefinition(["project_id"], unique=False),
        IndexDefinition(["cron_id"], unique=False),
    ],
)

MESSAGES_TABLE_DEF = TableDefinition(
    table_name="messages",
    columns=[
        ColumnDefinition("id", "integer", primary_key=True, nullable=False, autoincrement=True),
        ColumnDefinition("session_id", "string", length=128, nullable=False),
        ColumnDefinition("request_id", "string", length=128, nullable=False),
        ColumnDefinition("role", "string", length=32, nullable=False),
        ColumnDefinition("content", "mediumtext", nullable=False),
        ColumnDefinition("event_type", "string", length=64, nullable=True),
        ColumnDefinition("timestamp", "double", nullable=False),
    ],
    indexes=[
        IndexDefinition(["session_id", "request_id", "role"], unique=True),
        IndexDefinition(["session_id", "timestamp"], unique=False),
    ],
)

ALL_TABLE_DEFINITIONS = (SESSIONS_TABLE_DEF, MESSAGES_TABLE_DEF)


async def init_web_history_tables(handler) -> None:
    """在已连接的 handler 上注册 Web 历史表（幂等：表已存在则跳过创建）。"""
    for table_def in ALL_TABLE_DEFINITIONS:
        await handler.init_table(table_def)
