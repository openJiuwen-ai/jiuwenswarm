import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.routing.agent_request_timeout import (
    AGENT_SERVER_TIMEOUT_CODE,
    AGENT_SERVER_TIMEOUT_ERROR,
    AgentRequestTimeoutError,
    request_timeout_from_envelope,
    resolve_agent_request_timeout_seconds,
    send_agent_request_with_timeout,
)


def test_resolve_timeout_skips_stream_and_non_tui_requests():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="history.get",
        is_stream=True,
    ) is None
    assert resolve_agent_request_timeout_seconds(
        channel_id="web",
        method="history.get",
        is_stream=False,
    ) is None


def test_resolve_timeout_defaults_tui_unary_before_frontend_window():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="history.get",
        is_stream=False,
    ) == 25.0


def test_resolve_timeout_allows_tui_explicit_sixty_second_window():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="permissions.tools.update",
        is_stream=False,
        client_timeout_ms=60_000,
    ) == 55.0


def test_resolve_timeout_clamps_tui_client_timeout_to_safe_upper_bound():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="history.get",
        is_stream=False,
        client_timeout_ms=600_000,
    ) == 55.0


def test_resolve_timeout_extends_known_permissions_methods_for_older_clients():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="permissions.rules.create",
        is_stream=False,
    ) == 55.0


def test_resolve_timeout_extends_command_workflows_for_older_clients():
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="command.workflows",
        is_stream=False,
    ) == 55.0


def test_resolve_timeout_keeps_most_of_client_window_for_command_workflows():
    # 30s client → 28s gateway (2s grace); not the flat 5s cut used elsewhere.
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="command.workflows",
        is_stream=False,
        client_timeout_ms=30_000,
    ) == 28.0
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="command.workflows",
        is_stream=False,
        client_timeout_ms=10_000,
    ) == 8.0


def test_resolve_timeout_exempts_swarmflow_reply_from_unary_cap():
    """A swarmflow human reply tracks the human-turn timeout (600s default),
    so it must not be capped by the TUI 25s/55s unary limit."""
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="chat.swarmflow_reply",
        is_stream=False,
    ) is None
    # Even when a client passes a short client_timeout_ms, the exempt method
    # stays uncapped (the reply is async and must outlive the cap).
    assert resolve_agent_request_timeout_seconds(
        channel_id="tui",
        method="chat.swarmflow_reply",
        is_stream=False,
        client_timeout_ms=1_000,
    ) is None


def test_request_timeout_from_envelope_accepts_metadata_fallback():
    env = SimpleNamespace(
        channel="tui",
        method="history.get",
        is_stream=False,
        metadata={"client_timeout_ms": 60_000},
    )

    assert request_timeout_from_envelope(env) == 55.0


@pytest.mark.asyncio
async def test_send_agent_request_with_timeout_raises_stable_timeout_error(monkeypatch):
    class HangingAgentClient:
        async def send_request(self, env):
            await asyncio.Event().wait()

    env = SimpleNamespace(
        request_id="req-timeout-policy",
        channel="tui",
        method="history.get",
        is_stream=False,
        metadata={},
    )

    with pytest.raises(AgentRequestTimeoutError) as exc_info:
        await send_agent_request_with_timeout(
            HangingAgentClient(),
            env,
            label="test.policy",
            timeout_seconds=0.01,
        )

    assert str(exc_info.value) == AGENT_SERVER_TIMEOUT_ERROR
    assert exc_info.value.code == AGENT_SERVER_TIMEOUT_CODE
