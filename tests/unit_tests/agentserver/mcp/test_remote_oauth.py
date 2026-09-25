"""QCC OAuth lifecycle with real loopback callbacks and encrypted storage.

Only the remote HTTPS provider is mocked; never calls QCC production or uses
personal credentials. Run with pytest --asyncio-mode=auto.
"""

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from jiuwenswarm.server.runtime.mcp import remote_oauth as oauth

NAME = "qcc-company"


class Provider:
    def __init__(self):
        self.requests = []
        self.refreshes = 0
        self.revocations = []
        self.fail_refresh = False
        self.metadata_override = {}
        self.before_exchange = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == urlsplit(oauth.QCC_METADATA).path:
            return httpx.Response(
                200,
                json={
                    "resource": oauth.QCC_RESOURCE,
                    "authorization_servers": [oauth.QCC_ISSUER],
                },
            )
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": oauth.QCC_ISSUER,
                    "authorization_endpoint": oauth.QCC_ISSUER + "/oauth/authorize",
                    "token_endpoint": oauth.QCC_ISSUER + "/oauth/token",
                    "registration_endpoint": oauth.QCC_ISSUER + "/oauth/register",
                    "revocation_endpoint": oauth.QCC_ISSUER + "/oauth/revoke",
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    **self.metadata_override,
                },
            )
        if path == "/oauth/register":
            self.registration = json.loads(request.content)
            return httpx.Response(201, json={"client_id": "test-public-client"})
        form = parse_qs(request.content.decode())
        if path == "/oauth/token":
            if form["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                if self.fail_refresh:
                    return httpx.Response(
                        400, json={"error": "invalid_grant", "secret": "do-not-expose"}
                    )
                assert form["refresh_token"] == [f"refresh-{self.refreshes - 1}"]
            else:
                if self.before_exchange:
                    self.before_exchange()
                self.exchange = form
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{self.refreshes}",
                    "refresh_token": f"refresh-{self.refreshes}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp:tools",
                },
            )
        if path == "/oauth/revoke":
            self.revocations.append(form)
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected request: {path}")


@pytest.fixture
def flow(tmp_path):
    provider = Provider()
    manager = oauth.RemoteOAuthManager(
        tmp_path, transport=httpx.MockTransport(provider)
    )
    yield manager, provider
    for name, pending in list(manager.pending.items()):
        manager.cancel(name, pending.id)


def start(manager):
    response = manager.begin(NAME)
    query = parse_qs(urlsplit(response["auth_url"]).query)
    return response, query


def callback(query, **changes):
    params = {"state": query["state"][0], "code": "one-time-code", **changes}
    with httpx.Client(trust_env=False, timeout=5) as client:
        return client.get(query["redirect_uri"][0], params=params)


def authorize(manager):
    response, query = start(manager)
    assert callback(query).status_code == 200
    manager.wait(NAME, response["oauth_session"])
    manager.finish(NAME, response["oauth_session"])
    return response, query


def test_pkce_loopback_and_encrypted_restart(flow, tmp_path):
    manager, provider = flow
    response, query = authorize(manager)
    verifier = provider.exchange["code_verifier"][0]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [challenge]
    assert query["resource"] == [oauth.QCC_RESOURCE]
    assert provider.exchange["redirect_uri"] == query["redirect_uri"]
    assert provider.registration["client_name"] == "WorkSwarm"
    assert provider.registration["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in provider.registration
    assert "access-0" not in json.dumps(response)
    encrypted = (tmp_path / "mcp/credentials/qcc-company-oauth.json").read_text()
    assert "access-0" not in encrypted and "refresh-0" not in encrypted
    restarted = oauth.RemoteOAuthManager(
        tmp_path, transport=httpx.MockTransport(provider)
    )
    assert restarted.access_token(NAME) == "access-0"
    assert provider.refreshes == 0


def test_wrong_state_does_not_consume_legitimate_callback(flow):
    manager, provider = flow
    response, query = start(manager)
    assert callback(query, state="wrong").status_code == 400
    assert not manager.grant(NAME)
    assert callback(query).status_code == 200
    manager.wait(NAME, response["oauth_session"])
    assert len([r for r in provider.requests if r.url.path == "/oauth/token"]) == 1


def test_bad_host_and_duplicate_parameters_are_rejected(flow):
    manager, _ = flow
    _, query = start(manager)
    with httpx.Client(trust_env=False) as client:
        result = client.get(
            query["redirect_uri"][0],
            params={"state": query["state"][0], "code": "a"},
            headers={"Host": "attacker.example"},
        )
        assert result.status_code == 400
        url = (
            query["redirect_uri"][0]
            + "?"
            + urlencode({"state": query["state"][0]})
            + "&code=a&code=b"
        )
        assert client.get(url).status_code == 400
    assert not manager.grant(NAME)


@pytest.mark.parametrize(
    "override",
    [
        {"token_endpoint": "https://attacker.example/token"},
        {"registration_endpoint": "http://127.0.0.1/register"},
        {"issuer": "https://attacker.example"},
        {"code_challenge_methods_supported": ["plain"]},
        {"token_endpoint_auth_methods_supported": ["client_secret_post"]},
    ],
)
def test_discovery_rejects_untrusted_or_incompatible_metadata(flow, override):
    manager, provider = flow
    provider.metadata_override = override
    with pytest.raises(oauth.OAuthError):
        manager.begin(NAME)
    assert not any(r.url.path == "/oauth/register" for r in provider.requests)


def test_denied_authorization_does_not_store_credentials(flow):
    manager, provider = flow
    response, query = start(manager)
    assert callback(query, error="access_denied").status_code == 400
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, response["oauth_session"])
    assert not manager.grant(NAME)
    assert not any(r.url.path == "/oauth/token" for r in provider.requests)


def test_cancel_during_token_exchange_revokes_late_grant(flow):
    manager, provider = flow
    entered, release = threading.Event(), threading.Event()
    provider.before_exchange = lambda: (entered.set(), release.wait(5))
    response, query = start(manager)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(callback, query)
        assert entered.wait(5)
        manager.cancel(NAME, response["oauth_session"])
        release.set()
        assert future.result().status_code == 400
    assert not manager.grant(NAME)
    assert len(provider.revocations) == 1
    with pytest.raises(oauth.OAuthError):
        manager.finish(NAME, response["oauth_session"])


def test_cancel_after_callback_before_live_probe_removes_grant(flow):
    manager, _ = flow
    response, query = start(manager)
    assert callback(query).status_code == 200
    manager.cancel(NAME, response["oauth_session"])
    assert not manager.grant(NAME)
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, response["oauth_session"])


def test_timeout_closes_listener(flow, monkeypatch):
    manager, _ = flow
    monkeypatch.setattr(oauth, "AUTH_TIMEOUT", 0.1)
    response, _ = start(manager)
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, response["oauth_session"])
    assert manager.pending[NAME].server is None
    assert not manager.grant(NAME)


def test_multiple_managers_rotate_refresh_token_once(flow, tmp_path):
    manager, provider = flow
    authorize(manager)
    grant = manager.grant(NAME)
    grant["expires_at"] = 0
    manager._save(NAME, grant)
    managers = [
        oauth.RemoteOAuthManager(tmp_path, transport=httpx.MockTransport(provider))
        for _ in range(6)
    ]
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda m: m.access_token(NAME), managers))
    assert results == ["access-1"] * 6
    assert provider.refreshes == 1
    assert manager.grant(NAME)["refresh_token"] == "refresh-1"


def test_invalid_refresh_requires_reauthorization_without_loop(flow):
    manager, provider = flow
    authorize(manager)
    provider.fail_refresh = True
    with pytest.raises(oauth.OAuthError, match="expired"):
        manager.access_token(NAME, rejected_token="access-0")
    with pytest.raises(oauth.OAuthError, match="required"):
        manager.access_token(NAME)
    assert provider.refreshes == 1


def test_failed_storage_is_reported_not_silent_success(flow, monkeypatch):
    manager, _ = flow
    monkeypatch.setattr(manager.store, "save_token", lambda *_: None)
    response, query = start(manager)
    assert callback(query).status_code == 400
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, response["oauth_session"])


def test_disconnect_revokes_and_removes_encrypted_grant(flow):
    manager, provider = flow
    authorize(manager)
    assert manager.disconnect(NAME)
    assert not manager.grant(NAME)
    assert provider.revocations[0]["token"] == ["refresh-0"]


@pytest.mark.asyncio
async def test_request_retries_401_once_and_never_403(flow):
    manager, provider = flow
    authorize(manager)
    seen = []

    def resource(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(401, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(resource),
        auth=oauth.RemoteOAuthAuth(manager, NAME),
    ) as client:
        response = await client.post(oauth.QCC_RESOURCE, json={"method": "tools/list"})
    assert response.status_code == 401
    assert seen == ["Bearer access-0", "Bearer access-1"]
    assert provider.refreshes == 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403)),
        auth=oauth.RemoteOAuthAuth(manager, NAME),
    ) as client:
        assert (await client.get(oauth.QCC_RESOURCE)).status_code == 403
    assert provider.refreshes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/mcp",
        oauth.QCC_RESOURCE + "?other=1",
        "https://agent.qcc.com/mcp/risk/stream",
    ],
)
async def test_never_sends_token_to_unapproved_resource(flow, url):
    manager, _ = flow
    called = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: called.append(r)),
        auth=oauth.RemoteOAuthAuth(manager, NAME),
    ) as client:
        with pytest.raises(oauth.OAuthError):
            await client.get(url)
    assert not called


def test_replaced_session_cannot_finalize(flow):
    manager, _ = flow
    old, _ = start(manager)
    new, _ = start(manager)
    assert old["oauth_session"] != new["oauth_session"]
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, old["oauth_session"])


@pytest.mark.parametrize(
    "value",
    [
        "https://agent.qcc.com.evil/token",
        "https://u@agent.qcc.com/token",
        "http://agent.qcc.com/token",
        "https://agent.qcc.com/token#x",
        "https://agent.qcc.com/token?secret=x",
    ],
)
def test_endpoint_validation(value):
    with pytest.raises(oauth.OAuthError):
        oauth._endpoint(value)


@pytest.mark.parametrize(
    "override",
    [
        {"access_token": None},
        {"access_token": ""},
        {"access_token": "bad\nheader"},
        {"access_token": "bad\rheader"},
        {"refresh_token": None},
        {"refresh_token": ""},
        {"token_type": "Basic"},
        {"expires_in": "3600"},
        {"expires_in": True},
        {"expires_in": 0},
        {"expires_in": -1},
        {"scope": "other"},
        {"scope": None},
    ],
)
def test_invalid_token_metadata_never_creates_a_grant(flow, override):
    manager, provider = flow

    def invalid_provider(request):
        response = provider(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={**response.json(), **override})
        return response

    manager.transport = httpx.MockTransport(invalid_provider)
    response, query = start(manager)
    result = callback(query)
    assert result.status_code == 400
    assert "bad" not in result.text
    with pytest.raises(oauth.OAuthError):
        manager.wait(NAME, response["oauth_session"])
    assert not manager.grant(NAME)
