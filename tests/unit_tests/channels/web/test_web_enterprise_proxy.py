# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP regression tests for the enterprise User Web proxy."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


@contextmanager
def _serve(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def proxy(monkeypatch):
    requests = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers))
            if self.path == "/api/v1/user-console/user-face-upstream":
                self._authorize()
            elif self.path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                # One complete event followed by an unfinished chunk header.
                self.wfile.write(b"a\r\ndata: ok\n\n\r\n4")
            else:
                body = self.path.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_OPTIONS = do_GET

        def _authorize(self):
            authorization = self.headers["Authorization"]
            status = {
                "Bearer invalid": 401, "Bearer denied": 403, "Bearer error": 500,
            }.get(authorization, 200)
            self.send_response(status)
            if authorization == "Bearer allowed":
                self.send_header("X-Gateway-Http-Upstream", upstream)
                self.send_header("X-Gateway-Ws-Upstream", upstream.replace("http:", "ws:"))
            self.end_headers()

    class Web(_SpaStaticHandler):
        pass

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    with _serve(Upstream) as upstream:
        Web.api_target = upstream
        monkeypatch.setenv("USER_WEB_IDP_TARGET", upstream)
        monkeypatch.setenv("USER_WEB_MANAGER_TARGET", upstream)
        with _serve(Web) as url, httpx.Client(base_url=url, timeout=5, trust_env=False) as client:
            yield client, requests


def test_enterprise_proxy_routes_and_preserves_query(proxy):
    client, requests = proxy
    response = client.get("/idp/v1/auth/me", headers={"x-jiuwen-web-surface": "manager"})
    assert response.text == "/v1/auth/me"
    assert requests[-1][1].get_all("X-Jiuwen-Web-Surface") == ["user"]

    assert client.get("/manager-api/v1/authz/me").status_code == 404
    response = client.get("/manager-api/v1/user-console/agent-contexts?next=/manager-api/")
    assert response.text == "/api/v1/user-console/agent-contexts?next=/manager-api/"

    response = client.get(
        "/gateway-api/v1/chat?next=/gateway-api/",
        headers={"Cookie": "openjiuwen_user_access_token=allowed"},
    )
    assert response.text == "/api/v1/chat?next=/gateway-api/"
    assert requests[-2][1]["Authorization"] == "Bearer allowed"


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer invalid"}, 401),
        ({"Authorization": "Bearer denied"}, 403),
        ({"Authorization": "Bearer incomplete"}, 502),
        ({"Authorization": "Bearer error"}, 503),
    ],
)
def test_enterprise_proxy_stops_when_user_upstream_cannot_be_resolved(proxy, headers, expected_status):
    client, requests = proxy
    response = client.get("/gateway-api/v1/chat", headers=headers)
    assert response.status_code == expected_status
    assert all(path == "/api/v1/user-console/user-face-upstream" for path, _ in requests)


def test_enterprise_proxy_closes_truncated_sse_after_the_last_complete_event(proxy):
    client, _ = proxy
    response = client.get("/gateway-api/stream", headers={"Authorization": "Bearer allowed"})
    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"


def test_personal_proxy_forwards_api_and_options_to_the_original_upstream(proxy, monkeypatch):
    client, requests = proxy
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    assert client.get("/api/v1/chat").text == "/api/v1/chat"
    assert client.options("/api/v1/chat").text == "/api/v1/chat"
    assert [path for path, _ in requests] == ["/api/v1/chat", "/api/v1/chat"]
