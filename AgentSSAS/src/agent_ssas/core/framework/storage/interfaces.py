# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 存储插件协议接口定义。

定义 StoragePlugin 协议,作为存储后端的统一接口契约。
具体实现包括 SQLiteStore(生产环境)和 MemoryStore(测试环境)。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StoragePlugin(Protocol):
    """存储插件协议,定义存储后端的统一接口。

    所有存储后端(SQLiteStore、MemoryStore 等)需实现此协议。
    接口方法均为异步,保证存储操作不阻塞事件循环。
    """

    async def record_event(self, event: dict) -> str:
        """异步写入一条事件记录,返回 event_id。

        Args:
            event: 事件 dict,包含 event_id、event_type、event_class、
                interaction_seq、session_id、agent_id、trace_id、timestamp 等字段。

        Returns:
            事件唯一标识 event_id。
        """
        ...

    async def create_alert(self, alert: dict) -> str:
        """异步写入一条告警记录,返回 alert_id。

        Args:
            alert: 告警 dict,包含 alert_id、interaction_seq、session_id、
                trace_id、risk_level、risk_type、timestamp 等字段。

        Returns:
            告警唯一标识 alert_id。
        """
        ...

    async def get_events(self, session_id: str = "", limit: int = 100) -> list[dict]:
        """查询事件记录,支持按 session_id 过滤。

        Args:
            session_id: 会话 ID,为空字符串时不过滤,返回所有事件。
            limit: 返回记录数上限。

        Returns:
            事件 dict 列表,按写入时间倒序排列。
        """
        ...

    async def get_alerts(self, acknowledged: bool | None = None, limit: int = 50) -> list[dict]:
        """查询告警记录,支持按确认状态过滤。

        Args:
            acknowledged: 确认状态过滤。None 表示不过滤,True 表示已确认,
                False 表示未确认。
            limit: 返回记录数上限。

        Returns:
            告警 dict 列表,按写入时间倒序排列。
        """
        ...
