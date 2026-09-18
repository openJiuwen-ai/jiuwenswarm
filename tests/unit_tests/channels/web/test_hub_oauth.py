import json
import sys
import threading
import types
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import pytest

from jiuwenswarm.channels.web import hub_oauth
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


def test_hub_login_round_trip_with_mock_session(monkeypatch):
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "https://hub.example")
    hub_oauth._attempts.clear()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=None):
            return json.dumps({"data": {"provider": "gitcode", "access_token": "user-token", "user": {"login": "tester"}}}).encode()

    def mock_open(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(hub_oauth, "urlopen", mock_open)
    status, started = hub_oauth.start("gitcode", "127.0.0.1:6173")
    assert status == 200
    authorize = urlparse(started["authorize_url"])
    assert authorize.path == "/api/v1/auth/oauth/gitcode/start"
    return_uri = parse_qs(authorize.query)["redirect_to"][0]
    assert urlparse(return_uri).path == "/oauth/hub/callback"
    flow = parse_qs(urlparse(return_uri).query)["flow"][0]
    assert flow == started["flow"]

    status, result = hub_oauth.complete({"flow": flow, "oauth_session": "one-time", "oauth_provider": "gitcode"})
    assert (status, result) == (200, {"status": "complete"})
    assert requests[0].full_url == "https://hub.example/api/v1/auth/oauth/gitcode/session"
    assert json.loads(requests[0].data) == {"session": "one-time"}
    assert hub_oauth.result(flow, "wrong-secret") == (404, {"error": "unknown_flow"})
    assert hub_oauth.result(flow, started["claim"]) == (200, {"status": "complete", "provider": "gitcode", "access_token": "user-token", "user": {"login": "tester"}})
    assert hub_oauth.result(flow, started["claim"]) == (404, {"error": "unknown_flow"})


def test_hub_login_rejects_unrelated_callback_without_consuming_attempt(monkeypatch):
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "https://hub.example")
    hub_oauth._attempts.clear()
    status, started = hub_oauth.start("github", "127.0.0.1:6173")
    assert status == 200
    assert hub_oauth.complete({"flow": started["flow"], "oauth_session": "one-time", "oauth_provider": "gitcode"})[0] == 400
    assert hub_oauth.result(started["flow"], started["claim"]) == (200, {"status": "pending"})
    assert hub_oauth.start("gitcode", "evil.example")[0] == 400


def test_localhost_is_normalized_to_documented_loopback_address(monkeypatch):
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "https://hub.example")
    status, started = hub_oauth.start("gitcode", "localhost:6173")
    assert status == 200
    redirect_to = parse_qs(urlparse(started["authorize_url"]).query)["redirect_to"][0]
    assert urlparse(redirect_to).netloc == "127.0.0.1:6173"


def test_explicit_hub_test_environment_is_usable_without_enabling_arbitrary_http(monkeypatch):
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "http://119.8.233.112:8080")
    status, started = hub_oauth.start("github", "127.0.0.1:6173")
    assert status == 200
    assert urlparse(started["authorize_url"]).netloc == "119.8.233.112:8080"
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "http://example.com:8080")
    assert hub_oauth.start("github", "127.0.0.1:6173")[0] == 400


def test_oauth_uses_the_same_configured_hub_as_publishing(monkeypatch):
    monkeypatch.delenv("SKILLHUB_OAUTH_BASE_URL", raising=False)
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "http://119.8.233.112:8080")
    status, started = hub_oauth.start("gitcode", "127.0.0.1:6173")
    assert status == 200
    assert urlparse(started["authorize_url"]).netloc == "119.8.233.112:8080"


def test_test_hub_session_exchange_does_not_use_environment_proxy(monkeypatch):
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "http://119.8.233.112:8080")
    status, started = hub_oauth.start("gitcode", "127.0.0.1:6173")
    assert status == 200

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=None):
            return json.dumps({"data": {"access_token": "test-token", "user": {"login": "tester"}}}).encode()

    class DirectOpener:
        def open(self, request, timeout):
            assert request.full_url == "http://119.8.233.112:8080/api/v1/auth/oauth/gitcode/session"
            assert timeout == 15
            return Response()

    def direct_opener(handler):
        assert handler.proxies == {}
        return DirectOpener()

    monkeypatch.setattr(hub_oauth, "build_opener", direct_opener, raising=False)
    monkeypatch.setattr(hub_oauth, "urlopen", lambda *_args, **_kwargs: pytest.fail("environment proxy used"))
    assert hub_oauth.complete({"flow": started["flow"], "oauth_session": "test-ticket"}) == (200, {"status": "complete"})
    assert hub_oauth.result(started["flow"], started["claim"])[1]["access_token"] == "test-token"


@pytest.mark.parametrize("provider", ["gitcode", "github"])
def test_mock_hub_redirect_reaches_local_callback_and_delivers_token(monkeypatch, tmp_path, provider):
    hub_oauth._attempts.clear()

    class MockHub(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            assert parsed.path == f"/api/v1/auth/oauth/{provider}/start"
            return_uri = parse_qs(parsed.query)["redirect_to"][0]
            separator = "&" if "?" in return_uri else "?"
            self.send_response(302)
            self.send_header("Location", return_uri + separator + f"oauth_session=mock-ticket&oauth_provider={provider}")
            self.end_headers()

        def do_POST(self):  # noqa: N802
            assert self.path == f"/api/v1/auth/oauth/{provider}/session"
            assert json.loads(self.rfile.read(int(self.headers["Content-Length"]))) == {"session": "mock-ticket"}
            body = json.dumps({"data": {"provider": provider, "access_token": "mock-token", "user": {"login": "alice"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    mock_hub = ThreadingHTTPServer(("127.0.0.1", 0), MockHub)
    local = ThreadingHTTPServer(("127.0.0.1", 0), partial(_SpaStaticHandler, directory=str(tmp_path)))
    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", f"http://127.0.0.1:{mock_hub.server_port}")
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (mock_hub, local)]
    for thread in threads:
        thread.start()
    try:
        origin = f"http://127.0.0.1:{local.server_port}"
        request = Request(origin + "/marketplace-oauth/hub/start", json.dumps({"provider": provider}).encode(), {"Content-Type": "application/json"})
        with urlopen(request) as response:
            started = json.load(response)
        with urlopen(started["authorize_url"]) as response:
            assert response.url == origin + "/oauth/hub/done"
            assert response.status == 200
        request = Request(origin + "/marketplace-oauth/hub/result", json.dumps({"flow": started["flow"], "claim": started["claim"]}).encode(), {"Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        assert result["access_token"] == "mock-token"
        assert result["provider"] == provider
        assert result["user"]["login"] == "alice"
    finally:
        for server in (local, mock_hub):
            server.shutdown()
            server.server_close()


def test_hub_done_page_requests_close_with_manual_fallback(tmp_path):
    local = ThreadingHTTPServer(("127.0.0.1", 0), partial(_SpaStaticHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=local.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{local.server_port}/oauth/hub/done") as response:
            page = response.read().decode("utf-8")
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
        assert "window.close()" in page
        assert "手动关闭" in page
    finally:
        local.shutdown()
        local.server_close()


def test_desktop_opens_only_hub_authorization_in_system_browser(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    from jiuwenswarm.channels.desktop import desktop_app

    monkeypatch.setenv("SKILLHUB_OAUTH_BASE_URL", "https://hub.example")
    opened = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda url: opened.append(url) or True)
    api = desktop_app._WindowApi(None)
    assert api.open_external_url("https://hub.example/api/v1/auth/oauth/gitcode/start?redirect_to=x") is True
    assert api.open_external_url("https://evil.example/api/v1/auth/oauth/gitcode/start") is False
    assert api.open_external_url("https://hub.example/other") is False
    assert len(opened) == 1


def test_desktop_browser_uses_publishing_hub_when_oauth_override_is_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    from jiuwenswarm.channels.desktop import desktop_app

    monkeypatch.delenv("SKILLHUB_OAUTH_BASE_URL", raising=False)
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "http://119.8.233.112:8080")
    monkeypatch.setattr(desktop_app.webbrowser, "open", lambda _url: True)
    api = desktop_app._WindowApi(None)
    assert api.open_external_url("http://119.8.233.112:8080/api/v1/auth/oauth/github/start?redirect_to=x") is True
