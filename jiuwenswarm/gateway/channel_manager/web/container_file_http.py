# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway WebChannel ``/file-api`` HTTP routes (agentos_router + dual_protocol)."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
from typing import TYPE_CHECKING, Annotated, Any, Mapping
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

FILE_API_PREFIX = "/file-api"
MAX_UPLOAD_COUNT = 20
_STREAM_CHUNK = 65536
_VERIFIED_ASSET_TOKEN_KIND = "verified_asset_v1"


class RawFileQuery(BaseModel):
    """Query bundle for ``GET /file-api/raw-file`` (G.FNM.03)."""

    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(..., min_length=1)
    session_id: str = ""
    user_id: str | None = None
    agent_type: str | None = None
    offset: int | None = None
    limit: int | None = None
    # HTTP query name remains ``format``; avoid shadowing builtin ``format``.
    response_format: str = Field(default="binary", alias="format")


class FileContentGetQuery(BaseModel):
    """Query bundle for ``GET /file-api/file-content`` (G.FNM.03)."""

    path: str = Field(..., min_length=1)
    session_id: str = ""
    user_id: str | None = None
    agent_type: str | None = None
    encoding: str = "utf-8"


class ListFilesQuery(BaseModel):
    """Query bundle for ``GET /file-api/list-files`` and ``/list-markdown``."""

    model_config = ConfigDict(populate_by_name=True)

    # HTTP query name remains ``dir``; avoid shadowing builtin ``dir``.
    dir_path: str = Field(default="", alias="dir")
    session_id: str = ""
    user_id: str | None = None
    agent_type: str | None = None
    recursive: bool = False
    max_depth: int = 0


class UploadForm(BaseModel):
    """Form bundle for ``POST /file-api/upload`` (G.FNM.03)."""

    model_config = ConfigDict(populate_by_name=True)

    # HTTP form name remains ``dir``; avoid shadowing builtin ``dir``.
    dir_prefix: str = Field(default="", alias="dir")
    session_id: str = ""
    user_id: str | None = None
    agent_type: str | None = None


class MkdirQuery(BaseModel):
    """Query bundle for ``POST /file-api/mkdir``."""

    path: str = ""
    session_id: str = ""
    user_id: str | None = None
    agent_type: str | None = None
    mode: str | None = None
    recursive: bool = False


def _status_for_transfer_code(code: str) -> int:
    normalized = str(code or "").strip().upper()
    if normalized in {
        "BAD_REQUEST",
        "INVALID_PATH",
        "MISSING_FILE_PATH",
        "MISSING_SESSION_ID",
        "MISSING_FILE_CONTENT",
        "ONLY_MARKDOWN_SUPPORTED",
        "FORBIDDEN_DIR",
    }:
        return 400
    if normalized in {"UNAUTHORIZED", "AUTH_REQUIRED"}:
        return 401
    if normalized in {"FORBIDDEN", "FORBIDDEN_EXTENSION", "FORBIDDEN_PATH"}:
        return 403
    if normalized in {"NOT_FOUND", "INSTANCE_NOT_FOUND", "FILE_NOT_FOUND"}:
        return 404
    if normalized in {"PAYLOAD_TOO_LARGE", "FILE_TOO_LARGE"}:
        return 413
    if normalized in {"LIST_NOT_SUPPORTED", "NOT_IMPLEMENTED"}:
        return 501
    return 502


def _error_json(*, error: str, code: str | None = None, status_code: int | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": error}
    if code:
        payload["code"] = code
    status = status_code if status_code is not None else _status_for_transfer_code(code or "INTERNAL_ERROR")
    return JSONResponse(status_code=status, content=payload)


def _auth_headers_from_request(request: Request) -> dict[str, str]:
    from jiuwenswarm.extensions.agentos.auth.common import (
        extract_token_from_path_and_headers,
        headers_to_dict,
    )
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
        build_auth_headers_from_mapping,
        build_auth_headers_from_token,
    )

    header_map = headers_to_dict(request.headers)
    mapped = build_auth_headers_from_mapping(header_map)
    if mapped:
        return mapped
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return build_auth_headers_from_token(
        extract_token_from_path_and_headers(path, header_map),
    )


def _resolve_user_id(request: Request, explicit: str | None = None) -> str:
    if explicit and str(explicit).strip():
        uid = str(explicit).strip()
    else:
        header_uid = request.headers.get("x-user-id") or request.headers.get("X-User-Id")
        uid = str(header_uid or "").strip()
    if uid:
        request.state.agentos_user_id = uid
    return uid


def _bind_file_api_session(request: Request, session_id: str) -> None:
    sid = str(session_id or "").strip()
    if sid:
        request.state.agentos_session_id = sid


def _file_api_ids(request: Request) -> tuple[str, str]:
    uid = str(getattr(request.state, "agentos_user_id", "") or "").strip()
    if not uid:
        uid = str(request.query_params.get("user_id") or "").strip()
    if not uid:
        uid = str(request.headers.get("x-user-id") or request.headers.get("X-User-Id") or "").strip()
    sid = str(getattr(request.state, "agentos_session_id", "") or "").strip()
    if not sid:
        sid = str(request.query_params.get("session_id") or "").strip()
    return uid, sid


_FILE_API_DONE_KEYS = (
    "user_id",
    "session_id",
    "channel",
    "method",
    "path",
    "status",
    "latency_ms",
    "error",
    "uid_empty",
)


def _format_file_api_done(**fields: Any) -> str:
    parts = ["[WebChannel] file-api.done"]
    for key in _FILE_API_DONE_KEYS:
        if key not in fields:
            continue
        value = fields[key]
        if value in (None, ""):
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _log_file_api_done(
    request: Request,
    *,
    status: int,
    latency_ms: int,
    error: str = "",
) -> None:
    uid, sid = _file_api_ids(request)
    fields: dict[str, Any] = {
        "user_id": uid,
        "session_id": sid,
        "channel": "file-api",
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "latency_ms": latency_ms,
    }
    if not uid:
        fields["uid_empty"] = "yes"
    if error:
        fields["error"] = error
    level = logging.INFO if status < 400 else logging.WARNING
    logger.log(
        level,
        _format_file_api_done(**fields),
        extra={"session_id": sid} if sid else {},
    )


def _is_markdown_name(name: str) -> bool:
    lower = str(name or "").strip().lower()
    return lower.endswith(".md") or lower.endswith(".mdx")


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def map_container_list_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    """Map a YuanRong list entry to Gateway ``list-files`` shape."""
    name = str(item.get("name") or "")
    path = str(item.get("path") or "")
    is_directory = _truthy_bool(item.get("is_directory", item.get("isDirectory")))
    size_raw = item.get("size")
    try:
        size = int(size_raw) if size_raw is not None else 0
    except (TypeError, ValueError):
        size = 0
    modified = item.get("modified_time")
    if modified is None:
        modified = item.get("modifiedTime")
    file_type = item.get("type")
    return {
        "name": name,
        "path": path,
        "isDirectory": is_directory,
        "isMarkdown": (not is_directory) and _is_markdown_name(name),
        "size": size,
        "modifiedTime": modified,
        "type": file_type,
    }


def sort_container_list_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Directories first, then case-insensitive name (host ``/file-api/list-files``)."""
    return sorted(
        entries,
        key=lambda item: (not bool(item.get("isDirectory")), str(item.get("name") or "").lower()),
    )


def map_container_list_files(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    mapped = [map_container_list_entry(item) for item in items]
    return sort_container_list_entries(mapped)


def map_container_list_markdown(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for item in map_container_list_files(items):
        if item.get("isDirectory") or not item.get("isMarkdown"):
            continue
        files.append(
            {
                "name": item["name"],
                "path": item["path"],
                "size": item["size"],
                "modifiedTime": item.get("modifiedTime"),
            }
        )
    return files


def _decode_text(raw: bytes, encoding: str) -> tuple[str, str]:
    enc = (encoding or "utf-8").strip().lower() or "utf-8"
    if enc != "auto":
        return raw.decode(enc), enc
    try:
        import charset_normalizer

        detected = charset_normalizer.from_bytes(raw).best()
        detected_encoding = (detected.encoding if detected is not None else None) or "utf-8"
        try:
            return raw.decode(detected_encoding), detected_encoding
        except (UnicodeDecodeError, LookupError):
            pass
    except Exception:  # noqa: BLE001
        pass
    for try_encoding in ("utf-8", "gbk", "gb2312", "big5", "shift_jis", "euc_kr"):
        try:
            return raw.decode(try_encoding), try_encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise OSError("Unable to decode file with any known encoding")


def _decode_download_token_payload(token: str) -> dict[str, Any] | None:
    """Decode routing hints only; authorization remains in AgentServer."""
    try:
        encoded = token.split(".", 1)[0]
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def _decode_download_token_location(token: str) -> tuple[str, str] | None:
    """Read the routing fields from a signed AgentServer download token.

    AgentOS Gateway and the user's AgentServer do not necessarily share the
    download-token secret, so the Gateway cannot validate this HMAC.  The
    request's AgentOS credentials establish the user identity; this helper
    only obtains the container path and session used to select that user's
    runtime.  The token is deliberately never treated as an AgentOS bearer
    credential.
    """
    payload = _decode_download_token_payload(token)
    if payload is None:
        return None
    path = payload.get("path")
    session_id = payload.get("sid")
    if not isinstance(path, str) or not path.strip() or not isinstance(session_id, str):
        return None
    return path.strip(), session_id.strip()


def _parse_single_byte_range(
    range_header: str,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse one HTTP byte range without accepting multipart ranges."""
    if file_size <= 0 or not range_header.startswith("bytes=") or "," in range_header:
        return None
    value = range_header[6:]
    if "-" not in value:
        return None
    start_text, end_text = value.split("-", 1)
    if not start_text:
        if not end_text.isdecimal() or int(end_text) <= 0:
            return None
        return max(0, file_size - int(end_text)), file_size - 1
    if not start_text.isdecimal() or (end_text and not end_text.isdecimal()):
        return None
    start = int(start_text)
    if start >= file_size:
        return None
    end = min(int(end_text), file_size - 1) if end_text else file_size - 1
    return (start, end) if end >= start else None


def _decode_verified_chunk(
    payload: Mapping[str, Any],
    *,
    expected_offset: int,
    expected_limit: int,
) -> tuple[bytes, int, str, str, bool] | None:
    """Validate one E2A chunk envelope before exposing it over HTTP."""
    try:
        data = base64.b64decode(str(payload.get("data_base64") or ""), validate=True)
        offset = int(payload.get("offset"))
        chunk_size = int(payload.get("chunk_size"))
        total_size = int(payload.get("size"))
    except (TypeError, ValueError):
        return None
    if offset != expected_offset:
        return None
    if chunk_size != len(data):
        return None
    if len(data) > expected_limit:
        return None
    if total_size < 0:
        return None
    if offset < 0:
        return None
    if offset + len(data) > total_size:
        return None
    name = str(payload.get("name") or "download").rsplit("/", 1)[-1]
    mime_type = str(payload.get("mime_type") or "application/octet-stream")
    return data, total_size, name or "download", mime_type, bool(payload.get("eof"))


def attach_container_file_routes(app: FastAPI, channel: WebChannel) -> None:
    """Mount ``/file-api`` when channel has an AgentOSRouterClient backend."""
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
        AgentOSFileTransferError,
        AgentOSRouterClient,
        build_auth_headers_from_mapping,
    )
    from jiuwenswarm.extensions.agentos.auth.common import headers_to_dict

    client = getattr(channel, "container_file_client", None)
    if not isinstance(client, AgentOSRouterClient):
        return

    prefix = FILE_API_PREFIX

    @app.middleware("http")
    async def _file_api_access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not str(request.url.path or "").startswith(prefix):
            return await call_next(request)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            _log_file_api_done(
                request,
                status=500,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=type(exc).__name__,
            )
            raise
        _log_file_api_done(
            request,
            status=int(getattr(response, "status_code", 500) or 500),
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        return response

    async def _list_container_dir(
        request: Request,
        params: ListFilesQuery,
        *,
        markdown_only: bool,
    ) -> JSONResponse:
        uid = _resolve_user_id(request, params.user_id)
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)
        dir_arg = str(params.dir_path or "").strip()
        if not dir_arg:
            return _error_json(error="missing_dir", code="BAD_REQUEST", status_code=400)
        if int(params.max_depth) < 0:
            return _error_json(error="max_depth must be >= 0", code="BAD_REQUEST", status_code=400)

        sid = str(params.session_id or "").strip()
        _bind_file_api_session(request, sid)
        agent_type = params.agent_type if isinstance(params.agent_type, str) else None
        try:
            items = await client.list_container_files(
                user_id=uid,
                dir_path=dir_arg,
                recursive=bool(params.recursive),
                max_depth=int(params.max_depth),
                agent_type=agent_type,
                session_id=sid,
                auth_headers=_auth_headers_from_request(request),
            )
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/list] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)

        if markdown_only:
            return JSONResponse({"files": map_container_list_markdown(items)})
        return JSONResponse({"files": map_container_list_files(items)})

    @app.get(f"{prefix}/list-files")
    async def list_files_get(
        request: Request,
        params: Annotated[ListFilesQuery, Query()],
    ) -> JSONResponse:
        return await _list_container_dir(request, params, markdown_only=False)

    @app.get(f"{prefix}/list-markdown")
    async def list_markdown_get(
        request: Request,
        params: Annotated[ListFilesQuery, Query()],
    ) -> JSONResponse:
        return await _list_container_dir(request, params, markdown_only=True)

    @app.get(f"{prefix}/raw-file")
    async def raw_file_get(
        request: Request,
        params: Annotated[RawFileQuery, Query()],
    ) -> Response:
        uid = _resolve_user_id(request, params.user_id)
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)
        sid = str(params.session_id or "").strip()
        _bind_file_api_session(request, sid)
        path = params.path.strip()
        agent_type = params.agent_type if isinstance(params.agent_type, str) else None

        auth = _auth_headers_from_request(request)
        want_json = str(params.response_format or "").strip().lower() == "json"

        async def _one_chunk(off: int, lim: int):
            return await client.download_container_file(
                user_id=uid,
                path=path,
                offset=off,
                limit=lim,
                agent_type=agent_type,
                session_id=sid,
                auth_headers=auth,
            )

        try:
            if want_json or params.offset is not None or params.limit is not None:
                off = max(0, int(params.offset or 0))
                lim = int(params.limit if params.limit is not None else _STREAM_CHUNK)
                if lim <= 0:
                    return _error_json(error="invalid_offset_or_limit", code="BAD_REQUEST", status_code=400)
                chunk = await _one_chunk(off, lim)
                if want_json:
                    return JSONResponse(
                        {
                            "path": chunk.path,
                            "size": chunk.size,
                            "offset": chunk.offset,
                            "chunk_size": chunk.chunk_size,
                            "data_base64": base64.b64encode(chunk.data).decode("ascii"),
                            "eof": chunk.eof,
                            "content_type": chunk.content_type,
                        }
                    )
                return Response(
                    content=chunk.data,
                    media_type=chunk.content_type or "application/octet-stream",
                    headers={"Cache-Control": "no-store"},
                )

            # Peek first chunk for Content-Type, then stream remaining chunks.
            first = await _one_chunk(0, _STREAM_CHUNK)

            async def _stream_with_first():
                if first.data:
                    yield first.data
                if first.eof:
                    return
                off = first.offset + first.chunk_size
                while True:
                    chunk = await _one_chunk(off, _STREAM_CHUNK)
                    if chunk.data:
                        yield chunk.data
                    if chunk.eof:
                        break
                    off = chunk.offset + chunk.chunk_size

            return StreamingResponse(
                _stream_with_first(),
                media_type=first.content_type or "application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/raw-file] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)

    @app.api_route(f"{prefix}/download", methods=["GET", "HEAD"])
    async def download_sent_file(request: Request) -> Response:
        """Serve a ``send_file_to_user`` attachment from its owner's container.

        Browser file cards only have the opaque download token, unlike the
        file-browser APIs which receive an explicit path and user id.  In an
        AgentOS deployment this WebChannel port is the public HTTP entrypoint,
        hence it must translate that token to the container-file router call.
        """
        token = str(request.query_params.get("token") or "").strip()
        token_payload = _decode_download_token_payload(token)
        if token_payload is None:
            return _error_json(error="invalid_download_token", code="BAD_REQUEST", status_code=400)
        uid = _resolve_user_id(request, request.query_params.get("user_id"))
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)

        if token_payload.get("kind") == _VERIFIED_ASSET_TOKEN_KIND:
            from jiuwenswarm.common.schema.message import ReqMethod
            from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary
            from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import (
                _VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES,
            )

            session_id_value = token_payload.get("sid")
            if not isinstance(session_id_value, str):
                return _error_json(
                    error="invalid_download_token",
                    code="BAD_REQUEST",
                    status_code=400,
                )
            agent_client = getattr(channel, "agent_client", None)

            async def _verified_chunk(
                offset: int,
                limit: int,
            ) -> tuple[bytes, int, str, str, bool] | JSONResponse:
                ok, result = await fetch_agent_unary(
                    agent_client=agent_client,
                    req_method=ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK,
                    params={"token": token, "offset": offset, "limit": limit},
                    session_id=session_id_value,
                    user_id=uid,
                    channel_id=channel.channel_id,
                    label="file.download_verified_chunk",
                )
                if not ok:
                    return _error_json(
                        error=str(result.get("error") or "verified download failed"),
                        code=str(result.get("code") or "BAD_REQUEST"),
                    )
                decoded = _decode_verified_chunk(
                    result,
                    expected_offset=offset,
                    expected_limit=limit,
                )
                if decoded is None:
                    return _error_json(
                        error="invalid verified download response",
                        code="INTERNAL_ERROR",
                        status_code=502,
                    )
                return decoded

            first_result = await _verified_chunk(0, 1)
            if isinstance(first_result, JSONResponse):
                return first_result
            _, total_size, file_name, mime_type, _ = first_result
            range_header = str(request.headers.get("Range") or "").strip()
            byte_range = (
                _parse_single_byte_range(range_header, total_size)
                if range_header
                else None
            )
            if range_header and byte_range is None:
                return Response(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{total_size}",
                        "Content-Length": "0",
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "no-store",
                    },
                )

            start, end = byte_range or (0, max(total_size - 1, 0))
            content_length = 0 if total_size == 0 else end - start + 1
            inline = str(request.query_params.get("inline") or "").strip().lower() in {
                "1",
                "true",
            }
            disposition = "inline" if inline else "attachment"
            headers = {
                "Cache-Control": "no-store",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Disposition": (
                    f"{disposition}; filename*=UTF-8''{quote(file_name, safe='')}"
                ),
            }
            status_code = 206 if byte_range is not None else 200
            if byte_range is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
            if request.method == "HEAD" or total_size == 0:
                return Response(
                    status_code=status_code,
                    media_type=mime_type,
                    headers=headers,
                )

            async def _stream_verified_download():
                offset = start
                while offset <= end:
                    limit = min(
                        _VERIFIED_DOWNLOAD_CHUNK_MAX_BYTES,
                        end - offset + 1,
                    )
                    result = await _verified_chunk(offset, limit)
                    if isinstance(result, JSONResponse):
                        raise RuntimeError("verified download authorization expired")
                    data, size, name, content_type, _ = result
                    if size != total_size:
                        raise RuntimeError("verified download asset changed")
                    if name != file_name:
                        raise RuntimeError("verified download asset changed")
                    if content_type != mime_type:
                        raise RuntimeError("verified download asset changed")
                    if not data:
                        raise RuntimeError("verified download asset changed")
                    yield data
                    offset += len(data)

            return StreamingResponse(
                _stream_verified_download(),
                status_code=status_code,
                media_type=mime_type,
                headers=headers,
            )

        location = _decode_download_token_location(token)
        if location is None:
            return _error_json(error="invalid_download_token", code="BAD_REQUEST", status_code=400)

        file_path, session_id = location
        # Do not use _auth_headers_from_request here: when no Authorization
        # header is present it would promote the *file* token to Bearer auth.
        auth = build_auth_headers_from_mapping(headers_to_dict(request.headers))
        try:
            first = await client.download_container_file(
                user_id=uid,
                path=file_path,
                offset=0,
                limit=_STREAM_CHUNK,
                session_id=session_id,
                auth_headers=auth,
            )
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/download] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)

        file_name = file_path.rsplit("/", 1)[-1] or "download"
        mime_type = first.content_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name, safe='')}",
        }
        if first.size >= 0:
            headers["Content-Length"] = str(first.size)
        if request.method == "HEAD":
            return Response(status_code=200, media_type=mime_type, headers=headers)

        async def _stream_download():
            if first.data:
                yield first.data
            if first.eof:
                return
            offset = first.offset + first.chunk_size
            while True:
                chunk = await client.download_container_file(
                    user_id=uid,
                    path=file_path,
                    offset=offset,
                    limit=_STREAM_CHUNK,
                    session_id=session_id,
                    auth_headers=auth,
                )
                if chunk.data:
                    yield chunk.data
                if chunk.eof:
                    return
                offset = chunk.offset + chunk.chunk_size

        return StreamingResponse(_stream_download(), media_type=mime_type, headers=headers)

    @app.get(f"{prefix}/file-content")
    async def file_content_get(
        request: Request,
        params: Annotated[FileContentGetQuery, Query()],
    ) -> Response:
        uid = _resolve_user_id(request, params.user_id)
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)
        sid = str(params.session_id or "").strip()
        _bind_file_api_session(request, sid)
        path = params.path.strip()
        agent_type = params.agent_type if isinstance(params.agent_type, str) else None

        auth = _auth_headers_from_request(request)
        try:
            parts: list[bytes] = []
            off = 0
            while True:
                chunk = await client.download_container_file(
                    user_id=uid,
                    path=path,
                    offset=off,
                    limit=_STREAM_CHUNK,
                    agent_type=agent_type,
                    session_id=sid,
                    auth_headers=auth,
                )
                parts.append(chunk.data)
                if chunk.eof:
                    break
                off = chunk.offset + chunk.chunk_size
            raw = b"".join(parts)
            text, used = _decode_text(raw, params.encoding)
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except OSError as exc:
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/file-content GET] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)

        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"X-Original-Encoding": used, "Cache-Control": "no-store"},
        )

    @app.post(f"{prefix}/upload")
    async def upload_multipart(request: Request) -> JSONResponse:
        form_data = await request.form()
        form = UploadForm.model_validate(
            {
                "dir": str(form_data.get("dir") or ""),
                "session_id": str(form_data.get("session_id") or ""),
                "user_id": (
                    str(form_data.get("user_id")).strip()
                    if form_data.get("user_id") not in (None, "")
                    else None
                ),
                "agent_type": (
                    str(form_data.get("agent_type")).strip()
                    if form_data.get("agent_type") not in (None, "")
                    else None
                ),
            }
        )
        sid = str(form.session_id or "").strip()
        _bind_file_api_session(request, sid)
        uid = _resolve_user_id(request, form.user_id)
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)
        upload_dir = str(form.dir_prefix or "").strip()

        uploads = [
            item
            for item in form_data.getlist("file")
            if hasattr(item, "read") and hasattr(item, "filename")
        ]
        if not uploads:
            return _error_json(error="missing_file", code="BAD_REQUEST", status_code=400)
        if len(uploads) > MAX_UPLOAD_COUNT:
            return _error_json(
                error=f"too many files (max {MAX_UPLOAD_COUNT})",
                code="BAD_REQUEST",
                status_code=400,
            )

        auth = _auth_headers_from_request(request)
        ok_files: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for item in uploads:
            filename = str(item.filename or "upload.bin")
            try:
                content = await item.read()
                result = await client.upload_container_file(
                    user_id=uid,
                    path=filename,
                    dir_prefix=upload_dir,
                    content=content,
                    agent_type=form.agent_type if isinstance(form.agent_type, str) else None,
                    session_id=sid,
                    auth_headers=auth,
                )
                out_path = str(result.get("path") or "")
                mime, _ = mimetypes.guess_type(filename)
                ok_files.append(
                    {
                        "filename": filename,
                        "path": out_path,
                        "mime_type": mime or "application/octet-stream",
                        "size_bytes": int(result.get("size") or len(content)),
                    }
                )
            except AgentOSFileTransferError as exc:
                errors.append({"filename": filename, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("[file-api/upload] failed for %s: %s", filename, exc)
                errors.append({"filename": filename, "error": str(exc)})

        return JSONResponse({"ok": True, "files": ok_files, "errors": errors})

    @app.post(f"{prefix}/file-content")
    async def file_content_post(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error_json(error="invalid_json", code="BAD_REQUEST", status_code=400)
        if not isinstance(body, dict):
            return _error_json(error="invalid_json", code="BAD_REQUEST", status_code=400)

        request_path = body.get("path")
        if not isinstance(request_path, str) or not request_path.strip():
            return _error_json(error="missing_file_path", code="BAD_REQUEST", status_code=400)
        content = body.get("content")
        if not isinstance(content, str):
            return _error_json(error="missing_file_content", code="BAD_REQUEST", status_code=400)

        lower = request_path.strip().lower()
        if not (lower.endswith(".md") or lower.endswith(".mdx")):
            return _error_json(error="only_markdown_supported", code="BAD_REQUEST", status_code=400)

        sid = str(body.get("session_id") or "").strip()
        _bind_file_api_session(request, sid)
        uid = _resolve_user_id(
            request,
            body.get("user_id") if isinstance(body.get("user_id"), str) else None,
        )
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)

        agent_type = body.get("agent_type")
        try:
            await client.upload_container_file(
                user_id=uid,
                path=request_path.strip(),
                content=content.encode("utf-8"),
                agent_type=agent_type if isinstance(agent_type, str) else None,
                session_id=sid,
                auth_headers=_auth_headers_from_request(request),
            )
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/file-content POST] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)
        return JSONResponse({"ok": True})

    @app.post(f"{prefix}/mkdir")
    async def mkdir_dir(
        request: Request,
        params: Annotated[MkdirQuery, Query()],
    ) -> JSONResponse:
        uid = _resolve_user_id(request, params.user_id)
        if not uid:
            return _error_json(error="user_id is required", code="BAD_REQUEST", status_code=400)
        path = str(params.path or "").strip()
        if not path:
            return _error_json(error="missing_path", code="BAD_REQUEST", status_code=400)

        sid = str(params.session_id or "").strip()
        _bind_file_api_session(request, sid)
        agent_type = params.agent_type if isinstance(params.agent_type, str) else None
        mode = str(params.mode).strip() if params.mode is not None else None
        try:
            result = await client.mkdir_container_dir(
                user_id=uid,
                path=path,
                mode=mode or None,
                recursive=bool(params.recursive),
                agent_type=agent_type,
                session_id=sid,
                auth_headers=_auth_headers_from_request(request),
            )
        except AgentOSFileTransferError as exc:
            return _error_json(error=str(exc), code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[file-api/mkdir] failed: %s", exc)
            return _error_json(error=str(exc), code="INTERNAL_ERROR", status_code=500)
        created = result.get("created")
        if created is None:
            created = True
        return JSONResponse(
            {
                "success": True,
                "path": str(result.get("path") or path),
                "created": bool(created),
            }
        )

    logger.info(
        "WebChannel /file-api enabled: upload, raw-file, file-content, list-files, list-markdown, mkdir",
    )
