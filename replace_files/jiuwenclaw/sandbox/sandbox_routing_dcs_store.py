from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass

from jiuwenclaw.dcs import DcsClusterClient, DcsClusterConfig, load_config_from_env
from jiuwenclaw.utils import logger

# Backward-compatible alias for tests / imports.
SandboxRoutingDcsConfig = DcsClusterConfig
SANDBOX_ROUTING_REDIS_PREFIX = "jiuwen:sandboxRouting:"


def get_gateway_instance_id() -> str:
    explicit = os.environ.get("GATEWAY_INSTANCE_ID", "").strip()
    if explicit:
        return explicit
    return f"{socket.gethostname()}:{os.getpid()}"


def sandbox_adopt_existing_enabled() -> bool:
    raw = os.environ.get("SANDBOX_ADOPT_EXISTING")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _routing_ttl_seconds() -> int:
    # priority: SANDBOX_DCS_TTL_SECONDS -> SANDBOX_DURATION_SECONDS -> 3600
    raw = os.environ.get("SANDBOX_DCS_TTL_SECONDS", "").strip()
    if raw:
        return max(0, int(raw))
    raw = os.environ.get("SANDBOX_DURATION_SECONDS", "").strip()
    if raw:
        return max(0, int(raw))
    return 3600


@dataclass(frozen=True)
class SandboxRoutingRecord:
    routing_key: str
    sandbox_id: str
    gateway_id: str
    updated_at: float


class SandboxRoutingDcsStore:
    """Persist routing_key -> sandbox_id mapping for cross-gateway failover."""

    def __init__(self, config: DcsClusterConfig) -> None:
        self._config = config
        self._ttl_seconds = _routing_ttl_seconds()
        self._dcs = DcsClusterClient(config)

    @classmethod
    def from_env(cls) -> SandboxRoutingDcsStore:
        config = load_config_from_env(
            required=True,
            missing_host_error=(
                "SANDBOX_DCS_HOST environment variable is required when sandbox routing is enabled"
            ),
        )
        assert config is not None
        return cls(config)

    async def close(self) -> None:
        await self._dcs.close()

    @staticmethod
    def _routing_key(routing_key: str) -> str:
        return f"{SANDBOX_ROUTING_REDIS_PREFIX}{routing_key}"

    @staticmethod
    def _serialize(record: SandboxRoutingRecord) -> str:
        return json.dumps(
            {
                "sandbox_id": record.sandbox_id,
                "gateway_id": record.gateway_id,
                "updated_at": record.updated_at,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize(routing_key: str, raw: str) -> SandboxRoutingRecord | None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        sandbox_id = str(data.get("sandbox_id") or "").strip()
        if not sandbox_id:
            return None
        gateway_id = str(data.get("gateway_id") or "").strip()
        try:
            updated_at = float(data.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return SandboxRoutingRecord(
            routing_key=routing_key,
            sandbox_id=sandbox_id,
            gateway_id=gateway_id,
            updated_at=updated_at,
        )

    async def get_routing(self, routing_key: str) -> SandboxRoutingRecord | None:
        key = str(routing_key or "").strip()
        if not key:
            return None
        raw = await self._dcs.get(self._routing_key(key))
        if raw is None:
            return None
        return self._deserialize(key, str(raw))

    async def save_routing(
        self,
        routing_key: str,
        *,
        sandbox_id: str,
        gateway_id: str,
    ) -> SandboxRoutingRecord:
        key = str(routing_key or "").strip()
        sid = str(sandbox_id or "").strip()
        if not key or not sid:
            raise ValueError("routing_key and sandbox_id are required")
        record = SandboxRoutingRecord(
            routing_key=key,
            sandbox_id=sid,
            gateway_id=str(gateway_id or "").strip(),
            updated_at=time.time(),
        )
        redis_routing_key = self._routing_key(key)
        payload = self._serialize(record)
        await self._dcs.set_with_ttl(
            redis_routing_key,
            payload,
            ttl_seconds=self._ttl_seconds,
        )
        logger.info(
            "Saved sandbox routing to DCS: sandbox_id=%s gateway_id=%s",
            sid,
            record.gateway_id,
        )
        return record

    async def set_routing_nx(
        self,
        routing_key: str,
        *,
        sandbox_id: str,
        gateway_id: str,
    ) -> bool:
        """Return True if this gateway claimed the routing key."""
        key = str(routing_key or "").strip()
        sid = str(sandbox_id or "").strip()
        if not key or not sid:
            raise ValueError("routing_key and sandbox_id are required")
        record = SandboxRoutingRecord(
            routing_key=key,
            sandbox_id=sid,
            gateway_id=str(gateway_id or "").strip(),
            updated_at=time.time(),
        )
        redis_routing_key = self._routing_key(key)
        payload = self._serialize(record)
        claimed = await self._dcs.set_nx_with_ttl(
            redis_routing_key,
            payload,
            ttl_seconds=self._ttl_seconds,
        )
        if not claimed:
            return False
        logger.info(
            "Claimed sandbox routing in DCS (NX): sandbox_id=%s",
            sid,
        )
        return True

    async def refresh_routing_ttl(
        self,
        routing_key: str,
        *,
        sandbox_id: str,
        gateway_id: str,
    ) -> bool:
        """Refresh routing TTL and updated_at when the sandbox is renewed."""
        key = str(routing_key or "").strip()
        sid = str(sandbox_id or "").strip()
        gid = str(gateway_id or "").strip()
        if not key or not sid or self._ttl_seconds <= 0:
            return False
        existing = await self.get_routing(key)
        if existing is None or existing.sandbox_id != sid:
            return False
        await self.save_routing(key, sandbox_id=sid, gateway_id=gid or existing.gateway_id)
        return True

    async def delete_routing(self, routing_key: str) -> None:
        key = str(routing_key or "").strip()
        if not key:
            return
        await self._dcs.delete(self._routing_key(key))
        logger.info("Deleted sandbox routing from DCS")
