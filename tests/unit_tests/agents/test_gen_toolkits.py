# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.agents.harness.common.tools.gen_toolkits.

Every HTTP call is routed through an httpx.MockTransport, so no real network
request (and no real API key) is involved.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from jiuwenswarm.agents.harness.common.tools import gen_toolkits as gt

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32

_MM_GLOBAL = "https://api.minimax.io/v1"
_MM_CHINA = "https://api.minimaxi.com/v1"
_MM_BAD_KEY = {"base_resp": {"status_code": 2049, "status_msg": "invalid api key"}}
_ARK_INTL = "https://ark.ap-southeast.bytepluses.com/api/v3"
_ARK_CN = "https://ark.cn-beijing.volces.com/api/v3"

Handler = Callable[[httpx.Request], httpx.Response]
_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured once so repeated patching in one test never stacks


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route every httpx.AsyncClient built by the module through ``handler``;
    returns the list that records each request made."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    class _Patched(_REAL_ASYNC_CLIENT):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(recording)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)
    return seen


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """Polling loops must not really wait between polls."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gt.asyncio, "sleep", instant)


# Thin adapters that keep the call sites below flat: (backend, api_key, api_base, model, ...).
def _image(backend, api_key, api_base, model, prompt, aspect_ratio, save_dir):
    return gt.generate_image(gt.GenerationTarget(backend, api_key, api_base, model), prompt, aspect_ratio, save_dir)


def _submit(backend, api_key, api_base, model, prompt, ratio, resolution, seconds, audio, first_frame, save_dir):
    request = gt.VideoRequest(prompt, ratio, resolution, seconds, audio, first_frame)
    return gt.submit_video(gt.GenerationTarget(backend, api_key, api_base, model), request, save_dir)


def _check(backend, api_key, api_base, task_id, save_dir):
    return gt.check_video(gt.GenerationTarget(backend, api_key, api_base, ""), task_id, save_dir)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode())


def _saved_path(result: str) -> Path:
    assert "Saved to: " in result, result
    return Path(result.split("Saved to: ", 1)[1].splitlines()[0].split(", ")[0].strip())


def _ark_error(code: str, message: str = "msg", status: int = 404) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


# --------------------------------------------------------------------------- #
# Backend detection and dispatch
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "protocol,base,expected",
    [
        ("minimax", "https://anything.example/v1", "minimax"),  # saved 协议 wins
        ("modelark", "https://anything.example/v1", "modelark"),
        ("", _MM_CHINA, "minimax"),  # otherwise the host decides
        ("", "https://ark.cn-beijing.volces.com/api/coding/v3", "modelark"),
        ("", "https://openrouter.ai/api/v1", None),
        ("", "https://notminimax.io/v1", None),
    ],
)
def test_detect_backend(monkeypatch, protocol, base, expected):
    monkeypatch.setenv("TEST_GEN_PROTOCOL", protocol)
    assert gt.detect_backend("TEST_GEN_PROTOCOL", base) == expected


@pytest.mark.asyncio
async def test_unknown_backend_is_an_error():
    assert "unknown generation backend" in await _image("nope", "k", "b", "m", "p", "1:1", None)
    assert "unknown generation backend" in await _check("nope", "k", "b", "id", None)


# --------------------------------------------------------------------------- #
# MiniMax
# --------------------------------------------------------------------------- #

def _mm_ok_image() -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"image_base64": [base64.b64encode(_PNG).decode()]}, "base_resp": {"status_code": 0}}
    )


@pytest.mark.asyncio
async def test_minimax_image_success(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, lambda r: _mm_ok_image())
    result = await _image("minimax", "sk-x", _MM_GLOBAL, "image-01", "a fox", "16:9", str(tmp_path))
    saved = _saved_path(result)
    assert saved.parent == tmp_path and saved.read_bytes() == _PNG
    assert str(seen[0].url) == "https://api.minimax.io/v1/image_generation"
    assert seen[0].headers["authorization"] == "Bearer sk-x"
    assert _body(seen[0])["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_minimax_region_failover_and_bad_key(monkeypatch, tmp_path):
    def china_rejects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_MM_BAD_KEY) if request.url.host == "api.minimaxi.com" else _mm_ok_image()

    seen = _patch_client(monkeypatch, china_rejects)
    result = await _image("minimax", "k", _MM_CHINA, "image-01", "p", "1:1", str(tmp_path))
    assert result.startswith("Image generated successfully!")
    assert [r.url.host for r in seen] == ["api.minimaxi.com", "api.minimax.io"]

    seen = _patch_client(monkeypatch, lambda r: httpx.Response(200, json=_MM_BAD_KEY))
    result = await _image("minimax", "k", _MM_GLOBAL, "image-01", "p", "1:1", str(tmp_path))
    assert result.startswith("[ERROR]: MiniMax rejected the API key") and len(seen) == 2


def _mm_video_handler(statuses: list[str], *, error: Any = None) -> Handler:
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/video_generation":
            return httpx.Response(200, json={"task_id": "T1", "base_resp": {"status_code": 0}})
        if request.url.path == "/v2/query/video_generation/T1":
            status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            task: dict[str, Any] = {"status": status}
            if status == "succeeded":
                task["content"] = {"url": "https://cdn.example/v.mp4"}
            if error is not None:
                task["error"] = error
            return httpx.Response(200, json={"task": task, "base_resp": {"status_code": 0}})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_MP4)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_minimax_video_submit_poll_download(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _mm_video_handler(["queued", "running", "succeeded"]))
    result = await _submit(
        "minimax", "k", _MM_GLOBAL, "MiniMax-H3", "fox", "9:16", "1080p", 99, True,
        "data:image/png;base64,AAA", str(tmp_path),
    )
    assert result.startswith("Video generated successfully!")
    assert (tmp_path / "video_T1.mp4").read_bytes() == _MP4
    body = _body(seen[0])
    assert body["resolution"] == "2K" and body["duration"] == 15 and body["ratio"] == "9:16"
    assert body["content"][1]["role"] == "first_frame"
    assert "authorization" not in seen[-1].headers  # the pre-signed download must not carry the key


@pytest.mark.asyncio
async def test_minimax_video_pending_and_failed(monkeypatch, tmp_path):
    _patch_client(monkeypatch, _mm_video_handler(["running"]))
    pending = await _submit("minimax", "k", _MM_GLOBAL, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    assert "Video job T1 submitted and still running" in pending and "job_id=T1" in pending

    _patch_client(monkeypatch, _mm_video_handler(["failed"], error={"code": "E9", "message": "content policy"}))
    failed = await _submit("minimax", "k", _MM_GLOBAL, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    assert failed == "[ERROR]: video job T1 ended with status failed: E9 content policy"


@pytest.mark.asyncio
async def test_minimax_check_video(monkeypatch, tmp_path):
    _patch_client(monkeypatch, _mm_video_handler(["running"]))
    assert await _check("minimax", "k", _MM_GLOBAL, "T1", str(tmp_path)) == "Video job T1 is still running."
    _patch_client(monkeypatch, _mm_video_handler(["succeeded"]))
    result = await _check("minimax", "k", _MM_GLOBAL, "T1", str(tmp_path))
    assert result.startswith("Video generated successfully!") and (tmp_path / "video_T1.mp4").exists()


# --------------------------------------------------------------------------- #
# ModelArk
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_modelark_image_success_always_requests_2k(monkeypatch, tmp_path):
    data = [{"b64_json": base64.b64encode(_PNG).decode()}]
    seen = _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"data": data}))
    result = await _image("modelark", "ak", _ARK_INTL, "seedream-5-0-260128", "a fox", "16:9", str(tmp_path))
    assert _saved_path(result).read_bytes() == _PNG
    assert str(seen[0].url) == f"{_ARK_INTL}/images/generations"
    body = _body(seen[0])
    # the lite model only accepts 2k/3k/4k/WxH, so the tier is always 2k
    assert body["size"] == "2k" and body["watermark"] is False and "16:9" in body["prompt"]


@pytest.mark.asyncio
async def test_modelark_model_not_activated_gives_account_hint_without_retry(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, lambda r: _ark_error("ModelNotOpen", "not activated"))
    result = await _image("modelark", "k", _ARK_INTL, "dola-seedream-5-0-pro-260628", "p", "1:1", str(tmp_path))
    assert result.startswith("[ERROR]: ModelArk image generation failed: ModelNotOpen")
    assert "Activate this model" in result and "report it to the user" in result
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_modelark_region_failover_and_bad_key(monkeypatch, tmp_path):
    data = [{"b64_json": base64.b64encode(_PNG).decode()}]

    def only_intl_works(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ark.ap-southeast.bytepluses.com":
            return httpx.Response(200, json={"data": data})
        return _ark_error("AuthenticationError", "bad key", 401)

    seen = _patch_client(monkeypatch, only_intl_works)
    result = await _image("modelark", "k", _ARK_CN, "m", "p", "1:1", str(tmp_path))
    assert result.startswith("Image generated successfully!") and len(seen) == 2

    seen = _patch_client(monkeypatch, lambda r: _ark_error("AuthenticationError", "bad", 401))
    result = await _image("modelark", "k", _ARK_INTL, "m", "p", "1:1", str(tmp_path))
    assert result.startswith("[ERROR]: ModelArk request failed on every region host tried") and len(seen) == 3


def _ark_video_handler(statuses: list[str], *, error: Any = None) -> Handler:
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/contents/generations/tasks"):
            return httpx.Response(200, json={"id": "cgt-1"})
        if request.url.path.endswith("/contents/generations/tasks/cgt-1"):
            status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            payload: dict[str, Any] = {"id": "cgt-1", "status": status}
            if status == "succeeded":
                payload["content"] = {"video_url": "https://cdn.example/v.mp4"}
            if error is not None:
                payload["error"] = error
            return httpx.Response(200, json=payload)
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_MP4)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_modelark_video_submit_poll_download(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _ark_video_handler(["queued", "running", "succeeded"]))
    result = await _submit(
        "modelark", "ak", _ARK_INTL, "dreamina-seedance-2-5-260628", "fox", "16:9", "1080p", 5, True,
        "data:image/png;base64,AAA", str(tmp_path),
    )
    assert result.startswith("Video generated successfully!")
    assert (tmp_path / "video_cgt-1.mp4").read_bytes() == _MP4
    body = _body(seen[0])
    assert body["ratio"] == "adaptive"  # with a first frame the output follows that frame
    assert body["generate_audio"] is True and body["watermark"] is False and body["resolution"] == "1080p"
    assert "authorization" not in seen[-1].headers


@pytest.mark.asyncio
async def test_modelark_video_usage_limit_gets_account_hint(monkeypatch, tmp_path):
    error = {"code": "SetLimitExceeded", "message": "usage limit reached"}
    _patch_client(monkeypatch, _ark_video_handler(["failed"], error=error))
    result = await _submit("modelark", "k", _ARK_INTL, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    assert "ended with status failed: SetLimitExceeded" in result
    assert "Safe Experience Mode" in result and "report it to the user" in result


@pytest.mark.asyncio
async def test_modelark_video_pending_tells_agent_not_to_shell_sleep(monkeypatch, tmp_path):
    _patch_client(monkeypatch, _ark_video_handler(["running"]))
    pending = await _submit("modelark", "k", _ARK_INTL, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    assert "Video job cgt-1 submitted and still running" in pending and "do not use shell sleep" in pending
    check = await _check("modelark", "k", _ARK_INTL, "cgt-1", str(tmp_path))
    assert "still running" in check and "do not use shell sleep" in check
    # a 5 s Seedance clip takes ~2.5 min to render; the in-call wait must outlast that
    assert gt._MODELARK_MAX_POLL_SECONDS >= 240
