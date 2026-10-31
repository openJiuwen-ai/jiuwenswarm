# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image reference line construction and parsing."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    build_image_reference_line,
    parse_image_references,
)


def test_build_reference_line():
    line = build_image_reference_line(
        {"path": "/a/home.png", "filename": "home.png", "mime_type": "image/png", "size_bytes": 614400},
        1,
    )
    assert line == "[image #1] path=/a/home.png filename=home.png mime=image/png size=614400"


def test_parse_reference_lines():
    text = "对比一下\n[image #1] path=/a/home.png filename=home.png mime=image/png size=614400\n[image #2] path=/a/d.png filename=d.png mime=image/png size=409600"
    refs = parse_image_references(text)
    assert [r["index"] for r in refs] == [1, 2]
    assert refs[0]["path"] == "/a/home.png"


def test_parse_ignores_non_reference_text():
    assert parse_image_references("普通文本 /data/image/x.png") == []


def test_build_reference_line_omits_missing_size():
    line = build_image_reference_line(
        {"path": "/a/home.png", "filename": "home.png", "mime_type": "image/png"},
        2,
    )
    assert line == "[image #2] path=/a/home.png filename=home.png mime=image/png"


def test_parse_reference_line_without_size():
    refs = parse_image_references(
        "[image #1] path=/a/home.png filename=home.png mime=image/png"
    )
    assert refs[0]["index"] == 1
    assert refs[0]["path"] == "/a/home.png"
    assert refs[0]["filename"] == "home.png"
    assert refs[0]["mime_type"] == "image/png"
    assert "size_bytes" not in refs[0]