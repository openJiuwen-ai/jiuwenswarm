import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from jev_classifier import (
    ClassifierConfig,
    InvalidResponseError,
    JevClassifier,
    JevHttpConfig,
    JevHttpProvider,
    JevTimeoutError,
    ProviderError,
)
from jev_classifier.http_provider import parse_response
from jev_classifier.prompts import CRITERIA, INSTRUCTIONS

CONFIG = JevHttpConfig("https://jev.example/v1/systemone", "test-model", "test-secret")


def answer(choice="INTERRUPT", p_interrupt=0.95):
    return {"answers": {"action": {
        "type": "choice", "choice": choice, "confidence": 0.8,
        "probabilities": {"INTERRUPT": p_interrupt, "APPEND": 1 - p_interrupt},
    }}}


async def classify_http(handler, *, config=CONFIG, classifier_config=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        provider = JevHttpProvider(config, client=client)
        try:
            return await JevClassifier(provider, classifier_config).classify(
                context="Calculate using input v1.", messages=["Use corrected input v2.", "Keep CSV output."],
            )
        finally:
            assert not client.is_closed  # Adapter must not close caller-owned clients.


def test_http_contract_and_low_confidence_interrupt_is_not_rewritten():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=answer(p_interrupt=0.6))

    assert asyncio.run(classify_http(handler)) == "INTERRUPT"
    request, = requests
    assert request.method == "POST"
    assert str(request.url) == CONFIG.endpoint
    assert request.headers["authorization"] == "Bearer test-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "test-model"
    assert payload["state"] == {
        "context": "Calculate using input v1.",
        "messages": ["Use corrected input v2.", "Keep CSV output."],
    }
    action = payload["questions"]["action"]
    assert action["instructions"] == INSTRUCTIONS
    assert action["criteria"] == CRITERIA
    assert "test-secret" not in request.content.decode()
    assert "test-secret" not in repr(CONFIG)


@pytest.mark.parametrize("choice,probability", [("APPEND", 0.1), ("INTERRUPT", 1), ("APPEND", 0.5)])
def test_valid_response(choice, probability):
    assert parse_response(answer(choice, probability)) == choice


@pytest.mark.parametrize("body", [None, [], {}, {"answers": []}, {"answers": {"action": []}}])
def test_missing_response_shape(body):
    with pytest.raises(InvalidResponseError):
        parse_response(body)


@pytest.mark.parametrize("patch", [
    {"type": "text"}, {"choice": "abort"}, {"choice": ["APPEND"]}, {"choice": "APPEND"},
    {"confidence": None}, {"confidence": True}, {"confidence": float("inf")},
    {"probabilities": {}},
    {"probabilities": {"APPEND": 0, "INTERRUPT": 1, "OTHER": 0}},
    {"probabilities": {"APPEND": 0.2, "INTERRUPT": 0.95}},
    {"probabilities": {"APPEND": -0.1, "INTERRUPT": 1.1}},
    {"probabilities": {"APPEND": False, "INTERRUPT": True}},
    {"probabilities": {"APPEND": "0.05", "INTERRUPT": "0.95"}},
    {"probabilities": {"APPEND": 0.05, "INTERRUPT": float("nan")}},
    {"probabilities": {"APPEND": 0.05, "INTERRUPT": 10**1000}},
])
def test_invalid_typed_answer(patch):
    body = answer()
    body["answers"]["action"].update(patch)
    with pytest.raises(InvalidResponseError):
        parse_response(body)


@pytest.mark.parametrize("status", [302, 401, 422, 429, 500, 503])
def test_http_errors_are_not_retried_or_redirected(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://other.example/"}, text="private body")

    with pytest.raises(ProviderError, match=f"HTTP status {status}") as error:
        asyncio.run(classify_http(handler))
    assert len(calls) == 1
    assert "private body" not in str(error.value)


@pytest.mark.parametrize("exc,error", [
    (httpx.ReadTimeout("secret timeout detail"), JevTimeoutError),
    (httpx.ConnectError("secret transport detail"), ProviderError),
])
def test_transport_failure(exc, error):
    def handler(request):
        raise exc

    with pytest.raises(error) as result:
        asyncio.run(classify_http(handler))
    assert "secret" not in str(result.value)


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"", b"{\"answers\": null}"])
def test_invalid_json(body):
    with pytest.raises(InvalidResponseError):
        asyncio.run(classify_http(lambda request: httpx.Response(200, content=body)))


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, block=False):
        self.chunks = chunks
        self.block = block
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        for chunk in self.chunks:
            yield chunk
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def test_stream_limit_closes_response():
    stream = BodyStream([b"x" * 32, b"x" * 32])
    with pytest.raises(InvalidResponseError, match="max_response_bytes"):
        asyncio.run(classify_http(
            lambda request: httpx.Response(200, stream=stream),
            config=replace(CONFIG, max_response_bytes=40),
        ))
    assert stream.closed


def test_deadline_includes_response_body_and_closes_stream():
    stream = BodyStream(block=True)
    with pytest.raises(JevTimeoutError):
        asyncio.run(classify_http(
            lambda request: httpx.Response(200, stream=stream),
            classifier_config=ClassifierConfig(timeout_seconds=0.02),
        ))
    assert stream.closed


def test_http_caller_cancellation_closes_stream():
    async def run():
        stream = BodyStream(block=True)
        task = asyncio.create_task(classify_http(lambda request: httpx.Response(200, stream=stream)))
        await asyncio.wait_for(stream.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(run())


def test_owned_client_is_closed(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=answer())))
    monkeypatch.setattr("jev_classifier.http_provider.httpx.AsyncClient", lambda: client)
    classifier = JevClassifier(JevHttpProvider(CONFIG))
    assert asyncio.run(classifier.classify(context="working", messages=["update"])) == "INTERRUPT"
    assert client.is_closed


@pytest.mark.parametrize("endpoint", ["", "file:///tmp/jev", "https://user:pass@example.com/", "https://example.com/?key=x"])
def test_invalid_endpoint(endpoint):
    with pytest.raises(ValueError):
        replace(CONFIG, endpoint=endpoint)


@pytest.mark.parametrize("name,value", [
    ("api_key", ""), ("model", " "), ("max_response_bytes", 0), ("max_response_bytes", True),
])
def test_invalid_http_configuration(name, value):
    with pytest.raises(ValueError):
        replace(CONFIG, **{name: value})

