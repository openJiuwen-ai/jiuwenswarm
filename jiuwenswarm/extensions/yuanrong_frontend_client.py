# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""YuanrongFrontendAgentClient - openYuanRong Frontend HTTP 客户端.

通过 HTTP POST 调用 openYuanRong Frontend 的函数 invocation 接口。
另经 POST/DELETE /api/agent 管理常驻 agent 实例（create_sandbox / delete_sandbox）。
保留无 service_id 设计，使用 session_id 进行并发控制。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, TypedDict

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk, AgentRequest


logger = logging.getLogger(__name__)


class AgentMount(TypedDict, total=False):
    """Bind mount for POST /api/agent ``mounts``."""

    source: str
    target: str
    readonly: bool


class AgentRootfsSpec(TypedDict, total=False):
    """Inline ``runtime_spec.rootfs`` for POST /api/agent."""

    imageurl: str
    user: str
    ports: list[str]


class AgentRuntimeSpec(TypedDict, total=False):
    """Inline ``runtime_spec`` for POST /api/agent (bypass meta_service)."""

    runtime: str
    sandbox_type: str
    rootfs: AgentRootfsSpec
    cpu: int
    memory: int
    code_path: str
    cmds: list[list[str]]


@dataclass
class SandboxInfo:
    """YuanRong agent instance lifecycle record returned by /api/agent."""

    sandbox_id: str
    status: str = "ready"
    metadata: dict[str, Any] = field(default_factory=dict)


class YuanrongAgentApiError(RuntimeError):
    """Raised when YuanRong /api/agent returns a non-success response."""


@dataclass(frozen=True)
class AgentFileDownloadChunk:
    """One chunk from GET /api/agent/:instanceId/files/download."""

    data: bytes
    path: str
    offset: int
    chunk_size: int
    size: int
    content_type: str
    eof: bool


class YuanrongAgentFileError(RuntimeError):
    """Raised when YuanRong agent file upload/download fails."""

    def __init__(self, message: str, *, http_status: int = 500, error_code: str = "INTERNAL_ERROR") -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.error_code = str(error_code)


class YuanrongFrontendAgentClient(AgentServerClient):
    """openYuanRong Frontend HTTP 客户端.

    通过 HTTP POST 调用 openYuanRong frontend 的函数 invocation 接口。
    使用 session_id 进行并发控制，不使用 service_id/agent_id。
    另提供 create_sandbox / delete_sandbox，经 /api/agent 管理常驻 agent 实例。
    """

    def __init__(
        self,
        *,
        frontend_endpoint: str,
        function_version_urn: str,
        concurrency: int = 1,
        invoke_timeout_s: float = 60.0,
        agent_timeout_s: float = 300.0,
        agent_namespace: str = "default",
        session_ttl_s: int = 900,
    ) -> None:
        self._frontend_endpoint = (frontend_endpoint or "").rstrip("/")
        self._function_version_urn = (function_version_urn or "").strip()
        self._concurrency = max(int(concurrency), 1)
        self._invoke_timeout_s = float(invoke_timeout_s)
        self._agent_timeout_s = float(agent_timeout_s)
        self._agent_namespace = str(agent_namespace or "default").strip() or "default"
        # yuanrong X-Instance-Session.sessionTTL，单位：秒；0 = 立即解绑。
        # 默认 900s（15 分钟），保证会话对实例的亲和性，避免每次调用重建实例。
        self._session_ttl_s = max(int(session_ttl_s), 0)
        self._connected = False
        self._server_ready = False

    @property
    def function_version_urn(self) -> str:
        return self._function_version_urn

    @property
    def agent_namespace(self) -> str:
        return self._agent_namespace

    @property
    def frontend_endpoint(self) -> str:
        return self._frontend_endpoint

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    @property
    def server_ready(self) -> bool:
        return self._server_ready

    async def connect(self, uri: str) -> None:
        endpoint = (uri or "").strip()
        if endpoint and endpoint.lower().startswith(("http://", "https://")):
            self._frontend_endpoint = endpoint.rstrip("/")
        if not self._frontend_endpoint:
            raise ValueError("frontend_endpoint cannot be empty")
        if not self._function_version_urn:
            raise ValueError("function_version_urn cannot be empty")
        self._connected = True
        self._server_ready = True
        logger.info(
            "[YuanrontFrontendAgentClient] connected: endpoint=%s",
            self._frontend_endpoint,
        )

    async def disconnect(self) -> None:
        self._connected = False
        self._server_ready = False
        logger.info("[YuanrongFrontendAgentClient] disconnected")

    @staticmethod
    def _normalize_runtime_spec(
        runtime_spec: AgentRuntimeSpec | Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Normalize inline ``runtime_spec`` for POST /api/agent."""
        if not isinstance(runtime_spec, Mapping):
            raise ValueError("runtime_spec is required to create sandbox")
        runtime = str(runtime_spec.get("runtime") or "").strip()
        rootfs_raw = runtime_spec.get("rootfs")
        if not isinstance(rootfs_raw, Mapping):
            raise ValueError("runtime_spec.rootfs is required to create sandbox")
        imageurl = str(
            rootfs_raw.get("imageurl") or rootfs_raw.get("image_url") or ""
        ).strip()
        if not runtime:
            raise ValueError("runtime_spec.runtime is required to create sandbox")
        if not imageurl:
            raise ValueError(
                "runtime_spec.rootfs.imageurl is required to create sandbox"
            )

        rootfs: dict[str, Any] = {"imageurl": imageurl}
        user = str(rootfs_raw.get("user") or "").strip()
        if user:
            rootfs["user"] = user
        ports = rootfs_raw.get("ports")
        if isinstance(ports, list) and ports:
            rootfs["ports"] = [str(port) for port in ports]

        normalized: dict[str, Any] = {"runtime": runtime, "rootfs": rootfs}
        sandbox_type = str(runtime_spec.get("sandbox_type") or "").strip()
        if sandbox_type:
            normalized["sandbox_type"] = sandbox_type
        if runtime_spec.get("cpu") is not None:
            normalized["cpu"] = int(runtime_spec["cpu"])
        if runtime_spec.get("memory") is not None:
            normalized["memory"] = int(runtime_spec["memory"])
        cmds = runtime_spec.get("cmds")
        if isinstance(cmds, list) and cmds:
            normalized["cmds"] = cmds
        return normalized

    async def create_sandbox(
        self,
        *,
        namespace: str,
        name: str,
        workspace: str,
        runtime_spec: AgentRuntimeSpec | Mapping[str, Any],
        env_vars: dict[str, str] | None = None,
        mounts: list[AgentMount] | None = None,
    ) -> SandboxInfo:
        """Create a detached agent instance via POST /api/agent (inline mode).

        Mirrors Frontend ``CreateAgentRequest`` inline path:

        - ``namespace`` / ``name`` / ``workspace`` / ``runtime_spec``: required
        - ``runtime_spec.runtime`` + ``runtime_spec.rootfs.imageurl``: required
        - ``env_vars`` / ``mounts``: optional
        - does not send ``urn`` (inline takes priority over registered)
        """
        self._ensure_connected()
        normalized_namespace = str(namespace or "").strip()
        normalized_name = str(name or "").strip()
        normalized_workspace = str(workspace or "").strip()
        if not normalized_namespace:
            raise ValueError("namespace is required to create sandbox")
        if not normalized_name:
            raise ValueError("name is required to create sandbox")
        if not normalized_workspace:
            raise ValueError("workspace is required to create sandbox")
        if not normalized_workspace.startswith("/"):
            raise ValueError("workspace must be an absolute path")

        normalized_runtime_spec = self._normalize_runtime_spec(runtime_spec)
        payload: dict[str, Any] = {
            "namespace": normalized_namespace,
            "name": normalized_name,
            "workspace": normalized_workspace,
            "runtime_spec": normalized_runtime_spec,
        }

        if env_vars:
            payload["env_vars"] = {
                str(key): str(value) for key, value in dict(env_vars).items()
            }

        if mounts:
            payload["mounts"] = list(mounts)

        status, body = await asyncio.to_thread(self._do_agent_create, payload)
        parsed = self._parse_agent_api_response(body, status)
        instance_id = str(parsed.get("instance_id") or "").strip()
        if not instance_id:
            raise YuanrongAgentApiError(
                f"create agent missing instance_id: status={status}, body={body!r}"
            )

        info = SandboxInfo(
            sandbox_id=instance_id,
            status="ready",
            metadata={
                "instance_id": instance_id,
                "namespace": normalized_namespace,
                "name": normalized_name,
                "workspace": normalized_workspace,
                "runtime_spec": dict(normalized_runtime_spec),
                "env_vars": dict(payload.get("env_vars") or {}),
                "mounts": list(payload.get("mounts") or []),
                "provisioning": "yuanrong_agent_api_inline",
            },
        )
        logger.info(
            "[YuanrongFrontendAgentClient] create_sandbox: "
            "instance_id=%s name=%s namespace=%s runtime=%s imageurl=%s",
            instance_id,
            normalized_name,
            normalized_namespace,
            normalized_runtime_spec.get("runtime"),
            (normalized_runtime_spec.get("rootfs") or {}).get("imageurl"),
        )
        return info

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Destroy a detached agent instance via DELETE /api/agent/:instanceId."""
        self._ensure_connected()
        normalized_sandbox_id = str(sandbox_id or "").strip()
        if not normalized_sandbox_id:
            raise ValueError("sandbox_id is required to delete sandbox")

        status, body = await asyncio.to_thread(
            self._do_agent_delete,
            normalized_sandbox_id,
        )
        self._parse_agent_api_response(body, status)
        logger.info(
            "[YuanrongFrontendAgentClient] delete_sandbox: instance_id=%s",
            normalized_sandbox_id,
        )

    async def get_agent_info(self, instance_id: str) -> dict[str, Any]:
        """Query agent instance info via GET /api/agent/:instanceId.

        Returns the ``instance`` dict (contains node_ip, sandbox_ip,
        sandbox_type, rootfs, workspace, env_vars, etc.).
        """
        self._ensure_connected()
        normalized_id = str(instance_id or "").strip()
        if not normalized_id:
            raise ValueError("instance_id is required to get agent info")
        status, body = await asyncio.to_thread(self._do_agent_get, normalized_id)
        parsed = self._parse_agent_api_response(body, status)
        instance = parsed.get("instance")
        return instance if isinstance(instance, dict) else {}

    async def upload_agent_file(
        self,
        instance_id: str,
        path: str,
        data: bytes,
        *,
        auth_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upload a file into an agent container via POST /api/agent/:id/files/upload."""
        self._ensure_connected()
        normalized_id = str(instance_id or "").strip()
        normalized_path = str(path or "").strip()
        if not normalized_id:
            raise ValueError("instance_id is required to upload agent file")
        if not normalized_path:
            raise ValueError("path is required to upload agent file")
        status, body = await asyncio.to_thread(
            self._do_agent_file_upload,
            normalized_id,
            normalized_path,
            data,
            dict(auth_headers or {}),
        )
        return self._parse_agent_file_upload_response(body, status, normalized_path, len(data))

    async def download_agent_file(
        self,
        instance_id: str,
        path: str,
        *,
        offset: int = 0,
        limit: int = 65536,
        auth_headers: Mapping[str, str] | None = None,
    ) -> AgentFileDownloadChunk:
        """Download a file chunk from GET /api/agent/:id/files/download (Range)."""
        self._ensure_connected()
        normalized_id = str(instance_id or "").strip()
        normalized_path = str(path or "").strip()
        if not normalized_id:
            raise ValueError("instance_id is required to download agent file")
        if not normalized_path:
            raise ValueError("path is required to download agent file")
        resolved_offset = max(int(offset), 0)
        resolved_limit = max(int(limit), 1)
        status, data, content_type, total_size = await asyncio.to_thread(
            self._do_agent_file_download,
            normalized_id,
            normalized_path,
            resolved_offset,
            resolved_limit,
            dict(auth_headers or {}),
        )
        if status in {404, 413} or status >= 500 or not (200 <= status < 300):
            self._raise_agent_file_http_error(status, data)
        chunk_size = len(data)
        if total_size <= 0:
            total_size = resolved_offset + chunk_size
        eof = resolved_offset + chunk_size >= total_size
        return AgentFileDownloadChunk(
            data=data,
            path=normalized_path,
            offset=resolved_offset,
            chunk_size=chunk_size,
            size=total_size,
            content_type=content_type or "application/octet-stream",
            eof=eof,
        )

    async def list_agent_files(
        self,
        instance_id: str,
        path: str,
        *,
        recursive: bool = False,
        max_depth: int = 0,
        auth_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List files in an agent container via GET /api/agent/:id/files/list."""
        self._ensure_connected()
        normalized_id = str(instance_id or "").strip()
        normalized_path = str(path or "").strip()
        if not normalized_id:
            raise ValueError("instance_id is required to list agent files")
        if not normalized_path:
            raise ValueError("path is required to list agent files")
        if int(max_depth) < 0:
            raise ValueError("max_depth must be >= 0")
        status, body = await asyncio.to_thread(
            self._do_agent_file_list,
            normalized_id,
            normalized_path,
            bool(recursive),
            int(max_depth),
            dict(auth_headers or {}),
        )
        return self._parse_agent_file_list_response(body, status)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    def _agent_create_url(self) -> str:
        return f"{self._frontend_endpoint}/api/agent"

    def _agent_delete_url(self, instance_id: str) -> str:
        encoded = urllib.parse.quote(instance_id, safe="")
        return f"{self._frontend_endpoint}/api/agent/{encoded}"

    def _agent_files_upload_url(self, instance_id: str) -> str:
        encoded = urllib.parse.quote(instance_id, safe="")
        return f"{self._frontend_endpoint}/api/agent/{encoded}/files/upload"

    def _agent_files_download_url(self, instance_id: str, path: str) -> str:
        encoded_id = urllib.parse.quote(instance_id, safe="")
        encoded_path = urllib.parse.quote(path, safe="")
        return (
            f"{self._frontend_endpoint}/api/agent/{encoded_id}/files/download"
            f"?path={encoded_path}"
        )

    def _agent_files_list_url(
        self,
        instance_id: str,
        path: str,
        *,
        recursive: bool,
        max_depth: int,
    ) -> str:
        encoded_id = urllib.parse.quote(instance_id, safe="")
        query = urllib.parse.urlencode(
            {
                "path": path,
                "recursive": "true" if recursive else "false",
                "max_depth": str(int(max_depth)),
            }
        )
        return f"{self._frontend_endpoint}/api/agent/{encoded_id}/files/list?{query}"

    @staticmethod
    def _merge_auth_headers(base_headers: dict[str, str], auth_headers: dict[str, str]) -> dict[str, str]:
        merged = dict(base_headers)
        for key, value in auth_headers.items():
            if value is not None and str(value).strip():
                merged[str(key)] = str(value)
        return merged

    @staticmethod
    def _encode_multipart_form(
        fields: dict[str, str],
        *,
        file_field: str,
        file_bytes: bytes,
        filename: str = "file",
    ) -> tuple[bytes, str]:
        boundary = f"----YuanrongFormBoundary{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
        )
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        return bytes(body), f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _parse_content_range_total(content_range: str, *, fallback_size: int) -> int:
        text = str(content_range or "").strip()
        match = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", text, flags=re.IGNORECASE)
        if not match:
            return fallback_size
        total_text = match.group(1)
        if total_text == "*":
            return fallback_size
        try:
            return int(total_text)
        except ValueError:
            return fallback_size

    @staticmethod
    def _parse_agent_file_error_body(raw: bytes | str) -> str:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        text = text.strip()
        if not text:
            return "agent file request failed"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            return str(parsed.get("error") or parsed.get("message") or text)
        return text

    def _agent_file_http_error(self, status: int, body: bytes | str) -> YuanrongAgentFileError:
        message = self._parse_agent_file_error_body(body)
        if status == 404:
            lowered = message.lower()
            if "file not found" in lowered:
                code = "file_not_found"
            else:
                code = "instance_not_found"
        elif status == 413:
            code = "file_too_large"
        elif status == 400:
            code = "BAD_REQUEST"
        else:
            code = "INTERNAL_ERROR"
        return YuanrongAgentFileError(message, http_status=status, error_code=code)

    def _raise_agent_file_http_error(self, status: int, body: bytes | str) -> None:
        raise self._agent_file_http_error(status, body)

    def _parse_agent_file_upload_response(
        self,
        body: str,
        status: int,
        path: str,
        uploaded_size: int,
    ) -> dict[str, Any]:
        if not (200 <= status < 300):
            raise self._agent_file_http_error(status, body)
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise YuanrongAgentFileError(
                f"invalid upload response: {body!r}",
                http_status=status,
                error_code="INTERNAL_ERROR",
            ) from exc
        if not isinstance(parsed, dict):
            raise YuanrongAgentFileError(
                f"invalid upload response shape: {body!r}",
                http_status=status,
                error_code="INTERNAL_ERROR",
            )
        if parsed.get("success") is True:
            return {
                "success": True,
                "path": str(parsed.get("path") or path),
                "size": int(parsed.get("size") or uploaded_size),
            }
        error = str(parsed.get("error") or parsed.get("message") or "upload failed")
        raise self._agent_file_http_error(status or 500, json.dumps({"error": error}))

    def _parse_agent_file_list_response(self, body: str, status: int) -> list[dict[str, Any]]:
        if not (200 <= status < 300):
            raise self._agent_file_http_error(status, body)
        try:
            parsed = json.loads(body) if body else []
        except json.JSONDecodeError as exc:
            raise YuanrongAgentFileError(
                f"invalid list response: {body!r}",
                http_status=status,
                error_code="INTERNAL_ERROR",
            ) from exc
        # Frontend GET /files/list 返回的是 FaaS invoke 外壳
        # {"body": {"items": [...]}, "innerCode": "0", ...}
        # chat invoke 已经剥这层；list 必须同样处理，否则 gateway 看到空列表
        if isinstance(parsed, dict) and self._is_faas_envelope(parsed):
            parsed, faas_err = self._normalize_faas_body(parsed)
            if faas_err:
                raise self._agent_file_http_error(
                    status or 500,
                    json.dumps({"error": f"faas list failed: innerCode={faas_err}"}),
                )
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            if parsed.get("success") is False:
                error = str(parsed.get("error") or parsed.get("message") or "list failed")
                raise self._agent_file_http_error(status or 500, json.dumps({"error": error}))
            raw_items = parsed.get("items")
            if raw_items is None:
                raw_items = parsed.get("files")
            if raw_items is None:
                raw_items = parsed.get("data")
            if raw_items is None:
                raw_items = []
            if not isinstance(raw_items, list):
                raise YuanrongAgentFileError(
                    f"invalid list response shape: {body!r}",
                    http_status=status,
                    error_code="INTERNAL_ERROR",
                )
            items = raw_items
        else:
            raise YuanrongAgentFileError(
                f"invalid list response shape: {body!r}",
                http_status=status,
                error_code="INTERNAL_ERROR",
            )
        return [item for item in items if isinstance(item, dict)]

    def _do_agent_file_upload(
        self,
        instance_id: str,
        path: str,
        data: bytes,
        auth_headers: dict[str, str],
    ) -> tuple[int, str]:
        payload, content_type = self._encode_multipart_form(
            {"path": path},
            file_field="file",
            file_bytes=data,
            filename=path.rsplit("/", 1)[-1] or "file",
        )
        headers = self._merge_auth_headers({"Content-Type": content_type}, auth_headers)
        req = urllib.request.Request(
            self._agent_files_upload_url(instance_id),
            data=payload,
            headers=headers,
            method="POST",
        )
        return self._urlopen_request(
            req,
            timeout=self._agent_timeout_s,
            raise_on_timeout=True,
        )

    def _do_agent_file_download(
        self,
        instance_id: str,
        path: str,
        offset: int,
        limit: int,
        auth_headers: dict[str, str],
    ) -> tuple[int, bytes, str, int]:
        headers = self._merge_auth_headers({"Accept": "*/*"}, auth_headers)
        end = offset + limit - 1
        headers["Range"] = f"bytes={offset}-{end}"
        req = urllib.request.Request(
            self._agent_files_download_url(instance_id, path),
            headers=headers,
            method="GET",
        )
        resolved_timeout = float(self._agent_timeout_s)
        try:
            with urllib.request.urlopen(req, timeout=resolved_timeout) as resp:
                status = int(getattr(resp, "status", 200))
                data = resp.read()
                content_type = str(resp.headers.get("Content-Type") or "application/octet-stream")
                content_range = str(resp.headers.get("Content-Range") or "")
                content_length = int(resp.headers.get("Content-Length") or len(data) or 0)
                total_size = self._parse_content_range_total(
                    content_range,
                    fallback_size=offset + content_length,
                )
                return status, data, content_type, total_size
        except urllib.error.HTTPError as err:
            body = err.read() if err.fp else b""
            status = int(getattr(err, "code", 500) or 500)
            logger.error(
                "[YuanrongFrontendAgentClient] file download HTTP error: "
                "instance=%s path=%s code=%d",
                instance_id,
                path,
                status,
            )
            return status, body, "application/octet-stream", 0
        except Exception as err:
            logger.error(
                "[YuanrongFrontendAgentClient] file download failed: "
                "instance=%s path=%s error=%s",
                instance_id,
                path,
                err,
            )
            if self._is_timeout_error(err):
                raise YuanrongAgentApiError(
                    f"file download timeout after {resolved_timeout}s: {err}"
                ) from err
            return 500, str(err).encode("utf-8"), "application/octet-stream", 0

    def _do_agent_file_list(
        self,
        instance_id: str,
        path: str,
        recursive: bool,
        max_depth: int,
        auth_headers: dict[str, str],
    ) -> tuple[int, str]:
        headers = self._merge_auth_headers({"Accept": "application/json"}, auth_headers)
        req = urllib.request.Request(
            self._agent_files_list_url(
                instance_id,
                path,
                recursive=recursive,
                max_depth=max_depth,
            ),
            headers=headers,
            method="GET",
        )
        return self._urlopen_request(
            req,
            timeout=self._agent_timeout_s,
            raise_on_timeout=True,
        )

    @staticmethod
    def _parse_agent_api_response(body: str, status: int) -> dict[str, Any]:
        try:
            parsed = json.loads(body) if body else {}
        except Exception as exc:
            raise YuanrongAgentApiError(
                f"invalid agent API response: status={status}, body={body!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise YuanrongAgentApiError(
                f"invalid agent API response shape: status={status}, body={body!r}"
            )
        code = parsed.get("code")
        if not (200 <= status < 300) or code not in (200, "200"):
            message = parsed.get("message") or parsed.get("status") or body
            raise YuanrongAgentApiError(
                f"agent API failed: http_status={status}, code={code}, message={message!r}"
            )
        return parsed

    def _urlopen_request(
        self,
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        raise_on_timeout: bool = False,
    ) -> tuple[int, str]:
        resolved_timeout = (
            self._invoke_timeout_s if timeout is None else float(timeout)
        )
        try:
            with urllib.request.urlopen(req, timeout=resolved_timeout) as resp:
                status = int(getattr(resp, "status", 200))
                text = resp.read().decode("utf-8", errors="replace")
                return status, text
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace") if err.fp else str(err)
            logger.error(
                "[YuanrongFrontendAgentClient] HTTP error: url=%s code=%d",
                req.full_url,
                getattr(err, "code", 500),
            )
            return int(getattr(err, "code", 500) or 500), text
        except Exception as err:
            logger.error(
                "[YuanrongFrontendAgentClient] request failed: url=%s error=%s",
                req.full_url,
                str(err),
            )
            if raise_on_timeout and self._is_timeout_error(err):
                raise YuanrongAgentApiError(
                    f"request timeout after {resolved_timeout}s: "
                    f"url={req.full_url}, error={err}"
                ) from err
            return 500, str(err)

    @staticmethod
    def _is_timeout_error(err: BaseException) -> bool:
        if isinstance(err, TimeoutError):
            return True
        reason = getattr(err, "reason", None)
        if isinstance(reason, TimeoutError):
            return True
        text = str(err).lower()
        return "timed out" in text or "timeout" in type(err).__name__.lower()

    def _do_agent_create(self, payload: dict[str, Any]) -> tuple[int, str]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._agent_create_url(),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._urlopen_request(
            req,
            timeout=self._agent_timeout_s,
            raise_on_timeout=True,
        )

    def _do_agent_delete(self, instance_id: str) -> tuple[int, str]:
        req = urllib.request.Request(
            self._agent_delete_url(instance_id),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        return self._urlopen_request(
            req,
            timeout=self._agent_timeout_s,
            raise_on_timeout=True,
        )

    def _do_agent_get(self, instance_id: str) -> tuple[int, str]:
        req = urllib.request.Request(
            self._agent_delete_url(instance_id),  # same URL: /api/agent/{instanceId}
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        return self._urlopen_request(
            req,
            timeout=self._agent_timeout_s,
        )

    def _invoke_url(self) -> str:
        urn = urllib.parse.quote(self._function_version_urn, safe="")
        return f"{self._frontend_endpoint}/serverless/v1/functions/{urn}/invocations"

    def _build_invoke_payload(self, envelope: E2AEnvelope, *, stream: bool) -> dict[str, Any]:
        """构造 faas invocation 请求体（E2AEnvelope 形状，与 ws 入站完全一致）.

        直接用 envelope.to_dict() 发送，透传完整 channel_context（含
        gateway 已注入的 permission_context/enable_memory），不丢字段。
        仅覆盖 is_stream 以区分非流式/流式。
        """
        payload = envelope.to_dict()
        payload["is_stream"] = stream
        return payload

    @staticmethod
    def _is_faas_envelope(parsed: Any) -> bool:
        """是否为 faas executor 的外层封装形状.

        faas executor 把 clawee 返回值包成 {"body": <result>, "innerCode": ..., ...}，
        仅当确实识别到此形状（含 body+innerCode，且非标准 AgentResponse 形状）时才剥离，
        避免误吞 websocket 直连等其它路径返回的普通 dict。
        """
        if not isinstance(parsed, dict):
            return False
        if "body" not in parsed or "innerCode" not in parsed:
            return False
        # 已是标准 AgentResponse 形状则不当作 faas 封装处理
        return "payload" not in parsed and "ok" not in parsed

    @staticmethod
    def _normalize_faas_body(parsed: Any) -> tuple[Any, str | None]:
        """对 faas 返回体做「剥外层封装 + 二次解析」统一规范化.

        faas executor 把 clawee 返回值包成
        {"body": <result>, "innerCode": "0", "traceId":..., ...} 再 to_json_string，
        clawee.handler 返回 response_to_payload(resp) = json.dumps(asdict(resp)) 即 str，
        故 body 字段常是内层 JSON 字符串。本函数取出内层 body 并二次解析为 AgentResponse dict。

        非流式整体 body 与流式单个 chunk 共用此规范化，保证两条路径解析逻辑一致。
        仅当确实识别到 faas 外层形状（有 body+innerCode 且非 AgentResponse 形状）时剥离，
        避免误吞 websocket 直连等其它路径返回的普通 dict。

        Returns:
            (normalized, faas_error_code):faas_error_code 非 None 表示 faas 层错误（innerCode != "0"）。
        """
        # 剥 faas executor 外层封装
        if YuanrongFrontendAgentClient._is_faas_envelope(parsed):
            inner = parsed.get("body")
            if isinstance(inner, str) and inner.strip():
                try:
                    inner = json.loads(inner)
                except Exception:
                    inner = {"content": inner}
            if inner is None:
                inner = {}
            if not isinstance(inner, dict):
                inner = {"content": inner}
            inner_code = str(parsed.get("innerCode", "0"))
            if inner_code != "0":
                inner = dict(inner)
                inner["_faas_error_code"] = inner_code
                return inner, inner_code
            parsed = inner

        # 二次解析：faas 可能把 JSON 字符串放进 body 后再序列化一次，导致首次 json.loads 拿到 str
        if isinstance(parsed, str) and parsed.strip():
            try:
                parsed = json.loads(parsed)
            except Exception:
                parsed = {"content": parsed}

        return parsed, None

    @staticmethod
    def _is_agent_response_shape(parsed: Any) -> bool:
        """是否为标准 AgentResponse 形状（与 websocket parse_agent_server_wire_unary 透传语义对齐）."""
        return (
            isinstance(parsed, dict)
            and "payload" in parsed
            and "ok" in parsed
            and isinstance(parsed.get("payload"), dict)
        )

    def _parse_invoke_response(
        self,
        body: str,
        status: int,
        request: AgentRequest,
    ) -> AgentResponse:
        """faas 非流式 body → AgentResponse.

        复用 parse_agent_server_wire_unary（与 ws client 同款反解），补齐
        agent_ref/metadata 透传。faas executor 外层 {body, innerCode} 由
        _normalize_faas_body 剥离，内层 body 是 E2A wire 形状。
        """
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"content": body}

        parsed, faas_err = self._normalize_faas_body(parsed)

        # 尝试用 wire_codec 反解（与 ws client 完全相同的反解函数）
        try:
            resp = parse_agent_server_wire_unary(parsed)
            meta = dict(resp.metadata or {})
            meta["http_status"] = status
            if faas_err:
                meta["_faas_error_code"] = faas_err
            return AgentResponse(
                request_id=resp.request_id or request.request_id,
                channel_id=resp.channel_id or request.channel_id,
                ok=(200 <= status < 300) and resp.ok,
                payload=resp.payload,
                metadata=meta,
                agent_ref=resp.agent_ref,
            )
        except Exception as parse_err:
            logger.debug(
                "[YuanrongFrontendAgentClient] wire_codec unary parse failed, "
                "falling back to legacy shape: %s",
                parse_err,
            )

        # 兜底：标准 AgentResponse 形状（非 E2A wire）
        if self._is_agent_response_shape(parsed):
            meta = dict(parsed.get("metadata") or {})
            meta["http_status"] = status
            if faas_err:
                meta["_faas_error_code"] = faas_err
            return AgentResponse(
                request_id=str(parsed.get("request_id") or request.request_id),
                channel_id=str(parsed.get("channel_id") or request.channel_id),
                ok=(200 <= status < 300) and bool(parsed.get("ok", True)),
                payload=parsed.get("payload", {}),
                metadata=meta,
                agent_ref=parsed.get("agent_ref"),
            )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=200 <= status < 300,
            payload={"content": parsed},
            metadata={"http_status": status},
        )

    @staticmethod
    def _normalize_invoke_chunk(text: str) -> dict[str, Any]:
        """faas 流式 chunk data 内容 → 规范化 dict.

        复用 parse_agent_server_wire_chunk（与 ws client 同款反解），补齐
        agent_ref/metadata 透传。faas executor 外层 {body, innerCode} 由
        _normalize_faas_body 剥离，内层 body 是 E2A wire 形状。
        """
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"content": text}
        parsed, _ = YuanrongFrontendAgentClient._normalize_faas_body(parsed)

        # 尝试用 wire_codec 反解（与 ws client 完全相同的反解函数）
        try:
            chunk = parse_agent_server_wire_chunk(parsed)
            result: dict[str, Any] = {
                "request_id": chunk.request_id,
                "channel_id": chunk.channel_id,
                "payload": chunk.payload,
                "is_complete": chunk.is_complete,
                "agent_ref": chunk.agent_ref,
                "metadata": chunk.metadata,
            }
            return result
        except Exception as parse_err:
            logger.debug(
                "[YuanrongFrontendAgentClient] wire_codec chunk parse failed, "
                "falling back to legacy shape: %s",
                parse_err,
            )

        return parsed if isinstance(parsed, dict) else {"content": parsed}

    def _invoke_headers(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        req_method: str | None = None,
        stream: bool = False,
    ) -> dict[str, str]:
        """构造 faas invocation 请求头.

        除了 X-Instance-Session（会话并发控制）外，当 user_id 非空时附加
        X-Session-Context: {"sessionCtx": <uid>}，faas 据此为 CreateSandbox
        绑定用户标识（function_agent 日志 "Create sandbox for <uid>"）。
        user_id 为空时只记一条 uid_empty=yes 告警，不附加该 header。
        """
        headers = {
            "Content-Type": "application/json",
            "X-Instance-Session": json.dumps(
                {
                    "sessionID": session_id,
                    "sessionTTL": self._session_ttl_s,
                    "concurrency": self._concurrency,
                },
                ensure_ascii=False,
            ),
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        uid = str(user_id or "").strip()
        if uid:
            session_context = json.dumps({"sessionCtx": uid}, ensure_ascii=False)
            headers["X-Session-Context"] = session_context
            logger.debug(
                "[YuanrongFrontendAgentClient] invoke headers: method=%s session_id=%s user_id=%s "
                "X-Session-Context=%s stream=%s",
                req_method,
                session_id,
                uid,
                session_context,
                stream,
            )
        else:
            logger.info(
                "[YuanrongFrontendAgentClient] invoke headers: method=%s session_id=%s "
                "uid_empty=yes X-Session-Context omitted stream=%s",
                req_method,
                session_id,
                stream,
            )
        return headers

    def _do_invoke(
        self,
        payload: dict[str, Any],
        session_id: str,
        user_id: str | None = None,
    ) -> tuple[int, str]:
        headers = self._invoke_headers(
            session_id,
            user_id=user_id,
            req_method=payload.get("method"),
        )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._invoke_url(), data=data, headers=headers, method="POST")
        return self._urlopen_request(req, raise_on_timeout=True)

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        """发送非流式请求.

        Args:
            envelope: E2A 信封

        Returns:
            AgentResponse 响应
        """
        self._ensure_connected()
        payload = self._build_invoke_payload(envelope, stream=False)
        session_id = envelope.session_id or ""
        try:
            status, body = await asyncio.to_thread(
                self._do_invoke,
                payload,
                session_id,
                envelope.user_id,
            )
        except YuanrongAgentApiError as e:
            logger.warning("[YuanrongFrontendAgentClient] invoke failed: %s", e)
            return AgentResponse(
                request_id=envelope.request_id,
                channel_id=envelope.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        # 仍需 AgentRequest 形状供 _parse_invoke_response 填充 request_id/channel_id 兜底
        request = e2a_to_agent_request(envelope)
        return self._parse_invoke_response(body, status, request)

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        """发送流式请求.

        Args:
            envelope: E2A 信封

        Yields:
            AgentResponseChunk 响应块
        """
        self._ensure_connected()
        payload = self._build_invoke_payload(envelope, stream=True)
        # 仍需 AgentRequest 形状供 chunk 兜底填充 request_id/channel_id
        request = e2a_to_agent_request(envelope)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        session_id = envelope.session_id or ""
        reader_task = asyncio.create_task(
            asyncio.to_thread(
                self._do_invoke_stream,
                payload,
                session_id,
                queue,
                loop,
                envelope.user_id,
            )
        )
        try:
            while True:
                item_type, text = await queue.get()
                if item_type == "chunk" and text:
                    # SSE 解析已完成，复用 wire_codec 反解 chunk body
                    parsed_obj = self._normalize_invoke_chunk(text)
                    yield AgentResponseChunk(
                        request_id=str(parsed_obj.get("request_id") or request.request_id),
                        channel_id=str(parsed_obj.get("channel_id") or request.channel_id),
                        payload=parsed_obj.get("payload", parsed_obj.get("content")),
                        is_complete=bool(parsed_obj.get("is_complete", False)),
                        agent_ref=parsed_obj.get("agent_ref"),
                        metadata=dict(parsed_obj.get("metadata") or {}),
                    )
                elif item_type == "error":
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"error": text or "invoke stream failed"},
                        is_complete=False,
                    )
                elif item_type == "exception":
                    raise RuntimeError(f"invoke stream failed: {text}")
                elif item_type == "done":
                    break

            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=None,
                is_complete=True,
            )
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    def _do_invoke_stream(
        self,
        payload: dict[str, Any],
        session_id: str,
        out_queue: asyncio.Queue[tuple[str, str | None]],
        loop: asyncio.AbstractEventLoop,
        user_id: str | None = None,
    ) -> None:
        """执行流式 HTTP 调用（在线程中运行）.

        Args:
            payload: 请求负载
            session_id: 会话ID
            out_queue: 输出队列
            loop: 事件循环
            user_id: 用户ID（透传给 faas 的 X-Session-Context）
        """
        headers = self._invoke_headers(
            session_id,
            user_id=user_id,
            req_method=payload.get("method"),
            stream=True,
        )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._invoke_url(), data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self._invoke_timeout_s) as resp:
                status = int(getattr(resp, "status", 200))

                if not (200 <= status < 300):
                    text = resp.read().decode("utf-8", errors="replace")
                    logger.error("[YuanrontFrontendAgentClient] HTTP错误状态码: %d, 响应: %s", status, text[:500])
                    loop.call_soon_threadsafe(
                        out_queue.put_nowait,
                        ("error", json.dumps({"http_status": status, "body": text}, ensure_ascii=False)),
                    )
                    return

                # SSE 解析：按行处理
                chunk_count = 0
                total_bytes = 0
                sse_line_buffer = ""
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        # 处理缓冲区中剩余的数据
                        if sse_line_buffer.strip():
                            self._process_sse_chunk(sse_line_buffer, out_queue, loop)
                        break

                    chunk_text = chunk.decode("utf-8", errors="replace")
                    total_bytes += len(chunk)
                    chunk_count += 1

                    # SSE 解析：按行处理
                    sse_line_buffer += chunk_text
                    lines = sse_line_buffer.split('\n')
                    # 保留最后一个可能不完整的行
                    sse_line_buffer = lines[-1] if lines else ""

                    for line in lines[:-1]:
                        line_stripped = line.strip()
                        if line_stripped.startswith('data: '):
                            data_content = line_stripped[6:]  # 去掉 "data: " 前缀
                            self._process_sse_chunk(data_content, out_queue, loop)
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace") if err.fp else str(err)
            logger.error(
                "[YuanrontFrontendAgentClient] stream HTTP error: session_id=%s, code=%d",
                session_id,
                getattr(err, "code", 500),
            )
            loop.call_soon_threadsafe(
                out_queue.put_nowait,
                (
                    "error",
                    json.dumps({
                        "http_status": int(getattr(err, "code", 500) or 500),
                        "body": text
                    }, ensure_ascii=False),
                ),
            )
        except Exception as err:
            logger.error(
                "[YuanrontFrontendAgentClient] stream request failed: session_id=%s, error=%s",
                session_id,
                str(err),
            )
            loop.call_soon_threadsafe(out_queue.put_nowait, ("exception", str(err)))
        finally:
            loop.call_soon_threadsafe(out_queue.put_nowait, ("done", None))

    def _process_sse_chunk(
        self,
        data_content: str,
        out_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """处理 SSE 数据块.

        Args:
            data_content: data: 后的内容（已去掉前缀）
            out_queue: 输出队列
            loop: 事件循环
        """
        data_content_stripped = data_content.strip()

        # 检查是否是结束标记
        if data_content_stripped == "[DONE]":
            loop.call_soon_threadsafe(out_queue.put_nowait, ("done", None))
            return

        # 发送 JSON 数据
        loop.call_soon_threadsafe(out_queue.put_nowait, ("chunk", data_content_stripped))
