"""Desktop remote-MCP OAuth with explicit provider trust and encrypted grants.

The first profile is QCC company. No client secret, token or callback verifier
is persisted in state.json or returned to the renderer. Adding another provider
requires an explicit profile review; discovery is not permission to fetch an
arbitrary URL supplied by a server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import portalocker

QCC_RESOURCE = "https://agent.qcc.com/mcp/company/stream"
QCC_ISSUER = "https://agent.qcc.com"
QCC_METADATA = (
    "https://agent.qcc.com/mcp/.well-known/oauth-protected-resource/company/stream"
)
GRANT_KEY = "remote_oauth_grant"
AUTH_TIMEOUT = 300


class OAuthError(ValueError):
    """User-safe error: never include HTTP bodies, codes or credentials."""


def supports_oauth(name: str, entry: dict | None) -> bool:
    return bool(
        name == "qcc-company"
        and entry
        and entry.get("url") == QCC_RESOURCE
        and entry.get("transport") in {"streamable-http", "streamable_http", "http"}
    )


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise OAuthError("OAuth endpoint is missing.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "agent.qcc.com":
        raise OAuthError("OAuth endpoint is outside the trusted issuer.")
    if parsed.username or parsed.password:
        raise OAuthError("OAuth endpoint is outside the trusted issuer.")
    if parsed.fragment or parsed.query:
        raise OAuthError("OAuth endpoint is outside the trusted issuer.")
    return value


def _json_response(response: httpx.Response) -> dict:
    if not response.is_success:
        raise OAuthError(
            f"OAuth server returned HTTP {response.status_code}. Retry authorization."
        )
    try:
        data = response.json()
    except ValueError:
        raise OAuthError("Invalid OAuth server response.") from None
    if not isinstance(data, dict):
        raise OAuthError("Invalid OAuth server response.")
    return data


@dataclass
class PendingAuthorization:
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    error: str = ""
    consumed: bool = False
    finalized: bool = False
    server: ThreadingHTTPServer | None = None
    timer: threading.Timer | None = None

    def close_listener(self) -> None:
        if self.timer:
            self.timer.cancel()
        server, self.server = self.server, None
        if server:
            # May run inside a request handler. Never block the server thread.
            def close():
                server.shutdown()
                server.server_close()

            threading.Thread(target=close, daemon=True).start()


class RemoteOAuthManager:
    def __init__(self, workspace: Path | None = None, *, transport=None):
        from jiuwenswarm.common.utils import get_workspace_dir
        from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

        self.workspace = workspace or get_workspace_dir()
        self.store = CredentialStore(workspace_dir=self.workspace)
        self.transport = transport
        self.pending: dict[str, PendingAuthorization] = {}
        self.lock = threading.RLock()

    def _client(self):
        return httpx.Client(
            timeout=20, follow_redirects=False, transport=self.transport
        )

    def _grant_lock(self, name: str):
        self._check_name(name)
        root = self.workspace / "mcp" / "credentials"
        root.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(str(root / f".{name}.oauth.lock"), timeout=30)

    @staticmethod
    def _check_name(name: str) -> None:
        if name != "qcc-company":
            raise OAuthError("Remote OAuth is not available for this connector.")

    def grant(self, name: str) -> dict:
        self._check_name(name)
        raw = self.store.get_token(name + "-oauth", GRANT_KEY)
        if not raw:
            return {}
        try:
            grant = json.loads(raw)
            if grant["resource"] != QCC_RESOURCE or grant["issuer"] != QCC_ISSUER:
                raise ValueError
            if not isinstance(grant.get("expires_at"), (int, float)):
                raise TypeError
            for key in ("client_id", "access_token", "refresh_token"):
                if not isinstance(grant.get(key), str):
                    raise TypeError
            _endpoint(grant["token_endpoint"])
            _endpoint(grant["revocation_endpoint"])
            return grant
        except (KeyError, TypeError, ValueError):
            raise OAuthError(
                "Stored OAuth authorization is invalid. Reconnect."
            ) from None

    def _save(self, name: str, grant: dict) -> None:
        raw = json.dumps(grant)
        self.store.save_token(name + "-oauth", GRANT_KEY, raw)
        # CredentialStore's legacy API logs write failures; rotation must fail
        # closed if the replacement refresh token could not be saved.
        if self.store.get_token(name + "-oauth", GRANT_KEY) != raw:
            raise OAuthError("Could not save OAuth credentials. Reconnect.")

    @staticmethod
    def _discover(client: httpx.Client) -> dict:
        resource = _json_response(client.get(QCC_METADATA))
        if resource.get("resource") != QCC_RESOURCE or resource.get(
            "authorization_servers"
        ) != [QCC_ISSUER]:
            raise OAuthError("Unexpected OAuth resource or issuer.")
        meta = _json_response(
            client.get(QCC_ISSUER + "/.well-known/oauth-authorization-server")
        )
        if meta.get("issuer") != QCC_ISSUER or "S256" not in meta.get(
            "code_challenge_methods_supported", []
        ):
            raise OAuthError("OAuth issuer or PKCE S256 is not supported.")
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        ):
            meta[key] = _endpoint(meta.get(key))
        if "none" not in meta.get("token_endpoint_auth_methods_supported", []):
            raise OAuthError("OAuth public clients are not supported.")
        return meta

    @staticmethod
    def _tokens(data: dict, grant: dict) -> dict:
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        lifetime = data.get("expires_in")
        invalid = "OAuth server returned invalid token metadata."
        if not isinstance(access, str) or not access:
            raise OAuthError(invalid)
        if "\n" in access or "\r" in access:
            raise OAuthError(invalid)
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError(invalid)
        if str(data.get("token_type", "")).lower() != "bearer":
            raise OAuthError(invalid)
        if not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool):
            raise OAuthError(invalid)
        if not math.isfinite(lifetime) or lifetime <= 0:
            raise OAuthError(invalid)
        scope = data.get("scope", "mcp:tools")
        if not isinstance(scope, str) or "mcp:tools" not in scope.split():
            raise OAuthError("OAuth authorization did not grant MCP access.")
        return {
            **grant,
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": time.time() + lifetime,
            "scope": scope,
        }

    def complete_authorization(
        self,
        name: str,
        pending: PendingAuthorization,
        query: dict,
        registration: dict,
    ) -> str:
        """Exchange and store one validated callback, sanitizing provider errors."""
        meta = registration
        client_id = registration["client_id"]
        redirect_uri = registration["redirect_uri"]
        error = ""
        grant = None
        try:
            if query.get("error"):
                raise OAuthError("Authorization was denied. Retry when ready.")
            if len(query.get("code", [])) != 1 or not query["code"][0]:
                raise OAuthError("Authorization code is missing.")
            with self._client() as client:
                response = client.post(
                    meta["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": query["code"][0],
                        "redirect_uri": redirect_uri,
                        "code_verifier": pending.verifier,
                        "resource": QCC_RESOURCE,
                    },
                )
                grant = self._tokens(
                    _json_response(response),
                    {
                        "client_id": client_id,
                        "resource": QCC_RESOURCE,
                        "issuer": QCC_ISSUER,
                        "oauth_session": pending.id,
                        "token_endpoint": meta["token_endpoint"],
                        "revocation_endpoint": meta["revocation_endpoint"],
                    },
                )
            with pending.lock:
                if pending.done.is_set():
                    raise OAuthError("Authorization was cancelled or expired.")
                with self._grant_lock(name):
                    try:
                        previous = self.grant(name)
                    except OAuthError:
                        previous = {}
                    self._save(name, grant)
                pending.done.set()
            if previous:
                self._revoke(previous)
        except Exception:  # noqa: BLE001 — sanitize provider/IO errors at the credential boundary
            # Never expose provider responses/exceptions with a code/token.
            error = "Authorization failed, was cancelled, or expired. Please reconnect."
            if grant:
                self._revoke(grant)
            with pending.lock:
                pending.error = error
                pending.done.set()
        finally:
            pending.verifier = ""
            pending.close_listener()
        return error

    def begin(self, name: str) -> dict:
        self._check_name(name)
        # New attempts cancel old listeners. Keep the lock through setup so
        # concurrent starts cannot leak a listener or overwrite a newer flow.
        with self.lock:
            old = self.pending.get(name)
            if old:
                self.cancel(name, old.id)
            pending = PendingAuthorization()
            manager = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass  # request URL contains the one-time authorization code

                # BaseHTTPRequestHandler dispatch requires this exact method name.
                def do_GET(self):  # pylint: disable=huawei-invalid-name
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    expected_host = f"127.0.0.1:{self.server.server_port}"
                    valid = (
                        parsed.path == "/callback"
                        and self.headers.get("Host") == expected_host
                        and len(query.get("state", [])) == 1
                        and secrets.compare_digest(query["state"][0], pending.state)
                    )
                    if not valid:
                        self.reply(400, "Invalid OAuth callback.")
                        return
                    with pending.lock:
                        if pending.done.is_set() or pending.consumed:
                            self.reply(409, "Authorization has already finished.")
                            return
                        pending.consumed = True
                    error = manager.complete_authorization(
                        name,
                        pending,
                        query,
                        {**meta, "client_id": client_id, "redirect_uri": redirect_uri},
                    )
                    self.reply(
                        400 if error else 200,
                        error
                        or "Authorization received. Return to WorkSwarm to finish connecting.",
                    )

                def reply(self, status, text):
                    body = text.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            server.daemon_threads = True
            redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
            try:
                with self._client() as client:
                    meta = self._discover(client)
                    registration = _json_response(
                        client.post(
                            meta["registration_endpoint"],
                            json={
                                "client_name": "WorkSwarm",
                                "redirect_uris": [redirect_uri],
                                "grant_types": ["authorization_code", "refresh_token"],
                                "response_types": ["code"],
                                "token_endpoint_auth_method": "none",
                            },
                        )
                    )
                    client_id = registration.get("client_id")
                    if not isinstance(client_id, str) or not client_id:
                        raise OAuthError(
                            "OAuth registration did not return a client ID."
                        )
            except Exception:  # noqa: BLE001 — sanitize provider/IO errors at the credential boundary
                server.server_close()
                raise OAuthError(
                    "Could not prepare OAuth authorization. Please retry."
                ) from None
            pending.server = server
            self.pending[name] = pending
            threading.Thread(target=server.serve_forever, daemon=True).start()
            pending.timer = threading.Timer(
                AUTH_TIMEOUT, self.cancel, args=(name, pending.id)
            )
            pending.timer.daemon = True
            pending.timer.start()
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(pending.verifier.encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            url = (
                meta["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "scope": "mcp:tools",
                        "state": pending.state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "resource": QCC_RESOURCE,
                    }
                )
            )
            return {
                "type": "auth_required",
                "auth_required": True,
                "name": name,
                "oauth_session": pending.id,
                "auth_url": url,
                "auth_domain": "agent.qcc.com",
                "step_index": 0,
                "steps_total": 1,
            }

    def cancel(self, name: str, session: str) -> None:
        self._check_name(name)
        with self.lock:
            pending = self.pending.get(name)
            if not pending or pending.id != session:
                return
            with pending.lock:
                if not pending.finalized:
                    pending.error = "Authorization cancelled or timed out. Reconnect."
                    pending.done.set()
                    pending.verifier = ""
                    with self._grant_lock(name):
                        try:
                            grant = self.grant(name)
                        except OAuthError:
                            grant = {}
                        if grant.get("oauth_session") == session:
                            self._revoke(grant)
                            self.store.delete_mcp(name + "-oauth")
                pending.close_listener()

    def wait(self, name: str, session: str) -> None:
        self._check_name(name)
        with self.lock:
            pending = self.pending.get(name)
        if not pending or pending.id != session:
            raise OAuthError("Authorization session is no longer active. Reconnect.")
        if not pending.done.wait(AUTH_TIMEOUT + 1):
            self.cancel(name, session)
        with self.lock:
            if self.pending.get(name) is not pending or pending.error:
                raise OAuthError(
                    pending.error or "Authorization was replaced. Reconnect."
                )

    def finish(self, name: str, session: str) -> None:
        """Called only after the live MCP probe. Cancelled flows cannot connect."""
        with self.lock:
            pending = self.pending.get(name)
            if not pending or pending.id != session:
                raise OAuthError("Authorization was cancelled or replaced. Reconnect.")
            if pending.error or not pending.done.is_set():
                raise OAuthError("Authorization was cancelled or replaced. Reconnect.")
            pending.finalized = True

    def access_token(self, name: str, *, rejected_token: str | None = None) -> str:
        with self._grant_lock(name):
            grant = self.grant(name)
            if not grant or grant.get("invalid"):
                raise OAuthError(
                    "OAuth authorization is required. Reconnect the connector."
                )
            access = grant.get("access_token", "")
            # Another process may already have rotated the rejected token.
            if grant.get("expires_at", 0) > time.time() + 30 and (
                rejected_token is None or access != rejected_token
            ):
                return access
            with self._client() as client:
                response = client.post(
                    grant["token_endpoint"],
                    data={
                        "grant_type": "refresh_token",
                        "client_id": grant["client_id"],
                        "refresh_token": grant["refresh_token"],
                        "resource": QCC_RESOURCE,
                    },
                )
                if response.status_code in (400, 401):
                    grant["invalid"] = True
                    self._save(name, grant)
                    raise OAuthError(
                        "OAuth authorization expired. Reconnect the connector."
                    )
                replacement = self._tokens(_json_response(response), grant)
            self._save(name, replacement)
            return replacement["access_token"]

    def _revoke(self, grant: dict) -> bool:
        try:
            with self._client() as client:
                response = client.post(
                    _endpoint(grant["revocation_endpoint"]),
                    data={
                        "client_id": grant["client_id"],
                        "token": grant["refresh_token"],
                        "token_type_hint": "refresh_token",
                    },
                )
            return response.is_success
        except Exception:  # noqa: BLE001 — sanitize provider/IO errors at the credential boundary
            return False

    def disconnect(self, name: str) -> bool:
        self._check_name(name)
        with self.lock:
            pending = self.pending.get(name)
            if pending:
                self.cancel(name, pending.id)
            with self._grant_lock(name):
                try:
                    grant = self.grant(name)
                except OAuthError:
                    grant = {}
                revoked = not grant or self._revoke(grant)
                self.store.delete_mcp(name + "-oauth")
                if self.store.get_token(name + "-oauth", GRANT_KEY):
                    raise OAuthError("Could not remove OAuth credentials.")
                return revoked


class RemoteOAuthAuth(httpx.Auth):
    """Per-request refresh and at most one retry after a remote 401."""

    requires_request_body = True

    def __init__(self, manager: RemoteOAuthManager, name: str):
        self.manager = manager
        self.name = name

    async def async_auth_flow(self, request):
        if str(request.url) != QCC_RESOURCE:
            raise OAuthError("Refusing to send OAuth credentials to another resource.")
        token = await asyncio.to_thread(self.manager.access_token, self.name)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            await response.aread()
            token = await asyncio.to_thread(
                self.manager.access_token, self.name, rejected_token=token
            )
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


_manager: RemoteOAuthManager | None = None
_manager_lock = threading.Lock()


def oauth_manager() -> RemoteOAuthManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = RemoteOAuthManager()
        return _manager
