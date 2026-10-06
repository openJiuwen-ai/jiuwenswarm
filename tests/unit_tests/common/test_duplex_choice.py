from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from jiuwenswarm.common import duplex_clef, duplex_choice, duplex_jev
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage

SNAPSHOT = ControlSnapshot("v1", "r1", "c1", "model", goal="private-goal")
MESSAGES = (InboundMessage("m1", "user", "private-message"),)
SECRET = "test-secret-never-log"


def payload(choice="INTERRUPT", interrupt=0.95):
    return {"answers": {"action": {
        "type": "choice", "choice": choice, "confidence": 0.95,
        "probabilities": {"INTERRUPT": interrupt, "APPEND": 1 - interrupt},
    }}}


@pytest.fixture(params=[duplex_jev, duplex_clef], ids=["jev", "clef"])
def adapter(request, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", SECRET)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "private-account")
    module = request.param
    classify = module.classify_jev if module is duplex_jev else module.classify_clef
    log = Mock()
    monkeypatch.setattr(module, "logger", log)
    return module, classify, log


def rendered_logs(log):
    return "\n".join(call.args[0] % call.args[1:] for call in log.method_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,interrupt,expected,reason", [
    ("INTERRUPT", 0.95, "INTERRUPT", "threshold_met"),
    ("INTERRUPT", 0.8, "APPEND", "below_threshold"),
    ("APPEND", 0.1, "APPEND", "model_append"),
])
async def test_adapters_share_transport_and_safe_logs(adapter, choice, interrupt, expected, reason):
    module, classify, log = adapter
    requests = []

    async def handle(request):
        requests.append(request)
        body = payload(choice, interrupt)
        if module is duplex_clef:
            body = {"success": True, "result": body}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await classify(SNAPSHOT, MESSAGES, client=client)
        assert not client.is_closed
    assert result == {"action": expected}
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {SECRET}"
    state = json.loads(requests[0].content)["state"]
    assert isinstance(state, str if module is duplex_clef else dict)
    logged = rendered_logs(log)
    assert f"action={expected} reason={reason}" in logged
    assert "status_code=200" in logged
    assert logged.count("decision request_id=") == 1
    ids = [line.split("request_id=")[1].split()[0] for line in logged.splitlines()]
    assert len(set(ids)) == 1
    assert all(secret not in logged for secret in (SECRET, "private-account", "private-goal", "private-message"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 402, 429, 529])
async def test_adapters_do_not_retry_or_follow_redirects(adapter, status):
    _, classify, log = adapter
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.example/"},
                              json={"error": SECRET})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await classify(SNAPSHOT, MESSAGES, client=client)
        assert not client.is_closed
    assert len(requests) == 1
    assert SECRET not in rendered_logs(log)
    assert f"status_code={status}" in rendered_logs(log)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, True, "2", float("nan"), float("inf")])
async def test_adapters_validate_timeout_before_sending(adapter, timeout):
    _, classify, _ = adapter
    handler = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="timeout_seconds"):
            await classify(SNAPSHOT, MESSAGES, timeout_seconds=timeout, client=client)
    handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [0.5, True, "0.9", float("nan"), 1.1])
async def test_adapters_validate_threshold_before_sending(adapter, threshold):
    _, classify, _ = adapter
    handler = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await classify(SNAPSHOT, MESSAGES, settings={"interrupt_threshold": threshold}, client=client)
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_owned_client_closes_on_cancellation(adapter, monkeypatch):
    _, classify, log = adapter
    entered = asyncio.Event()

    async def handle(request):
        entered.set()
        await asyncio.Event().wait()

    real_client = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        client = real_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(duplex_choice.httpx, "AsyncClient", factory)
    task = asyncio.create_task(classify(SNAPSHOT, MESSAGES))
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(clients) == 1 and clients[0].is_closed
    assert "reason=caller_cancelled" in rendered_logs(log)
    assert SECRET not in rendered_logs(log)
