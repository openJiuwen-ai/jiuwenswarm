# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""同步 Store 与 Gateway MySQL 之间的单线程连接。

aiomysql 连接绑在创建它的 event loop 上。控制面和路由 Store 的方法仍是同步的，
所以每个进程只开一条后台线程，所有 SQL 都投到这条线程上执行。
不复用 Gateway 主 loop 上的 ``Database.current()``，避免跨 loop 用同一条连接。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

_START_TIMEOUT_SEC = 30.0
_CANCEL_GRACE_SEC = 1.0

_actor: "TeamMysqlActor | None" = None
_actor_lock = threading.Lock()


def _gateway_db_kind() -> str:
    import os

    from gateway.enterprise.db.settings import get_settings

    # 只在读取团队库配置时丢掉空密码。集群 Secret 注入的空值会挡住仓库 .env，
    # 不能放进所有 Gateway 连接共用的 load_env。
    if os.environ.get("GATEWAY_DB_PASSWORD") == "":
        os.environ.pop("GATEWAY_DB_PASSWORD", None)
    return str(get_settings().gateway_db_type or "").strip().lower()


def use_team_mysql() -> bool:
    """三张控制面表是否走 Gateway MySQL。

    ``postgresql``、``postgres``、``pg`` 与 ``sqlite`` 一样返回 False，不抛错。
    团队表只在类型为 ``mysql`` 时使用 ``MySQLHandler``。
    """
    return _gateway_db_kind() == "mysql"


class TeamMysqlActor:
    """进程内唯一的 MySQL 连接线程。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._handler: Any = None
        self._ready = False
        self._start_lock = threading.Lock()
        self._call_timeout = 35.0

    def start(self) -> None:
        """连上库并建好三张控制面表。失败时异常直接抛出。"""
        if not use_team_mysql():
            raise RuntimeError("team control actor requires GATEWAY_DB_TYPE=mysql")
        if self._ready:
            return
        with self._start_lock:
            if self._ready:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop,
                args=(loop,),
                name="team-mysql",
                daemon=True,
            )
            self._loop = loop
            self._thread = thread
            thread.start()
            fut = asyncio.run_coroutine_threadsafe(self._init(), loop)
            fut.result(timeout=_START_TIMEOUT_SEC)
            self._ready = True

    def call(self, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        """在连接线程上执行协程并等结果。"""
        if not self._ready or self._loop is None or self._thread is None:
            raise RuntimeError("Team MySQL actor is not started")
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "TeamMysqlActor.call cannot be reentered from the MySQL thread"
            )
        task_box: dict[str, asyncio.Task[Any]] = {}

        async def _wrapped() -> Any:
            task_box["task"] = asyncio.current_task()
            return await coro_fn()

        fut = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
        try:
            return fut.result(timeout=self._call_timeout)
        except TimeoutError as exc:
            self._abandon_timed_out_call(fut, task_box.get("task"))
            raise TimeoutError(
                f"TeamMysqlActor.call timed out after {self._call_timeout:.0f}s"
            ) from exc

    def _abandon_timed_out_call(self, fut: Any, task: asyncio.Task[Any] | None) -> None:
        """Cancel the in-flight query. Reconnect if it keeps the connection."""
        if task is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(task.cancel)
        else:
            fut.cancel()
        try:
            fut.result(timeout=_CANCEL_GRACE_SEC)
        except TimeoutError:
            logger.warning("[TeamMysql] timed out call did not cancel; reconnecting")
            self._reconnect()
        except (asyncio.CancelledError, Exception):
            if task is not None and not task.done():
                logger.warning("[TeamMysql] timed out call still running; reconnecting")
                self._reconnect()
            else:
                logger.debug("[TeamMysql] timed out call cancelled", exc_info=True)

    def _reconnect(self) -> None:
        if self._loop is None:
            return
        reset = asyncio.run_coroutine_threadsafe(self._replace_handler(), self._loop)
        try:
            reset.result(timeout=_START_TIMEOUT_SEC)
        except Exception:
            logger.exception("[TeamMysql] reconnect after abandoned call failed")

    async def _replace_handler(self) -> None:
        """Drop the connection a cancelled query may still hold, then connect again."""
        old = self._handler
        self._handler = None
        if old is not None:
            try:
                await old.disconnect()
            except Exception:
                logger.exception("[TeamMysql] disconnect abandoned handler failed")
                engine = getattr(old, "engine", None)
                if engine is not None:
                    try:
                        await engine.dispose()
                    except Exception:
                        logger.exception("[TeamMysql] dispose abandoned engine failed")
        await self._init()

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _init(self) -> None:
        from openjiuwen_runtime.foundation.db.engine_options import get_command_timeout
        from openjiuwen_runtime.foundation.db.mysql_handler import MySQLHandler

        from gateway.config.enterprise.tables.team_control_models import (
            TEAM_CONTROL_TABLES,
        )
        from gateway.config.enterprise.tables.team_quota_models import (
            TEAM_QUOTA_TABLES,
            ensure_team_quota_org_column,
        )
        from gateway.enterprise.db.settings import get_settings

        cfg = get_settings()
        self._call_timeout = float(get_command_timeout()) + 5.0
        handler = MySQLHandler(
            host=str(cfg.gateway_db_host).strip(),
            port=int(cfg.gateway_db_port),
            user=str(cfg.gateway_db_user).strip(),
            password=str(cfg.gateway_db_password),
            database=str(cfg.gateway_db_name).strip(),
        )
        await handler.init_database()
        await handler.connect()
        for table_def in (*TEAM_CONTROL_TABLES, *TEAM_QUOTA_TABLES):
            await handler.init_table(table_def)
        await ensure_team_quota_org_column(handler.get_engine())
        self._handler = handler
        logger.info(
            "[TeamMysql] connected %s:%s/%s",
            cfg.gateway_db_host,
            cfg.gateway_db_port,
            cfg.gateway_db_name,
        )

    @property
    def handler(self) -> Any:
        if self._handler is None:
            raise RuntimeError("Team MySQL handler is not connected")
        return self._handler


async def run_team_store(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run one sync team-store call.

    ``start`` and ``call`` wait with ``fut.result()``. On the HTTP event loop
    that wait is moved to a worker thread so ``/health`` can still be served.
    Personal SQLite and callers without a running loop stay on this thread.
    """
    if not use_team_mysql():
        return fn(*args, **kwargs)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def get_team_mysql_actor() -> TeamMysqlActor:
    """返回已连上库的 actor。第一次调用会阻塞到建表完成。"""
    global _actor
    if _actor is not None and _actor._ready:
        return _actor
    with _actor_lock:
        if _actor is not None and _actor._ready:
            return _actor
        actor = TeamMysqlActor()
        actor.start()
        _actor = actor
        return actor


def reset_team_mysql_actor() -> None:
    """测试用：丢掉进程内 actor，下次 ``get_team_mysql_actor`` 重新连接。"""
    global _actor
    with _actor_lock:
        _actor = None


def is_unique_violation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ())
    if args and args[0] in (1062, "1062"):
        return True
    return "Duplicate entry" in str(orig)


_LIST_PAGE = 500


def record_to_dict(record: Any) -> dict[str, Any]:
    """Turn one ``MySQLHandler`` row into the dict shape raw SQL used to return."""
    table = getattr(record, "__table__", None)
    if table is None:
        raise TypeError("MySQL handler record has no table")
    return {column.name: getattr(record, column.name) for column in table.columns}


async def list_record_dicts(
    handler: Any,
    table_name: str,
    *,
    filters: dict[str, Any] | None = None,
    order_by: list[tuple[str, bool]] | str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read rows through ``list_records``.

    A set limit is passed through. Without one, pages are concatenated so a
    list is not cut off at the handler default of 100.
    """
    if limit is not None:
        records = await handler.list_records(
            table_name,
            filters=filters,
            limit=limit,
            order_by=order_by,
        )
        return [record_to_dict(record) for record in records]
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        records = await handler.list_records(
            table_name,
            filters=filters,
            limit=_LIST_PAGE,
            offset=offset,
            order_by=order_by,
        )
        rows.extend(record_to_dict(record) for record in records)
        if len(records) < _LIST_PAGE:
            return rows
        offset += _LIST_PAGE


async def fetch_all(handler: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    engine = handler.get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]


async def fetch_one(handler: Any, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    rows = await fetch_all(handler, sql, params)
    return rows[0] if rows else None


async def run_tx(handler: Any, fn: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    engine = handler.get_engine()
    async with engine.begin() as conn:
        return await fn(conn)
