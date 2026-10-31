# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""read_file 校验轨对多文件并行读取与 ToolOutput 结果的判定。"""

from __future__ import annotations

from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    _looks_like_binary_payload,
    build_native_image_tool_error,
    build_provided_image_read_error,
    extract_paths_from_arguments,
    extract_vision_target,
    is_image_file_path,
    is_non_text_file_path,
    is_vision_tool,
    validate_read_file_result,
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


def test_provided_image_read_error_forbids_read_file() -> None:
    error = build_provided_image_read_error("/uploads/home.png")

    assert error.startswith("[READ_FILE_ERROR]")
    assert "read_file" in error
    assert "/uploads/home.png" in error


def test_provided_image_read_error_english() -> None:
    error = build_provided_image_read_error("/uploads/home.png", language="en")

    assert error.startswith("[READ_FILE_ERROR]")
    assert "do not call read_file" in error


def test_extract_paths_from_arguments_covers_file_paths_list() -> None:
    paths = extract_paths_from_arguments(
        {"file_paths": ["/uploads/a.png", "/uploads/b.png"]}
    )

    assert paths == ["/uploads/a.png", "/uploads/b.png"]


def test_extract_paths_from_arguments_single_path() -> None:
    assert extract_paths_from_arguments({"file_path": "/uploads/a.png"}) == [
        "/uploads/a.png"
    ]


def test_is_vision_tool_recognizes_vqa_names() -> None:
    assert is_vision_tool("visual_question_answering") is True
    assert is_vision_tool("image_reading") is True
    assert is_vision_tool("read_file") is False


def test_extract_vision_target_reads_image_path_or_url() -> None:
    assert (
        extract_vision_target(
            {"image_path_or_url": "/uploads/a.png", "question": "这是什么"}
        )
        == "/uploads/a.png"
    )


def test_native_image_tool_error_mentions_provided_pixels() -> None:
    error = build_native_image_tool_error()

    assert error.startswith("[VISION_TOOL_SKIPPED]")
    assert "visual_question_answering" in error
    assert "像素" in error


def test_native_image_tool_error_english() -> None:
    error = build_native_image_tool_error(language="en")

    assert error.startswith("[VISION_TOOL_SKIPPED]")
    assert "Do not call" in error


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
