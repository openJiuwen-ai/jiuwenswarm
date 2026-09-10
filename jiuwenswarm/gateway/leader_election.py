# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Distributed Leader Election using Redis SETNX + TTL for PRIMARY/STANDBY failover."""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import asyncio
import logging
import os
import uuid
from enum import Enum
from typing import Awaitable, Callable

from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)


class Role(Enum):
    """Leader election role states."""

    PRIMARY = "primary"
    STANDBY = "standby"


class LeaderElection:
    """Distributed leader election using Redis SETNX + TTL.

    Redis lock algorithm:
    - Lock acquisition: SET {instance_id}:gateway:leader <instance_id:uuid> NX EX 30
    - Lock renewal: EXPIRE {instance_id}:gateway:leader 30 (PRIMARY runs every 10s)
    - Lock release: DEL {instance_id}:gateway:leader (only if value matches)
    - Lock competition: STANDBY tries SET {instance_id}:gateway:leader <instance_id:uuid> NX EX 30 every 5s

    A single leader loop task handles both PRIMARY renewal and STANDBY competition.
    """

    DEFAULT_LOCK_KEY = "gateway:leader"
    DEFAULT_LOCK_TTL_SECONDS = 30
    DEFAULT_RENEWAL_INTERVAL_SECONDS = 10
    DEFAULT_COMPETITION_INTERVAL_SECONDS = 5

    _instance: LeaderElection | None = None

    @staticmethod
    def _with_instance_prefix(instance_id: str, lock_key: str) -> str:
        iid = str(instance_id or "").strip()
        if not iid:
            return str(lock_key or "").strip()
        key = str(lock_key or "").strip()
        if key.startswith(f"{iid}:"):
            return key
        return f"{iid}:{key}"

    def __init__(self) -> None:
        self._instance_id = self._get_default_instance_id()
        self._lock_key = self._with_instance_prefix(self._instance_id, self.DEFAULT_LOCK_KEY)
        self._lock_ttl = self.DEFAULT_LOCK_TTL_SECONDS
        self._renewal_interval = self.DEFAULT_RENEWAL_INTERVAL_SECONDS
        self._competition_interval = self.DEFAULT_COMPETITION_INTERVAL_SECONDS

        self._role: Role = Role.STANDBY
        self._lock_value: str = f"{self._instance_id}:{uuid.uuid4()}"
        self._callbacks: list[Callable[[Role], Awaitable[None]]] = []

        self._leader_task: asyncio.Task | None = None
        self._running = False
        self._enabled = False

        self._load_config()

    @staticmethod
    def _get_default_instance_id() -> str:
        """Get instance id from gateway.instance_id / JIUWENCLAW_ID / hostname."""
        try:
            config = get_config()
            gateway_config = config.get("gateway", {}) if isinstance(config, dict) else {}
            instance_id = gateway_config.get("instance_id", "")
            if instance_id and str(instance_id).strip():
                return str(instance_id).strip()
        except Exception:  # noqa: BLE001
            pass
        env_id = os.getenv("JIUWENCLAW_ID", "").strip()
        if env_id:
            return env_id
        import socket

        try:
            return socket.gethostname()
        except Exception:  # noqa: BLE001
            return "unknown"

    @classmethod
    def get_instance(cls) -> LeaderElection:
        """Get singleton instance. Creates from config on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_config(self) -> None:
        """Load config from config.yaml leader_election section."""
        try:
            config = get_config()
        except Exception:  # noqa: BLE001
            config = {}
        le_config = config.get("leader_election", {}) if isinstance(config, dict) else {}
        if not isinstance(le_config, dict):
            le_config = {}

        configured_lock_key = str(le_config.get("lock_key") or self.DEFAULT_LOCK_KEY)
        self._lock_key = self._with_instance_prefix(self._instance_id, configured_lock_key)
        self._lock_ttl = int(
            le_config.get("lock_ttl_seconds", self.DEFAULT_LOCK_TTL_SECONDS) or self.DEFAULT_LOCK_TTL_SECONDS
        )
        self._renewal_interval = int(
            le_config.get("renewal_interval_seconds", self.DEFAULT_RENEWAL_INTERVAL_SECONDS)
            or self.DEFAULT_RENEWAL_INTERVAL_SECONDS
        )
        self._competition_interval = int(
            le_config.get("competition_interval_seconds", self.DEFAULT_COMPETITION_INTERVAL_SECONDS)
            or self.DEFAULT_COMPETITION_INTERVAL_SECONDS
        )

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing only)."""
        cls._instance = None

    @property
    def role(self) -> Role:
        return self._role

    @property
    def is_primary(self) -> bool:
        return self._role == Role.PRIMARY

    @property
    def instance_id(self) -> str:
        """This Gateway instance's unique identifier."""
        return self._instance_id

    @staticmethod
    def _get_redis_client():
        from jiuwenswarm.extensions.redis.redis_runtime import get_gateway_redis_client

        return get_gateway_redis_client()

    async def _acquire_lock(self) -> bool:
        """Try to acquire lock using SET NX EX."""
        try:
            redis = self._get_redis_client()
            if redis is None:
                return False
            acquired = await redis.set_nx(
                self._lock_key,
                self._lock_value,
                ttl_seconds=self._lock_ttl,
            )
            if acquired:
                logger.info("[LeaderElection] Lock acquired, role=PRIMARY")
            return acquired
        except Exception:
            logger.exception("[LeaderElection] Failed to acquire lock")
            return False

    async def _release_lock(self) -> None:
        """Release lock only if value matches."""
        try:
            redis = self._get_redis_client()
            if redis is None:
                return
            current_value = await redis.get(self._lock_key)
            if current_value is not None and current_value == self._lock_value:
                await redis.delete(self._lock_key)
                logger.info("[LeaderElection] Lock released")
            else:
                logger.debug("[LeaderElection] Lock already lost or value mismatch, skip release")
        except Exception:
            logger.exception("[LeaderElection] Failed to release lock")

    async def _renew_lock(self) -> None:
        """Renew lock TTL using EXPIRE."""
        try:
            redis = self._get_redis_client()
            if redis is None:
                await self._demote_to_standby()
                return
            current_value = await redis.get(self._lock_key)
            if current_value is not None and current_value == self._lock_value:
                await redis.expire(self._lock_key, self._lock_ttl)
                logger.debug("[LeaderElection] Lock TTL renewed: %ds", self._lock_ttl)
            else:
                logger.warning("[LeaderElection] Lock lost during renewal, demoting to STANDBY")
                await self._demote_to_standby()
        except Exception:
            logger.exception("[LeaderElection] Failed to renew lock")

    async def _on_role_change(self, new_role: Role) -> None:
        self._role = new_role
        logger.info("[LeaderElection] Role changed to: %s", new_role.value)
        for callback in self._callbacks:
            try:
                await callback(new_role)
            except Exception:
                logger.exception("[LeaderElection] Callback error")

    async def _promote_to_primary(self) -> None:
        await self._on_role_change(Role.PRIMARY)

    async def _demote_to_standby(self) -> None:
        await self._on_role_change(Role.STANDBY)

    async def _leader_loop(self) -> None:
        while self._running:
            try:
                if self._role == Role.PRIMARY:
                    await asyncio.sleep(self._renewal_interval)
                    if not self._running:
                        break
                    await self._renew_lock()
                else:
                    await asyncio.sleep(self._competition_interval)
                    if not self._running:
                        break
                    if await self._acquire_lock():
                        await self._promote_to_primary()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[LeaderElection] Leader loop error")

    def register_callback(self, callback: Callable[[Role], Awaitable[None]]) -> None:
        self._callbacks.append(callback)
        logger.debug("[LeaderElection] Callback registered, total: %d", len(self._callbacks))

    def unregister_callback(self, callback: Callable[[Role], Awaitable[None]]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)
            logger.debug("[LeaderElection] Callback unregistered, total: %d", len(self._callbacks))

    async def start(self) -> None:
        """Start LeaderElection when enterprise distributed Redis is active."""
        if self._running:
            logger.warning("[LeaderElection] Already running")
            return

        # 仅企业版 + distributed Redis 时启用
        if not is_enterprise():
            logger.info("[LeaderElection] not enterprise edition; skip")
            return

        from jiuwenswarm.extensions.redis.redis_runtime import (
            get_declared_deployment_mode,
            get_gateway_redis_client,
        )

        if get_declared_deployment_mode() != "active-standby":
            logger.info("[LeaderElection] standalone mode; skip")
            return
        if get_gateway_redis_client() is None:
            logger.warning("[LeaderElection] Redis unavailable; skip")
            return

        self._enabled = True
        self._running = True
        logger.info(
            "[LeaderElection] Starting: instance_id=%s, lock_key=%s, ttl=%ds, renewal=%ds, competition=%ds",
            self._instance_id,
            self._lock_key,
            self._lock_ttl,
            self._renewal_interval,
            self._competition_interval,
        )

        try:
            if await self._acquire_lock():
                await self._promote_to_primary()
            else:
                await self._demote_to_standby()
        except Exception:
            logger.exception("[LeaderElection] Failed to acquire initial lock")
            self._running = False
            self._enabled = False
            return

        self._leader_task = asyncio.create_task(self._leader_loop(), name="gateway-leader-election")

    async def stop(self) -> None:
        """Stop LeaderElection: cancel task, release lock, demote to STANDBY."""
        if not self._running and not self._enabled:
            return

        logger.info("[LeaderElection] Stopping")
        self._running = False

        if self._leader_task is not None:
            self._leader_task.cancel()
            try:
                await self._leader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[LeaderElection] Error awaiting leader task")
            self._leader_task = None

        if self._role == Role.PRIMARY:
            try:
                await self._release_lock()
            except Exception:
                logger.exception("[LeaderElection] Error releasing lock")
            await self._demote_to_standby()

        self._enabled = False
        logger.info("[LeaderElection] Stopped")
