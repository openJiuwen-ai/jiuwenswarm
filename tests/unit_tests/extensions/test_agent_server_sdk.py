"""The extension SDK forwards Gateway AgentServer requests."""

import pytest

from jiuwenswarm.extensions.sdk.agent_server import (
    request_agent_unary,
    shares_gateway_directory,
)
from jiuwenswarm.gateway.routing import e2a_proxy


@pytest.mark.asyncio
async def test_request_agent_unary_forwards_request(monkeypatch) -> None:
    seen = {}

    async def fetch(**kwargs):
        seen.update(kwargs)
        return True, {"id": "doc-1"}

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", fetch)
    client = object()
    result = await request_agent_unary(
        agent_client=client,
        method="brain.ingest",
        params={"kind": "link"},
        channel_id="slack",
    )

    assert result == (True, {"id": "doc-1"})
    assert seen == {
        "agent_client": client,
        "req_method": "brain.ingest",
        "params": {"kind": "link"},
        "session_id": None,
        "user_id": None,
        "channel_id": "slack",
        "label": "extension",
    }


def test_shared_directory_check_uses_gateway_client_rule(monkeypatch) -> None:
    client = object()
    monkeypatch.setattr(e2a_proxy, "is_legacy_shared_directory_client", lambda value: value is client)
    assert shares_gateway_directory(client)
    assert not shares_gateway_directory(object())
