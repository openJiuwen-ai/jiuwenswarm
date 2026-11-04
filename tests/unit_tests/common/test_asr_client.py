# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the shared ASR client's WebSocket adapter against a local server."""

import io
import json
import wave

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from jiuwenswarm.common.asr_client import AsrClient, AsrError, normalize_ws_endpoint


def test_normalize_ws_endpoint_adds_inference_route():
    assert normalize_ws_endpoint("wss://ws-demo.cn-beijing.maas.aliyuncs.com/") == (
        "wss://ws-demo.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )


def _wav_audio(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _event(name: str, payload: dict | None = None, **header) -> str:
    return json.dumps({"header": {"event": name, **header}, "payload": payload or {}})


async def _transcribe_ws(handler, pcm: bytes, **serve_kwargs) -> str:
    async with websockets.serve(handler, "127.0.0.1", 0, **serve_kwargs) as server:
        port = server.sockets[0].getsockname()[1]
        client = AsrClient(
            f"ws://127.0.0.1:{port}",
            "task-secret",
            "qwen-audio-3.0-asr-flash-streaming",
            timeout=5.0,
            max_retries=0,
        )
        return await client.transcribe(_wav_audio(pcm), "audio/wav")


@pytest.mark.asyncio
async def test_asr_client_websocket_joins_final_sentences():
    pcm = b"\x01\x00\xff\xff" * 4000  # 0.5 s at 16 kHz -> five 100 ms chunks
    seen = {"chunks": []}

    async def handler(conn):
        seen["path"] = conn.request.path
        seen["auth"] = conn.request.headers.get("Authorization")
        seen["run_task"] = json.loads(await conn.recv())
        await conn.send(_event("task-started"))
        message = await conn.recv()
        while isinstance(message, bytes):
            seen["chunks"].append(message)
            message = await conn.recv()
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
async def test_asr_client_websocket_surfaces_task_failed():
    async def handler(conn):
        await conn.recv()
        await conn.send(
            _event("task-failed", error_code="InvalidParameter", error_message="bad model")
        )
        await conn.wait_closed()

    with pytest.raises(AsrError, match="InvalidParameter: bad model") as error:
        await _transcribe_ws(handler, b"\x00\x00" * 1600)
    assert error.value.code == "ASR_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_asr_client_websocket_handshake_rejected():
    def reject(_conn, _request):
        return Response(401, "Unauthorized", Headers(), b"invalid api key")

    with pytest.raises(AsrError, match="401") as error:
        await _transcribe_ws(None, b"\x00\x00" * 1600, process_request=reject)
    assert error.value.code == "ASR_UPSTREAM_ERROR"


