"""WorkspaceFileAdapter：工作区与文件域用户业务适配器。

复用 ``server/runtime/attachments``（媒体/文档附件中立工具）与
``common/utils`` 目录门面；本适配器只读写当前 AgentServer 注入目录。

覆盖：
- ``media.persist``：浏览器上传图片 base64 解码后落盘
  ``agent/sessions/<sid>/uploads``（复用 ``normalize_chat_media_attachments``）；
- ``document.persist`` / ``document.formats``：文档本地路径黑名单校验与格式
  列表（复用 ``persist_and_parse_documents`` / ``forbidden_formats``，不落盘）；
- ``path.select_directory`` / ``path.select_files``：**注入目录内枚举**（决策 D3）。
  目录选择在注入 workspace 根内解析/校验并返回 AgentServer 侧绝对路径；
  文件选择枚举注入目录下可上传文件元数据（不含 base64，避免大帧压垮
  WebSocket；小附件内容经 E2A 或受认证 HTTP bridge 传输）；
- ``file.import_url``：chat.send 上行外部 url 文件由 AgentServer 下载落盘；
- ``file.upload_chunk``：分块上传（受认证 HTTP bridge 大文件传输）；
- ``im.file_persist``：IM 平台附件落盘（Gateway 下载字节后经 E2A 交
  AgentServer 落盘注入目录，Gateway 不直写用户目录）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import stat
from pathlib import Path
from typing import Any, Final

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_workspace_dir, get_user_workspace_dir
from jiuwenswarm.agents.harness.common.tools.web_file_download import is_path_within_user_dirs
from jiuwenswarm.server.runtime.attachments.document_attachments import (
    forbidden_formats,
    persist_and_parse_documents,
)
from jiuwenswarm.server.runtime.attachments.media_attachments import (
    normalize_chat_media_attachments,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import unique_upload_path
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)

logger = logging.getLogger(__name__)

#: 文件枚举（path.select_files）排除的隐藏前缀
_HIDDEN_PREFIXES: Final[tuple[str, ...]] = (".", "_")
#: 文件枚举最多返回的条目数（防御性上限）
_SELECT_FILES_LIMIT: Final[int] = 200

_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif"}
)
#: ``path.select_files`` 的图片会经 E2A 返回 base64。保持原始内容不超过 4MB，
#: 避免 4/3 膨胀后超过内部 WebSocket 的 8MB 帧上限。
_SELECT_IMAGE_MAX_BYTES: Final[int] = 4 * 1024 * 1024
# Keep base64 + E2A envelope well below the internal 8 MiB frame limit.
_VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES: Final[int] = 512 * 1024


def _parse_optional_str(params: dict[str, Any], key: str) -> str | None:
    raw = params.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be string")
    return raw.strip() or None


def _resolve_within_workspace(raw: str | None, base: Path) -> Path | None:
    """把 initial_dir 解析为注入 workspace 根内的绝对路径（决策 D3 边界）。

    - 空值 → 返回 workspace 根；
    - 绝对路径 → 展开 ``~`` 后必须位于 base 内（commonpath 校验），否则视为越界；
    - 相对路径 → 拼接在 base 下。
    """
    if not raw:
        return base
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            candidate.resolve(strict=False)
        except OSError:
            pass
        base_resolved = base.resolve(strict=False)
        try:
            candidate_resolved = candidate.resolve(strict=False)
        except OSError:
            candidate_resolved = candidate
        if candidate_resolved != base_resolved and base_resolved not in candidate_resolved.parents:
            return None
        return candidate_resolved
    return (base / candidate).resolve(strict=False)


class WorkspaceFileAdapter(GatewayAdapter):
    """工作区与文件域适配器：media/document/path/upload/file_import/im_file_persist。"""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.MEDIA_PERSIST.value,
            ReqMethod.DOCUMENT_PERSIST.value,
            ReqMethod.DOCUMENT_FORMATS.value,
            ReqMethod.PATH_SELECT_DIRECTORY.value,
            ReqMethod.PATH_SELECT_FILES.value,
            ReqMethod.FILE_IMPORT_URL.value,
            ReqMethod.FILE_UPLOAD_CHUNK.value,
            ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK.value,
            ReqMethod.IM_FILE_PERSIST.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method
        if method == ReqMethod.MEDIA_PERSIST:
            return await self._handle_media_persist(request)
        if method == ReqMethod.DOCUMENT_PERSIST:
            return await self._handle_document_persist(request)
        if method == ReqMethod.DOCUMENT_FORMATS:
            return await self._handle_document_formats(request)
        if method == ReqMethod.PATH_SELECT_DIRECTORY:
            return await self._handle_select_directory(request)
        if method == ReqMethod.PATH_SELECT_FILES:
            return await self._handle_select_files(request)
        if method == ReqMethod.FILE_IMPORT_URL:
            return await self._handle_import_url(request)
        if method == ReqMethod.FILE_UPLOAD_CHUNK:
            return await self._handle_upload_chunk(request)
        if method == ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK:
            return await self._handle_verified_download_chunk(request)
        if method == ReqMethod.IM_FILE_PERSIST:
            return await self._handle_im_file_persist(request)
        return build_error_response(
            request,
            f"unsupported method: {method}",
            code="BAD_REQUEST",
        )

    async def _handle_media_persist(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        normalized = dict(params)
        try:
            # 落盘为小 IO（base64 图片 ≤10MB×≤8 张），但放线程池避免阻塞事件循环
            await asyncio.to_thread(
                normalize_chat_media_attachments,
                normalized,
                request.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[WorkspaceFileAdapter] media.persist failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        # 与 Web 原 handler 一致：只回传关心的 key
        payload: dict[str, Any] = {}
        for key in ("content", "query", "media_items", "files"):
            if key in normalized:
                payload[key] = normalized[key]
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_document_persist(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        normalized = dict(params)
        try:
            # 文档校验含逐条 path.is_file/stat 同步 IO，放线程池避免阻塞事件循环。
            await asyncio.to_thread(persist_and_parse_documents, normalized)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[WorkspaceFileAdapter] document.persist failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        payload: dict[str, Any] = {}
        for key in (
            "content",
            "query",
            "media_items",
            "files",
            "documents",
            "document_errors",
            "forbidden_formats",
        ):
            if key in normalized:
                payload[key] = normalized[key]
        if "forbidden_formats" not in payload:
            payload["forbidden_formats"] = forbidden_formats()
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_document_formats(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"forbidden_formats": forbidden_formats()},
            metadata=request.metadata,
        )

    async def _handle_select_directory(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        try:
            initial_dir = _parse_optional_str(params, "initial_dir")
        except ValueError as exc:
            return build_error_response(request, str(exc), code="BAD_REQUEST")

        base = get_agent_workspace_dir()
        if not initial_dir:
            # 无明确目标时不再把 workspace 根当作"已选中"返回（否则前端会直接
            # 用目录名创建项目、默认选中 jiuwen workspace，且跳过命名步骤）。
            # AgentOS/远程部署的容器内无法弹系统目录选择器，返回 UNSUPPORTED
            # 让前端回退到手动输入表单（项目名 + 绝对路径，见
            # frontend selectProjectDirectory 的 unsupported 分支）。
            return build_error_response(
                request,
                "system directory picker is unavailable in this environment; "
                "enter the project name and absolute path manually",
                code="UNSUPPORTED",
            )
        selected = _resolve_within_workspace(initial_dir, base)
        if selected is None:
            return build_error_response(
                request,
                "initial_dir must be inside the agent workspace",
                code="BAD_REQUEST",
            )
        if not selected.exists():
            # 注入目录尚未创建：返回 cancelled，前端回落（与对话框取消同形）
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"path": None, "cancelled": True},
                metadata=request.metadata,
            )
        if not selected.is_dir():
            return build_error_response(
                request, "path is not a directory", code="BAD_REQUEST"
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"path": str(selected), "cancelled": False},
            metadata=request.metadata,
        )

    async def _handle_select_files(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        try:
            initial_dir = _parse_optional_str(params, "initial_dir")
        except ValueError as exc:
            return build_error_response(request, str(exc), code="BAD_REQUEST")
        raw_multiple = params.get("allow_multiple", True)
        if not isinstance(raw_multiple, bool):
            return build_error_response(
                request, "allow_multiple must be boolean", code="BAD_REQUEST"
            )
        # allow_multiple 仅影响前端交互，枚举行为一致；保留参数校验契约

        base = get_agent_workspace_dir()
        selected_dir = _resolve_within_workspace(initial_dir, base)
        if selected_dir is None:
            return build_error_response(
                request,
                "initial_dir must be inside the agent workspace",
                code="BAD_REQUEST",
            )

        def _enumerate_files() -> list[dict[str, Any]]:
            if not selected_dir.is_dir():
                return []
            files: list[dict[str, Any]] = []
            try:
                entries = sorted(selected_dir.iterdir(), key=lambda p: p.name.lower())
            except OSError as exc:
                logger.warning("[WorkspaceFileAdapter] path.select_files list failed: %s", exc)
                return []
            for entry in entries:
                if not entry.is_file():
                    continue
                if entry.name.startswith(_HIDDEN_PREFIXES):
                    continue
                files.append(_describe_local_file(entry))
                if len(files) >= _SELECT_FILES_LIMIT:
                    break
            return files

        try:
            files = await asyncio.to_thread(_enumerate_files)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[WorkspaceFileAdapter] path.select_files failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"files": files, "cancelled": False},
            metadata=request.metadata,
        )

    async def _handle_im_file_persist(self, request: AgentRequest) -> AgentResponse:
        """IM 平台附件落盘（在 AgentServer 注入目录写入 <平台>_files/downloads）。

        Gateway 通过平台 API 下载附件字节后，经 base64 交给本 method 落盘到
        注入目录，返回带本地 ``path`` 的文件元数据供后续链路消费；Gateway
        不再直写 ``agent/workspace/<平台>_files/downloads/``（方案 §10.5 / 决策 D6）。
        """
        import base64 as _base64
        import re as _re

        params = request.params if isinstance(request.params, dict) else {}
        platform = str(params.get("platform") or "").strip().lower()
        if not platform or not _re.fullmatch(r"[a-z0-9_-]+", platform):
            return build_error_response(request, "platform is required", code="BAD_REQUEST")
        category = str(params.get("category") or "").strip().lower()
        if not category or not _re.fullmatch(r"[a-z0-9_-]+", category):
            return build_error_response(request, "category is required", code="BAD_REQUEST")
        filename = str(params.get("filename") or "").strip()
        if not filename:
            return build_error_response(request, "filename is required", code="BAD_REQUEST")
        raw_data = params.get("data")
        if not isinstance(raw_data, str) or not raw_data:
            return build_error_response(request, "data (base64) is required", code="BAD_REQUEST")

        try:
            content = _base64.b64decode(raw_data, validate=True)
        except Exception as exc:  # noqa: BLE001
            return build_error_response(request, f"invalid base64 data: {exc}", code="BAD_REQUEST")
        if not content:
            return build_error_response(request, "data (base64) is empty", code="BAD_REQUEST")

        workspace = get_agent_workspace_dir()
        base_dir = workspace / f"{platform}_files" / "downloads" / category
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            target = unique_upload_path(base_dir / filename)
            target.write_bytes(content)
        except OSError as exc:
            logger.exception("[WorkspaceFileAdapter] im.file_persist write failed: %s", exc)
            return build_error_response(request, f"failed to persist attachment: {exc}", code="INTERNAL_ERROR")

        mime_type, _ = mimetypes.guess_type(filename)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "path": str(target),
                "name": target.name,
                "filename": filename,
                "size": len(content),
                "mime_type": mime_type or "application/octet-stream",
                "platform": platform,
                "file_category": category,
            },
            metadata=request.metadata,
        )

    async def _handle_upload_chunk(self, request: AgentRequest) -> AgentResponse:
        """Append one Gateway-uploaded chunk to the current user directory.

        AgentOS routes every chunk through its existing E2A connection, so the
        destination is selected by the authenticated Gateway route rather than
        by a Gateway-local HTTP address.  ``user_id`` is intentionally not
        inspected here: this process's injected data directory is authoritative.
        """
        params = request.params if isinstance(request.params, dict) else {}
        upload_id = str(params.get("upload_id") or "").strip()
        rel_path = str(params.get("target_rel_path") or "").strip().replace("\\", "/")
        resolved_path = str(params.get("resolved_path") or "").strip()
        raw_chunk = params.get("data")
        is_final = bool(params.get("final", False))
        if not upload_id or not rel_path or not isinstance(raw_chunk, str):
            return build_error_response(request, "upload_id, target_rel_path and data are required", code="BAD_REQUEST")
        if rel_path.startswith("/") or ".." in rel_path.split("/"):
            return build_error_response(request, "invalid_target_path", code="BAD_REQUEST")
        try:
            chunk = base64.b64decode(raw_chunk, validate=True)
        except Exception as exc:  # noqa: BLE001
            return build_error_response(request, f"invalid base64 data: {exc}", code="BAD_REQUEST")
        if not chunk:
            return build_error_response(request, "data is empty", code="BAD_REQUEST")

        target = (
            Path(resolved_path).resolve(strict=False)
            if resolved_path
            else (get_user_workspace_dir() / rel_path).resolve(strict=False)
        )
        if not is_path_within_user_dirs(str(target)):
            return build_error_response(request, "path_outside_workspace", code="BAD_REQUEST")
        try:
            if not resolved_path:
                target = unique_upload_path(target)
            await asyncio.to_thread(_append_upload_chunk, target, chunk)
            size = target.stat().st_size
        except OSError as exc:
            logger.warning("[WorkspaceFileAdapter] file.upload_chunk write failed: %s", exc)
            return build_error_response(request, f"failed to persist upload: {exc}", code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"path": str(target), "size": size, "upload_id": upload_id, "final": is_final},
            metadata=request.metadata,
        )

    async def _handle_verified_download_chunk(
        self,
        request: AgentRequest,
    ) -> AgentResponse:
        """Validate and read one bounded Smart Approval sealed-asset chunk."""
        params = request.params if isinstance(request.params, dict) else {}
        token = str(params.get("token") or "").strip()
        try:
            offset = int(params.get("offset", 0))
            limit = int(params.get("limit", _VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES))
        except (TypeError, ValueError):
            return build_error_response(
                request,
                "offset and limit must be integers",
                code="BAD_REQUEST",
            )
        if not token or offset < 0 or not 0 < limit <= _VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES:
            return build_error_response(
                request,
                "invalid token, offset, or limit",
                code="BAD_REQUEST",
            )

        from jiuwenswarm.agents.harness.common.tools.web_file_download import (
            validate_file_download_token,
        )

        payload = validate_file_download_token(
            token,
            session_id=request.session_id,
        )
        if payload is None or payload.get("kind") != "verified_asset_v1":
            return build_error_response(
                request,
                "verified download token rejected",
                code="FORBIDDEN",
            )

        try:
            claimed_size = int(payload.get("size"))
        except (TypeError, ValueError):
            return build_error_response(
                request,
                "verified asset claims are invalid",
                code="FORBIDDEN",
            )
        if claimed_size < 0 or offset > claimed_size:
            return build_error_response(
                request,
                "verified asset range is invalid",
                code="BAD_REQUEST",
            )

        file_path = Path(str(payload.get("path") or ""))
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(file_path, flags, 0o400)
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != claimed_size:
                return build_error_response(
                    request,
                    "verified asset changed after authorization",
                    code="FORBIDDEN",
                )
            os.lseek(file_descriptor, offset, os.SEEK_SET)
            data = os.read(file_descriptor, min(limit, claimed_size - offset))
        except OSError:
            return build_error_response(
                request,
                "verified asset is unavailable",
                code="NOT_FOUND",
            )
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)

        name = Path(str(payload.get("name") or "download")).name or "download"
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "data_base64": base64.b64encode(data).decode("ascii"),
                "offset": offset,
                "chunk_size": len(data),
                "size": claimed_size,
                "eof": offset + len(data) >= claimed_size,
                "name": name,
                "mime_type": mime_type,
            },
            metadata=request.metadata,
        )

    async def _handle_import_url(self, request: AgentRequest) -> AgentResponse:
        """chat.send 上行外部 url 文件导入（AgentServer 下载落盘注入目录）。

        Gateway ``web_connect._process_files`` 不再下载/落盘，改为把 ``files``
        中的外部 ``url`` 列表经本 method 交给 AgentServer：AgentServer 在
        ``get_agent_workspace_dir()``（注入目录）下载落盘，返回带本地 ``path``
        的文件元数据，供后续 CHAT_SEND 链路消费。Gateway 全程不接触文件内容。
        """
        params = request.params if isinstance(request.params, dict) else {}
        raw_files = params.get("files")
        overwrite = bool(params.get("overwrite", False))
        if not isinstance(raw_files, list):
            return build_error_response(
                request, "files must be a list", code="BAD_REQUEST"
            )
        imported: list[dict[str, Any]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                imported.append(item)
                continue
            try:
                if overwrite:
                    result = await asyncio.to_thread(
                        _import_url_file, item, overwrite=True
                    )
                else:
                    # Keep the historical one-argument call for the default
                    # AgentOS behavior and existing extension hooks.
                    result = await asyncio.to_thread(_import_url_file, item)
                imported.append(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[WorkspaceFileAdapter] file.import_url item failed: %s", exc)
                imported.append({**item, "error": str(exc)})
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"files": imported},
            metadata=request.metadata,
        )


def _append_upload_chunk(target: Path, chunk: bytes) -> None:
    """Append a decoded E2A upload chunk without buffering the complete file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as handle:
        handle.write(chunk)


def _import_url_file(item: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    """下载单个外部 url 文件到注入 workspace 并返回带本地 path 的元数据。"""
    import mimetypes as _mimetypes
    import urllib.request

    file_url = str(item.get("url") or item.get("uri") or "").strip()
    if not file_url:
        raise ValueError("missing url for file import")
    fallback_name = "download"
    try:
        fallback_name = Path(file_url.split("?")[0]).name or "download"
    except (ValueError, IndexError):
        pass
    filename = str(
        item.get("name") or item.get("filename") or fallback_name
    ).strip() or "download"

    workspace_dir = get_agent_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    target = workspace_dir / filename if overwrite else unique_upload_path(workspace_dir / filename)

    req = urllib.request.Request(file_url, headers={"User-Agent": "jiuwenswarm-agent"})
    with urllib.request.urlopen(req, timeout=60) as upstream:  # noqa: S310
        with open(target, "wb") as out:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                out.write(chunk)
    size = 0
    try:
        size = target.stat().st_size
    except OSError:
        pass
    mime_type, _ = _mimetypes.guess_type(target.name)
    return {
        **item,
        "path": str(target),
        "filename": target.name,
        "size": size,
        "mime_type": mime_type or "application/octet-stream",
    }


def _describe_local_file(path: Path) -> dict[str, Any]:
    """与桌面端 select_local_files 同形的文件元数据。

    - image 文件附带 ``base64`` 内容（≤4MB，``_SELECT_IMAGE_MAX_BYTES``），
      供前端 ``InputArea`` 预览与上传（前端硬判 ``kind=image && base64``）；
    - document 只返回路径元数据（内容经 CHAT_SEND 链路按 ``@path`` 引用）。
    """
    filename = path.name
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type:
        mime_type = "application/octet-stream"
    suffix = path.suffix.lower()
    kind = "image" if suffix in _IMAGE_EXTENSIONS else "document"
    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass
    info: dict[str, Any] = {
        "path": str(path),
        "filename": filename,
        "size": size,
        "mime_type": mime_type,
        "kind": kind,
    }
    if kind == "image" and 0 < size <= _SELECT_IMAGE_MAX_BYTES:
        try:
            info["base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning(
                "[WorkspaceFileAdapter] select_files 读取图片失败: %s error=%s",
                path,
                exc,
            )
    return info
