from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

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
    server = NS(requests=[], payload=answer(), status=200, error=None, gate=None,
                entered=asyncio.Event(), cancelled=asyncio.Event())

    async def handle(request):
        server.requests.append(request)
        server.entered.set()
        if server.gate is not None:
            try:
                await server.gate.wait()
            finally:
                server.cancelled.set()
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
@pytest.mark.parametrize("action,probability,threshold,expected", [
    ("APPEND", 0.1, 0.9, "APPEND"),
    ("INTERRUPT", 0.8, 0.9, "APPEND"),
    ("INTERRUPT", 0.9, 0.9, "INTERRUPT"),
    ("INTERRUPT", 0.95, 0.99, "APPEND"),
    ("INTERRUPT", 0.7, 0.6, "INTERRUPT"),
    ("INTERRUPT", 0.5, 0.9, "APPEND"),
])
async def test_interrupt_probability_gate(endpoint, monkeypatch, action, probability, threshold, expected):
    endpoint.payload = answer(action, probability)
    log = Mock()
    monkeypatch.setattr(jev.logger, "info", log)
    assert await jev.classify_jev(SNAPSHOT, MESSAGES, settings={"interrupt_threshold": threshold}) == {
        "action": expected}
    fmt, *args = log.call_args.args
    rendered = fmt % tuple(args)
    assert f"choice={action} effective={expected}" in rendered
    assert "p_interrupt=" in rendered
    assert "test-only-secret" not in rendered
    assert MESSAGES[0].content not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"type": "noul"}, {"choice": "DELETE"}, {"choice": "APPEND"},
    {"probabilities": {}}, {"probabilities": {"APPEND": 0.05, "INTERRUPT": 0.95, "OTHER": 0}},
    {"probabilities": {"APPEND": 0.2, "INTERRUPT": 0.95}},
    {"probabilities": {"APPEND": -0.1, "INTERRUPT": 1.1}},
    {"probabilities": {"APPEND": False, "INTERRUPT": True}},
    {"probabilities": {"APPEND": "0.05", "INTERRUPT": "0.95"}},
    {"confidence": None}, {"confidence": 2},
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
@pytest.mark.parametrize("threshold", [0, 0.5, 1.1, True, "0.9", float("nan"), float("inf")])
async def test_invalid_threshold_prevents_request(endpoint, threshold):
    with pytest.raises(ValueError):
        await jev.classify_jev(SNAPSHOT, MESSAGES, settings={"interrupt_threshold": threshold})
    assert not endpoint.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 422, 429, 500, 529])
async def test_http_failure_is_not_retried(endpoint, monkeypatch, status):
    endpoint.status = status
    log = Mock()
    monkeypatch.setattr(jev.logger, "warning", log)
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev)
    assert result.status == "error"
    assert result.attempts == len(endpoint.requests) == 1
    fmt, *args = log.call_args.args
    rendered = fmt % tuple(args)
    assert f"status_code={status}" in rendered
    assert "test-only-secret" not in rendered


@pytest.mark.asyncio
async def test_http_timeout_is_reported_as_timeout(endpoint):
    endpoint.error = httpx.ReadTimeout("request timed out")
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev)
    assert result.status == "timeout"
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
async def test_total_deadline_and_cancellation_stop_request(endpoint):
    endpoint.gate = asyncio.Event()
    result = await observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev,
                            timeout_seconds=0.02)
    assert result.status == "timeout"
    assert endpoint.cancelled.is_set()
    endpoint.entered.clear()
    endpoint.cancelled.clear()
    task = asyncio.create_task(observe(SNAPSHOT, MESSAGES, classify=jev.classify_jev))
    await asyncio.wait_for(endpoint.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert endpoint.cancelled.is_set()




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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_response_numbers_cannot_admit_interrupt(value):
    payload = answer()
    payload["answers"]["action"]["probabilities"]["INTERRUPT"] = value
    with pytest.raises(ValueError):
        jev._decision(payload, 0.9)
    payload = answer()
    payload["answers"]["action"]["confidence"] = value
    with pytest.raises(ValueError):
        jev._decision(payload, 0.9)


def test_replay_cli_uses_jev_without_sdk_model_config(endpoint, monkeypatch, tmp_path):
    from pathlib import Path

    from jiuwenswarm.common.duplex_benchmark import main

    cases = Path(__file__).parents[2] / "fixtures/duplex/routing_cases.jsonl"
    output = tmp_path / "replay.json"
    monkeypatch.setattr("sys.argv", ["duplex_benchmark", str(cases), "--backend", "jev",
                                   "--repeats", "1", "--output", str(output)])
    main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["backend"] == "jev"
    assert report["model_name"] == "jev-1.13.0"
    assert report["interrupt_threshold"] == 0.9
    assert report["runs"] == len(endpoint.requests) == 6
    assert report["failures"] == 0
    assert report["correct"] == 3
    assert report["false_interrupts"] == 3
    assert all("expected_action" not in request.content.decode() for request in endpoint.requests)
