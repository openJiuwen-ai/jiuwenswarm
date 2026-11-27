from __future__ import annotations

import json
import time
from dataclasses import dataclass

from jiuwenclaw.dcs import DcsClusterClient, DcsClusterConfig, load_config_from_env, session_dcs_ttl_seconds
from jiuwenclaw.utils import logger

SandboxLogDcsConfig = DcsClusterConfig


@dataclass(frozen=True)
class SandboxLogRecord:
    sandbox_id: str
    url: str
    name: str = ""
    uploaded_at: float = 0.0


class SandboxLogDcsStore:
    """Persist sandbox run log OBS URLs keyed by sandbox_id."""

    def __init__(self, config: DcsClusterConfig) -> None:
        self._config = config
        self._ttl_seconds = session_dcs_ttl_seconds()
        self._dcs = DcsClusterClient(config)

    @classmethod
    def from_env(cls) -> SandboxLogDcsStore:
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
    def _redis_key(sandbox_id: str) -> str:
        return f"jiuwen:sandboxLog:{sandbox_id}"

    @staticmethod
    def _serialize(record: SandboxLogRecord) -> str:
        return json.dumps(
            {
                "url": record.url,
                "name": record.name,
                "uploaded_at": record.uploaded_at,
                "sandbox_id": record.sandbox_id,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize(sandbox_id: str, raw: str) -> SandboxLogRecord | None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        url = str(data.get("url") or "").strip()
        if not url:
            return None
        try:
            uploaded_at = float(data.get("uploaded_at") or 0.0)
        except (TypeError, ValueError):
            uploaded_at = 0.0
        return SandboxLogRecord(
            sandbox_id=sandbox_id,
            url=url,
            name=str(data.get("name") or "").strip(),
            uploaded_at=uploaded_at,
        )

    async def get_sandbox_log(self, sandbox_id: str) -> SandboxLogRecord | None:
        sid = str(sandbox_id or "").strip()
        if not sid:
            return None
        raw = await self._dcs.get(self._redis_key(sid))
        if raw is None:
            return None
        return self._deserialize(sid, str(raw))

    async def put_sandbox_log(
        self,
        sandbox_id: str,
        *,
        url: str,
        name: str = "",
    ) -> SandboxLogRecord:
        sid = str(sandbox_id or "").strip()
        obs_url = str(url or "").strip()
        if not sid or not obs_url:
            raise ValueError("sandbox_id and url are required")
        record = SandboxLogRecord(
            sandbox_id=sid,
            url=obs_url,
            name=str(name or "").strip(),
            uploaded_at=time.time(),
        )
        await self._dcs.set_with_ttl(
            self._redis_key(sid),
            self._serialize(record),
            ttl_seconds=self._ttl_seconds,
        )
        logger.info(
            "Saved sandbox log snapshot to DCS: sandbox_id=%s",
            sid,
        )
        return record

    async def delete_sandbox_log(self, sandbox_id: str) -> None:
        sid = str(sandbox_id or "").strip()
        if not sid:
            return
        await self._dcs.delete(self._redis_key(sid))
        logger.info("Deleted sandbox log snapshot from DCS: sandbox_id=%s", sid)
