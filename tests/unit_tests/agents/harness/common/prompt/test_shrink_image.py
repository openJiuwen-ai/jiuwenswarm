# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the unified image shrink helper ``shrink_image``."""

from __future__ import annotations

import base64
import io

from PIL import Image

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import shrink_image


def _tiny_png() -> bytes:
    image = Image.new("RGB", (128, 128), (210, 70, 60))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _large_png() -> bytes:
    image = Image.effect_noise((1600, 1200), 60)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_shrink_small_image_returns_original(tmp_path):
    p = tmp_path / "small.png"
    p.write_bytes(_tiny_png())
    uri = shrink_image(str(p), max_bytes=500_000, max_dimension=1536)
    assert uri.startswith("data:image/png;base64,")


def test_shrink_small_image_preserves_original_bytes(tmp_path):
    p = tmp_path / "small.png"
    original = _tiny_png()
    p.write_bytes(original)
    uri = shrink_image(str(p), max_bytes=500_000, max_dimension=1536)
    encoded = uri.split(",", 1)[1]
    assert base64.b64decode(encoded) == original


def test_shrink_large_image_bounds_bytes(tmp_path):
    p = tmp_path / "big.png"
    original = _large_png()
    assert len(original) > 100_000  # precondition: the fixture really is "large"
    p.write_bytes(original)
    uri = shrink_image(str(p), max_bytes=100_000, max_dimension=1536)
    encoded = uri.split(",", 1)[1]
    data = base64.b64decode(encoded)
    assert len(data) <= 100_000
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1536


def test_shrink_missing_file_returns_empty(tmp_path):
    assert shrink_image(str(tmp_path / "nope.png"), max_bytes=1, max_dimension=1) == ""


def test_shrink_invalid_image_returns_empty(tmp_path):
    p = tmp_path / "broken.png"
    p.write_bytes(b"not a real png file")
    assert shrink_image(str(p), max_bytes=500_000, max_dimension=1536) == ""


def test_shrink_does_not_modify_original_file(tmp_path):
    p = tmp_path / "big.png"
    original = _large_png()
    p.write_bytes(original)
    shrink_image(str(p), max_bytes=100_000, max_dimension=1536)
    assert p.read_bytes() == original