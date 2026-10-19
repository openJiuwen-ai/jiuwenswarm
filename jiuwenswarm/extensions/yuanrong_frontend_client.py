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

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    def _agent_create_url(self) -> str:
        return f"{self._frontend_endpoint}/api/agent"

    def _agent_delete_url(self, instance_id: str) -> str:
        encoded = urllib.parse.quote(instance_id, safe="")
        return f"{self._frontend_endpoint}/api/agent/{encoded}"

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
        return self._urlopen_request(req)

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
        status, body = await asyncio.to_thread(
            self._do_invoke,
            payload,
            session_id,
            envelope.user_id,
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
