# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""multipart/form-data 解析公共层。

技能上传（``/file-api/skills/*``）与统一资产上传
（``/file-api/assets/upload-temp``）共用同一份解析实现；解析失败抛
:class:`MultipartParseError`，由各调用方按自身错误码体系收敛
（技能侧转 ``SkillRpcError``，资产侧转 ``ASSET_*`` 契约码）。

解析结果：字段名 → 值。带 filename 的分部返回
``{"filename": str, "content": bytes}``，纯文本分部返回 ``str``。
"""

from __future__ import annotations

import email.parser
import email.policy
from typing import Any


class MultipartParseError(ValueError):
    """multipart 表单无法解析。``str(exc)`` 可直接作为用户可见错误信息。"""


def parse_multipart_form(content_type: str, body: bytes) -> dict[str, Any]:
    """解析 multipart/form-data，返回字段名 → str 或 {filename, content}."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise MultipartParseError("Content-Type 须为 multipart/form-data")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + body)
    if not msg.is_multipart():
        raise MultipartParseError("无法解析 multipart 表单")

    fields: dict[str, Any] = {}
    for part in msg.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            content = part.get_content()
            if isinstance(content, str):
                payload = content.encode("utf-8")
            elif isinstance(content, bytes):
                payload = content
            else:
                payload = b""
        if filename is not None:
            fields[name] = {"filename": filename, "content": payload}
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields
