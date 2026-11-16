# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``/file-api/skills/*`` multipart 上传处理."""

from __future__ import annotations

import asyncio
import email.parser
import email.policy
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent_gateway.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from e2a.models import E2AEnvelope
from e2a.wire_codec import parse_agent_server_wire_unary

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.protocol import CONNECTION_ROLE_HEADER, CONNECTION_ROLE_RPC
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_PACKAGE,
    ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT,
    ERROR_SKILL_UNSAFE_PATH,
    SKILL_ZIP_MAX_UPLOAD_BYTES,
    SkillManager,
    SkillRpcError,
)

logger = logging.getLogger(__name__)

#: 两阶段上传的受控临时根目录名；目录结构与历史扁平前缀均在此统一回收。
SKILL_UPLOAD_ROOT_NAME = "jiuwenswarm_skill_upload"
#: 历史版本直接落在临时目录根下的扁平目录前缀，仅用于兜底回收。
SKILL_UPLOAD_LEGACY_PREFIX = "jiuwenswarm_skill_upload_"
#: 上传包大小上限，与 SkillManager 的 zip 限制保持一致。
SKILL_UPLOAD_MAX_BYTES = SKILL_ZIP_MAX_UPLOAD_BYTES
#: 请求体上限：预留 multipart 头部开销，避免合法 zip 被误拒。
SKILL_UPLOAD_MAX_REQUEST_BYTES = SKILL_UPLOAD_MAX_BYTES + 2 * 1024 * 1024
#: 上传残留的兜底 TTL（前端 discard 未送达时回收）。
SKILL_UPLOAD_TTL_SECONDS = 24 * 60 * 60

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


def skill_upload_root() -> Path:
    """返回受控上传根目录（不保证存在）."""
    return Path(tempfile.gettempdir()) / SKILL_UPLOAD_ROOT_NAME


def _resolve_existing(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def discard_skill_upload(path_value: str) -> bool:
    """删除受控上传根内的上传文件或专属目录（幂等）.

    仅接受以下两类路径，其余一律拒绝，避免误删用户文件：
    - ``<root>/<file>``：dev 中间件使用的扁平布局；
    - ``<root>/<upload-dir>/<file>``：upload-temp 创建的专属目录。
    """
    text = str(path_value or "").strip()
    if not text:
        return False
    root = _resolve_existing(skill_upload_root())
    if root is None:
        return False
    resolved = _resolve_existing(Path(text))
    if resolved is None or resolved == root:
        return False
    if resolved.parent == root:
        try:
            resolved.unlink()
        except OSError:
            return False
        return True
    if resolved.parent.parent == root and resolved.parent != root:
        shutil.rmtree(resolved.parent, ignore_errors=True)
        return not resolved.parent.exists()
    return False


def cleanup_stale_skill_uploads(
    max_age_seconds: int = SKILL_UPLOAD_TTL_SECONDS,
) -> int:
    """回收超过 TTL 的上传残留（新布局专属目录 + 历史扁平前缀目录）."""
    root = skill_upload_root()
    tmpdir = Path(tempfile.gettempdir())
    candidates: list[Path] = []
    if root.is_dir():
        try:
            candidates.extend(root.iterdir())
        except OSError:
            pass
    try:
        candidates.extend(tmpdir.glob(f"{SKILL_UPLOAD_LEGACY_PREFIX}*"))
    except OSError:
        pass

    removed = 0
    now = time.time()
    visited: set[Path] = set()
    for entry in candidates:
        try:
            if entry == root or entry in visited:
                continue
            visited.add(entry)
            if now - entry.stat().st_mtime < max_age_seconds:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def handle_skills_upload_temp_http(
    *,
    content_type: str,
    body: bytes,
) -> tuple[int, dict[str, Any]]:
    """保存上传文件，返回后续 skills.* WebSocket RPC 使用的本地路径."""
    if len(body) > SKILL_UPLOAD_MAX_REQUEST_BYTES:
        return 413, skill_http_error_body("SKILL_FILE_TOO_LARGE", "上传包超过限制")
    try:
        fields = parse_multipart_form(content_type, body)
        file_field = fields.get("file")
        if not isinstance(file_field, dict) or not isinstance(file_field.get("content"), (bytes, bytearray)):
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "缺少 file 字段")
        filename = file_field.get("filename")
        if not isinstance(filename, str):
            raise SkillRpcError(ERROR_SKILL_UNSAFE_PATH, "文件路径非法或指向保留目录")
        if (
            filename in {"", ".", ".."}
            or any(char in filename for char in '/\\:\x00')
            or PureWindowsPath(filename).is_reserved()
        ):
            raise SkillRpcError(ERROR_SKILL_UNSAFE_PATH, "文件路径非法或指向保留目录")

        cleanup_stale_skill_uploads()
        upload_root = skill_upload_root()
        upload_root.mkdir(parents=True, exist_ok=True)
        upload_dir = Path(tempfile.mkdtemp(prefix="", dir=str(upload_root)))
        dest = upload_dir / filename
        try:
            dest.write_bytes(bytes(file_field["content"]))
        except (OSError, ValueError, TypeError):
            shutil.rmtree(upload_dir)
            raise
        # 文件须保留到后续 RPC（含同名技能覆盖确认后的再次导入），
        # 由导入完成后的服务端清理、前端 discard 或 TTL 回收负责删除。
        return 200, {"path": str(dest)}
    except SkillRpcError as exc:
        return skill_http_error_status(exc.code), skill_http_error_body(exc.code, exc.message)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skills_multipart_http] temporary upload failed: %s", exc)
        return 500, skill_http_error_body(ERROR_SKILL_INVALID_PACKAGE, str(exc))


def _parse_overwrite(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _build_ws_origin(uri: str) -> str | None:
    """将 ws/wss URI 转为标准浏览器 Origin（与 AgentServer 握手校验对齐）。"""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}"


class _AgentServerSkillWsClient:
    """``skills.*`` 专用最小 WebSocket 客户端（单连接、单次非流式请求）。

    仅覆盖本模块实际使用的能力：``connect``（含 ``connection.ack`` 首帧容错）、
    单次 ``send_request``（按 ``request_id`` 关联响应）、``disconnect``。
    不引入流式接收、server_push、断线重连与取消登记等北向客户端特性。
    """

    #: 等待 AgentServer ``connection.ack`` 的上限（秒）。
    _ACK_TIMEOUT_S = 5.0

    def __init__(self) -> None:
        self._ws: Any = None

    async def connect(self, uri: str) -> None:
        # 声明为一次性 RPC 连接：AgentServer 不会把它当作 server push 通道，
        # 断开时也不会清掉 Gateway 主连接的推送通道。
        role_headers = {CONNECTION_ROLE_HEADER: CONNECTION_ROLE_RPC}
        try:
            from websockets.legacy.client import connect as connect_fn

            header_kwargs: dict[str, Any] = {"extra_headers": list(role_headers.items())}
        except ImportError:
            import websockets

            connect_fn = websockets.connect
            header_kwargs = {"additional_headers": role_headers}
        self._ws = await connect_fn(
            uri,
            origin=_build_ws_origin(uri),
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5.0,
            max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            **header_kwargs,
        )
        # AgentServer 建连后必发 connection.ack 事件帧；须先消费掉，
        # 否则会被当作首个响应而错位。
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self._ACK_TIMEOUT_S)
        data = json.loads(raw)
        if not (data.get("type") == "event" and data.get("event") == "connection.ack"):
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE, "AgentServer 未就绪（未收到 connection.ack）"
            )

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        """发送非流式 E2A 信封，返回按 ``request_id`` 关联的响应。"""
        request_id = str(envelope.request_id)
        await self._ws.send(json.dumps(envelope.to_dict(), ensure_ascii=False))
        while True:
            data = json.loads(await self._ws.recv())
            if data.get("type") == "event":
                # connection.ack 等事件帧不承载请求响应。
                continue
            if str(data.get("request_id") or "") != request_id:
                logger.warning(
                    "[skills_multipart_http] 丢弃 request_id 不匹配的响应: %s",
                    data.get("request_id"),
                )
                continue
            return parse_agent_server_wire_unary(data)

    async def disconnect(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skills_multipart_http] 关闭 AgentServer 连接异常: %s", exc)
        finally:
            self._ws = None


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
    client = _AgentServerSkillWsClient()
    uri = _agent_server_ws_uri()
    request_id = f"file-api-{uuid.uuid4().hex}"
    try:
        await client.connect(uri)
        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id="web",
            session_id=f"file-api-{request_id}",
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
