# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway proxy for the AgentServer-owned Heartbeat controller."""

from __future__ import annotations

import secrets
import time
from typing import Any

from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a_or_fallback
from jiuwenswarm.common.schema.message import Message, ReqMethod


class HeartbeatServiceUnavailableError(Exception):
    """AgentServer cannot currently serve Heartbeat RPC requests."""


class HeartbeatControllerProxy:
    """Preserve the existing handler-facing API while moving ownership server-side."""

    def __init__(self, agent_client: Any) -> None:
        self._agent_client = agent_client

    async def _request(
        self,
        action: str,
        data: dict[str, Any],
        *,
        channel_id: str,
        session_id: str,
        user_id: str = "",
    ) -> dict[str, Any]:
        request_id = f"heartbeat-rpc-{secrets.token_hex(8)}"
        msg = Message(
            id=request_id,
            type="req",
            channel_id=channel_id or "web",
            session_id=session_id,
            req_method=ReqMethod.HEARTBEAT_JOB,
            params={"action": action, "data": dict(data or {})},
            timestamp=time.time(),
            ok=True,
            is_stream=False,
            user_id=user_id or None,
        )
        response = await self._agent_client.send_request(
            message_to_e2a_or_fallback(msg)
        )
        payload = response.payload if isinstance(response.payload, dict) else {}
        if not response.ok:
            code = str(payload.get("code") or "INTERNAL_ERROR")
            error = str(payload.get("error") or "heartbeat operation failed")
            if code == "BAD_REQUEST":
                raise ValueError(error)
            if code == "FORBIDDEN":
                raise PermissionError(error)
            if code == "NOT_FOUND":
                raise KeyError(error)
            if code == "CONFLICT":
                raise RuntimeError(error)
            if code == "SERVICE_UNAVAILABLE":
                raise HeartbeatServiceUnavailableError(error)
            raise RuntimeError(error)
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else {}

    async def list_jobs(
        self,
        params: dict[str, Any],
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "list",
            params,
            channel_id=str(params.get("channel_id") or "web"),
            session_id=str(access_session_id or params.get("session_id") or ""),
            user_id=user_id,
        )

    async def get_meta(self, *, user_id: str = "") -> dict[str, Any]:
        return await self._request(
            "meta", {}, channel_id="web", session_id="", user_id=user_id
        )

    async def get_job(
        self,
        job_id: str,
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        result = await self._request(
            "get",
            {"job_id": job_id},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )
        return result.get("job") if isinstance(result.get("job"), dict) else None

    async def create_job(
        self, params: dict[str, Any], *, user_id: str = ""
    ) -> dict[str, Any]:
        result = await self._request(
            "create",
            params,
            channel_id=str(params.get("channel_id") or "web"),
            session_id=str(params.get("session_id") or ""),
            user_id=str(user_id or params.get("user_id") or ""),
        )
        return dict(result.get("job") or {})

    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        result = await self._request(
            "update",
            {"job_id": job_id, "patch": patch},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )
        return dict(result.get("job") or {})

    async def delete_job(
        self,
        job_id: str,
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "delete",
            {"job_id": job_id},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )

    async def toggle_job(
        self,
        job_id: str,
        enabled: bool,
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        result = await self._request(
            "toggle",
            {"job_id": job_id, "enabled": enabled},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )
        return dict(result.get("job") or {})

    async def preview_job(
        self,
        job_id: str,
        count: int = 5,
        *,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "preview",
            {"job_id": job_id, "count": count},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )

    async def run_now(
        self,
        job_id: str,
        *,
        reschedule: bool = False,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "run_now",
            {"job_id": job_id, "reschedule": reschedule},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )

    async def cancel_run(
        self,
        job_id: str,
        *,
        pause_schedule: bool = False,
        access_session_id: str | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "cancel",
            {"job_id": job_id, "pause_schedule": pause_schedule},
            channel_id="web",
            session_id=str(access_session_id or ""),
            user_id=user_id,
        )


__all__ = ["HeartbeatControllerProxy", "HeartbeatServiceUnavailableError"]
