# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""图片引用行构造/解析 + ``extract_multimodal_image_files`` 的 files 形态归一。"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.multimodal.image_reference import (
    augment_message_with_reference_lines,
    build_image_reference_line,
    extract_multimodal_image_files,
    normalize_image_mention_text,
    normalized_text_mentions_image,
    parse_image_references,
    text_mentions_image,
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


# ---------------------------------------------------------------------------
# leader 分发兜底：消息里只提文件名 / 路径时补引用行
# ---------------------------------------------------------------------------

_MANIFEST = [
    {
        "type": "image",
        "path": "/tmp/2.jpg",
        "filename": "2.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 111,
    },
    {
        "type": "image",
        "path": "/tmp/3.jpg",
        "filename": "3.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 222,
    },
]


def test_augment_by_filename_appends_reference_line() -> None:
    text = "请识别 3.jpg 并给出描述。"

    updated = augment_message_with_reference_lines(text, _MANIFEST)

    assert updated.startswith(text)
    assert "[image #1] path=/tmp/3.jpg filename=3.jpg mime=image/jpeg size=222" in updated
    assert "2.jpg" not in updated.split("\n")[-1]


def test_augment_by_path_appends_reference_line() -> None:
    text = "图片在 /tmp/2.jpg"

    updated = augment_message_with_reference_lines(text, _MANIFEST)

    assert "[image #1] path=/tmp/2.jpg filename=2.jpg mime=image/jpeg size=111" in updated


def test_augment_skips_images_not_mentioned() -> None:
    text = "请识别第二张图片"  # 没有文件名 / 路径，无从判断是哪张

    assert augment_message_with_reference_lines(text, _MANIFEST) == text


def test_augment_is_idempotent_for_existing_reference_line() -> None:
    text = (
        "任务：\n"
        "[image #2] path=/tmp/3.jpg filename=3.jpg mime=image/jpeg size=222"
    )

    assert augment_message_with_reference_lines(text, _MANIFEST) == text


def test_augment_uses_next_index_and_respects_cap() -> None:
    text = "看 [image #7] path=/other.png filename=other.png mime=image/png，识别 2.jpg 与 3.jpg"

    updated = augment_message_with_reference_lines(text, _MANIFEST, max_images=1)

    assert "[image #8] path=/tmp/2.jpg" in updated
    assert "/tmp/3.jpg" not in updated


def test_augment_without_manifest_is_noop() -> None:
    text = "识别 3.jpg"
    assert augment_message_with_reference_lines(text, None) == text
    assert augment_message_with_reference_lines(text, []) == text


# ---------------------------------------------------------------------------
# 提及判定（CR-9634-2）：边界对齐 + 分隔符 / 大小写归一
# ---------------------------------------------------------------------------


def test_text_mentions_image_requires_token_boundaries() -> None:
    """命中必须整名对齐：``1.png`` 不命中 ``11.png`` / ``data.png``。"""

    image = _image("/tmp/1.png", filename="1.png")

    assert text_mentions_image(image, "请识别 1.png 并描述") is True
    assert text_mentions_image(image, "请识别3.png") is False
    assert text_mentions_image(image, "见 C:/uploads/11.png") is False
    assert text_mentions_image(image, "见 data.png") is False


def test_text_mentions_image_matches_chinese_adjacent_filename() -> None:
    """中文紧贴文件名仍是合法提及（边界按 ASCII 文件名类字符判定）。"""

    image = _image("/tmp/3.jpg", filename="3.jpg")

    assert text_mentions_image(image, "请识别3.jpg") is True


def test_text_mentions_image_normalizes_separator_and_case() -> None:
    """路径比较前统一分隔符并忽略大小写；filename 按 basename 形态比较。"""

    image = _image("C:/uploads/3.jpg", filename="C:\\uploads\\3.jpg")

    assert text_mentions_image(image, "图片在 C:\\Uploads\\3.JPG") is True
    assert text_mentions_image(image, "图片在 c:/uploads/3.jpg") is True
    assert text_mentions_image(image, "图片在 C:/uploads/13.jpg") is False


def test_augment_ignores_substring_filename_mention() -> None:
    """leader 兜底同样不得把 11.png / data.png 当成 1.png（裸子串过宽）。"""

    manifest = [{"type": "image", "path": "/tmp/1.png", "filename": "1.png", "mime_type": "image/png"}]

    assert augment_message_with_reference_lines("识别 11.png", manifest) == "识别 11.png"
    assert augment_message_with_reference_lines("识别 data.png", manifest) == "识别 data.png"


def test_normalized_text_mentions_image_reuses_one_normalized_text() -> None:
    """已归一入口：调用方归一一次即可对多张图复用（批量场景不重复 lower 同一段文本）。"""

    normalized = normalize_image_mention_text("请识别 C:\\Uploads\\1.PNG")
    first = _image("/tmp/1.png", filename="1.png")
    second = _image("/tmp/11.png", filename="11.png")

    assert normalized_text_mentions_image(first, normalized) is True
    assert normalized_text_mentions_image(second, normalized) is False


def test_augment_matches_windows_separator_path_mention() -> None:
    """正文用 Windows 反斜杠形态写路径时也能补引用行（文件名不同名时靠路径归一）。"""

    manifest = [
        {
            "type": "image",
            "path": "C:/uploads/3.jpg",
            "filename": "截图 3.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 222,
        }
    ]

    updated = augment_message_with_reference_lines("请识别 C:\\uploads\\3.jpg", manifest)

    assert "[image #1] path=C:/uploads/3.jpg" in updated

