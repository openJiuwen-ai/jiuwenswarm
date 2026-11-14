# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process persistence lifecycle without agent-mode or optional Rail imports."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any
from urllib.parse import quote

from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.sqlite_config import normalize_journal_mode, workspace_sqlite_journal_mode
from jiuwenswarm.common.utils import get_checkpoint_dir
from jiuwenswarm.enterprise import is_enterprise

logger = logging.getLogger(__name__)
_LOG = "[checkpointer]"

_PERSISTENT_CHECKPOINTER_LOCK: asyncio.Lock | None = None
_PERSISTENT_CHECKPOINTER_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_PERSISTENT_CHECKPOINTER_LOCK_INIT = threading.Lock()
_PERSISTENT_CHECKPOINTER_READY = False
_shared_checkpointer: Any = None
_shared_engine: Any = None
_shared_engine_kind: str | None = None


def _env_int(*names: str, default: int) -> int:
    for name in names:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return default


def _gateway_db_pool_kwargs() -> dict[str, Any]:
    return {
        "pool_size": _env_int("GATEWAY_DB_POOL_SIZE", "RUNTIME_DB_POOL_SIZE", default=2),
        "max_overflow": _env_int(
            "GATEWAY_DB_MAX_OVERFLOW", "RUNTIME_DB_MAX_OVERFLOW", default=20
        ),
        "pool_timeout": _env_int(
            "GATEWAY_DB_POOL_TIMEOUT", "RUNTIME_DB_POOL_TIMEOUT", default=30
        ),
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }


def _gateway_db_creds(*, default_port: str, default_user: str) -> tuple[str, str, str, str, str]:
    return (
        os.getenv("GATEWAY_DB_HOST", "").strip(),
        os.getenv("GATEWAY_DB_PORT", default_port).strip() or default_port,
        os.getenv("GATEWAY_DB_USER", default_user).strip() or default_user,
        os.getenv("GATEWAY_DB_PASSWORD", "").strip(),
        os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip() or "openjiuwen_gateway",
    )


async def _get_persistent_checkpointer_lock() -> asyncio.Lock:
    """Lazy-init / rebind process-wide lock to the running event loop."""
    global _PERSISTENT_CHECKPOINTER_LOCK, _PERSISTENT_CHECKPOINTER_LOCK_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError(
            "_get_persistent_checkpointer_lock must be called from a running event loop"
        ) from exc
    with _PERSISTENT_CHECKPOINTER_LOCK_INIT:
        bound = _PERSISTENT_CHECKPOINTER_LOCK_LOOP
        if _PERSISTENT_CHECKPOINTER_LOCK is None or bound is None or bound is not current:
            if bound is not None and bound is not current and not bound.is_closed():
                logger.warning("%s checkpointer lock rebound: %r -> %r", _LOG, bound, current)
            _PERSISTENT_CHECKPOINTER_LOCK = asyncio.Lock()
            _PERSISTENT_CHECKPOINTER_LOCK_LOOP = current
    return _PERSISTENT_CHECKPOINTER_LOCK


async def _build_mysql_engine():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    host, port, user, password, db_name = _gateway_db_creds(
        default_port="3306", default_user="root"
    )
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}"
    # Best-effort: least-privilege accounts often lack CREATE DATABASE even when
    # the target DB already exists (DBA-provisioned). Failures here must not
    # block connecting to the configured database.
    temp = create_async_engine(
        f"mysql+aiomysql://{auth}@{host}:{port}?charset=utf8mb4", echo=False
    )
    try:
        try:
            async with temp.begin() as conn:
                await conn.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
        except Exception as exc:
            logger.warning(
                "%s skip CREATE DATABASE for %s (will use existing DB): %s",
                _LOG,
                db_name,
                exc,
            )
    finally:
        await temp.dispose()

    engine = create_async_engine(
        f"mysql+aiomysql://{auth}@{host}:{port}/{db_name}?charset=utf8mb4",
        **_gateway_db_pool_kwargs(),
    )
    logger.info("%s checkpoint MySQL ready: %s:%s/%s", _LOG, host, port, db_name)
    return engine


async def _build_postgresql_engine():
    from openjiuwen_runtime.foundation.db.postgresql_handler import PostgreSQLHandler

    host, port, user, password, db_name = _gateway_db_creds(
        default_port="5432", default_user="postgres"
    )
    schema = os.getenv("GATEWAY_PG_SCHEMA", "public").strip() or "public"
    handler = PostgreSQLHandler(
        host=host,
        port=int(port),
        database=db_name,
        schema=schema,
        user=user,
        password=password,
    )
    # Best-effort ensure DB/schema exist; connect() below remains hard-fail.
    try:
        await handler.init_database()
    except Exception as exc:
        logger.warning(
            "%s skip PostgreSQL init_database for %s schema=%s "
            "(will use existing DB): %s",
            _LOG,
            db_name,
            schema,
            exc,
        )
    await handler.connect()
    logger.info(
        "%s checkpoint PostgreSQL ready: %s:%s/%s schema=%s",
        _LOG,
        host,
        port,
        db_name,
        schema,
    )
    return handler.get_engine()


async def _enterprise_db_client() -> tuple[str, Any] | None:
    """Shared MySQL/PG engine for enterprise checkpoint.

    Non-enterprise returns ``None`` (caller uses local SQLite). Enterprise requires
    ``GATEWAY_DB_HOST`` and a recognized ``CHECKPOINT_DB_TYPE`` / ``GATEWAY_DB_TYPE``;
    misconfiguration fails fast instead of falling back to SQLite.

    Returns ``(kind, engine)`` where kind is ``mysql`` or ``postgresql``.
    """
    global _shared_engine, _shared_engine_kind
    if not is_enterprise():
        return None
    if _shared_engine is not None and _shared_engine_kind is not None:
        return _shared_engine_kind, _shared_engine
    if not os.getenv("GATEWAY_DB_HOST", "").strip():
        raise RuntimeError(
            "enterprise mode requires GATEWAY_DB_HOST for shared checkpoint storage; "
            "refusing to fall back to local SQLite"
        )

    from openjiuwen_runtime.foundation.db.utils import is_mysql, is_postgresql

    # CHECKPOINT_DB_TYPE overrides; otherwise align with enterprise deploy (GATEWAY_DB_TYPE).
    db_type = (
        os.getenv("CHECKPOINT_DB_TYPE") or os.getenv("GATEWAY_DB_TYPE") or ""
    ).strip().lower()
    try:
        if is_mysql(db_type):
            kind, engine = "mysql", await _build_mysql_engine()
        elif is_postgresql(db_type):
            kind, engine = "postgresql", await _build_postgresql_engine()
        else:
            raise RuntimeError(
                "enterprise mode requires a recognized checkpoint db type "
                f"(CHECKPOINT_DB_TYPE / GATEWAY_DB_TYPE), got {db_type!r}; "
                "refusing to fall back to local SQLite"
            )
    except Exception as exc:
        logger.error("%s failed to create checkpoint %s engine: %s", _LOG, db_type or "db", exc)
        raise

    _shared_engine = engine
    _shared_engine_kind = kind
    logger.info("%s use %s db_client for checkpointer", _LOG, kind)
    return kind, engine


async def _dispose_engine(engine: Any) -> None:
    dispose = getattr(engine, "dispose", None)
    if callable(dispose):
        await dispose()


async def _rollback_shared_engine(db_client: Any | None = None) -> None:
    """Drop a half-initialized enterprise engine so the next ensure can recreate it."""
    global _shared_engine, _shared_engine_kind
    engine = db_client if db_client is not None else _shared_engine
    _shared_engine = None
    _shared_engine_kind = None
    if engine is None:
        return
    try:
        await _dispose_engine(engine)
    except Exception as exc:
        logger.warning("%s failed to dispose rolled-back checkpoint engine: %s", _LOG, exc)


async def ensure_persistent_checkpointer() -> None:
    """Ensure process-wide checkpointer (sqlite, or enterprise MySQL/PostgreSQL)."""
    global _PERSISTENT_CHECKPOINTER_READY, _shared_checkpointer

    if _PERSISTENT_CHECKPOINTER_READY:
        if _shared_checkpointer is not None:
            CheckpointerFactory.set_default_checkpointer(_shared_checkpointer)
        return

    lock = await _get_persistent_checkpointer_lock()
    try:
        await lock.acquire()
    except RuntimeError as exc:
        logger.exception("%s checkpointer lock acquire failed: %s", _LOG, exc)
        raise

    try:
        if _PERSISTENT_CHECKPOINTER_READY:
            if _shared_checkpointer is not None:
                CheckpointerFactory.set_default_checkpointer(_shared_checkpointer)
            return

        db_client: Any | None = None
        try:
            PersistenceCheckpointerProvider()
            path = get_checkpoint_dir()
            conf: dict[str, Any] = {
                "db_type": "sqlite",
                "db_path": f"{path}/checkpoint",
            }
            config = get_config() or {}
            checkpoint_config = config.get("checkpoint") or {}
            journal_mode = checkpoint_config.get("sqlite_journal_mode")
            if journal_mode is None:
                journal_mode = workspace_sqlite_journal_mode(config)
            if journal_mode is not None:
                conf["db_journal_mode"] = normalize_journal_mode(journal_mode)

            enterprise = await _enterprise_db_client()
            backend = "sqlite"
            if enterprise is not None:
                backend, db_client = enterprise
                conf["db_client"] = db_client

            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(type="persistence", conf=conf),
            )
            from jiuwenswarm.runtime.context_checkpointer import ContextCheckpointer

            checkpointer = ContextCheckpointer(checkpointer)
            _shared_checkpointer = checkpointer
            CheckpointerFactory.set_default_checkpointer(checkpointer)
            _PERSISTENT_CHECKPOINTER_READY = True
            if backend == "sqlite":
                logger.info(
                    "%s persistent checkpointer ready: %s",
                    _LOG,
                    path / "checkpoint",
                )
            elif backend == "postgresql":
                host, port, _, _, db_name = _gateway_db_creds(
                    default_port="5432", default_user="postgres"
                )
                schema = os.getenv("GATEWAY_PG_SCHEMA", "public").strip() or "public"
                logger.info(
                    "%s persistent checkpointer ready: postgresql %s:%s/%s schema=%s",
                    _LOG,
                    host,
                    port,
                    db_name,
                    schema,
                )
            else:
                host, port, _, _, db_name = _gateway_db_creds(
                    default_port="3306", default_user="root"
                )
                logger.info(
                    "%s persistent checkpointer ready: %s %s:%s/%s",
                    _LOG,
                    backend,
                    host,
                    port,
                    db_name,
                )
        except Exception as exc:
            await _rollback_shared_engine(db_client)
            logger.error("%s fail to setup checkpoint due to: %s", _LOG, exc)
            raise RuntimeError("persistent checkpointer initialization failed") from exc
    finally:
        lock.release()


async def close_persistent_checkpointer() -> None:
    """Dispose the process-owned persistent checkpointer connection pool."""
    global _PERSISTENT_CHECKPOINTER_READY, _shared_checkpointer, _shared_engine
    global _shared_engine_kind

    # Also clean half-init leaks where create failed after caching _shared_engine.
    if not _PERSISTENT_CHECKPOINTER_READY and _shared_engine is None:
        return

    async with await _get_persistent_checkpointer_lock():
        if not _PERSISTENT_CHECKPOINTER_READY and _shared_engine is None:
            return

        kv_engine = getattr(
            getattr(CheckpointerFactory.get_checkpointer(), "_kv_store", None),
            "engine",
            None,
        )
        owned_engine = _shared_engine
        dispose_error: BaseException | None = None
        reset_error: BaseException | None = None
        try:
            if kv_engine is not None:
                await _dispose_engine(kv_engine)
            # Enterprise path owns the engine; dispose if provider did not expose it
            # via _kv_store.engine (or expose a different object).
            if owned_engine is not None and owned_engine is not kv_engine:
                await _dispose_engine(owned_engine)
        except BaseException as exc:
            dispose_error = exc
        try:
            CheckpointerFactory.set_default_checkpointer(None)
        except BaseException as exc:
            reset_error = exc
        finally:
            # Even partial dispose must force the next startup through init.
            _PERSISTENT_CHECKPOINTER_READY = False
            _shared_checkpointer = None
            _shared_engine = None
            _shared_engine_kind = None

        if dispose_error is not None:
            if reset_error is not None:
                logger.warning(
                    "%s checkpointer factory reset failed while preserving dispose error: %s",
                    _LOG,
                    reset_error,
                )
            raise dispose_error
        if reset_error is not None:
            raise reset_error
        logger.info("%s persistent checkpointer closed", _LOG)
