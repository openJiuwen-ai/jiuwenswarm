# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Session Manager - 管理 session 任务队列和并发控制.

提供：
- Session 任务队列管理（先进后出，新任务优先）
- Session 任务执行器
- Session 任务取消
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


class SessionManager:
    """Session 任务管理器.

    管理多 session 并发执行，同 session 内任务按先进后出顺序执行.
    """

    def __init__(self) -> None:
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._session_priorities: dict[str, int] = {}
        self._session_queues: dict[str, asyncio.PriorityQueue] = {}
        self._session_processors: dict[str, asyncio.Task] = {}

    def get_session_id(self, session_id: str | None) -> str:
        """获取 session_id，默认为 'default'."""
        return session_id or "default"

    def get_session_queues(self):
        return self._session_queues

    def get_session_tasks(self):
        return self._session_tasks

    async def cancel_session_task(self, session_id: str, log_msg_prefix: str = "") -> None:
        """取消指定 session 的非流式任务."""
        task = self._session_tasks.get(session_id)
        if task is not None and not task.done():
            logger.info(
                format_session_log(
                    session_id,
                    f"[SessionManager] {log_msg_prefix}取消 session 非流式任务",
                )
            )
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._session_tasks[session_id] = None

    async def cancel_all_session_tasks(self, log_msg_prefix: str = "") -> None:
        """取消所有 session 的非流式任务."""
        for session_id in list(self._session_tasks.keys()):
            await self.cancel_session_task(session_id, log_msg_prefix)

    async def ensure_session_processor(self, session_id: str) -> None:
        """确保 session 的任务处理器在运行."""
        if session_id not in self._session_processors or self._session_processors[session_id].done():
            self._session_queues[session_id] = asyncio.PriorityQueue()
            self._session_priorities[session_id] = 0

            async def process_session_queue():
                """处理 session 任务队列（先进后出执行，新任务优先）."""
                queue = self._session_queues[session_id]
                while True:
                    try:
                        priority, task_func = await queue.get()
                        if task_func is None:
                            break

                        self._session_tasks[session_id] = asyncio.create_task(task_func())
                        try:
                            await self._session_tasks[session_id]
                        finally:
                            self._session_tasks[session_id] = None
                            queue.task_done()

                    except asyncio.CancelledError:
                        logger.info(format_session_log(session_id, "[SessionManager] Session 任务处理器被取消"))
                        break
                    except Exception as e:
                        logger.error("[SessionManager] Session 任务处理器异常: %s", e)

                self._session_queues.pop(session_id, None)
                self._session_priorities.pop(session_id, None)
                self._session_tasks.pop(session_id, None)
                self._session_processors.pop(session_id, None)
                logger.info(format_session_log(session_id, "[SessionManager] Session 任务处理器已关闭"))

            self._session_processors[session_id] = asyncio.create_task(process_session_queue())

    async def submit_task(
        self,
        session_id: str,
        task_func: Callable[[], Awaitable[Any]],
    ) -> None:
        """提交任务到 session 队列.

        Args:
            session_id: Session ID.
            task_func: 异步任务函数.
        """
        await self.ensure_session_processor(session_id)
        self._session_priorities[session_id] -= 1
        priority = self._session_priorities[session_id]
        await self._session_queues[session_id].put((priority, task_func))

    async def submit_and_wait(
        self,
        session_id: str,
        task_func: Callable[[], Awaitable[Any]],
    ) -> Any:
        """提交任务到 session 队列并等待结果.

        Args:
            session_id: Session ID.
            task_func: 异步任务函数.

        Returns:
            任务执行结果.
        """
        await self.ensure_session_processor(session_id)
        result_future = asyncio.get_event_loop().create_future()

        async def wrapped_task():
            try:
                result = await task_func()
                result_future.set_result(result)
            except Exception as e:
                result_future.set_exception(e)

        self._session_priorities[session_id] -= 1
        priority = self._session_priorities[session_id]
        await self._session_queues[session_id].put((priority, wrapped_task))

        return await result_future

    def get_current_task(self, session_id: str) -> asyncio.Task | None:
        """获取当前 session 正在执行的任务."""
        return self._session_tasks.get(session_id)

    def has_active_processor(self, session_id: str) -> bool:
        """检查 session 是否有活跃的处理器."""
        return (
            session_id in self._session_processors
            and not self._session_processors[session_id].done()
        )
