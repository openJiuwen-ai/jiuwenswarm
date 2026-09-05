# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.agents.harness.common.tools.video_gen_tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from jiuwenswarm.agents.harness.common.tools import video_gen_tools as vg

# The @tool decorator wraps each function in a LocalFunction; ``._func`` is the
# original plain async function it wraps, callable directly with the same
# keyword arguments - see LocalFunction.__init__ (openjiuwen). Testing through
# this avoids bootstrapping the whole tool-invocation/callback framework that
# the wrapper's own __call__ wires up, which is out of scope for a unit test.
generate_video = vg.generate_video._func
check_video_status = vg.check_video_status._func


_TEST_API_BASE = "https://video-model.example/api/v1"
_TEST_MODEL = "example/video-gen-model"


def _clear_video_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VIDEO_API_KEY", "VIDEO_API_BASE", "VIDEO_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)


def _set_video_model_config(monkeypatch: pytest.MonkeyPatch, *, api_key: str = "sk-test") -> None:
    """Configure the Video Model panel slot the tools now read exclusively."""
    monkeypatch.setenv("VIDEO_API_KEY", api_key)
    monkeypatch.setenv("VIDEO_API_BASE", _TEST_API_BASE)
    monkeypatch.setenv("VIDEO_MODEL_NAME", _TEST_MODEL)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with a clean slate for video-gen env vars."""
    _clear_video_env(monkeypatch)
    yield


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Force every httpx.AsyncClient() constructed by the module under test to
    route through a MockTransport, so no real network call is ever made."""
    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = _mock_transport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedAsyncClient)


def _speed_up_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the poll loop's interval/budget to the same value, so it runs
    exactly one real (but ~10ms) iteration instead of up to _MAX_POLL_SECONDS
    wall-clock seconds - ``elapsed`` is a plain accumulator incremented by
    ``_POLL_INTERVAL_SECONDS`` each pass (see _poll_job), so setting both to
    0.01 guarantees the loop body runs once and then exits (0.01 < 0.01 is
    False), deterministically."""
    monkeypatch.setattr(vg, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(vg, "_MAX_POLL_SECONDS", 0.01)


# ---------------------------------------------------------------------------
# _get_video_gen_api_credentials
# ---------------------------------------------------------------------------


def test_credentials_empty_when_video_model_config_unset():
    api_key, api_base, model = vg._get_video_gen_api_credentials()
    assert (api_key, api_base, model) == ("", "", "")


def test_credentials_read_only_from_video_model_config_panel(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch, api_key="sk-from-video-model-panel")

    api_key, api_base, model = vg._get_video_gen_api_credentials()

    assert api_key == "sk-from-video-model-panel"
    assert api_base == _TEST_API_BASE
    assert model == _TEST_MODEL


def test_credentials_partial_config_leaves_missing_fields_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEO_API_KEY", "sk-only-key-set")
    # VIDEO_API_BASE / VIDEO_MODEL_NAME intentionally left unset.

    api_key, api_base, model = vg._get_video_gen_api_credentials()

    assert api_key == "sk-only-key-set"
    assert api_base == ""
    assert model == ""


# ---------------------------------------------------------------------------
# _resolve_frame_reference
# ---------------------------------------------------------------------------


def test_resolve_frame_reference_empty_returns_none():
    assert vg._resolve_frame_reference("") == (None, None)
    assert vg._resolve_frame_reference("   ") == (None, None)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/frame.png",
        "https://example.com/frame.png",
        "data:image/png;base64,aGVsbG8=",
    ],
)
def test_resolve_frame_reference_passes_through_urls_and_data_uris(value: str):
    resolved, err = vg._resolve_frame_reference(value)
    assert resolved == value
    assert err is None


def test_resolve_frame_reference_missing_local_file_errors():
    resolved, err = vg._resolve_frame_reference("no/such/file.png")
    assert resolved is None
    assert err is not None
    assert "is not a URL/data URI and no such file exists" in err


def test_resolve_frame_reference_encodes_local_image_as_data_uri(tmp_path: Path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")

    resolved, err = vg._resolve_frame_reference(str(image_path))

    assert err is None
    assert resolved is not None
    assert resolved.startswith("data:image/png;base64,")
    import base64

    b64_part = resolved.split(",", 1)[1]
    assert base64.b64decode(b64_part) == image_path.read_bytes()


def test_resolve_frame_reference_unknown_mime_falls_back_to_png(tmp_path: Path):
    odd_file = tmp_path / "frame.unknownext"
    odd_file.write_bytes(b"bytes")

    resolved, err = vg._resolve_frame_reference(str(odd_file))

    assert err is None
    assert resolved.startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# _resolve_save_path
# ---------------------------------------------------------------------------


def test_resolve_save_path_defaults_to_agent_workspace_generated_videos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)

    target = vg._resolve_save_path(None, "video_abc.mp4")

    assert target == tmp_path / "generated_videos" / "video_abc.mp4"
    assert target.parent.is_dir()


def test_resolve_save_path_honors_custom_save_dir(tmp_path: Path):
    custom_dir = tmp_path / "my_videos"

    target = vg._resolve_save_path(str(custom_dir), "video_abc.mp4")

    assert target == custom_dir / "video_abc.mp4"
    assert custom_dir.is_dir()


# ---------------------------------------------------------------------------
# generate_video - guard clauses (no HTTP call should happen)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_video_without_api_key_returns_error(monkeypatch: pytest.MonkeyPatch):
    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made without an API key, got {request.url}")

    _patch_async_client(monkeypatch, _unexpected_request)

    result = await generate_video(prompt="a golden retriever puppy running")

    assert result == (
        "[ERROR]: video generation is not configured - set the Video Model "
        "API key, API URL, and model name in configuration settings."
    )


@pytest.mark.asyncio
async def test_generate_video_with_partial_config_returns_error(monkeypatch: pytest.MonkeyPatch):
    """Key set but API URL/model missing - no built-in default to fall back
    to, so this must be treated the same as fully unconfigured."""
    monkeypatch.setenv("VIDEO_API_KEY", "sk-test")

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made with partial config, got {request.url}")

    _patch_async_client(monkeypatch, _unexpected_request)

    result = await generate_video(prompt="a golden retriever puppy running")

    assert result == (
        "[ERROR]: video generation is not configured - set the Video Model "
        "API key, API URL, and model name in configuration settings."
    )


@pytest.mark.asyncio
async def test_check_video_status_with_partial_config_returns_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEO_API_KEY", "sk-test")

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made with partial config, got {request.url}")

    _patch_async_client(monkeypatch, _unexpected_request)

    result = await check_video_status(job_id="job-123")

    assert result == (
        "[ERROR]: video generation is not configured - set the Video Model "
        "API key, API URL, and model name in configuration settings."
    )


@pytest.mark.asyncio
async def test_generate_video_with_blank_prompt_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made with a blank prompt, got {request.url}")

    _patch_async_client(monkeypatch, _unexpected_request)

    result = await generate_video(prompt="   ")

    assert result == "[ERROR]: prompt is required."


@pytest.mark.asyncio
async def test_generate_video_with_missing_first_frame_file_returns_error(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_video_model_config(monkeypatch)

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made for an invalid frame path, got {request.url}")

    _patch_async_client(monkeypatch, _unexpected_request)

    result = await generate_video(
        prompt="a puppy running", first_frame_path="does/not/exist.png"
    )

    assert result.startswith("[ERROR]: first_frame_path")


# ---------------------------------------------------------------------------
# generate_video - happy path (job completes within the first poll window)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_video_success_downloads_and_saves_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_video_model_config(monkeypatch)
    video_bytes = b"fake-mp4-bytes"
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/api/v1/videos" and request.method == "POST":
            return httpx.Response(
                200, json={"id": "job-123", "status": "completed", "polling_url": None}
            )
        if request.url.path == "/api/v1/videos/job-123/content":
            return httpx.Response(200, content=video_bytes)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(
        prompt="a golden retriever puppy running through a meadow",
        aspect_ratio="16:9",
        resolution="480p",
        duration_seconds=5,
        save_dir=str(tmp_path),
    )

    assert "Video generated successfully!" in result
    saved_path = tmp_path / "video_job-123.mp4"
    assert str(saved_path) in result
    assert saved_path.read_bytes() == video_bytes

    submit_request = requests_seen[0]
    submitted_body = json.loads(submit_request.content)
    assert submitted_body["model"] == _TEST_MODEL
    assert submitted_body["prompt"] == "a golden retriever puppy running through a meadow"
    assert submitted_body["aspect_ratio"] == "16:9"
    assert submitted_body["resolution"] == "480p"
    assert submitted_body["duration"] == 5
    assert submit_request.headers["authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_generate_video_includes_frame_images_for_image_to_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_video_model_config(monkeypatch)
    frame_path = tmp_path / "first_frame.png"
    frame_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.url.path == "/api/v1/videos" and request.method == "POST":
            return httpx.Response(200, json={"id": "job-456", "status": "completed"})
        if request.url.path == "/api/v1/videos/job-456/content":
            return httpx.Response(200, content=b"video-bytes")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(
        prompt="animate this scene",
        first_frame_path=str(frame_path),
        save_dir=str(tmp_path),
    )

    assert "Video generated successfully!" in result
    submitted_body = json.loads(requests_seen[0].content)
    assert "frame_images" in submitted_body
    assert submitted_body["frame_images"][0]["frame_type"] == "first_frame"
    assert submitted_body["frame_images"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


# ---------------------------------------------------------------------------
# generate_video - error / edge paths from the remote API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_video_submit_failure_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request: invalid model")

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(prompt="a puppy running")

    assert result == "[ERROR]: video generation submit failed: 400 bad request: invalid model"


@pytest.mark.asyncio
async def test_generate_video_submit_without_job_id_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "queued"})

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(prompt="a puppy running")

    assert result.startswith("[ERROR]: video generation submit returned no job id")


@pytest.mark.asyncio
async def test_generate_video_terminal_failed_status_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "job-err", "status": "failed", "error": "model overloaded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(prompt="a puppy running")

    assert result == (
        "[ERROR]: video job job-err ended with status failed: model overloaded"
    )


@pytest.mark.asyncio
async def test_generate_video_still_running_after_poll_window_reports_job_id(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_video_model_config(monkeypatch)
    _speed_up_polling(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-slow", "status": "pending"})
        # Exactly one poll GET happens (see _speed_up_polling); it reports
        # still-running, so the loop exits after this single iteration.
        return httpx.Response(200, json={"id": "job-slow", "status": "running"})

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(prompt="a puppy running")

    assert "job-slow" in result
    assert "check_video_status" in result
    assert "job_id=job-slow" in result


@pytest.mark.asyncio
async def test_generate_video_http_error_during_submit_is_caught(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_async_client(monkeypatch, handler)

    result = await generate_video(prompt="a puppy running")

    assert result.startswith("[ERROR]: video generation request failed:")


# ---------------------------------------------------------------------------
# check_video_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_video_status_without_api_key_returns_error(monkeypatch: pytest.MonkeyPatch):
    result = await check_video_status(job_id="job-123")
    assert result == (
        "[ERROR]: video generation is not configured - set the Video Model "
        "API key, API URL, and model name in configuration settings."
    )


@pytest.mark.asyncio
async def test_check_video_status_with_blank_job_id_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)
    result = await check_video_status(job_id="   ")
    assert result == "[ERROR]: job_id is required."


@pytest.mark.asyncio
async def test_check_video_status_completed_downloads_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_video_model_config(monkeypatch)
    video_bytes = b"fake-mp4-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/job-789":
            return httpx.Response(200, json={"id": "job-789", "status": "completed"})
        if request.url.path == "/api/v1/videos/job-789/content":
            return httpx.Response(200, content=video_bytes)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _patch_async_client(monkeypatch, handler)

    result = await check_video_status(job_id="job-789", save_dir=str(tmp_path))

    assert "Video generated successfully!" in result
    assert (tmp_path / "video_job-789.mp4").read_bytes() == video_bytes


@pytest.mark.asyncio
async def test_check_video_status_still_running(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job-789", "status": "processing"})

    _patch_async_client(monkeypatch, handler)

    result = await check_video_status(job_id="job-789")

    assert result == "Video job job-789 is still processing."


@pytest.mark.asyncio
async def test_check_video_status_terminal_failure(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "job-789", "status": "cancelled", "error": "user cancelled"}
        )

    _patch_async_client(monkeypatch, handler)

    result = await check_video_status(job_id="job-789")

    assert result == (
        "[ERROR]: video job job-789 ended with status cancelled: user cancelled"
    )


@pytest.mark.asyncio
async def test_check_video_status_non_200_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="job not found")

    _patch_async_client(monkeypatch, handler)

    result = await check_video_status(job_id="job-missing")

    assert result == "[ERROR]: checking video job job-missing failed: 404 job not found"


@pytest.mark.asyncio
async def test_check_video_status_http_error_is_caught(monkeypatch: pytest.MonkeyPatch):
    _set_video_model_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_async_client(monkeypatch, handler)

    result = await check_video_status(job_id="job-789")

    assert result.startswith("[ERROR]: checking video job job-789 failed:")


# ---------------------------------------------------------------------------
# _download_video / _poll_job (direct helper coverage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_video_non_200_returns_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    _patch_async_client(monkeypatch, handler)

    async with httpx.AsyncClient() as client:
        result = await vg._download_video(client, {}, _TEST_API_BASE, "job-x", None)

    assert result == "[ERROR]: video job job-x completed but downloading content failed: 500"


@pytest.mark.asyncio
async def test_poll_job_raises_on_non_200_poll_response(monkeypatch: pytest.MonkeyPatch):
    _speed_up_polling(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    _patch_async_client(monkeypatch, handler)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="polling video job job-y failed: 503"):
            await vg._poll_job(client, {}, "job-y", f"{_TEST_API_BASE}/videos/job-y", "pending")
