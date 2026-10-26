"""Regression test for envelope-path session→request mapping.

The JSON-RPC `session/prompt` handler registers
``_active_prompt_request_by_session[session_id] = msg.id`` so that
``_message_from_gateway_event`` can route streamed events back to the
originating prompt request. The envelope path used by ``app_cli`` (and
any external stdio driver speaking E2AEnvelope) historically forgot
that registration -- the channel would then drop every gateway event
and the client's ``proc.stdout`` reader would hang waiting for a final
frame that never arrives.

This test pins the contract: after ``_handle_raw_line`` processes an
envelope with ``session_id``, the mapping must be populated.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect import (
    AcpChannel,
    AcpChannelConfig,
)


class _NoopBus:
    @staticmethod
    async def publish_user_messages(_msg: Any) -> None:
        return None


def _build_channel() -> AcpChannel:
    return AcpChannel(
        AcpChannelConfig(
            enabled=True,
            channel_id="acp",
            default_session_id="acp_cli_session",
            metadata={},
        ),
        router=_NoopBus(),
        gateway_url=None,
    )


def _envelope(session_id: str, *, request_id: str = "req-1") -> str:
    """Minimal session/prompt envelope as written to acp_channel stdin."""
    return json.dumps({
        "protocol_version": "1.0",
        "request_id": request_id,
        "method": "session/prompt",
        "params": {"content": "hi"},
        "channel": "acp",
        "session_id": session_id,
        "is_stream": True,
        "jsonrpc_id": request_id,
    })


async def _noop_dispatch(_msg: Any) -> None:
    return None


async def _handle_envelope_line(channel: AcpChannel, line: str) -> None:
    handler = getattr(channel, "_handle_raw_line")
    await handler(line)


def _mapped_request_id(channel: AcpChannel, session_id: str) -> str | None:
    mapping = getattr(channel, "_active_prompt_request_by_session")
    value = mapping.get(session_id)
    return value if isinstance(value, str) else None


def _has_request_context(channel: AcpChannel, request_id: str) -> bool:
    request_ctx = getattr(channel, "_request_ctx")
    return request_id in request_ctx


@pytest.mark.asyncio
async def test_handle_raw_line_registers_session_to_request_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _build_channel()
    # Skip dispatch (no gateway available in unit tests); we only care about
    # what state `_handle_raw_line` itself leaves behind.
    monkeypatch.setattr(channel, "_dispatch_message", _noop_dispatch)

    sid = "session-xyz"
    await _handle_envelope_line(channel, _envelope(sid))

    mapped_request_id = _mapped_request_id(channel, sid)
    assert mapped_request_id is not None, (
        "envelope path must register session→request mapping so gateway events"
        " can be routed back to the originating prompt"
    )
    assert _has_request_context(channel, mapped_request_id)


@pytest.mark.asyncio
async def test_handle_raw_line_skips_mapping_when_session_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Envelope with missing session_id falls back to the channel default.
    channel = _build_channel()
    monkeypatch.setattr(channel, "_dispatch_message", _noop_dispatch)

    env = json.dumps({
        "protocol_version": "1.0",
        "request_id": "req-2",
        "method": "session/prompt",
        "params": {"content": "hi"},
        "channel": "acp",
        # session_id intentionally absent — _envelope_to_message falls back
        # to channel default
        "is_stream": True,
        "jsonrpc_id": "req-2",
    })
    await _handle_envelope_line(channel, env)
    # Default session id from config (not None), so mapping IS populated.
    # The contract is "if msg.session_id, register" — falsy session_id skips.
    assert _mapped_request_id(channel, "acp_cli_session") is not None
