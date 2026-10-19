import base64
import io
import json
import wave

import httpx
import pytest
import websockets

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


def test_task_asr_endpoint_adds_dashscope_path_for_websocket_base():
    assert task_asr_endpoint("wss://ws-demo.cn-beijing.maas.aliyuncs.com") == (
        "wss://ws-demo.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )
    assert task_asr_endpoint("wss://ws-demo.cn-beijing.maas.aliyuncs.com/") == (
        "wss://ws-demo.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )
    assert task_asr_endpoint("ws://127.0.0.1:9000/custom/path") == (
        "ws://127.0.0.1:9000/custom/path"
    )


def test_task_asr_endpoint_rejects_other_schemes():
    with pytest.raises(TaskAsrError) as error:
        task_asr_endpoint("ftp://example.com/v1")
    assert error.value.code == "ASR_BAD_CONFIG"


def _wav_bytes(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _event(name, task_id, **extra):
    header = {"event": name, "task_id": task_id}
    header.update(extra.pop("header", {}))
    return json.dumps({"header": header, "payload": extra.get("payload", {})})


def _sentence(task_id, text, end):
    return _event(
        "result-generated",
        task_id,
        payload={"output": {"sentence": {"text": text, "sentence_end": end}}},
    )


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_joins_final_sentences():
    pcm = (b"\x01\x00\xff\xff" * 4000)  # 8000 samples = 0.5 s at 16 kHz
    seen = {}

    async def handler(conn):
        seen["path"] = conn.request.path
        seen["auth"] = conn.request.headers.get("Authorization")
        run_task = json.loads(await conn.recv())
        seen["run_task"] = run_task
        task_id = run_task["header"]["task_id"]
        await conn.send(_event("task-started", task_id))
        audio = b""
        while True:
            message = await conn.recv()
            if isinstance(message, bytes):
                seen.setdefault("chunks", []).append(len(message))
                audio += message
                continue
            seen["finish"] = json.loads(message)
            break
        seen["audio"] = audio
        await conn.send(_sentence(task_id, "你好", False))
        await conn.send(_sentence(task_id, "你好，", True))
        await conn.send(_event(
            "result-generated",
            task_id,
            payload={"output": {"sentence": {"heartbeat": True, "sentence_end": True}}},
        ))
        await conn.send(_sentence(task_id, "世界。", True))
        await conn.send(_event("task-finished", task_id))

    server, base = await _serve(handler)
    try:
        result = await transcribe_task_audio(
            {
                "audio_base64": base64.b64encode(_wav_bytes(pcm)).decode(),
                "mime_type": "audio/wav",
            },
            environ={
                "ASR_API_BASE": base,
                "ASR_API_KEY": "task-secret",
                "ASR_MODEL_NAME": "qwen-audio-3.0-asr-flash-streaming",
            },
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result == "你好，世界。"
    assert seen["path"] == "/api-ws/v1/inference"
    assert seen["auth"] == "Bearer task-secret"
    header = seen["run_task"]["header"]
    payload = seen["run_task"]["payload"]
    assert header["action"] == "run-task" and header["streaming"] == "duplex"
    assert payload["task_group"] == "audio"
    assert payload["task"] == "asr"
    assert payload["function"] == "recognition"
    assert payload["model"] == "qwen-audio-3.0-asr-flash-streaming"
    assert payload["parameters"] == {"format": "pcm", "sample_rate": 16000}
    assert seen["finish"]["header"]["action"] == "finish-task"
    assert seen["finish"]["header"]["task_id"] == header["task_id"]
    assert seen["audio"] == pcm
    assert seen["chunks"] == [3200, 3200, 3200, 3200, 3200]


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_surfaces_task_failed():
    async def handler(conn):
        task_id = json.loads(await conn.recv())["header"]["task_id"]
        await conn.send(_event(
            "task-failed",
            task_id,
            header={"error_code": "InvalidParameter", "error_message": "bad model"},
        ))
        await conn.wait_closed()

    server, base = await _serve(handler)
    try:
        with pytest.raises(TaskAsrError, match="InvalidParameter: bad model") as error:
            await transcribe_task_audio(
                {
                    "audio_base64": base64.b64encode(_wav_bytes(b"\x00\x00" * 1600)).decode(),
                    "mime_type": "audio/wav",
                },
                environ={
                    "ASR_API_BASE": base + "/api-ws/v1/inference",
                    "ASR_MODEL_NAME": "bad-model",
                },
            )
    finally:
        server.close()
        await server.wait_closed()
    assert error.value.code == "ASR_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_transcribe_task_audio_websocket_handshake_rejected():
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def reject(_conn, _request):
        return Response(401, "Unauthorized", Headers(), b"invalid api key")

    server = await websockets.serve(
        lambda conn: pytest.fail("handshake must be rejected"),
        "127.0.0.1",
        0,
        process_request=reject,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(TaskAsrError, match="401") as error:
            await transcribe_task_audio(
                {
                    "audio_base64": base64.b64encode(_wav_bytes(b"\x00\x00" * 1600)).decode(),
                    "mime_type": "audio/wav",
                },
                environ={
                    "ASR_API_BASE": f"ws://127.0.0.1:{port}",
                    "ASR_API_KEY": "wrong-key",
                    "ASR_MODEL_NAME": "qwen-audio-3.0-asr-flash-streaming",
                },
            )
    finally:
        server.close()
        await server.wait_closed()
    assert error.value.code == "ASR_UPSTREAM_ERROR"


_HTTP_ENV = {"ASR_API_BASE": "https://example.com/v1", "ASR_MODEL_NAME": "task-asr-model"}
_WEBM_PARAMS = {"audio_base64": base64.b64encode(b"audio").decode(), "mime_type": "audio/webm"}


@pytest.mark.asyncio
async def test_transcribe_task_audio_rejects_non_json_success_body():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, text="<html>login</html>", headers={"content-type": "text/html"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TaskAsrError, match="无效 JSON") as error:
            await transcribe_task_audio(_WEBM_PARAMS, environ=_HTTP_ENV, client=client)
    assert error.value.code == "ASR_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_transcribe_task_audio_rejects_broken_json_body():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, text="{bad", headers={"content-type": "application/json"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(TaskAsrError, match="无效 JSON") as error:
            await transcribe_task_audio(_WEBM_PARAMS, environ=_HTTP_ENV, client=client)
    assert error.value.code == "ASR_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_transcribe_task_audio_keeps_endpoint_query():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"text": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await transcribe_task_audio(
            _WEBM_PARAMS,
            environ={
                **_HTTP_ENV,
                "ASR_API_BASE": "https://example.com/openai/deployments/asr?api-version=2024-06-01",
            },
            client=client,
        )
    assert result == "ok"
    assert seen[0].path == "/openai/deployments/asr/audio/transcriptions"
    assert seen[0].params["api-version"] == "2024-06-01"
