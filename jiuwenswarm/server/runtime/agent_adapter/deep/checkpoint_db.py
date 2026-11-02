"""Persistent checkpointer singleton and MySQL/PostgreSQL engine helpers.

Module-level helpers with no adapter state (approach C).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import quote_plus

from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider

from jiuwenswarm.common.utils import get_checkpoint_dir
from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)


_PERSISTENT_CHECKPOINTER_LOCK: asyncio.Lock | None = None
_PERSISTENT_CHECKPOINTER_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_PERSISTENT_CHECKPOINTER_LOCK_INIT = threading.Lock()
_PERSISTENT_CHECKPOINTER_READY = False
_shared_checkpoint_checkpointer: Any = None
_shared_mysql_checkpoint_engine: Any = None
_shared_postgresql_checkpoint_engine: Any = None


def reset_shared_checkpoint_for_tests() -> None:
    """Reset process-wide checkpoint singleton (tests only)."""
    global _shared_checkpoint_checkpointer, _PERSISTENT_CHECKPOINTER_READY
    global _PERSISTENT_CHECKPOINTER_LOCK, _PERSISTENT_CHECKPOINTER_LOCK_LOOP
    global _shared_mysql_checkpoint_engine, _shared_postgresql_checkpoint_engine
    _shared_checkpoint_checkpointer = None
    _PERSISTENT_CHECKPOINTER_READY = False
    _PERSISTENT_CHECKPOINTER_LOCK = None
    _PERSISTENT_CHECKPOINTER_LOCK_LOOP = None
    _shared_mysql_checkpoint_engine = None
    _shared_postgresql_checkpoint_engine = None


def _gateway_db_pool_kwargs() -> dict[str, Any]:
    """SQLAlchemy 连接池参数（``GATEWAY_DB_POOL_*`` / ``RUNTIME_DB_POOL_*``）。"""

    def _int_env(*names: str, default: int) -> int:
        for name in names:
            raw = os.getenv(name, "").strip()
            if not raw:
                continue
            try:
                return int(raw)
            except ValueError:
                continue
        return default

    return {
        "pool_size": _int_env("GATEWAY_DB_POOL_SIZE", "RUNTIME_DB_POOL_SIZE", default=2),
        "max_overflow": _int_env("GATEWAY_DB_MAX_OVERFLOW", "RUNTIME_DB_MAX_OVERFLOW", default=20),
        "pool_timeout": _int_env("GATEWAY_DB_POOL_TIMEOUT", "RUNTIME_DB_POOL_TIMEOUT", default=30),
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _get_persistent_checkpointer_lock() -> asyncio.Lock:
    """Lazy-init or rebind the process-wide checkpointer lock to the running loop.

    ``asyncio.Lock`` binds to the loop of its first ``acquire()``; any later
    acquire from a different loop raises ``RuntimeError("... is bound to a
    different event loop")``. The first call may have happened on a transient
    loop (test harness, ephemeral worker) that is gone by the time AgentServer's
    main loop needs the lock. To stay correct under that drift we:

    1. Lazily construct the Lock inside the caller's running loop (guarded by a
       ``threading.Lock`` so concurrent first-acquires from different threads
       cannot create two Locks).
    2. Before returning, verify the Lock is still bound to the current loop. If
       it is bound to a dead/foreign loop, drop and rebuild it so the caller can
       re-acquire safely. This rebinding is also done under the threading guard.

    After ``_PERSISTENT_CHECKPOINTER_READY`` flips True no one acquires the lock,
    so rebinding is a no-op for the steady state.
    """
    global _PERSISTENT_CHECKPOINTER_LOCK, _PERSISTENT_CHECKPOINTER_LOCK_LOOP
    current = _running_loop()
    if current is None:
        raise RuntimeError(
            "_get_persistent_checkpointer_lock must be called from a running event loop"
        )
    with _PERSISTENT_CHECKPOINTER_LOCK_INIT:
        bound = _PERSISTENT_CHECKPOINTER_LOCK_LOOP
        if _PERSISTENT_CHECKPOINTER_LOCK is None or bound is None or bound is not current:
            if bound is not None and bound is not current and not bound.is_closed():
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] _PERSISTENT_CHECKPOINTER_LOCK rebound: "
                    "old_loop=%r new_loop=%r (lock repr=%r)",
                    bound,
                    current,
                    _PERSISTENT_CHECKPOINTER_LOCK,
                )
            _PERSISTENT_CHECKPOINTER_LOCK = asyncio.Lock()
            _PERSISTENT_CHECKPOINTER_LOCK_LOOP = current
    return _PERSISTENT_CHECKPOINTER_LOCK

def _patch_compiler_for_on_conflict():
    """使 MySQL 和 PostgreSQL SQLAlchemy 编译器支持 SQLite 的 ON CONFLICT DO UPDATE.

    openjiuwen SDK 的 DbBasedKVStore 硬编码了 SQLite upsert 语法.
    此 patch 在 SQL 编译阶段将 ON CONFLICT ... DO UPDATE 翻译为对应数据库的语法,
    使 checkpoint 可以正常写入 MySQL/PostgreSQL.
    """
    try:
        from sqlalchemy.ext.compiler import compiles
        from sqlalchemy.dialects.sqlite.dml import OnConflictDoUpdate

        @compiles(OnConflictDoUpdate, "mysql")
        def _mysql_on_conflict_do_update(element, compiler, **kw):
            values = getattr(element, "_update_values", None)
            set_pairs = []
            if isinstance(values, dict):
                for col_key in values:
                    col_name = compiler.preparer.format_column(col_key)
                    set_pairs.append(f"{col_name} = VALUES({col_name})")
            if not set_pairs:
                set_pairs.append("value = VALUES(value)")
            return f"\nON DUPLICATE KEY UPDATE {', '.join(set_pairs)}"

        @compiles(OnConflictDoUpdate, "postgresql")
        def _postgresql_on_conflict_do_update(element, compiler, **kw):
            values = getattr(element, "_update_values", None)
            set_pairs = []
            if isinstance(values, dict):
                for col_key in values:
                    col_name = compiler.preparer.format_column(col_key)
                    set_pairs.append(f"{col_name} = EXCLUDED.{col_name}")
            if not set_pairs:
                set_pairs.append("value = EXCLUDED.value")
            return f" ON CONFLICT (key) DO UPDATE SET {', '.join(set_pairs)}"
    except Exception:
        pass


_patch_compiler_for_on_conflict()


async def _build_mysql_async_engine():
    """构建 / 复用 checkpoint MySQL AsyncEngine。

    连接参数从 ``GATEWAY_DB_*`` 读取；池参数见 ``GATEWAY_DB_POOL_*``。
    进程内复用同一 engine，避免每个 agent 再建独立连接池。
    未配置 ``GATEWAY_DB_HOST`` 或非企业版时返回 None，由调用方决定是否回退 SQLite。
    配置了 ``GATEWAY_DB_HOST`` 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite 不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    global _shared_mysql_checkpoint_engine
    if not is_enterprise():
        return None
    if _shared_mysql_checkpoint_engine is not None:
        return _shared_mysql_checkpoint_engine
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        db_port = os.getenv("GATEWAY_DB_PORT", "3306").strip()
        db_user = os.getenv("GATEWAY_DB_USER", "root").strip()
        db_password = os.getenv("GATEWAY_DB_PASSWORD", "").strip()
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        # URL-encode 用户名/密码，避免特殊字符（如 @ : / # %）破坏连接串解析
        _encoded_user = quote_plus(db_user)
        _encoded_password = quote_plus(db_password)
        # 先连接不指定库，自动建库
        server_url = (
            f"mysql+aiomysql://{_encoded_user}:{_encoded_password}"
            f"@{db_host}:{db_port}?charset=utf8mb4"
        )
        temp_engine = create_async_engine(server_url, echo=False)
        async with temp_engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        await temp_engine.dispose()

        # 再连接目标库
        db_url = (
            f"mysql+aiomysql://{_encoded_user}:{_encoded_password}"
            f"@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
        )
        pool_kwargs = _gateway_db_pool_kwargs()
        engine = create_async_engine(db_url, **pool_kwargs)
        logger.info(
            "[JiuWenSwarmDeepAdapter] checkpoint MySQL engine created: %s:%s/%s "
            "pool_size=%s max_overflow=%s pool_timeout=%s",
            db_host,
            db_port,
            db_name,
            pool_kwargs["pool_size"],
            pool_kwargs["max_overflow"],
            pool_kwargs["pool_timeout"],
        )

        # 确保 kv_store.value 列为 LONGTEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'kv_store' AND COLUMN_NAME = 'value'"
                ),
                {"db": db_name},
            )
            row = result.fetchone()
            if row is None:
                await conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS kv_store ("
                        "`key` VARCHAR(512) PRIMARY KEY,"
                        "`value` LONGTEXT NOT NULL"
                        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store table created with value LONGTEXT"
                )
            elif str(row[0]).upper() != "LONGTEXT":
                await conn.execute(
                    text(
                        "ALTER TABLE kv_store MODIFY COLUMN `value` LONGTEXT NOT NULL"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store.value altered to LONGTEXT"
                )

        _shared_mysql_checkpoint_engine = engine
        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenSwarmDeepAdapter] failed to create checkpoint MySQL engine: %s",
            exc,
        )
        raise


async def _build_postgresql_async_engine():
    """构建 / 复用 checkpoint PostgreSQL AsyncEngine。

    连接参数从 ``GATEWAY_DB_*`` 读取；池参数见 ``GATEWAY_DB_POOL_*``。
    进程内复用同一 engine，避免每个 agent 再建独立连接池。
    未配置 ``GATEWAY_DB_HOST`` 或非企业版时返回 None，由调用方决定是否回退 SQLite。
    配置了 ``GATEWAY_DB_HOST`` 但连接/初始化失败时抛出异常，避免静默回退到 SQLite。
    注意：SQLite 不扛并发，并发时会报：(sqlite3.OperationalError) disk I/O error
    """
    global _shared_postgresql_checkpoint_engine
    if not is_enterprise():
        return None
    if _shared_postgresql_checkpoint_engine is not None:
        return _shared_postgresql_checkpoint_engine
    db_host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not db_host:
        return None
    try:
        from sqlalchemy import text
        from openjiuwen_runtime.foundation.db.postgresql_handler import PostgreSQLHandler

        db_port = os.getenv("GATEWAY_DB_PORT", "5432").strip()
        db_user = os.getenv("GATEWAY_DB_USER", "postgres").strip()
        db_password = os.getenv("GATEWAY_DB_PASSWORD", "").strip()
        db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip()
        pg_schema = os.getenv("GATEWAY_PG_SCHEMA", "public").strip() or "public"
        handler = PostgreSQLHandler(
            host=db_host,
            port=int(db_port),
            database=db_name,
            schema=pg_schema,
            user=db_user,
            password=db_password,
        )
        # init_database 幂等：确保目标库与 schema 存在（schema 存在是 search_path 生效的前提）
        await handler.init_database()
        await handler.connect()
        engine = handler.get_engine()
        logger.info(
            "[JiuWenSwarmDeepAdapter] checkpoint PostgreSQL engine created: %s:%s/%s schema=%s",
            db_host,
            db_port,
            db_name,
            pg_schema,
        )

        # 确保 kv_store.value 列为 TEXT，避免 checkpoint 序列化数据被截断
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_catalog = :db AND table_schema = :schema "
                    "AND table_name = 'kv_store' AND column_name = 'value'"
                ),
                {"db": db_name, "schema": pg_schema},
            )
            row = result.fetchone()
            if row is None:
                await conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS kv_store ("
                        "key VARCHAR(512) PRIMARY KEY,"
                        "value TEXT NOT NULL"
                        ")"
                    )
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store table created with value TEXT"
                )
            elif str(row[0]).lower() != "text":
                await conn.execute(
                    text("ALTER TABLE kv_store ALTER COLUMN value TYPE TEXT")
                )
                logger.info(
                    "[JiuWenSwarmDeepAdapter] kv_store.value altered to TEXT"
                )

        _shared_postgresql_checkpoint_engine = engine
        return engine
    except Exception as exc:
        logger.error(
            "[JiuWenSwarmDeepAdapter] failed to create checkpoint PostgreSQL engine: %s",
            exc,
        )
        raise


async def ensure_persistent_checkpointer() -> None:
    """Ensure the process-wide default checkpointer uses sqlite / MySQL / PostgreSQL persistence."""
    global _PERSISTENT_CHECKPOINTER_READY, _shared_checkpoint_checkpointer

    if _PERSISTENT_CHECKPOINTER_READY:
        if _shared_checkpoint_checkpointer is not None:
            CheckpointerFactory.set_default_checkpointer(_shared_checkpoint_checkpointer)
        return

    t_cp_start = time.perf_counter()
    lock = await _get_persistent_checkpointer_lock()
    acquired = False
    try:
        try:
            await lock.acquire()
        except RuntimeError as acquire_exc:
            logger.exception(
                "[JiuWenSwarmDeepAdapter] _PERSISTENT_CHECKPOINTER_LOCK acquire failed: %s "
                "(lock repr=%r, running_loop=%r)",
                acquire_exc,
                lock,
                asyncio.get_event_loop(),
            )
            raise
        acquired = True
        if _PERSISTENT_CHECKPOINTER_READY:
            if _shared_checkpoint_checkpointer is not None:
                CheckpointerFactory.set_default_checkpointer(_shared_checkpoint_checkpointer)
            return

        try:
            PersistenceCheckpointerProvider()
            checkpoint_path = get_checkpoint_dir()
            conf: dict[str, Any] = {
                "db_type": "sqlite",
                "db_path": f"{checkpoint_path}/checkpoint",
            }

            # 企业版：CHECKPOINT_DB_TYPE=mysql|postgresql 时改用对应库（复用 GATEWAY_DB_*）
            checkpoint_db_type = os.getenv("CHECKPOINT_DB_TYPE", "").strip().lower()
            if is_enterprise():
                from openjiuwen_runtime.foundation.db.utils import is_mysql, is_postgresql
                if is_mysql(checkpoint_db_type):
                    mysql_engine = await _build_mysql_async_engine()
                    if mysql_engine is not None:
                        conf["db_client"] = mysql_engine
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] use mysql db_client for checkpointer"
                        )
                elif is_postgresql(checkpoint_db_type):
                    postgresql_engine = await _build_postgresql_async_engine()
                    if postgresql_engine is not None:
                        conf["db_client"] = postgresql_engine
                        logger.info(
                            "[JiuWenSwarmDeepAdapter] use postgresql db_client for checkpointer"
                        )

            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf=conf),
            )
            # [PERF] 打点:包一层计时,定位每轮 checkpoint restore/save 的真实耗时
            import functools as _perf_ft

            def _perf_wrap_cp(_name, _fn):
                if not asyncio.iscoroutinefunction(_fn):
                    return _fn

                @_perf_ft.wraps(_fn)
                async def _timed(*args, **kwargs):
                    _t0 = time.perf_counter()
                    try:
                        return await _fn(*args, **kwargs)
                    finally:
                        logger.info(
                            "[PERF] checkpoint.%s ms=%.1f",
                            _name,
                            (time.perf_counter() - _t0) * 1000,
                        )

                return _timed

            for _cp_name in dir(checkpointer):
                if _cp_name.startswith("_"):
                    continue
                try:
                    _cp_attr = getattr(checkpointer, _cp_name)
                except Exception:  # noqa: BLE001
                    continue
                if callable(_cp_attr):
                    try:
                        setattr(checkpointer, _cp_name, _perf_wrap_cp(_cp_name, _cp_attr))
                    except Exception:  # noqa: BLE001
                        pass
            _shared_checkpoint_checkpointer = checkpointer
            CheckpointerFactory.set_default_checkpointer(checkpointer)
            _PERSISTENT_CHECKPOINTER_READY = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] persistent checkpointer ready: %s elapsed_ms=%.1f",
                checkpoint_path / "checkpoint",
                (time.perf_counter() - t_cp_start) * 1000,
            )
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] fail to setup checkpoint due to: %s "
                "elapsed_ms=%.1f",
                exc,
                (time.perf_counter() - t_cp_start) * 1000,
            )
            raise RuntimeError("persistent checkpointer initialization failed") from exc
    finally:
        if acquired:
            lock.release()
