# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""multipart/form-data 解析公共层单测。"""

from __future__ import annotations

import io

import pytest

from jiuwenswarm.common.multipart_form import MultipartParseError, parse_multipart_form

_BOUNDARY = "----testboundary"


def _multipart(
    fields: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes]] | None = None,
) -> tuple[str, bytes]:
    buffer = io.BytesIO()
    for name, value in (fields or {}).items():
        buffer.write(
            f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, content) in (files or {}).items():
        buffer.write(
            (
                f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        buffer.write(content)
        buffer.write(b"\r\n")
    buffer.write(f"--{_BOUNDARY}--\r\n".encode())
    return f"multipart/form-data; boundary={_BOUNDARY}", buffer.getvalue()


def test_parse_text_fields():
    content_type, body = _multipart({"link": "https://a.example", "desc": "文本"})
    fields = parse_multipart_form(content_type, body)
    assert fields["link"] == "https://a.example"
    assert fields["desc"] == "文本"


def test_parse_file_part_keeps_filename_and_bytes():
    content_type, body = _multipart(
        {"kind": "skill"}, {"file": ("pack.zip", b"\x00\x01binary")}
    )
    fields = parse_multipart_form(content_type, body)
    assert fields["kind"] == "skill"
    assert fields["file"] == {"filename": "pack.zip", "content": b"\x00\x01binary"}


def test_rejects_non_multipart_content_type():
    with pytest.raises(MultipartParseError):
        parse_multipart_form("application/json", b"{}")


def test_rejects_body_without_parts():
    with pytest.raises(MultipartParseError):
        parse_multipart_form(f"multipart/form-data; boundary={_BOUNDARY}", b"plain")


def test_skills_wrapper_keeps_skill_rpc_error():
    """技能侧包装行为不变：解析失败仍抛 SkillRpcError(SKILL_INVALID_PACKAGE)。"""
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillRpcError
    from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
        parse_multipart_form as skills_parse,
    )

    with pytest.raises(SkillRpcError) as excinfo:
        skills_parse("application/json", b"{}")
    assert excinfo.value.code == "SKILL_INVALID_PACKAGE"

    content_type, body = _multipart({"a": "1"})
    assert skills_parse(content_type, body) == {"a": "1"}
