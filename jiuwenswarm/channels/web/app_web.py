# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Serve built frontend static files with optional reverse proxy.

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import logging
import mimetypes
import os
import posixpath
import select
import socket
import ssl
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early
parse_dotenv_early("jiuwenswarm-web")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_insecure_ssl_context, get_ssl_verify
from jiuwenswarm.agents.harness.team.bootstrap import configure_agent_teams_home
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.ws_diagnostics import describe_ws_exception, format_ws_diagnostics
from jiuwenswarm.common.utils import get_agent_root_dir, get_logs_dir, \
    get_agent_sessions_dir, get_user_workspace_dir, \
    wait_for_tcp_port, SensitiveDataFilter
from jiuwenswarm.server.runtime.session.session_history import history_exists, load_history_records

configure_agent_teams_home()

# AgentServer HTTP bridge 基址解析与上传执行统一收口在 gateway/routing 公共模块，
# 供 Web 静态服务 / IM 附件落盘钩子 / Web media.persist 大图分流共用（避免各处
# 重复推导）。此处保留私有别名兼容既有调用点，并 re-export 扩展点。
from jiuwenswarm.gateway.routing.agent_http_bridge import (
    resolve_agent_http_base,
    resolve_agent_http_base_for_token,
    resolve_agent_upload_base,
    set_agent_http_base_resolver,  # noqa: F401  — re-exported 扩展点
)

_resolve_agent_http_base = resolve_agent_http_base
_resolve_agent_upload_base = resolve_agent_upload_base


def _parse_single_byte_range(
    range_header: str,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse one HTTP byte range for the legacy local-download fallback.

    The AgentServer owns the same helper for its HTTP endpoint.  The Web static
    server still needs it when a single-user AgentServer is temporarily down
    and the verified shared-directory fallback serves the file itself.
    """
    if file_size == 0 or not range_header.startswith("bytes=") or "," in range_header:
        return None
    range_value = range_header[6:]
    if "-" not in range_value:
        return None
    start_text, end_text = range_value.split("-", 1)
    if not start_text:
        if not end_text.isascii() or not end_text.isdecimal():
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        return max(0, file_size - suffix_length), file_size - 1
    if not start_text.isascii() or not start_text.isdecimal():
        return None
    if end_text and (not end_text.isascii() or not end_text.isdecimal()):
        return None
    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        return None
    return start, min(end, file_size - 1)


def _get_agent_teams_root() -> Path:
    """Return the agent teams root after dotenv initialization."""
    from openjiuwen.agent_teams.paths import get_agent_teams_home

    return get_agent_teams_home().resolve()


def _get_package_dir() -> Path:
    """Get the jiuwenswarm/channels/web package directory."""
    # app_web.py is at jiuwenswarm/channels/web/app_web.py
    # So parent is jiuwenswarm/channels/web/
    return Path(__file__).resolve().parent


def _default_dist_dir() -> Path:
    """Return default dist directory for frontend static files.

    The version-controlled frontend build ships alongside the code in all
    three run modes, so the package-internal dist is the only correct
    source of truth:
    - source dev: <repo>/jiuwenswarm/channels/web/frontend/dist
      (overwritten by ``npm run build``, loaded live)
    - whl install: <site-packages>/jiuwenswarm/channels/web/frontend/dist
    - frozen exe: <_MEIPASS>/jiuwenswarm/channels/web/frontend/dist

    A user-data dist (e.g. ``~/.jiuwenswarm/channels/web/frontend/dist``)
    is never code, is not maintained by any build/upgrade flow, and once
    stale silently overrides the shipped version — causing the
    "backend upgraded but frontend still old" mismatch. Do not load it.
    """
    package_dir = _get_package_dir()
    dist_dir = package_dir / "frontend" / "dist"
    return dist_dir


def _normalize_lang_suffix(name: str) -> str:
    """将 xxxx_zh.MD / xxxx_en.MD 规范为 xxxx.MD（去除 _zh/_en 后缀）。"""
    stem, suffix = name.rpartition(".")[0], name.rpartition(".")[2]
    suffix_lower = suffix.lower()
    if suffix_lower in ("md", "mdx"):
        stem_lower = stem.lower()
        if stem_lower.endswith("_zh"):
            stem = stem[:-3]
        elif stem_lower.endswith("_en"):
            stem = stem[:-3]
    return f"{stem}.{suffix}" if stem else name


def _generate_agent_data(project_root: Path) -> None:
    """Generate agent/workspace/agent-data.json from agent tree."""
    agent_root = (project_root / "agent").resolve()
    workspace_root = (agent_root / "workspace").resolve()
    output_path = (workspace_root / "agent-data.json").resolve()
    root_folder_key = "__root__"

    if not agent_root.exists():
        raise FileNotFoundError("agent directory not found")
    if not agent_root.is_dir():
        raise NotADirectoryError("agent is not a directory")

    folder_data: dict[str, list[dict[str, str | bool]]] = {}
    seen_paths: dict[str, set[str]] = {}  # folder_key -> normalized paths，用于去重
    for entry in sorted(workspace_root.rglob("*")):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        # Skip files in hidden directories (e.g., .agent_history)
        if any(part.startswith(".") for part in entry.relative_to(workspace_root).parts):
            continue
        relative_folder_path = entry.parent.relative_to(agent_root).as_posix()
        folder_key = root_folder_key if relative_folder_path == "." else relative_folder_path

        display_name = _normalize_lang_suffix(entry.name)
        display_path = (
            f"agent/{relative_folder_path}/{display_name}".replace("/.", "/").replace("//", "/")
            if relative_folder_path != "."
            else f"agent/{display_name}"
        )
        seen = seen_paths.setdefault(folder_key, set())
        if display_path in seen:
            continue  # 同一文件夹内 _zh 与 _en 并存时只保留先出现的
        seen.add(display_path)

        folder_data.setdefault(folder_key, []).append(
            {
                "name": display_name,
                "path": display_path,
                "isMarkdown": entry.suffix.lower() in {".md", ".mdx"},
            }
        )

    sorted_folder_data = {
        folder_key: sorted(files, key=lambda item: item["path"])
        for folder_key, files in sorted(folder_data.items(), key=lambda item: item[0])
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sorted_folder_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _uses_agentos_routing() -> bool:
    """Whether this static service is paired with an AgentOS Gateway.

    The static server owns no per-user route context.  Its historical local
    file APIs are therefore valid only for the shared-directory deployment;
    in AgentOS they must never silently read the Gateway deployment directory.
    """
    try:
        from jiuwenswarm.common.config import get_config_raw

        gateway = (get_config_raw() or {}).get("gateway") or {}
        client = gateway.get("agent_client") or {}
        return str(client.get("type") or "").strip().lower() == "agentos_router"
    except Exception:  # noqa: BLE001
        return False


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
    # control-panel (IAM) base URL for /auth-api/*, e.g. http://127.0.0.1:8090
    iam_target = ""
    # 一体机模式标记: jiuwenswarm-web --remote 开启。
    # 仅一体机场景为 True; 普通部署为 False。前端据此决定是否显示登出按钮。
    remote_mode = False
    ws_disable_compress = False

    # --- /auth-api cookie-based auth bridge ---
    # access_token 实测 TTL 15min(900s), refresh_token 实测 7d(604800s)。
    _AUTH_COOKIE_NAME = "jw_token"
    _AUTH_COOKIE_MAX_AGE = 900
    _AUTH_REFRESH_COOKIE_NAME = "jw_refresh"
    _AUTH_REFRESH_MAX_AGE = 7 * 24 * 3600
    _AUTH_API_PREFIX = "/auth-api"
    project_root = get_user_workspace_dir()
    workspace_root = get_agent_root_dir()
    agent_teams_root = _get_agent_teams_root()
    logs_root = get_logs_dir()
    auto_harness_root = project_root / "auto-harness"
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

    def _is_auth_api_route(self) -> bool:
        # /auth-api/* 反代到 control-panel (IAM),浏览器侧走 HttpOnly cookie
        return urlparse(self.path).path.startswith(self._AUTH_API_PREFIX + "/")

    def _is_web_config_route(self) -> bool:
        # /api/web-config: 本地端点, 返回 web 启动配置(如一体机模式标记)。
        # 必须在 _is_api_route 之前判定, 否则会被当 /api/* 反代到 gateway。
        return urlparse(self.path).path == "/api/web-config"

    def _handle_web_config(self) -> None:
        """返回 web 启动配置 JSON, 供前端探测(如一体机模式 → 显示登出按钮, IAM 是否启用)。"""
        payload = {
            "remote": bool(self.remote_mode),
            "iam_enabled": bool(self.iam_target),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_ws_route(self) -> bool:
        return urlparse(self.path).path.startswith("/ws")

    def _is_file_api_route(self) -> bool:
        return urlparse(self.path).path.startswith("/file-api/")

    def _is_share_api_route(self) -> bool:
        return urlparse(self.path).path.startswith("/share-api/")

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
            resp_body = resp.read()

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in self._HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp_body)
        except Exception as exc:  # noqa: BLE001
            self.log_error("proxy http error: %s", exc)
            self.send_error(502, "proxy http error")
        finally:
            conn.close()

    def _proxy_auth_http(self) -> None:
        """反向代理 /auth-api/* 到 control-panel (IAM)。

        1. 路径改写: /auth-api/... -> /api/... (control-panel 的路由前缀是 /api)
        2. 登录响应拦截: 从 JSON body 取 access_token/refresh_token, 写 HttpOnly cookie
        3. 非 login 请求: control-panel 用 HTTPBearer 只认 Authorization 头,
           故从 jw_token cookie 取 token 注入 Authorization: Bearer
        4. 屏蔽上游 Set-Cookie (control-panel 不发, 但防御性)
        """
        if not self.iam_target:
            self.send_error(503, "iam target not configured")
            return
        parsed = urlparse(self.iam_target)
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

        # 路径改写: /auth-api/v1/auth/login -> /api/v1/auth/login
        # 用 posixpath 库函数构造 URL 路径段 (G.FIO.05: 避免字符串拼接构造路径,
        # 用库函数屏蔽 OS 差异; posixpath 跨平台恒为正斜杠, 适合 URL 路径)。
        req_path = urlparse(self.path).path
        auth_prefix_dir = posixpath.join(self._AUTH_API_PREFIX, "")  # /auth-api/
        upstream_path = auth_prefix_dir  # 不会命中
        if req_path.startswith(auth_prefix_dir):
            tail = req_path[len(self._AUTH_API_PREFIX) + 1:]
            upstream_path = posixpath.join("/api", tail)
        if urlparse(self.path).query:
            upstream_path += "?" + urlparse(self.path).query
        is_login = upstream_path.rstrip("/").endswith("/api/v1/auth/login")
        is_logout = upstream_path.rstrip("/").endswith("/api/v1/auth/logout")

        try:
            body = b""
            if self.command not in ("GET", "HEAD"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""

            # logout 要求 body 带 refresh_token, 但 token 在 HttpOnly cookie 里前端拿不到。
            # 反代层从 jw_refresh cookie 取 refresh_token 注入 body, 前端只需 POST 空 body。
            if is_logout:
                refresh_cookie = self._get_auth_cookie(self._AUTH_REFRESH_COOKIE_NAME)
                if refresh_cookie:
                    try:
                        payload = json.loads(body.decode("utf-8")) if body else {}
                    except Exception:  # noqa: BLE001
                        payload = {}
                    if not payload.get("refresh_token"):
                        payload["refresh_token"] = refresh_cookie
                    body = json.dumps(payload).encode("utf-8")

            forward_headers: dict[str, str] = {}
            for key, value in self.headers.items():
                kl = key.lower()
                if kl in self._HOP_BY_HOP_HEADERS:
                    continue
                if kl == "host":
                    continue
                # 上游 Set-Cookie 由我们接管 (cookie 化), 屏蔽转发
                if kl == "cookie":
                    continue
                # Content-Length 由 body 决定, 屏蔽原值避免与改写后的 body 长度冲突
                if kl == "content-length":
                    continue
                forward_headers[key] = value
            forward_headers["Host"] = parsed.netloc

            # control-panel 只认 Authorization: Bearer, 不读 cookie。
            # 非 login 请求: 从 jw_token cookie 取 token 注入头。
            if not is_login and not any(k.lower() == "authorization" for k in forward_headers):
                cookie_token = self._get_auth_cookie()
                if cookie_token:
                    forward_headers["Authorization"] = f"Bearer {cookie_token}"

            # 由当前 body 长度决定 Content-Length (login 空 body / logout 注入 refresh_token 后已改写)。
            if self.command not in ("GET", "HEAD"):
                forward_headers["Content-Length"] = str(len(body))

            conn.request(self.command, upstream_path, body=body, headers=forward_headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            self.send_response(resp.status, resp.reason)
            # 登录成功: 拦截 body 写 cookie
            wrote_cookies = False
            if is_login and resp.status == 200:
                try:
                    payload = json.loads(resp_body.decode("utf-8"))
                    access_token = payload.get("access_token") or (payload.get("data") or {}).get("access_token")
                    refresh_token = payload.get("refresh_token") or (payload.get("data") or {}).get("refresh_token")
                    if access_token:
                        self._set_auth_cookie(access_token)
                        wrote_cookies = True
                    if refresh_token:
                        self._set_auth_cookie(refresh_token, self._AUTH_REFRESH_COOKIE_NAME, self._AUTH_REFRESH_MAX_AGE)
                        wrote_cookies = True
                except Exception:  # noqa: BLE001
                    pass
            # 转发响应头, 但 Set-Cookie 由我们接管
            for key, value in resp.getheaders():
                kl = key.lower()
                if kl in self._HOP_BY_HOP_HEADERS:
                    continue
                if kl == "set-cookie":
                    continue  # 已由 _set_auth_cookie / _clear_auth_cookie 接管
                self.send_header(key, value)
            # 登出: 无论上游返回啥 (control-panel logout 可能 500), 都清 HttpOnly cookie。
            # 前端 JS 清不掉 HttpOnly, 必须由后端发过期 Set-Cookie; 清后 reload 即回登录页。
            if is_logout:
                self._clear_auth_cookie()
                self._clear_auth_cookie(self._AUTH_REFRESH_COOKIE_NAME)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp_body)
        except Exception as exc:  # noqa: BLE001
            self.log_error("proxy auth http error: %s", exc)
            self.send_error(502, "proxy auth http error")
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
            # 注入 cookie 里的 access_token 作为 ?token=, gateway 鉴权优先级最高。
            # 浏览器 WS 无法带 Authorization 头, 故走 query 注入。
            upstream_path = self.path
            token = self._get_auth_cookie()
            if token:
                if "?" in upstream_path:
                    base, q = upstream_path.split("?", 1)
                    upstream_path = f"{base}?token={quote(token)}&{q}"
                else:
                    upstream_path = f"{upstream_path}?token={quote(token)}"
            request_lines = [f"{self.command} {upstream_path} HTTP/1.1"]
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
        if self._is_auth_api_route():
            # /auth-api/* 优先, 反代到 control-panel (IAM), 走 cookie 桥接
            self._proxy_auth_http()
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

    @staticmethod
    def _is_markdown(path_obj: Path) -> bool:
        ext = path_obj.suffix.lower()
        return ext in {".md", ".mdx"}

    @classmethod
    def _is_path_under_allowed_root(cls, target: Path) -> bool:
        target_resolved = target.resolve()
        try:
            in_workspace = (
                os.path.commonpath([str(cls.workspace_root), str(target_resolved)])
                == str(cls.workspace_root)
            )
            in_agent_teams = (
                os.path.commonpath([str(cls.agent_teams_root), str(target_resolved)]) == str(cls.agent_teams_root)
            )
            in_logs = os.path.commonpath([str(cls.logs_root), str(target_resolved)]) == str(cls.logs_root)
            in_auto_harness = \
                os.path.commonpath([str(cls.auto_harness_root), str(target_resolved)]) == str(cls.auto_harness_root)
            return in_workspace or in_agent_teams or in_logs or in_auto_harness
        except ValueError:
            return False

    def _write_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    @staticmethod
    def _parse_query(query: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        if not query:
            return parsed
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
            else:
                k, v = pair, ""
            parsed[unquote(k)] = unquote(v)
        return parsed

    # --- /auth-api cookie helpers ---
    def _get_auth_cookie(self, name: str | None = None) -> str:
        """从 Cookie 头读取指定鉴权 cookie,默认 access token。"""
        cookie_name = name or self._AUTH_COOKIE_NAME
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            if k.strip() == cookie_name:
                return unquote(v)
        return ""

    def _set_auth_cookie(self, token: str, name: str | None = None, max_age: int | None = None) -> None:
        """写 HttpOnly + SameSite=Lax 的 Set-Cookie,前端 JS 读不到 token 明文。"""
        cookie_name = name or self._AUTH_COOKIE_NAME
        cookie_age = max_age if max_age is not None else self._AUTH_COOKIE_MAX_AGE
        self.send_header(
            "Set-Cookie",
            f"{cookie_name}={quote(token, safe='')}; Path=/; HttpOnly; SameSite=Lax; Max-Age={cookie_age}",
        )

    def _clear_auth_cookie(self, name: str | None = None) -> None:
        """发过期 Set-Cookie 清掉 HttpOnly cookie (前端 JS 清不掉 HttpOnly, 必须后端清)。"""
        cookie_name = name or self._AUTH_COOKIE_NAME
        self.send_header(
            "Set-Cookie",
            f"{cookie_name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        )

    def _resolve_session_title(self, session_dir: Path, history: list[dict[str, Any]]) -> str:
        metadata_path = session_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                title = metadata.get("title") if isinstance(metadata, dict) else None
                if isinstance(title, str) and title.strip():
                    return title.strip()
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass
        for record in history:
            if record.get("role") == "user":
                content = record.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip().replace("\n", " ")[:80]
        return session_dir.name

    def _build_share_snapshot(
        self,
        *,
        session_id: str,
    ) -> tuple[dict[str, Any], str]:
        sessions_root = get_agent_sessions_dir().resolve()
        session_dir = (sessions_root / session_id).resolve()
        try:
            if os.path.commonpath([str(sessions_root), str(session_dir)]) != str(sessions_root):
                raise FileNotFoundError("history_not_found")
        except ValueError as exc:
            raise FileNotFoundError("history_not_found") from exc

        if not session_dir.exists() or not history_exists(session_id):
            raise FileNotFoundError("history_not_found")
        try:
            history_raw = load_history_records(session_id)
        except Exception as exc:
            raise ValueError("invalid_history_json") from exc
        if not isinstance(history_raw, list):
            raise ValueError("invalid_history_shape")

        history = [item for item in history_raw if isinstance(item, dict)]
        exported_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"jiuwenswarm-share-{timestamp}.png"
        snapshot = {
            "session_id": session_id,
            "metadata": {
                "title": self._resolve_session_title(session_dir, history),
                "exported_at": exported_at,
                "filename": filename,
            },
            "records": history_raw,
        }
        return snapshot, filename

    def _handle_share_api_get(self, parsed) -> None:
        if _uses_agentos_routing():
            self._write_json(503, {"error": "agentserver_file_rpc_required"})
            return
        if parsed.path != "/share-api/snapshot":
            self._write_json(404, {"error": "not_found"})
            return
        query = self._parse_query(parsed.query)
        session_id = query.get("session_id", "").strip()
        if not session_id:
            self._write_json(400, {"error": "missing_session_id"})
            return
        try:
            snapshot, filename = self._build_share_snapshot(
                session_id=session_id,
            )
        except FileNotFoundError:
            self._write_json(404, {"error": "history_not_found"})
            return
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        self._write_json(200, {"filename": filename, "snapshot": snapshot})

    def _handle_file_api_get(self, parsed) -> None:
        path = parsed.path
        query = self._parse_query(parsed.query)

        # All non-token file APIs below access ``project_root`` directly.
        # That root belongs to the Gateway deployment in AgentOS, not to the
        # routed user's mounted AgentServer directory.  Do not provide a
        # deployment-directory fallback while the E2A file RPC endpoint is
        # being used by AgentOS clients.
        if _uses_agentos_routing() and path in {
            "/file-api/list-markdown",
            "/file-api/list-files",
            "/file-api/raw-file",
            "/file-api/file-content",
        }:
            self._write_json(503, {"error": "agentserver_file_rpc_required"})
            return

        if path == "/file-api/list-markdown":
            dir_arg = query.get("dir", "")
            if not dir_arg:
                self._write_json(400, {"error": "missing_dir"})
                return
            full_dir = (self.project_root / dir_arg).resolve()
            if not self._is_path_under_allowed_root(full_dir):
                self._write_json(403, {"error": "forbidden_dir"})
                return
            if not full_dir.exists() or not full_dir.is_dir():
                self._write_json(200, {"files": []})
                return
            files = []
            for entry in sorted(full_dir.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_file() or not self._is_markdown(entry):
                    continue
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry.relative_to(self.project_root)),
                    }
                )
            self._write_json(200, {"files": files})
            return

        if path == "/file-api/list-files":
            dir_arg = query.get("dir", "")
            if not dir_arg:
                self._write_json(400, {"error": "missing_dir"})
                return
            full_dir = (self.project_root / dir_arg).resolve()
            if not self._is_path_under_allowed_root(full_dir):
                self._write_json(403, {"error": "forbidden_dir"})
                return
            if not full_dir.exists() or not full_dir.is_dir():
                self._write_json(200, {"files": []})
                return
            files = []
            entries = sorted(
                full_dir.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
            for entry in entries:
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry.relative_to(self.project_root)),
                        "isMarkdown": self._is_markdown(entry) if entry.is_file() else False,
                        "isDirectory": entry.is_dir(),
                    }
                )
            self._write_json(200, {"files": files})
            return

        if path == "/file-api/raw-file":
            file_arg = query.get("path", "")
            if not file_arg:
                self._write_json(400, {"error": "missing_file_path"})
                return
            full_path = (self.project_root / file_arg).resolve()
            if not self._is_path_under_allowed_root(full_path):
                self._write_json(403, {"error": "forbidden_path"})
                return
            if not full_path.is_file():
                self._write_json(404, {"error": "file_not_found"})
                return

            mime_type, _ = mimetypes.guess_type(full_path.name)
            self.send_response(200)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(full_path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                with full_path.open("rb") as file_obj:
                    while True:
                        chunk = file_obj.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            return

        if path == "/file-api/file-content":
            file_arg = query.get("path", "")
            encoding_arg = query.get("encoding", "utf-8")
            if not file_arg:
                self._write_json(400, {"error": "missing_file_path"})
                return
            full_path = (self.project_root / file_arg).resolve()
            if not self._is_path_under_allowed_root(full_path):
                self._write_json(403, {"error": "forbidden_path"})
                return
            if not full_path.exists():
                if file_arg.replace("\\", "/") == "agent/workspace/agent-data.json":
                    try:
                        _generate_agent_data(self.project_root)
                    except Exception as exc:  # noqa: BLE001
                        self._write_json(500, {"error": "generate_failed", "detail": str(exc)})
                        return
                if not full_path.exists():
                    self._write_json(404, {"error": "file_not_found", "fullPath": str(full_path)})
                    return

            def read_file_with_encoding(file_path: Path, encoding: str) -> tuple[str, str]:
                if encoding == "auto":
                    import charset_normalizer
                    raw_data = file_path.read_bytes()
                    detected = charset_normalizer.from_bytes(raw_data).best()
                    if detected is None:
                        detected_encoding = "utf-8"
                    else:
                        detected_encoding = detected.encoding or "utf-8"
                    try:
                        return raw_data.decode(detected_encoding), detected_encoding
                    except (UnicodeDecodeError, LookupError) as decode_exc:
                        for try_encoding in ["gbk", "gb2312", "big5", "shift_jis", "euc_kr"]:
                            try:
                                return raw_data.decode(try_encoding), try_encoding
                            except (UnicodeDecodeError, LookupError):
                                continue
                        raise OSError("Unable to decode file with any known encoding") from decode_exc
                else:
                    return file_path.read_text(encoding=encoding), encoding

            try:
                data, used_encoding = read_file_with_encoding(full_path, encoding_arg)
            except OSError as exc:
                self._write_json(500, {"error": str(exc)})
                return
            body = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Original-Encoding", used_encoding)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        if path == "/file-api/download":
            self._handle_file_download(query)
            return

        if path == "/file-api/ws-debug-config":
            self._write_json(
                200,
                {
                    "wsDisableCompress": bool(type(self).ws_disable_compress),
                },
            )
            return

        self._write_json(404, {"error": "not_found"})

    def _handle_file_download(self, query: dict[str, str]) -> None:
        """处理 ``/file-api/download``（Phase 2 迁移：代理目标 AgentServer）。

        - Gateway 侧保留 token 校验作为第一道防线；本进程 secret 能校验通过的
          token（单机同目录 / 部署侧签发）保持原有下载范围；
        - 内容与路径判定由目标 AgentServer 在其注入目录执行。若 token 能由
          单用户本地 secret 验证，则 AgentServer 不可达时可安全回退到同一用户
          目录的本地读取；AgentOS token 无法由 Gateway 验证，因此始终不回退。
        """
        token = query.get("token", "")
        if not token:
            self._write_json(400, {"error": "missing_token"})
            return

        # AgentOS 的浏览器入口是本静态服务（通常 5173），而实际能够按用户
        # sandbox 下载文件的是 WebChannel（api_target，通常 19000）。send_file
        # 已在 URL 中附带 user_id；直接将同一请求转发到 WebChannel，避免再按
        # 单机 AgentServer HTTP bridge（18092）解析并错误返回 503。
        if _uses_agentos_routing() and query.get("user_id", "").strip():
            self._proxy_http()
            return

        try:
            from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                PURPOSE_SKILL_CONTENT_IMAGE,
                validate_file_download_token,
            )
            from jiuwenswarm.server.runtime.skill.skill_content_images import (
                extract_request_session_id,
                resolve_skill_content_image_file,
                validate_skill_content_image_payload,
            )
            from jiuwenswarm.server.runtime.skill.skill_files import SkillFilesError
        except ImportError:
            self._write_json(500, {"error": "download_module_unavailable"})
            return

        payload = validate_file_download_token(token)
        # Skill 正文图片 token intentionally does not carry an absolute path.
        # In the legacy shared-directory layout it must therefore be resolved
        # through the skill manifest before entering the generic file bridge.
        if payload is not None and str(payload.get("purpose") or "") == PURPOSE_SKILL_CONTENT_IMAGE:
            request_sid = extract_request_session_id(query=query, headers=self.headers)
            error = validate_skill_content_image_payload(
                payload, request_session_id=request_sid
            )
            if error:
                self._write_json(403, {"error": error})
                return
            version_value = payload.get("version")
            version = str(version_value).strip() if version_value else None
            try:
                file_path, _ = resolve_skill_content_image_file(
                    name=str(payload.get("name") or ""),
                    version=version,
                    relative_path=str(payload.get("relative_path") or ""),
                )
            except SkillFilesError as exc:
                code = str(exc.code or "")
                self._write_json(
                    404 if code in {"SKILL_NOT_FOUND", "SKILL_VERSION_NOT_FOUND"} else 403,
                    {
                        "error": "file_not_found"
                        if code in {"SKILL_NOT_FOUND", "SKILL_VERSION_NOT_FOUND"}
                        else "forbidden_path",
                        "code": code,
                    },
                )
                return
            self._serve_verified_local_download(str(file_path), inline=True)
            return
        if payload is not None:
            # 本进程 secret 能校验并不意味着 token 的路径位于 Gateway 宿主机。
            # AgentOS 的部署与用户 AgentServer 可能共用下载密钥；此时 token
            # 仍可被 Gateway 验证，但 ``path`` 是用户容器内路径，必须先按 token
            # 携带的 bridge 地址代理给目标 AgentServer，不能在这里误判 404。
            file_path = str(payload.get("path") or "")
            has_target_bridge = bool(
                str(payload.get("download_http_base") or "").strip()
            )
            if not file_path or (not has_target_bridge and not os.path.isfile(file_path)):
                self._write_json(404, {"error": "file_not_found"})
                return

            # Legacy Gateway and AgentServer deliberately share this verified
            # directory.  Do not proxy these downloads to the AgentServer WS
            # port: it is not an HTTP file server.  Routed tokens carry their
            # explicit bridge address and continue through the proxy below.
            if not has_target_bridge:
                raw_inline = query.get("inline")
                if isinstance(raw_inline, list):
                    raw_inline = raw_inline[0] if raw_inline else ""
                inline = str(raw_inline or "").strip().lower() in {"1", "true"}
                _SpaStaticHandler._serve_verified_local_download(
                    self, file_path, inline=inline
                )
                return

        # 代理到目标 AgentServer（AgentOS 下 token 由用户 AgentServer secret 签发，
        # Gateway 校验必然失败，仍转发由 AgentServer 做最终校验与路径判定）。
        raw_inline = query.get("inline")
        if isinstance(raw_inline, list):
            raw_inline = raw_inline[0] if raw_inline else ""
        inline = str(raw_inline or "").strip().lower() in {"1", "true"}
        if self._proxy_file_download(token, inline=inline):
            return

        # In the legacy single-user layout Gateway and AgentServer share the
        # same verified user directory.  Preserve the pre-AgentOS availability
        # contract only when that local file is actually present.  A token for
        # a routed AgentServer may be locally verifiable when deployments reuse
        # the same secret, but it must never fall back to the Gateway directory.
        if payload is not None and os.path.isfile(file_path):
            _SpaStaticHandler._serve_verified_local_download(
                self, file_path, inline=inline
            )
            return

        self.logger.warning("[file-api/download] 目标 AgentServer 不可达: token=%s...", token[:8])
        self._write_json(503, {"error": "agent_server_unavailable"})

    def _serve_verified_local_download(self, file_path: str, *, inline: bool) -> None:
        """Stream a token-verified legacy single-user file with Range support."""
        try:
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            byte_range = None
            range_header = self.headers.get("Range")
            if range_header:
                byte_range = _parse_single_byte_range(range_header, file_size)
                if byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
            start, end = byte_range or (0, max(0, file_size - 1))
            content_length = 0 if file_size == 0 else end - start + 1
            self.send_response(206 if byte_range is not None else 200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Disposition",
                f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(file_name, safe='')}",
            )
            self.send_header("Cache-Control", "no-store")
            if byte_range is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()
            if self.command != "HEAD":
                with open(file_path, "rb") as file_handle:
                    file_handle.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk = file_handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except Exception as exc:  # noqa: BLE001
            self.log_error("file download error: %s", exc)
            self._write_json(500, {"error": "download_failed", "detail": str(exc)})

    def _proxy_file_download(self, token: str, *, inline: bool = False) -> bool:
        """把下载请求代理到目标 AgentServer 的 HTTP 端点并流式回传。

        - 透传 ``inline`` 查询参数与 ``Range`` 请求头（AgentServer 侧解析）；
        - 透传上游全部相关响应头（Content-Type/Length/Disposition、
          Accept-Ranges/Content-Range、Cache-Control）与状态码（200/206/416）；
        - 上游 4xx/5xx（如 AgentServer 校验 token 失败 403、文件缺失 404）
          原样透传，避免误报 503；仅网络级不可达返回 False（→ 503）。
        """
        try:
            import urllib.error
            import urllib.request
            import shutil

            base = resolve_agent_http_base_for_token(
                token, endpoint="download"
            ) or _resolve_agent_http_base()
            query_parts = [f"token={quote(token, safe='')}"]
            if inline:
                query_parts.append("inline=1")
            url = f"{base}/file-api/download?{'&'.join(query_parts)}"
            headers: dict[str, str] = {}
            range_header = self.headers.get("Range")
            if range_header:
                headers["Range"] = range_header
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as upstream:  # noqa: S310
                status = int(getattr(upstream, "status", 200))
                self.send_response(status)
                for header_name in (
                    "Content-Type",
                    "Content-Length",
                    "Content-Disposition",
                    "Accept-Ranges",
                    "Content-Range",
                    "Cache-Control",
                ):
                    value = upstream.headers.get(header_name)
                    if value:
                        self.send_header(header_name, value)
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(upstream, self.wfile, length=65536)
            return True
        except urllib.error.HTTPError as exc:
            # 上游返回 4xx/5xx（403 token 无效/越界、404 缺失、416 非法 Range 等）：
            # 透传状态码与 body，保持错误语义不混淆为 503。
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                body = b""
            try:
                self.send_response(int(exc.code))
                self.send_header(
                    "Content-Type",
                    exc.headers.get("Content-Type", "application/json; charset=utf-8")
                    if exc.headers
                    else "application/json; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(body)))
                if exc.headers and exc.headers.get("Content-Range"):
                    self.send_header("Content-Range", exc.headers.get("Content-Range"))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[file-api/download] 代理转发失败: %s", exc)
            return False

    def _handle_file_upload_proxy(self, parsed) -> None:
        """处理 ``POST /file-api/upload``（Phase 2 HTTP bridge 上传代理）。

        读取客户端上传 body 后转发到目标 AgentServer 的上传端点（不落盘
        Gateway），由 AgentServer 校验 upload token 并落盘注入目录。
        AgentServer 不可达 → 503 可重试错误（方案 §8 禁止本地 fallback）。
        """
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        token = (query.get("token") or [""])[0]
        if not token:
            self._write_json(400, {"error": "missing_token"})
            return
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._write_json(400, {"error": "invalid_content_length"})
            return
        if content_length <= 0:
            self._write_json(400, {"error": "empty_body"})
            return

        # The legacy Gateway and AgentServer share one verified user directory.
        # Its upload token has no routed HTTP base, so persist it locally rather
        # than forwarding to the AgentServer's nonexistent HTTP upload listener.
        if not _uses_agentos_routing():
            try:
                from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                    validate_file_download_token,
                )
                from jiuwenswarm.common.utils import (
                    get_user_workspace_dir as _get_user_workspace_dir,
                )

                payload = validate_file_download_token(token)
                target_rel_path = str((payload or {}).get("path") or "").strip()
                if payload is not None and target_rel_path and not payload.get("upload_http_base"):
                    root = Path(_get_user_workspace_dir()).resolve()
                    target = (root / target_rel_path).resolve()
                    if root not in target.parents:
                        self._write_json(403, {"error": "forbidden_path"})
                        return
                    body = self.rfile.read(content_length)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    self._write_json(200, {"path": str(target), "size": len(body)})
                    return
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[file-api/upload] local legacy persist failed: %s", exc)
                self._write_json(500, {"error": "upload_failed"})
                return

        import urllib.error
        import urllib.request

        base = resolve_agent_http_base_for_token(
            token, endpoint="upload"
        ) or _resolve_agent_upload_base()
        url = f"{base}/file-api/upload?token={quote(token, safe='')}"
        body = self.rfile.read(content_length)
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(body))},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as upstream:  # noqa: S310
                status = int(getattr(upstream, "status", 200))
                raw = upstream.read()
        except urllib.error.HTTPError as exc:
            # 上游校验/落盘失败（403 越界、400 非法 token 等）：透传状态码与 body
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            status = int(exc.code)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[file-api/upload] 代理转发失败: %s", exc)
            self._write_json(503, {"error": "agent_server_unavailable"})
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _handle_file_api_post(self, parsed) -> None:
        if parsed.path == "/file-api/skills/import":
            self._handle_skills_import_upload()
            return
        if parsed.path == "/file-api/skills/create-from-knowledge":
            self._handle_skills_create_from_knowledge()
            return

        if _uses_agentos_routing() and parsed.path in {
            "/file-api/rebuild-agent-data",
            "/file-api/file-content",
        }:
            self._write_json(503, {"error": "agentserver_file_rpc_required"})
            return
        if parsed.path == "/file-api/rebuild-agent-data":
            try:
                _generate_agent_data(self.project_root)
            except Exception as exc:  # noqa: BLE001
                self._write_json(500, {"error": "rebuild_failed", "detail": str(exc)})
                return

            self._write_json(200, {"ok": True})
            return

        if parsed.path == "/file-api/upload":
            self._handle_file_upload_proxy(parsed)
            return

        if parsed.path == "/file-api/file-content":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except json.JSONDecodeError:
                self._write_json(400, {"error": "invalid_json"})
                return

            request_path = payload.get("path")
            request_content = payload.get("content")
            if not isinstance(request_path, str) or not request_path.strip():
                self._write_json(400, {"error": "missing_file_path"})
                return
            if not isinstance(request_content, str):
                self._write_json(400, {"error": "missing_file_content"})
                return

            full_path = (self.project_root / request_path).resolve()
            if not self._is_path_under_allowed_root(full_path):
                self._write_json(403, {"error": "forbidden_path"})
                return
            if not self._is_markdown(full_path):
                self._write_json(400, {"error": "only_markdown_supported"})
                return
            if not full_path.exists():
                self._write_json(404, {"error": "file_not_found"})
                return

            try:
                full_path.write_text(request_content, encoding="utf-8")
            except OSError as exc:
                self._write_json(500, {"error": str(exc)})
                return
            self._write_json(200, {"ok": True})
            return

        if parsed.path == "/file-api/ws-debug-config":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except json.JSONDecodeError:
                self._write_json(400, {"error": "invalid_json"})
                return

            ws_disable_compress = payload.get("wsDisableCompress")
            if not isinstance(ws_disable_compress, bool):
                self._write_json(400, {"error": "invalid_ws_disable_compress"})
                return

            type(self).ws_disable_compress = ws_disable_compress
            self.logger.info(
                "[jiuwenswarm-web] ws disable compress updated: %s",
                ws_disable_compress,
            )
            self._write_json(200, {"ok": True, "wsDisableCompress": ws_disable_compress})
            return

        self._write_json(404, {"error": "not_found"})

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _handle_skills_import_upload(self) -> None:
        try:
            from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
                handle_skills_import_http,
            )
        except ImportError as exc:
            self.log_error("skills import module unavailable: %s", exc)
            self._write_json(500, {"code": "SKILL_INVALID_PACKAGE", "message": "import 模块不可用", "error": "import 模块不可用"})
            return
        content_type = self.headers.get("Content-Type", "")
        body = self._read_request_body()
        status, payload = handle_skills_import_http(
            content_type=content_type,
            body=body,
            use_local_manager=True,
        )
        self._write_json(status, payload)

    def _handle_skills_create_from_knowledge(self) -> None:
        try:
            from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
                handle_skills_create_from_knowledge_http,
            )
        except ImportError as exc:
            self.log_error("skills create-from-knowledge module unavailable: %s", exc)
            self._write_json(
                500,
                {
                    "code": "SKILL_INVALID_PACKAGE",
                    "message": "create-from-knowledge 模块不可用",
                    "error": "create-from-knowledge 模块不可用",
                },
            )
            return
        content_type = self.headers.get("Content-Type", "")
        body = self._read_request_body()
        status, payload = handle_skills_create_from_knowledge_http(
            content_type=content_type,
            body=body,
        )
        self._write_json(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self._is_share_api_route():
            self._handle_share_api_get(parsed)
            return
        if self._is_file_api_route():
            self._handle_file_api_get(parsed)
            return
        if self._is_web_config_route():
            self._handle_web_config()
            return
        if self._dispatch_proxy():
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self._is_file_api_route():
            self._handle_file_api_post(parsed)
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
        if self._dispatch_proxy():
            return
        self.send_error(405, "method not allowed")

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self._is_file_api_route():
            self._handle_file_api_get(parsed)
            return
        if self._dispatch_proxy():
            return
        super().do_HEAD()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        self.logger.info("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        self.logger.error("%s - %s", self.address_string(), format % args)

    def send_head(self):
        parsed = urlparse(self.path)
        req_path = unquote(parsed.path)
        rel_path = req_path.lstrip("/") or "index.html"

        base_dir = Path(self.directory or os.getcwd()).resolve()
        target = (base_dir / rel_path).resolve()
        in_base = os.path.commonpath([str(base_dir), str(target)]) == str(base_dir)

        if in_base and target.exists():
            return super().send_head()

        self.path = "/index.html"
        return super().send_head()


def _normalize_api_target(value: str) -> str:
    if not value:
        return ""
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
    # control-panel (IAM) 默认地址; 部署脚本经 IAM_AUTH_SERVICE_URL 注入, 否则留空(无鉴权, 直接进入主页面)
    default_iam = os.getenv("IAM_AUTH_SERVICE_URL", "")

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
        "--iam-target",
        default=default_iam,
        help="control-panel (IAM) base URL for /auth-api/*, e.g. http://127.0.0.1:8090.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="All-in-one (yitiji) mode flag. Enables frontend logout button. Default off.",
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
        iam_target = _normalize_api_target(args.iam_target.strip() or default_iam)
        remote_mode = bool(args.remote)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    # default_project_root should be the user workspace root (~/.jiuwenswarm in package mode)
    # get_root_dir() already handles this correctly
    default_project_root = get_user_workspace_dir()

    project_root = default_project_root
    workspace_root = (project_root / "agent").resolve()
    agent_teams_root = _get_agent_teams_root()
    logs_root = get_logs_dir().resolve()
    logger = _setup_logger(logs_root, args.log_level)

    class _ConfiguredHandler(_SpaStaticHandler):
        pass

    _ConfiguredHandler.api_target = api_target
    _ConfiguredHandler.ws_target = ws_target
    _ConfiguredHandler.iam_target = iam_target
    _ConfiguredHandler.remote_mode = remote_mode
    _ConfiguredHandler.ws_disable_compress = args.ws_disable_compress
    _ConfiguredHandler.project_root = project_root
    _ConfiguredHandler.workspace_root = workspace_root
    _ConfiguredHandler.agent_teams_root = agent_teams_root
    _ConfiguredHandler.logs_root = logs_root
    _ConfiguredHandler.logger = logger
    handler = partial(_ConfiguredHandler, directory=str(dist_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)

    logger.info("[jiuwenswarm-web] serving %s", dist_dir)
    logger.info("[jiuwenswarm-web] http://%s:%s", args.host, args.port)
    logger.info("[jiuwenswarm-web] /api -> %s", api_target)
    logger.info("[jiuwenswarm-web] /ws  -> %s", ws_target)
    logger.info("[jiuwenswarm-web] /auth-api -> %s", iam_target)
    logger.info("[jiuwenswarm-web] all-in-one (remote) mode: %s", remote_mode)
    logger.info("[jiuwenswarm-web] ws disable compress: %s", args.ws_disable_compress)
    logger.info("[jiuwenswarm-web] /file-api roots -> %s, %s, %s", workspace_root, agent_teams_root, logs_root)

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
