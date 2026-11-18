"""Regression coverage for AgentServer-to-Gateway cron push routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


class _CapturingCronController:
    def __init__(self) -> None:
        self.create_params: dict | None = None
        self.update_patch: dict | None = None
        self.list_kwargs: dict | None = None
        self.user_id = ""
        self.list_jobs_result: list[dict] = []

    async def create_job(self, params: dict) -> dict:
        self.create_params = dict(params)
        return {"id": "job-1"}

    async def get_job(self, job_id: str, **_kwargs) -> dict:
        return {"id": job_id, "user_id": self.user_id}

    async def update_job(self, job_id: str, patch: dict, **_kwargs) -> dict:
        _ = job_id
        self.update_patch = dict(patch)
        return {"id": "job-1"}

    async def list_jobs(self, **kwargs) -> list[dict]:
        self.list_kwargs = dict(kwargs)
        return list(self.list_jobs_result)


class _RunNowCronController:
    def __init__(self) -> None:
        self.run_calls = 0
        self.user_id = ""

    async def run_now(self, job_id: str, **_kwargs) -> str:
        _ = job_id
        self.run_calls += 1
        return "job-1:1710000000"

    async def get_job(self, job_id: str, **_kwargs) -> dict:
        return {"id": job_id, "user_id": self.user_id}


class _CapturingAgentClient:
    def __init__(self) -> None:
        self.envs: list[object] = []

    async def send_request(self, env: object) -> object:
        self.envs.append(env)
        from types import SimpleNamespace

        return SimpleNamespace(ok=True, payload={})


class _PushCapableAgentOSClient:
    """Minimal routed client: AgentOS forwards server-push via this callback."""

    def __init__(self) -> None:
        self.push_handler = None

    def set_server_push_handler(self, handler) -> None:
        self.push_handler = handler


def _bare_handler(agent_client: object, controller: _CapturingCronController) -> MessageHandler:
    """Avoid singleton initialization; cron push only needs these three fields."""
    handler = object.__new__(MessageHandler)
    handler.agent_client = agent_client
    handler._cron_controller = controller
    handler._stream_modes = {}
    handler._stream_user_ids = {}
    handler._stream_sessions = {}
    handler._stream_metadata = {}
    handler._non_stream_chat_messages = {}

    async def _publish(_message) -> None:
        return None

    handler.publish_robot_messages = _publish
    return handler


@pytest.mark.asyncio
async def test_agentos_cron_push_rejects_other_users_job() -> None:
    controller = _CapturingCronController()
    controller.user_id = "user-b"
    client_type = type("AgentOSRouterClient", (), {})
    client_type.__module__ = "jiuwenswarm.extensions.agentos.routing"
    handler = _bare_handler(client_type(), controller)

    await handler._handle_cron_push_payload(
        payload={"action": "update", "data": {"job_id": "job-b", "patch": {"name": "bad"}}},
        request_id="req-a",
        channel_id="web",
        session_id="sess-a",
        metadata=None,
        user_id="user-a",
    )

    assert controller.update_patch is None


