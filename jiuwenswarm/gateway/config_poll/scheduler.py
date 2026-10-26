# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import asyncio
import logging
import os

from jiuwenswarm.common.config import get_config

from .db import POLL_TABLES
from .syncer import ConfigPollSyncer

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 10.0

_scheduler: ConfigPollScheduler | None = None


def _parse_bool(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def config_poll_enabled() -> bool:
    if not is_enterprise():
        return False
    env = os.getenv("GATEWAY_CONFIG_POLL_ENABLED")
    if env is not None and str(env).strip():
        return _parse_bool(env, default=True)
    poll = (get_config().get("gateway") or {}).get("config_poll") or {}
    if isinstance(poll, dict) and "enabled" in poll:
        return _parse_bool(poll.get("enabled"), default=True)
    return True


def config_poll_interval_seconds() -> float:
    env = os.getenv("GATEWAY_CONFIG_POLL_INTERVAL_SECONDS", "").strip()
    if env:
        return max(1.0, float(env))
    poll = (get_config().get("gateway") or {}).get("config_poll") or {}
    if isinstance(poll, dict) and poll.get("interval_seconds") is not None:
        return max(1.0, float(poll["interval_seconds"]))
    return DEFAULT_POLL_INTERVAL_SECONDS


class ConfigPollScheduler:
    """周期拉取共享库配置；启停模式与 ``GatewayHeartbeatService`` 一致。"""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self._enabled = config_poll_enabled() if enabled is None else enabled
        self._interval_seconds = (
            config_poll_interval_seconds()
            if interval_seconds is None
            else max(1.0, float(interval_seconds))
        )
        self._syncer = ConfigPollSyncer()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[ConfigPoll] disabled; scheduler not started")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="gateway-config-poll")
        logger.info(
            "[ConfigPoll] scheduler started interval=%ss tables=%s",
            self._interval_seconds,
            ",".join(POLL_TABLES),
        )

    async def stop(self) -> None:
        self._running = False
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("[ConfigPoll] scheduler stopped")

    async def run_once(self) -> None:
        await self._syncer.run_once()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._syncer.run_once()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("[ConfigPoll] poll loop iteration failed")
            if not self._running:
                break
            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                break


def get_config_poll_scheduler() -> ConfigPollScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ConfigPollScheduler()
    return _scheduler
