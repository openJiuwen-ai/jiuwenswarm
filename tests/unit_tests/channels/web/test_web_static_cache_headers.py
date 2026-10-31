# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""静态资源 Cache-Control 头行为测试。

打包桌面端的前端产物由该静态服务提供；Vite 哈希资产需要 immutable 长缓存
（浏览器跳过条件请求，二次启动免全部资源往返），index.html 与 SPA fallback
必须 no-cache 回源验证。用真实 ThreadingHTTPServer 断言线上路径的头行为。
"""

from __future__ import annotations

import http.client
import re
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


class _QuietSpaHandler(_SpaStaticHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


@pytest.fixture()
def static_server(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (tmp_path / "assets" / "index-Byh9IHKO.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "assets" / "index-DbwWYrAX.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "assets" / "spreadsheetPreview.worker-CK4ZsbC3.js").write_text("w()", encoding="utf-8")
    (tmp_path / "logo.svg").write_text("<svg/>", encoding="utf-8")

    handler = partial(_QuietSpaHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(base_url: str, path: str, extra_headers: dict[str, str] | None = None):
    netloc = urlsplit(base_url).netloc
    conn = http.client.HTTPConnection(netloc)
    try:
        conn.request("GET", path, headers=extra_headers or {})
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


def test_hashed_assets_are_immutable(static_server: str) -> None:
    status, headers, body = _request(static_server, "/assets/index-Byh9IHKO.js")
    assert status == 200
    assert body == b"console.log(1)"
    assert headers["cache-control"] == "public, max-age=31536000, immutable"

    # 含点号/多段名的哈希资产（worker chunk）同样命中
    status, headers, _ = _request(static_server, "/assets/spreadsheetPreview.worker-CK4ZsbC3.js")
    assert status == 200
    assert headers["cache-control"] == "public, max-age=31536000, immutable"


def test_index_and_root_files_revalidate(static_server: str) -> None:
    status, headers, body = _request(static_server, "/")
    assert status == 200
    assert body == b"<html>app</html>"
    assert headers["cache-control"] == "no-cache"

    status, headers, body = _request(static_server, "/logo.svg")
    assert status == 200
    assert headers["cache-control"] == "no-cache"


def test_spa_fallback_serves_index_with_revalidate(static_server: str) -> None:
    status, headers, body = _request(static_server, "/some/client/route")
    assert status == 200
    assert body == b"<html>app</html>"
    assert headers["cache-control"] == "no-cache"


def test_conditional_request_for_asset_returns_304_with_immutable(static_server: str) -> None:
    _, headers, _ = _request(static_server, "/assets/index-Byh9IHKO.js")
    last_modified = headers["last-modified"]
    assert last_modified

    status, headers, body = _request(
        static_server,
        "/assets/index-Byh9IHKO.js",
        {"If-Modified-Since": last_modified},
    )
    assert status == 304
    assert body == b""
    assert headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("route", ["/", "/chat/", "/chat/new", "/chat/web_probe", "/design/project/session/"])
def test_cold_load_resolves_relative_assets(static_server: str, tmp_path: Path, route: str) -> None:
    (tmp_path / "index.html").write_text(
        '<script type="module" src="./assets/index-Byh9IHKO.js"></script>'
        '<link rel="stylesheet" href="./assets/index-DbwWYrAX.css">',
        encoding="utf-8",
    )
    status, headers, body = _request(static_server, route)
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    references = re.findall(r'(?:src|href)="([^"]+)"', body.decode())
    for reference, expected_type in zip(references, ["text/javascript", "text/css"], strict=True):
        asset_path = urlsplit(urljoin(static_server + route, reference)).path
        status, headers, body = _request(static_server, asset_path + "?v=1")
        assert status == 200
        assert headers["content-type"].startswith(expected_type)
        assert headers["cache-control"] == "public, max-age=31536000, immutable"
        assert b"<script" not in body


@pytest.mark.parametrize("path", ["/assets/missing.js", "/chat/assets/missing.css", "/missing.svg", "/chat/missing.js"])
def test_missing_static_resource_returns_404(static_server: str, path: str) -> None:
    status, _, body = _request(static_server, path)
    assert status == 404
    assert body != b"<html>app</html>"


def test_prefixed_asset_cannot_escape_assets_directory(static_server: str) -> None:
    status, _, body = _request(static_server, "/chat/assets/%2e%2e/logo.svg")
    assert status == 404
    assert body != b"<svg/>"


def test_prefixed_asset_conditional_request(static_server: str) -> None:
    path = "/chat/assets/spreadsheetPreview.worker-CK4ZsbC3.js"
    status, headers, body = _request(static_server, path)
    assert status == 200
    assert body == b"w()"
    status, headers, body = _request(static_server, path, {"If-Modified-Since": headers["last-modified"]})
    assert status == 304
    assert body == b""
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
