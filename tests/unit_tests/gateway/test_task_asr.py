import base64
import io
import json
import wave

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from jiuwenswarm.gateway.channel_manager.web.task_asr import (
    TaskAsrError,
    task_asr_endpoint,
    transcribe_task_audio,
)


def test_task_asr_endpoint_accepts_base_or_full_route():
    assert task_asr_endpoint("https://example.com/v1") == (
        "https://example.com/v1/audio/transcriptions"
    )
    assert task_asr_endpoint("https://example.com/v1/audio/transcriptions/") == (
        "https://example.com/v1/audio/transcriptions"
    )
    assert task_asr_endpoint("wss://ws-demo.cn-beijing.maas.aliyuncs.com/") == (
        "wss://ws-demo.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )


@pytest.mark.asyncio
async def test_transcribe_task_audio_posts_openai_compatible_multipart():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer task-secret"
        body = await request.aread()
        assert b"task-recording.webm" in body
        assert b'name="model"' in body
        assert b"task-asr-model" in body
        assert b"RIFF-audio" in body
        return httpx.Response(200, json={"text": " hello world "})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transcribe_task_audio(
            {
                "audio_base64": base64.b64encode(b"RIFF-audio").decode(),
                "mime_type": "audio/webm;codecs=opus",
            },
            environ={
                "ASR_API_BASE": "https://example.com/v1",
                "ASR_API_KEY": "task-secret",
                "ASR_MODEL_NAME": "task-asr-model",
            },
            client=client,
        )

    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_task_audio_rejects_invalid_recording_before_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("request must not be sent")
        )
    ) as client:
        with pytest.raises(TaskAsrError, match="Base64") as error:
            await transcribe_task_audio(
                {"audio_base64": "not-base64", "mime_type": "audio/webm"},
                environ={
                    "ASR_API_BASE": "https://example.com/v1",
                    "ASR_MODEL_NAME": "task-asr-model",
                },
                client=client,
            )
    assert error.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_transcribe_task_audio_surfaces_upstream_message():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "invalid key"}})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TaskAsrError, match="invalid key") as error:
            await transcribe_task_audio(
                {
                    "audio_base64": base64.b64encode(b"audio").decode(),
                    "mime_type": "audio/webm",
                },
                environ={
                    "ASR_API_BASE": "https://example.com/v1",
                    "ASR_MODEL_NAME": "task-asr-model",
                },
                client=client,
            )
    assert error.value.code == "ASR_UPSTREAM_ERROR"


_HTTP_ENV = {"ASR_API_BASE": "https://example.com/v1", "ASR_MODEL_NAME": "task-asr-model"}
_WEBM = {"audio_base64": base64.b64encode(b"audio").decode(), "mime_type": "audio/webm"}


def _wav_params(pcm: bytes) -> dict:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return {"audio_base64": base64.b64encode(buffer.getvalue()).decode(), "mime_type": "audio/wav"}


def _event(name: str, payload: dict | None = None, **header) -> str:
    return json.dumps({"header": {"event": name, **header}, "payload": payload or {}})


async def _transcribe_ws(handler, pcm: bytes, **serve_kwargs) -> str:
    async with websockets.serve(handler, "127.0.0.1", 0, **serve_kwargs) as server:
        port = server.sockets[0].getsockname()[1]
        return await transcribe_task_audio(
            _wav_params(pcm),
            environ={
                "ASR_API_BASE": f"ws://127.0.0.1:{port}",
                "ASR_API_KEY": "task-secret",
                "ASR_MODEL_NAME": "qwen-audio-3.0-asr-flash-streaming",
            },
        )


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_joins_final_sentences():
    pcm = b"\x01\x00\xff\xff" * 4000  # 0.5 s at 16 kHz -> five 100 ms chunks
    seen = {"chunks": []}

    async def handler(conn):
        seen["path"] = conn.request.path
        seen["auth"] = conn.request.headers.get("Authorization")
        seen["run_task"] = json.loads(await conn.recv())
        await conn.send(_event("task-started"))
        while isinstance(message := await conn.recv(), bytes):
            seen["chunks"].append(message)
        seen["finish"] = json.loads(message)
        for text, end, heartbeat in (("你好", False, False), ("你好，", True, False),
                                     ("", True, True), ("世界。", True, False)):
            sentence = {"text": text, "sentence_end": end, "heartbeat": heartbeat}
            await conn.send(_event("result-generated", {"output": {"sentence": sentence}}))
        await conn.send(_event("task-finished"))

    assert await _transcribe_ws(handler, pcm) == "你好，世界。"
    assert seen["path"] == "/api-ws/v1/inference"
    assert seen["auth"] == "Bearer task-secret"
    payload = seen["run_task"]["payload"]
    assert seen["run_task"]["header"]["action"] == "run-task"
    assert (payload["task"], payload["function"], payload["model"]) == (
        "asr", "recognition", "qwen-audio-3.0-asr-flash-streaming"
    )
    assert payload["parameters"] == {"format": "pcm", "sample_rate": 16000}
    assert seen["finish"]["header"]["action"] == "finish-task"
    assert [len(chunk) for chunk in seen["chunks"]] == [3200] * 5
    assert b"".join(seen["chunks"]) == pcm


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_surfaces_task_failed():
    async def handler(conn):
        await conn.recv()
        await conn.send(
            _event("task-failed", error_code="InvalidParameter", error_message="bad model")
        )
        await conn.wait_closed()

    with pytest.raises(TaskAsrError, match="InvalidParameter: bad model") as error:
        await _transcribe_ws(handler, b"\x00\x00" * 1600)
    assert error.value.code == "ASR_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_handshake_rejected():
    def reject(_conn, _request):
        return Response(401, "Unauthorized", Headers(), b"invalid api key")

    with pytest.raises(TaskAsrError, match="401") as error:
        await _transcribe_ws(None, b"\x00\x00" * 1600, process_request=reject)
    assert error.value.code == "ASR_UPSTREAM_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [("<html>login</html>", "text/html"), ("{bad", "application/json")],
)
async def test_transcribe_task_audio_rejects_unparsable_success_body(body, content_type):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text=body, headers={"content-type": content_type})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TaskAsrError, match="无效 JSON") as error:
            await transcribe_task_audio(_WEBM, environ=_HTTP_ENV, client=client)
    assert error.value.code == "ASR_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_transcribe_task_audio_keeps_endpoint_query():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"text": "ok"})

    base = "https://example.com/openai/deployments/asr?api-version=2024-06-01"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transcribe_task_audio(
            _WEBM, environ={**_HTTP_ENV, "ASR_API_BASE": base}, client=client
        )
    assert result == "ok"
    assert seen[0].path == "/openai/deployments/asr/audio/transcriptions"
    assert seen[0].params["api-version"] == "2024-06-01"
