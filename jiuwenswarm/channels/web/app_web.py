# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Serve built frontend static files with optional reverse proxy.

Production role: SPA + health; business file/share/sessions HTTP lives on
Gateway Web HTTP — this process proxies those paths when Ingress still hits Web.
``/api`` (non-sessions) and ``/ws`` proxy to Gateway WebChannel remain.

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import io
import json
import logging
import mimetypes
import os
import select
import socket
import ssl
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early
parse_dotenv_early("jiuwenswarm-web")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_insecure_ssl_context, get_ssl_verify
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.ws_diagnostics import describe_ws_exception, format_ws_diagnostics
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.utils import (
    get_logs_dir,
    get_root_dir,
    get_user_workspace_dir,
    wait_for_tcp_port,
    SensitiveDataFilter,
)
from jiuwenswarm.gateway.channel_manager.web.web_http_server import resolve_web_http_port


def _get_package_dir() -> Path:
    """Get the jiuwenswarm/channels/web package directory."""
    # app_web.py is at jiuwenswarm/channels/web/app_web.py
    # So parent is jiuwenswarm/channels/web/
    return Path(__file__).resolve().parent


def _default_dist_dir() -> Path:
    """Return default dist directory for frontend static files."""
    # Priority 1: user workspace channels/web/frontend/dist
    root = get_root_dir()
    user_dist = root / "channels" / "web" / "frontend" / "dist"
    if user_dist.exists():
        return user_dist
    # Priority 2: package internal channels/web/frontend/dist
    package_dir = _get_package_dir()
    dist_dir = package_dir / "frontend" / "dist"
    if dist_dir.exists():
        return dist_dir
    # Fallback: return package internal path
    return dist_dir


def _inject_user_web_runtime_config(
    document: str,
    login_auth_simulate: bool = True,
    web_transport: str = "websocket",
) -> str:
    """Inject runtime edition and login-auth flags into index.html."""
    edition = "enterprise" if is_enterprise() else "personal"
    return (
        document.replace("__JIUWENSWARM_EDITION_VALUE__", edition)
        .replace(
            "__JIUWEN_LOGIN_AUTH_SIMULATE_VALUE__",
            "true" if login_auth_simulate else "false",
        )
        .replace("__JIUWEN_WEB_TRANSPORT_VALUE__", web_transport)
    )


def _parse_login_auth_simulate(raw: str | None) -> bool:
    value = "true" if raw is None or not raw.strip() else raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"LOGIN_AUTH_SIMULATE 配置非法：期望 true 或 false，实际为 {raw!r}"
    )


def _parse_web_transport(raw: str | None) -> str:
    """Parse WEB_TRANSPORT; unset follows edition (enterprise→http, personal→websocket)."""
    value = (raw or "").strip().lower()
    if value in ("http", "a2"):
        return "http"
    if value in ("websocket", "ws"):
        return "websocket"
    return "http" if is_enterprise() else "websocket"


def _probe_http_service(
    target: str, path: str, service_name: str, timeout: float = 3.0
) -> tuple[bool, str]:
    """Probe an authentication dependency; 401/403 still prove reachability."""
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, f"{service_name}地址非法：{target!r}"
    connection_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout)
    base_path = parsed.path.rstrip("/")
    try:
        connection.request("GET", f"{base_path}{path}")
        response = connection.getresponse()
        response.read()
        if response.status >= 500:
            return False, f"{service_name}返回 HTTP {response.status}"
        return True, f"HTTP {response.status}"
    except (OSError, http.client.HTTPException) as exc:
        return False, str(exc)
    finally:
        connection.close()


def _probe_identity_service(target: str, timeout: float = 3.0) -> tuple[bool, str]:
    return _probe_http_service(target, "/v1/auth/me", "ID认证服务", timeout)


def _probe_manager_service(target: str, timeout: float = 3.0) -> tuple[bool, str]:
    return _probe_http_service(
        target, "/api/v1/user-console/agent-contexts", "Manager业务接口", timeout
    )


class _SpaStaticHandler(SimpleHTTPRequestHandler):
    """Static file handler with SPA fallback to index.html."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
    }

    api_target = ""
    ws_target = ""
    web_http_target = ""
    idp_target = ""
    manager_api_target = ""
    ws_disable_compress = False
    login_auth_simulate = True
    web_transport = "websocket"
    logger = logging.getLogger(__name__)

    _HOP_BY_HOP_HEADERS = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    _WS_LOG_MAX_CHARS = 2000
    _HTTP_PROXY_TIMEOUT = 30
    _PROXY_STREAM_CHUNK = 65536
    _WS_CONNECT_TIMEOUT = 10
    _WS_SELECT_TIMEOUT = 60
    _WS_RECV_BUFFER = 65536
    _WS_HANDSHAKE_MAX_SIZE = 65536
    _WS_HANDSHAKE_RECV_SIZE = 4096
    _DEFAULT_HTTPS_PORT = 443
    _DEFAULT_HTTP_PORT = 80

    def guess_type(self, path: str) -> str:
        suffix = Path(path).suffix.lower()
        if suffix in self.extensions_map:
            return self.extensions_map[suffix]

        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            return guessed

        return "application/octet-stream"

    class _WsTextFrameParser:
        """Parse websocket text frames from a byte stream."""

        def __init__(self) -> None:
            self._buffer = bytearray()
            self._fragmented_text = bytearray()
            self._awaiting_continuation = False

        def feed(self, data: bytes) -> list[str]:
            self._buffer.extend(data)
            messages: list[str] = []
            while True:
                if len(self._buffer) < 2:
                    break

                first = self._buffer[0]
                second = self._buffer[1]
                fin = bool(first & 0x80)
                rsv = first & 0x70
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                payload_len = second & 0x7F
                idx = 2

                if payload_len == 126:
                    if len(self._buffer) < idx + 2:
                        break
                    payload_len = int.from_bytes(self._buffer[idx:idx + 2], "big")
                    idx += 2
                elif payload_len == 127:
                    if len(self._buffer) < idx + 8:
                        break
                    payload_len = int.from_bytes(self._buffer[idx:idx + 8], "big")
                    idx += 8

                mask_key = b""
                if masked:
                    if len(self._buffer) < idx + 4:
                        break
                    mask_key = bytes(self._buffer[idx:idx + 4])
                    idx += 4

                frame_end = idx + payload_len
                if len(self._buffer) < frame_end:
                    break

                payload = bytes(self._buffer[idx:frame_end])
                del self._buffer[:frame_end]

                if masked:
                    payload = bytes(
                        b ^ mask_key[i % 4]
                        for i, b in enumerate(payload)
                    )

                if rsv:
                    continue

                if opcode in (0x8, 0x9, 0xA):
                    continue

                if opcode == 0x1:
                    if fin:
                        messages.append(payload.decode("utf-8", errors="replace"))
                    else:
                        self._fragmented_text = bytearray(payload)
                        self._awaiting_continuation = True
                    continue

                if opcode == 0x0 and self._awaiting_continuation:
                    self._fragmented_text.extend(payload)
                    if fin:
                        messages.append(
                            bytes(self._fragmented_text).decode("utf-8", errors="replace")
                        )
                        self._fragmented_text.clear()
                        self._awaiting_continuation = False
                    continue

                if opcode == 0x2:
                    self._fragmented_text.clear()
                    self._awaiting_continuation = False
                    continue

                self._fragmented_text.clear()
                self._awaiting_continuation = False

            return messages

    @classmethod
    def _truncate_for_ws_log(cls, text: str) -> str:
        if len(text) <= cls._WS_LOG_MAX_CHARS:
            return text
        return f"{text[:cls._WS_LOG_MAX_CHARS]}...<truncated:{len(text) - cls._WS_LOG_MAX_CHARS}>"

    @classmethod
    def _format_ws_part(cls, value: Any) -> str:
        if isinstance(value, str):
            return cls._truncate_for_ws_log(value)
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            text = str(value)
        return cls._truncate_for_ws_log(text)

    def _log_ws_business_message(self, direction: str, raw_message: str) -> None:
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        msg_type = payload.get("type")
        if msg_type == "req":
            self.logger.info(
                "[ws][%s][req] id=%s method=%s params=%s",
                direction,
                self._format_ws_part(payload.get("id")),
                self._format_ws_part(payload.get("method")),
                self._format_ws_part(payload.get("params")),
            )
            return
        if msg_type == "res":
            self.logger.info(
                "[ws][%s][res] id=%s ok=%s payload=%s error=%s code=%s",
                direction,
                self._format_ws_part(payload.get("id")),
                self._format_ws_part(payload.get("ok")),
                self._format_ws_part(payload.get("payload")),
                self._format_ws_part(payload.get("error")),
                self._format_ws_part(payload.get("code")),
            )
            return
        if msg_type == "event":
            self.logger.info(
                "[ws][%s][event] event=%s seq=%s stream_id=%s payload=%s",
                direction,
                self._format_ws_part(payload.get("event")),
                self._format_ws_part(payload.get("seq")),
                self._format_ws_part(payload.get("stream_id")),
                self._format_ws_part(payload.get("payload")),
            )

    def _is_api_route(self) -> bool:
        return urlparse(self.path).path.startswith("/api")

    def _is_ws_route(self) -> bool:
        return urlparse(self.path).path.startswith("/ws")

    def _is_websocket_upgrade(self) -> bool:
        upgrade = self.headers.get("Upgrade", "")
        connection = self.headers.get("Connection", "")
        return "websocket" in upgrade.lower() and "upgrade" in connection.lower()

    def _proxy_http(self) -> None:
        parsed = urlparse(self.api_target)
        if parsed.scheme == "https":
            ssl_ctx = None if get_ssl_verify() else get_insecure_ssl_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or self._DEFAULT_HTTPS_PORT,
                timeout=self._HTTP_PROXY_TIMEOUT,
                context=ssl_ctx,
            )
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port or self._DEFAULT_HTTP_PORT,
                timeout=self._HTTP_PROXY_TIMEOUT,
            )

        try:
            body = b""
            if self.command not in ("GET", "HEAD"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""

            forward_headers: dict[str, str] = {}
            for key, value in self.headers.items():
                if key.lower() in self._HOP_BY_HOP_HEADERS:
                    continue
                if key.lower() == "host":
                    continue
                forward_headers[key] = value
            forward_headers["Host"] = parsed.netloc

            conn.request(self.command, self.path, body=body, headers=forward_headers)
            resp = conn.getresponse()

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in self._HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            # 流式泵送（read1 + 逐块 flush）：SSE（chat.delta / history.message）的
            # 实时性依赖 body 增量到达。此前 resp.read() 整段读完才回包——响应头
            # 和全部帧推迟到上游流结束，浏览器把 8s 的流式回复在流结束时一次性
            # 收到，既丢失流式渲染，又让响应头超过前端 15s 请求超时（触发
            # REQUEST_TIMEOUT → 前端自动 interrupt）。
            # 注意必须用 read1：read(65536) 会跨 chunk 凑满 64KB 才返回，SSE 小帧
            # 永远凑不满，等于仍然整段缓冲。HTTP/1.0 下无 Content-Length 的响应
            # 由连接关闭定界（SSE 场景）。
            while True:
                chunk = resp.read1(self._PROXY_STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as exc:  # noqa: BLE001
            self.log_error("proxy http error: %s", exc)
            try:
                self.send_error(502, "proxy http error")
            except Exception:  # noqa: BLE001
                # 响应头已发出（流中断）：无法再回 502，断开连接由关闭定界
                self.close_connection = True
        finally:
            conn.close()

    def _proxy_websocket_tunnel(self) -> None:
        parsed = urlparse(self.ws_target)
        if parsed.scheme not in ("ws", "wss", "http", "https"):
            self.send_error(500, "ws proxy target must be ws/wss/http/https")
            return

        upstream_host = parsed.hostname or "127.0.0.1"
        upstream_port = parsed.port or (
            self._DEFAULT_HTTPS_PORT if parsed.scheme in ("wss", "https") else self._DEFAULT_HTTP_PORT
        )

        try:
            upstream = socket.create_connection((upstream_host, upstream_port), timeout=self._WS_CONNECT_TIMEOUT)
            if parsed.scheme in ("wss", "https"):
                ctx = ssl.create_default_context() if get_ssl_verify() else get_insecure_ssl_context()
                upstream = ctx.wrap_socket(upstream, server_hostname=upstream_host)
        except OSError as exc:
            self.log_error(
                "proxy ws connect failed: %s",
                format_ws_diagnostics(
                    {
                        "client": self.client_address,
                        "upstream_host": upstream_host,
                        "upstream_port": upstream_port,
                        "scheme": parsed.scheme,
                    },
                    describe_ws_exception(exc),
                ),
            )
            self.send_error(502, "proxy ws connect failed")
            return

        try:
            request_lines = [f"{self.command} {self.path} HTTP/1.1"]
            for key, value in self.headers.items():
                # Optional debug mode: disable websocket compression so frames stay
                # plain text and can be parsed for req/res/event logging.
                if self.ws_disable_compress and key.lower() == "sec-websocket-extensions":
                    continue
                if key.lower() == "host":
                    request_lines.append(f"Host: {upstream_host}:{upstream_port}")
                else:
                    request_lines.append(f"{key}: {value}")
            if not any(line.lower().startswith("host:") for line in request_lines[1:]):
                request_lines.append(f"Host: {upstream_host}:{upstream_port}")
            raw_req = ("\r\n".join(request_lines) + "\r\n\r\n").encode("utf-8")
            upstream.sendall(raw_req)

            response_head = b""
            while b"\r\n\r\n" not in response_head:
                chunk = upstream.recv(self._WS_HANDSHAKE_RECV_SIZE)
                if not chunk:
                    break
                response_head += chunk
                if len(response_head) > self._WS_HANDSHAKE_MAX_SIZE:
                    break
            if not response_head:
                self.log_error(
                    "proxy ws handshake failed: %s",
                    format_ws_diagnostics(
                        {
                            "client": self.client_address,
                            "upstream_host": upstream_host,
                            "upstream_port": upstream_port,
                            "reason": "empty response",
                        }
                    ),
                )
                self.send_error(502, "proxy ws handshake failed: empty response")
                return

            self.connection.sendall(response_head)

            if b" 101 " not in response_head.split(b"\r\n", 1)[0]:
                status_line = response_head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
                self.logger.info(
                    "[ws][handshake] upstream returned non-101, tunnel closed: %s",
                    format_ws_diagnostics(
                        {
                            "client": self.client_address,
                            "upstream_host": upstream_host,
                            "upstream_port": upstream_port,
                            "status": status_line,
                        }
                    ),
                )
                return

            self.logger.info(
                "[ws][handshake] tunnel established %s <-> %s:%s",
                self.client_address[0], upstream_host, upstream_port,
            )
            self.connection.setblocking(False)
            upstream.setblocking(False)
            sockets = [self.connection, upstream]
            client_parser = self._WsTextFrameParser()
            server_parser = self._WsTextFrameParser()
            while True:
                readable, _, errored = select.select(sockets, [], sockets, self._WS_SELECT_TIMEOUT)
                if errored:
                    self.log_error(
                        "proxy ws socket error, closing tunnel: %s",
                        format_ws_diagnostics(
                            {
                                "client": self.client_address,
                                "upstream_host": upstream_host,
                                "upstream_port": upstream_port,
                                "errored": [
                                    "client" if sock is self.connection else "upstream"
                                    for sock in errored
                                ],
                            }
                        ),
                    )
                    break
                if not readable:
                    continue
                for sock in readable:
                    direction = "frontend->backend" if sock is self.connection else "backend->frontend"
                    try:
                        data = sock.recv(self._WS_RECV_BUFFER)
                    except OSError as recv_exc:
                        self.log_error(
                            "proxy ws recv failed, closing tunnel: %s",
                            format_ws_diagnostics(
                                {
                                    "client": self.client_address,
                                    "upstream_host": upstream_host,
                                    "upstream_port": upstream_port,
                                    "direction": direction,
                                },
                                describe_ws_exception(recv_exc),
                            ),
                        )
                        data = b""
                    if not data:
                        self.logger.info(
                            "[ws][tunnel] peer closed: %s",
                            format_ws_diagnostics(
                                {
                                    "client": self.client_address,
                                    "upstream_host": upstream_host,
                                    "upstream_port": upstream_port,
                                    "direction": direction,
                                }
                            ),
                        )
                        return
                    target = upstream if sock is self.connection else self.connection
                    if sock is self.connection:
                        for text_message in client_parser.feed(data):
                            self._log_ws_business_message("frontend->backend", text_message)
                    else:
                        for text_message in server_parser.feed(data):
                            self._log_ws_business_message("backend->frontend", text_message)
                    # 非阻塞 socket 写入：循环增量 send，缓冲区满时等待可写后继续，
                    # 跨平台覆盖 Windows WSAEWOULDBLOCK (10035) 与 POSIX EAGAIN/EWOULDBLOCK。
                    pending = data
                    while pending:
                        try:
                            sent = target.send(pending)
                        except OSError as e:
                            would_block = (
                                getattr(e, "winerror", None) == 10035
                                or e.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                            )
                            if not would_block:
                                raise
                            _, writable, _ = select.select([], [target], [], 1.0)
                            if not writable:
                                # 长时间不可写，对端疑似卡死，关闭隧道避免空转
                                self.log_error(
                                    "proxy ws write stalled, closing tunnel: %s",
                                    format_ws_diagnostics(
                                        {
                                            "client": self.client_address,
                                            "upstream_host": upstream_host,
                                            "upstream_port": upstream_port,
                                            "direction": direction,
                                            "pending_bytes": len(pending),
                                        }
                                    ),
                                )
                                return
                            continue
                        pending = pending[sent:]
        except Exception as exc:  # noqa: BLE001
            self.log_error(
                "proxy ws error: %s",
                format_ws_diagnostics(
                    {
                        "client": self.client_address,
                        "upstream_host": upstream_host,
                        "upstream_port": upstream_port,
                    },
                    describe_ws_exception(exc),
                ),
            )
            try:
                self.send_error(502, "proxy ws error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                upstream.close()
            except Exception:  # noqa: BLE001
                pass

    def _dispatch_proxy(self) -> bool:
        # 用户面认证和目录请求仍走同源地址，由本服务转发到集群内的 Identity/Manager。
        path = urlparse(self.path).path
        if path == "/idp" or path.startswith("/idp/"):
            return self._proxy_named_http(self.idp_target, "/idp")
        if path == "/manager-api" or path.startswith("/manager-api/"):
            return self._proxy_named_http(self.manager_api_target, "/manager-api", "/api")
        # /api/sessions* is handled by _is_web_http_route (Gateway Web HTTP), not WebChannel.
        if self._is_web_http_route():
            self._proxy_web_http()
            return True
        if self._is_api_route():
            self._proxy_http()
            return True
        if self._is_ws_route():
            if self._is_websocket_upgrade():
                self._proxy_websocket_tunnel()
            else:
                self.send_error(400, "expected websocket upgrade")
            return True
        return False


    def _is_web_http_route(self) -> bool:
        """Paths served by Gateway Web HTTP; proxy when Ingress still points at Web static."""
        path = urlparse(self.path).path
        if path == "/gateway-api" or path.startswith("/gateway-api/"):
            return True
        if path.startswith("/file-api/") or path.startswith("/share-api/"):
            return True
        if path == "/api/sessions" or path.startswith("/api/sessions/"):
            return True
        if path == "/api/v1" or path.startswith("/api/v1/"):
            return True
        return False

    def _proxy_named_http(self, target: str, prefix: str, replacement: str = "") -> bool:
        if not target:
            self.send_error(502, "proxy target not configured")
            return True
        original_path = self.path
        parsed = urlparse(original_path)
        path = parsed.path
        if path == prefix:
            path = replacement or "/"
        elif replacement:
            path = replacement + path[len(prefix):]
        else:
            path = path[len(prefix):] or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.path = path
        self.__dict__["api_target"] = target
        try:
            self._proxy_http()
        finally:
            self.path = original_path
            self.__dict__.pop("api_target", None)
        return True

    def _proxy_web_http(self) -> None:
        if not self.web_http_target:
            self.send_error(502, "web http proxy target not configured")
            return
        original_path = self.path
        parsed_path = urlparse(original_path).path
        if parsed_path == "/gateway-api" or parsed_path.startswith("/gateway-api/"):
            self.path = f"/api{original_path[len('/gateway-api'):]}"
        self.__dict__["api_target"] = self.web_http_target
        try:
            self._proxy_http()
        finally:
            self.__dict__.pop("api_target", None)
            self.path = original_path

    def do_GET(self) -> None:  # noqa: N802
        if self._is_web_http_route():
            self._proxy_web_http()
            return
        if self._dispatch_proxy():
            return
        if self._is_document_request():
            index = Path(self.directory or os.getcwd()) / "index.html"
            body = _inject_user_web_runtime_config(
                index.read_text(encoding="utf-8"),
                self.login_auth_simulate,
                self.web_transport,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 运行时配置已逐请求注入 index.html，必须禁止缓存，
            # 否则浏览器会复用含过期/跨用户配置的旧响应。
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self._is_web_http_route():
            self._proxy_web_http()
            return
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_PUT(self) -> None:  # noqa: N802
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_PATCH(self) -> None:  # noqa: N802
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self._is_web_http_route():
            self._proxy_web_http()
            return
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_HEAD(self) -> None:  # noqa: N802
        if self._is_web_http_route():
            self._proxy_web_http()
            return
        if self._dispatch_proxy():
            return
        super().do_HEAD()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        self.logger.info("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        self.logger.error("%s - %s", self.address_string(), format % args)

    def _is_document_request(self) -> bool:
        # 与上游一致：仅 Accept 含 text/html 的请求视为文档请求，磁盘上不存在的
        # SPA 子路由也按文档处理（由 do_GET 注入 index.html）。
        # Accept 缺失（如 IAB 首次导航）的 root 请求不走这里，改由 send_head
        # 对 index.html 逐请求注入运行时配置兜底，行为不回退。
        path = urlparse(self.path).path
        if "text/html" not in self.headers.get("Accept", ""):
            return False
        if path in ("/", "/index.html"):
            return True
        rel_path = unquote(path).lstrip("/")
        if not rel_path:
            return True
        base_dir = Path(self.directory or os.getcwd()).resolve()
        target = (base_dir / rel_path).resolve()
        if os.path.commonpath([str(base_dir), str(target)]) != str(base_dir):
            return False
        return not target.exists()

    def send_head(self):
        parsed = urlparse(self.path)
        req_path = unquote(parsed.path)
        rel_path = req_path.lstrip("/") or "index.html"

        base_dir = Path(self.directory or os.getcwd()).resolve()
        target = (base_dir / rel_path).resolve()
        in_base = os.path.commonpath([str(base_dir), str(target)]) == str(base_dir)

        if in_base and target.exists():
            if target.name == "index.html":
                # Accept 缺失（如 IAB 首次导航）时 _is_document_request 为 False
                # 走到这里：index.html 必须逐请求注入运行时配置，禁止裸发。
                body = _inject_user_web_runtime_config(
                    target.read_text(encoding="utf-8"),
                    self.login_auth_simulate,
                    self.web_transport,
                ).encode("utf-8")
                f = io.BytesIO(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Cache-Control", "no-cache, no-store, must-revalidate"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return f
            return super().send_head()

        # Vite base:'./' produces relative asset URLs (./assets/...). When the
        # SPA route is a sub-path (e.g. /chat/new), the browser resolves these
        # to /chat/assets/... which don't exist under dist/. Strip leading path
        # segments and retry from the dist root before falling back to index.html.
        static_exts = (".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".ico",
                       ".webp", ".gif", ".woff", ".woff2", ".ttf", ".eot",
                       ".map", ".json", ".webmanifest")
        if req_path.endswith(static_exts) and "/" in rel_path:
            basename = rel_path.rsplit("/", 1)[-1]
            # Try progressively shorter prefixes (e.g. chat/assets/x.js ->
            # assets/x.js -> x.js).
            parts = rel_path.split("/")
            for i in range(1, len(parts)):
                candidate_rel = "/".join(parts[i:])
                candidate = (base_dir / candidate_rel).resolve()
                cand_in_base = os.path.commonpath(
                    [str(base_dir), str(candidate)]
                ) == str(base_dir)
                if cand_in_base and candidate.exists():
                    self.path = "/" + candidate_rel
                    return super().send_head()

        # SPA fallback: serve index.html with runtime config injected.
        index_path = base_dir / "index.html"
        if index_path.exists():
            body = _inject_user_web_runtime_config(
                index_path.read_text(encoding="utf-8"),
                self.login_auth_simulate,
                self.web_transport,
            ).encode("utf-8")
            f = io.BytesIO(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # 运行时配置已逐请求注入 index.html，必须禁止缓存，
            # 否则浏览器会复用含过期/跨用户配置的旧响应。
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return f

        self.path = "/index.html"
        return super().send_head()


def _normalize_api_target(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"api target must be http/https: {value}")
    return value.rstrip("/")


def _normalize_ws_target(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        value = value.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        parsed = urlparse(value)
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError(f"ws target must be ws/wss/http/https: {value}")
    return value.rstrip("/")


def _setup_logger(logs_root: Path, log_level: str) -> logging.Logger:
    logs_root.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(__name__)
    lg.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    lg.propagate = True
    for h in lg.handlers[:]:
        h.close()
        lg.removeHandler(h)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ws-dev.log 会原样记录前端↔后端业务报文（含 config.validate 等 method 的
    # model_params，其中带 api_key/api_base 等敏感字段），必须挂脱敏 filter，
    # 否则 api_key 明文落盘。propagate 到根 logger 的 handler 虽已脱敏，
    # 但本 handler 自身需独立挂载，才能保证 ws-dev.log 也脱敏。
    privacy_filter = SensitiveDataFilter()

    file_handler = logging.FileHandler(logs_root / "ws-dev.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(privacy_filter)
    lg.addHandler(file_handler)
    return lg


def _wait_for_gateway(ws_target: str, logger: logging.Logger) -> None:
    """Wait for the gateway WebSocket target to become available."""
    parsed = urlparse(ws_target)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
    logger.info("[jiuwenswarm-web] waiting for gateway %s:%s ...", host, port)
    if wait_for_tcp_port(host, port, timeout=15.0, max_attempts=15, target_state="connected"):
        logger.info("[jiuwenswarm-web] gateway available")
    else:
        logger.warning("[jiuwenswarm-web] gateway not available after 15 seconds")


def main() -> None:
    from jiuwenswarm.dotenv_early import get_parsed_dotenv

    # Check if --name bootstrap .env was loaded successfully
    # (parse_dotenv_early() already processed it at module import time)
    # This is just a fallback check for error handling
    _early_name = None
    for i, arg in enumerate(sys.argv):
        if arg == "--name" and i + 1 < len(sys.argv):
            _early_name = sys.argv[i + 1]

    if _early_name and get_parsed_dotenv() is None:
        # Early parsing failed - error was already printed
        raise SystemExit(1)

    # Read defaults from environment variables (for multi-instance support)
    # FRONTEND_PORT is used for this HTTP static server
    # WEB_PORT is the WebChannel websocket endpoint that this server proxies to
    default_host = os.getenv("FRONTEND_HOST", "localhost")
    default_port = int(os.getenv("FRONTEND_PORT", "5173"))
    web_port = os.getenv("WEB_PORT", "19000")  # WebChannel websocket port (proxy target)
    default_proxy = os.getenv("GATEWAY_URL", f"http://127.0.0.1:{web_port}")

    parser = argparse.ArgumentParser(description="Serve JiuwenSwarm frontend static files.")
    parser.add_argument("--host", default=default_host, help="Host to bind.")
    parser.add_argument("--port", type=int, default=default_port, help="Port to bind.")
    parser.add_argument(
        "--dist",
        default=str(_default_dist_dir()),
        help="Path to frontend dist directory.",
    )
    parser.add_argument(
        "--proxy-target",
        default=default_proxy,
        help="Backend base URL for proxy (used as default for api/ws).",
    )
    parser.add_argument(
        "--api-target",
        default="",
        help="Override backend target for /api (http/https).",
    )
    parser.add_argument(
        "--ws-target",
        default="",
        help="Override backend target for /ws (ws/wss/http/https).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level for static web server. e.g. DEBUG/INFO/WARNING/ERROR",
    )
    parser.add_argument(
        "--ws-disable-compress",
        action="store_true",
        help="Disable websocket compression for easier ws req/res/event debug logging.",
    )
    parser.add_argument(
        "--name",
        metavar="<name>",
        help="Start a named instance from instances.yaml.",
    )
    parser.add_argument(
        "--dotenv",
        metavar="<path>",
        help="Load environment from .env file (processed at startup, not used here).",
    )
    args = parser.parse_args()

    install_async_dump_handler("web")

    dist_dir = Path(args.dist).expanduser().resolve()
    if not dist_dir.exists():
        raise SystemExit(f"dist directory not found: {dist_dir}")
    if not dist_dir.is_dir():
        raise SystemExit(f"dist path is not a directory: {dist_dir}")

    try:
        proxy_target = args.proxy_target.strip()
        api_target = _normalize_api_target(args.api_target.strip() or proxy_target)
        ws_target = _normalize_ws_target(args.ws_target.strip() or proxy_target)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    logs_root = get_logs_dir().resolve()
    logger = _setup_logger(logs_root, args.log_level)

    web_port_int = int(os.getenv("WEB_PORT", "19000"))
    web_http_port = resolve_web_http_port(web_port_int)
    web_http_target = f"http://127.0.0.1:{web_http_port}"
    explicit_web_http = os.getenv("GATEWAY_WEB_HTTP_URL", "").strip()
    if explicit_web_http:
        web_http_target = explicit_web_http.rstrip("/")

    class _ConfiguredHandler(_SpaStaticHandler):
        pass

    _ConfiguredHandler.api_target = api_target
    _ConfiguredHandler.ws_target = ws_target
    _ConfiguredHandler.idp_target = os.getenv("USER_WEB_IDP_TARGET", "").strip()
    _ConfiguredHandler.manager_api_target = os.getenv("USER_WEB_MANAGER_TARGET", "").strip()
    enterprise = is_enterprise()
    login_auth_simulate_raw = os.getenv("LOGIN_AUTH_SIMULATE")
    try:
        login_auth_simulate = _parse_login_auth_simulate(
            login_auth_simulate_raw
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _ConfiguredHandler.login_auth_simulate = login_auth_simulate
    web_transport = _parse_web_transport(os.getenv("WEB_TRANSPORT"))
    _ConfiguredHandler.web_transport = web_transport
    _ConfiguredHandler.web_http_target = web_http_target
    _ConfiguredHandler.ws_disable_compress = args.ws_disable_compress
    _ConfiguredHandler.logger = logger
    handler = partial(_ConfiguredHandler, directory=str(dist_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)

    logger.info("[jiuwenswarm-web] serving %s", dist_dir)
    if login_auth_simulate_raw is None or not login_auth_simulate_raw.strip():
        logger.info(
            "[jiuwenswarm-web] LOGIN_AUTH_SIMULATE 未配置，按默认值 true 启用登录认证模拟调试"
        )
    if not enterprise:
        logger.info("[jiuwenswarm-web] personal 模式：跳过企业登录认证")
        if not login_auth_simulate:
            logger.warning(
                "[jiuwenswarm-web] 配置冲突：personal 模式仍将跳过企业登录认证；"
                "LOGIN_AUTH_SIMULATE=false 不会启用正式身份认证"
            )
    elif login_auth_simulate:
        logger.info("[jiuwenswarm-web] 【登录认证模拟调试模式已开启】")
    else:
        logger.info(
            "[jiuwenswarm-web] 【正式身份认证模式，依赖manager ID认证服务】"
        )
        missing_targets = []
        if not _ConfiguredHandler.idp_target:
            missing_targets.append("USER_WEB_IDP_TARGET")
        if not _ConfiguredHandler.manager_api_target:
            missing_targets.append("USER_WEB_MANAGER_TARGET")
        if missing_targets:
            logger.error(
                "[jiuwenswarm-web] 正式登录模式缺少 %s；请配置 manager 认证及业务接口地址",
                "、".join(missing_targets),
            )
        else:
            probes = (
                ("manager ID认证服务", "USER_WEB_IDP_TARGET", _probe_identity_service, _ConfiguredHandler.idp_target),
                ("Manager业务接口", "USER_WEB_MANAGER_TARGET", _probe_manager_service, _ConfiguredHandler.manager_api_target),
            )
            for service_name, config_name, probe, target in probes:
                reachable, detail = probe(target)
                if reachable:
                    logger.info("[jiuwenswarm-web] %s连通性检查通过：%s", service_name, detail)
                else:
                    logger.error(
                        "[jiuwenswarm-web] 当前为正式登录模式，%s暂不可用（%s）；"
                        "请检查 %s、网络和服务状态",
                        service_name,
                        detail,
                        config_name,
                    )
    logger.info("[jiuwenswarm-web] http://%s:%s", args.host, args.port)
    logger.info("[jiuwenswarm-web] /api -> %s", api_target)
    logger.info("[jiuwenswarm-web] /ws  -> %s", ws_target)
    logger.info(
        "[jiuwenswarm-web] /gateway-api,/file-api,/share-api,/api/sessions*,/api/v1* -> %s",
        web_http_target,
    )
    logger.info("[jiuwenswarm-web] ws disable compress: %s", args.ws_disable_compress)

    _wait_for_gateway(ws_target, logger)

    _web_info_path = (get_user_workspace_dir() / ".updates").resolve()
    _web_info_path.mkdir(parents=True, exist_ok=True)
    _web_info_file = _web_info_path / "web_process.json"
    try:
        _web_info_file.write_text(
            json.dumps({"pid": os.getpid(), "argv": sys.argv[:]}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to write web process info: %s", exc)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _web_info_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to remove web process info: %s", exc)
        server.server_close()
        logger.info("[jiuwenswarm-web] server closed")


if __name__ == "__main__":
    main()
