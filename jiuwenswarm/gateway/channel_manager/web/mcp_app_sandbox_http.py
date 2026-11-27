# coding: utf-8
"""MCP Apps sandbox proxy served on the WebChannel port.

MCP Apps (``io.modelcontextprotocol/ui``) render untrusted app HTML. The spec's
recommended host architecture is a double iframe: the chat UI embeds an outer
"sandbox proxy" page served from a *different origin* than the UI, and that
page writes the app HTML into an inner iframe with ``document.write`` and
relays postMessage traffic in both directions. This gives each app a real
``http://`` document (libraries such as CesiumJS break under ``about:srcdoc``)
while keeping it cross-origin to the host UI.

The WebChannel port (default 19000) differs from the UI port (default 5173),
so it is a separate origin. The Content-Security-Policy is sent as an HTTP
header, built from the resource's ``_meta.ui.csp`` passed as ``?csp=<json>``;
the inner document inherits it and cannot remove it. ``connect-src`` never
includes ``'self'``, so apps cannot call this server's HTTP API.

Routes:
  - ``GET /api/v1/mcp-app/sandbox``  -> ``{"url": ...}`` absolute sandbox URL
    (the UI reaches this through its same-origin API proxy)
  - ``GET /mcp-app-sandbox?csp=...``  -> the sandbox proxy page
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

SANDBOX_PATH = "/mcp-app-sandbox"
SANDBOX_INFO_PATH = "/api/v1/mcp-app/sandbox"

# Only plain origins (optionally with a path) and data:/blob: are accepted, so
# a server cannot smuggle keywords such as 'unsafe-eval' or extra directives.
_NONE = "'none'"
_CSP_SOURCE = re.compile(r"^(https?://[a-z0-9.*-]+(:\d+)?(/[^\s;,'\"]*)?|data:|blob:)$", re.IGNORECASE)


def _sources(value: Any, own_key: str | None = None) -> str:
    if not isinstance(value, list):
        return ""
    sources = [item for item in value if isinstance(item, str) and _CSP_SOURCE.match(item)]
    if own_key:
        # 服务自身 origin 一律剔除（含 frameDomains）：内层 iframe 在该
        # origin 上 allow-same-origin，放行 frame-src 即给 App 一个同源
        # 嵌套 frame，可读写后端 /api/v1/*（PR !10997 CR-11）。
        sources = [item for item in sources if _origin_key(item) != own_key]
    return " ".join(sources)


def _origin_key(source: str) -> str:
    """Scheme://netloc of a CSP source string (path/query stripped)."""
    parts = urlsplit(source)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def build_mcp_app_csp(csp: Any, own_origin: str | None = None) -> str:
    """Content-Security-Policy for an app, from its resource ``_meta.ui.csp``.

    Script/worker allowances match the ext-apps reference host (basic-host):
    bundled and WebGL apps (CesiumJS, Three.js) need eval, WebAssembly and
    blob: workers. ``connect-src`` stays limited to declared domains. All
    source domains (connect/frame/resource/baseUri) drop a source matching
    the serving origin: the inner iframe is allow-same-origin on that
    origin, so a server could otherwise declare its way to same-origin
    frames and read/write the backend API (PR !10997 CR-11).
    """
    csp = csp if isinstance(csp, dict) else {}
    own_key = _origin_key(own_origin) if own_origin else None
    resources = _sources(csp.get("resourceDomains"), own_key)
    connect = _sources(csp.get("connectDomains"), own_key)
    frames = _sources(csp.get("frameDomains"), own_key)
    bases = _sources(csp.get("baseUriDomains"), own_key)
    directives = [
        "default-src 'none'",
        f"script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob: data: {resources}".strip(),
        f"worker-src blob: {resources}".strip(),
        f"style-src 'unsafe-inline' blob: data: {resources}".strip(),
        f"img-src data: blob: {resources}".strip(),
        f"font-src data: blob: {resources}".strip(),
        f"media-src data: blob: {resources}".strip(),
        f"connect-src {connect or _NONE}",
        f"frame-src {frames or _NONE}",
        f"base-uri {bases or _NONE}",
        "form-action 'none'",
        "object-src 'none'",
    ]
    return "; ".join(directives)


# The proxy script. The host UI must be a loopback origin (the desktop app and
# local dev); messages are only accepted from the embedding page's origin and
# from the inner app frame, and relayed with explicit target origins.
_SANDBOX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>MCP App sandbox</title>
<style>
html, body { margin: 0; height: 100%; width: 100%; background: transparent; }
body { display: flex; flex-direction: column; }
iframe { flex-grow: 1; border: 0; padding: 0; background: transparent; color-scheme: inherit; }
</style>
</head>
<body>
<script>
(function () {
  var HOST_PATTERN = /^http:\\/\\/(localhost|127\\.0\\.0\\.1)(:\\d+)?$/;
  if (window.self === window.top) throw new Error('MCP App sandbox must be embedded');
  var hostOrigin = document.referrer ? new URL(document.referrer).origin : '';
  if (!HOST_PATTERN.test(hostOrigin)) throw new Error('MCP App sandbox: host origin not allowed: ' + hostOrigin);
  var ownOrigin = window.location.origin;
  var inner = document.createElement('iframe');
  inner.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
  document.body.appendChild(inner);
  var PERMISSION_FEATURES = { camera: 'camera', microphone: 'microphone', geolocation: 'geolocation', clipboardWrite: 'clipboard-write' };
  window.addEventListener('message', function (event) {
    var data = event.data;
    if (event.source === window.parent) {
      if (event.origin !== hostOrigin) return;
      if (data && data.method === 'ui/notifications/sandbox-resource-ready') {
        var params = data.params || {};
        var perms = params.permissions || {};
        var allow = Object.keys(PERMISSION_FEATURES).filter(function (k) { return perms[k]; })
          .map(function (k) { return PERMISSION_FEATURES[k]; }).join('; ');
        if (allow) inner.setAttribute('allow', allow);
        if (typeof params.html === 'string') {
          var doc = inner.contentDocument;
          doc.open();
          doc.write(params.html);
          doc.close();
        }
      } else if (inner.contentWindow) {
        inner.contentWindow.postMessage(data, ownOrigin);
      }
    } else if (event.source === inner.contentWindow) {
      if (event.origin !== ownOrigin) return;
      window.parent.postMessage(data, hostOrigin);
    }
  });
  window.parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} }, hostOrigin);
})();
</script>
</body>
</html>
"""


def _server_origin(request: Request) -> str:
    """Origin of the socket this server is bound to (not the proxied Host)."""
    server = request.scope.get("server") or ()
    host, port = (server[0], server[1]) if len(server) >= 2 else (request.url.hostname, request.url.port)
    if host in (None, "", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in str(host):
        host = f"[{host}]"
    return f"http://{host}:{port}" if port else f"http://{host}"


def register_mcp_app_sandbox_routes(app: FastAPI) -> None:
    """Mount the MCP Apps sandbox proxy routes on the WebChannel app."""

    @app.get(SANDBOX_INFO_PATH, include_in_schema=False)
    async def mcp_app_sandbox_info(request: Request) -> JSONResponse:
        return JSONResponse(
            {"url": _server_origin(request) + SANDBOX_PATH},
            headers={"Cache-Control": "no-store"},
        )

    @app.get(SANDBOX_PATH, include_in_schema=False)
    async def mcp_app_sandbox(request: Request, csp: str = Query(default="")) -> HTMLResponse:
        own_origin = _server_origin(request)
        try:
            parsed = json.loads(csp) if csp else None
        except ValueError:
            parsed = None
        return HTMLResponse(
            _SANDBOX_HTML,
            headers={
                # frame-ancestors 与脚本内 HOST_PATTERN 同语义（宿主 UI 须
                # loopback）；非 loopback/HTTPS 泛化随 origin 策略任务处理。
                "Content-Security-Policy": (
                    build_mcp_app_csp(parsed, own_origin=own_origin)
                    + "; frame-ancestors 'self' http://localhost:* http://127.0.0.1:*"
                ),
                "Cache-Control": "no-store",
                # Apps inherit this policy. Send the (local) origin: some tile/
                # asset servers, e.g. OpenStreetMap, reject requests without a
                # Referer under their usage policy.
                "Referrer-Policy": "origin",
            },
        )
