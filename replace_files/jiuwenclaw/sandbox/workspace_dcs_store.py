from __future__ import annotations

import json
import time
from dataclasses import dataclass

from jiuwenclaw.dcs import DcsClusterClient, DcsClusterConfig, load_config_from_env, session_dcs_ttl_seconds
from jiuwenclaw.utils import logger

WorkspaceDcsConfig = DcsClusterConfig


@dataclass(frozen=True)
class WorkspaceRecord:
    session_id: str
    url: str
    name: str = ""
    uploaded_at: float = 0.0
    routing_key: str = ""
    sandbox_id: str = ""


class WorkspaceDcsStore:
    """Persist session workspace OBS snapshots for sandbox destroy / restore."""

    def __init__(self, config: DcsClusterConfig) -> None:
        self._config = config
        self._ttl_seconds = session_dcs_ttl_seconds()
        self._dcs = DcsClusterClient(config)

    @classmethod
    def from_env(cls) -> WorkspaceDcsStore:
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
    def _redis_key(session_id: str) -> str:
        return f"jiuwen:workspace:{session_id}"

    @staticmethod
    def _serialize(record: WorkspaceRecord) -> str:
        return json.dumps(
            {
                "url": record.url,
                "name": record.name,
                "uploaded_at": record.uploaded_at,
                "routing_key": record.routing_key,
                "sandbox_id": record.sandbox_id,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize(session_id: str, raw: str) -> WorkspaceRecord | None:
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
        return WorkspaceRecord(
            session_id=session_id,
            url=url,
            name=str(data.get("name") or "").strip(),
            uploaded_at=uploaded_at,
            routing_key=str(data.get("routing_key") or "").strip(),
            sandbox_id=str(data.get("sandbox_id") or "").strip(),
        )

    async def get_workspace(self, session_id: str) -> WorkspaceRecord | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        raw = await self._dcs.get(self._redis_key(sid))
        if raw is None:
            return None
        return self._deserialize(sid, str(raw))

    async def put_workspace(
        self,
        session_id: str,
        *,
        url: str,
        name: str = "",
        routing_key: str = "",
        sandbox_id: str = "",
    ) -> WorkspaceRecord:
        sid = str(session_id or "").strip()
        obs_url = str(url or "").strip()
        if not sid or not obs_url:
            raise ValueError("session_id and url are required")
        record = WorkspaceRecord(
            session_id=sid,
            url=obs_url,
            name=str(name or "").strip(),
            uploaded_at=time.time(),
            routing_key=str(routing_key or "").strip(),
            sandbox_id=str(sandbox_id or "").strip(),
        )
        await self._dcs.set_with_ttl(
            self._redis_key(sid),
            self._serialize(record),
            ttl_seconds=self._ttl_seconds,
        )
        logger.info(
            "Saved workspace snapshot to DCS: session_id=%s sandbox_id=%s",
            sid,
            record.sandbox_id,
        )
        return record

    async def delete_workspace(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        await self._dcs.delete(self._redis_key(sid))
        logger.info("Deleted workspace snapshot from DCS: session_id=%s", sid)
