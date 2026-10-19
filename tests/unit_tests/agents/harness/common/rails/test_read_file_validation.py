# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""read_file 校验轨对多文件并行读取与 ToolOutput 结果的判定。"""

from __future__ import annotations

import pytest
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.tool_result_schema import (
    normalize_tool_result,
)
from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    _looks_like_binary_payload,
    build_native_image_tool_error,
    build_provided_image_read_error,
    extract_paths_from_arguments,
    is_image_file_path,
    is_non_text_file_path,
    is_remote_image_url,
    is_vision_tool,
    validate_read_file_result,
    vision_tool_image_targets,
    vision_tool_targets_all_provided,
)

_SVG_WITH_BASE64 = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
    '<image href="data:image/png;base64,'
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    '/></svg>'
)


def _multi_file_output(content_a: str, content_b: str) -> ToolOutput:
    return ToolOutput(
        success=True,
        data={
            "files": [
                {
                    "file_path": "a/read_file_validation.py",
                    "content": content_a,
                    "type": "text",
                    "multimodal": [],
                    "line_count": 10,
                    "error": None,
                },
                {
                    "file_path": "b/notes.md",
                    "content": content_b,
                    "type": "text",
                    "multimodal": [],
                    "line_count": 5,
                    "error": None,
                },
            ],
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "parallel_read": True,
            "multimodal": [],
        },
    )


def test_parallel_read_aggregate_with_binary_marker_in_content_passes() -> None:
    """多文件聚合结果里的源码文本含 "binary file" 字样不得整体误判为二进制误读。"""
    result = _multi_file_output(
        'BINARY_MARKERS = ("binary file", "not a text file")\n' * 40,
        "# notes about binary file handling\n" * 20,
    )

    assert validate_read_file_result("", result) == (True, None)


def test_parallel_read_aggregate_as_plain_dict_passes() -> None:
    """dict 形态的聚合结果（不经过 ToolOutput）同样放行。"""
    result = _multi_file_output("print('hello')\n", "abc").model_dump()

    assert validate_read_file_result("", result) == (True, None)


def test_single_file_tool_output_content_with_marker_passes() -> None:
    """单文件 ToolOutput：正确解包 data.content，长文本不套用特征词表。"""
    result = ToolOutput(
        success=True,
        data={
            "file_path": "src/validators.py",
            "content": 'if "binary file" in lowered:\n    return True\n' * 60,
            "type": "text",
        },
    )

    assert validate_read_file_result("src/validators.py", result) == (True, None)


def test_single_file_tool_output_failure_uses_error() -> None:
    result = ToolOutput(success=False, error="Binary files cannot be read as text: 'x.bin'.")

    ok, error = validate_read_file_result("x.bin", result)

    assert ok is False
    assert "Binary files cannot be read" in error


def test_short_error_message_content_still_rejected() -> None:
    """错误消息样的短文本命中特征词仍被拦截（词表的本职场景）。"""
    ok, error = validate_read_file_result("a.txt", "Cannot read binary file: a.txt")

    assert ok is False
    assert error is not None


def test_null_bytes_in_long_content_still_rejected() -> None:
    """长文本中的空字节（真实二进制误读）不受长度门限影响。"""
    result = ToolOutput(
        success=True,
        data={
            "file_path": "blob.dat",
            "content": "text" * 100 + "\x00" + "more text" * 100,
            "type": "text",
        },
    )

    ok, _error = validate_read_file_result("blob.dat", result)

    assert ok is False


def test_empty_result_still_rejected() -> None:
    ok, _error = validate_read_file_result("a.txt", "")

    assert ok is False


@pytest.mark.parametrize(
    ("language", "forbidden_phrase"),
    [("cn", "无需再调用 read_file"), ("en", "do not call read_file")],
    ids=["cn", "en"],
)
def test_provided_image_read_error_forbids_read_file(language, forbidden_phrase) -> None:
    error = build_provided_image_read_error("/uploads/home.png", language=language)

    assert error.startswith("[READ_FILE_ERROR]")
    assert "/uploads/home.png" in error
    assert forbidden_phrase in error


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"file_paths": ["/uploads/a.png", "/uploads/b.png"]}, ["/uploads/a.png", "/uploads/b.png"]),
        ({"file_path": "/uploads/a.png"}, ["/uploads/a.png"]),
    ],
    ids=["batch", "single"],
)
def test_extract_paths_from_arguments_covers_single_and_batch(arguments, expected) -> None:
    # 批量拒绝（N4）依赖这里把 file_paths 摊平成路径列表。
    assert extract_paths_from_arguments(arguments) == expected


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("visual_question_answering", True),
        ("vision_qa", True),
        ("VisionQA", True),
        ("image_reading", True),
        # agent-core 的真实 OCR 工具名 + 前端 / 权限投影的显示名别名。
        ("image_ocr", True),
        ("ImageOCR", True),
        ("  image_ocr  ", True),
        ("read_file", False),
        ("bash", False),
        ("", False),
    ],
)
def test_is_vision_tool_recognizes_vqa_and_ocr_names(tool_name, expected) -> None:
    """``image_ocr`` 漏登记会让原生看图仍去调 OCR（CR-8）；别名两侧都要认。"""

    assert is_vision_tool(tool_name) is expected


@pytest.mark.parametrize(
    ("language", "required_phrase"),
    [("cn", "像素"), ("en", "Do not call")],
    ids=["cn", "en"],
)
def test_native_image_tool_error_is_language_aware(language, required_phrase) -> None:
    error = build_native_image_tool_error(language=language)

    assert error.startswith("[VISION_TOOL_SKIPPED]")
    assert "visual_question_answering" in error
    assert required_phrase in error


def test_svg_is_read_as_text_not_image() -> None:
    """SVG 是文本 XML：前置拦截与后置校验都不应把它当图片。"""
    assert is_image_file_path("diagram.svg") is False
    assert is_non_text_file_path("diagram.svg") is False


def test_svg_with_embedded_base64_passes_validation() -> None:
    """内嵌 base64 图片的合法 SVG 不得被二进制启发式误拦。"""
    assert _looks_like_binary_payload(_SVG_WITH_BASE64) is False
    assert validate_read_file_result("diagram.svg", _SVG_WITH_BASE64) == (True, None)


def test_image_extensions_still_blocked() -> None:
    """防误扩大：位图扩展名仍按图片/二进制拦截。"""
    assert is_image_file_path("photo.png") is True
    assert is_non_text_file_path("photo.jpg") is True

    ok, error = validate_read_file_result("photo.png", "\x00\x01binary")

    assert ok is False
    assert error is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a.png", True),
        ("http://example.com/a.png", True),
        ("  HTTP://example.com/a.png  ", True),
        ("/uploads/a.png", False),
        ("C:\\work\\a.png", False),
        ("", False),
    ],
)
def test_is_remote_image_url_detects_http(value, expected) -> None:
    """远程 HTTP(S) 图片的目标不能按"已提供像素"拦截，read_file 无法替代抓取。"""

    assert is_remote_image_url(value) is expected


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("visual_question_answering", {"image_path_or_url": "/u/a.png", "question": "?"}, ["/u/a.png"]),
        ("image_ocr", {"image_path_or_url": "https://x/a.png", "prompt": "ocr"}, ["https://x/a.png"]),
        ("image_reading", {"local_url": "/u/a.png"}, ["/u/a.png"]),
        ("image_reading", {"remote_url": "https://x/a.png"}, ["https://x/a.png"]),
        # 未附带图片 / 缺参 → 未知目标
        ("visual_question_answering", {"question": "?"}, []),
        ("visual_question_answering", {}, []),
        ("image_ocr", None, []),
    ],
)
def test_vision_tool_image_targets_extracts_path_or_url(tool_name, arguments, expected) -> None:
    assert vision_tool_image_targets(tool_name, arguments) == expected


def test_vision_tool_targets_all_provided_blocks_only_provided_local_path(tmp_path) -> None:
    """已提供的本地目标→拦；远程 / 未知 / 未提供目标→放行（ND：避免误封远程图片）。"""
    provided = tmp_path / "a.png"
    provided.write_bytes(b"fake")

    assert vision_tool_targets_all_provided(
        "visual_question_answering",
        {"image_path_or_url": str(provided), "question": "?"},
        [str(provided)],
    ) is True

    assert vision_tool_targets_all_provided(
        "visual_question_answering",
        {"image_path_or_url": "https://cdn.example.com/b.png", "question": "?"},
        [str(provided)],
    ) is False

    assert vision_tool_targets_all_provided(
        "visual_question_answering",
        {"image_path_or_url": str(tmp_path / "unknown.png"), "question": "?"},
        [str(provided)],
    ) is False

    assert vision_tool_targets_all_provided(
        "visual_question_answering",
        {"question": "no image arg"},
        [str(provided)],
    ) is False


# ── CR-8：归一 rail 先于 read_file 校验执行（结果已是 _ResultDict）────────────


def test_normalized_empty_file_still_rejected() -> None:
    # 归一 rail(82) 先于本 rail(80)：空文件归一后是 {"data":{"content":""},...}，
    # 不能被 str(dict) repr 判成“非空”，端到端必须仍报 [READ_FILE_ERROR]。
    normalized = normalize_tool_result("")
    ok, error = validate_read_file_result("a.txt", normalized)

    assert ok is False
    assert error is not None
    assert error.startswith("[READ_FILE_ERROR]")
    assert "为空" in error


def test_normalized_text_passes() -> None:
    assert validate_read_file_result("a.txt", normalize_tool_result("hello")) == (True, None)


def test_normalized_null_bytes_still_rejected() -> None:
    # 归一后若退回 str(dict)，NUL 会被 repr 转义成 \x00，二进制启发式失效。
    normalized = normalize_tool_result("a\x00b" + "x" * 600)
    ok, error = validate_read_file_result("a.txt", normalized)

    assert ok is False
    assert error is not None
    assert error.startswith("[READ_FILE_ERROR]")


@pytest.mark.parametrize(
    "warning",
    [
        "Warning: the file exists but the contents are empty.",
        "Warning: the document exists but the contents are empty.",
    ],
)
def test_empty_file_warning_text_is_rejected(warning: str) -> None:
    # openjiuwen read_file 对空文件返回 warning 文本（success=True），归一后
    # 必须仍判 [READ_FILE_ERROR]，否则模型会把空文件当成功并反复重试（CR-8）。
    ok, error = validate_read_file_result("a.txt", normalize_tool_result(warning))

    assert ok is False
    assert error is not None
    assert "为空" in error
