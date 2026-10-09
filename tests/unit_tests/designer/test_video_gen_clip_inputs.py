# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools import gen_toolkits as gt
from jiuwenswarm.server.runtime.designer import media_generation as mg


def _png(path: Path, payload: bytes = b"png") -> str:
    path.write_bytes(payload)
    return str(path)


def _wan_body(model: str, *, file_url: str | None = None, **fields: Any) -> dict[str, Any]:
    """The Wan request body for designer inputs (local paths are data URIs by then)."""
    first_frame = fields.pop("first_frame", None)
    refs = fields.pop("reference_images", ())
    request = gt.VideoRequest(
        prompt="shot",
        aspect_ratio=fields.pop("aspect_ratio", "16:9"),
        resolution=fields.pop("resolution", ""),
        duration_seconds=fields.pop("duration", 5),
        first_frame_data_uri=mg.image_uri(first_frame),
        reference_image_uris=tuple(mg.image_uri(item) for item in refs),
        **fields,
    )
    body = gt._dashscope_video_body(model, request, file_url)
    return {"model": body["model"], **body["input"], **body["parameters"]}


def test_local_keyframe_becomes_data_uri(tmp_path: Path) -> None:
    url = mg.image_uri(_png(tmp_path / "shot1.png"))
    assert url is not None and url.startswith("data:image/png;base64,")
    assert mg.image_uri(str(tmp_path / "missing.png")) is None
    assert mg.image_uri("https://cdn.example/a.png") == "https://cdn.example/a.png"


def test_wan3_references_include_the_first_frame(tmp_path: Path) -> None:
    frame = _png(tmp_path / "shot1.png")
    extra = _png(tmp_path / "character.png", b"png-extra")
    params = _wan_body("wan3.0-video", first_frame=frame, reference_images=[frame, extra])
    assert params["model"] == "wan3.0-video"
    assert "img_url" not in params and "shot_type" not in params and "resolution" not in params
    assert params["size"] == "1280*720"
    assert [item["type"] for item in params["media"]] == ["reference_image", "reference_image"]


def test_wan3_lone_first_frame_is_image_to_video(tmp_path: Path) -> None:
    params = _wan_body("wan3.0-video", first_frame=_png(tmp_path / "shot1.png"))
    assert str(params["img_url"]).startswith("data:image/png;base64,")
    assert params["resolution"] == "720P" and params["audio"] is False
    assert "shot_type" not in params and "size" not in params and "ratio" not in params


def test_wan3_img_url_uses_requested_resolution(tmp_path: Path) -> None:
    params = _wan_body(
        "wan3.0-video", size="480*854", resolution="1080p", first_frame=_png(tmp_path / "shot1.png")
    )
    assert "size" not in params and "ratio" not in params
    assert params["resolution"] == "1080P"


def test_wan3_uses_media_for_character_scene_and_storyboard(tmp_path: Path) -> None:
    refs = [_png(tmp_path / f"{name}.png", name.encode()) for name in ("keyframe", "character", "scene")]
    params = _wan_body("wan3.0-video", reference_images=refs, file_url="oss://bucket/storyboard.md")
    assert "img_url" not in params and "reference_urls" not in params and "shot_type" not in params
    assert params["audio"] is False
    media = params["media"]
    assert [item["type"] for item in media] == ["reference_image"] * 3 + ["file"]
    assert all(item["url"].startswith("data:image/png;base64,") for item in media[:-1])
    assert media[-1]["url"] == "oss://bucket/storyboard.md"


def test_wan3_storyboard_with_keyframe_reference_is_not_img_url(tmp_path: Path) -> None:
    frame = _png(tmp_path / "keyframe.png")
    params = _wan_body(
        "wan3.0-video", first_frame=frame, reference_images=[frame], file_url="oss://bucket/storyboard.md"
    )
    assert "img_url" not in params
    assert [item["type"] for item in params["media"]] == ["reference_image", "file"]


def test_wan3_compose_score_can_enable_audio() -> None:
    params = _wan_body("wan3.0-video", generate_audio=True)
    assert params["audio"] is True and "img_url" not in params


def test_non_wan3_model_keeps_img_url_for_a_lone_first_frame(tmp_path: Path) -> None:
    params = _wan_body("custom-video-model", first_frame=_png(tmp_path / "shot1.png"))
    assert "img_url" in params and "reference_urls" not in params and "media" not in params


def test_text_only_default_size() -> None:
    params = _wan_body("wan3.0-video")
    assert params["size"] == "1280*720" and "img_url" not in params


def test_reference_mode_480p_uses_size_not_resolution(tmp_path: Path) -> None:
    params = _wan_body(
        "wan3.0-video",
        size="854*480",
        resolution="480p",
        reference_images=[_png(tmp_path / "character.png")],
        reference_mode=True,
    )
    assert params["size"] == "832*480" and "resolution" not in params and "media" in params


def test_text_only_480p_uses_size_not_resolution() -> None:
    params = _wan_body("wan3.0-video", size="854*480", resolution="480p")
    assert params["size"] == "832*480" and "resolution" not in params and "img_url" not in params


# --------------------------------------------------------------------------- #
# media_generation: Settings slot, reference loading and waiting for the clip
# --------------------------------------------------------------------------- #

@pytest.fixture()
def dashscope_video_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "true")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "sk-test")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://dashscope-intl.aliyuncs.com/api/v1")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan3.0-video")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "dashscope")


def test_generation_problem_explains_each_gap(monkeypatch: pytest.MonkeyPatch, dashscope_video_slot: None) -> None:
    assert mg.generation_problem("video") is None
    # An OpenRouter-style endpoint is a supported Design backend now, so the
    # only remaining gaps are configuration ones.
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "openrouter")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://openrouter.ai/api/v1")
    assert mg.generation_problem("video") is None
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "")
    assert "not configured" in (mg.generation_problem("video") or "")
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "false")
    assert "switched off" in (mg.generation_problem("video") or "")


def test_vllm_omni_slot_needs_only_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "true")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "http://127.0.0.1:8091/v1")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "vllm-omni")
    assert mg.generation_problem("video") is None


def test_generate_video_waits_for_the_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dashscope_video_slot: None
) -> None:
    submitted: list[tuple[gt.GenerationTarget, gt.VideoRequest]] = []
    checks = iter(["Video job ds-1 is still running.", f"Video generated successfully!\nSaved to: {tmp_path}/v.mp4"])

    async def fake_submit(target: gt.GenerationTarget, request: gt.VideoRequest, save_dir: str | None) -> str:
        submitted.append((target, request))
        return "Video job ds-1 submitted and still running after 300s - generation can take several minutes."

    async def fake_check(target: gt.GenerationTarget, task_id: str, save_dir: str | None) -> str:
        assert task_id == "ds-1" and save_dir == str(tmp_path)
        return next(checks)

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mg.gen_toolkits, "submit_video", fake_submit)
    monkeypatch.setattr(mg.gen_toolkits, "check_video", fake_check)
    monkeypatch.setattr(mg.asyncio, "sleep", instant)
    character = _png(tmp_path / "character.png")
    request = mg.DesignerVideoRequest(
        prompt="shot 1",
        duration=5,
        size="720*1280",
        resolution="720P",
        reference_images=(character,),
        reference_file=str(tmp_path / "storyboard.md"),
        audio=True,
        reference_mode=True,
        model="wan3.0-video-prime",
    )
    result = asyncio.run(mg.generate_video(request, save_dir=str(tmp_path)))
    assert result == {"video_path": f"{tmp_path}/v.mp4"}
    target, video_request = submitted[0]
    assert target.backend == "dashscope" and target.model == "wan3.0-video-prime"
    assert video_request.aspect_ratio == "9:16" and video_request.resolution == "720p"
    assert video_request.reference_mode and video_request.generate_audio
    assert video_request.reference_image_uris[0].startswith("data:image/png;base64,")
    assert video_request.reference_file_path == str(tmp_path / "storyboard.md")


def test_generate_video_reports_backend_errors_and_bad_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dashscope_video_slot: None
) -> None:
    async def failing_submit(*_args: Any) -> str:
        return "[ERROR]: DashScope video generation submit failed: InvalidParameter: bad size"

    monkeypatch.setattr(mg.gen_toolkits, "submit_video", failing_submit)
    result = asyncio.run(mg.generate_video(mg.DesignerVideoRequest(prompt="shot")))
    assert result == {"error": "[ERROR]: DashScope video generation submit failed: InvalidParameter: bad size"}

    missing = mg.DesignerVideoRequest(prompt="shot", reference_images=(str(tmp_path / "gone.png"),))
    assert "none could be read" in asyncio.run(mg.generate_video(missing))["error"]
