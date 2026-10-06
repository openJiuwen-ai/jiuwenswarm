# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""静态资源 Cache-Control 头行为测试。

打包桌面端的前端产物由该静态服务提供；Vite 哈希资产需要 immutable 长缓存
（浏览器跳过条件请求，二次启动免全部资源往返），index.html 与 SPA fallback
必须 no-cache 回源验证。用真实 ThreadingHTTPServer 断言线上路径的头行为。
"""

from __future__ import annotations

import http.client
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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
