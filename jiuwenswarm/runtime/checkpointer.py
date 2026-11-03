# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process persistence lifecycle without agent-mode or optional Rail imports."""
from __future__ import annotations

import asyncio
import logging
import threading

from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.sqlite_config import normalize_journal_mode, workspace_sqlite_journal_mode
from jiuwenswarm.common.utils import get_checkpoint_dir

logger = logging.getLogger(__name__)

_PERSISTENT_CHECKPOINTER_LOCK: asyncio.Lock | None = None
_PERSISTENT_CHECKPOINTER_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_PERSISTENT_CHECKPOINTER_LOCK_INIT = threading.Lock()
_PERSISTENT_CHECKPOINTER_READY = False


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


async def ensure_persistent_checkpointer() -> None:
    """Ensure the process-wide default checkpointer uses sqlite persistence."""
    global _PERSISTENT_CHECKPOINTER_READY

    if _PERSISTENT_CHECKPOINTER_READY:
        return

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
            return

        try:
            PersistenceCheckpointerProvider()
            checkpoint_path = get_checkpoint_dir()
            conf = {"db_type": "sqlite", "db_path": f"{checkpoint_path}/checkpoint"}
            config = get_config() or {}
            checkpoint_config = config.get("checkpoint") or {}
            journal_mode = checkpoint_config.get("sqlite_journal_mode")
            if journal_mode is None:
                journal_mode = workspace_sqlite_journal_mode(config)
            if journal_mode is not None:
                conf["db_journal_mode"] = normalize_journal_mode(journal_mode)
            checkpointer = await CheckpointerFactory.create(
                CheckpointerConfig(
                    type="persistence",
                    conf=conf,
                ),
            )
            CheckpointerFactory.set_default_checkpointer(checkpointer)
            _PERSISTENT_CHECKPOINTER_READY = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] persistent checkpointer ready: %s",
                checkpoint_path / "checkpoint",
            )
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] fail to setup checkpoint due to: %s",
                exc,
            )
            raise RuntimeError("persistent checkpointer initialization failed") from exc
    finally:
        if acquired:
            lock.release()


async def close_persistent_checkpointer() -> None:
    """Dispose the process-owned persistent checkpointer connection pool."""
    global _PERSISTENT_CHECKPOINTER_READY

    if not _PERSISTENT_CHECKPOINTER_READY:
        return
    lock = await _get_persistent_checkpointer_lock()
    async with lock:
        if not _PERSISTENT_CHECKPOINTER_READY:
            return
        checkpointer = CheckpointerFactory.get_checkpointer()
        kv_store = getattr(checkpointer, "_kv_store", None)
        engine = getattr(kv_store, "engine", None)
        dispose = getattr(engine, "dispose", None)
        dispose_error: BaseException | None = None
        try:
            if callable(dispose):
                await dispose()
        except BaseException as exc:
            dispose_error = exc

        reset_error: BaseException | None = None
        try:
            CheckpointerFactory.set_default_checkpointer(None)
        except BaseException as exc:
            reset_error = exc
        finally:
            # READY describes whether the currently installed factory value is
            # safe to reuse.  Even a failed/partial dispose must force the next
            # Runtime startup through initialization instead of reusing a
            # potentially closed pool.
            _PERSISTENT_CHECKPOINTER_READY = False

        if dispose_error is not None:
            if reset_error is not None:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] checkpointer factory reset failed "
                    "while preserving dispose error: %s",
                    reset_error,
                )
            raise dispose_error
        if reset_error is not None:
            raise reset_error
        logger.info("[JiuWenSwarmDeepAdapter] persistent checkpointer closed")
