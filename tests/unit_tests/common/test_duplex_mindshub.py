from __future__ import annotations

import json

import httpx
import pytest

from jiuwenswarm.common import duplex_mindshub as mindshub
from jiuwenswarm.common.duplex_router import ControlSnapshot, InboundMessage, observe, state_for

SNAPSHOT = ControlSnapshot("v1", "r1", "c1", "tool", goal="Deploy service", next_action="Use Kafka")
MESSAGES = (InboundMessage("m1", "user", "Kafka is forbidden; use the existing database."),)


@pytest.fixture
def endpoint(monkeypatch):
    requests = []
    response = {"choices": [{"message": {"content": '{"action":"INTERRUPT"}'}}]}
    state = {"requests": requests, "response": response, "status": 200}

    async def handle(request):
        requests.append(request)
        return httpx.Response(state["status"], json=state["response"])

    client = httpx.AsyncClient
    monkeypatch.setattr(mindshub.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setenv("MINDSHUB_API_KEY", "test-only-secret")
    return state


@pytest.mark.asyncio
async def test_request_and_strict_response(endpoint):
    assert await mindshub.classify_mindshub(SNAPSHOT, MESSAGES) == {"action": "INTERRUPT"}
    request, = endpoint["requests"]
    assert str(request.url) == "https://api.mindshub.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-only-secret"
    body = json.loads(request.content)
    assert body["model"] == "mindshub_air"
    assert body["messages"][0]["role"] == "system"
    assert json.loads(body["messages"][1]["content"]) == state_for(SNAPSHOT, MESSAGES)
    assert "test-only-secret" not in request.content.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '{"action":"APPEND"}', '{"action":"INTERRUPT"}',
])
async def test_both_actions(endpoint, content):
    endpoint["response"]["choices"][0]["message"]["content"] = content
    assert await mindshub.classify_mindshub(SNAPSHOT, MESSAGES) == json.loads(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "INTERRUPT", '```json\n{"action":"INTERRUPT"}\n```',
    '{"action":"STOP"}', '{"action":"INTERRUPT","reason":"extra"}',
    '{"action":"INTERRUPT"} trailing', None,
])
async def test_bad_response_falls_back(endpoint, content):
    endpoint["response"]["choices"][0]["message"]["content"] = content
    result = await observe(SNAPSHOT, MESSAGES, classify=mindshub.classify_mindshub)
    assert result.status == "error"
    assert result.proposed_action == "UNDECIDED"
    assert len(endpoint["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 422, 429, 500, 529])
async def test_http_failure_does_not_retry(endpoint, status):
    endpoint["status"] = status
    result = await observe(SNAPSHOT, MESSAGES, classify=mindshub.classify_mindshub)
    assert result.status == "error"
    assert result.attempts == len(endpoint["requests"]) == 1




def test_replay_cli_uses_mindshub_without_sdk_model_config(endpoint, monkeypatch, tmp_path):
    from pathlib import Path
    from jiuwenswarm.common.duplex_benchmark import main

    cases = Path(__file__).parents[2] / "fixtures/duplex/routing_cases.jsonl"
    output = tmp_path / "replay.json"
    monkeypatch.setattr("sys.argv", ["duplex_benchmark", str(cases), "--backend", "mindshub",
                                    "--repeats", "1", "--output", str(output)])
    main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["backend"] == "mindshub"
    assert report["model_name"] == "mindshub_air"
    assert report["runs"] == len(endpoint["requests"]) == 6
    assert report["failures"] == 0
    assert all("expected_action" not in request.content.decode() for request in endpoint["requests"])
