# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Serve built frontend static files with optional reverse proxy."""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import mimetypes
import os
import select
import socket
import ssl
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jiuwenclaw.agentserver.tools.ssl_config import get_insecure_ssl_context, get_ssl_verify
from jiuwenclaw.utils import get_agent_root_dir, get_logs_dir, get_service_root_dir, \
    get_multi_tenant_user_workspace_dir, get_root_dir, get_user_workspace_dir, is_package_installation


def _get_package_dir() -> Path:
    """Get the jiuwenclaw package directory (for package-internal files)."""
    if is_package_installation():
        # In package mode, app_web.py is at site-packages/jiuwenclaw/app_web.py
        # So parent is site-packages/jiuwenclaw/
        return Path(__file__).resolve().parent
    else:
        # In source mode, app_web.py is at <repo>/jiuwenclaw/app_web.py
        # So parent is <repo>/jiuwenclaw/
        return Path(__file__).resolve().parent


def _default_dist_dir() -> Path:
    """Return default dist directory for local repo layout."""
    root = get_root_dir()
    # Try user workspace web/dist first (if copied from package)
    if (root / "web" / "dist").exists():
        return root / "web" / "dist"
    # Try package internal web/dist (package installation)
    package_dir = _get_package_dir()
    if (package_dir / "web" / "dist").exists():
        return package_dir / "web" / "dist"
    # Fallback
    return root / "web" / "dist"


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
    """Generate agent/jiuwenclaw_workspace/agent-data.json from default tenant workspace.

    Scans entire agent directory including sessions, workspace, and other subdirs.
    Sessions directories are sorted by modification time (most recent first).
    Output follows DeepAgent standard layout.

    Note: project_root parameter is ignored; uses default multi-tenant path instead.
    """
    # 使用默认多租户路径（单租户作为多租户的默认特例）
    default_workspace = get_multi_tenant_user_workspace_dir("default", "default")
    if default_workspace is None:
        raise FileNotFoundError("default multi-tenant workspace not found")
    agent_root = (default_workspace / "agent").resolve()
    workspace_root = (agent_root / "jiuwenclaw_workspace").resolve()
    output_path = (workspace_root / "agent-data.json").resolve()
    root_folder_key = "__root__"

    if not agent_root.exists():
        raise FileNotFoundError("agent directory not found")
    if not agent_root.is_dir():
        raise NotADirectoryError("agent is not a directory")

    folder_data: dict[str, list[dict[str, str | bool]]] = {}
    seen_paths: dict[str, set[str]] = {}  # folder_key -> normalized paths，用于去重
    # 记录每个 session 目录的修改时间，用于倒序排序
    session_mtime_map: dict[str, float] = {}

    # 扫描整个 agent 目录（包括 sessions, jiuwenclaw_workspace 等）
    for entry in sorted(agent_root.rglob("*")):
        if not entry.is_file() or entry.name.startswith("."):
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

        # 对于 sessions 目录，记录其修改时间
        if relative_folder_path.startswith("sessions/") and entry.parent.exists():
            session_mtime_map[folder_key] = entry.parent.stat().st_mtime

    # 排序：sessions 目录按修改时间倒序，其他目录按字母顺序
    def sort_folder_key(item: tuple[str, list]) -> tuple[int, float | str, str]:
        folder_key = item[0]
        if folder_key.startswith("sessions/"):
            # sessions 目录优先，按修改时间倒序
            mtime = session_mtime_map.get(folder_key, 0)
            return (0, -mtime, folder_key)  # 负数实现倒序
        else:
            # 其他目录按字母顺序
            return (1, 0, folder_key)

    def sort_files(folder_key: str, files: list) -> list:
        if folder_key.startswith("sessions/"):
            # sessions 目录下的文件按修改时间倒序
            return sorted(files, key=lambda item: -session_mtime_map.get(folder_key, 0))
        else:
            # 其他目录按 path 字母顺序
            return sorted(files, key=lambda item: item["path"])

    sorted_folder_data = {
        folder_key: sort_files(folder_key, files)
        for folder_key, files in sorted(folder_data.items(), key=sort_folder_key)
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sorted_folder_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
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
    ws_disable_compress = False
    project_root = get_user_workspace_dir()
    workspace_root = get_agent_root_dir()
    logs_root = get_logs_dir()
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
                    payload_len = int.from_bytes(self._buffer[idx : idx + 2], "big")
                    idx += 2
                elif payload_len == 127:
                    if len(self._buffer) < idx + 8:
                        break
                    payload_len = int.from_bytes(self._buffer[idx : idx + 8], "big")
                    idx += 8

                mask_key = b""
                if masked:
                    if len(self._buffer) < idx + 4:
                        break
                    mask_key = bytes(self._buffer[idx : idx + 4])
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
            params = payload.get("params")
            self.logger.info(
                "[ws][%s][req] id=%s method=%s has_params=%s",
                direction,
                self._format_ws_part(payload.get("id")),
                self._format_ws_part(payload.get("method")),
                bool(params) if isinstance(params, dict) else False,
            )
            return
        if msg_type == "res":
            res_payload = payload.get("payload")
            self.logger.info(
                "[ws][%s][res] id=%s ok=%s has_payload=%s has_error=%s code=%s",
                direction,
                self._format_ws_part(payload.get("id")),
                self._format_ws_part(payload.get("ok")),
                bool(res_payload) if isinstance(res_payload, dict) else False,
                payload.get("error") is not None,
                self._format_ws_part(payload.get("code")),
            )
            return
        if msg_type == "event":
            event_payload = payload.get("payload")
            self.logger.info(
                "[ws][%s][event] event=%s seq=%s stream_id=%s has_payload=%s",
                direction,
                self._format_ws_part(payload.get("event")),
                self._format_ws_part(payload.get("seq")),
                self._format_ws_part(payload.get("stream_id")),
                bool(event_payload) if isinstance(event_payload, dict) else False,
            )

    def _is_api_route(self) -> bool:
        return urlparse(self.path).path.startswith("/api")

    def _is_ws_route(self) -> bool:
        return urlparse(self.path).path.startswith("/ws")

    def _is_file_api_route(self) -> bool:
        return urlparse(self.path).path.startswith("/file-api/")

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
            try:
                self.send_error(502, "proxy http error")
            except (OSError, RuntimeError):
                pass
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
            self.log_error("proxy ws connect failed: %s", exc)
            try:
                self.send_error(502, "proxy ws connect failed")
            except OSError:
                pass
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
                self.send_error(502, "proxy ws handshake failed: empty response")
                return

            self.connection.sendall(response_head)

            if b" 101 " not in response_head.split(b"\r\n", 1)[0]:
                self.logger.info("[ws][handshake] upstream returned non-101, tunnel closed")
                return

            self.logger.info("[ws][handshake] tunnel established %s <-> %s:%s", self.client_address[0], upstream_host, upstream_port)
            self.connection.setblocking(False)
            upstream.setblocking(False)
            sockets = [self.connection, upstream]
            client_parser = self._WsTextFrameParser()
            server_parser = self._WsTextFrameParser()
            while True:
                readable, _, errored = select.select(sockets, [], sockets, self._WS_SELECT_TIMEOUT)
                if errored:
                    break
                if not readable:
                    continue
                for sock in readable:
                    try:
                        data = sock.recv(self._WS_RECV_BUFFER)
                    except OSError:
                        data = b""
                    if not data:
                        return
                    if sock is self.connection:
                        for text_message in client_parser.feed(data):
                            self._log_ws_business_message("frontend->backend", text_message)
                        upstream.sendall(data)
                    else:
                        for text_message in server_parser.feed(data):
                            self._log_ws_business_message("backend->frontend", text_message)
                        self.connection.sendall(data)
        except Exception as exc:  # noqa: BLE001
            self.log_error("proxy ws error: %s", exc)
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
            in_workspace = os.path.commonpath([str(cls.workspace_root), str(target_resolved)]) == str(cls.workspace_root)
            in_logs = os.path.commonpath([str(cls.logs_root), str(target_resolved)]) == str(cls.logs_root)
            # 允许多租户路径（在 get_user_workspace_dir 下的 service_*/agent_* 目录）
            user_workspace = str(get_user_workspace_dir())
            in_user_workspace = os.path.commonpath([user_workspace, str(target_resolved)]) == user_workspace
            return in_workspace or in_logs or in_user_workspace
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

    def _handle_file_api_get(self, parsed) -> None:
        path = parsed.path
        query = {}
        if parsed.query:
            for pair in parsed.query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                else:
                    k, v = pair, ""
                query[unquote(k)] = unquote(v)

        if path == "/file-api/list-markdown":
            dir_arg = query.get("dir", "")
            if not dir_arg:
                self._write_json(400, {"error": "missing_dir"})
                return
            # 使用默认多租户路径（单租户作为多租户的默认特例）
            base_dir = get_multi_tenant_user_workspace_dir("default", "default")
            if base_dir is None:
                self._write_json(500, {"error": "default_workspace_not_found"})
                return
            full_dir = (base_dir / dir_arg).resolve()
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
                        "path": str(entry.relative_to(base_dir)),
                    }
                )
            self._write_json(200, {"files": files})
            return

        if path == "/file-api/list-files":
            dir_arg = query.get("dir", "")
            if not dir_arg:
                self._write_json(400, {"error": "missing_dir"})
                return
            # 使用默认多租户路径（单租户作为多租户的默认特例）
            base_dir = get_multi_tenant_user_workspace_dir("default", "default")
            if base_dir is None:
                self._write_json(500, {"error": "default_workspace_not_found"})
                return
            full_dir = (base_dir / dir_arg).resolve()
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
            # 计算相对路径时使用 base_dir
            for entry in entries:
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry.relative_to(base_dir)),
                        "isMarkdown": self._is_markdown(entry) if entry.is_file() else False,
                        "isDirectory": entry.is_dir(),
                    }
                )
            self._write_json(200, {"files": files})
            return

        if path == "/file-api/file-content":
            file_arg = query.get("path", "")
            if not file_arg:
                self._write_json(400, {"error": "missing_file_path"})
                return
            # 根据路径前缀决定 base_dir
            # - .logs/ 路径使用 service 根目录（service 级别共享）
            # - .checkpoint/ 路径使用 agent_default 级别
            # - agent/ 路径使用默认多租户路径
            file_arg_normalized = file_arg.replace("\\", "/")
            if file_arg_normalized.startswith(".logs/") or file_arg_normalized.startswith("logs/"):
                base_dir = get_service_root_dir()
            else:
                # .checkpoint/ 和其他路径都使用默认多租户路径
                base_dir = get_multi_tenant_user_workspace_dir("default", "default")
                if base_dir is None:
                    self._write_json(500, {"error": "default_workspace_not_found"})
                    return
            full_path = (base_dir / file_arg).resolve()
            if not self._is_path_under_allowed_root(full_path):
                self._write_json(403, {"error": "forbidden_path"})
                return
            if not full_path.exists():
                if file_arg.replace("\\", "/") == "agent/jiuwenclaw_workspace/agent-data.json":
                    try:
                        _generate_agent_data(base_dir)
                    except Exception as exc:  # noqa: BLE001
                        self._write_json(500, {"error": "generate_failed", "detail": str(exc)})
                        return
                if not full_path.exists():
                    self._write_json(404, {"error": "file_not_found", "fullPath": str(full_path)})
                    return
            try:
                data = full_path.read_text(encoding="utf-8")
            except OSError as exc:
                self._write_json(500, {"error": str(exc)})
                return
            body = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
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

    def _handle_file_api_post(self, parsed) -> None:
        if parsed.path == "/file-api/rebuild-agent-data":
            try:
                base_dir = get_multi_tenant_user_workspace_dir("default", "default")
                if base_dir is None:
                    self._write_json(500, {"error": "default_workspace_not_found"})
                    return
                _generate_agent_data(base_dir)
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

            # 使用默认多租户路径（单租户作为多租户的默认特例）
            base_dir = get_multi_tenant_user_workspace_dir("default", "default")
            if base_dir is None:
                self._write_json(500, {"error": "default_workspace_not_found"})
                return
            full_path = (base_dir / request_path).resolve()
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
                "[jiuwenclaw-web] ws disable compress updated: %s",
                ws_disable_compress,
            )
            self._write_json(200, {"ok": True, "wsDisableCompress": ws_disable_compress})
            return

        self._write_json(404, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
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
        fmt="%(asctime)s.%(msecs)03d [%(process)d] %(levelname)s %(name)s %(filename)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(logs_root / "ws-dev.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    lg.addHandler(file_handler)
    return lg


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve JiuwenClaw frontend static files.")
    parser.add_argument("--host", default=os.getenv("JIUWENCLAW_WEB_HOST", "localhost"), help="Host to bind.")
    parser.add_argument("--port", type=int, default=os.getenv("JIUWENCLAW_WEB_PORT", "5173"), help="Port to bind.")
    parser.add_argument(
        "--dist",
        default=str(_default_dist_dir()),
        help="Path to frontend dist directory.",
    )
    parser.add_argument(
        "--proxy-target",
        default=os.getenv("JIUWENCLAW_WEB_PROXY_TARGET", "http://127.0.0.1:19000"),
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
    args = parser.parse_args()

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

    # default_project_root should be the user workspace root (~/.jiuwenclaw in package mode)
    # get_root_dir() already handles this correctly
    default_project_root = get_user_workspace_dir()

    project_root = default_project_root
    # 多租户架构：workspace_root 指向默认多租户路径
    workspace_root = get_multi_tenant_user_workspace_dir("default", "default")
    if workspace_root is None:
        workspace_root = project_root
    logs_root = get_logs_dir().resolve()
    logger = _setup_logger(logs_root, args.log_level)

    class _ConfiguredHandler(_SpaStaticHandler):
        pass

    _ConfiguredHandler.api_target = api_target
    _ConfiguredHandler.ws_target = ws_target
    _ConfiguredHandler.ws_disable_compress = args.ws_disable_compress
    _ConfiguredHandler.project_root = project_root
    _ConfiguredHandler.workspace_root = workspace_root
    _ConfiguredHandler.logs_root = logs_root
    _ConfiguredHandler.logger = logger
    handler = partial(_ConfiguredHandler, directory=str(dist_dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)

    logger.info("[jiuwenclaw-web] serving %s", dist_dir)
    logger.info("[jiuwenclaw-web] http://%s:%s", args.host, args.port)
    logger.info("[jiuwenclaw-web] /api -> %s", api_target)
    logger.info("[jiuwenclaw-web] /ws  -> %s", ws_target)
    logger.info("[jiuwenclaw-web] ws disable compress: %s", args.ws_disable_compress)
    logger.info("[jiuwenclaw-web] /file-api roots -> %s, %s", workspace_root, logs_root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("[jiuwenclaw-web] server closed")


if __name__ == "__main__":
    main()
