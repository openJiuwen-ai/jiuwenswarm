# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Session Manager - 管理 session 任务队列和并发控制.

提供：
- Session 任务队列管理（先进后出，新任务优先）
- Session 任务执行器
- Session 任务取消
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Awaitable, Callable

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
        self._task_result_futures: dict[asyncio.Task, asyncio.Future[Any]] = {}
        self._closing_tasks: dict[str, set[asyncio.Task]] = {}

    @staticmethod
    def get_session_id(session_id: str | None) -> str:
        """获取 session_id，默认为 'default'."""
        return session_id or "default"

    @staticmethod
    def _is_oneshot_session(session_id: str) -> bool:
        """判断是否为一次性 session（心跳/定时任务），其 session_id 永不复用.

        这类 session 每次都用全新 session_id，任务执行完后 processor 不会再有
        新任务进来，必须主动回收，否则 processor 协程永久阻塞在 queue.get()，
        连同队列/字典条目泄漏。判定口径与 interface_deep 中一致.
        """
        return session_id.startswith("heartbeat") or session_id.startswith("cron")

    async def cancel_session_task(
        self,
        session_id: str,
        log_msg_prefix: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        """取消指定 session 的非流式任务."""
        task = self._session_tasks.get(session_id)
        if task is not None and not task.done():
            logger.info(
                "[SessionManager] %s取消 session 非流式任务: session_id=%s",
                log_msg_prefix,
                session_id,
            )
            task.cancel()
            terminated = False
            if wait_timeout is None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                terminated = True
            else:
                done, _ = await asyncio.wait({task}, timeout=wait_timeout)
                if task in done:
                    try:
                        task.result()
                    except (asyncio.CancelledError, Exception):
                        pass
                    terminated = True
                else:
                    logger.warning(
                        "[SessionManager] %scancel_session_task wait timeout: "
                        "session_id=%s wait_timeout=%s",
                        log_msg_prefix,
                        session_id,
                        wait_timeout,
                    )
            if terminated:
                if self._session_tasks.get(session_id) is task:
                    self._session_tasks[session_id] = None
                logger.info(
                    "[SessionManager] %ssession task terminated: session_id=%s",
                    log_msg_prefix,
                    session_id,
                )

    async def cancel_all_session_tasks(self, log_msg_prefix: str = "") -> None:
        """取消所有 session 的非流式任务."""
        for session_id in list(self._session_tasks.keys()):
            await self.cancel_session_task(session_id, log_msg_prefix)

    @staticmethod
    def _cancel_result_future(result_future: asyncio.Future[Any] | None) -> None:
        if result_future is not None and not result_future.done():
            result_future.cancel()

    def _finish_closing_task(self, session_id: str, task: asyncio.Task) -> None:
        closing_tasks = self._closing_tasks.get(session_id)
        if closing_tasks is None or task not in closing_tasks:
            return
        closing_tasks.discard(task)
        if not closing_tasks:
            self._closing_tasks.pop(session_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "[SessionManager] close session failed: session_id=%s",
                session_id,
            )

    def _track_closing_task(self, session_id: str, task: asyncio.Task) -> None:
        closing_tasks = self._closing_tasks.setdefault(session_id, set())
        if task in closing_tasks:
            return
        closing_tasks.add(task)
        task.add_done_callback(
            lambda completed, sid=session_id: self._finish_closing_task(
                sid, completed
            )
        )

    async def close_session(
        self,
        session_id: str,
        wait_timeout: float | None = 5.0,
    ) -> bool:
        """停止并释放指定 session 当前这一代任务处理器."""
        closing_tasks = set(self._closing_tasks.get(session_id, ()))
        had_runtime = bool(closing_tasks) or (
            session_id in self._session_tasks
            or session_id in self._session_priorities
            or session_id in self._session_queues
            or session_id in self._session_processors
        )
        processor = self._session_processors.pop(session_id, None)
        queue = self._session_queues.pop(session_id, None)
        self._session_priorities.pop(session_id, None)
        task = self._session_tasks.pop(session_id, None)

        if task is not None:
            self._cancel_result_future(self._task_result_futures.pop(task, None))
        if queue is not None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    _, _, _, result_future = item
                    self._cancel_result_future(result_future)
                finally:
                    queue.task_done()

        current_task = asyncio.current_task()
        if processor is not None and not processor.done():
            if processor is not current_task:
                processor.cancel()
        elif task is not None and not task.done():
            if task is not current_task:
                task.cancel()

        wait_target = processor or task
        if wait_target is not None:
            self._track_closing_task(session_id, wait_target)
            closing_tasks.add(wait_target)

        wait_targets = {
            target for target in closing_tasks if target is not current_task
        }
        if wait_targets:
            done, pending = await asyncio.wait(
                wait_targets,
                timeout=wait_timeout,
            )
            for completed in done:
                self._finish_closing_task(session_id, completed)
            if pending:
                logger.warning(
                    "[SessionManager] close session wait timeout: "
                    "session_id=%s wait_timeout=%s pending=%d",
                    session_id,
                    wait_timeout,
                    len(pending),
                )

        return had_runtime

    async def close_all_sessions(self) -> None:
        """停止并释放全部 session 任务处理器."""
        session_ids = set(self._session_tasks)
        session_ids.update(self._session_priorities)
        session_ids.update(self._session_queues)
        session_ids.update(self._session_processors)
        session_ids.update(self._closing_tasks)
        if session_ids:
            await asyncio.gather(
                *(self.close_session(session_id) for session_id in session_ids)
            )

    async def ensure_session_processor(self, session_id: str) -> None:
        """确保 session 的任务处理器在运行."""
        if (
            session_id not in self._session_processors
            or self._session_processors[session_id].done()
        ):
            self._session_queues[session_id] = asyncio.PriorityQueue()
            self._session_priorities[session_id] = 0

            async def process_session_queue():
                """处理 session 任务队列（先进后出执行，新任务优先）."""
                queue = self._session_queues[session_id]
                processor = asyncio.current_task()
                try:
                    while True:
                        if (
                            self._session_queues.get(session_id) is not queue
                            or self._session_processors.get(session_id) is not processor
                        ):
                            break
                        try:
                            item = await queue.get()
                            # Queue items include the optional submit_and_wait result Future.
                            # The sentinel is (priority, None, None, None).
                            priority, task_func, task_ctx, result_future = item
                            if task_func is None:
                                queue.task_done()
                                break

                            # Pass the captured ContextVar context to create_task
                            # so the new Task inherits the caller's ContextVars
                            # (workspace, cwd, project_root, etc.) rather than
                            # the processor Task's (possibly stale) context.
                            task = asyncio.create_task(task_func(), context=task_ctx)
                            if result_future is not None:
                                self._task_result_futures[task] = result_future
                            self._session_tasks[session_id] = task
                            try:
                                await task
                            finally:
                                self._task_result_futures.pop(task, None)
                                if (
                                    self._session_queues.get(session_id) is queue
                                    and self._session_tasks.get(session_id) is task
                                ):
                                    self._session_tasks[session_id] = None
                                queue.task_done()
                                # queue.get() 的下一次 await 会保留当前协程帧；主动清空
                                # 上一轮闭包、Context 和 Task，避免空闲 session 持有对象图。
                                item = task_func = task_ctx = result_future = task = None

                        except asyncio.CancelledError:
                            logger.info(
                                "[SessionManager] Session 任务处理器被取消: session_id=%s",
                                session_id,
                            )
                            break
                        except Exception as exc:
                            logger.error(
                                "[SessionManager] Session 任务处理器异常: %s",
                                exc,
                            )
                finally:
                    # close_session 允许相同 session_id 在旧 processor 退出前重连。
                    # 只有映射仍属于本代 processor 时才能清理，避免误删新一代状态。
                    if self._session_queues.get(session_id) is queue:
                        self._session_queues.pop(session_id, None)
                        self._session_priorities.pop(session_id, None)
                        self._session_tasks.pop(session_id, None)
                    if self._session_processors.get(session_id) is processor:
                        self._session_processors.pop(session_id, None)
                    logger.info(
                        "[SessionManager] Session 任务处理器已关闭: session_id=%s",
                        session_id,
                    )

            self._session_processors[session_id] = asyncio.create_task(
                process_session_queue()
            )

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
        # Snapshot ContextVars so the agent task inherits the caller's
        # context (workspace, cwd, project_root set by init_cwd, etc.)
        ctx = contextvars.copy_context()
        await self._session_queues[session_id].put((priority, task_func, ctx, None))

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
            except asyncio.CancelledError:
                self._cancel_result_future(result_future)
                raise
            except Exception as e:
                if not result_future.done():
                    result_future.set_exception(e)
            else:
                if not result_future.done():
                    result_future.set_result(result)

        self._session_priorities[session_id] -= 1
        priority = self._session_priorities[session_id]
        # Snapshot ContextVars so the agent task inherits the caller's
        # context (workspace, cwd, project_root set by init_cwd, etc.)
        ctx = contextvars.copy_context()
        await self._session_queues[session_id].put(
            (priority, wrapped_task, ctx, result_future)
        )

        try:
            return await result_future
        finally:
            # 一次性 session（heartbeat/cron）session_id 永不复用，任务结束后
            # 不会再有新任务进来。这里发一个 None 哨兵让 processor 退出 while 循环，
            # 走既有清理逻辑回收队列/字典条目，避免 processor 协程永久泄漏。
            # 哨兵用较大正数优先级，确保排在所有已入队任务之后执行（不抢占未跑的任务）。
            if self._is_oneshot_session(session_id):
                queue = self._session_queues.get(session_id)
                if queue is not None:
                    await queue.put((1_000_000_000, None, None, None))

    def get_current_task(self, session_id: str) -> asyncio.Task | None:
        """获取当前 session 正在执行的任务."""
        return self._session_tasks.get(session_id)

    def has_active_processor(self, session_id: str) -> bool:
        """检查 session 是否有活跃的处理器."""
        return (
            session_id in self._session_processors
            and not self._session_processors[session_id].done()
        )

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        """Return whether session-owned queue or task state is retained."""
        if session_id is not None:
            return bool(
                session_id in self._session_tasks
                or session_id in self._session_priorities
                or session_id in self._session_queues
                or session_id in self._session_processors
                or session_id in self._closing_tasks
            )
        return bool(
            self._session_tasks
            or self._session_priorities
            or self._session_queues
            or self._session_processors
            or self._task_result_futures
            or self._closing_tasks
        )

    def has_active_tasks(self) -> bool:
        """是否有活跃的 session 任务（供 dreaming busy_checker 使用）。"""
        return any(t is not None and not t.done() for t in self._session_tasks.values())
