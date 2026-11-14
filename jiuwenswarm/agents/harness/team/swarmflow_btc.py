# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""BackgroundTaskController control-plane adapter for swarmflow pause/resume/stop.

``dev-stable`` historically shipped a no-arg controller::

    async def pause(self) -> bool
    async def resume(self) -> bool
    def is_paused(self) -> bool
    # no stop()

Newer agent-core builds accept an optional ``run_id`` and expose ``stop``::

    async def pause(self, run_id: str | None = None) -> bool
    async def resume(self, run_id: str | None = None, *, via=None) -> bool
    async def stop(self, run_id: str | None = None) -> bool

Web buttons and team teardown call these helpers so a locked older pin does not
``TypeError`` on ``pause(run_id)`` / missing ``stop``, while a newer pin keeps
per-run targeting.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _method_accepts_run_id(method: Any) -> bool:
    """Whether ``method`` declares a ``run_id`` parameter."""
    try:
        return "run_id" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


async def _abort_handle(task_id: str, handle: Any) -> None:
    """Run the three-step abort used by legacy ``pause`` (best-effort)."""
    abort_event = getattr(handle, "abort_event", None)
    if abort_event is not None:
        abort_event.set()
    backend = getattr(handle, "backend", None)
    if backend is not None:
        abort_sessions = getattr(backend, "abort_sessions", None)
        if callable(abort_sessions):
            try:
                await abort_sessions()
            except Exception:  # noqa: BLE001 - cancel still proceeds
                logger.debug(
                    "[swarmflow_btc] abort_sessions failed for %s",
                    task_id,
                    exc_info=True,
                )
    native = getattr(handle, "native", None)
    runtime = getattr(native, "async_tool_runtime", None) if native is not None else None
    cancel = getattr(runtime, "cancel", None) if runtime is not None else None
    if callable(cancel):
        try:
            await cancel(task_id)
        except Exception:  # noqa: BLE001 - cancel is best-effort
            logger.debug(
                "[swarmflow_btc] cancel failed for %s",
                task_id,
                exc_info=True,
            )


def _registry(controller: Any, attr: str) -> dict[str, Any]:
    """Return the controller's run registry dict (create if missing).

    Do not use ``getattr(..., {}) or {}``: an empty registry is falsy and would
    bind writes to a throwaway dict instead of the controller field.
    """
    value = getattr(controller, attr, None)
    if isinstance(value, dict):
        return value
    fresh: dict[str, Any] = {}
    try:
        setattr(controller, attr, fresh)
    except AttributeError:
        # Plain object / frozen stand-in: callers see an empty registry → no-op.
        return fresh
    return fresh


async def _legacy_pause(controller: Any, run_id: str | None) -> bool:
    """No-arg BTC pause, optionally scoped to one ``run_id`` via the registry."""
    lock = getattr(controller, "_lock", None)
    active = _registry(controller, "_active")
    paused = _registry(controller, "_paused")

    async def _body() -> bool:
        if run_id is None:
            targets = dict(active)
        else:
            handle = active.get(run_id)
            if handle is None:
                return False
            targets = {run_id: handle}
        if not targets:
            return False
        for task_id, handle in list(targets.items()):
            await _abort_handle(task_id, handle)
            relaunch = getattr(handle, "relaunch", None)
            if callable(relaunch):
                paused[task_id] = relaunch
            active.pop(task_id, None)
        return True

    if lock is None:
        return await _body()
    async with lock:
        return await _body()


async def _legacy_resume(controller: Any, run_id: str | None) -> bool:
    """No-arg BTC resume, optionally scoped to one paused ``run_id``."""
    lock = getattr(controller, "_lock", None)
    paused = _registry(controller, "_paused")

    async def _body() -> bool:
        if run_id is None:
            targets = dict(paused)
        else:
            relaunch = paused.get(run_id)
            if relaunch is None:
                return False
            targets = {run_id: relaunch}
        if not targets:
            return False
        for task_id, relaunch in list(targets.items()):
            try:
                relaunch()
            except Exception:  # noqa: BLE001 - do not strand siblings
                logger.debug(
                    "[swarmflow_btc] relaunch failed for %s",
                    task_id,
                    exc_info=True,
                )
            paused.pop(task_id, None)
        return True

    if lock is None:
        return await _body()
    async with lock:
        return await _body()


async def _legacy_stop(controller: Any, run_id: str | None) -> bool:
    """Approximate ``stop`` on pins without ``BackgroundTaskController.stop``.

    Abort active handle(s) like pause, but drop relaunch tickets instead of
    parking them — matches "stop seals / discards resume" semantics. Already
    paused runs are discarded from ``_paused`` without relaunch.
    """
    lock = getattr(controller, "_lock", None)
    active = _registry(controller, "_active")
    paused = _registry(controller, "_paused")

    async def _body() -> bool:
        acted = False
        if run_id is None:
            active_targets = dict(active)
            paused_ids = list(paused.keys())
        else:
            handle = active.get(run_id)
            active_targets = {run_id: handle} if handle is not None else {}
            paused_ids = [run_id] if run_id in paused else []

        for task_id, handle in list(active_targets.items()):
            await _abort_handle(task_id, handle)
            active.pop(task_id, None)
            paused.pop(task_id, None)
            acted = True

        for task_id in paused_ids:
            if paused.pop(task_id, None) is not None:
                acted = True
        return acted

    if lock is None:
        return await _body()
    async with lock:
        return await _body()


async def pause_background_tasks(
    controller: Any,
    run_id: str | None = None,
) -> bool:
    """Pause one swarmflow run (or all when ``run_id`` is None)."""
    method = getattr(controller, "pause", None)
    if not callable(method):
        return False
    if _method_accepts_run_id(method):
        return bool(await method(run_id))
    return await _legacy_pause(controller, run_id)


async def resume_background_tasks(
    controller: Any,
    run_id: str | None = None,
) -> bool:
    """Resume one paused swarmflow run (or all when ``run_id`` is None)."""
    method = getattr(controller, "resume", None)
    if not callable(method):
        return False
    if _method_accepts_run_id(method):
        return bool(await method(run_id))
    return await _legacy_resume(controller, run_id)


async def stop_background_tasks(
    controller: Any,
    run_id: str | None = None,
) -> bool:
    """Stop one swarmflow run (or all when ``run_id`` is None).

    Prefer native ``stop`` when present; otherwise abort + drop relaunch tickets
    on the legacy registry.
    """
    method = getattr(controller, "stop", None)
    if callable(method):
        if _method_accepts_run_id(method):
            return bool(await method(run_id))
        return bool(await method())
    return await _legacy_stop(controller, run_id)


__all__ = [
    "pause_background_tasks",
    "resume_background_tasks",
    "stop_background_tasks",
]
