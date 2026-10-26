# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""read_file 校验轨对多文件并行读取与 ToolOutput 结果的判定。"""

from __future__ import annotations

from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    validate_read_file_result,
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
