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


def _parse_single_byte_range(
    range_header: str,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse one HTTP byte range, returning inclusive start and end offsets."""
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
    ws_disable_compress = False
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
                        raise OSError(f"Unable to decode file with any known encoding") from decode_exc
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
        token = query.get("token", "")
        if not token:
            self._write_json(400, {"error": "missing_token"})
            return

        try:
            from jiuwenswarm.agents.harness.common.tools.web_file_download import (
                validate_file_download_token,
            )
        except ImportError:
            self._write_json(500, {"error": "download_module_unavailable"})
            return

        payload = validate_file_download_token(token)
        if payload is None:
            self._write_json(403, {"error": "invalid_or_expired_token"})
            return

        file_path = payload.get("path", "")
        if not file_path or not os.path.isfile(file_path):
            self._write_json(404, {"error": "file_not_found"})
            return

        try:
            file_size = os.path.getsize(file_path)
            file_name = os.path.basename(file_path)

            mime_type, _ = mimetypes.guess_type(file_name)
            if not mime_type:
                mime_type = "application/octet-stream"

            range_header = self.headers.get("Range")
            byte_range = None
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
            encoded_name = quote(file_name, safe="")
            disposition = (
                "inline"
                if query.get("inline", "").lower() in {"1", "true"}
                else "attachment"
            )
            self.send_header(
                "Content-Disposition",
                f"{disposition}; filename*=UTF-8''{encoded_name}",
            )
            self.send_header("Cache-Control", "no-store")
            if byte_range is not None:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{file_size}",
                )
            self.end_headers()

            if self.command != "HEAD":
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = content_length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except Exception as exc:
            self.log_error("file download error: %s", exc)
            try:
                self._write_json(500, {"error": "download_failed", "detail": str(exc)})
            except Exception:
                # Connection may already be closed or broken; nothing more we can do.
                self.log_error("failed to send download error response")

    def _handle_file_api_post(self, parsed) -> None:
        if parsed.path == "/file-api/rebuild-agent-data":
            try:
                _generate_agent_data(self.project_root)
            except Exception as exc:  # noqa: BLE001
                self._write_json(500, {"error": "rebuild_failed", "detail": str(exc)})
                return

            self._write_json(200, {"ok": True})
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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if self._is_share_api_route():
            self._handle_share_api_get(parsed)
            return
        if self._is_file_api_route():
            self._handle_file_api_get(parsed)
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
