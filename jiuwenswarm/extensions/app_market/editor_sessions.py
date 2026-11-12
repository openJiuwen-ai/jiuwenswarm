"""Resolve one persistent editing conversation per market application."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable

from openjiuwen_protocol.message import ReqMethod

from .app_market.storage import MarketStorage

CoreRequest = Callable[[ReqMethod, dict[str, Any]], Awaitable[tuple[bool, dict[str, Any]]]]


class EditorSessionError(Exception):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("error", "Cannot open application editor"))
        self.code = payload.get("code", "EDIT_SESSION_FAILED")


class AppEditorSessions:
    def __init__(self, storage: MarketStorage):
        self._storage = storage
        self._locks: dict[str, asyncio.Lock] = {}

    async def open(self, app_id: str, owner_id: str, request: CoreRequest, options: dict[str, Any]) -> dict[str, Any]:
        """Serialize lookup/create/bind without importing AgentServer runtime state."""
        async def call(method: ReqMethod, params: dict[str, Any]) -> dict[str, Any]:
            ok, payload = await request(method, params)
            if not ok:
                raise EditorSessionError(payload)
            return payload

        async with self._locks.setdefault(app_id, asyncio.Lock()):
            manifest = self._storage.load_manifest(app_id)
            if manifest is None or manifest.owner_id != owner_id:
                raise EditorSessionError({"error": "Application not found", "code": "NOT_FOUND"})
            if manifest.editor_session_id:
                try:
                    session = await call(ReqMethod.SESSION_GET_METADATA, {"session_id": manifest.editor_session_id})
                    return {"session": {**session, "session_id": manifest.editor_session_id}, "created": False}
                except EditorSessionError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                manifest.editor_session_id = ""
                manifest.editor_create_token = ""
            if not manifest.source_session:
                raise EditorSessionError({"error": "Application source session is missing"})
            source = await call(ReqMethod.SESSION_GET_METADATA, {"session_id": manifest.source_session})
            projects = await call(ReqMethod.PROJECT_LIST, {"filter": "all", "work_mode": source["work_mode"]})
            project = next((p for p in projects["projects"] if p["project_id"] == source["project_id"]), None)
            if not project or project.get("hidden"):
                raise EditorSessionError({"error": "Application source project is unavailable"})
            if not manifest.editor_create_token:
                manifest.editor_create_token = uuid.uuid4().hex
                self._storage.save_manifest(app_id, manifest)
            created = await call(ReqMethod.SESSION_CREATE, {
                **options,
                "create_token": manifest.editor_create_token,
                "mode": f"agent.{project['work_mode']}.normal",
                "is_swarm": False,
                "persist_session": False,
                "project_id": project["project_id"],
                "work_mode": project["work_mode"],
            })
            # Publishing can update metadata while creation is awaiting Core.
            current = self._storage.load_manifest(app_id)
            if current is None or current.owner_id != owner_id or current.editor_create_token != manifest.editor_create_token:
                raise EditorSessionError({"error": "Application changed while opening editor", "code": "CONFLICT"})
            current.editor_session_id = created["session_id"]
            self._storage.save_manifest(app_id, current)
            session = await call(ReqMethod.SESSION_GET_METADATA, {"session_id": current.editor_session_id})
            return {"session": {**session, "session_id": current.editor_session_id}, "created": True}
