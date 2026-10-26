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
import requests

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
async def test_minimax_image_explicit_size_sends_width_and_height(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, lambda r: _mm_ok_image())
    target = gt.GenerationTarget("minimax", "k", _MM_GLOBAL, "image-01")
    await gt.generate_image(target, "fox", "16:9", str(tmp_path), gt.ImageOptions(size="1424x800"))
    body = _body(seen[0])
    assert (body["width"], body["height"]) == (1424, 800) and "aspect_ratio" not in body


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
        "minimax", "k", _MM_GLOBAL, "MiniMax-H3", "fox", "9:16", "1080p", 15, True,
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


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "api_base"),
    [("minimax", _MM_CHINA), ("modelark", _ARK_INTL), ("dashscope", "https://dashscope.aliyuncs.com/api/v1")],
)
async def test_check_video_keeps_job_pending_when_provider_unreachable(monkeypatch, tmp_path, backend, api_base):
    _patch_client(monkeypatch, _unreachable)
    result = await _check(backend, "k", api_base, "T1", str(tmp_path))
    assert result.startswith("Video job T1 is still unconfirmed") and "getaddrinfo failed" in result


@pytest.mark.asyncio
async def test_minimax_check_video_keeps_job_pending_when_download_times_out(monkeypatch, tmp_path):
    succeeded = _mm_video_handler(["succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            raise httpx.ReadTimeout("timed out", request=request)
        return succeeded(request)

    _patch_client(monkeypatch, handler)
    result = await _check("minimax", "k", _MM_GLOBAL, "T1", str(tmp_path))
    assert result.startswith("Video job T1 is still unconfirmed")


@pytest.mark.asyncio
async def test_minimax_video_submit_polls_past_a_dns_failure(monkeypatch, tmp_path):
    rendering = _mm_video_handler(["running", "succeeded"])
    failures = iter([True])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/query/") and next(failures, False):
            return _unreachable(request)
        return rendering(request)

    _patch_client(monkeypatch, handler)
    result = await _submit("minimax", "k", _MM_GLOBAL, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    assert result.startswith("Video generated successfully!")


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
@pytest.mark.parametrize("base", [_ARK_CN, _ARK_INTL])
@pytest.mark.parametrize("operation", ["image", "submit", "check"])
async def test_modelark_auth_failure_stays_on_configured_region(monkeypatch, tmp_path, base, operation):
    seen = _patch_client(monkeypatch, lambda r: _ark_error("AuthenticationError", "bad key", 401))
    if operation == "image":
        result = await _image("modelark", "k", base, "m", "p", "1:1", str(tmp_path))
    elif operation == "submit":
        result = await _submit("modelark", "k", base, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    else:
        result = await _check("modelark", "k", base, "cgt-1", str(tmp_path))
    assert result.startswith("[ERROR]: ModelArk request failed on the configured region")
    assert len(seen) == 1 and str(seen[0].url).startswith(f"{base}/")


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://proxy.example/api/v3", "https://ark.cn-beijing.volces.com/api/coding/v3"])
async def test_modelark_generation_rejects_non_regular_official_api_before_sending_key(monkeypatch, tmp_path, base):
    seen = _patch_client(monkeypatch, lambda r: pytest.fail("unexpected request"))
    image = await _image("modelark", "k", base, "m", "p", "1:1", str(tmp_path))
    submit = await _submit("modelark", "k", base, "m", "p", "16:9", "720p", 5, False, None, str(tmp_path))
    check = await _check("modelark", "k", base, "cgt-1", str(tmp_path))
    assert all(result.startswith("[ERROR]: Use an official regional Ark") for result in (image, submit, check))
    assert not seen


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
async def test_modelark_seedance_25_allows_duration_above_15(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _ark_video_handler(["succeeded"]))
    await _submit(
        "modelark", "ak", _ARK_INTL, "doubao-seedance-2-5-260628", "fox", "16:9", "720p", 25, False,
        None, str(tmp_path),
    )
    assert _body(seen[0])["duration"] == 25


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


# --------------------------------------------------------------------------- #
# Reference images (MiniMax / ModelArk)
# --------------------------------------------------------------------------- #

_REF_A = "data:image/png;base64,QUFB"
_REF_B = "https://cdn.example/b.png"


def _ref_request(**overrides: Any) -> gt.VideoRequest:
    fields: dict[str, Any] = {
        "prompt": "fox", "aspect_ratio": "16:9", "resolution": "720p", "duration_seconds": 5,
        "reference_image_uris": (_REF_A, _REF_B),
    }
    fields.update(overrides)
    return gt.VideoRequest(**fields)


@pytest.mark.parametrize(
    "first_frame,reference_mode,expected_roles",
    [
        (None, False, ["reference_image", "reference_image"]),
        ("data:image/png;base64,RkY=", False, ["first_frame", "reference_image", "reference_image"]),
        # reference mode folds the first frame into the references
        ("data:image/png;base64,RkY=", True, ["reference_image", "reference_image", "reference_image"]),
        # a first frame that is already a reference is not sent twice
        (_REF_A, False, ["reference_image", "reference_image"]),
    ],
)
def test_video_content_roles(first_frame, reference_mode, expected_roles):
    request = _ref_request(first_frame_data_uri=first_frame, reference_mode=reference_mode)
    frame, refs = gt._video_references(request)
    content = gt._video_content("fox", frame, refs)
    assert content[0] == {"type": "text", "text": "fox"}
    assert [part["role"] for part in content[1:]] == expected_roles


@pytest.mark.asyncio
async def test_minimax_and_modelark_video_send_reference_images(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _mm_video_handler(["succeeded"]))
    await gt.submit_video(gt.GenerationTarget("minimax", "k", _MM_GLOBAL, "MiniMax-H3"), _ref_request(), str(tmp_path))
    assert [part.get("role") for part in _body(seen[0])["content"]] == [None, "reference_image", "reference_image"]

    seen = _patch_client(monkeypatch, _ark_video_handler(["succeeded"]))
    await gt.submit_video(gt.GenerationTarget("modelark", "k", _ARK_INTL, "m"), _ref_request(), str(tmp_path))
    body = _body(seen[0])
    assert [part.get("role") for part in body["content"]] == [None, "reference_image", "reference_image"]
    assert body["ratio"] == "16:9"  # only a literal first frame makes the ratio adaptive


@pytest.mark.asyncio
async def test_extra_video_options_are_vllm_omni_only():
    target = gt.GenerationTarget("minimax", "k", _MM_GLOBAL, "MiniMax-H3")
    result = await gt.submit_video(target, _ref_request(extra={"seed": 1}), None)
    assert result == "[ERROR]: extra video request options are only supported by vLLM-Omni."


@pytest.mark.asyncio
async def test_modelark_image_explicit_size_and_references(monkeypatch, tmp_path):
    data = [{"b64_json": base64.b64encode(_PNG).decode()}]
    seen = _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"data": data}))
    options = gt.ImageOptions(size="1280*720", reference_image_uris=(_REF_A,))
    await gt.generate_image(gt.GenerationTarget("modelark", "k", _ARK_INTL, "m"), "fox", "16:9", str(tmp_path), options)
    body = _body(seen[0])
    assert body["size"] == "1280x720"
    assert body["image"] == [_REF_A] and body["prompt"] == "fox"


# --------------------------------------------------------------------------- #
# DashScope
# --------------------------------------------------------------------------- #

_DS_CN = "https://dashscope.aliyuncs.com/api/v1"
_DS_INTL = "https://dashscope-intl.aliyuncs.com/api/v1"


@pytest.mark.parametrize(
    "protocol,base,expected",
    [
        ("dashscope", "https://anything.example/v1", "dashscope"),
        ("vllm-omni", "http://127.0.0.1:8091/v1", "vllm-omni"),
        ("", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "dashscope"),
        ("", "http://127.0.0.1:8091/v1", None),  # a local server is only vLLM-Omni when the 协议 says so
    ],
)
def test_detect_new_backends(monkeypatch, protocol, base, expected):
    monkeypatch.setenv("TEST_GEN_PROTOCOL", protocol)
    assert gt.detect_backend("TEST_GEN_PROTOCOL", base) == expected


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", _DS_CN),
        ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1/", _DS_INTL),
        (_DS_INTL, _DS_INTL),
        ("", _DS_CN),
    ],
)
def test_dashscope_base(configured, expected):
    assert gt._dashscope_base(configured) == expected


def _ds_video_handler(statuses: list[str], *, rejected_host: str = "") -> Handler:
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == rejected_host:
            return httpx.Response(401, json={"code": "InvalidApiKey", "message": "Invalid API-key provided."})
        if request.url.path.endswith("/services/aigc/video-generation/video-synthesis"):
            return httpx.Response(200, json={"output": {"task_id": "ds-1", "task_status": "PENDING"}})
        if request.url.path.endswith("/tasks/ds-1"):
            status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            output: dict[str, Any] = {"task_id": "ds-1", "task_status": status}
            if status == "SUCCEEDED":
                output["video_url"] = "https://cdn.example/v.mp4"
            if status == "FAILED":
                output.update(code="DataInspectionFailed", message="unsafe")
            return httpx.Response(200, json={"output": output})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_MP4)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_dashscope_wan3_reference_video(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _ds_video_handler(["RUNNING", "SUCCEEDED"]))
    request = _ref_request(size="1280*720", generate_audio=True)
    target = gt.GenerationTarget("dashscope", "sk", _DS_CN, "wan3.0-video")
    result = await gt.submit_video(target, request, str(tmp_path))
    assert result.startswith("Video generated successfully!")
    assert (tmp_path / "video_ds-1.mp4").read_bytes() == _MP4
    submit = seen[0]
    assert submit.headers["x-dashscope-async"] == "enable"
    body = _body(submit)
    assert body["model"] == "wan3.0-video"
    assert body["input"]["media"] == [
        {"type": "reference_image", "url": _REF_A}, {"type": "reference_image", "url": _REF_B},
    ]
    assert body["parameters"] == {"duration": 5, "audio": True, "size": "1280*720", "ratio": "16:9"}
    assert str(seen[1].url) == f"{_DS_CN}/tasks/ds-1"


@pytest.mark.parametrize(
    "model,first_frame,reference_uris,expected_input,expected_parameters",
    [
        # wan3 with a lone first frame is image-to-video at a resolution tier
        ("wan3.0-video-prime", "data:image/png;base64,RkY=", (), {"img_url": "data:image/png;base64,RkY="},
         {"duration": 5, "audio": False, "resolution": "720P"}),
        # wan3 text-to-video
        ("wan3.0-video", None, (), {}, {"duration": 5, "audio": False, "size": "1280*720"}),
        # wan2.x references ride reference_urls with a multi-shot layout
        ("wan2.6-r2v", None, (_REF_A,), {"reference_urls": [_REF_A]},
         {"duration": 5, "size": "1280*720", "shot_type": "multi"}),
    ],
)
def test_dashscope_video_body_modes(model, first_frame, reference_uris, expected_input, expected_parameters):
    request = gt.VideoRequest("fox", "16:9", "720p", 5, first_frame_data_uri=first_frame,
                              reference_image_uris=reference_uris)
    body = gt._dashscope_video_body(model, request, None)
    assert body["input"] == {"prompt": "fox", **expected_input}
    assert body["parameters"] == expected_parameters


def test_dashscope_video_size_snaps_480p():
    request = gt.VideoRequest("fox", "16:9", "480p", 5, size="854*480")
    assert gt._dashscope_video_size(request) == "832*480"
    assert gt._dashscope_video_size(gt.VideoRequest("fox", "9:16", "1080p", 5)) == "1080*1920"


@pytest.mark.asyncio
async def test_dashscope_wan3_file_reference_is_uploaded(monkeypatch, tmp_path):
    storyboard = tmp_path / "storyboard.md"
    storyboard.write_text("# shots")
    uploads: list[tuple[str, str, str]] = []

    def fake_upload(model: str, path: str, api_key: str, base: str) -> str:
        uploads.append((model, path, base))
        return "oss://bucket/storyboard.md"

    monkeypatch.setattr(gt, "_dashscope_upload_file", fake_upload)
    seen = _patch_client(monkeypatch, _ds_video_handler(["SUCCEEDED"]))
    request = gt.VideoRequest("fox", "16:9", "720p", 5, reference_file_path=str(storyboard))
    await gt.submit_video(gt.GenerationTarget("dashscope", "sk", _DS_CN, "wan3.0-video"), request, str(tmp_path))
    assert uploads == [("wan3.0-video", str(storyboard), _DS_CN)]
    assert seen[0].headers["x-dashscope-ossresourceresolve"] == "enable"
    assert _body(seen[0])["input"]["media"] == [{"type": "file", "url": "oss://bucket/storyboard.md"}]


@pytest.mark.asyncio
async def test_dashscope_region_failover_pending_and_failed(monkeypatch, tmp_path):
    seen = _patch_client(monkeypatch, _ds_video_handler(["SUCCEEDED"], rejected_host="dashscope.aliyuncs.com"))
    target = gt.GenerationTarget("dashscope", "sk", _DS_CN, "wan3.0-video")
    result = await gt.submit_video(target, _ref_request(), str(tmp_path))
    assert result.startswith("Video generated successfully!")
    assert [r.url.host for r in seen[:2]] == ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"]
    assert seen[2].url.host == "dashscope-intl.aliyuncs.com"  # polling stays on the region that accepted the key

    _patch_client(monkeypatch, _ds_video_handler(["RUNNING"]))
    pending = await gt.submit_video(target, _ref_request(), str(tmp_path))
    assert "Video job ds-1 submitted and still running" in pending
    assert "still running" in await gt.check_video(target, "ds-1", str(tmp_path))

    _patch_client(monkeypatch, _ds_video_handler(["FAILED"]))
    failed = await gt.check_video(target, "ds-1", str(tmp_path))
    assert failed == "[ERROR]: video job ds-1 ended with status failed: DataInspectionFailed unsafe"


@pytest.mark.asyncio
async def test_dashscope_qwen_image_with_references(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/aigc/multimodal-generation/generation"):
            content = [{"image": "https://cdn.example/i.png"}]
            return httpx.Response(200, json={"output": {"choices": [{"message": {"content": content}}]}})
        return httpx.Response(200, content=_PNG)

    seen = _patch_client(monkeypatch, handler)
    options = gt.ImageOptions(size="1K", reference_image_uris=(_REF_A,))
    compatible_mode = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    target = gt.GenerationTarget("dashscope", "sk", compatible_mode, "qwen-image-3.0")
    result = await gt.generate_image(target, "a fox", "16:9", str(tmp_path), options)
    assert _saved_path(result).read_bytes() == _PNG
    assert str(seen[0].url) == f"{_DS_CN}/services/aigc/multimodal-generation/generation"
    body = _body(seen[0])
    assert body["input"]["messages"][0]["content"] == [{"image": _REF_A}, {"text": "a fox"}]
    assert body["parameters"] == {"size": "1024*1024", "n": 1, "watermark": False}


# --------------------------------------------------------------------------- #
# vLLM-Omni
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_vllm_omni_video_submit_poll_download(monkeypatch, tmp_path):
    submitted: dict[str, Any] = {}
    statuses = ["in_progress", "completed"]

    def fake_submit(prompt: str, **kwargs: Any) -> str:
        submitted.update(prompt=prompt, **kwargs)
        return "vid-1"

    def fake_query(**kwargs: Any) -> gt.vllm_omni_gen.VllmOmniVideoStatus:
        return gt.vllm_omni_gen.VllmOmniVideoStatus(statuses.pop(0))

    def fake_download(*, output_path: Path, **kwargs: Any) -> Path:
        output_path.write_bytes(_MP4)
        return output_path

    monkeypatch.setattr(gt.vllm_omni_gen, "submit_vllm_omni_video_sync", fake_submit)
    monkeypatch.setattr(gt.vllm_omni_gen, "query_vllm_omni_video_sync", fake_query)
    monkeypatch.setattr(gt.vllm_omni_gen, "download_vllm_omni_video_sync", fake_download)
    target = gt.GenerationTarget("vllm-omni", "", "http://127.0.0.1:8091/v1", "")
    request = _ref_request(first_frame_data_uri="data:image/png;base64,RkY=", extra={"seed": 7})
    result = await gt.submit_video(target, request, str(tmp_path))
    assert (tmp_path / "video_vid-1.mp4").read_bytes() == _MP4 and "Saved to:" in result
    assert submitted["first_frame"] == "data:image/png;base64,RkY="
    assert submitted["reference_images"] == [_REF_A, _REF_B]
    assert submitted["size"] == "1280*720" and submitted["seed"] == 7 and submitted["api_key"] == ""


@pytest.mark.asyncio
async def test_vllm_omni_check_video_reports_failure(monkeypatch):
    monkeypatch.setattr(
        gt.vllm_omni_gen, "query_vllm_omni_video_sync",
        lambda **kwargs: gt.vllm_omni_gen.VllmOmniVideoStatus("failed", "OOM"),
    )
    target = gt.GenerationTarget("vllm-omni", "", "http://127.0.0.1:8091/v1", "")
    assert await gt.check_video(target, "vid-1", None) == "[ERROR]: video job vid-1 ended with status failed: OOM"


@pytest.mark.asyncio
async def test_vllm_omni_check_video_keeps_job_pending_when_server_unreachable(monkeypatch):
    def unreachable(**kwargs: Any) -> gt.vllm_omni_gen.VllmOmniVideoStatus:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(gt.vllm_omni_gen, "query_vllm_omni_video_sync", unreachable)
    target = gt.GenerationTarget("vllm-omni", "", "http://127.0.0.1:8091/v1", "")
    assert (await gt.check_video(target, "vid-1", None)).startswith("Video job vid-1 is still unconfirmed")


@pytest.mark.parametrize(
    ("aspect_ratio", "resolution", "expected"),
    [
        ("9:16", "720P", "720*1280"),
        ("16:9", "768P", "1360*768"),
        ("9:16", "2K", "1440*2560"),
        ("21:9", "1080p", "2192*944"),
        ("4:3", "480P", "736*560"),
        ("16:9", "8K", None),
    ],
)
def test_vllm_omni_size_covers_every_tier_and_ratio(aspect_ratio, resolution, expected):
    assert gt._vllm_omni_size(None, aspect_ratio, resolution) == expected
    assert gt._vllm_omni_size("640*480", aspect_ratio, resolution) == "640*480"
