# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded default preparation of one profile's shared model resources.

This never creates a Session, starts an Agent interaction, or sends a model
request. A ready profile is not EXECUTION_MIN_READY. Budget observations include
concurrent process work; exceeding one disables retries for that configuration.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.runtime.model_observation import bind_model_observer, unbind_model_observer

logger = logging.getLogger(__name__)
_ACTIVE_PREWARM: ContextVar[ProfilePrewarmer | None] = ContextVar("worker_profile_prewarm", default=None)


class PreparationDeferred(RuntimeError):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


async def prewarm_checkpoint() -> None:
    """Stop subsequent optional steps; never interrupt an accepted sync import."""
    controller = _ACTIVE_PREWARM.get()
    if controller is not None:
        await controller.checkpoint()


def _number(config: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value)) if math.isfinite(value) else default


def _options(config: dict) -> dict:
    raw = config.get("runtime_prewarm")
    return {"enabled": True, **(raw if isinstance(raw, dict) else {})}


def _identity(config: dict) -> str:
    from jiuwenswarm.runtime.kernel import identity_reference

    return identity_reference(config)


def _rss() -> int:
    import psutil

    return psutil.Process().memory_info().rss


class ProfilePrewarmer:
    """One task, one profile, finite retries per effective configuration."""

    def __init__(self, *, get_config: Callable[[], dict],
                 prepare: Callable[[str, str, dict], Awaitable[Any]],
                 admission: Any, stop_event: asyncio.Event) -> None:
        self._get_config = get_config
        self._prepare = prepare
        self._admission = admission
        self._stop = stop_event
        self._identity = ""
        self._mode = ""
        self._channel = ""
        self._attempts = 0
        self._state = "disabled"
        self._metrics: dict[str, Any] = {}
        self._hits = 0
        self._warm_model: Any = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._budget: tuple[dict, float, float, int] | None = None
        self._foreground_tokens: set[object] = set()
        self._foreground_idle = asyncio.Event()
        self._foreground_idle.set()

    def pause_background(self) -> object:
        """Keep this optional profile behind the complete foreground turn."""
        token = object()
        self._foreground_tokens.add(token)
        self._foreground_idle.clear()
        return token

    def resume_background(self, token: object) -> None:
        self._foreground_tokens.discard(token)
        if not self._foreground_tokens:
            self._foreground_idle.set()

    @property
    def foreground_paused(self) -> bool:
        return bool(self._foreground_tokens)

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("profile prewarmer is closed")
        # Disabled installations have no polling task and import no model code.
        if _options(self._get_config()).get("enabled") is not True:
            return
        if self._task is None or self._task.done():
            bind_model_observer(self)
            self._task = asyncio.create_task(self._run(), name="profile-prewarm")
            self._task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._state = "failed"
            logger.warning("[Worker] profile prewarm stopped: %s", type(error).__name__)

    def snapshot(self) -> dict[str, Any]:
        return {"state": self._state, "identity": self._identity,
                "mode": self._mode, "channel_id": self._channel,
                "attempts": self._attempts, "hits": self._hits,
                "foreground_paused": self.foreground_paused, **self._metrics}

    def note_use(self, *, mode: str, channel_id: str, config: dict) -> bool:
        from gateway.common.mode_matrix import is_code_profile_mode, is_team_mode

        profile = "code" if is_code_profile_mode(mode) else "agent"
        hit = (not self._closed and self._state == "ready" and profile == self._mode
               and channel_id == self._channel and _identity(config) == self._identity
               and _options(config).get("enabled") is True and not is_team_mode(mode))
        logger.info("[Worker] profile_ready_at_request=%s mode=%s channel_id=%s",
                    hit, profile, channel_id)
        return hit

    def note_model_use(self, model: Any) -> bool:
        if self._closed or self._state != "ready" or self._warm_model is None or model is not self._warm_model:
            return False
        config = self._get_config()
        hit = _options(config).get("enabled") is True and _identity(config) == self._identity
        if hit:
            self._hits += 1
            logger.info("[Worker] profile_prewarm_resource_hit=true mode=%s", self._mode)
        return hit

    async def _run(self) -> None:
        await self._admission.wait_minimum_ready()
        while not self._stop.is_set():
            config = self._get_config()
            options = _options(config)
            if options.get("enabled") is not True:
                self._state = "disabled"
                self._identity = ""
                self._warm_model = None
                await asyncio.sleep(1)
                continue
            identity = _identity(config)
            if identity != self._identity:
                self._identity = identity
                self._attempts = 0
                self._state = "pending"
                self._metrics = {}
                self._warm_model = None
            self._mode = str(options.get("mode", "agent"))
            self._channel = str(options.get("channel_id", "web"))
            if self._mode not in {"agent", "code"} or self._channel not in {"web", "tui", "default"}:
                self._state = "invalid_config"
            limit = int(_number(options, "max_attempts", 2, 1, 2))
            if self._state in {"ready", "budget_exceeded", "invalid_config"} or self._attempts >= limit:
                await asyncio.sleep(1)
                continue
            delay = _number(options, "idle_delay_seconds", 1, 0, 60)
            if self._attempts:
                delay = _number(options, "retry_delay_seconds", 5, 0, 60)
            await asyncio.sleep(delay)
            await self._foreground_idle.wait()
            if self._stop.is_set() or _identity(self._get_config()) != identity:
                continue
            async with self._admission.background_slot():
                # Admission can wait behind foreground work/config updates.
                if (self._stop.is_set() or self.foreground_paused
                        or _identity(self._get_config()) != identity):
                    continue
                await self._attempt(config, options, identity)

    async def _attempt(self, config: dict, options: dict, identity: str) -> None:
        self._attempts += 1
        self._state = "warming"
        self._metrics = {}
        began, cpu = time.monotonic(), time.process_time()
        self._metrics["attempt_started_at_ns"] = time.perf_counter_ns()
        before = _rss()
        self._budget = options, began, cpu, before
        token = _ACTIVE_PREWARM.set(self)
        try:
            timeout = _number(options, "timeout_seconds", 10, 0.1, 10)
            async with asyncio.timeout(timeout):
                await self.checkpoint()
                # AgentManager borrower pins belong to this finite attempt,
                # rather than to the controller's polling lifetime.
                model = await asyncio.create_task(self._prepare(self._mode, self._channel, config),
                                                  name="profile-prewarm-attempt")
                await self.checkpoint()
            self._state = "ready" if _identity(self._get_config()) == identity else "stale"
            self._warm_model = model if self._state == "ready" else None
        except asyncio.CancelledError:
            self._state = "cancelled"
            raise
        except PreparationDeferred as exc:
            self._state = exc.state
            self._warm_model = None
        except Exception as exc:  # noqa: BLE001 - optional work must not fail Runtime
            self._state = "failed"
            self._metrics["error_type"] = type(exc).__name__
            logger.warning("[Worker] profile prewarm attempt %d failed: %s",
                           self._attempts, type(exc).__name__)
        finally:
            _ACTIVE_PREWARM.reset(token)
            self._budget = None
            elapsed = time.monotonic() - began
            cpu_elapsed = time.process_time() - cpu
            delta_mb = max(0, _rss() - before) / (1024 * 1024)
            self._metrics.update(wall_ms=elapsed * 1000, cpu_ms=cpu_elapsed * 1000,
                                 rss_delta_mb=delta_mb, attempt_completed_at_ns=time.perf_counter_ns())
            if self._state not in {"cancelled", "stale"} and (
                cpu_elapsed > _number(options, "max_cpu_seconds", 5, 0.001, 5)
                or delta_mb > _number(options, "max_rss_delta_mb", 256, 0.001, 256)
                or elapsed > _number(options, "timeout_seconds", 10, 0.1, 10)
            ):
                self._state = "budget_exceeded"
            logger.info("[Worker] profile_prewarm=%s", self.snapshot())

    async def checkpoint(self) -> None:
        await asyncio.sleep(0)
        if self._closed or self._stop.is_set():
            raise asyncio.CancelledError()
        if _identity(self._get_config()) != self._identity:
            raise PreparationDeferred("stale")
        if self._budget is not None:
            options, began, cpu, before = self._budget
            if (time.monotonic() - began > _number(options, "timeout_seconds", 10, .1, 10)
                    or time.process_time() - cpu > _number(options, "max_cpu_seconds", 5, .001, 5)
                    or max(0, _rss() - before) / (1024 * 1024)
                    > _number(options, "max_rss_delta_mb", 256, .001, 256)):
                raise PreparationDeferred("budget_exceeded")
        if self.foreground_paused or self._admission.background_paused:
            raise PreparationDeferred("yielded_to_foreground")

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._warm_model = None
        self._foreground_tokens.clear()
        self._foreground_idle.set()
        unbind_model_observer(self)
