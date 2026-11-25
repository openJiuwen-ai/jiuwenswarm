"""Local HTTP surface: static pages from ``web/``, read-only snapshot, view state.

Every response is cross-origin friendly (the host iframe is an opaque origin and
sends no credentials) and ``no-store``. The team data path is read-only: team
data has no write endpoint, and ``/ui-state`` only accepts the nine view keys.

Nothing here may touch host Ops: the ops view is bound to one event call and is
never reachable from a request thread, which is what makes the cache read-only.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from . import ui_state
from .snapshot import (
    GLOBAL_SCOPE_KEY,
    SCHEMA_VERSION,
    SnapshotCache,
    empty_snapshot,
    now_ms,
)
from .ui_state import UiStateError, UiStateStore

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
LONG_POLL_DEFAULT = 25
LONG_POLL_MAX = 25
MAX_BODY_BYTES = 64 * 1024
BIND_ADDRESS = "127.0.0.1"

STATIC_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, PUT, PATCH, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}

CAPABILITIES = ("snapshot", "long_poll", "ui_state", "ui_state_patch", "cors_star")


class ServiceContext:
    """Plain-data state shared by the HTTP threads and the event handlers."""

    def __init__(self, package_id: str, web_root: Path | None = None) -> None:
        """Create the caches and health counters for one activation."""
        self.package_id = package_id
        self.web_root = web_root if web_root is not None else WEB_ROOT
        self.snapshots = SnapshotCache()
        self.ui_state = UiStateStore()
        self.pid = os.getpid()
        self.started_at = now_ms()
        self.port = 0
        self.bind = BIND_ADDRESS
        self.bind_error: str | None = None
        self.events_received = 0
        self.last_event_at: int | None = None
        self.handler_registered = False
        self.closing = threading.Event()
        self._lock = threading.Lock()

    def note_event(self, at: int | None = None) -> None:
        """Count one delivered ``team.monitor_event``."""
        with self._lock:
            self.events_received += 1
            self.last_event_at = at if isinstance(at, int) else now_ms()

    def note_bind(self, port: int) -> None:
        """Record the port the kernel actually assigned (R2)."""
        self.port = int(port)

    def note_bind_error(self, error: str) -> None:
        """Record a listen failure so ``/health`` and rendering can react (R6)."""
        self.bind_error = error

    def stopping(self) -> bool:
        """True once the listener is closing; long polls use it to bail out."""
        return self.closing.is_set()

    def draining_ports(self) -> list[dict[str, int]]:
        """Mode B drain list; this build follows R3 (one port), so it stays empty."""
        return []

    def health(self) -> dict[str, Any]:
        """The single probe used by acceptance (§5.4)."""
        with self._lock:
            received = self.events_received
            last_event_at = self.last_event_at
        payload: dict[str, Any] = {
            "ok": self.bind_error is None and not self.closing.is_set(),
            "pid": self.pid,
            "port": self.port,
            "bind": self.bind,
            "started_at": self.started_at,
            "schema_version": SCHEMA_VERSION,
            "snapshot": self.snapshots.summary(),
            "events": {
                "received": received,
                "last_event_at": last_event_at,
                "handler_registered": self.handler_registered,
            },
            "ui_state": {"scopes": self.ui_state.scope_count(), "ttl_seconds": self.ui_state.ttl_seconds},
            "draining_ports": self.draining_ports(),
            "capabilities": list(CAPABILITIES),
        }
        if self.bind_error is not None:
            payload["bind_error"] = self.bind_error
        return payload


def _scope_of(raw_sid: Any) -> tuple[str, str, str]:
    """Map a ``sid`` into ``(kind, key, sid)``; empty sid means global scope."""
    sid = raw_sid if isinstance(raw_sid, str) else ""
    if sid:
        return "session", sid, sid
    return "global", GLOBAL_SCOPE_KEY, ""


def _query_value(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _int_query(query: Mapping[str, list[str]], name: str) -> int | None:
    raw = _query_value(query, name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} not an int") from error


class TrackingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that remembers live connections so drain can cut them.

    ``shutdown()``/``server_close()`` only release the *listening* socket. Already
    accepted connections would otherwise live on until the peer or their own
    timeout ends them (a long poll may sit for 25s), which leaves ``ESTABLISHED``
    sockets behind an uninstalled plugin. The owner of this server can therefore
    force them closed (F2). Connection lifetime is per request, matching the
    ``Connection: close`` every response carries (O1).
    """

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Track every accepted socket in a lock-protected set."""
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        """Remember the socket before its handler thread starts."""
        with self._connections_lock:
            self._connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: socket.socket) -> None:
        """Forget the socket, ignoring the ``OSError`` a forced close may have caused."""
        try:
            super().shutdown_request(request)
        except OSError:
            pass
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def active_connection_count(self) -> int:
        """Number of accepted sockets that have not finished yet."""
        with self._connections_lock:
            return len(self._connections)

    def close_active_connections(self, timeout: float = 1.0) -> int:
        """Cut every accepted socket; returns how many were still open.

        ``shutdown(SHUT_RDWR)`` unblocks a thread parked in ``rfile.read``,
        ``wfile.write`` or a long poll, so it unwinds and closes its own fd in
        ``shutdown_request``. Anything still tracked after ``timeout`` is closed
        here; ``socket.close`` is idempotent per socket object, so the later
        stdlib close of the same object is a no-op rather than a double close.
        """
        with self._connections_lock:
            live = list(self._connections)
        for connection in live:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:  # already gone
                pass
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._connections_lock:
                if not self._connections:
                    return len(live)
            time.sleep(0.01)
        with self._connections_lock:
            stubborn = list(self._connections)
        for connection in stubborn:
            try:
                connection.close()
            except OSError:  # pragma: no cover - close does not raise in practice
                pass
        return len(live)


def build_handler(context: ServiceContext) -> type[BaseHTTPRequestHandler]:
    """Create the request handler class bound to ``context``."""

    class Handler(BaseHTTPRequestHandler):
        """Routes: /snapshot, /ui-state, /health, /web/**, OPTIONS."""

        server_version = "TeamCollabViz/1.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - stdlib signature
            """Silence stderr access logs; the host would only spam its console."""
            return

        # ------------------------------------------------------------ sending

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra: Mapping[str, str] | None = None,
            *,
            with_body: bool = True,
        ) -> None:
            # O1 (§5.0): never promise keep-alive. The header and ``close_connection``
            # are set together, otherwise a HTTP/1.1 client may legally reuse a socket
            # the server is about to shut down (that mismatch showed up as
            # ``ConnectionAbortedError`` on the second request).
            self.close_connection = True
            try:
                self.send_response(status)
                for name, value in CORS_HEADERS.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if body and with_body:
                    self.wfile.write(body)
            except OSError:
                # Peer gone, or the drain path (F2) shut this socket down first.
                return

        def _json(
            self,
            status: int,
            payload: Any,
            extra: Mapping[str, str] | None = None,
            *,
            with_body: bool = True,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra, with_body=with_body)

        def _error(self, status: int, code: str, message: str, *, with_body: bool = True) -> None:
            self._json(status, {"code": code, "message": message, "revision": None}, with_body=with_body)

        def _no_content(self) -> None:
            self._send(204, b"", "text/plain; charset=utf-8")

        # ------------------------------------------------------------- verbs

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib signature
            """CORS preflight: PUT/PATCH with JSON always triggers it."""
            self._no_content()

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            self._safe(self._route_get)

        def do_PUT(self) -> None:  # noqa: N802 - stdlib signature
            self._safe(lambda: self._write_state("put"))

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib signature
            self._safe(lambda: self._write_state("patch"))

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
            # HEAD keeps the full header set (CORS included) but no entity body.
            self._error(405, "METHOD_NOT_ALLOWED", "仅支持 GET / PUT / PATCH / OPTIONS", with_body=False)

        def __getattr__(self, name: str) -> Any:
            """Catch every unimplemented verb so it still gets JSON + CORS (§5.0).

            ``BaseHTTPRequestHandler`` falls back to ``send_error(501)`` (an HTML
            body with **no** CORS headers) whenever ``do_<METHOD>`` is missing.
            Intercepting the lookup here keeps the "CORS on every response,
            including 4xx/OPTIONS" rule true for POST/DELETE/TRACE/… as well.
            """
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

        def _method_not_allowed(self) -> None:
            """Answer an unsupported verb over the normal JSON error channel."""
            # The body of such a request is never read, so do not reuse the socket.
            self.close_connection = True
            self._error(405, "METHOD_NOT_ALLOWED", f"不支持的方法: {self.command}")

        def _safe(self, action: Any) -> None:
            """Never let a handler exception escape as a dropped connection."""
            try:
                action()
            except OSError:
                # BrokenPipeError / ConnectionResetError / ConnectionAbortedError, plus
                # the ENOTCONN that a socket closed by the drain path (F2) raises on write.
                return
            except Exception as error:  # noqa: BLE001 - degrade instead of crashing
                try:
                    self._error(500, "INTERNAL_ERROR", f"服务内部错误: {error!r}")
                except Exception:  # noqa: BLE001 - response already gone
                    return

        # ------------------------------------------------------------ routing

        def _route_get(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query, keep_blank_values=True)
            if path == "/snapshot":
                return self._handle_snapshot(query)
            if path == "/ui-state":
                return self._handle_state_get(query)
            if path == "/health":
                return self._json(200, context.health())
            if path in ("/favicon.ico", "/robots.txt"):
                return self._no_content()
            if path in ("/", ""):
                return self._serve_static("team-info.html")
            if path.startswith("/web/"):
                return self._serve_static(path[len("/web/"):])
            return self._error(404, "NOT_FOUND", f"未知路径: {path}")

        # ----------------------------------------------------------- snapshot

        def _handle_snapshot(self, query: Mapping[str, list[str]]) -> None:
            try:
                since = _int_query(query, "since")
                wait = _int_query(query, "wait")
            except ValueError:
                return self._error(400, "BAD_QUERY", "since / wait 必须是整数")
            if since is not None and since < 0:
                return self._error(400, "BAD_QUERY", "since 必须 >= 0")
            if wait is None:
                wait = LONG_POLL_DEFAULT
            if wait < 0 or wait > LONG_POLL_MAX:
                return self._error(400, "BAD_QUERY", f"wait 必须在 0..{LONG_POLL_MAX} 之间")

            kind, key, _ = _scope_of(_query_value(query, "sid"))
            snapshots = context.snapshots
            try:
                payload, revision = self._snapshot_record(kind, key)
                revision_reset = False
                if since is not None and since > revision:
                    revision_reset = True
                elif since is not None and since == revision:
                    if wait > 0 and snapshots.wait_change(key, since, float(wait), context.stopping):
                        payload, revision = self._snapshot_record(kind, key)
                    if revision == since:
                        # The unchanged body still carries ``stale``/``served_at``
                        # (contract v1.6) so the expiry badge can flip without a
                        # revision change.
                        return self._json(
                            200,
                            {
                                "unchanged": True,
                                "revision": revision,
                                "generated_at": payload.get("generated_at"),
                                "served_at": now_ms(),
                                "stale": payload.get("stale"),
                            },
                        )
            except Exception as error:  # noqa: BLE001 - degrade, never 500 a tab
                payload, revision = snapshots.view(key)
                if payload is None:
                    return self._error(503, "SNAPSHOT_UNAVAILABLE", f"快照不可用: {error!r}")
                payload["stale"] = True
                payload["stale_reason"] = "ops_error"
                payload["served_at"] = now_ms()
                return self._json(200, payload, {"ETag": f'"{revision}"'})

            # ``view`` already stamped ``served_at``; refresh it after any wait.
            payload["served_at"] = now_ms()
            payload["revision"] = revision
            if revision_reset:
                payload["revision_reset"] = True
            self._json(200, payload, {"ETag": f'"{revision}"'})

        def _snapshot_record(self, kind: str, key: str) -> tuple[dict[str, Any], int]:
            """Return a private response copy, creating the §2.8 placeholder if cold."""
            payload, revision = context.snapshots.view(key)
            if payload is None:
                context.snapshots.ensure(
                    key,
                    lambda: empty_snapshot(scope_kind=kind, scope_key=key),
                )
                payload, revision = context.snapshots.view(key)
            if payload is None:  # pragma: no cover - ensure() always fills the slot
                raise RuntimeError("snapshot placeholder missing")
            return payload, revision

        # ----------------------------------------------------------- ui-state

        def _handle_state_get(self, query: Mapping[str, list[str]]) -> None:
            kind, key, _ = _scope_of(_query_value(query, "sid"))
            state, updated_at, empty = context.ui_state.get_view(key)
            self._json(
                200,
                {
                    "schema_version": ui_state.SCHEMA_VERSION,
                    "scope": {"kind": kind, "key": key},
                    # Always the complete nine-key object (§5.3.1 U2/U3); ``empty``
                    # only means "never written", never "no state to return".
                    "state": state,
                    "updated_at": updated_at,
                    "empty": empty,
                },
            )

        def _write_state(self, mode: str) -> None:
            parts = urlsplit(self.path)
            if parts.path != "/ui-state":
                return self._error(404, "NOT_FOUND", f"未知路径: {parts.path}")
            query = parse_qs(parts.query, keep_blank_values=True)
            body, failure = self._read_body_json()
            if failure is not None:
                return self._error(400, failure[0], failure[1])
            if not isinstance(body, Mapping):
                return self._error(400, "BAD_BODY", "请求体必须是 JSON 对象")
            sid = body.get("sid")
            if not isinstance(sid, str) or not sid:
                sid = _query_value(query, "sid") or ""
            kind, key, _ = _scope_of(sid)
            state_payload = body.get("state")
            try:
                if mode == "put":
                    state, updated_at, size = context.ui_state.put(key, state_payload)
                else:
                    state, updated_at, size = context.ui_state.patch(key, state_payload)
            except UiStateError as error:
                return self._error(400, error.code, str(error))
            self._json(
                200,
                {
                    "ok": True,
                    "scope": {"kind": kind, "key": key},
                    "updated_at": updated_at,
                    "bytes": size,
                    "state": state,
                    "empty": False,
                },
            )

        def _read_body_json(self) -> tuple[Any, tuple[str, str] | None]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return None, ("BAD_BODY", "Content-Length 非法")
            if length <= 0:
                return None, ("BAD_BODY", "请求体为空")
            if length > MAX_BODY_BYTES:
                return None, ("BAD_BODY", f"请求体超过 {MAX_BODY_BYTES} 字节")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8")), None
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return None, ("BAD_JSON", f"JSON 解析失败: {error}")

        # ------------------------------------------------------------ static

        def _serve_static(self, relative: str) -> None:
            target = self._resolve_static(relative)
            if target is None:
                return self._error(404, "NOT_FOUND", f"静态资源不存在: {relative}")
            try:
                body = target.read_bytes()
            except OSError as error:
                return self._error(404, "NOT_FOUND", f"静态资源不可读: {error!r}")
            content_type = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, body, content_type)

        def _resolve_static(self, relative: str) -> Path | None:
            """Resolve inside ``web/`` only; rejects traversal, absolute and symlinks."""
            if not isinstance(relative, str) or not relative or "\x00" in relative:
                return None
            if relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
                return None
            root = context.web_root.resolve()
            candidate = (root / relative).resolve()
            if candidate != root and root not in candidate.parents:
                return None
            if candidate.is_symlink() or not candidate.is_file():
                return None
            return candidate

    return Handler
