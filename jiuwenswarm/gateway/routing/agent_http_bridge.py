"""Gateway → 目标 AgentServer 的受认证 HTTP bridge 客户端（Phase 2）。

下载端点由 AgentServer 在 WS 端口上拦截（``agent_ws_server._process_request``）。
上传端点在 AgentOS 部署时由路由/扩展层注入 ``JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE``
环境变量提供；单机模式下 AgentServer 不启动独立 HTTP 上传监听器，``media.persist``
大图在 Gateway 侧直接写入共享用户目录（见 ``app_web_handlers._persist_media_locally``）。
本模块为 Gateway 侧多个调用方（Web 静态服务 ``app_web``、IM 附件落盘钩子、Web
``media.persist`` 大图分流）提供统一的基址解析与上传执行，避免各处重复推导。

传输取舍（方案 §10.5）：大文件走受认证 HTTP bridge（Gateway 仅鉴权转发、
不落盘），避免大 base64 帧压垮 Gateway ↔ AgentServer 内部 WebSocket
（``AGENT_WS_MAX_MESSAGE_BYTES`` 帧限制）；小附件与文本内容走 E2A。
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: 上传 HTTP 请求超时（秒）。大附件可达数十 MB，放宽到 300s。
UPLOAD_TIMEOUT_SECONDS = 300

#: 走 base64 E2A 的载荷上限（原始字节）。超过此阈值改走 HTTP bridge 上传，
#: 保证 base64 帧（约 4/3 膨胀 + JSON 信封开销）远低于内部 WS 8MB 帧限制。
E2A_PAYLOAD_MAX_BYTES = 4 * 1024 * 1024

# Keep each encoded chunk comfortably below the 8MB internal WebSocket frame
# limit after base64 and E2A-envelope overhead.
_E2A_UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024


def resolve_agent_host_port() -> tuple[str, str, str]:
    """返回 (http_scheme, host, ws_port)，与 Gateway WS 客户端地址解析一致。

    Gateway 的 AgentServer 地址优先级为 ``AGENT_SERVER_URL`` 优先于
    ``AGENT_SERVER_HOST`` + ``AGENT_SERVER_PORT``（见 app_gateway 的
    ``agent_server_url`` 推导）。下载端点由 AgentServer 在 WS 端口上拦截
    （``agent_ws_server._process_request``），因此下载 HTTP 基址应与 WS 地址
    同 host/port，仅把 ``ws``/``wss`` 换成 ``http``/``https``。
    """
    url = os.getenv("AGENT_SERVER_URL")
    if url:
        try:
            parsed = urlparse(url)
            if parsed.hostname and parsed.port:
                scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
                return scheme, parsed.hostname, str(parsed.port)
        except ValueError:
            pass

    host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1")
    port = os.getenv("AGENT_SERVER_PORT") or os.getenv("AGENT_PORT", "18092")
    return "http", host, str(port)


def resolve_agent_http_base() -> str:
    """返回目标 AgentServer 的下载 HTTP 端点基址（HTTP bridge 代理目标）。

    优先级：
    1. ``JIUWENSWARM_AGENT_HTTP_BASE`` 环境变量（AgentOS 部署注入沙箱网络基址）；
    2. 单机模式：由 ``AGENT_SERVER_URL`` / ``AGENT_SERVER_HOST`` + ``AGENT_SERVER_PORT``
       （默认 18092）推导 ``http(s)://<host>:<port>``（AgentServer 在 WS 端口拦截
       ``/file-api/download``）。
    """
    env_base = os.getenv("JIUWENSWARM_AGENT_HTTP_BASE")
    if env_base:
        return env_base.rstrip("/")
    scheme, host, port = resolve_agent_host_port()
    return f"{scheme}://{host}:{port}"


def resolve_agent_upload_base() -> str:
    """返回目标 AgentServer 的上传 HTTP 端点基址。

    AgentOS 部署时由路由/扩展层注入 ``JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE``
    或 ``JIUWENSWARM_AGENT_HTTP_PORT`` 环境变量；未注入时按 WS 端口 + 1 推导
    （该推导值仅对已启动独立上传监听器的部署有效，单机模式不会监听该端口，
    调用方应改走本地共享目录写入而非 HTTP 上传）。下载/WS 的
    ``JIUWENSWARM_AGENT_HTTP_BASE`` 不能复用为上传基址；上传监听器恒绑定
    localhost，故端口推导沿用 WS 端口但 host 固定 ``127.0.0.1``。
    """
    env_base = os.getenv("JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE")
    if env_base:
        return env_base.rstrip("/")
    env_port = os.getenv("JIUWENSWARM_AGENT_HTTP_PORT")
    if env_port:
        return f"http://127.0.0.1:{env_port}"
    _, _, port = resolve_agent_host_port()
    try:
        return f"http://127.0.0.1:{int(port) + 1}"
    except (TypeError, ValueError):
        return f"http://127.0.0.1:{port}"


#: 按请求（token payload）解析目标 AgentServer HTTP 基址的可注入扩展点。
#: AgentOS 多用户部署由路由/扩展层注册：``fn(payload: dict) -> str | None``，
#: 返回 ``None`` 时回落环境变量/单机默认推导。单机模式不注册也保持正确。
_agent_http_base_resolver: Any = None


def set_agent_http_base_resolver(resolver: Any) -> None:
    """注册按请求解析 AgentServer HTTP 基址的函数（AgentOS 部署侧扩展点）。"""
    global _agent_http_base_resolver
    _agent_http_base_resolver = resolver


def _decode_token_payload(token: str) -> dict[str, Any] | None:
    """无密钥解码下载/上传 token 的明文 payload（签名校验仍由 AgentServer 负责）。

    仅用于路由决策（解析 ``sid``/``path``），不用于鉴权。
    """
    import base64 as _base64
    import json as _json

    try:
        payload_b64 = token.split(".", 1)[0]
        padding = "=" * (-len(payload_b64) % 4)
        decoded = _base64.urlsafe_b64decode(payload_b64 + padding)
        payload = _json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        return None


def resolve_agent_http_base_for_token(token: str, *, endpoint: str) -> str:
    """解析当前请求应代理到的 AgentServer HTTP 基址。

    Token payload 只用于提取 resolver 所需的路由标识，不能提供代理目标：
    payload 在此处尚未完成签名校验，直接使用其中的 URL 会让攻击者控制
    Gateway 的出站请求目标。AgentOS 多用户部署应注册 resolver；旧部署继续
    使用环境变量或单机默认推导。
    """
    if endpoint not in {"download", "upload"}:
        raise ValueError(f"unsupported HTTP bridge endpoint: {endpoint}")
    payload = _decode_token_payload(token)
    resolver = _agent_http_base_resolver
    if resolver is not None and payload is not None:
        try:
            # 新 resolver 可按端点返回不同基址；保留单参数回调兼容已有部署扩展。
            try:
                base = resolver(payload, endpoint)
            except TypeError:
                base = resolver(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent_http_bridge] agent http base resolver failed: %s", exc)
            base = None
        if base:
            return str(base).rstrip("/")
    return ""


def upload_file_bytes(content: bytes, target_rel_path: str) -> tuple[bool, dict[str, Any]]:
    """把文件字节经受认证 HTTP bridge 上传到目标 AgentServer，返回 ``(ok, payload)``。

    - ``target_rel_path`` 为相对用户目录根的目标路径（如
      ``agent/workspace/feishu_files/downloads/images/a.png``），与
      ``generate_file_upload_token`` 的 payload 语义一致；
    - AgentServer 校验 token 后按注入目录落盘并做目录边界校验；本函数不落盘
      Gateway 本地；
    - 失败返回 ``(False, {error, code})``，调用方按可重试错误处理（方案 §8
      禁止 Gateway 本地 fallback）。
    """
    import json as _json
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    from jiuwenswarm.agents.harness.common.tools.web_file_download import (
        generate_file_upload_token,
    )

    token = generate_file_upload_token(str(target_rel_path))
    base = (
        resolve_agent_http_base_for_token(token, endpoint="upload")
        or resolve_agent_upload_base()
    )
    url = f"{base}/file-api/upload?token={quote(token, safe='')}"
    req = urllib.request.Request(
        url,
        data=content,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(content)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_SECONDS) as upstream:  # noqa: S310
            status = int(getattr(upstream, "status", 200))
            raw = upstream.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        message = raw.decode("utf-8", errors="replace").strip() or f"upload failed: {exc.code}"
        return False, {"error": message, "code": "UPLOAD_FAILED"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent_http_bridge] upload 转发失败: %s", exc)
        return False, {"error": str(exc), "code": "SERVICE_UNAVAILABLE"}

    if status != 200:
        message = raw.decode("utf-8", errors="replace").strip() or f"upload failed: {status}"
        return False, {"error": message, "code": "UPLOAD_FAILED"}
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False, {"error": "invalid upload response", "code": "UPLOAD_FAILED"}
    if not isinstance(payload, dict) or not str(payload.get("path") or "").strip():
        return False, {"error": "upload response missing path", "code": "UPLOAD_FAILED"}
    return True, payload


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
