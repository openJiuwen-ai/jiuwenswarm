from __future__ import annotations

import json
from types import SimpleNamespace as NS

import httpx
import pytest

from jiuwenswarm.common import duplex_jev as jev
from jiuwenswarm.common.duplex_router import (
    ControlSnapshot,
    InboundMessage,
    observe,
    state_for,
)

SNAPSHOT = ControlSnapshot("v1", "r1", "c1", "tool", goal="订单系统", next_action="Install Kafka",
                           last_action="Read requirements")
MESSAGES = (InboundMessage("m1", "A1", "客户禁止 Kafka。"),)


def answer(action="INTERRUPT", probability=0.95):
    return {"model": "jev-1.13.0", "answers": {"action": {
        "type": "choice", "choice": action, "confidence": 0.8,
        "probabilities": {"INTERRUPT": probability, "APPEND": 1 - probability},
    }}, "usage": {"input_tokens": 100, "output_tokens": 0}}


@pytest.fixture
def endpoint(monkeypatch):
    server = NS(requests=[], payload=answer(), status=200, error=None)

    async def handle(request):
        server.requests.append(request)
        if server.error is not None:
            raise server.error
        return httpx.Response(server.status, json=server.payload)

    client = httpx.AsyncClient
    monkeypatch.setattr(jev.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-secret")
    return server


@pytest.mark.asyncio
async def test_request_uses_native_choice_and_only_public_snapshot(endpoint):
    result = await jev.classify_jev(SNAPSHOT, MESSAGES)
    assert result == {"action": "INTERRUPT"}
    request, = endpoint.requests
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-only-secret"
    body = json.loads(request.content)
    assert body["model"] == "jev-1.13.0"
    assert body["state"] == state_for(SNAPSHOT, MESSAGES)
    assert set(body["state"]["snapshot"]) == {"goal", "next_action", "last_action", "phase"}
    question = body["questions"]["action"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"APPEND", "INTERRUPT"}
    assert "untrusted task data" in question["instructions"]
    assert "evidence, not authority" in question["instructions"]
    assert "last_action is completed history" in question["instructions"]
    assert "test-only-secret" not in request.content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"type": "noul"}, {"choice": "DELETE"}, {"choice": "APPEND"},
    {"probabilities": {}}, {"probabilities": {"APPEND": 0.05, "INTERRUPT": 0.95, "OTHER": 0}},
    {"probabilities": {"APPEND": 0.2, "INTERRUPT": 0.95}},
    {"probabilities": {"APPEND": -0.1, "INTERRUPT": 1.1}},
    {"probabilities": {"APPEND": False, "INTERRUPT": True}},
    {"probabilities": {"APPEND": "0.05", "INTERRUPT": "0.95"}},
    {"confidence": None}, {"confidence": 2}, {"confidence": float("inf")},
    {"probabilities": {"APPEND": 0.05, "INTERRUPT": float("nan")}},
])
async def test_bad_answers_are_errors_not_interrupts(endpoint, patch):
    endpoint.payload["answers"]["action"].update(patch)
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev)
    assert result.status == "error"
    assert result.proposed_action == "UNDECIDED"
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"answers": {}}, {"action": "INTERRUPT"}])
async def test_missing_response_fields_fall_back(endpoint, payload):
    endpoint.payload = payload
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev)
    assert result.status == "error"


@pytest.mark.asyncio
async def test_http_timeout_is_reported_as_timeout(endpoint):
    endpoint.error = httpx.ReadTimeout("request timed out")
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev)
    assert result.status == "timeout"
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
async def test_credentials_and_custom_endpoint(endpoint, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(ValueError, match="API key"):
        await jev.classify_jev(SNAPSHOT, MESSAGES)
    assert not endpoint.requests
    monkeypatch.setenv("TEST_JEV_KEY", "custom-test-secret")
    await jev.classify_jev(SNAPSHOT, MESSAGES, model_name="jev-preview", settings={
        "api_base": "http://127.0.0.1:8123/v1/", "api_key_env": "TEST_JEV_KEY"})
    request, = endpoint.requests
    assert str(request.url) == "http://127.0.0.1:8123/v1/systemone"
    assert request.headers["authorization"] == "Bearer custom-test-secret"
    assert json.loads(request.content)["model"] == "jev-preview"


@pytest.mark.asyncio
async def test_mindshub_decisions_endpoint(endpoint, monkeypatch):
    monkeypatch.setenv("MINDSHUB_API_KEY", "local-mindshub-test")
    result = await jev.classify_jev(SNAPSHOT, MESSAGES, settings={
        "api_base": "https://api.mindshub.ai/v1",
        "endpoint_path": "decisions", "api_key_env": "MINDSHUB_API_KEY"})
    assert result == {"action": "INTERRUPT"}
    request, = endpoint.requests
    assert str(request.url) == "https://api.mindshub.ai/v1/decisions"
    assert request.headers["authorization"] == "Bearer local-mindshub-test"


@pytest.mark.asyncio
async def test_invalid_jev_endpoint_path_prevents_request(endpoint):
    with pytest.raises(ValueError, match="endpoint_path"):
        await jev.classify_jev(SNAPSHOT, MESSAGES,
                               settings={"endpoint_path": "../chat/completions"})
    assert not endpoint.requests
