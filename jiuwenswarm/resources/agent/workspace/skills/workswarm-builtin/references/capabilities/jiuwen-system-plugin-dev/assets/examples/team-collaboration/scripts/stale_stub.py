"""Stub data source for the "stale badge" rendering check (Leader 批准的条件方案 B).

用途与边界（务必遵守，写进验收报告的证据分层）：
- **只**用于证明前端「过期徽标随 `stale` 亮起/撤下」的**渲染逻辑**；
- **不能**证明后端降级链路（后端降级由 `tests/integration_isolated_load.py` 的
  D1–D7 用例单独背书）；
- 不修改任何后端代码、不向后端要任何调试参数；只静态托管真实 `web/` 目录，
  并用「真实快照 + 强制 stale 标记」作为数据源。

典型用法（独立端口，真实前端文件）：:

    python -X utf8 scripts/stale_stub.py \\
        --web-dir team-collab-viz/web \\
        --real-base http://127.0.0.1:51917 \\
        --sid <sid> --stale true --port-file stub_port.txt --seconds 90

端点：
- `GET /web/**`：静态托管真实 `web/` 文件（禁止路径穿越）
- `GET /snapshot`：返回「真实快照 + 强制 stale 标记」；带 `since==revision` 时返回
  `unchanged` 形状（与契约 v1.6 一致，含 stale/served_at/generated_at）
- `GET /health`：最小形状，`snapshot.stale` 与快照一致
- `GET /ui-state`：200 空状态（`state=null, empty=true`），避免前端回填干扰本次观察
- `PATCH|PUT /ui-state`、`OPTIONS`：CORS 预检与写入的 no-op 应答
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATE: dict[str, object] = {"base": None, "sid": "", "stale": True, "web_dir": None, "snapshot": None}
LOCK = threading.Lock()


def now_ms() -> int:
    """Millisecond epoch clock (contract uses ms everywhere)."""
    return int(time.time() * 1000)


def fetch_real_snapshot(base: str, sid: str) -> dict:
    """Read one real snapshot; on failure fall back to a same-shape synthetic one."""
    url = f"{base}/snapshot?sid={sid}&wait=0"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and "schema_version" in payload:
            payload["_source"] = "real_snapshot"
            return payload
        raise ValueError("unexpected snapshot shape")
    except Exception as error:  # noqa: BLE001 - stub must always answer something
        print(f"[stub] real snapshot unavailable ({error!r}); using synthetic fixture", flush=True)
        return {
            "schema_version": "1.1",
            "revision": 1,
            "generated_at": now_ms(),
            "served_at": now_ms(),
            "stale": False,
            "stale_reason": None,
            "scope": {"kind": "session", "key": sid},
            "availability": {
                "session_map_source": "event",
                "team_readable": True,
                "last_event_at": now_ms(),
                "last_success_at": now_ms(),
            },
            "team": {
                "team_name": "stub_team",
                "display_name": "过期徽标渲染验证（stub 数据源）",
                "leader_member_name": "stub-leader",
                "desc": None,
                "created_at": now_ms(),
                "status": "running",
                "status_label": "运行中",
                "status_source": "event",
                "progress": {"total": 1, "completed": 0, "cancelled": 0, "in_review": 0, "blocked": 0,
                             "pending": 1, "in_progress": 0, "percent": 0.0},
            },
            "members": [],
            "tasks": [],
            "graph": {"nodes": [], "edges": [], "layers": [], "has_cycle": False},
            "limits": {"members_total": 0, "tasks_total": 0},
            "_source": "synthetic_fixture",
        }


class Handler(BaseHTTPRequestHandler):
    """Minimal CORS-friendly server implementing the endpoints the page touches."""

    server_version = "tcv-stale-stub/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - quiet by design
        print("[stub] " + fmt % args, flush=True)

    # ---------------------------------------------------------------- helpers

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,PUT,PATCH,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot_payload(self) -> dict:
        """Real snapshot with the stale flag forced, keeping every other field identical."""
        with LOCK:
            base = dict(STATE["snapshot"] or {})  # type: ignore[arg-type]
            stale = bool(STATE["stale"])
        payload = dict(base)
        payload["stale"] = stale
        payload["stale_reason"] = "ops_error" if stale else None
        payload["served_at"] = now_ms()
        return payload

    # ----------------------------------------------------------------- routes

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/health":
            payload = self._snapshot_payload()
            self._json(200, {
                "ok": True,
                "port": self.server.server_address[1],
                "bind": self.server.server_address[0],
                "draining_ports": [],
                "snapshot": {
                    "revision": payload.get("revision"),
                    "stale": payload.get("stale"),
                    "stale_reason": payload.get("stale_reason"),
                },
                "capabilities": {"schema_version": payload.get("schema_version", "1.1")},
                "stub": True,
            })
            return
        if path == "/snapshot":
            payload = self._snapshot_payload()
            raw_since = query.get("since", [None])[0]
            try:
                since = int(raw_since) if raw_since is not None else None
            except ValueError:
                self._json(400, {"ok": False, "code": "BAD_QUERY"})
                return
            if since is not None and since == payload.get("revision"):
                self._json(200, {
                    "unchanged": True,
                    "revision": payload.get("revision"),
                    "generated_at": payload.get("generated_at"),
                    "served_at": payload.get("served_at"),
                    "stale": payload.get("stale"),
                })
                return
            self._json(200, payload)
            return
        if path == "/ui-state":
            self._json(200, {"schema_version": "1.1", "scope": {"kind": "session", "key": STATE["sid"]},
                             "state": None, "updated_at": None, "empty": True, "stub": True})
            return
        if path in ("/favicon.ico",):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._static(path)

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._json(200, {"ok": True, "stub": True, "state": None, "updated_at": now_ms(), "bytes": length})

    do_PUT = do_PATCH  # noqa: N815 - same no-op semantics for the stub

    # ---------------------------------------------------------------- static

    def _static(self, path: str) -> None:
        root: Path = STATE["web_dir"]  # type: ignore[assignment]
        relative = path[len("/web/"):] if path.startswith("/web/") else path.lstrip("/")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._json(403, {"ok": False, "code": "FORBIDDEN"})
            return
        if not candidate.is_file():
            self._json(404, {"ok": False, "code": "NOT_FOUND", "path": path})
            return
        body = candidate.read_bytes()
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="真实 web/ + 强制 stale 快照的渲染验证 stub")
    parser.add_argument("--web-dir", type=Path, required=True, help="真实插件 web 目录")
    parser.add_argument("--real-base", default="", help="真实服务 base（用于取一份真实快照做基底）")
    parser.add_argument("--sid", default="", help="会话标识（与 iframe 的 sid 查询参数一致）")
    parser.add_argument("--stale", choices=("true", "false"), default="true", help="强制 stale 标记")
    parser.add_argument("--port-file", type=Path, default=None, help="把实际端口写入该文件")
    parser.add_argument("--seconds", type=int, default=90, help="自动退出前的存活秒数")
    arguments = parser.parse_args()

    web_dir = arguments.web_dir.expanduser().resolve()
    if not (web_dir / "team-info.html").is_file():
        parser.exit(2, f"web 目录不包含 team-info.html：{web_dir}\n")

    snapshot = fetch_real_snapshot(arguments.real_base, arguments.sid) if arguments.real_base else None
    if snapshot is None:
        snapshot = fetch_real_snapshot("", arguments.sid)
    with LOCK:
        STATE["base"] = arguments.real_base
        STATE["sid"] = arguments.sid
        STATE["stale"] = arguments.stale == "true"
        STATE["web_dir"] = web_dir
        STATE["snapshot"] = snapshot

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    print(f"STUB_PORT={port}", flush=True)
    print(f"STUB_BASE=http://127.0.0.1:{port}", flush=True)
    print(f"STUB_SNAP_SOURCE={snapshot.get('_source')}", flush=True)
    print(f"STUB_STALE={STATE['stale']}", flush=True)
    if arguments.port_file is not None:
        arguments.port_file.write_text(str(port), encoding="utf-8")

    timer = threading.Timer(arguments.seconds, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        pass
    finally:
        server.server_close()
    print("[stub] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
