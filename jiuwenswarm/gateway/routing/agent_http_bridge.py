"""Gateway → 目标 AgentServer 的受认证 HTTP bridge 客户端（Phase 2）。

基址解析与 HTTP 上传执行已下沉 ``jiuwenswarm.common.client.agent_http_bridge``
（保留侧 Web 静态服务 ``channels/web/app_web`` 同样消费）。此处：

- re-export 下沉后的符号，保持既有 import 路径兼容；
- 保留 Gateway 专有的 E2A 分片上传变体 ``upload_file_bytes_via_e2a``
  （依赖 ``gateway.routing.e2a_proxy`` 的 E2A 路由，不下沉）。

传输取舍：大文件走受认证 HTTP bridge（Gateway 仅鉴权转发、不落盘），避免大
base64 帧压垮 Gateway ↔ AgentServer 内部 WebSocket（``AGENT_WS_MAX_MESSAGE_BYTES``
帧限制）；小附件与文本内容走 E2A。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.client.agent_http_bridge import (  # noqa: F401
    UPLOAD_TIMEOUT_SECONDS,
    resolve_agent_host_port,
    resolve_agent_http_base,
    resolve_agent_http_base_for_token,
    resolve_agent_upload_base,
    set_agent_http_base_resolver,
    upload_file_bytes,
)

#: 走 base64 E2A 的载荷上限（原始字节）。超过此阈值改走 HTTP bridge 上传，
#: 保证 base64 帧（约 4/3 膨胀 + JSON 信封开销）远低于内部 WS 8MB 帧限制。
E2A_PAYLOAD_MAX_BYTES = 4 * 1024 * 1024

# Keep each encoded chunk comfortably below the 8MB internal WebSocket frame
# limit after base64 and E2A-envelope overhead.
_E2A_UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024

__all__ = [
    "E2A_PAYLOAD_MAX_BYTES",
    "UPLOAD_TIMEOUT_SECONDS",
    "resolve_agent_host_port",
    "resolve_agent_http_base",
    "resolve_agent_http_base_for_token",
    "resolve_agent_upload_base",
    "set_agent_http_base_resolver",
    "upload_file_bytes",
    "upload_file_bytes_via_e2a",
]


async def upload_file_bytes_via_e2a(
    content: bytes,
    target_rel_path: str,
    *,
    agent_client: Any,
    user_id: str | None,
    channel_id: str,
    session_id: str | None,
) -> tuple[bool, dict[str, Any]]:
    """Upload a large file to an AgentOS runtime through bounded E2A chunks.

    The former HTTP helper derives an address and signing secret in the Gateway
    process, which is only valid for the shared-directory single-user layout.
    In AgentOS the E2A router is the authority that selects the user runtime,
    so each chunk goes through that existing route instead.
    """
    import base64
    import uuid

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

    if not content:
        return False, {"error": "empty upload", "code": "BAD_REQUEST"}
    upload_id = uuid.uuid4().hex
    resolved_path = ""
    last_payload: dict[str, Any] = {}
    for offset in range(0, len(content), _E2A_UPLOAD_CHUNK_BYTES):
        chunk = content[offset:offset + _E2A_UPLOAD_CHUNK_BYTES]
        ok, payload = await fetch_agent_unary(
            agent_client=agent_client,
            req_method=ReqMethod.FILE_UPLOAD_CHUNK,
            params={
                "upload_id": upload_id,
                "target_rel_path": target_rel_path,
                "resolved_path": resolved_path,
                "data": base64.b64encode(chunk).decode("ascii"),
                "final": offset + len(chunk) >= len(content),
            },
            session_id=session_id,
            user_id=user_id,
            channel_id=channel_id,
            label="file.upload_chunk",
        )
        if not ok:
            return False, payload
        resolved_path = str(payload.get("path") or "").strip()
        if not resolved_path:
            return False, {"error": "upload response missing path", "code": "UPLOAD_FAILED"}
        last_payload = payload
    return True, last_payload