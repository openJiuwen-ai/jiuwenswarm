# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""图片引用行构造/解析 + ``extract_multimodal_image_files`` 的 files 形态归一。"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.multimodal.image_reference import (
    build_image_reference_line,
    extract_multimodal_image_files,
    parse_image_references,
)


def _image(path: str = "a.png", **overrides):
    item = {"type": "image", "filename": "a.png", "path": path, "mime_type": "image/png"}
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# 引用行 build → parse 往返（S_22 数据结构契约）
# ---------------------------------------------------------------------------

# 引用行是 S_22「数据结构」里的模型可见契约；size 段可缺（旧历史 / 元数据缺失），
# 用 build→parse 往返覆盖带 size / 不带 size 两档，格式只在这张表里钉一次。
# 第三档钉住「上传文件名含空格」：Windows 截图名、含空格的下载目录都很常见。
_ROUND_TRIP = [
    (
        {"path": "/a/home.png", "filename": "home.png", "mime_type": "image/png", "size_bytes": 614400},
        1,
        "[image #1] path=/a/home.png filename=home.png mime=image/png size=614400",
    ),
    (
        {"path": "/a/home.png", "filename": "home.png", "mime_type": "image/png"},
        2,
        "[image #2] path=/a/home.png filename=home.png mime=image/png",
    ),
    (
        {
            "path": "/a/team docs/我的 图片.png",
            "filename": "screen shot 2026.png",
            "mime_type": "image/png",
            "size_bytes": 2048,
        },
        3,
        "[image #3] path=/a/team docs/我的 图片.png filename=screen shot 2026.png "
        "mime=image/png size=2048",
    ),
]


@pytest.mark.parametrize(
    ("image_file", "index", "expected_line"),
    _ROUND_TRIP,
    ids=["with-size", "without-size", "paths-with-spaces"],
)
def test_reference_line_build_parse_round_trip(image_file, index, expected_line):
    line = build_image_reference_line(image_file, index)
    assert line == expected_line
    assert parse_image_references(line) == [{"index": index, **image_file}]


def test_parse_reference_lines_from_multiline_text():
    text = (
        "对比一下\n"
        "[image #1] path=/a/home.png filename=home.png mime=image/png size=614400\n"
        "[image #2] path=/a/d.png filename=d.png mime=image/png size=409600"
    )
    refs = parse_image_references(text)
    assert [r["index"] for r in refs] == [1, 2]
    assert refs[0]["path"] == "/a/home.png"


def test_parse_reference_line_with_spaces_in_path_and_filename():
    """含空格的路径 / 文件名不得整行解析失败（图片静默丢失）。"""

    text = (
        "[image #1] path=/data/upload/team docs/屏幕 截图.png "
        "filename=屏幕 截图.png mime=image/png size=4096"
    )
    refs = parse_image_references(text)
    assert refs == [
        {
            "index": 1,
            "path": "/data/upload/team docs/屏幕 截图.png",
            "filename": "屏幕 截图.png",
            "mime_type": "image/png",
            "size_bytes": 4096,
        }
    ]


def test_parse_ignores_non_reference_text():
    # 正文里的普通路径不得被当成图片引用（过度解析会把用户提问切成假引用）。
    assert parse_image_references("普通文本 /data/image/x.png") == []


# ---------------------------------------------------------------------------
# extract_multimodal_image_files 的 files 形态归一
# ---------------------------------------------------------------------------


def test_extract_from_files_list() -> None:
    """relay 通道的 files 是 list 形态，必须被识别（否则原生多模态在 relay 主链路失效）。"""

    files = [_image("/tmp/a.png"), _image("/tmp/b.png")]
    result = extract_multimodal_image_files({"files": files})
    assert [r["path"] for r in result] == ["/tmp/a.png", "/tmp/b.png"]


def test_extract_from_files_dict() -> None:
    """web 通道的 files 是 dict 信封：uploaded_images 列表被识别。"""

    files = {"uploaded_images": [_image("/tmp/a.png")]}
    result = extract_multimodal_image_files({"files": files})
    assert [r["path"] for r in result] == ["/tmp/a.png"]


def test_extract_from_media_items() -> None:
    """media_items 列表形态依旧生效（web normalize 后的等价入口）。"""

    result = extract_multimodal_image_files({"media_items": [_image("/tmp/a.png")]})
    assert [r["path"] for r in result] == ["/tmp/a.png"]


def test_dedup_and_invalid_filtered() -> None:
    """重复路径去重；非 image 项被 _normalize_image_file 过滤。"""

    files = [
        _image("/tmp/a.png"),
        _image("/tmp/a.png"),
        {"type": "file", "path": "/tmp/x.pdf", "mime_type": "application/pdf"},
    ]
    result = extract_multimodal_image_files({"files": files})
    assert [r["path"] for r in result] == ["/tmp/a.png"]
