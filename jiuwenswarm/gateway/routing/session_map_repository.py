# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMap PersistentStore Repository (not under gateway/config)."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.routing.session_map import Session, _session_from_stored_value
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

SESSION_MAP_STORE_NAME = "session_map"


class SessionMapCodec:
    """Session domain object <-> PersistentStore record."""

    @staticmethod
    def identity(identity_key: str) -> dict[str, Any]:
        return {"identity_key": str(identity_key)}

    @staticmethod
    def from_record(record: dict[str, Any]) -> tuple[str, Session] | None:
        key = str(record.get("identity_key") or "").strip()
        if not key:
            return None
        sess = _session_from_stored_value(
            {
                "session_id": record.get("session_id"),
                "service_id": record.get("service_id"),
                "agent_id": record.get("agent_id"),
            }
        )
        if sess is None:
            return None
        return key, sess

    @staticmethod
    def to_record(identity_key: str, session: Session) -> dict[str, Any]:
        return {
            "identity_key": str(identity_key),
            "session_id": session.session_id,
            "service_id": session.service_id,
            "agent_id": session.agent_id,
        }

    @staticmethod
    def to_updates(session: Session) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "service_id": session.service_id,
            "agent_id": session.agent_id,
        }


class SessionMapRepository:
    """session_map domain CRUD; does not branch on edition."""

    def __init__(self, store: PersistentStore, codec: SessionMapCodec | None = None) -> None:
        self._store = store
        self._codec = codec or SessionMapCodec()

    async def get(self, identity_key: str) -> Session | None:
        row = await self._store.get(
            SESSION_MAP_STORE_NAME,
            self._codec.identity(identity_key),
        )
        if row is None:
            return None
        decoded = self._codec.from_record(row)
        return decoded[1] if decoded else None

    async def list_all(self) -> dict[str, Session]:
        rows = await self._store.list(SESSION_MAP_STORE_NAME)
        out: dict[str, Session] = {}
        for row in rows:
            decoded = self._codec.from_record(row)
            if decoded is not None:
                out[decoded[0]] = decoded[1]
        return out

    async def upsert(self, identity_key: str, session: Session) -> Session:
        key = self._codec.identity(identity_key)
        updated = await self._store.update(
            SESSION_MAP_STORE_NAME,
            key,
            self._codec.to_updates(session),
        )
        if updated is not None:
            decoded = self._codec.from_record(updated)
            return decoded[1] if decoded else session
        created = await self._store.create(
            SESSION_MAP_STORE_NAME,
            self._codec.to_record(identity_key, session),
        )
        decoded = self._codec.from_record(created)
        return decoded[1] if decoded else session

    async def delete(self, identity_key: str) -> bool:
        return await self._store.delete(
            SESSION_MAP_STORE_NAME,
            self._codec.identity(identity_key),
        )


__all__ = [
    "SESSION_MAP_STORE_NAME",
    "SessionMapCodec",
    "SessionMapRepository",
]
