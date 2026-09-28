"""Regression coverage for AgentServer-to-Gateway cron push routing."""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


class _CapturingCronController:
    def __init__(self) -> None:
        self.create_params: dict | None = None
        self.update_patch: dict | None = None
        self.user_id = ""

    async def create_job(self, params: dict, *, auth_session=None) -> dict:
        self.create_params = dict(params)
        return {"id": "job-1"}

    async def get_job(self, job_id: str) -> dict:
        return {"id": job_id, "user_id": self.user_id}

    async def update_job(self, job_id: str, patch: dict, *, auth_session=None) -> dict:
        _ = job_id
        self.update_patch = dict(patch)
        return {"id": "job-1"}


class _RunNowCronController:
    def __init__(self) -> None:
        self.run_calls = 0
        self.user_id = ""

    async def run_now(self, job_id: str) -> str:
        _ = job_id
        self.run_calls += 1
        return "job-1:1710000000"

    async def get_job(self, job_id: str) -> dict:
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

    async def _publish(_message) -> None:
        return None

    handler.publish_robot_messages = _publish
    return handler


@pytest.mark.asyncio
async def test_single_user_cron_push_keeps_local_project_validation() -> None:
    controller = _CapturingCronController()
    handler = _bare_handler(object(), controller)

    await handler._handle_cron_push_payload(
        payload={"action": "create", "data": {"name": "job"}},
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
    )

    assert controller.create_params is not None
    assert "_agentos_project_binding_verified" not in controller.create_params


@pytest.mark.asyncio
async def test_agentos_cron_push_marks_remote_project_binding_verified() -> None:
    controller = _CapturingCronController()
    client_type = type("AgentOSRouterClient", (), {})
    client_type.__module__ = "jiuwenswarm.extensions.agentos.routing"
    handler = _bare_handler(client_type(), controller)

    await handler._handle_cron_push_payload(
        payload={"action": "update", "data": {"job_id": "job-1", "patch": {}}},
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
    )

    assert controller.update_patch == {"_agentos_project_binding_verified": True}


@pytest.mark.asyncio
async def test_cron_push_run_now_acks_run_id_back_to_agent() -> None:
    """P2：Gateway 处理 run_now 后把 run_id 经 E2A（cron.run_now.ack）回传 AgentServer。"""
    controller = _RunNowCronController()
    controller.user_id = "user-a"
    client = _CapturingAgentClient()
    handler = _bare_handler(client, controller)

    await handler._handle_cron_push_payload(
        payload={"action": "run_now", "data": {"job_id": "job-1"}},
        request_id="req-run-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
    )

    assert controller.run_calls == 1
    assert len(client.envs) == 1
    env = client.envs[0]
    assert env.method == "cron.run_now.ack"
    assert env.params["ack_request_id"] == "req-run-1"
    assert env.params["job_id"] == "job-1"
    assert env.params["run_id"] == "job-1:1710000000"


@pytest.mark.asyncio
async def test_cron_push_run_now_ack_keeps_agentos_user_route() -> None:
    controller = _RunNowCronController()
    controller.user_id = "user-a"
    client = _CapturingAgentClient()
    handler = _bare_handler(client, controller)

    await handler._handle_cron_push_payload(
        payload={"action": "run_now", "data": {"job_id": "job-1"}},
        request_id="req-run-1",
        channel_id="web",
        session_id="sess-1",
        metadata=None,
        user_id="user-a",
    )

    assert client.envs[0].user_id == "user-a"


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


@pytest.mark.asyncio
async def test_late_cron_push_keeps_authenticated_owner_after_stream_cleanup() -> None:
    """A delayed server-push must not create an invisible ownerless job."""
    controller = _CapturingCronController()
    client_type = type("AgentOSRouterClient", (), {})
    client_type.__module__ = "jiuwenswarm.extensions.agentos.routing"
    handler = _bare_handler(client_type(), controller)

    # Simulate the normal race: the stream finalizer has already removed the
    # request->user mapping when the queued server-push callback runs.
    wire = build_server_push_wire(
        {
            "request_id": "req-late",
            "channel_id": "web",
            "session_id": "sess-1",
            "response_kind": "cron",
            "metadata": {"_jiuwenswarm_cron_owner_user_id": "user-a"},
            "body": {"action": "create", "status": "ok", "data": {"name": "job"}},
        }
    )

    await handler._handle_agent_server_push(wire)

    assert controller.create_params is not None
    assert controller.create_params["user_id"] == "user-a"
    assert controller.create_params["_agentos_project_binding_verified"] is True


@pytest.mark.asyncio
async def test_cron_command_id_survives_wire_projection_for_gateway_ack() -> None:
    controller = _CapturingCronController()
    client_type = type("AgentOSRouterClient", (), {})
    client_type.__module__ = "jiuwenswarm.extensions.agentos.routing"
    handler = _bare_handler(client_type(), controller)
    acknowledgements: list[tuple[str, object]] = []

    async def _ack(*, command_id: str, data: object, user_id=None) -> None:
        acknowledgements.append((command_id, data))

    handler._push_cron_command_ack = _ack
    wire = build_server_push_wire({
        "request_id": "req-list", "channel_id": "web", "response_kind": "cron",
        "metadata": {"_jiuwenswarm_cron_owner_user_id": "user-a"},
        "body": {"command_id": "cmd-1", "action": "list", "status": "ok", "data": {}},
    })
    async def _list_jobs() -> list[dict]:
        return []

    controller.list_jobs = _list_jobs
    await handler._handle_agent_server_push(wire)

    assert acknowledgements == [("cmd-1", [])]


def test_message_handler_registers_push_callback_for_agentos_router() -> None:
    """AgentOSRouterClient is not a local WS client but supports server-push."""
    client = _PushCapableAgentOSClient()
    handler = object.__new__(MessageHandler)

    handler.agent_client = client
    handler._register_agent_server_push_handler()

    assert client.push_handler == handler._handle_agent_server_push
