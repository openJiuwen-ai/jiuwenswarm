# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Design image/video generation on an OpenRouter-style slot.

The chat tools in ``visual_gen_tools`` / ``video_gen_tools`` are openjiuwen
``@tool`` objects: the module attribute is a ``LocalFunction`` wrapper, not the
coroutine, so Design reaches through ``_func`` for the plain callable. These
tests patch ``_func`` and therefore fail if the wiring goes back to calling the
module attribute directly (which raises
``TypeError: 'LocalFunction' object is not callable``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools import video_gen_tools, visual_gen_tools
from jiuwenswarm.server.runtime.designer import media_generation as mg


@pytest.fixture
def openrouter_image_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The slot shape a 图片处理 panel pointing at OpenRouter produces."""
    monkeypatch.setenv("VISUAL_GEN_ENABLED", "true")
    monkeypatch.setenv("VISUAL_GEN_API_KEY", "sk-or-test")
    monkeypatch.setenv("VISUAL_GEN_API_BASE", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VISUAL_GEN_MODEL_NAME", "google/gemini-3.1-flash-image")
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "google")


@pytest.fixture
def openrouter_video_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Note the vendor-name 协议 (``bytedance``) with an OpenRouter base URL."""
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "true")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "sk-or-test")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "bytedance/seedance-2.0-fast")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "bytedance")


def test_generation_tools_are_framework_wrapped() -> None:
    """The reason ``_func`` is needed at all: these are Tool objects, not functions."""
    for tool in (
        visual_gen_tools.generate_visual,
        video_gen_tools.generate_video,
        video_gen_tools.check_video_status,
    ):
        assert not callable(tool)
        assert callable(tool._func)  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_image_calls_the_openrouter_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_image_slot: None
) -> None:
    saved = tmp_path / "out.png"
    seen: list[tuple[Any, ...]] = []

    async def fake_generate_visual(prompt, aspect_ratio="16:9", resolution="512", save_dir=None):
        seen.append((prompt, aspect_ratio, resolution, save_dir))
        return f"Image generated successfully!\nSaved to: {saved}"

    monkeypatch.setattr(visual_gen_tools.generate_visual, "_func", fake_generate_visual)
    result = await mg.generate_image("关键帧", size="1024x1024", save_dir=str(tmp_path))

    assert result == {"image_path": str(saved)}
    [(prompt, _aspect_ratio, resolution, save_dir)] = seen
    assert prompt == "关键帧"
    assert resolution == "1024"  # short edge of 1024x1024
    assert save_dir == str(tmp_path)


@pytest.mark.asyncio
async def test_image_drops_references_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_image_slot: None
) -> None:
    """That path is text-to-image, so refs degrade to a warning, not an error."""
    character = tmp_path / "character.png"
    character.write_bytes(b"png-character")
    saved = tmp_path / "out.png"

    async def fake_generate_visual(prompt, aspect_ratio="16:9", resolution="512", save_dir=None):
        return f"Image generated successfully!\nSaved to: {saved}"

    monkeypatch.setattr(visual_gen_tools.generate_visual, "_func", fake_generate_visual)
    result = await mg.generate_image("关键帧", reference_images=[str(character)])

    assert result == {"image_path": str(saved)}


@pytest.mark.asyncio
async def test_video_submits_then_polls_the_openrouter_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_video_slot: None
) -> None:
    job = "gen-vid-1791447875-kcvDiEOcJLDv7t6LWyap"
    saved = tmp_path / "clip.mp4"
    submitted: list[tuple[Any, ...]] = []
    polled: list[str] = []

    async def fake_generate_video(*args: Any, **kwargs: Any) -> str:
        submitted.append(args)
        return (
            f"Video job {job} submitted and still pending after 120s - generation can take "
            f"several minutes. Call check_video_status with job_id={job} to check progress "
            "and download it once ready."
        )

    async def fake_check_video_status(job_id: str, save_dir: str | None = None) -> str:
        polled.append(job_id)
        return (
            f"Video generated successfully!\nSaved to: {saved}\n(job {job_id} - the remote "
            "source URL expires 24h after completion, so this local file is now the durable copy.)"
        )

    monkeypatch.setattr(video_gen_tools.generate_video, "_func", fake_generate_video)
    monkeypatch.setattr(video_gen_tools.check_video_status, "_func", fake_check_video_status)
    monkeypatch.setattr(mg, "_VIDEO_POLL_SECONDS", 0)

    result = await mg.generate_video(mg.DesignerVideoRequest(prompt="shot", duration=5))

    assert result == {"video_path": str(saved)}
    assert polled == [job]  # the submitted job id drives the polling loop
    assert submitted and submitted[0][0] == "shot"


def test_openrouter_host_passes_the_gate(
    monkeypatch: pytest.MonkeyPatch, openrouter_video_slot: None, openrouter_image_slot: None
) -> None:
    assert mg.generation_problem("video") is None
    assert mg.generation_problem("image") is None


def test_unknown_endpoint_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, openrouter_video_slot: None
) -> None:
    """The guard the native-only check provided is kept, just widened."""
    monkeypatch.delenv("VIDEO_GEN_ENDPOINT_PROFILE", raising=False)
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://api.example.com/v1")

    problem = mg.generation_problem("video")

    assert problem is not None
    assert "OpenRouter endpoint" in problem  # the message names the way out
    assert "https://api.example.com/v1" in problem


def test_unknown_image_endpoint_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, openrouter_image_slot: None
) -> None:
    monkeypatch.delenv("VISUAL_GEN_ENDPOINT_PROFILE", raising=False)
    monkeypatch.setenv("VISUAL_GEN_API_BASE", "https://api.example.com/v1")

    assert "OpenRouter endpoint" in (mg.generation_problem("image") or "")


def test_endpoint_profile_admits_a_proxied_openrouter(
    monkeypatch: pytest.MonkeyPatch, openrouter_video_slot: None
) -> None:
    """A non-openrouter.ai host is fine when the slot declares the profile."""
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://gateway.internal/v1")
    monkeypatch.setenv("VIDEO_GEN_ENDPOINT_PROFILE", "openrouter")

    assert mg.generation_problem("video") is None


# --- reference-to-video on the OpenRouter path -------------------------------
#
# These requests used to be refused outright ("only supports text-to-video and
# first-frame image-to-video, not reference images"), which blocked Design clips
# on a model that does support them and told the user to switch providers.


def _video_body(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": "bytedance/seedance-2.0-fast",
        "prompt": "shot",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "duration_seconds": 5,
        "generate_audio": False,
        "first_frame": None,
        "reference_uris": [],
        "reference_mode": False,
    }
    kwargs.update(overrides)
    return video_gen_tools._openrouter_video_body(**kwargs)  # pylint: disable=protected-access


def test_reference_images_ride_input_references() -> None:
    body = _video_body(reference_uris=["https://cdn/a.png", "https://cdn/b.png"])

    assert body["input_references"] == [
        {"type": "image_url", "image_url": {"url": "https://cdn/a.png"}},
        {"type": "image_url", "image_url": {"url": "https://cdn/b.png"}},
    ]
    assert "frame_images" not in body


def test_a_lone_first_frame_still_uses_frame_images() -> None:
    body = _video_body(first_frame="data:image/png;base64,AAA")

    assert body["frame_images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA"},
            "frame_type": "first_frame",
        }
    ]
    assert "input_references" not in body


def test_reference_mode_folds_the_first_frame_into_references() -> None:
    """Matches every native backend: reference mode uses it as a reference."""
    body = _video_body(first_frame="data:image/png;base64,AAA", reference_mode=True)

    assert body["input_references"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    ]
    assert "frame_images" not in body


def test_reference_mode_puts_the_frame_ahead_of_the_other_references() -> None:
    frame = "data:image/png;base64,FRAME"
    body = _video_body(
        first_frame=frame, reference_uris=["https://cdn/character.png"], reference_mode=True
    )

    assert [item["image_url"]["url"] for item in body["input_references"]] == [
        frame,
        "https://cdn/character.png",
    ]


def test_a_frame_already_listed_as_a_reference_is_not_sent_twice() -> None:
    frame = "data:image/png;base64,AAA"
    body = _video_body(first_frame=frame, reference_uris=[frame])

    assert "frame_images" not in body
    assert body["input_references"] == [{"type": "image_url", "image_url": {"url": frame}}]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    @property
    def text(self) -> str:
        return str(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Captures the POST body; the job is already terminal so no poll happens."""

    instances: list["_FakeClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, headers: Any = None, json: Any = None) -> _FakeResponse:
        self.posts.append((url, json))
        return _FakeResponse({"id": "job-1", "status": "failed", "error": "stopped for the test"})

    async def get(self, url: str, headers: Any = None) -> _FakeResponse:
        return _FakeResponse({"id": "job-1", "status": "failed"})


@pytest.mark.asyncio
async def test_generate_video_sends_references_instead_of_refusing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_video_slot: None
) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(video_gen_tools.httpx, "AsyncClient", _FakeClient)
    character = tmp_path / "character.png"
    character.write_bytes(b"png-character")
    scene = tmp_path / "scene.png"
    scene.write_bytes(b"png-scene")

    result = await video_gen_tools.generate_video._func(  # pylint: disable=protected-access
        "shot", "16:9", "720p", 5, None, False, str(tmp_path),
        [str(character), str(scene)], False,
    )

    assert "not reference images" not in result
    [(url, body)] = _FakeClient.instances[-1].posts
    assert url.endswith("/videos")
    references = body["input_references"]
    assert len(references) == 2
    assert all(item["type"] == "image_url" for item in references)
    assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in references)
    assert "frame_images" not in body


class _RejectingResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return {}


class _RejectingClient:
    """A provider that refuses the submit outright."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_RejectingClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, url: str, headers: Any = None, json: Any = None) -> _RejectingResponse:
        return _RejectingResponse(400, "reference images are not supported by this model")

    async def get(self, url: str, headers: Any = None) -> _RejectingResponse:
        return _RejectingResponse(400, "")


@pytest.mark.asyncio
async def test_a_provider_rejection_still_explains_the_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_video_slot: None
) -> None:
    """The old block became guidance: only a real rejection triggers it now."""
    monkeypatch.setattr(video_gen_tools.httpx, "AsyncClient", _RejectingClient)
    character = tmp_path / "character.png"
    character.write_bytes(b"png-character")

    result = await video_gen_tools.generate_video._func(  # pylint: disable=protected-access
        "shot", "16:9", "720p", 5, None, False, str(tmp_path), [str(character)], False,
    )

    assert "reference images are not supported by this model" in result  # provider detail kept
    assert "1 reference image(s) were sent as input_references" in result
    assert "OpenRouter (bytedance/seedance-2.x)" in result  # OpenRouter named as supported
    assert "first_frame_path" in result


@pytest.mark.asyncio
async def test_a_provider_rejection_without_references_adds_no_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, openrouter_video_slot: None
) -> None:
    monkeypatch.setattr(video_gen_tools.httpx, "AsyncClient", _RejectingClient)

    result = await video_gen_tools.generate_video._func(  # pylint: disable=protected-access
        "shot", "16:9", "720p", 5, None, False, str(tmp_path), None, False,
    )

    assert "reference images are not supported by this model" in result
    assert "input_references" not in result

