# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
import json

import pytest

from jiuwenswarm.agents.harness.common.tools import vllm_omni_gen
from jiuwenswarm.agents.harness.common.tools.vllm_omni_gen import (
    VllmOmniVideoInputs,
)


@pytest.mark.parametrize(
    ("served_model_id", "expected_key"),
    [
        ("MiniMaxAI/MiniMax-H3", "minimax-h3"),
        ("/models/MiniMax-H3", "minimax-h3"),
        ("/models/MiniMax-H3/FL2VA", "minimax-h3"),
        ("minimax-h3-max", "minimax-h3"),
        ("Qwen/Qwen-Image", None),
        ("Wan-AI/Wan2.2-T2V-A14B-Diffusers", None),
        ("", None),
        (None, None),
    ],
)
def test_match_video_spec(served_model_id: str | None, expected_key: str | None) -> None:
    spec = vllm_omni_gen.match_video_spec(served_model_id)
    assert (spec.key if spec else None) == expected_key


def _h3_spec() -> vllm_omni_gen.VllmOmniVideoSpec:
    spec = vllm_omni_gen.match_video_spec("MiniMaxAI/MiniMax-H3")
    assert spec is not None
    return spec


def test_minimax_h3_t2va_form_hardcodes_recipe_sampling() -> None:
    form = _h3_spec().build(
        VllmOmniVideoInputs(size="1344*768", duration=8, resolution=None, has_references=False)
    )

    assert form.fields["num_inference_steps"] == "50"
    assert form.fields["flow_shift"] == "12.0"
    assert form.fields["width"] == "1344"
    assert form.fields["height"] == "768"
    # T2VA requires one named output ratio.
    assert form.fields["aspect_ratio"] == "16:9"
    assert "short_edge" not in form.fields
    # seed / quality are never sent.
    assert "seed" not in form.fields
    assert "quality" not in form.fields
    assert form.extra_params == {"task": "t2va", "duration": 8.0, "audio_flow_shift": 3.0}


def test_minimax_h3_ref2va_form_with_explicit_size() -> None:
    form = _h3_spec().build(
        VllmOmniVideoInputs(size="1280*720", duration=5, resolution="768P", has_references=True)
    )

    # 720 is snapped to a 32-pixel canvas multiple.
    assert form.fields["width"] == "1280"
    assert form.fields["height"] in {"704", "736"}
    # Ref2VA with explicit dimensions sends no aspect_ratio / short_edge.
    assert "aspect_ratio" not in form.fields
    assert "short_edge" not in form.fields
    assert form.extra_params is not None
    assert form.extra_params["task"] == "ref2va"
    assert form.extra_params["audio_flow_shift"] == 3.0


def test_minimax_h3_ref2va_form_without_size_uses_adaptive_canvas() -> None:
    form = _h3_spec().build(
        VllmOmniVideoInputs(size=None, duration=None, resolution=None, has_references=True)
    )

    assert "width" not in form.fields
    assert "height" not in form.fields
    assert form.fields["aspect_ratio"] == "adaptive"
    assert form.fields["short_edge"] == "768"
    assert form.extra_params is not None
    # Unspecified duration uses the catalog default (H3 default_sec=5).
    assert form.extra_params["duration"] == 5.0


def test_minimax_h3_duration_keeps_the_4s_floor_and_snaps_to_catalog_max() -> None:
    spec = _h3_spec()
    low = spec.build(
        VllmOmniVideoInputs(
            size=None, duration=1, resolution=None, has_references=False, model_id="MiniMax-H3"
        )
    )
    high = spec.build(
        VllmOmniVideoInputs(
            size=None, duration=30, resolution=None, has_references=False, model_id="MiniMax-H3"
        )
    )

    assert low.extra_params is not None and low.extra_params["duration"] == 4.0
    assert high.extra_params is not None and high.extra_params["duration"] == 15.0


def test_minimax_h3_max_vllm_form_snaps_duration_to_5() -> None:
    spec = vllm_omni_gen.match_video_spec("MiniMaxAI/MiniMax-H3-Max")
    assert spec is not None and spec.key == "minimax-h3-max"
    form = spec.build(
        VllmOmniVideoInputs(
            size=None,
            duration=4,
            resolution=None,
            has_references=False,
            model_id="MiniMax-H3-Max",
        )
    )
    assert form.extra_params is not None and form.extra_params["duration"] == 5.0


def test_generic_video_form_never_builds_extra_params() -> None:
    form = vllm_omni_gen._build_generic_video_form(
        VllmOmniVideoInputs(size="1280*720", duration=6, resolution=None, has_references=False)
    )

    assert form.extra_params is None
    assert form.fields == {"size": "1280x720", "seconds": "6"}


class _Resp:
    def __init__(self, ok: bool, payload: object, status_code: int = 200, content: bytes = b""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload
        self.content = content or b"fake-bytes"
        self.text = str(payload)

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"http {self.status_code}")


def test_invoke_video_two_request_flow_with_registered_h3(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "MiniMaxAI/MiniMax-H3"}]})
        if method == "POST" and url.endswith("/videos"):
            posts.append(kwargs)
            return _Resp(True, {"id": "video-1", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-1"):
            return _Resp(True, {"id": "video-1", "status": "completed"})
        if method == "GET" and url.endswith("/videos/video-1/content"):
            return _Resp(True, {}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    result = vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
        "a cat runs",
        api_key="",
        api_base="http://127.0.0.1:8091/v1",
        model="",
        size="1344*768",
        duration=8,
        resolution=None,
    )

    assert result["video_path"].endswith(".mp4")
    fields = posts[0]["data"]
    # The served model id wins over an (empty) configured name.
    assert fields["model"] == "MiniMaxAI/MiniMax-H3"
    assert fields["fps"] == "24"
    assert fields["num_inference_steps"] == "50"
    assert fields["flow_shift"] == "12.0"
    assert "seed" not in fields
    assert "quality" not in fields
    extra = json.loads(fields["extra_params"])
    assert extra == {"task": "t2va", "duration": 8.0, "audio_flow_shift": 3.0}
    # No references -> no multipart files.
    assert not posts[0]["files"]


def test_invoke_video_ref2va_uploads_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUg=="))
    posts: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "/models/MiniMax-H3/FL2VA"}]})
        if method == "POST" and url.endswith("/videos"):
            posts.append(kwargs)
            return _Resp(True, {"id": "video-2", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-2"):
            return _Resp(True, {"id": "video-2", "status": "completed"})
        if method == "GET" and url.endswith("/videos/video-2/content"):
            return _Resp(True, {}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    result = vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
        "animate this",
        api_key="k",
        api_base="http://127.0.0.1:8091/v1",
        model="",
        size=None,
        duration=5,
        resolution=None,
        first_frame=str(ref),
    )

    assert "video_path" in result
    fields = posts[0]["data"]
    extra = json.loads(fields["extra_params"])
    assert extra["task"] == "ref2va"
    # No size -> adaptive canvas on the fixed 768 short edge.
    assert fields["aspect_ratio"] == "adaptive"
    assert fields["short_edge"] == "768"
    files = posts[0]["files"]
    assert len(files) == 1
    assert files[0][0] == "input_reference"
    assert files[0][1][0] == "ref.png"


def test_invoke_video_unregistered_model_sends_no_extra_params(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "Wan-AI/Wan2.2-T2V-A14B-Diffusers"}]})
        if method == "POST" and url.endswith("/videos"):
            posts.append(kwargs)
            return _Resp(True, {"id": "video-3", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-3"):
            return _Resp(True, {"id": "video-3", "status": "completed"})
        if method == "GET" and url.endswith("/videos/video-3/content"):
            return _Resp(True, {}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    result = vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
        "a boat",
        api_key="",
        api_base="http://127.0.0.1:8091/v1",
        model="",
        size="1280*720",
        duration=6,
        resolution=None,
    )

    assert "video_path" in result
    fields = posts[0]["data"]
    assert fields["model"] == "Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    assert fields["fps"] == "24"
    assert fields["size"] == "1280x720"
    assert fields["seconds"] == "6"
    assert "extra_params" not in fields
    assert "num_inference_steps" not in fields
    assert "flow_shift" not in fields


def test_invoke_video_models_probe_failure_falls_back_to_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            raise ConnectionError("boom")
        if method == "POST" and url.endswith("/videos"):
            posts.append(kwargs)
            return _Resp(True, {"id": "video-4", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-4"):
            return _Resp(True, {"id": "video-4", "status": "completed"})
        if method == "GET" and url.endswith("/videos/video-4/content"):
            return _Resp(True, {}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    result = vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
        "a boat",
        api_key="",
        api_base="http://127.0.0.1:8091/v1",
        model="My-Configured-Model",
        size=None,
        duration=5,
        resolution=None,
    )

    assert "video_path" in result
    fields = posts[0]["data"]
    assert fields["model"] == "My-Configured-Model"
    # Without a served model id the registry cannot match: no extra_params.
    assert "extra_params" not in fields


def test_invoke_video_failed_job_surfaces_error_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "MiniMaxAI/MiniMax-H3"}]})
        if method == "POST" and url.endswith("/videos"):
            return _Resp(True, {"id": "video-5", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-5"):
            return _Resp(
                True,
                {"id": "video-5", "status": "failed", "error": {"code": 400, "message": "boom"}},
            )
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    with pytest.raises(ValueError, match="boom"):
        vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
            "a boat",
            api_key="",
            api_base="http://127.0.0.1:8091/v1",
            model="",
            size=None,
            duration=5,
            resolution=None,
        )


def test_invoke_image_uses_configured_model_without_models_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[tuple[str, str]] = []
    png_b64 = base64.b64encode(b"png-bytes").decode("ascii")

    def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        if method == "POST" and url.endswith("/images/generations"):
            return _Resp(True, {"data": [{"b64_json": png_b64}]})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    result = vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
        "a dragon",
        api_key="",
        api_base="http://127.0.0.1:8000/v1",
        model="Qwen-Image",
        size="1024x1024",
    )

    assert result["image_path"].endswith(".png")
    # A configured model name is used directly; no GET /models probe is sent.
    assert calls == [("POST", "http://127.0.0.1:8000/v1/images/generations")]


def test_invoke_image_discovers_model_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []
    png_b64 = base64.b64encode(b"png-bytes").decode("ascii")

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "Tongyi-MAI/Z-Image-Turbo"}]})
        if method == "POST" and url.endswith("/images/generations"):
            posts.append(kwargs)
            return _Resp(True, {"data": [{"b64_json": png_b64}]})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    result = vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
        "a dragon",
        api_key="",
        api_base="http://127.0.0.1:8000/v1",
        model="",
        size="1024*1024",
    )

    assert result["image_path"].endswith(".png")
    payload = posts[0]["json"]
    assert payload["model"] == "Tongyi-MAI/Z-Image-Turbo"
    assert payload["size"] == "1024x1024"
    assert "seed" not in payload


def test_invoke_image_with_references_uses_edits_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    ref_a = tmp_path / "a.png"
    ref_b = tmp_path / "b.png"
    ref_a.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUg=="))
    ref_b.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUg=="))
    posts: list[dict] = []
    png_b64 = base64.b64encode(b"edited-png").decode("ascii")

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "Qwen/Qwen-Image-Edit-2511"}]})
        if method == "POST" and url.endswith("/images/edits"):
            posts.append(kwargs)
            return _Resp(True, {"data": [{"b64_json": png_b64}]})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    result = vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
        "put the bear on a beach",
        api_key="",
        api_base="http://127.0.0.1:8000/v1",
        model="",
        size=None,
        reference_images=[str(ref_a), str(ref_b), str(ref_a)],  # dupe is deduped
    )

    assert result["image_path"].endswith(".png")
    fields = posts[0]["data"]
    assert fields["model"] == "Qwen/Qwen-Image-Edit-2511"
    # No explicit size: the field is omitted so the server infers it ("auto").
    assert "size" not in fields
    assert "seed" not in fields
    files = posts[0]["files"]
    assert [item[0] for item in files] == ["image", "image"]
    assert [item[1][0] for item in files] == ["a.png", "b.png"]


def test_invoke_image_edits_forwards_explicit_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    ref = tmp_path / "in.png"
    ref.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUg=="))
    posts: list[dict] = []
    png_b64 = base64.b64encode(b"edited-png").decode("ascii")

    def fake_request(method: str, url: str, **kwargs):
        if method == "POST" and url.endswith("/images/edits"):
            posts.append(kwargs)
            return _Resp(True, {"data": [{"b64_json": png_b64}]})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    result = vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
        "make it pop",
        api_key="",
        api_base="http://127.0.0.1:8000/v1",
        model="Qwen-Image-Edit",
        size="1024*1024",
        reference_images=[str(ref)],
    )

    assert result["image_path"].endswith(".png")
    assert posts[0]["data"]["size"] == "1024x1024"


def test_invoke_image_with_unreadable_references_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def fake_request(method: str, url: str, **kwargs):
        raise AssertionError(f"no HTTP expected, got {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    with pytest.raises(ValueError, match="none could be read"):
        vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
            "edit this",
            api_key="",
            api_base="http://127.0.0.1:8000/v1",
            model="Qwen-Image-Edit",
            size=None,
            reference_images=["/nonexistent/missing.png"],
        )


def test_invoke_video_comfyui_extras_become_form_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUg=="))
    clip = tmp_path / "motion.mp4"
    clip.write_bytes(b"mp4")
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"mp3")
    posts: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        if method == "GET" and url.endswith("/models"):
            return _Resp(True, {"data": [{"id": "Wan-AI/Wan2.2-T2V-A14B-Diffusers"}]})
        if method == "POST" and url.endswith("/videos"):
            posts.append(kwargs)
            return _Resp(True, {"id": "video-6", "status": "queued"})
        if method == "GET" and url.endswith("/videos/video-6"):
            return _Resp(True, {"id": "video-6", "status": "completed"})
        if method == "GET" and url.endswith("/videos/video-6/content"):
            return _Resp(True, {}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(vllm_omni_gen.time, "sleep", lambda *_: None)

    result = vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
        "a boat",
        api_key="",
        api_base="http://127.0.0.1:8091/v1",
        model="",
        size="832x480",
        duration=2.5,
        resolution=None,
        reference_images=[str(ref)],
        reference_videos=[str(clip)],
        reference_audios=[str(voice)],
        fps=16,
        negative_prompt="blurry",
        extra_params={"audio_flow_shift": 2.0},
        num_inference_steps=30,
        vae_use_tiling=True,
        boundary_ratio=0.875,
    )

    assert "video_path" in result
    fields = posts[0]["data"]
    assert fields["width"] == "832"
    assert fields["height"] == "480"
    assert fields["fps"] == "16"
    assert fields["num_frames"] == "40"
    assert "size" not in fields and "seconds" not in fields
    assert fields["negative_prompt"] == "blurry"
    assert fields["num_inference_steps"] == "30"
    assert fields["vae_use_tiling"] == "true"
    assert fields["boundary_ratio"] == "0.875"
    assert json.loads(fields["extra_params"]) == {"audio_flow_shift": 2.0}
    names = [item[1][0] for item in posts[0]["files"]]
    assert names == ["ref.png", "motion.mp4", "voice.mp3"]


def test_invoke_video_rejects_reserved_extra_fields(tmp_path) -> None:
    with pytest.raises(ValueError, match="prompt"):
        vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
            "a boat",
            api_key="",
            api_base="http://127.0.0.1:8091/v1",
            model="",
            size=None,
            duration=5,
            resolution=None,
            prompt="shadowed",
        )


def test_invoke_video_audio_only_references_raise(tmp_path) -> None:
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"mp3")
    with pytest.raises(ValueError, match="audio-only"):
        vllm_omni_gen.invoke_vllm_omni_video_generation_sync(
            "a boat",
            api_key="",
            api_base="http://127.0.0.1:8091/v1",
            model="m",
            size=None,
            duration=5,
            resolution=None,
            reference_audios=[str(voice)],
        )


def test_invoke_image_comfyui_extras_join_the_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []
    png_b64 = base64.b64encode(b"png-bytes").decode("ascii")

    def fake_request(method: str, url: str, **kwargs):
        if method == "POST" and url.endswith("/images/generations"):
            posts.append(kwargs)
            return _Resp(True, {"data": [{"b64_json": png_b64}]})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(vllm_omni_gen, "_http_request", fake_request)
    monkeypatch.setattr(vllm_omni_gen, "get_agent_workspace_dir", lambda: tmp_path)

    vllm_omni_gen.invoke_vllm_omni_image_generation_sync(
        "a dragon",
        api_key="",
        api_base="http://127.0.0.1:8000/v1",
        model="Qwen-Image",
        size="768x512",
        negative_prompt="text",
        seed=7,
        guidance_scale=4.0,
        vae_use_slicing=False,
    )

    payload = posts[0]["json"]
    assert payload["size"] == "768x512"
    assert payload["negative_prompt"] == "text"
    assert payload["seed"] == 7
    assert payload["guidance_scale"] == 4.0
    assert payload["vae_use_slicing"] is False
