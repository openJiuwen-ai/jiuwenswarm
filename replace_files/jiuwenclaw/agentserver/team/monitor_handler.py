# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team Monitor 处理器.

处理 Team Monitor 的事件流和状态查询，将团队状态转换为前端可消费的格式.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from openjiuwen.agent_teams.monitor import create_monitor, TeamMonitor
from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType
from openjiuwen.agent_teams.agent.team_agent import TeamAgent

from jiuwenclaw.agentserver.team.event_types import (
    get_team_event_type,
    get_event_category,
)
from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


class TeamMonitorHandler:
    """Team Monitor 处理器.

    封装 Monitor 的创建、事件处理和状态查询，提供简化的接口给前端.
    """

    def __init__(
        self,
        team_agent: TeamAgent,
        session_id: str,
    ):
        """初始化处理器.

        Args:
            team_agent: TeamAgent 实例
            session_id: 会话 ID
        """
        self._team_agent = team_agent
        self._session_id = session_id
        self._monitor: TeamMonitor | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动 Monitor."""
        if self._running:
            return

        try:
            from openjiuwen.agent_teams.spawn.context import set_session_id, reset_session_id
            
            token = set_session_id(self._session_id)
            try:
                self._monitor = create_monitor(self._team_agent)
                await self._monitor.start()
            finally:
                reset_session_id(token)
            
            self._running = True

            # 启动事件收集任务
            self._event_task = asyncio.create_task(self._collect_events())

            logger.info(
                format_session_log(self._session_id, "[TeamMonitorHandler] Monitor 启动成功")
            )

        except Exception as e:
            logger.error(
                format_session_log(self._session_id, f"[TeamMonitorHandler] Monitor 启动失败: error={e}")
            )
            raise

    async def stop(self) -> None:
        """停止 Monitor."""
        self._running = False

        if self._event_task is not None:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None

        if self._monitor is not None:
            try:
                await self._monitor.stop()
            except Exception as e:
                logger.warning(
                    format_session_log(self._session_id, f"[TeamMonitorHandler] Monitor 停止失败: error={e}")
                )
            self._monitor = None

        logger.info(
            format_session_log(self._session_id, "[TeamMonitorHandler] Monitor 已停止")
        )

    async def _collect_events(self) -> None:
        """后台任务：收集 Monitor 事件."""
        if self._monitor is None:
            return

        try:
            async for event in self._monitor.events():
                if not self._running:
                    break

                event_dict = await self._convert_event_to_dict(event)
                if event_dict:
                    await self._event_queue.put(event_dict)

        except Exception as e:
            logger.error(
                format_session_log(self._session_id, f"[TeamMonitorHandler] 事件收集失败: error={e}")
            )

    @staticmethod
    def _handle_member_spawned(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理成员创建事件."""
        base["member_id"] = event.member_id
        return base

    @staticmethod
    def _handle_member_status_changed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理成员状态变更事件."""
        base.update({
            "member_id": event.member_id,
            "old_status": event.old_status,
            "new_status": event.new_status,
        })
        return base

    @staticmethod
    def _handle_member_execution_changed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理成员执行状态变更事件."""
        base.update({
            "member_id": event.member_id,
            "old_status": event.old_status,
            "new_status": event.new_status,
        })
        return base

    @staticmethod
    def _handle_member_restarted(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理成员重启事件."""
        base.update({
            "member_id": event.member_id,
            "reason": event.reason,
            "restart_count": event.restart_count,
        })
        return base

    @staticmethod
    def _handle_member_shutdown(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理成员关闭事件."""
        base.update({
            "member_id": event.member_id,
            "force": event.force,
        })
        return base

    @staticmethod
    def _handle_task_created(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理任务创建事件."""
        base.update({
            "task_id": event.task_id,
            "status": event.status,
        })
        return base

    @staticmethod
    def _handle_task_claimed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理任务认领事件."""
        base["task_id"] = event.task_id
        return base

    @staticmethod
    def _handle_task_completed(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理任务完成事件."""
        base["task_id"] = event.task_id
        return base

    @staticmethod
    def _handle_task_cancelled(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理任务取消事件."""
        base["task_id"] = event.task_id
        return base

    @staticmethod
    def _handle_task_unblocked(base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理任务解除阻塞事件."""
        base["task_id"] = event.task_id
        return base

    async def _handle_message(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理点对点消息事件."""
        message_content = await self._get_message_content(event.message_id)
        base.update({
            "message_id": event.message_id,
            "from_member": event.from_member,
            "to_member": event.to_member,
            "content": message_content,
        })
        return base

    async def _handle_broadcast(self, base: dict[str, Any], event: MonitorEvent) -> dict[str, Any]:
        """处理广播消息事件."""
        message_content = await self._get_message_content(event.message_id)
        base.update({
            "message_id": event.message_id,
            "from_member": event.from_member,
            "content": message_content,
        })
        return base

    async def _get_message_content(self, message_id: str | None) -> str:
        """获取消息内容.

        Args:
            message_id: 消息 ID

        Returns:
            消息内容，如果获取失败返回空字符串
        """
        if not message_id or not self._monitor:
            return ""

        try:
            from openjiuwen.agent_teams.spawn.context import set_session_id, reset_session_id
            
            token = set_session_id(self._session_id)
            try:
                messages = await self._monitor.get_messages()
                for message in messages:
                    if message.message_id == message_id:
                        return message.content or ""
                return ""
            finally:
                reset_session_id(token)
        except Exception as e:
            logger.warning(
                "[TeamMonitorHandler] 查询消息内容失败: message_id=%s, error=%s",
                message_id,
                e,
            )
            return ""

    async def _convert_event_to_dict(self, event: MonitorEvent) -> dict[str, Any] | None:
        """将 MonitorEvent 转换为字典格式.

        Args:
            event: MonitorEvent 实例

        Returns:
            事件字典，如果事件类型不需要处理返回 None
        """
        team_event_type = get_team_event_type(event.event_type)
        if team_event_type is None:
            return None

        event_category = get_event_category(team_event_type)

        event_data: dict[str, Any] = {
            "type": team_event_type.value,
            "team_id": event.team_id,
        }

        if event.member_id:
            event_data["member_id"] = event.member_id

        event_handlers = {
            MonitorEventType.MEMBER_SPAWNED: self._handle_member_spawned,
            MonitorEventType.MEMBER_STATUS_CHANGED: self._handle_member_status_changed,
            MonitorEventType.MEMBER_EXECUTION_CHANGED: self._handle_member_execution_changed,
            MonitorEventType.MEMBER_RESTARTED: self._handle_member_restarted,
            MonitorEventType.MEMBER_SHUTDOWN: self._handle_member_shutdown,
            MonitorEventType.TASK_CREATED: self._handle_task_created,
            MonitorEventType.TASK_CLAIMED: self._handle_task_claimed,
            MonitorEventType.TASK_COMPLETED: self._handle_task_completed,
            MonitorEventType.TASK_CANCELLED: self._handle_task_cancelled,
            MonitorEventType.TASK_UNBLOCKED: self._handle_task_unblocked,
            MonitorEventType.MESSAGE: self._handle_message,
            MonitorEventType.BROADCAST: self._handle_broadcast,
        }

        handler = event_handlers.get(event.event_type)
        if handler is None:
            return None

        if asyncio.iscoroutinefunction(handler):
            event_data = await handler(event_data, event)
        else:
            event_data = handler(event_data, event)

        return {
            "event_type": event_category.value,
            "event": event_data,
        }

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """获取事件流.

        Yields:
            事件字典
        """
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(
                    format_session_log(self._session_id, f"[TeamMonitorHandler] 事件流错误: error={e}")
                )
                break

    @property
    def is_running(self) -> bool:
        """Monitor 是否正在运行."""
        return self._running

    @property
    def team_id(self) -> str | None:
        """团队 ID."""
        return self._monitor.team_id if self._monitor else None
