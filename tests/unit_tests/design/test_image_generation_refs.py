# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.server.runtime.designer import media_generation


def _png_data_uri(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[gen_toolkits.ImageOptions]:
    monkeypatch.setenv("VISUAL_GEN_ENABLED", "true")
    monkeypatch.setenv("VISUAL_GEN_API_KEY", "sk-test")
    monkeypatch.setenv("VISUAL_GEN_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
    monkeypatch.setenv("VISUAL_GEN_MODEL_NAME", "qwen-image-3.0")
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "dashscope")
    saved = tmp_path / "out.png"
    seen: list[gen_toolkits.ImageOptions] = []

    async def fake_generate_image(target, prompt, aspect_ratio, save_dir, options=None):
        assert target.backend == gen_toolkits.DASHSCOPE
        seen.append(options)
        return f"Image generated successfully!\nSaved to: {saved}"

    monkeypatch.setattr(gen_toolkits, "generate_image", fake_generate_image)
    return seen


@pytest.mark.asyncio
async def test_references_keep_their_order(tmp_path: Path, captured) -> None:
    character = tmp_path / "character.png"
    scene = tmp_path / "scene.png"
    character.write_bytes(b"png-character")
    scene.write_bytes(b"png-scene")
    result = await media_generation.generate_image(
        "分镜内容：镜号 2；人物变化 主体入画。",
        reference_images=[str(character), str(scene)],
    )
    assert "image_path" in result
    [options] = captured
    assert options.reference_image_uris == (_png_data_uri(b"png-character"), _png_data_uri(b"png-scene"))


@pytest.mark.asyncio
async def test_file_uri_and_https_references(tmp_path: Path, captured) -> None:
    image = tmp_path / "ref.png"
    image.write_bytes(b"png-ref")
    await media_generation.generate_image(
        "关键帧",
        reference_images=[image.resolve().as_uri(), "https://example.com/character.png"],
    )
    [options] = captured
    assert options.reference_image_uris == (_png_data_uri(b"png-ref"), "https://example.com/character.png")


@pytest.mark.asyncio
async def test_text_only_without_refs(captured) -> None:
    await media_generation.generate_image("关键帧")
    [options] = captured
    assert options.reference_image_uris == ()


@pytest.mark.asyncio
async def test_unreadable_references_are_an_error(tmp_path: Path, captured) -> None:
    result = await media_generation.generate_image(
        "关键帧", reference_images=[str(tmp_path / "missing.png")]
    )
    assert "error" in result
    assert captured == []
