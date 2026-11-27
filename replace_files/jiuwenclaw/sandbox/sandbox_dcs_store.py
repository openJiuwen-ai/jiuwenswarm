from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from jiuwenclaw.dcs import DcsClusterClient, DcsClusterConfig, load_config_from_env
from jiuwenclaw.sandbox.open_ability import OpenAbilityEndpoint
from jiuwenclaw.utils import logger

# Backward-compatible alias for existing imports.
SandboxDcsConfig = DcsClusterConfig
_SANDBOX_DURATION_SECONDS_ENV = "SANDBOX_DURATION_SECONDS"
_SANDBOX_DCS_TTL_SECONDS_ENV = "SANDBOX_DCS_TTL_SECONDS"
_SANDBOX_DEFAULT_DURATION_SECONDS = 3600


def _metadata_ttl_seconds() -> int:
    raw_ttl = os.environ.get(_SANDBOX_DCS_TTL_SECONDS_ENV, "").strip()
    if raw_ttl:
        return max(0, int(raw_ttl))
    raw_duration = os.environ.get(_SANDBOX_DURATION_SECONDS_ENV, "").strip()
    if raw_duration:
        return max(0, int(raw_duration))
    return _SANDBOX_DEFAULT_DURATION_SECONDS


@dataclass(frozen=True)
class SandboxDcsRecord:
    sandbox_id: str
    api_key_sha256: str


class SandboxDcsStore:
    """Persist sandbox metadata to Huawei DCS (Redis Cluster) via redis-py."""

    def __init__(self, config: SandboxDcsConfig) -> None:
        self._config = config
        self._ttl_seconds = _metadata_ttl_seconds()
        self._dcs = DcsClusterClient(config)

    @classmethod
    def from_env(cls) -> SandboxDcsStore:
        config = load_config_from_env(
            required=True,
            missing_host_error=(
                "SANDBOX_DCS_HOST environment variable is required when sandbox routing is enabled"
            ),
        )
        assert config is not None
        return cls(config)

    @staticmethod
    def _api_key_key(sandbox_id: str) -> str:
        return f"jiuwen:sandboxApiKey:{sandbox_id}"

    @staticmethod
    def _sandbox_to_oa_key(sandbox_id: str) -> str:
        return f"jiuwen:sandboxToOA:{sandbox_id}"

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_api_key_value(raw: str) -> str | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower()):
            return value.lower()
        return None

    @staticmethod
    def _parse_sandbox_to_oa_value(raw: str) -> OpenAbilityEndpoint | None:
        text = str(raw or "").strip()
        if not text or ":" not in text:
            return None
        host, port_raw = text.rsplit(":", 1)
        host = host.strip()
        port_raw = port_raw.strip()
        if not host or not port_raw:
            return None
        try:
            port = int(port_raw)
        except ValueError:
            return None
        if port <= 0 or port > 65535:
            return None
        return OpenAbilityEndpoint(host=host, port=port)

    async def close(self) -> None:
        await self._dcs.close()

    async def save_sandbox(
        self,
        sandbox_id: str,
        *,
        api_key: str,
    ) -> SandboxDcsRecord:
        api_key_sha256 = self._hash_api_key(api_key)
        record = SandboxDcsRecord(
            sandbox_id=sandbox_id,
            api_key_sha256=api_key_sha256,
        )
        key = self._api_key_key(sandbox_id)
        await self._dcs.set_with_ttl(key, api_key_sha256, ttl_seconds=self._ttl_seconds)
        logger.info("Saved sandbox API key hash to DCS: key=%s", key)
        return record

    async def refresh_sandbox_ttl(self, sandbox_id: str) -> None:
        sid = str(sandbox_id or "").strip()
        if not sid or self._ttl_seconds <= 0:
            return
        for key in (self._api_key_key(sid), self._sandbox_to_oa_key(sid)):
            if await self._dcs.expire(key, ttl_seconds=self._ttl_seconds):
                logger.debug(
                    "Refreshed sandbox DCS TTL: key=%s ttl_seconds=%d",
                    key,
                    self._ttl_seconds,
                )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self._dcs.delete(
            self._api_key_key(sandbox_id),
            self._sandbox_to_oa_key(sandbox_id),
        )

    async def get_sandbox(self, sandbox_id: str) -> SandboxDcsRecord | None:
        raw = await self._dcs.get(self._api_key_key(sandbox_id))
        if raw is None:
            return None
        api_key_sha256 = self._decode_api_key_value(raw)
        if api_key_sha256 is None:
            return None
        return SandboxDcsRecord(
            sandbox_id=sandbox_id,
            api_key_sha256=api_key_sha256,
        )

    async def get_openability_endpoint(self, sandbox_id: str) -> OpenAbilityEndpoint | None:
        raw = await self._dcs.get(self._sandbox_to_oa_key(sandbox_id))
        if raw is None:
            return None
        return self._parse_sandbox_to_oa_value(raw)
