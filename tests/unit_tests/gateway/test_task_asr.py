import base64

import httpx
import pytest

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
