# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process Heartbeat contracts after AgentServer ownership migration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from websockets.legacy.server import serve as websocket_serve

from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import (
    HeartbeatRuntimeBridge,
)
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.gateway.heartbeat import (
    HeartbeatControllerProxy,
    HeartbeatServiceUnavailableError,
)
from jiuwenswarm.gateway.health_check import (
    GatewayHealthCheckService,
    HealthCheckConfig,
)
from jiuwenswarm.gateway.message_handler import MessageHandler
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class _Context:
    channel_id = "web"
    session_id = "session-1"
    user_id = "user-1"
    metadata = {"user_id": "fallback-user"}


def test_health_check_accepts_legacy_probe_wire_names() -> None:
    assert ReqMethod.HEARTBEAT_GET_CONF.value == "heartbeat.get_conf"
    assert ReqMethod.HEARTBEAT_SET_CONF.value == "heartbeat.set_conf"
    assert EventType.HEARTBEAT_RELAY is EventType.HEALTH_CHECK_RELAY
    assert EventType("heartbeat.relay") is EventType.HEALTH_CHECK_RELAY
    assert EventType("health_check.relay") is EventType.HEALTH_CHECK_RELAY


def test_legacy_health_check_relay_normalizes_at_gateway_ingress() -> None:
    message = MessageHandler._response_to_message(
        SimpleNamespace(
            request_id="legacy-probe",
            channel_id="web",
            payload={"event_type": "heartbeat.relay", "heartbeat": "HEALTH_CHECK_OK"},
            metadata={},
            agent_ref=None,
            ok=True,
        ),
        "health-check-session",
    )

    assert message.type == "event"
    assert message.event_type is EventType.HEALTH_CHECK_RELAY
    assert message.payload == {
        "event_type": "health_check.relay",
        "heartbeat": "HEALTH_CHECK_OK",
        "health_check": "HEALTH_CHECK_OK",
    }


async def test_health_check_relay_keeps_legacy_payload_alias() -> None:
    published = []

    class Client:
        async def send_request(self, envelope):  # noqa: ANN001
            return SimpleNamespace(payload={"health_check": "HEALTH_CHECK_OK"})

    class Handler:
        async def publish_robot_messages(self, message):  # noqa: ANN001
            published.append(message)

    service = GatewayHealthCheckService(
        Client(),
        HealthCheckConfig(interval_seconds=30, relay_channel_id="web"),
        message_handler=Handler(),
    )
    await service._tick()

    assert len(published) == 1
    assert published[0].event_type is EventType.HEALTH_CHECK_RELAY
    assert published[0].payload == {
        "health_check": "HEALTH_CHECK_OK",
        "heartbeat": "HEALTH_CHECK_OK",
    }


async def test_agent_tools_call_agentserver_local_service() -> None:
    calls: list[tuple[str, dict, dict]] = []

    class Service:
        async def handle_operation(self, action, data, **context):  # noqa: ANN001
            calls.append((action, data, context))
            return {"ok": True}

    tools = HeartbeatRuntimeBridge(Service()).build_tools(context=_Context())
    assert len(tools) == 9
    list_jobs = next(tool for tool in tools if tool.card.name == "heartbeat_list_jobs")
    create = next(tool for tool in tools if tool.card.name == "heartbeat_create_job")
    assert "delete_after_run" not in create.card.input_params["properties"]
    assert "active_count, not len(jobs)" in list_jobs.card.description
    assert "authoritative active-job limit check" in create.card.description
    max_runs_schema = create.card.input_params["properties"]["max_runs"]
    assert max_runs_schema["type"] == ["integer", "null"]
    assert max_runs_schema["default"] == 12
    run_at_schema = create.card.input_params["properties"]["schedule"]["properties"]["run_at"]
    assert run_at_schema["maximum"] == 253_402_300_799.0
    assert "Unix timestamp in seconds" in run_at_schema["description"]
    result = await create._func(
        name="follow up",
        prompt="continue",
        schedule={"type": "interval", "interval_seconds": 120},
        max_runs=None,
    )
    assert result == {"ok": True}
    action, data, context = calls[-1]
    assert action == "create"
    assert data["max_runs"] is None
    assert context == {
        "channel_id": "web",
        "session_id": "session-1",
        "user_id": "user-1",
        "source": "agent_tool",
    }

    await create.invoke(
        {
            "name": "default finite follow up",
            "prompt": "continue",
            "schedule": {"type": "interval", "interval_seconds": 120},
        }
    )
    assert calls[-1][1]["max_runs"] == 12

    await create.invoke(
        {
            "name": "explicit unlimited follow up",
            "prompt": "continue",
            "schedule": {"type": "interval", "interval_seconds": 120},
            "max_runs": None,
        }
    )
    assert calls[-1][1]["max_runs"] is None

    await create._func(
        name="finite follow up",
        prompt="continue",
        schedule={"type": "interval", "interval_seconds": 120},
    )
    assert "max_runs" not in calls[-1][1]


async def test_agent_tools_are_hidden_without_local_service() -> None:
    assert HeartbeatRuntimeBridge().build_tools(context=_Context()) == []


async def test_gateway_proxy_uses_one_unary_heartbeat_rpc() -> None:
    captured = []

    class Client:
        async def send_request(self, envelope):  # noqa: ANN001
            captured.append(envelope)
            return SimpleNamespace(
                ok=True,
                payload={"result": {"jobs": [{"id": "job-1"}]}},
            )

    proxy = HeartbeatControllerProxy(Client())
    result = await proxy.list_jobs(
        {}, access_session_id="session-1", user_id="user-1"
    )
    assert result == {"jobs": [{"id": "job-1"}]}
    envelope = captured[0]
    assert envelope.method == ReqMethod.HEARTBEAT_JOB.value
    assert envelope.session_id == "session-1"
    assert envelope.user_id == "user-1"
    assert envelope.params == {"action": "list", "data": {}}


async def test_gateway_proxy_roundtrips_over_real_agentserver_websocket() -> None:
    calls: list[tuple[str, dict, dict]] = []
    cancel_calls: list[dict] = []

    class Execution:
        @staticmethod
        def active_session_ids() -> set[str]:
            return set()

    class Runtime:
        protocol_version = "1"
        is_available = True
        execution = Execution()

        async def handle_operation(self, action, data, **context):  # noqa: ANN001
            calls.append((action, data, context))
            return {"jobs": [{"id": "job-wire"}]}

    class Manager:
        async def cancel_all_inflight_work(self, **kwargs):  # noqa: ANN003
            cancel_calls.append(kwargs)
            return None

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._heartbeat_runtime = Runtime()
    server._agent_manager = Manager()
    server._scheduler_service = None
    server._scheduler_agent = None
    server._session_stream_tasks = {}
    server._current_ws = None
    server._current_send_lock = None
    server._acp_client_capabilities_by_ws = {}
    server._ping_interval = None
    server._ping_timeout = None

    listener = await websocket_serve(
        server._connection_handler,
        "127.0.0.1",
        0,
        ping_interval=None,
    )
    client = WebSocketAgentServerClient(ping_interval=None, ping_timeout=None)
    try:
        port = listener.sockets[0].getsockname()[1]
        await client.connect(f"ws://127.0.0.1:{port}")
        assert client.server_ready is True

        result = await HeartbeatControllerProxy(client).list_jobs(
            {}, access_session_id="session-wire", user_id="user-wire"
        )

        assert result == {"jobs": [{"id": "job-wire"}]}
        assert calls == [
            (
                "list",
                {},
                {
                    "channel_id": "web",
                    "session_id": "session-wire",
                    "user_id": "user-wire",
                    "source": "web_rpc",
                },
            )
        ]
    finally:
        await client.disconnect()
        listener.close()
        await listener.wait_closed()
        await asyncio.sleep(0)

    assert len(cancel_calls) == 1
    assert "gateway ws closed" in cancel_calls[0]["reason"]
    assert cancel_calls[0]["exclude_session_ids"] == set()


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("BAD_REQUEST", ValueError),
        ("FORBIDDEN", PermissionError),
        ("NOT_FOUND", KeyError),
        ("CONFLICT", RuntimeError),
        ("SERVICE_UNAVAILABLE", HeartbeatServiceUnavailableError),
    ],
)
async def test_gateway_proxy_preserves_error_classes(code, error_type) -> None:
    class Client:
        async def send_request(self, envelope):  # noqa: ANN001
            return SimpleNamespace(
                ok=False,
                payload={"code": code, "error": "failed"},
            )

    with pytest.raises(error_type):
        await HeartbeatControllerProxy(Client()).get_job(
            "job-1",
            access_session_id="session-1",
        )
