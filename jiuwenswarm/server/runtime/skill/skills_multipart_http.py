# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``/file-api/skills/import`` 与 ``/file-api/skills/create-from-knowledge`` multipart 处理."""

from __future__ import annotations

import asyncio
import email.parser
import email.policy
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_PACKAGE,
    ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT,
    SkillManager,
    SkillRpcError,
)

logger = logging.getLogger(__name__)

_SKILL_HTTP_ERROR_STATUS: dict[str, int] = {
    "SKILL_ALREADY_EXISTS": 409,
    "SKILL_IMPORT_OVERWRITE_REQUIRED": 409,
    "SKILL_NAME_CONFLICT": 409,
    "SKILL_PUBLISH_VERSION_CONFLICT": 409,
    "SKILL_BUILTIN_READ_ONLY": 403,
    "SKILL_DOWNLOAD_TOKEN_INVALID": 401,
    "SKILL_NOT_FOUND": 404,
    "SKILL_VERSION_NOT_FOUND": 404,
    "SKILL_INVALID_PACKAGE": 400,
    "SKILL_INVALID_METADATA": 400,
    "SKILL_RESERVED_PATH": 400,
    "SKILL_UNSAFE_PATH": 400,
    "SKILL_FILE_TOO_LARGE": 400,
    "SKILL_KNOWLEDGE_INPUT_CONFLICT": 400,
    "SKILL_VERSION_CONTENT_INVALID": 400,
    "SKILL_REBUILD_FAILED": 500,
}


def skill_http_error_status(code: str) -> int:
    return _SKILL_HTTP_ERROR_STATUS.get(code, 400)


def skill_http_error_body(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "error": message}


def parse_multipart_form(content_type: str, body: bytes) -> dict[str, Any]:
    """解析 multipart/form-data，返回字段名 → str 或 {filename, content}."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "Content-Type 须为 multipart/form-data")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + body)
    if not msg.is_multipart():
        raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "无法解析 multipart 表单")

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


def _parse_overwrite(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _agent_server_ws_uri() -> str:
    url = (os.getenv("AGENT_SERVER_URL") or "").strip()
    if url:
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        return url
    host = (os.getenv("AGENT_SERVER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = (os.getenv("AGENT_SERVER_PORT") or os.getenv("AGENT_PORT") or "18092").strip()
    return f"ws://{host}:{port}"


async def _call_agent_skill_rpc(
    *,
    method: ReqMethod,
    params: dict[str, Any],
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """经 AgentServer WebSocket 调用 skills.*，返回 payload 或抛 SkillRpcError."""
    client = WebSocketAgentServerClient(ping_interval=None, ping_timeout=None)
    uri = _agent_server_ws_uri()
    request_id = f"file-api-{uuid.uuid4().hex}"
    try:
        await client.connect(uri)
        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id="web",
            session_id=f"file-api:{request_id}",
            req_method=method,
            params=params,
            is_stream=False,
        )
        resp = await asyncio.wait_for(client.send_request(envelope), timeout=timeout_s)
    finally:
        try:
            await client.disconnect()
        except OSError:
            pass

    payload = resp.payload if isinstance(getattr(resp, "payload", None), dict) else {}
    if not getattr(resp, "ok", False):
        code = str(payload.get("code") or "").strip() or ERROR_SKILL_INVALID_PACKAGE
        message = str(payload.get("message") or payload.get("error") or "请求失败").strip()
        raise SkillRpcError(code, message)
    return payload


def _run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            error.append(exc)

    import threading

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")


def handle_skills_import_http(
    *,
    content_type: str,
    body: bytes,
    use_local_manager: bool = False,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /file-api/skills/import``，返回 (status, json_body)."""
    try:
        fields = parse_multipart_form(content_type, body)
        file_field = fields.get("file")
        if not isinstance(file_field, dict) or not isinstance(file_field.get("content"), (bytes, bytearray)):
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "缺少 file 字段")
        filename = str(file_field.get("filename") or "upload.zip")
        content = bytes(file_field["content"])
        overwrite = _parse_overwrite(fields.get("overwrite", "false"))

        name_lower = filename.lower()
        if name_lower.endswith(".skill") or name_lower.endswith(".skill.zip") or not name_lower.endswith(".zip"):
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "仅接受 .zip 文件")

        with tempfile.TemporaryDirectory(prefix="jiuwenswarm_import_http_") as tmpdir:
            zip_path = Path(tmpdir) / "upload.zip"
            zip_path.write_bytes(content)
            params = {"path": str(zip_path), "overwrite": overwrite}
            if use_local_manager:
                payload = _run_coro(SkillManager().handle_skills_import_upload(params))
            else:
                payload = _run_coro(
                    _call_agent_skill_rpc(
                        method=ReqMethod.SKILLS_IMPORT_UPLOAD,
                        params=params,
                    )
                )
        return 200, payload
    except SkillRpcError as exc:
        return skill_http_error_status(exc.code), skill_http_error_body(exc.code, exc.message)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skills_multipart_http] import failed: %s", exc)
        return 500, skill_http_error_body("SKILL_INVALID_PACKAGE", str(exc))


def _parse_knowledge_upload_fields(
    fields: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    """解析 create-from-knowledge 表单字段，返回 RPC params 与可选上传目录."""
    link_raw = fields.get("link")
    link = str(link_raw).strip() if isinstance(link_raw, str) else ""
    file_field = fields.get("file")
    has_file = isinstance(file_field, dict) and isinstance(
        file_field.get("content"), (bytes, bytearray)
    )
    has_link = bool(link)
    if has_link == has_file:
        raise SkillRpcError(
            ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT,
            "link 与 file 必须且只能提供一个",
        )

    skill_description_raw = fields.get("skill_description")
    skill_description = (
        str(skill_description_raw).strip() if isinstance(skill_description_raw, str) else ""
    )

    params: dict[str, Any] = {"skill_description": skill_description}
    upload_dir: Path | None = None
    if has_link:
        params["link"] = link
    else:
        if not isinstance(file_field, dict):
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "缺少 file 字段")
        upload_dir = Path(tempfile.mkdtemp(prefix="jiuwenswarm_knowledge_upload_"))
        filename = str(file_field.get("filename") or "document.bin")
        safe_name = Path(filename).name or "document.bin"
        dest = upload_dir / safe_name
        dest.write_bytes(bytes(file_field["content"]))
        params["file_path"] = str(dest)
    return params, upload_dir


def handle_skills_create_from_knowledge_http(
    *,
    content_type: str,
    body: bytes,
    use_local_manager: bool = False,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /file-api/skills/create-from-knowledge``，返回 (status, json_body)."""
    upload_dir: Path | None = None
    try:
        fields = parse_multipart_form(content_type, body)
        params, upload_dir = _parse_knowledge_upload_fields(fields)

        if use_local_manager:
            # 单测/无 Agent 时仅跑领域准备；完整路径需 Agent 静默执行
            mgr = SkillManager()
            prepared = _run_coro(mgr.handle_skills_create_from_knowledge(params))
            return 200, prepared

        payload = _run_coro(
            _call_agent_skill_rpc(
                method=ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE,
                params=params,
                timeout_s=900.0,
            )
        )
        return 200, payload
    except SkillRpcError as exc:
        return skill_http_error_status(exc.code), skill_http_error_body(exc.code, exc.message)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skills_multipart_http] create-from-knowledge failed: %s", exc)
        return 500, skill_http_error_body("SKILL_INVALID_PACKAGE", str(exc))
    finally:
        if upload_dir is not None and use_local_manager:
            # Agent 路径由 Agent 侧清理；本地仅准备时留下给调用方，此处不删
            pass
